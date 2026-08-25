"""Fayda flow providers. The active mode is admin-selectable at runtime
(settings key `fayda_mode`):

  'api'     — the fayda-railway HTTP API (gateway does the work)
  'server4' — the native Server-4 flow, run here, from THIS host's IP
  'proxy'   — the same native Server-4 flow, but its Fayda-facing traffic is sent
              through an external CONNECT proxy (settings key `relay_proxy_url`),
              so Fayda sees the PROXY's IP. Still no gateway in between.
  'server5' — the resident-portal identity path (api-resident.fayda.et). Same
              eSignet OTP exchange, different identity source, and it spends NO
              App Check pool token. 16-digit FAN only.
"""
from .. import config
from ..repo import settings as settings_repo
from .api_provider import ApiProvider
from .server4_provider import Server4Provider
from .resident_provider import Server5Provider

MODES = ("api", "server4", "proxy", "server5")

_providers = {}


def _norm(mode) -> str:
    m = (mode or "").strip().lower()
    return m if m in MODES else "api"


def _make(mode: str):
    if mode == "server5":
        return Server5Provider()
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


async def allow_typed_fan(bot_id: int | None = None) -> bool:
    """Server 5: may this bot start a download from a TYPED FAN, or is the QR
    screenshot required?

    Off by default, and deliberately so. A typed FAN cannot produce a verifiable
    QR — we refuse to fabricate one — so that download arrives as a plain PDF with
    no card and no QR. Turning this on is a decision about what users receive, not
    a convenience toggle, so it must be made explicitly per bot.
    """
    try:
        if bot_id is not None:
            v = (await settings_repo.get(f"s5_allow_fan_{bot_id}") or "").strip()
            if v:
                return v.lower() in ("1", "true", "yes", "on")
        return await settings_repo.get_bool("s5_allow_fan", False)
    except Exception:
        return False        # DB down → the stricter behaviour


async def set_allow_typed_fan(bot_id: int, allow: bool) -> bool:
    await settings_repo.set(f"s5_allow_fan_{bot_id}", "true" if allow else "false")
    return allow


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


def set_qr_context(qr_png: bytes | None) -> None:
    """Server 5: the QR scanned off the user's screenshot, to be drawn on the card.
    Passing the SCANNED QR keeps the real signature, so the card verifies; a QR
    rebuilt from the identity data carries a sample signature and never will."""
    from .resident_provider import set_scanned_qr
    set_scanned_qr(qr_png)


def set_vip_context(vip: bool) -> None:
    """Route this download's Server-4 token pull to the regular or VIP pool."""
    from .server4_provider import set_vip
    set_vip(vip)
