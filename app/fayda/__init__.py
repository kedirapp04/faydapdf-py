"""Fayda flow providers. The active mode is admin-selectable at runtime
(settings key `fayda_mode`):

  'api'     — the fayda-railway HTTP API (gateway does the work)
  'server4' — the native Server-4 flow, run here, from THIS host's IP
  'proxy'   — the same native Server-4 flow, but its Fayda-facing traffic is sent
              through an external CONNECT proxy (settings key `relay_proxy_url`),
              so Fayda sees the PROXY's IP. Still no gateway in between.
"""
from .. import config
from ..repo import settings as settings_repo
from .api_provider import ApiProvider
from .server4_provider import Server4Provider

MODES = ("api", "server4", "proxy")

_providers = {}


def _norm(mode) -> str:
    m = (mode or "").strip().lower()
    return m if m in MODES else "api"


def _make(mode: str):
    # 'proxy' is Server-4 with a different exit IP — same flow, same provider class.
    if mode in ("server4", "proxy"):
        return Server4Provider()
    return ApiProvider()


async def active_mode(bot_id: int | None = None) -> str:
    """The active download mode. A per-bot override (settings key `fayda_mode_<bot_id>`)
    wins when set; otherwise the global `fayda_mode`; otherwise the env default."""
    try:
        mode = ""
        if bot_id is not None:
            mode = (await settings_repo.get(f"fayda_mode_{bot_id}") or "").strip()
        if not mode:
            mode = await settings_repo.get("fayda_mode", config.FAYDA_MODE_DEFAULT)
    except Exception:
        mode = config.FAYDA_MODE_DEFAULT   # DB down → fall back to the env default
    return _norm(mode)


async def set_mode(mode: str, bot_id: int | None = None) -> str:
    """Set the GLOBAL mode, or a PER-BOT override when bot_id is given. For a per-bot
    override, mode 'global' (or empty) CLEARS it so that bot follows the global mode."""
    if bot_id is not None:
        if mode in ("global", "", None):
            await settings_repo.set(f"fayda_mode_{bot_id}", "")
            return "global"
        mode = _norm(mode)
        await settings_repo.set(f"fayda_mode_{bot_id}", mode)
        return mode
    mode = _norm(mode)
    await settings_repo.set("fayda_mode", mode)
    return mode


async def bot_mode_override(bot_id: int) -> str:
    """The raw per-bot override: 'api' / 'server4' / 'proxy' / '' (='global')."""
    try:
        return (await settings_repo.get(f"fayda_mode_{bot_id}") or "").strip()
    except Exception:
        return ""


async def get_provider(bot_id: int | None = None):
    mode = await active_mode(bot_id)
    if mode not in _providers:
        _providers[mode] = _make(mode)
    # Decide HERE whether this download exits through the proxy, so every caller gets
    # it right without having to know about proxy mode.
    from .server4_provider import set_proxy
    set_proxy(mode == "proxy")
    return _providers[mode], mode


def _api_provider() -> ApiProvider:
    if "api" not in _providers:
        _providers["api"] = ApiProvider()
    return _providers["api"]


async def forgot_fan_direct(phone: str) -> dict:
    """Forgot-FAN straight to id.et. Preferred over the gateway because it preserves the real
    reason (and id.et's retryAfter) instead of a generic 'service unavailable'."""
    from .forgot_direct import request_fcn_sms
    return await request_fcn_sms(phone)


async def forgot_fan(name: str, phone: str) -> dict:
    """FAN/FIN recovery — independent of the download mode. Always via the API
    provider when it's configured (Server-4 mode has no native recovery), else the
    active provider."""
    if config.FAYDA_API_URL and config.FAYDA_API_KEY:
        return await _api_provider().forgot_fan(name, phone)
    prov, _ = await get_provider()
    return await prov.forgot_fan(name, phone)


async def pool_status() -> dict:
    """Server-4 token-pool health (for the admin dashboard)."""
    from .server4_provider import pool_status as _ps
    return await _ps()


async def proxy_test(url: str = "") -> dict:
    """Check the proxy used by 'proxy' mode and report the IP Fayda would see."""
    from .proxy_net import probe
    return await probe(url)


def set_vip_context(vip: bool) -> None:
    """Route this download's Server-4 token pull to the regular or VIP pool."""
    from .server4_provider import set_vip
    set_vip(vip)
