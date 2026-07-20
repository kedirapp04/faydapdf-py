"""Maintenance mode — mirrors faydapdf-railway's maintenance toggle.

Two levels so the operator can degrade gracefully instead of hard-stopping:

  * off  — normal operation.
  * low  — only DOWNLOAD attempts are blocked (Get PDF / Get Screenshot buttons,
           or a bare 12–16 digit FIN/FAN). Wallet, Add-balance, forgot-FAN still work,
           so users can keep topping up while the download backend is being fixed.
  * high — the bot is effectively closed: EVERY DB-touching action shows the notice.
           Only /start and ❓ Help work.

Admins always bypass. The notice is bilingual (custom text overrides the default),
typically pointing users to the free bot while a payment/backend issue is fixed.
"""
from ..repo import settings as settings_repo
from .. import i18n

LEVELS = ("off", "low", "high")
CYCLE = {"off": "low", "low": "high", "high": "off"}
LABELS = {"off": "Off", "low": "Low (downloads only)", "high": "High (everything)"}


async def level() -> str:
    """Current maintenance level. Never raises — a DB blip reads as 'off' so the
    gate fails OPEN (we don't want a settings hiccup to lock everyone out)."""
    try:
        v = await settings_repo.get("maintenance_level", "off")
    except Exception:
        return "off"
    return v if v in LEVELS else "off"


async def message() -> str:
    """The notice users see. Admin's custom text wins; otherwise the bilingual default."""
    try:
        custom = await settings_repo.get("maintenance_message")
    except Exception:
        custom = None
    if custom and custom.strip():
        return custom.strip()
    return i18n.t("maintenance_default")


async def set_level(lvl: str) -> str:
    lvl = lvl if lvl in LEVELS else "off"
    await settings_repo.set("maintenance_level", lvl)
    return lvl


async def set_message(msg: str) -> None:
    await settings_repo.set("maintenance_message", (msg or "").strip())
