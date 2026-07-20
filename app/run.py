"""Combined entrypoint: bot polling + web admin in ONE process, one DB pool.

This is the entrypoint for a single-service deploy (e.g. Railway/Heroku, where a
service runs one process and exposes one HTTP port). The web admin binds Railway's
injected $PORT and gets the service's HTTPS domain; the bot(s) long-poll alongside
it on the same asyncio loop and the same asyncpg pool.

Always-on: each half (bot / web) runs under a supervisor that restarts it with
backoff if it ever raises, so a transient crash in one never takes the process down
or stops the other. Railway's restart policy is the final backstop if the whole
process dies (OOM, etc.).

For a VPS you can still run the two separately (`python -m app.main` and
`python -m app.web`) if you'd rather split them across processes/machines.
"""
import asyncio
import logging

import uvicorn

from . import config, web
from .db import init_pool, close_pool, health_loop
from .main import run_bot
from .services.broadcast_worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("faydapdf-py")


async def _run_web() -> None:
    # Fresh Server each call so the supervisor can restart it cleanly.
    # lifespan="off": app/run.py owns the single shared pool, so the FastAPI
    # lifespan must NOT init/close its own (that would double- or early-close it).
    server = uvicorn.Server(uvicorn.Config(
        web.app, host=config.WEB_HOST, port=config.WEB_PORT,
        log_level="info", lifespan="off",
    ))
    await server.serve()


async def _supervise(name: str, factory) -> None:
    """Run factory() forever; on any crash, log and restart with capped backoff."""
    delay = 2
    while True:
        try:
            await factory()
            log.warning("%s task exited cleanly — restarting.", name)
            delay = 2
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s task crashed — restarting in %ss.", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def main() -> None:
    await init_pool()
    log.info("DB pool ready. Web admin on %s:%s", config.WEB_HOST, config.WEB_PORT)
    asyncio.create_task(health_loop())  # DB-down recovery monitor
    asyncio.create_task(_supervise("bcast", worker_loop))  # broadcast delivery worker
    try:
        # Neither supervisor returns; if one is cancelled (shutdown) the other is too.
        await asyncio.gather(_supervise("bot", run_bot), _supervise("web", _run_web))
    finally:
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
