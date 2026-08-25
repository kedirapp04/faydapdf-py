"""Cross-bot notifications. Sends a message to a user via a SPECIFIC bot (the one
they started) using the Telegram HTTP API — works from any process (bot or web),
even for a bot this process doesn't poll."""
import aiohttp

from . import config


_BLOCKED_MARKERS = ("bot was blocked", "blocked by the user", "user is deactivated",
                    "chat not found", "forbidden", "peer_id_invalid",
                    "bot can't initiate", "user not found", "have no rights to send")


# Telegram method + payload key for each media kind. Photos/videos take a caption;
# everything here does, which is why one code path covers them all.
MEDIA_METHODS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "document": ("sendDocument", "document"),
    "audio": ("sendAudio", "audio"),
    "animation": ("sendAnimation", "animation"),
}


def _token_for(bot_id):
    token = config.BOT_REGISTRY.get(int(bot_id)) if bot_id else None
    return token or config.BOT_TOKEN      # fall back to the primary bot


def _decorate(payload: dict, parse_mode, buttons) -> dict:
    if parse_mode in ("HTML", "Markdown", "MarkdownV2"):
        payload["parse_mode"] = parse_mode
    if buttons:
        rows = [[{"text": b["text"], "url": b["url"]}] for b in buttons if b.get("text") and b.get("url")]
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
    return payload


async def send_media_ex(bot_id, chat_id, media_type: str, file_id: str, caption: str = "",
                        parse_mode: str | None = None, buttons=None) -> dict:
    """Send a photo/video/document/audio/animation by file_id, with an optional caption.

    Same delivery-detail contract as send_ex, so the broadcast worker's retry,
    flood-wait and blocked-user logic needs no special case.

    NOTE: a file_id is only valid for the bot that produced it — sending one via a
    different bot fails. The upload endpoint and the campaign therefore pin media
    broadcasts to a single bot.
    """
    method, key = MEDIA_METHODS.get(media_type or "", (None, None))
    if not method or not file_id:
        return {"ok": False, "status": 0, "error": f"unsupported media type {media_type!r}"}
    payload = {"chat_id": int(chat_id), key: file_id}
    if caption:
        payload["caption"] = caption[:1024]      # Telegram's caption limit
    return await _post(_token_for(bot_id), method, _decorate(payload, parse_mode, buttons))


async def send_ex(bot_id, chat_id, text: str, parse_mode: str | None = None, buttons=None) -> dict:
    """Send and return delivery detail: {ok, status, retry_after, blocked, error}.
    `blocked` = the user has blocked/deactivated the bot (drop them); `retry_after`
    (seconds) is set on a 429 flood-wait."""
    payload = _decorate({"chat_id": int(chat_id), "text": text}, parse_mode, buttons)
    return await _post(_token_for(bot_id), "sendMessage", payload)


async def _post(token: str, method: str, payload: dict) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(f"https://api.telegram.org/bot{token}/{method}", json=payload) as r:
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
