"""Dashboard aggregate stats — one round-trip, small payload.

The admin UI polls this every few seconds, and the counts scan large tables
(users, downloads), so the result is cached in memory for a few seconds. Rapid
polls and tab switches then serve from RAM instead of re-scanning the DB.
"""
import time

from ..db import pool

_cache: dict | None = None
_at: float = 0.0
_TTL = 4.0


async def dashboard(force: bool = False) -> dict:
    global _cache, _at
    if not force and _cache is not None and (time.monotonic() - _at) <= _TTL:
        return _cache
    row = await pool().fetchrow(
        """
        SELECT
          (SELECT count(*)::int FROM users)                                             AS users,
          (SELECT count(*)::int FROM users WHERE status='active')                       AS active,
          (SELECT count(*)::int FROM users WHERE status='blocked')                      AS blocked,
          (SELECT count(*)::int FROM users WHERE is_vip)                                AS vip,
          (SELECT count(*)::int FROM payments WHERE status='pending')                   AS pending_payments,
          (SELECT count(*)::int FROM downloads WHERE day=current_date)                  AS downloads_today,
          (SELECT count(*)::int FROM downloads)                                         AS downloads_total,
          (SELECT COALESCE(SUM(balance_cents),0)::bigint FROM users)                    AS balance_cents,
          (SELECT COALESCE(SUM(amount_cents),0)::bigint FROM payments
             WHERE status='approved' AND decided_at::date=current_date)                AS topups_today_cents
        """
    )
    _cache = dict(row)
    _at = time.monotonic()
    return _cache
