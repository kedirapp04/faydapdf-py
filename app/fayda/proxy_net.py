"""Send the Fayda-facing HTTP calls out through an external CONNECT proxy, so Fayda
sees the PROXY's IP instead of this host's.

Used by download mode 'proxy' (see fayda/__init__.py), which runs the same native
Server-4 flow as 'server4' — the ONLY difference is the exit IP. There is no
manager/dispatcher in between: this bot still drives every step itself.

Deliberately NOT proxied: the Server-4 token pool. That is our own server, so a
proxy hop would add latency and a failure point and hide nothing.

The proxy URL is admin-set (`relay_proxy_url`), e.g. http://user:pass@VPS-IP:8888 —
aiohttp reads the credentials straight out of the URL.
"""
import time

import aiohttp

from .. import config
from ..repo import settings as settings_repo

SETTING = "relay_proxy_url"


async def proxy_url() -> str:
    """The configured proxy, or "" — admin setting first, env as the seed."""
    try:
        v = (await settings_repo.get(SETTING) or "").strip()
    except Exception:
        v = ""                      # DB down → fall back to the env value
    return v or config.RELAY_PROXY_URL


class _ProxySession(aiohttp.ClientSession):
    """A session that routes every request through `proxy`.

    aiohttp has no session-level proxy option (only a per-request `proxy=`), and the
    Server-4 flow issues its requests from many places, so injecting it here is the
    only way to be sure none of them slips out directly. A caller that passes its own
    `proxy=` still wins.
    """
    # ClientSession warns on unknown attributes unless they're declared here.
    ATTRS = aiohttp.ClientSession.ATTRS | {"_forced_proxy"}

    def __init__(self, *a, proxy: str = "", **kw):
        self._forced_proxy = proxy or None
        super().__init__(*a, **kw)

    async def _request(self, method, url, **kw):
        if self._forced_proxy and kw.get("proxy") is None:
            kw["proxy"] = self._forced_proxy
        return await super()._request(method, url, **kw)


def session_for(timeout, url: str) -> aiohttp.ClientSession:
    """A session pinned to an ALREADY-resolved proxy URL (empty = direct)."""
    return _ProxySession(timeout=timeout, proxy=url) if url else aiohttp.ClientSession(timeout=timeout)


def url_of(sess: aiohttp.ClientSession) -> str:
    """The proxy a session is pinned to, or "". Lets a multi-step flow keep every leg
    on the SAME exit IP even if the admin edits the setting halfway through."""
    return getattr(sess, "_forced_proxy", None) or ""


async def session(timeout, use_proxy: bool) -> aiohttp.ClientSession:
    """A session for Fayda-facing traffic: proxied in 'proxy' mode, direct otherwise.

    With no proxy URL configured this returns a DIRECT session rather than failing —
    the download still goes through (from this host's IP), which is the same
    behaviour as plain Server-4 mode.
    """
    return session_for(timeout, await proxy_url() if use_proxy else "")


async def _probe_fayda(url: str, timeout) -> dict:
    """Fallback when the proxy's host allowlist blocks the IP-echo service: prove it can
    reach Fayda itself, which is the only host downloads care about."""
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(config.ESIGNET_BASE, proxy=url) as r:
                status = r.status
    except aiohttp.ClientHttpProxyError as e:
        return {"ok": False, "error": f"proxy blocks {config.ESIGNET_BASE} too (HTTP {e.status})"
                                      " — add .fayda.et to PROXY_ALLOW_HOSTS"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "ip": "", "direct_ip": "", "changed": True,
            "ms": int((time.monotonic() - t0) * 1000),
            "note": f"reached Fayda through the proxy (HTTP {status}). Exit IP not shown — "
                    "the proxy's host allowlist blocks the IP-echo service. Add ifconfig.me "
                    "to PROXY_ALLOW_HOSTS to see it."}


async def probe(url: str = "", test_url: str = "https://ifconfig.me/ip") -> dict:
    """Admin 'Test' button. Fetches the exit IP through the proxy and again directly,
    so the answer isn't just 'it connected' but 'Fayda would see THIS IP, and it is
    (not) the same one we use now'."""
    url = (url or "").strip() or await proxy_url()
    if not url:
        return {"ok": False, "error": "No proxy URL configured."}
    t0 = time.monotonic()
    to = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=to) as s:
            async with s.get(test_url, proxy=url) as r:
                ip = (await r.text()).strip()[:60]
                if r.status != 200:
                    return {"ok": False, "error": f"proxy returned HTTP {r.status}"}
    except aiohttp.ClientHttpProxyError as e:
        if e.status == 403:
            # A tightened PROXY_ALLOW_HOSTS blocks the IP-echo host. That says nothing
            # about whether DOWNLOADS work, so ask the question that actually matters:
            # can it reach Fayda? Then we just can't name the exit IP.
            return await _probe_fayda(url, to)
        hint = " — check the username/password" if e.status == 407 else ""
        return {"ok": False, "error": f"proxy refused us: HTTP {e.status}{hint}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    ms = int((time.monotonic() - t0) * 1000)
    direct = ""
    try:
        async with aiohttp.ClientSession(timeout=to) as s:
            async with s.get(test_url) as r:
                direct = (await r.text()).strip()[:60]
    except Exception:
        pass
    return {"ok": True, "ip": ip, "direct_ip": direct, "ms": ms,
            "changed": bool(direct) and ip != direct}
