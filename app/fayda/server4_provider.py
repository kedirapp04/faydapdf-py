"""Native Server-4 (Fayda app v1.1.9) provider — no dependency on fayda-railway.

Faithful port of faydapdf-railway/server3AuthFlow.js (Server-4 path):
  1. take a FRESH single-use App Check token from the ntknpro pool,
  2. GET /api/v2/auth/authorize with X-Firebase-AppCheck  (cached ~10 min template),
  3. eSignet: authorize page → csrf → oauth-details → send-otp,
  4. eSignet: authenticate(OTP) → auth-code → { code },
  5. POST /api/v2/auth/callback  { code, codeVerifier, state } with a fresh token,
  6. render the returned user data into a PDF (reportlab).

⚠️ NEEDS A LIVE TEST PASS: the exact eSignet request shapes / headers / the PDF
layout can only be confirmed against a real pool token + the Fayda backend. Until
then, API mode is the safe default.
"""
import base64
import hashlib
import os
import secrets
import time
from urllib.parse import urlparse, parse_qs

import aiohttp

from .. import config
from .base import FaydaProvider, ok, err
from . import pdf_render

_TIMEOUT = aiohttp.ClientTimeout(total=60)

# In-memory auth sessions (send_otp → verify_pdf). Keyed by an opaque id we return
# as `session`. A user stays on one bot/process between the two steps, so this is
# safe; sessions are short-lived and swept on use / TTL.
_SESSIONS: dict[str, dict] = {}
_SESSION_TTL = 600  # 10 min

# authorize-template cache (the static eSignet link; per-user PKCE stamped on top).
_authorize_cache = {"url": None, "at": 0.0}


# ── helpers ──────────────────────────────────────────────────────────────────
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _pkce() -> dict:
    verifier = _b64url(os.urandom(96))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return {"verifier": verifier, "challenge": challenge}


def _state() -> str:
    return f"{_b64url(os.urandom(18))}.{_b64url(os.urandom(6))}"


def _now() -> float:
    return time.time()


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _browser_headers(extra: dict | None = None) -> dict:
    base = config.ESIGNET_BASE
    h = {
        "accept": "application/json, text/plain, */*",
        "origin": base,
        "referer": f"{base}/login?state=fayda-app",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    }
    if extra:
        h.update(extra)
    return h


def _esignet_headers(sess: dict, extra: dict | None = None) -> dict:
    e = {
        "Content-Type": "application/json",
        "X-XSRF-TOKEN": sess["xsrf"],
        "oauth-details-hash": sess["oauth_hash"],
        "oauth-details-key": sess["transaction_id"],
    }
    if extra:
        e.update(extra)
    return _browser_headers(e)


def _oauth_details_request(authorize_url: str) -> dict:
    q = parse_qs(urlparse(authorize_url).query)
    g = lambda k: (q.get(k) or [None])[0]
    claims = g("claims")
    import json
    claims_obj = None
    if claims:
        try:
            claims_obj = json.loads(claims)
        except Exception:
            claims_obj = None
    return {
        "nonce": g("nonce"), "state": g("state"), "clientId": g("client_id"),
        "redirectUri": g("redirect_uri"), "responseType": g("response_type"),
        "scope": g("scope"), "acrValues": g("acr_values"), "claims": claims_obj,
        "claimsLocales": g("claims_locales"), "display": g("display"),
        "maxAge": g("max_age"), "prompt": g("prompt"), "uiLocales": g("ui_locales"),
        "codeChallenge": g("code_challenge"), "codeChallengeMethod": g("code_challenge_method"),
    }


def _hash_oauth_details(oauth_details: dict) -> str:
    import json
    return _b64url(hashlib.sha256(json.dumps(oauth_details, separators=(",", ":")).encode()).digest())


def _payload(data: dict) -> dict:
    return (data or {}).get("response") or (data or {}).get("data") or (data or {})


def _esignet_error(data: dict) -> str | None:
    errs = (data or {}).get("errors")
    if isinstance(errs, list) and errs:
        e = errs[0]
        return str(e.get("errorMessage") or e.get("errorCode") or "eSignet error")
    return None


async def _sweep():
    """Drop expired auth sessions AND close their aiohttp ClientSession (otherwise a
    user who requests an OTP and never enters it leaks a connection)."""
    dead = [k for k, v in _SESSIONS.items() if _now() - v["at"] > _SESSION_TTL]
    for k in dead:
        v = _SESSIONS.pop(k, None)
        if v and v.get("http"):
            try:
                await v["http"].close()
            except Exception:
                pass


