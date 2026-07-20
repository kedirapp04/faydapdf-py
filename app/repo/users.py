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


async def page(status: str | None, q: str | None, limit: int, offset: int,
               is_vip: bool | None = None, mode: str | None = None,
               bonus: str | None = None) -> tuple[list[dict], int]:
    """Paginated + optional filters (status / VIP / billing mode / bonus) + search
    (username or id). Returns (rows, total)."""
    where, args = [], []
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if is_vip is not None:
        args.append(is_vip)
        where.append(f"is_vip = ${len(args)}")
    if mode:
        args.append(mode)
        where.append(f"billing_mode = ${len(args)}")
    if bonus == "wallet":                 # holds an unspent separate bonus wallet
        where.append("bonus_balance_cents > 0")
    elif bonus == "unspent":              # has balance but was never charged (never downloaded)
        where.append("balance_cents > 0 AND NOT EXISTS "
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
    # count + page fetch are independent → run concurrently (halves wall-time)
    import asyncio
    total, rows = await asyncio.gather(
        pool().fetchval(f"SELECT count(*)::int FROM users {clause}", *args),
        pool().fetch(
            "SELECT *, (SELECT count(*)::int FROM downloads d WHERE d.user_id = users.telegram_id) "
            f"AS downloads_count FROM users {clause} ORDER BY created_at DESC "
            f"LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
            *args, limit, offset),
    )
    return [dict(r) for r in rows], total


async def usage(user_id: int) -> dict:
    p = pool()
    total = await p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1", int(user_id))
    today = await p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1 AND day=current_date", int(user_id))
    return {"downloads_total": total, "downloads_today": today}
