"""Fayda flow providers. The active mode is admin-selectable at runtime
(settings key `fayda_mode`): 'api' (fayda-railway HTTP API) or 'server4'
(native Server-4 flow)."""
from .. import config
from ..repo import settings as settings_repo
from .api_provider import ApiProvider
from .server4_provider import Server4Provider

_providers = {}


def _make(mode: str):
    if mode == "server4":
        return Server4Provider()
    return ApiProvider()


async def active_mode() -> str:
    try:
        mode = await settings_repo.get("fayda_mode", config.FAYDA_MODE_DEFAULT)
    except Exception:
        mode = config.FAYDA_MODE_DEFAULT   # DB down → fall back to the env default
    return "server4" if mode == "server4" else "api"


async def set_mode(mode: str) -> str:
    mode = "server4" if mode == "server4" else "api"
    await settings_repo.set("fayda_mode", mode)
    return mode


async def get_provider():
    mode = await active_mode()
    if mode not in _providers:
        _providers[mode] = _make(mode)
    return _providers[mode], mode


def _api_provider() -> ApiProvider:
    if "api" not in _providers:
        _providers["api"] = ApiProvider()
    return _providers["api"]


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


def set_vip_context(vip: bool) -> None:
    """Route this download's Server-4 token pull to the regular or VIP pool."""
    from .server4_provider import set_vip
    set_vip(vip)
