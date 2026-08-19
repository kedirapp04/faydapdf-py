"""Billing: price resolution, pre-flight gate, and the atomic per-download charge.

Money is cents everywhere. `charge_and_log` logs the download AND moves money for
the user's billing mode in ONE transaction, so a download can never be delivered
without being accounted for (or vice-versa)."""
import re

from ..db import pool
from .. import config, i18n
from ..repo import wallet
from ..repo import settings as settings_repo

VIP_DISCOUNT_KEY = "vip_price_cents"


def birr(cents: int) -> str:
    cents = int(cents or 0)
    return f"{cents / 100:.2f}".rstrip("0").rstrip(".") + " Birr"


# ── top-up bonus tiers (admin-editable via /topupbonus) ───────────────────────
# When a user's approved top-up reaches a tier, grant that % to the BONUS wallet
# (spent before the normal balance). A tier is (min_birr, pct); the % applied is the
# HIGHEST tier whose min_birr <= the top-up amount. Below the lowest tier → no bonus.
DEFAULT_TOPUP_BONUS_TIERS = [(200, 10), (500, 15), (1000, 20), (2000, 25)]
TOPUP_BONUS_KEY = "topup_bonus_tiers"
TOPUP_BONUS_ENABLED_KEY = "topup_bonus_enabled"


async def topup_bonus_enabled() -> bool:
    """Master on/off for the top-up bonus (kept separate from the tier values so toggling
    off doesn't erase them). Default ON."""
    return await settings_repo.get_bool(TOPUP_BONUS_ENABLED_KEY, True)


async def set_topup_bonus_enabled(on: bool) -> None:
    await settings_repo.set_bool(TOPUP_BONUS_ENABLED_KEY, bool(on))


def _parse_tiers(spec: str) -> list:
    """'min:pct min:pct …' (space/comma separated) → sorted [(min_birr, pct)]. Bad
    entries are skipped; empty/None → [] (bonus disabled)."""
    out = []
    for part in re.split(r"[,\s]+", (spec or "").strip()):
        if not part:
            continue
        try:
            m_s, p_s = part.split(":")
            m, p = int(float(m_s)), int(float(p_s))
        except (ValueError, TypeError):
            continue
        if m >= 0 and 0 <= p <= 100:
            out.append((m, p))
    return sorted(set(out))


def _fmt_tiers(tiers: list) -> str:
    return " ".join(f"{m}:{p}" for m, p in tiers)


async def topup_bonus_tiers() -> list:
    """Admin-set tiers, or the default promo if never configured. An explicit '' disables."""
    v = await settings_repo.get(TOPUP_BONUS_KEY)
    if v is None:
        return list(DEFAULT_TOPUP_BONUS_TIERS)
    return _parse_tiers(v)


async def set_topup_bonus_tiers(spec: str) -> list:
    tiers = _parse_tiers(spec)
    await settings_repo.set(TOPUP_BONUS_KEY, _fmt_tiers(tiers))
    return tiers


async def topup_bonus_cents(amount_cents: int) -> int:
    """Bonus (cents) for an approved top-up: amount × (highest tier ≤ amount)%. 0 if the
    bonus is off, the amount is 0, or no tier is reached."""
    amount_cents = int(amount_cents or 0)
    if amount_cents <= 0 or not await topup_bonus_enabled():
        return 0
    pct = 0
    for min_birr, tier_pct in await topup_bonus_tiers():   # ascending
        if amount_cents >= min_birr * 100:
            pct = tier_pct
    return amount_cents * pct // 100


async def topup_bonus_lines() -> str:
    """Language-neutral tier list for the add-balance screen / admin view (as ranges).
    Empty string when the bonus is disabled."""
    tiers = await topup_bonus_tiers()
    if not tiers:
        return ""
    lines = []
    for i, (m, p) in enumerate(tiers):
        high = tiers[i + 1][0] - 1 if i + 1 < len(tiers) else None
        rng = f"{m}–{high}" if high is not None else f"{m}+"
        lines.append(f"  • {rng} Birr → +{p}%")
    return "\n".join(lines)


async def global_price_cents() -> int:
    v = await settings_repo.get("global_price_cents")
    return int(v) if v is not None else config.GLOBAL_PRICE_CENTS


async def free_mode() -> bool:
    """Global 'all downloads free' switch."""
    return await settings_repo.get_bool("free_mode", False)


async def new_price_cents() -> int:
    """Price for the NEW wallet (money topped up after the price change). Unset → same as
    the old price, so behaviour is unchanged until an admin sets it."""
    v = await settings_repo.get("new_price_cents")
    return int(v) if v is not None else await global_price_cents()


async def price_for(user: dict) -> int:
    if await free_mode():
        return 0
    if user.get("is_vip"):
        vip = await settings_repo.get(VIP_DISCOUNT_KEY)
        if vip is not None:
            return int(vip)
    if user.get("price_override_cents") is not None:
        return int(user["price_override_cents"])
    # Two-tier global price. While ANY old-wallet money is left the download is charged at
    # the OLD price — even if that balance can't cover it alone, in which case the rest is
    # topped up from the new wallet (still at the old price). Only once the old wallet hits
    # zero does the NEW price apply.
    old = await global_price_cents()
    new = await new_price_cents()
    if new == old:
        return old
    return old if int(user.get("balance_cents") or 0) > 0 else new


