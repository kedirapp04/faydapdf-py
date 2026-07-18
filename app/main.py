"""Entrypoint: init the DB pool, wire routers, start long-polling."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from . import config
from .db import init_pool, close_pool
from .handlers import admin, user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("faydapdf-py")


async def run_bot() -> None:
    """Poll every bot this process owns. Assumes the DB pool is already up (so the
    combined runner in app/run.py can share one pool with the web admin)."""
    # One dispatcher (shared handlers) polling every bot this process owns. All
    # bots share the same Postgres, so users/balance are the same account on any
    # bot. Run several processes with POLL_ONLY to spread load across machines.
    from aiogram.types import BotCommand
    bots = [Bot(token=t) for t in config.POLL_TOKENS]
    dp = Dispatcher(storage=MemoryStorage())
    # Admin router first so its FSM states win over the user catch-all handler.
    dp.include_router(admin.router)
    dp.include_router(user.router)

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
            await b.session.close()


async def main() -> None:
    await init_pool()
    log.info("DB pool ready.")
    try:
        await run_bot()
    finally:
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
