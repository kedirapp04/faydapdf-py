"""Server 5 — the resident-portal identity path.

Replays what the resident portal web app does: authorize with the RESIDENT
portal's own OAuth client, run the standard eSignet OTP exchange, then call two
api-resident endpoints to pull the full identity record — photo, bilingual
name/address, UIN.

    1  build the authorize URL          (local, no network, NO App Check token)
    2  init eSignet session + send OTP  (shared with Server 4)
    3  authenticate(OTP) → auth code    (shared with Server 4)
    4  exchangeAutheCode  → bare JWT    (api-resident)
    5  exchangeResident   → identity    (api-resident, ~3 MB)

Two things set it apart from Server 4:

* **It draws no pool token.** The resident portal is a plain OAuth client, so
  there is no `/api/v2/auth/authorize` call and no App Check token consumed —
  Server 5 keeps working when the token pool is empty.
* **It returns data, not pictures.** Server 4's callback hands over ready-made
  card images and a QR; this API does not, so the cards and QR are generated
  locally (see cards.py) before the PDF/screenshot layer sees the record.

FAN only: the resident client_id only ever asks for the 16-digit FAN. A 12-digit
FIN comes back as an eSignet error that reads like "your number is wrong" when it
is merely the wrong KIND of number for this server, so it is refused up front.
"""
import asyncio
import contextvars
import re
import secrets
import time
from urllib.parse import urlencode

import aiohttp

from .. import config
from ..repo import settings as settings_repo
from .base import FaydaProvider, ok, err
from . import cards, proxy_net
from .server4_provider import (
    EsignetError, esignet_auth_code, _init_esignet, _esignet_headers, _esignet_error,
    _json_step, _pkce, _state, _spoof_ip, _b64url, _iso, _proxy_ctx,
)

_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Phase 5 returns ~3 MB (photo + biometric XML). The base timeout is nowhere near
# enough on a slow link, and a timeout here costs the user their download.
_RESIDENT_TIMEOUT = aiohttp.ClientTimeout(total=max(35, config.RESIDENT_TIMEOUT_S))
_RESIDENT_RETRIES = 3

_SESSIONS: dict[str, dict] = {}
_SESSION_TTL = 600

FAN_RE = re.compile(r"^\d{16}$")

# The QR scanned off the user's Telebirr Fayda screenshot, set per download
# just like the VIP flag. Carrying the SCANNED QR matters: it keeps the real
# signature, so the finished card verifies. A generated one never can.
_qr_ctx = contextvars.ContextVar("s5_qr", default=None)


def set_scanned_qr(qr_png: bytes | None) -> None:
    _qr_ctx.set(qr_png or None)


def _now() -> float:
    return time.time()


async def _sweep():
    dead = [k for k, v in _SESSIONS.items() if _now() - v["at"] > _SESSION_TTL]
    for k in dead:
        v = _SESSIONS.pop(k, None)
        if v and v.get("http"):
            try:
                await v["http"].close()
            except Exception:
                pass


# ── Phase 1 ──────────────────────────────────────────────────────────────────
def _authorize_url(pkce: dict, nonce: str, state: str) -> str:
    """Built in-process. Unlike Servers 4, there is no backend authorize call and no
    App Check token spent here — the resident portal is a plain OAuth client."""
    # Parameter-for-parameter what resident.fayda.et sends (verified against a live
    # capture). No prompt/max_age — the real client omits them.
    q = {
        "client_id": config.RESIDENT_CLIENT_ID,
        "redirect_uri": config.RESIDENT_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile email",
        "acr_values": "mosip:idp:acr:generated-code",     # OTP
        "claims": '{"userinfo":{"individual_id":{"essential":true}}}',
        "code_challenge": pkce["challenge"],
        "code_challenge_method": "S256",
        "display": "page",
        "nonce": nonce,
        "state": state,
        "claims_locales": "en am",
        "ui_locales": "en",
    }
    return f"{config.ESIGNET_BASE}/authorize?{urlencode(q)}"


