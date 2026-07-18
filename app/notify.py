"""Cross-bot notifications. Sends a message to a user via a SPECIFIC bot (the one
they started) using the Telegram HTTP API — works from any process (bot or web),
even for a bot this process doesn't poll."""
import aiohttp

from . import config


async def send(bot_id, chat_id, text: str) -> bool:
    token = config.BOT_REGISTRY.get(int(bot_id)) if bot_id else None
    if not token:
        token = config.BOT_TOKEN  # fallback to the primary bot
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": int(chat_id), "text": text},
            ) as r:
                return r.status == 200
    except Exception:
        return False


async def notify_user(user_id, text: str) -> bool:
    """Notify a user via the bot they last used (looked up from the DB)."""
    from .db import pool
    bot_id = await pool().fetchval("SELECT last_bot_id FROM users WHERE telegram_id=$1", int(user_id))
    return await send(bot_id, user_id, text)
