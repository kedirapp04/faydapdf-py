"""Transactional wallet primitives — the heart of the money-correctness design.

Every balance change:
  1. locks the user row (`SELECT ... FOR UPDATE`) so concurrent requests serialize,
  2. writes an append-only `wallet_ledger` row (audit trail), and
  3. updates the cached `users.balance_cents`,
ALL inside the caller's single transaction. If anything fails the whole thing
rolls back, so balance and history can never disagree.

These helpers take a live `conn` inside a transaction — callers (payments.approve,
billing.charge) compose them atomically.
"""
import asyncpg


class InsufficientFunds(Exception):
    """A debit would drive the balance below zero. Callers decide how to handle it
    (e.g. billing.charge_and_log bills only what's available and books the rest as
    debt) — but the ledger never records a negative balance."""


async def _apply(conn: asyncpg.Connection, user_id: int, kind: str, amount_cents: int,
                 reason: str, ref_type: str | None, ref_id: int | None) -> int:
    if amount_cents < 0:
        raise ValueError("amount must be positive")
    row = await conn.fetchrow(
        "SELECT balance_cents, owed_cents FROM users WHERE telegram_id=$1 FOR UPDATE", user_id
    )
    if row is None:
        raise ValueError(f"user {user_id} not found")
        
    new_balance = row["balance_cents"]
    new_owed = row["owed_cents"]
    
    if kind == "credit":
        # Pay off any debt first
        pay_debt = min(new_owed, amount_cents)
        new_owed -= pay_debt
        leftover = amount_cents - pay_debt
        new_balance += leftover
    else:
        new_balance -= amount_cents
        
    if new_balance < 0:
        # Safety net: never let a debit make the cached balance negative.
        raise InsufficientFunds(f"user {user_id}: balance {row['balance_cents']} < debit {amount_cents}")
        
    await conn.execute(
        "UPDATE users SET balance_cents=$1, owed_cents=$2, updated_at=now() WHERE telegram_id=$3",
        new_balance, new_owed, user_id,
    )
    await conn.execute(
        """INSERT INTO wallet_ledger
             (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        user_id, kind, amount_cents, new_balance, reason, ref_type, ref_id,
    )
    return new_balance


async def credit(conn, user_id: int, amount_cents: int, reason: str,
                 ref_type: str | None = None, ref_id: int | None = None) -> int:
    return await _apply(conn, user_id, "credit", amount_cents, reason, ref_type, ref_id)


async def debit(conn, user_id: int, amount_cents: int, reason: str,
                ref_type: str | None = None, ref_id: int | None = None) -> int:
    return await _apply(conn, user_id, "debit", amount_cents, reason, ref_type, ref_id)
