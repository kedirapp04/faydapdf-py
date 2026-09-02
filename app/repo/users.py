"""User records."""
from ..db import pool
from . import wallet
from . import settings as settings_repo

DEFAULT_WELCOME_BONUS_CENTS = 2000   # 20 Birr, admin-overridable via settings key


async def get(user_id) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM users WHERE telegram_id = $1", int(user_id))
    return dict(row) if row else None


async def _welcome_bonus_cents() -> int:
    v = await settings_repo.get("welcome_bonus_cents")   # cached read
    if v is not None and str(v).lstrip("-").isdigit():
        return max(0, int(v))
    return DEFAULT_WELCOME_BONUS_CENTS


async def ensure(user_id, username: str | None = None) -> dict:
    """Create the user if new (active), else keep the username in sync. A brand-new
    user is granted the welcome bonus INTO THE BONUS WALLET (one-time, only on the
    real INSERT — existing users are never re-granted).

    The common case (an EXISTING user) is a single round-trip; only a first-ever
    insert pays the extra bonus transaction."""
    uid = int(user_id)
    row = await pool().fetchrow(
        """INSERT INTO users (telegram_id, username, billing_mode)
           VALUES ($1, $2, 'prepaid')
           ON CONFLICT (telegram_id) DO UPDATE
             SET username = COALESCE(EXCLUDED.username, users.username)
           RETURNING *, (xmax = 0) AS _inserted""",
        uid, username,
    )
    if row["_inserted"]:                       # xmax=0 ⇒ this call inserted the row
        wb = await _welcome_bonus_cents()
        if wb > 0:
            async with pool().acquire() as conn:
                async with conn.transaction():
                    await wallet.credit_bonus(conn, uid, wb, reason="welcome_bonus")
            row = await pool().fetchrow("SELECT * FROM users WHERE telegram_id=$1", uid)
    d = dict(row)
    d.pop("_inserted", None)
    return d


async def set_status(user_id, status: str) -> None:
    await pool().execute(
        "UPDATE users SET status=$1, approved_at=CASE WHEN $1='active' AND approved_at IS NULL THEN now() ELSE approved_at END, updated_at=now() WHERE telegram_id=$2",
        status, int(user_id),
    )


async def set_billing_mode(user_id, mode: str) -> None:
    await pool().execute("UPDATE users SET billing_mode=$1, updated_at=now() WHERE telegram_id=$2", mode, int(user_id))


async def set_price_override(user_id, cents: int | None) -> None:
    await pool().execute("UPDATE users SET price_override_cents=$1, updated_at=now() WHERE telegram_id=$2", cents, int(user_id))


async def set_vip(user_id, is_vip: bool) -> None:
    await pool().execute("UPDATE users SET is_vip=$1, updated_at=now() WHERE telegram_id=$2", is_vip, int(user_id))


async def set_delivery_pref(user_id, pref: str) -> None:
    if pref not in ("both", "pdf", "screenshot"):
        pref = "both"
    await pool().execute("UPDATE users SET delivery_pref=$1, updated_at=now() WHERE telegram_id=$2", pref, int(user_id))


async def set_credit_limit(user_id, cents: int) -> None:
    await pool().execute("UPDATE users SET credit_limit_cents=$1, updated_at=now() WHERE telegram_id=$2", max(0, cents), int(user_id))


async def list_by_status(status: str, limit: int = 50, offset: int = 0) -> list[dict]:
    rows = await pool().fetch(
        "SELECT * FROM users WHERE status=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        status, limit, offset,
    )
    return [dict(r) for r in rows]


async def count() -> int:
    return await pool().fetchval("SELECT count(*)::int FROM users")


# ── money cohorts ───────────────────────────────────────────────────────────────
# Defined ONCE here and used by both the Users filter and the broadcast segments, so a
# filter always selects exactly the users counted on the Money tab. Keep these rules in
# step with _BONUS_SPLIT_SQL in app/web.py.
#
# The catch these encode: bonus reached users two different ways. Legacy (the 50/15-Birr
# era) was credited INTO balance_cents; everything since goes to the separate
# bonus_balance_cents wallet. So "real money" = balance minus whatever legacy grant is
# still sitting in the OLD wallet, and a user who never paid holds no real money at all.
SQL_WALLET_GRANT = ("COALESCE((SELECT sum(l.amount_cents) FROM wallet_ledger l "
                    "WHERE l.user_id = users.telegram_id AND l.kind = 'credit' "
                    "AND l.reason IN ('welcome_bonus','topup_bonus','bonus')), 0)")
SQL_HAS_PAID = ("EXISTS (SELECT 1 FROM payments p WHERE p.user_id = users.telegram_id "
                "AND p.status = 'approved')")
SQL_LEGACY_LEFT = f"LEAST(balance_cents, GREATEST(bonus_cents - {SQL_WALLET_GRANT}, 0))"
SQL_REAL_MONEY = f"(balance_cents + balance_new_cents - {SQL_LEGACY_LEFT})"
SQL_ANY_BALANCE = "(balance_cents + balance_new_cents) > 0"