# ── flow ─────────────────────────────────────────────────────────────────────
async def take_pool_token(min_seconds: int | None = None) -> str:
    url, csrf = config.SERVER4_TOKEN_API_URL, config.SERVER4_TOKEN_API_CSRF
    if not url or not csrf:
        return ""
    secs = config.SERVER4_TOKEN_MIN_SECONDS if min_seconds is None else min_seconds
    if secs > 0:
        url += ("&" if "?" in url else "?") + f"min_seconds={secs}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, headers={"X-CSRF-Token": csrf}) as r:
                d = await r.json(content_type=None)
                if str(d.get("status") or "").lower() in ("", "active", "warning"):
                    return str(d.get("token") or d.get("value") or "").strip()
    except aiohttp.ClientError:
        pass
    return ""


def _stats_url() -> str:
    """Where to read pool health from. Explicit SERVER4_TOKEN_STATS_URL wins; else
    derive it from the token URL by swapping the last path segment to 'stats'."""
    if config.SERVER4_TOKEN_STATS_URL:
        return config.SERVER4_TOKEN_STATS_URL
    base = config.SERVER4_TOKEN_API_URL
    if not base:
        return ""
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(base)
    path = p.path.rsplit("/", 1)[0] + "/stats" if "/" in p.path else "/stats"
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


async def pool_status() -> dict:
    """Best-effort health read of the ntknpro token pool (for the admin dashboard).
    Returns {ok, status, data} or {ok:False, error}. Does NOT consume a token."""
    url = _stats_url()
    if not url:
        return {"ok": False, "error": "token API not configured"}
    headers = {}
    if config.SERVER4_TOKEN_API_CSRF:
        headers["X-CSRF-Token"] = config.SERVER4_TOKEN_API_CSRF
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, headers=headers) as r:
                data = await r.json(content_type=None)
                return {"ok": True, "status": r.status, "url": url, "data": data if isinstance(data, dict) else {"value": data}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}


def _backend_headers(app_check: str | None = None) -> dict:
    h = {"accept": "application/json, text/plain, */*", "Content-Type": "application/json",
         "x-api-key": config.FAYDA_BACKEND_API_KEY}
    if app_check:
        h["X-Firebase-AppCheck"] = app_check
    return h


async def _authorize(http: aiohttp.ClientSession, pkce: dict, state: str) -> str:
    """Return the eSignet authorize URL (from cache, or one fresh token refreshes it)."""
    ttl = config.SERVER4_AUTHORIZE_CACHE_MS / 1000.0
    if ttl > 0 and _authorize_cache["url"] and (_now() - _authorize_cache["at"]) < ttl:
        template = _authorize_cache["url"]
    else:
        token = await take_pool_token()
        if not token:
            raise RuntimeError("no App Check token (pool empty/unreachable)")
        url = (config.FAYDA_API_BASE + "/api/v2/auth/authorize"
               + f"?codeChallenge={pkce['challenge']}&state={state}")
        async with http.get(url, headers=_backend_headers(token)) as r:
            data = await r.json(content_type=None)
        template = data.get("data") or data.get("url") or data.get("authUrl") or data
        if not isinstance(template, str) or not template.startswith("http"):
            raise RuntimeError("authorize did not return a URL")
        if ttl > 0:
            _authorize_cache.update(url=template, at=_now())
    # stamp THIS request's PKCE + state on the (possibly cached) template
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(template)
    q = dict(parse_qsl(parts.query))
    q.update(code_challenge=pkce["challenge"], code_challenge_method="S256", state=state)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


async def _init_esignet(http: aiohttp.ClientSession, authorize_url: str) -> dict:
    p = urlparse(authorize_url)
    # 1) authorize page (sets cookies)
    async with http.get(f"{config.ESIGNET_BASE}{p.path}?{p.query}",
                        headers=_browser_headers({"accept": "text/html,application/xhtml+xml,*/*;q=0.8"})):
        pass
    # 2) csrf
    async with http.get(f"{config.ESIGNET_BASE}/v1/esignet/csrf/token",
                        headers=_browser_headers({"Content-Type": "application/json", "referer": authorize_url})) as r:
        csrf_data = await r.json(content_type=None)
    xsrf = ""
    for c in http.cookie_jar:
        if c.key == "XSRF-TOKEN":
            xsrf = c.value
    xsrf = xsrf or csrf_data.get("token") or csrf_data.get("csrfToken") or (csrf_data.get("response") or {}).get("token") or ""
    if not xsrf:
        raise RuntimeError("no eSignet CSRF token")
    # 3) oauth-details
    body = {"requestTime": _iso(), "request": _oauth_details_request(authorize_url)}
    async with http.post(f"{config.ESIGNET_BASE}/v1/esignet/authorization/v2/oauth-details",
                        headers=_browser_headers({"Content-Type": "application/json", "X-XSRF-TOKEN": xsrf, "referer": authorize_url}),
                        json=body) as r:
        od = await r.json(content_type=None)
    if _esignet_error(od):
        raise RuntimeError(_esignet_error(od))
    details = _payload(od)
    txn = details.get("transactionId")
    if not txn:
        raise RuntimeError("eSignet oauth-details returned no transactionId")
    return {"xsrf": xsrf, "oauth_details": details, "oauth_hash": _hash_oauth_details(details), "transaction_id": txn}


