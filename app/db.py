"""asyncpg connection pool + schema migration runner.

One shared pool serves every handler concurrently (async, non-blocking), so many
users are handled at once with no per-query lateness. Money operations acquire a
connection and open an explicit transaction (see app/repo/wallet.py).
"""
import pathlib
import asyncpg

from . import config

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        command_timeout=30,
    )
    await _run_schema()
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


async def _run_schema() -> None:
    sql = (pathlib.Path(__file__).resolve().parent.parent / "schema.sql").read_text(encoding="utf-8")
    async with _pool.acquire() as conn:
        await conn.execute(sql)