# ── Phases 4 + 5 ─────────────────────────────────────────────────────────────
async def basic_auth() -> str:
    """The api-resident Basic credential — admin setting first, env as the seed.

    Accepts what people actually paste: the bare base64, a whole
    "Basic <base64>" header, or plain "resident:<secret>" (encoded here). Getting
    this subtly wrong is otherwise a 401 with nothing to see.
    """
    try:
        v = (await settings_repo.get("resident_basic_auth") or "").strip()
    except Exception:
        v = ""                       # DB down → fall back to the env value
    v = v or config.RESIDENT_BASIC_AUTH
    if v[:6].lower() == "basic ":
        v = v[6:].strip()
    if ":" in v and not v.endswith("="):     # looks like raw user:secret
        import base64 as _b64
        v = _b64.b64encode(v.encode()).decode()
    return v


def _resident_headers(basic: str, id_token: str | None = None) -> dict:
    h = {"Content-Type": "application/json", "Authorization": f"Basic {basic}"}
    if id_token:
        h["X-Authorization"] = f"Bearer {id_token}"
    return h


_JWT_RE = re.compile(r"^[\w-]+\.[\w-]*\.[\w-]+$")


async def _exchange_auth_code(http: aiohttp.ClientSession, code: str, sess: dict, basic: str) -> str:
    """Auth code → id_token. The middleware answers text/html carrying a BARE JWT;
    anything that is not a dotted JWT (a JSON error envelope, an HTML error page) is
    a failure, not a token."""
    body = {"code": code, "code_verifier": sess["pkce"]["verifier"],
            "redirect_uri": config.RESIDENT_REDIRECT_URI,
            "client_id": config.RESIDENT_CLIENT_ID,
            "nonce": sess["nonce"], "state": sess["state"]}
    async with http.post(f"{config.RESIDENT_API_BASE}/esignet/exchangeAutheCode",
                         headers=_resident_headers(basic), json=body) as r:
        text = (await r.text()).strip().strip('"')
        status = r.status
    if not _JWT_RE.match(text):
        snippet = " ".join(text.split())[:140]
        raise RuntimeError(f"exchangeAutheCode returned no JWT (HTTP {status}): {snippet}")
    return text


async def _exchange_resident(http: aiohttp.ClientSession, id_token: str, basic: str) -> dict:
    """id_token → the full identity record (~3 MB).

    Retries transport failures only — the JWT stays valid across the retry window, so
    a dropped connection on a 3 MB download is worth another go, while an auth
    rejection is not and must surface immediately.
    """
    body = {"headers": {"Content-Type": "application/json"}}
    last = None
    for attempt in range(1, _RESIDENT_RETRIES + 1):
        try:
            async with http.post(f"{config.RESIDENT_API_BASE}/esignet/exchangeResident",
                                 headers=_resident_headers(basic, id_token), json=body,
                                 timeout=_RESIDENT_TIMEOUT) as r:
                if r.status >= 500:
                    last = RuntimeError(f"exchangeResident HTTP {r.status}")
                    raise last
                if r.status != 200:
                    raise RuntimeError(f"exchangeResident HTTP {r.status}")
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
            last = e
            if attempt >= _RESIDENT_RETRIES or not _retryable(e):
                break
            await asyncio.sleep(0.8 * attempt)
    raise last or RuntimeError("exchangeResident failed")


def _retryable(e: Exception) -> bool:
    if isinstance(e, (aiohttp.ClientConnectionError, asyncio.TimeoutError)):
        return True
    return "HTTP 5" in str(e)


# ── transform ────────────────────────────────────────────────────────────────
def _lang(value, want: str) -> str:
    """The resident API returns bilingual fields as
       [{language:'eng', value:'…'}, {language:'amh', value:'…'}]
    Pull one language out; pass plain strings through unchanged."""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("language", "")).lower().startswith(want[:3]):
                return str(item.get("value") or "").strip()
        return ""
    return str(value or "").strip()


