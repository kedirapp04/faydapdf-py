"""Key/value runtime settings (admin-toggleable — e.g. active Fayda mode, paused)."""
from ..db import pool


async def get(key: str, default: str | None = None) -> str | None:
    v = await pool().fetchval("SELECT value FROM settings WHERE key=$1", key)
    return v if v is not None else default


async def set(key: str, value: str) -> None:
    await pool().execute(
        "INSERT INTO settings (key, value) VALUES ($1,$2) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, value,
    )


async def get_bool(key: str, default: bool = False) -> bool:
    v = await get(key)
    if v is None:
        return default
    return v == "1"


async def set_bool(key: str, value: bool) -> None:
    await set(key, "1" if value else "0")
