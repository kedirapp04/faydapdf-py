"""Dashboard aggregate stats — one round-trip, small payload (fast to poll live)."""
from ..db import pool


async def dashboard() -> dict:
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
    return dict(row)
