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


# ── new-price wallet (two-tier pricing) ──────────────────────────────────────
# `balance_cents` holds money topped up BEFORE the price change (charged at the OLD
# price); this wallet holds every NEW top-up (charged at the NEW price). Debt is still
# paid off first, so a top-up never leaves the user owing while holding credit.
async def credit_new(conn, user_id: int, amount_cents: int, reason: str,
                     ref_type: str | None = None, ref_id: int | None = None) -> int:
    """Credit the NEW-price wallet. Returns the new balance_new_cents."""
    if amount_cents < 0:
        raise ValueError("amount must be positive")
    row = await conn.fetchrow(
        "SELECT balance_cents, balance_new_cents, owed_cents FROM users WHERE telegram_id=$1 FOR UPDATE",
        user_id)
    if row is None:
        raise ValueError(f"user {user_id} not found")
    owed = row["owed_cents"]
    pay_debt = min(owed, amount_cents)
    new_owed = owed - pay_debt
    new_new = row["balance_new_cents"] + (amount_cents - pay_debt)
    await conn.execute(
        "UPDATE users SET balance_new_cents=$1, owed_cents=$2, updated_at=now() WHERE telegram_id=$3",
        new_new, new_owed, user_id)
    # balance_after records the (unchanged) OLD-wallet balance so the ledger invariant
    # "cached balance_cents == last ledger balance_after" still holds.
    await conn.execute(
        """INSERT INTO wallet_ledger
             (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id)
           VALUES ($1,'credit',$2,$3,$4,$5,$6)""",
        user_id, amount_cents, row["balance_cents"], reason, ref_type, ref_id)
    return new_new


async def debit_any(conn, user_id: int, amount_cents: int, reason: str,
                    ref_type: str | None = None, ref_id: int | None = None) -> tuple:
    """Admin deduct across BOTH money wallets — newest money first (a deduct usually
    reverses a recent top-up, which landed in the new wallet), then the old wallet. Never
    goes negative. Returns (taken, balance_cents, balance_new_cents)."""
    if amount_cents <= 0:
        return 0, 0, 0
    row = await conn.fetchrow(
        "SELECT balance_cents, balance_new_cents FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)
    if row is None:
        raise ValueError(f"user {user_id} not found")
    take_new = min(row["balance_new_cents"], amount_cents)
    take_old = min(row["balance_cents"], amount_cents - take_new)
    taken = take_new + take_old
    if taken <= 0:
        return 0, row["balance_cents"], row["balance_new_cents"]
    new_old, new_new = row["balance_cents"] - take_old, row["balance_new_cents"] - take_new
    await conn.execute(
        "UPDATE users SET balance_cents=$1, balance_new_cents=$2, updated_at=now() WHERE telegram_id=$3",
        new_old, new_new, user_id)
    await conn.execute(
        """INSERT INTO wallet_ledger
             (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id)
           VALUES ($1,'debit',$2,$3,$4,$5,$6)""",
        user_id, taken, new_old, reason, ref_type, ref_id)
    return taken, new_old, new_new


# ── separate bonus wallet (bonus_balance_cents) ──────────────────────────────
# The bonus wallet is spendable money kept OUT of the normal balance. Granting a
# bonus adds here (and to the lifetime bonus_cents record); a download spends here
# FIRST (see billing.charge_and_log). Ledger rows are still written for the audit
# trail — balance_after_cents records the (unchanged) MAIN balance so the invariant
# "cached balance == last ledger balance_after" still holds.
async def credit_bonus(conn, user_id: int, amount_cents: int, reason: str = "bonus",
                       ref_type: str | None = "bonus", ref_id: int | None = None) -> int:
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    row = await conn.fetchrow(
        "SELECT balance_cents, bonus_balance_cents FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)
    if row is None:
        raise ValueError(f"user {user_id} not found")
    new_bonus = row["bonus_balance_cents"] + amount_cents
    await conn.execute(
        "UPDATE users SET bonus_balance_cents=$1, bonus_cents=bonus_cents+$2, updated_at=now() WHERE telegram_id=$3",
        new_bonus, amount_cents, user_id)
    await conn.execute(
        """INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id)
           VALUES ($1,'credit',$2,$3,$4,$5,$6)""",
        user_id, amount_cents, row["balance_cents"], reason, ref_type, ref_id)
    return new_bonus


async def spend_bonus(conn, user_id: int, amount_cents: int, reason: str = "download_bonus",
                      ref_type: str | None = "download", ref_id: int | None = None) -> tuple[int, int]:
    """Spend up to `amount_cents` from the bonus wallet. Returns (spent, new_bonus_balance).
    Never goes below zero; caller charges the shortfall to the normal balance."""
    if amount_cents <= 0:
        return 0, 0
    row = await conn.fetchrow(
        "SELECT balance_cents, bonus_balance_cents FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)
    if row is None:
        raise ValueError(f"user {user_id} not found")
    spent = min(row["bonus_balance_cents"], amount_cents)
    new_bonus = row["bonus_balance_cents"] - spent
    if spent > 0:
        await conn.execute(
            "UPDATE users SET bonus_balance_cents=$1, updated_at=now() WHERE telegram_id=$2", new_bonus, user_id)
        await conn.execute(
            """INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id)
               VALUES ($1,'debit',$2,$3,$4,$5,$6)""",
            user_id, spent, row["balance_cents"], reason, ref_type, ref_id)
    return spent, new_bonus