def _identity(resp: dict) -> dict:
    """Locate `data.identity` in the response, tolerating the usual nestings."""
    for path in (("data", "identity"), ("identity",), ("data", "data", "identity"), ("response", "identity")):
        cur = resp
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict) and cur:
            return cur
    return resp.get("data") if isinstance(resp.get("data"), dict) else {}


def _photo(resp: dict, ident: dict) -> str:
    for src in (ident, resp, resp.get("data") if isinstance(resp.get("data"), dict) else {}):
        if isinstance(src, dict):
            for k in ("photo", "face", "image", "profileImage"):
                v = src.get(k)
                if isinstance(v, str) and len(v) > 256:
                    return re.sub(r"^data:image/\w+;base64,", "", v)
    return ""


def to_record(resp: dict, fan: str) -> dict:
    """Resident response → the flat record the PDF, card and screenshot layers all
    already understand. Keeping the shape identical to Server 4's is what lets
    Server 5 reuse the entire delivery path unchanged."""
    d = _identity(resp)
    g = lambda *keys: next((d[k] for k in keys if d.get(k) not in (None, "")), "")
    eng = lambda *keys: _lang(g(*keys), "eng")
    amh = lambda *keys: _lang(g(*keys), "amh")
    from .etdate import to_ethiopian_date
    dob = str(g("dateOfBirth", "birthdate", "dob") or "").strip()
    uin = str(g("UIN", "uin", "vid", "VID") or "").strip()
    return {
        "fullName_eng": eng("fullName", "name"),
        "fullName_amh": amh("fullName", "name"),
        "gender_eng": eng("gender", "sex"),
        "gender_amh": amh("gender", "sex"),
        "dateOfBirth_eng": dob,
        # The API gives only the Gregorian date; the card's Amharic-font DOB is the
        # Ethiopian-calendar form, computed here (Server 4's API supplied it ready-made).
        "dateOfBirth_et": to_ethiopian_date(dob),
        "citizenship_Eng": eng("nationality", "residenceStatus"),
        "citizenship_amh": amh("nationality", "residenceStatus"),
        "phone": str(g("phone", "phoneNumber") or "").strip(),
        "region_eng": eng("region"), "region_amh": amh("region"),
        "zone_eng": eng("zone"), "zone_amh": amh("zone"),
        "woreda_eng": eng("woreda"), "woreda_amh": amh("woreda"),
        "fcn": fan or uin,
        "fin": uin,
        "photo": _photo(resp, d),
    }


def _card_data(rec: dict) -> dict:
    """The card generator uses its own field names (sex_*, dobGc, fan, nationality_*)."""
    return {
        "fullName_eng": rec["fullName_eng"], "fullName_amh": rec["fullName_amh"],
        "sex_eng": rec["gender_eng"], "sex_amh": rec["gender_amh"],
        "dobGc": rec["dateOfBirth_eng"],
        "fan": rec["fcn"], "fin": rec["fin"],
        "nationality_eng": rec["citizenship_Eng"], "nationality_amh": rec["citizenship_amh"],
        "region_eng": rec["region_eng"], "region_amh": rec["region_amh"],
        "zone_eng": rec["zone_eng"], "zone_amh": rec["zone_amh"],
        "woreda_eng": rec["woreda_eng"], "woreda_amh": rec["woreda_amh"],
        "phone": rec["phone"], "photo": rec["photo"],
    }


def _b64(b: bytes | None) -> str:
    import base64
    return base64.b64encode(b).decode() if b else ""