MONEY_COHORTS: dict[str, str] = {
    # The three balance-view cohorts: users holding a positive balance in each basis.
    "has_gross":    "(balance_cents + balance_new_cents + bonus_balance_cents) > 0",  # incl. bonus
    "has_nowallet": "(balance_cents + balance_new_cents) > 0",                        # without bonus wallet
    "has_net":      f"({SQL_HAS_PAID} AND {SQL_REAL_MONEY} > 0)",                     # without any bonus
    "old_balance":  "balance_cents > 0",
    "new_balance":  "balance_new_cents > 0",
    "both_wallets": "balance_cents > 0 AND balance_new_cents > 0",
    "real_money":   f"{SQL_HAS_PAID} AND {SQL_REAL_MONEY} > 0",
    "free_money":   f"NOT {SQL_HAS_PAID} AND {SQL_ANY_BALANCE}",
    "spent_out":    f"{SQL_HAS_PAID} AND {SQL_ANY_BALANCE} AND {SQL_REAL_MONEY} <= 0",
    "bonus_wallet": "bonus_balance_cents > 0",
    "in_debt":      "owed_cents > 0",
    "empty":        f"NOT {SQL_ANY_BALANCE} AND bonus_balance_cents = 0",
}

# Three balance bases the admin can sort / filter / total by. All are interpolated
# into SQL, so they MUST come from these constants, never from raw input.
#   gross    — everything spendable: old + new + separate bonus wallet
#   nowallet — without the separate bonus wallet (old + new)
#   net      — no bonus at all (real money): the legacy bonus baked into balance is
#              removed, and a user who never paid counts as 0 (their whole balance is
#              promotional). Matches the dashboard "Net balance (no bonus)" tile
#              exactly (verified: same per-user formula).
_BAL_GROSS = "(balance_cents + balance_new_cents + bonus_balance_cents)"
_BAL_NOWALLET = "(balance_cents + balance_new_cents)"
_BAL_NET = f"(CASE WHEN {SQL_HAS_PAID} THEN {SQL_REAL_MONEY} ELSE 0 END)"
BALANCE_BASIS: dict[str, str] = {
    "gross":    _BAL_GROSS,
    "nowallet": _BAL_NOWALLET,
    "net":      _BAL_NET,
}

# Non-balance sort orders (whitelisted). Balance sorts are built from the chosen
# basis at query time (see page()).
SORT_ORDERS = {
    "created":   "created_at DESC",
    "downloads": "downloads_count DESC, created_at DESC",
}


async def page(status: str | None, q: str | None, limit: int, offset: int,
               is_vip: bool | None = None, mode: str | None = None,
               bonus: str | None = None, money: str | None = None,
               sort: str | None = None,
               bal_min: int | None = None, bal_max: int | None = None,
               basis: str | None = None) -> tuple[list[dict], int, int]:
    """Paginated + optional filters (status / VIP / billing mode / bonus / money cohort
    / balance range) + search (username or id) + sort. `basis` chooses which balance
    the range filter, the balance sort AND the summed total use — gross (incl. bonus),
    nowallet (no bonus wallet) or net (no bonus at all). bal_min/bal_max are in CENTS.
    Returns (rows, total_count, summed_balance_in_basis)."""
    bexpr = BALANCE_BASIS.get(basis or "gross", _BAL_GROSS)
    if sort == "balance":
        order = f"{bexpr} DESC, created_at DESC"
    elif sort == "balance_asc":
        order = f"{bexpr} ASC, created_at DESC"
    else:
        order = SORT_ORDERS.get(sort or "created", SORT_ORDERS["created"])
    where, args = [], []
    if money and money in MONEY_COHORTS:
        where.append(f"({MONEY_COHORTS[money]})")
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if is_vip is not None:
        args.append(is_vip)
        where.append(f"is_vip = ${len(args)}")
    if mode:
        args.append(mode)
        where.append(f"billing_mode = ${len(args)}")
    if bal_min is not None:               # balance (in the chosen basis) ≥ this
        args.append(int(bal_min))
        where.append(f"{bexpr} >= ${len(args)}")
    if bal_max is not None:               # balance (in the chosen basis) ≤ this
        args.append(int(bal_max))
        where.append(f"{bexpr} <= ${len(args)}")
    if bonus == "wallet":                 # holds an unspent separate bonus wallet
        where.append("bonus_balance_cents > 0")
    elif bonus == "unspent":              # has balance but was never charged (never downloaded)
        where.append(f"{SQL_ANY_BALANCE} AND NOT EXISTS "
                     "(SELECT 1 FROM downloads d WHERE d.user_id = users.telegram_id)")
    if q:
        term = q.strip().lstrip("@")
        if term.isdigit():
            args.append(int(term))
            where.append(f"telegram_id = ${len(args)}")
        else:
            args.append(f"%{term}%")
            where.append(f"username ILIKE ${len(args)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # count+sum and the page fetch are independent → run concurrently. The summary
    # covers the WHOLE filtered set (every match), not just this page.
    import asyncio
    summary, rows = await asyncio.gather(
        pool().fetchrow(f"SELECT count(*)::int AS n, "
                        f"COALESCE(SUM({bexpr}),0)::bigint AS bal FROM users {clause}", *args),
        pool().fetch(
            "SELECT *, (SELECT count(*)::int FROM downloads d WHERE d.user_id = users.telegram_id) "
            f"AS downloads_count FROM users {clause} ORDER BY {order} "
            f"LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
            *args, limit, offset),
    )
    return [dict(r) for r in rows], summary["n"], summary["bal"]


async def usage(user_id: int) -> dict:
    p = pool()
    total = await p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1", int(user_id))
    today = await p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1 AND day=current_date", int(user_id))
    return {"downloads_total": total, "downloads_today": today}
