"""Cross-bot notifications. Sends a message to a user via a SPECIFIC bot (the one
they started) using the Telegram HTTP API — works from any process (bot or web),
even for a bot this process doesn't poll."""
import aiohttp

from . import config


_BLOCKED_MARKERS = ("bot was blocked", "blocked by the user", "user is deactivated",
                    "chat not found", "forbidden", "peer_id_invalid",
                    "bot can't initiate", "user not found", "have no rights to send")


async def send_ex(bot_id, chat_id, text: str, parse_mode: str | None = None, buttons=None) -> dict:
    """Send and return delivery detail: {ok, status, retry_after, blocked, error}.
    `blocked` = the user has blocked/deactivated the bot (drop them); `retry_after`
    (seconds) is set on a 429 flood-wait."""
    token = config.BOT_REGISTRY.get(int(bot_id)) if bot_id else None
    if not token:
        token = config.BOT_TOKEN  # fallback to the primary bot
    payload = {"chat_id": int(chat_id), "text": text}
    if parse_mode in ("HTML", "Markdown", "MarkdownV2"):
        payload["parse_mode"] = parse_mode
    if buttons:
        rows = [[{"text": b["text"], "url": b["url"]}] for b in buttons if b.get("text") and b.get("url")]
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload) as r:
                if r.status == 200:
                    return {"ok": True, "status": 200}
                try:
                    d = await r.json(content_type=None)
                except Exception:
                    d = {}
                desc = str(d.get("description") or "").lower()
                if r.status == 429:
                    ra = 5
                    try:
                        ra = int((d.get("parameters") or {}).get("retry_after") or 5)
                    except Exception:
                        ra = 5
                    return {"ok": False, "status": 429, "retry_after": max(1, min(600, ra)), "error": desc}
                blocked = r.status in (403, 400) and any(m in desc for m in _BLOCKED_MARKERS)
                return {"ok": False, "status": r.status, "blocked": blocked, "error": desc[:200]}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)[:200]}


async def send(bot_id, chat_id, text: str, parse_mode: str | None = None, buttons=None) -> bool:
    """Boolean send (back-compat). See send_ex for delivery detail."""
    r = await send_ex(bot_id, chat_id, text, parse_mode, buttons)
    return bool(r.get("ok"))


async def notify_user(user_id, text: str) -> bool:
    """Notify a user via the bot they last used (looked up from the DB)."""
    from .db import pool
    bot_id = await pool().fetchval("SELECT last_bot_id FROM users WHERE telegram_id=$1", int(user_id))
    return await send(bot_id, user_id, text)


async def notify_user_ex(user_id, text: str, parse_mode: str | None = None) -> dict:
    """Like notify_user but returns the delivery detail {ok, blocked, error, …}."""
    from .db import pool
    bot_id = await pool().fetchval("SELECT last_bot_id FROM users WHERE telegram_id=$1", int(user_id))
    return await send_ex(bot_id, user_id, text, parse_mode)
