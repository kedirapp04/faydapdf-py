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
import time

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
                # statement_cache_size=0 makes the pool safe behind a TRANSACTION-MODE
                # connection pooler (PgBouncer / Supabase 6543): those recycle server
                # connections per-transaction, so cached server-side prepared statements
                # would break. Harmless on a direct connection too. This lets us point
                # DATABASE_URL at a transaction pooler and run thousands of concurrent
                # users over a small set of physical DB connections.
                statement_cache_size=0,
                max_inactive_connection_lifetime=300,  # recycle idle conns (pooler-friendly)
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


_recheck_at = 0.0


async def recheck_if_down() -> bool:
    """Like db_ready(), but if the DB is marked DOWN it opportunistically RE-PROBES here
    (throttled to every few seconds) and flips back to ready on success — so recovery is
    driven by real user activity too, not only the background health_loop. This makes the
    'temporarily unavailable' state self-heal even if the monitor ever stalls."""
    global _db_ready, _recheck_at
    if _db_ready:
        return True
    now = time.monotonic()
    if now - _recheck_at < 3:
        return False
    _recheck_at = now
    if await _probe(timeout=8):
        _db_ready = True
        log.warning("DB recovered (on-demand recheck)")
        try:
            await asyncio.wait_for(_pool.expire_connections(), timeout=10)
        except Exception:
            pass
        return True
    return False


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


async def _probe(timeout: float = 8.0) -> bool:
    """Is Postgres reachable right now? The whole probe (connect/acquire + query) runs
    under ONE total timeout so it can never hang — a hang here would freeze the recovery
    monitor and the DB would stay 'down' forever.

    While DOWN, the pool's connections are likely stale (a network blip leaves asyncpg
    holding sockets the server/pooler already closed — 'connection was closed in the
    middle of operation'). A pooled ping would keep failing on those and never notice the
    DB is back, so we test with a FRESH short-lived connection instead. While UP, a cheap
    pooled ping is enough and opens no extra connections.

    `timeout` is passed generously while DOWN: this DB is remote (Railway PG reached over
    a long link), so a healthy connect can still take several seconds — a tight timeout
    would reject those and recovery would never stick."""
    if _pool is None:
        return False
    try:
        async with asyncio.timeout(timeout):
            if _db_ready:
                async with _pool.acquire() as conn:
                    await conn.execute("SELECT 1")
            else:
                conn = await asyncpg.connect(dsn=config.DATABASE_URL, statement_cache_size=0)
                try:
                    await conn.execute("SELECT 1")
                finally:
                    await conn.close()
        return True
    except Exception:
        return False


async def health_loop(interval: float = 10.0) -> None:
    """Background monitor: ping Postgres and flip dbReady. On recovery it flushes the
    pool's now-stale connections (so real queries reconnect fresh instead of re-tripping
    the down flag) and re-reads the policy. Bulletproof: one iteration can never kill the
    monitor. Never returns.

    Adaptive cadence: while UP, a light 8s-timeout ping every `interval`s. While DOWN,
    poll every 3s with a 20s timeout — a slow remote DB needs the extra room, and the
    faster cadence catches the brief windows it's reachable so 'unavailable' clears in
    seconds instead of minutes."""
    global _db_ready
    while True:
        try:
            down = not _db_ready
            await asyncio.sleep(3 if down else interval)
            ok = await _probe(timeout=20 if down else 8)
            if ok and not _db_ready:
                _db_ready = True
                log.warning("DB recovered")
                try:
                    # Drop every connection the pool is holding; the next acquire() for a
                    # real query opens a fresh one, so a recovered DB actually serves again.
                    await asyncio.wait_for(_pool.expire_connections(), timeout=10)
                except Exception:
                    log.exception("expire_connections after recovery failed")
                try:
                    # MUST be timeout-bounded: a pool query here (over just-recovered, maybe
                    # still-flaky connections) could otherwise hang and freeze the whole
                    # monitor — which is exactly what left the DB stuck 'down' for ages.
                    await asyncio.wait_for(_load_policy(), timeout=10)
                except Exception:
                    pass
            elif not ok and _db_ready:
                _db_ready = False
                log.warning("DB DOWN (health probe failed)")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("health_loop iteration error — continuing")
