"""Entrypoint: init the DB pool, wire routers, start long-polling."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from . import config
from .db import init_pool, close_pool, health_loop
from .handlers import admin, user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("faydapdf-py")


_dp: Dispatcher | None = None


def _dispatcher() -> Dispatcher:
    """Build the dispatcher and attach the routers ONCE. A router can only ever be
    attached to a single dispatcher, so re-including them on a supervisor restart
    raises 'Router is already attached' — hence this module-level cache. Reusing the
    dispatcher across restarts also keeps in-flight FSM state (MemoryStorage)."""
    global _dp
    if _dp is None:
        _dp = Dispatcher(storage=MemoryStorage())
        # Admin router first so its FSM states win over the user catch-all handler.
        _dp.include_router(admin.router)
        _dp.include_router(user.router)
    return _dp


async def run_bot() -> None:
    """Poll every bot this process owns. Assumes the DB pool is already up (so the
    combined runner in app/run.py can share one pool with the web admin). All bots
    share the same Postgres, so users/balance are the same account on any bot."""
    from aiogram.types import BotCommand
    dp = _dispatcher()
    # Validate each token up front and poll ONLY the good ones. Otherwise a single
    # revoked/typo'd token (Telegram 'Unauthorized') makes start_polling's gather raise
    # and takes the whole fleet down.
    bots = []
    for t in config.POLL_TOKENS:
        b = Bot(token=t)
        try:
            await b.get_me()
        except Exception as e:
            log.error("Skipping a bot token (%s: %s) — check BOT_TOKENS.", type(e).__name__, e)
            try:
                await b.session.close()
            except Exception:
                pass
            continue
        bots.append(b)
    if not bots:
        log.error("No valid bot tokens to poll — fix BOT_TOKENS. Retrying in 30s.")
        await asyncio.sleep(30)
        return

    cmds = [
        BotCommand(command="start", description="Start"),
        BotCommand(command="forgotfan", description="Recover your FAN/FIN by SMS (free)"),
        BotCommand(command="admin", description="Admin panel (admin only)"),
    ]
    for b in bots:
        try:
            await b.set_my_commands(cmds)
        except Exception as e:
            log.warning("set_my_commands failed for bot: %s", e)
    log.info("Polling %d bot(s). Registry: %s. Admins: %s",
             len(bots), ", ".join(str(i) for i in config.BOT_REGISTRY), ", ".join(config.ADMIN_IDS) or "(none)")
    try:
        await dp.start_polling(*bots)
    finally:
        for b in bots:
            try:
                await b.session.close()
            except Exception:
                pass


async def main() -> None:
    await init_pool()
    log.info("DB pool ready.")
    asyncio.create_task(health_loop())  # DB-down recovery monitor
    try:
        await run_bot()
    finally:
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
