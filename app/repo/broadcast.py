"""Persistent broadcast campaigns + recipients.

A campaign snapshots one `broadcast_recipients` row per targeted user up-front, so a
big blast can be paused/resumed and survives a restart (the delivery worker just
re-scans for `status='sending'` campaigns and `pending` recipients). Counters are
recomputed from the recipient rows on read, so they can never drift.
"""
import json

from ..db import pool

STALE_SENDING_SECONDS = 180   # a recipient stuck 'sending' this long is reclaimed once


async def create(title, segment, filter_json, message, parse_mode, buttons) -> int:
    return await pool().fetchval(
        """INSERT INTO broadcast_campaigns (title, segment, filter_json, message, parse_mode, buttons_json, status)
           VALUES ($1,$2,$3,$4,$5,$6,'draft') RETURNING id""",
        title or None, segment or "all", json.dumps(filter_json or {}),
        message, parse_mode, json.dumps(buttons or []))


async def snapshot(campaign_id: int, recipients: list[tuple]) -> int:
    """recipients = [(user_id, bot_id), …]. Bulk-insert, set total + start sending."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            if recipients:
                await conn.copy_records_to_table(
                    "broadcast_recipients",
                    records=[(campaign_id, uid, bid) for (uid, bid) in recipients],
                    columns=["campaign_id", "user_id", "bot_id"])
            await conn.execute(
                "UPDATE broadcast_campaigns SET total=$1, status='sending', started_at=now() WHERE id=$2",
                len(recipients), campaign_id)
    return len(recipients)


def _counts_sql(alias: str = "c") -> str:
    return (f"(SELECT count(*) FROM broadcast_recipients r WHERE r.campaign_id={alias}.id AND r.status='sent')::int AS sent,"
            f"(SELECT count(*) FROM broadcast_recipients r WHERE r.campaign_id={alias}.id AND r.status='failed')::int AS failed,"
            f"(SELECT count(*) FROM broadcast_recipients r WHERE r.campaign_id={alias}.id AND r.status='blocked')::int AS blocked,"
            f"(SELECT count(*) FROM broadcast_recipients r WHERE r.campaign_id={alias}.id AND r.status IN ('pending','sending'))::int AS remaining")


async def list_campaigns(limit: int = 50) -> list[dict]:
    rows = await pool().fetch(
        f"""SELECT c.id, c.title, c.segment, c.status, c.total, c.parse_mode,
                   c.created_at, c.started_at, c.finished_at, {_counts_sql('c')}
            FROM broadcast_campaigns c ORDER BY c.id DESC LIMIT $1""", limit)
    return [dict(r) for r in rows]


async def get(campaign_id: int) -> dict | None:
    row = await pool().fetchrow(
        f"""SELECT c.*, {_counts_sql('c')} FROM broadcast_campaigns c WHERE c.id=$1""", campaign_id)
    return dict(row) if row else None


async def report(campaign_id: int) -> dict:
    c = await get(campaign_id)
    if not c:
        return {}
    errors = await pool().fetch(
        "SELECT error, count(*)::int n FROM broadcast_recipients WHERE campaign_id=$1 AND status='failed' "
        "AND error IS NOT NULL GROUP BY error ORDER BY n DESC LIMIT 10", campaign_id)
    return {"campaign": _dto(c), "top_errors": [{"error": e["error"], "n": e["n"]} for e in errors]}


async def control(campaign_id: int, action: str) -> dict:
    c = await get(campaign_id)
    if not c:
        return {"ok": False, "error": "not found"}
    st = c["status"]
    if action == "pause" and st == "sending":
        await pool().execute("UPDATE broadcast_campaigns SET status='paused' WHERE id=$1", campaign_id)
    elif action == "resume" and st == "paused":
        await pool().execute("UPDATE broadcast_campaigns SET status='sending' WHERE id=$1", campaign_id)
    elif action == "cancel" and st in ("sending", "paused", "draft"):
        async with pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE broadcast_recipients SET status='skipped' WHERE campaign_id=$1 AND status IN ('pending','sending')", campaign_id)
                await conn.execute("UPDATE broadcast_campaigns SET status='cancelled', finished_at=now() WHERE id=$1", campaign_id)
    elif action == "delete" and st in ("completed", "cancelled", "draft"):
        await pool().execute("DELETE FROM broadcast_campaigns WHERE id=$1", campaign_id)
    else:
        return {"ok": False, "error": f"cannot {action} a {st} campaign"}
    return {"ok": True}


# ── worker-facing ────────────────────────────────────────────────────────────
async def pick_sending() -> dict | None:
    row = await pool().fetchrow(
        "SELECT * FROM broadcast_campaigns WHERE status='sending' ORDER BY id ASC LIMIT 1")
    return dict(row) if row else None


async def claim_batch(campaign_id: int, limit: int) -> list[dict]:
    """Atomically claim up to `limit` pending (or stale-sending) recipients."""
    rows = await pool().fetch(
        f"""UPDATE broadcast_recipients SET status='sending', tried_at=now()
            WHERE id IN (
                SELECT id FROM broadcast_recipients
                WHERE campaign_id=$1 AND (status='pending'
                    OR (status='sending' AND tried_at < now() - interval '{STALE_SENDING_SECONDS} seconds'))
                ORDER BY id ASC LIMIT $2
                FOR UPDATE SKIP LOCKED)
            RETURNING id, user_id, bot_id, retries""",
        campaign_id, limit)
    return [dict(r) for r in rows]


async def mark(recipient_id: int, status: str, error: str | None = None) -> None:
    await pool().execute(
        "UPDATE broadcast_recipients SET status=$1, error=$2, tried_at=now() WHERE id=$3",
        status, (error or None), recipient_id)


async def release(recipient_id: int) -> None:
    await pool().execute(
        "UPDATE broadcast_recipients SET status='pending', retries=retries+1, tried_at=now() WHERE id=$1",
        recipient_id)


async def finish_if_done(campaign_id: int) -> bool:
    remaining = await pool().fetchval(
        "SELECT count(*)::int FROM broadcast_recipients WHERE campaign_id=$1 AND status IN ('pending','sending')",
        campaign_id)
    if remaining == 0:
        await pool().execute(
            "UPDATE broadcast_campaigns SET status='completed', finished_at=now() WHERE id=$1 AND status='sending'",
            campaign_id)
        return True
    return False


def _dto(c: dict) -> dict:
    return {"id": c["id"], "title": c.get("title"), "segment": c.get("segment"),
            "status": c["status"], "total": c["total"], "sent": c.get("sent", 0),
            "failed": c.get("failed", 0), "blocked": c.get("blocked", 0),
            "remaining": c.get("remaining", 0),
            "created_at": c["created_at"].isoformat() if c.get("created_at") else None,
            "finished_at": c["finished_at"].isoformat() if c.get("finished_at") else None}


# ── blocked-user helpers ─────────────────────────────────────────────────────
async def mark_user_blocked(user_id: int, reason: str) -> None:
    await pool().execute(
        "UPDATE users SET is_blocked=true, blocked_at=now(), blocked_reason=$2 WHERE telegram_id=$1",
        user_id, (reason or "")[:500])


async def blocked_summary() -> dict:
    p = pool()
    blocked = await p.fetchval("SELECT count(*)::int FROM users WHERE is_blocked=true")
    reactivated = await p.fetchval(
        "SELECT count(*)::int FROM users WHERE unblocked_at > now() - interval '30 days'")
    return {"blocked": blocked or 0, "reactivated_30d": reactivated or 0}
