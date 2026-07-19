"""User records."""
from ..db import pool


async def get(user_id) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM users WHERE telegram_id = $1", int(user_id))
    return dict(row) if row else None


async def ensure(user_id, username: str | None = None) -> dict:
    """Create the user if new (active), else keep the username in sync."""
    uid = int(user_id)
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO users (telegram_id, username)
               VALUES ($1, $2)
               ON CONFLICT (telegram_id) DO UPDATE
                 SET username = COALESCE(EXCLUDED.username, users.username)
               RETURNING *""",
            uid, username,
        )
    return dict(row)


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
               is_vip: bool | None = None, mode: str | None = None) -> tuple[list[dict], int]:
    """Paginated + optional filters (status / VIP / billing mode) + search
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
    if q:
        term = q.strip().lstrip("@")
        if term.isdigit():
            args.append(int(term))
            where.append(f"telegram_id = ${len(args)}")
        else:
            args.append(f"%{term}%")
            where.append(f"username ILIKE ${len(args)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = await pool().fetchval(f"SELECT count(*)::int FROM users {clause}", *args)
    rows = await pool().fetch(
        f"SELECT * FROM users {clause} ORDER BY created_at DESC LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
        *args, limit, offset,
    )
    return [dict(r) for r in rows], total


async def usage(user_id: int) -> dict:
    p = pool()
    total = await p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1", int(user_id))
    today = await p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1 AND day=current_date", int(user_id))
    return {"downloads_total": total, "downloads_today": today}
