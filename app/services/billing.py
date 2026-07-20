"""Billing: price resolution, pre-flight gate, and the atomic per-download charge.

Money is cents everywhere. `charge_and_log` logs the download AND moves money for
the user's billing mode in ONE transaction, so a download can never be delivered
without being accounted for (or vice-versa)."""
from ..db import pool
from .. import config, i18n
from ..repo import wallet
from ..repo import settings as settings_repo

VIP_DISCOUNT_KEY = "vip_price_cents"


def birr(cents: int) -> str:
    cents = int(cents or 0)
    return f"{cents / 100:.2f}".rstrip("0").rstrip(".") + " Birr"


async def global_price_cents() -> int:
    v = await settings_repo.get("global_price_cents")
    return int(v) if v is not None else config.GLOBAL_PRICE_CENTS


async def free_mode() -> bool:
    """Global 'all downloads free' switch."""
    return await settings_repo.get_bool("free_mode", False)


async def price_for(user: dict) -> int:
    if await free_mode():
        return 0
    if user.get("is_vip"):
        vip = await settings_repo.get(VIP_DISCOUNT_KEY)
        if vip is not None:
            return int(vip)
    if user.get("price_override_cents") is not None:
        return int(user["price_override_cents"])
    return await global_price_cents()


async def today_count(user_id: int) -> int:
    return await pool().fetchval(
        "SELECT count(*)::int FROM downloads WHERE user_id=$1 AND day=current_date", user_id
    )


async def can_download(user: dict) -> tuple[bool, str, int]:
    """(ok, reason, price_cents). Pre-flight — run before sending the OTP."""
    price = await price_for(user)
    mode = user["billing_mode"]
    bonus = user.get("bonus_balance_cents", 0)   # spendable bonus wallet (0 for legacy users)
    if mode == "prepaid":
        if bonus + user["balance_cents"] < price:
            return False, i18n.t("reason_insufficient", need=birr(price), have=birr(bonus + user["balance_cents"])), price
    elif mode == "postpaid":
        purchasing_power = bonus + user["balance_cents"] + user["credit_limit_cents"] - user["owed_cents"]
        if purchasing_power < price:
            return False, i18n.t("reason_postpaid_limit", need=birr(price)), price
    else:  # counter
        uid = user["telegram_id"]
        if user["total_limit"] > 0:
            total = await pool().fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1", uid)
            if total >= user["total_limit"]:
                return False, i18n.t("reason_total_limit"), price
        if user["daily_limit"] > 0 and await today_count(uid) >= user["daily_limit"]:
            return False, i18n.t("reason_daily_limit"), price
    return True, "", price


async def charge_and_log(user_id: int, price_cents: int, mode: str, fan_hash: str, fmt: str = "pdf") -> dict:
    """Atomically record the download and apply the charge for this billing mode.
    Returns {mode, charged, balance, owed} so the caller can show a deduction line.

    The whole thing runs under a SINGLE up-front user-row lock, so concurrent charges
    for the same user serialize cleanly (no lock-ordering deadlock). Bonus wallet is
    spent first, then balance; any shortfall is booked as `owed_cents`. Balance and
    bonus can never go negative and no cent is ever created or lost."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            # Acquire the exclusive row lock FIRST (before any other statement) so every
            # concurrent charge for this user takes the same lock in the same order.
            urow = await conn.fetchrow(
                "SELECT balance_cents, bonus_balance_cents, owed_cents FROM users WHERE telegram_id=$1 FOR UPDATE",
                user_id,
            )
            dl_id = await conn.fetchval(
                "INSERT INTO downloads (user_id, fan_hash, format, cost_cents) VALUES ($1,$2,$3,$4) RETURNING id",
                user_id, fan_hash, fmt, price_cents,
            )
            charged = from_bonus = 0
            balance = owed = bonus_balance = None
            if price_cents > 0 and mode in ("prepaid", "postpaid") and urow:
                bal, bonus, owed = urow["balance_cents"], urow["bonus_balance_cents"], urow["owed_cents"]
                from_bonus = min(bonus, price_cents)                    # bonus wallet first
                remaining = price_cents - from_bonus
                from_balance = min(bal, remaining) if remaining > 0 else 0
                shortfall = remaining - from_balance                    # → owed (postpaid / over-spend)
                bonus_balance = bonus - from_bonus
                balance = bal - from_balance
                owed = owed + shortfall
                await conn.execute(
                    "UPDATE users SET bonus_balance_cents=$1, balance_cents=$2, owed_cents=$3, updated_at=now() "
                    "WHERE telegram_id=$4",
                    bonus_balance, balance, owed, user_id,
                )
                # audit rows (balance_after = final main balance keeps the ledger invariant)
                if from_bonus > 0:
                    await conn.execute(
                        "INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id) "
                        "VALUES ($1,'debit',$2,$3,'download_bonus','download',$4)", user_id, from_bonus, balance, dl_id)
                if from_balance > 0:
                    await conn.execute(
                        "INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id) "
                        "VALUES ($1,'debit',$2,$3,'download','download',$4)", user_id, from_balance, balance, dl_id)
                charged = price_cents   # what the download cost (bonus + balance + any booked debt)
            # counter: no money movement
            return {"mode": mode, "charged": charged, "from_bonus": from_bonus,
                    "balance": balance, "bonus_balance": bonus_balance, "owed": owed}