class Server5Provider(FaydaProvider):
    name = "server5"

    async def send_otp(self, individual_id: str) -> dict:
        if not FAN_RE.match(str(individual_id or "").strip()):
            return err("This needs a 16-digit FAN. Please send your 16-digit FAN.")
        if not await basic_auth():
            return err("This download option is not available right now. Please try again later.")
        await _sweep()
        # Server 5 can also run through the VPS proxy — same flow, different exit IP.
        http = await proxy_net.session(_TIMEOUT, _proxy_ctx.get())
        try:
            # 32-char hex nonce/state, exactly like the portal's own client. Server 4's
            # dotted state format is a different client's convention — don't borrow it
            # here just because the module is next door.
            pkce = _pkce()
            state, nonce = secrets.token_hex(16), secrets.token_hex(16)
            spoof_ip = await _spoof_ip()
            sess = await _init_esignet(http, _authorize_url(pkce, nonce, state), spoof_ip)
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "individualId": individual_id,
                "otpChannels": config.RESIDENT_OTP_CHANNELS, "captchaToken": None}}
            async with http.post(f"{config.ESIGNET_BASE}/v1/esignet/authorization/send-otp",
                                 headers=_esignet_headers(sess), json=body) as r:
                d = await _json_step(r, "send-otp")
            if _esignet_error(d):
                await http.close()
                return err(_esignet_error(d))
            sid = secrets.token_hex(12)
            _SESSIONS[sid] = {"http": http, "sess": sess, "pkce": pkce, "state": state,
                              "nonce": nonce, "individual": individual_id, "at": _now(),
                              "proxy": proxy_net.url_of(http), "qr": _qr_ctx.get()}
            masked = (d.get("response") or {}).get("maskedMobile")
            return ok(session=sid, masked_mobile=masked)
        except Exception as e:
            await http.close()
            # Keep the reason in the logs; users get a plain message with no server naming.
            print("[server5] send-otp failed:", e)
            return err("Couldn't send the OTP — please try again.")

    async def verify_pdf(self, session, otp: str) -> dict:
        st = _SESSIONS.pop(str(session), None)
        if not st:
            return err("Session expired — send the FAN again.")
        http, sess = st["http"], st["sess"]
        try:
            basic = await basic_auth()
            code = await esignet_auth_code(http, sess, st["individual"], otp)
            id_token = await _exchange_auth_code(http, code, st, basic)
            resp = await _exchange_resident(http, id_token, basic)
            rec = to_record(resp, st["individual"])
            if not (rec["fullName_eng"] or rec["fullName_amh"] or rec["fcn"]):
                # Never charge for a blank template — make the user retry instead.
                return err("Couldn't retrieve your Fayda data — please try again.")

            # This API returns data, not pictures: draw the QR and cards before the
            # PDF layer sees the record, so it renders exactly like Server 4's.
            # With a scanned QR the card carries the user's REAL, verifiable code.
            # Without one (typed-FAN path, admin-enabled per bot) the QR is built
            # from the identity data and will not pass verification — the user is
            # warned before the download starts.
            drawn = await cards.build(_card_data(rec), qr_png=st.get("qr"))
            if drawn["qr"]:
                rec["QRCodes"] = _b64(drawn["qr"])
            if drawn["front"]:
                rec["fronts"] = _b64(drawn["front"])
            if drawn["back"]:
                rec["backs"] = _b64(drawn["back"])

            # Hand the renderers the SAME shape Server 4's callback has. The JS
            # renderer only looks under user.data / data.user.data / data — a flat
            # record silently yields an empty page, i.e. a blank PDF the user still
            # gets charged for.
            payload = {"user": {"data": rec}}
            from . import js_render
            pdf_bytes, name = await js_render.render(payload, engine=config.PDF_ENGINE)
            shots = []
            try:
                from . import screenshot_render
                shots = await asyncio.to_thread(screenshot_render.render, payload)
            except Exception as e:
                print("[screenshot_render]", e)
            return ok(pdf=pdf_bytes, filename=f"{name}.pdf", screenshots=shots)
        except EsignetError as e:
            return err(str(e))
        except Exception as e:
            print("[server5] verify failed:", e)
            return err("Couldn't complete the download — please try again.")
        finally:
            await http.close()

    async def forgot_fan(self, name: str, phone: str) -> dict:
        return err("Forgot-FAN needs API mode configured (FAYDA_API_URL / FAYDA_API_KEY).")