class Server4Provider(FaydaProvider):
    name = "server4"

    async def send_otp(self, individual_id: str) -> dict:
        await _sweep()
        http = aiohttp.ClientSession(timeout=_TIMEOUT)
        try:
            pkce, state = _pkce(), _state()
            authorize_url = await _authorize(http, pkce, state)
            sess = await _init_esignet(http, authorize_url)
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "individualId": individual_id,
                "otpChannels": config.FAYDA_OTP_CHANNELS, "captchaToken": None}}
            async with http.post(f"{config.ESIGNET_BASE}/v1/esignet/authorization/send-otp",
                                headers=_esignet_headers(sess), json=body) as r:
                d = await r.json(content_type=None)
            if _esignet_error(d):
                await http.close()
                return err(_esignet_error(d))
            sid = secrets.token_hex(12)
            _SESSIONS[sid] = {"http": http, "sess": sess, "pkce": pkce, "state": state,
                              "individual": individual_id, "at": _now()}
            masked = (d.get("response") or {}).get("maskedMobile")
            return ok(session=sid, masked_mobile=masked)
        except Exception as e:
            await http.close()
            return err(f"Server-4 send-OTP failed: {e}")

    async def verify_pdf(self, session, otp: str) -> dict:
        st = _SESSIONS.pop(str(session), None)
        if not st:
            return err("Session expired — send the FIN again.")
        http, sess, pkce, state, individual = st["http"], st["sess"], st["pkce"], st["state"], st["individual"]
        try:
            # authenticate(OTP)
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "individualId": individual,
                "challengeList": [{"authFactorType": "OTP", "challenge": otp, "format": "alpha-numeric"}]}}
            async with http.post(f"{config.ESIGNET_BASE}/v1/esignet/authorization/v2/authenticate",
                                headers=_esignet_headers(sess), json=body) as r:
                ad = await r.json(content_type=None)
            if _esignet_error(ad):
                return err(_esignet_error(ad))
            details = sess["oauth_details"]
            accepted = list(details.get("essentialClaims") or []) + list(details.get("voluntaryClaims") or [])
            accepted = sorted(set(x for x in accepted if x))
            # auth-code
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "acceptedClaims": accepted, "permittedAuthorizeScopes": []}}
            async with http.post(f"{config.ESIGNET_BASE}/v1/esignet/authorization/auth-code",
                                headers=_esignet_headers(sess), json=body) as r:
                cd = await r.json(content_type=None)
            if _esignet_error(cd):
                return err(_esignet_error(cd))
            code = _payload(cd).get("code")
            if not code:
                return err("eSignet returned no authorization code.")
            # callback with a FRESH single-use token
            token = await take_pool_token()
            cb_body = {"code": code, "codeVerifier": pkce["verifier"], "state": state}
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as cb:
                async with cb.post(config.FAYDA_API_BASE + "/api/v2/auth/callback",
                                   headers=_backend_headers(token or None), json=cb_body) as r:
                    user = await r.json(content_type=None)
            pdf_bytes, name = pdf_render.render(user)
            # Screenshots (front/back/photo-qr) are best-effort — a failure here must
            # NEVER stop the PDF from being delivered.
            shots = []
            try:
                from . import screenshot_render
                shots = screenshot_render.render(user)
            except Exception as e:
                print("[screenshot_render]", e)
            return ok(pdf=pdf_bytes, filename=f"{name}.pdf", screenshots=shots)
        except Exception as e:
            return err(f"Server-4 verify failed: {e}")
        finally:
            await http.close()

    async def forgot_fan(self, name: str, phone: str) -> dict:
        # Recovery is mode-independent; callers should use fayda.forgot_fan(), which
        # routes through the API provider. Kept here only as a safety net.
        return err("Forgot-FAN needs API mode configured (FAYDA_API_URL / FAYDA_API_KEY).")
