"""Payment receipts + the atomic approve/reject state machine.

This module is where the old bot's bugs are fixed:
  * A receipt is inserted with UNIQUE(receipt_id) → it can be submitted ONCE, so
    it can never double-credit ("added balance twice" / "removed without removing").
  * approve() runs mark-approved + credit-balance + ledger in ONE transaction,
    guarded by `WHERE status='pending'` and `FOR UPDATE`. So you can never end up
    with "balance added but receipt still pending", or a double-approve race.
"""
from ..db import pool
from . import wallet

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


async def submit(user_id: int, receipt_id: str, bank: str, amount_cents: int,
                 provider: str) -> tuple[dict, bool]:
    """Idempotent insert. Returns (payment_row, created). If the receipt already
    exists, returns the existing row with created=False (never a duplicate)."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO payments (user_id, receipt_id, bank, amount_cents, status, provider)
               VALUES ($1,$2,$3,$4,'pending',$5)
               ON CONFLICT (receipt_id) DO NOTHING
               RETURNING *""",
            int(user_id), receipt_id, bank, int(amount_cents), provider,
        )
        if row is not None:
            return dict(row), True
        existing = await conn.fetchrow("SELECT * FROM payments WHERE receipt_id=$1", receipt_id)
        return dict(existing), False


async def get(payment_id: int) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM payments WHERE id=$1", int(payment_id))
    return dict(row) if row else None


async def approve(payment_id: int, admin_id, amount_cents: int | None = None) -> dict:
    """Atomically approve a pending payment AND credit the balance. Idempotent:
    a payment that isn't 'pending' anymore is a no-op (returns already_*)."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            pay = await conn.fetchrow("SELECT * FROM payments WHERE id=$1 FOR UPDATE", int(payment_id))
            if pay is None:
                return {"ok": False, "error": "not_found"}
            if pay["status"] != PENDING:
                return {"ok": False, "error": f"already_{pay['status']}", "payment": dict(pay)}

            amount = amount_cents if amount_cents is not None else pay["amount_cents"]
            if amount is None or amount <= 0:
                return {"ok": False, "error": "no_amount", "payment": dict(pay)}

            # Guard against a concurrent approver: only the row still 'pending' updates.
            res = await conn.execute(
                "UPDATE payments SET status='approved', decided_by=$1, decided_at=now(), amount_cents=$2 "
                "WHERE id=$3 AND status='pending'",
                str(admin_id), int(amount), int(payment_id),
            )
            if res.endswith(" 0"):
                return {"ok": False, "error": "race"}

            new_balance = await wallet.credit(
                conn, pay["user_id"], int(amount), "topup", ref_type="payment", ref_id=int(payment_id)
            )
            return {
                "ok": True,
                "user_id": pay["user_id"],
                "amount_cents": int(amount),
                "balance_cents": new_balance,
            }


async def reject(payment_id: int, admin_id, reason: str = "") -> dict:
    """Reject a pending payment. No money moves. Idempotent."""
    async with pool().acquire() as conn:
        res = await conn.execute(
            "UPDATE payments SET status='rejected', decided_by=$1, decided_at=now(), reason=$2 "
            "WHERE id=$3 AND status='pending'",
            str(admin_id), reason, int(payment_id),
        )
        if res.endswith(" 0"):
            row = await conn.fetchrow("SELECT status FROM payments WHERE id=$1", int(payment_id))
            if row is None:
                return {"ok": False, "error": "not_found"}
            return {"ok": False, "error": f"already_{row['status']}"}
        return {"ok": True}


async def list_pending(limit: int = 20, offset: int = 0) -> list[dict]:
    rows = await pool().fetch(
        "SELECT * FROM payments WHERE status='pending' ORDER BY created_at ASC LIMIT $1 OFFSET $2",
        limit, offset,
    )
    return [dict(r) for r in rows]


async def count_pending() -> int:
    return await pool().fetchval("SELECT count(*)::int FROM payments WHERE status='pending'")


async def page(status: str | None, q: str | None, limit: int, offset: int) -> tuple[list[dict], int, dict]:
    """Paginated receipts, any status, optional search (receipt id / user id / #id).
    Returns (rows, total, counts_by_status)."""
    where, args = [], []
    if status in ("pending", "approved", "rejected"):
        args.append(status)
        where.append(f"status = ${len(args)}")
    if q:
        term = q.strip().lstrip("#")
        if term.isdigit():
            # numeric: payment id, user id, or the admin (decided_by) who reviewed it
            args.append(int(term))
            where.append(f"(user_id = ${len(args)} OR id = ${len(args)} OR decided_by = ${len(args)})")
        else:
            # text: receipt id or the provider/approver (verifypayment/leul/relay/manual…)
            args.append(f"%{term.upper()}%")
            where.append(f"(upper(receipt_id) LIKE ${len(args)} OR upper(provider) LIKE ${len(args)})")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = await pool().fetchval(f"SELECT count(*)::int FROM payments {clause}", *args)
    rows = await pool().fetch(
        f"SELECT * FROM payments {clause} ORDER BY created_at DESC NULLS LAST LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
        *args, limit, offset)
    counts = {r["status"]: r["n"] for r in await pool().fetch(
        "SELECT status, count(*)::int n FROM payments GROUP BY status")}
    return [dict(r) for r in rows], total, counts
