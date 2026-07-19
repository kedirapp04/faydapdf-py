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


async def price_for(user: dict) -> int:
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
    if mode == "prepaid":
        if user["balance_cents"] < price:
            return False, i18n.t("reason_insufficient", need=birr(price), have=birr(user["balance_cents"])), price
    elif mode == "postpaid":
        purchasing_power = user["balance_cents"] + user["credit_limit_cents"] - user["owed_cents"]
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


async def charge_and_log(user_id: int, price_cents: int, mode: str, fan_hash: str, fmt: str = "pdf") -> None:
    """Atomically record the download and apply the charge for this billing mode.

    For prepaid, the whole thing runs under the user-row lock: we bill only what's
    actually in the balance and book any shortfall (from a concurrent spend) as
    `owed_cents` — so the balance can never go negative and no money is lost."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            dl_id = await conn.fetchval(
                "INSERT INTO downloads (user_id, fan_hash, format, cost_cents) VALUES ($1,$2,$3,$4) RETURNING id",
                user_id, fan_hash, fmt, price_cents,
            )
            if price_cents > 0 and mode in ("prepaid", "postpaid"):
                # Lock the row and read the live balance; another device may have
                # spent since the pre-flight gate. Debit up to the balance, no more.
                row = await conn.fetchrow(
                    "SELECT balance_cents FROM users WHERE telegram_id=$1 FOR UPDATE", user_id
                )
                bal = row["balance_cents"] if row else 0
                pay_now = min(bal, price_cents) if bal > 0 else 0
                shortfall = price_cents - pay_now
                if pay_now > 0:
                    await wallet.debit(conn, user_id, pay_now, "download", ref_type="download", ref_id=dl_id)
                if shortfall > 0:
                    await conn.execute(
                        "UPDATE users SET owed_cents = owed_cents + $1, updated_at=now() WHERE telegram_id=$2",
                        shortfall, user_id,
                    )
            # counter: no money movement