async def display_price_for(user: dict) -> int:
    """The price to SHOW a user — their personal price if they have one (free mode / VIP /
    per-user override), otherwise the NEW (list) price. Someone still holding old-wallet
    balance is charged the OLD price, i.e. they simply pay less than advertised."""
    if await free_mode():
        return 0
    if user.get("is_vip"):
        vip = await settings_repo.get(VIP_DISCOUNT_KEY)
        if vip is not None:
            return int(vip)
    if user.get("price_override_cents") is not None:
        return int(user["price_override_cents"])
    return await new_price_cents()


async def today_count(user_id: int) -> int:
    return await pool().fetchval(
        "SELECT count(*)::int FROM downloads WHERE user_id=$1 AND day=current_date", user_id
    )


async def can_download(user: dict) -> tuple[bool, str, int]:
    """(ok, reason, price_cents). Pre-flight — run before sending the OTP."""
    price = await price_for(user)
    mode = user["billing_mode"]
    bonus = user.get("bonus_balance_cents", 0)   # spendable bonus wallet (0 for legacy users)
    funds = bonus + user["balance_cents"] + user.get("balance_new_cents", 0)   # both price tiers
    if mode == "prepaid":
        if funds < price:
            return False, i18n.t("reason_insufficient", need=birr(price), have=birr(funds)), price
    elif mode == "postpaid":
        purchasing_power = funds + user["credit_limit_cents"] - user["owed_cents"]
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


async def refund_download(user_id: int, charge: dict) -> bool:
    """Undo a charge whose download was never delivered (e.g. the Telegram upload failed).

    Puts every cent back in the SAME wallet it came from, clears any debt that was booked,
    and deletes the download row so counters/limits aren't consumed either — all in one
    transaction under the user-row lock, exactly like charge_and_log. Returns True if
    anything was reversed."""
    if not charge or not charge.get("charged"):
        return False
    from_bonus = int(charge.get("from_bonus") or 0)
    from_balance = int(charge.get("from_balance") or 0)
    from_new = int(charge.get("from_new") or 0)
    shortfall = int(charge.get("shortfall") or 0)
    dl_id = charge.get("download_id")
    if not (from_bonus or from_balance or from_new or shortfall):
        return False
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT balance_cents, balance_new_cents, bonus_balance_cents, owed_cents "
                "FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)
            if row is None:
                return False
            balance = row["balance_cents"] + from_balance
            balance_new = row["balance_new_cents"] + from_new
            bonus_balance = row["bonus_balance_cents"] + from_bonus
            owed = max(0, row["owed_cents"] - shortfall)      # un-book the debt we created
            await conn.execute(
                "UPDATE users SET balance_cents=$1, balance_new_cents=$2, bonus_balance_cents=$3, "
                "owed_cents=$4, updated_at=now() WHERE telegram_id=$5",
                balance, balance_new, bonus_balance, owed, user_id)
            await conn.execute(
                "INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id) "
                "VALUES ($1,'credit',$2,$3,'refund_undelivered','download',$4)",
                user_id, from_bonus + from_balance + from_new + shortfall, balance, dl_id)
            if dl_id:      # the download never happened — don't let it eat a counter slot
                await conn.execute("DELETE FROM downloads WHERE id=$1", int(dl_id))
    return True


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
                "SELECT balance_cents, balance_new_cents, bonus_balance_cents, owed_cents "
                "FROM users WHERE telegram_id=$1 FOR UPDATE",
                user_id,
            )
            dl_id = await conn.fetchval(
                "INSERT INTO downloads (user_id, fan_hash, format, cost_cents) VALUES ($1,$2,$3,$4) RETURNING id",
                user_id, fan_hash, fmt, price_cents,
            )
            charged = from_bonus = from_balance = from_new = shortfall = 0
            balance = owed = bonus_balance = balance_new = None
            if price_cents > 0 and mode in ("prepaid", "postpaid") and urow:
                bal, bal_new = urow["balance_cents"], urow["balance_new_cents"]
                bonus, owed = urow["bonus_balance_cents"], urow["owed_cents"]
                # Spend order: bonus (free) → OLD-price wallet → NEW-price wallet → owed.
                # Draining the old wallet first is what retires the old price naturally.
                from_bonus = min(bonus, price_cents)
                remaining = price_cents - from_bonus
                from_balance = min(bal, remaining) if remaining > 0 else 0
                remaining -= from_balance
                from_new = min(bal_new, remaining) if remaining > 0 else 0
                shortfall = remaining - from_new                        # → owed (postpaid / over-spend)
                bonus_balance = bonus - from_bonus
                balance = bal - from_balance
                balance_new = bal_new - from_new
                owed = owed + shortfall
                await conn.execute(
                    "UPDATE users SET bonus_balance_cents=$1, balance_cents=$2, balance_new_cents=$3, "
                    "owed_cents=$4, updated_at=now() WHERE telegram_id=$5",
                    bonus_balance, balance, balance_new, owed, user_id,
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
                if from_new > 0:
                    await conn.execute(
                        "INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id) "
                        "VALUES ($1,'debit',$2,$3,'download_new','download',$4)", user_id, from_new, balance, dl_id)
                charged = price_cents   # what the download cost (bonus + both wallets + any booked debt)
            # counter: no money movement
            return {"mode": mode, "charged": charged, "from_bonus": from_bonus,
                    "balance": balance, "balance_new": balance_new,
                    "bonus_balance": bonus_balance, "owed": owed,
                    # exact per-wallet breakdown + the download row, so a failed
                    # delivery can be reversed to the cent (see refund_download)
                    "from_balance": from_balance, "from_new": from_new,
                    "shortfall": shortfall, "download_id": dl_id}
