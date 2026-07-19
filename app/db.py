"""asyncpg connection pool + schema migration runner + DB-down policy.

One shared pool serves every handler concurrently (async, non-blocking), so many
users are handled at once with no per-query lateness. Money operations acquire a
connection and open an explicit transaction (see app/repo/wallet.py).

DB-down handling (ported from faydapdf-railway): a `dbReady` flag tracks Postgres
health, flipped by a background recovery monitor (health_loop). When the DB is
unreachable the bot applies an admin-set `db_down_policy`:
  • refuse    → block downloads with a "temporarily unavailable" message (default)
  • free      → serve the download WITHOUT charging or recording it
  • fallback  → same as free here (the full memory-replay queue is not ported)
The policy is cached in memory (so it's readable even while the DB is down) and
persisted in settings; it seeds from the DB_DOWN_POLICY env var.
"""
import asyncio
import logging
import os
import pathlib

import asyncpg

from . import config

log = logging.getLogger("faydapdf-py.db")

_pool: asyncpg.Pool | None = None
_db_ready: bool = True

_POLICIES = ("refuse", "free", "fallback")
_db_down_policy: str = (os.getenv("DB_DOWN_POLICY") or "refuse").strip().lower()
if _db_down_policy not in _POLICIES:
    _db_down_policy = "refuse"


async def init_pool(retries: int = 5, delay: float = 2.0) -> asyncpg.Pool:
    """Create the pool + run the schema, retrying a few times so a brief DB
    unavailability at boot recovers instead of instantly crash-looping."""
    global _pool, _db_ready
    last = None
    for attempt in range(1, retries + 1):
        try:
            _pool = await asyncpg.create_pool(
                dsn=config.DATABASE_URL,
                min_size=config.DB_POOL_MIN,
                max_size=config.DB_POOL_MAX,
                command_timeout=30,
            )
            await _run_schema()
            _db_ready = True
            await _load_policy()
            return _pool
        except Exception as e:
            last = e
            log.warning("DB init attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                await asyncio.sleep(delay)
    raise last


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


# ── DB-down policy + health ──────────────────────────────────────────────────
def db_ready() -> bool:
    return _db_ready


def mark_db_down() -> None:
    """Called by a handler that just caught a DB connection error, so the policy
    kicks in immediately (the health loop would otherwise notice within its interval)."""
    global _db_ready
    if _db_ready:
        _db_ready = False
        log.warning("DB marked DOWN (caught query error)")


def db_down_policy() -> str:
    return _db_down_policy if _db_down_policy in _POLICIES else "refuse"


async def set_db_down_policy(policy: str) -> str:
    global _db_down_policy
    _db_down_policy = policy if policy in _POLICIES else "refuse"
    try:
        from .repo import settings as settings_repo
        await settings_repo.set("db_down_policy", _db_down_policy)
    except Exception:
        pass
    return _db_down_policy


async def _load_policy() -> None:
    global _db_down_policy
    try:
        from .repo import settings as settings_repo
        v = await settings_repo.get("db_down_policy")
        if v in _POLICIES:
            _db_down_policy = v
    except Exception:
        pass


async def _probe() -> bool:
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5)
        return True
    except Exception:
        return False


async def health_loop(interval: float = 10.0) -> None:
    """Background monitor: ping Postgres every `interval`s and flip dbReady. On
    recovery it also re-reads the policy from settings. Never returns."""
    global _db_ready
    while True:
        await asyncio.sleep(interval)
        ok = await _probe()
        if ok and not _db_ready:
            _db_ready = True
            log.warning("DB recovered")
            await _load_policy()
        elif not ok and _db_ready:
            _db_ready = False
            log.warning("DB DOWN (health probe failed)")
