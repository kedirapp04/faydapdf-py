"""Background broadcast delivery worker.

One asyncio task drains `status='sending'` campaigns, oldest first. Each tick claims a
batch of pending recipients, personalizes + sends them, and records the outcome:
  * sent      → recipient 'sent'
  * blocked   → recipient 'blocked' + user flagged is_blocked (skipped by future blasts)
  * 429       → global pause for retry_after seconds, recipient released to pending
  * transient → released to pending up to MAX_RETRIES, then 'failed'
State is all in the DB, so a restart resumes exactly where it left off.
"""
import asyncio
import json
import logging
import time

from ..db import pool, db_ready
from ..repo import broadcast as bc
from .. import notify
from . import personalize

log = logging.getLogger("faydapdf-py.bcast")

TICK_SECONDS = 1.0
BATCH = 15            # ≈15 messages/sec (one batch per tick)
CONCURRENCY = 6
MAX_RETRIES = 3

_pause_until = 0.0    # monotonic time; set on a 429 flood-wait


async def _load_users(ids: list[int]) -> dict:
    if not ids:
        return {}
    rows = await pool().fetch(
        "SELECT telegram_id, username, first_name, balance_cents, bonus_balance_cents "
        "FROM users WHERE telegram_id = ANY($1::bigint[])", ids)
    return {r["telegram_id"]: dict(r) for r in rows}


async def _deliver_one(camp, rec, user, buttons):
    global _pause_until
    text = personalize.render(camp["message"], user or {"telegram_id": rec["user_id"]}, camp["parse_mode"])
    res = await notify.send_ex(rec["bot_id"], rec["user_id"], text, camp["parse_mode"], buttons)
    if res.get("ok"):
        await bc.mark(rec["id"], "sent")
    elif res.get("blocked"):
        await bc.mark(rec["id"], "blocked", res.get("error"))
        await bc.mark_user_blocked(rec["user_id"], res.get("error") or "blocked")
    elif res.get("status") == 429:
        _pause_until = time.monotonic() + float(res.get("retry_after") or 5)
        await bc.release(rec["id"])
    elif rec["retries"] < MAX_RETRIES:
        await bc.release(rec["id"])
    else:
        await bc.mark(rec["id"], "failed", res.get("error"))


async def _tick():
    if time.monotonic() < _pause_until:
        return
    if not db_ready():
        return
    camp = await bc.pick_sending()
    if not camp:
        return
    batch = await bc.claim_batch(camp["id"], BATCH)
    if not batch:
        await bc.finish_if_done(camp["id"])
        return
    users = await _load_users([r["user_id"] for r in batch])
    try:
        buttons = json.loads(camp.get("buttons_json") or "[]")
    except Exception:
        buttons = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _run(rec):
        async with sem:
            try:
                await _deliver_one(camp, rec, users.get(rec["user_id"]), buttons)
            except Exception:
                log.exception("broadcast deliver failed for recipient %s", rec.get("id"))
                try:
                    await bc.release(rec["id"])
                except Exception:
                    pass

    await asyncio.gather(*[_run(r) for r in batch])
    await bc.finish_if_done(camp["id"])


async def worker_loop():
    log.info("Broadcast delivery worker started.")
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("broadcast worker tick error")
        await asyncio.sleep(TICK_SECONDS)
