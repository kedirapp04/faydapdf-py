"""Key/value runtime settings (admin-toggleable — e.g. active Fayda mode, paused).

Backed by an in-memory cache so the hot paths (every bot message reads the
maintenance level / paused / free-mode / approver; the dashboard reads ~15 keys)
don't pay a DB round-trip each. The whole table is tiny, so one query refills the
whole cache; writes update it in place. A short TTL keeps multi-instance deploys
in sync.
"""
import time

from ..db import pool

_cache: dict[str, str] = {}
_at: float = 0.0
_TTL = 3.0   # seconds; a toggle on another instance propagates within this window


async def _ensure_fresh() -> None:
    global _cache, _at
    if time.monotonic() - _at <= _TTL:
        return
    try:
        rows = await pool().fetch("SELECT key, value FROM settings")
        _cache = {r["key"]: r["value"] for r in rows}
        _at = time.monotonic()
    except Exception:
        pass   # keep serving the stale cache if the DB blips


async def get(key: str, default: str | None = None) -> str | None:
    await _ensure_fresh()
    v = _cache.get(key)
    return v if v is not None else default


async def set(key: str, value: str) -> None:
    await pool().execute(
        "INSERT INTO settings (key, value) VALUES ($1,$2) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, value,
    )
    _cache[key] = value   # reflect the write immediately (no TTL wait)


async def get_bool(key: str, default: bool = False) -> bool:
    v = await get(key)
    if v is None:
        return default
    return v == "1"


async def set_bool(key: str, value: bool) -> None:
    await set(key, "1" if value else "0")


def invalidate() -> None:
    """Force the next get() to reload from the DB."""
    global _at
    _at = 0.0
