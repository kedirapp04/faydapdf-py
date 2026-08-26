"""Server 6 — FaydaPass / VeriFayda 2.0 (OpenID4VCI on pass.fayda.et).

The flow the FaydaPass wallet app uses. Unlike Servers 4/5 (which end at an
eSignet callback returning identity JSON), this runs the full OpenID4VCI issuance
and receives an SD-JWT verifiable credential signed by Fayda. The identity fields
live in the credential's disclosures.

    1  CSRF token                          (auth.pass.fayda.et)
    2  oauth-details v3  → transactionId
    3  send-otp                            (accepts a 12-digit FIN OR 16-digit FAN)
    4  authenticate v3 (OTP)
    5  claim-details → auth-code → { code }
    6  get-token (Mimoto proxy)  → access_token + c_nonce
    7  build a holder-binding proof JWT (ES256, per request)
    8  POST credential  → SD-JWT
    9  parse the SD-JWT disclosures → identity + photo

What sets it apart from Servers 4/5:
  * v3 eSignet endpoints, and it needs the CSRF cookie/header.
  * NO App Check token and NO credential secret — a public PKCE client.
  * A per-request holder-binding proof JWT signed with an EC P-256 key.
  * English only — the credential carries no Amharic, so the Amharic side of the
    card is blank for Server 6.
  * Accepts both FIN and FAN; the id used for send-otp MUST be reused for
    authenticate.

Reconstructed call-for-call from a live HAR + the decompiled app. See
server6-faydapass-vci.md.
"""
import asyncio
import base64
import hashlib
import json
import re
import secrets
import time

import aiohttp

from .. import config
from .base import FaydaProvider, ok, err
from . import cards
from .server4_provider import (
    EsignetError, _b64url, _pkce, _iso, _esignet_error, _json_step, _hash_oauth_details,
)

_TIMEOUT = aiohttp.ClientTimeout(total=60)
_CRED_TIMEOUT = aiohttp.ClientTimeout(total=max(35, config.PASS_TIMEOUT_S))

_SESSIONS: dict[str, dict] = {}
_SESSION_TTL = 600

FIN_FAN_RE = re.compile(r"^\d{12}$|^\d{16}$")


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


def _headers(extra: dict | None = None) -> dict:
    h = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "user-agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"),
        "origin": config.PASS_ESIGNET_BASE,
    }
    if extra:
        h.update(extra)
    return h


def _esignet_headers(sess: dict, extra: dict | None = None) -> dict:
    e = {
        "X-XSRF-TOKEN": sess["xsrf"],
        "oauth-details-key": sess["transaction_id"],
        "oauth-details-hash": sess["oauth_hash"],
    }
    if extra:
        e.update(extra)
    return _headers(e)


# ── Phases 1-2: CSRF + oauth-details v3 ──────────────────────────────────────
async def _init(http: aiohttp.ClientSession) -> dict:
    # CSRF: sets the XSRF-TOKEN cookie and returns the token.
    async with http.get(f"{config.PASS_ESIGNET_BASE}/v1/esignet/csrf/token",
                        headers=_headers()) as r:
        csrf = await r.json(content_type=None)
    xsrf = ""
    for c in http.cookie_jar:
        if c.key == "XSRF-TOKEN":
            xsrf = c.value
    xsrf = xsrf or csrf.get("token") or (csrf.get("response") or {}).get("token") or ""
    if not xsrf:
        raise RuntimeError("no CSRF token from pass.fayda.et")

    pkce = _pkce()
    nonce, state = secrets.token_hex(16), secrets.token_hex(16)
    body = {"requestTime": _iso(), "request": {
        "nonce": nonce, "state": state,
        "clientId": config.PASS_CLIENT_ID,
        "redirectUri": config.PASS_REDIRECT_URI,
        "responseType": "code",
        "scope": config.PASS_SCOPE,
        "codeChallenge": pkce["challenge"],
        "codeChallengeMethod": "S256"}}
    async with http.post(f"{config.PASS_ESIGNET_BASE}/v1/esignet/authorization/v3/oauth-details",
                        headers=_headers({"X-XSRF-TOKEN": xsrf}), json=body) as r:
        od = await _json_step(r, "oauth-details")
    if _esignet_error(od):
        raise EsignetError(_esignet_error(od))
    resp = od.get("response") or {}
    txn = resp.get("transactionId")
    if not txn:
        raise RuntimeError("oauth-details returned no transactionId")
    # oauth-details-hash = b64url(sha256(compact-json(response))) — verified against
    # the live client's own hash.
    return {"xsrf": xsrf, "transaction_id": txn, "oauth_hash": _hash_oauth_details(resp),
            "pkce": pkce, "nonce": nonce, "state": state}


# ── Phases 6-7: token + holder-binding proof ─────────────────────────────────
async def _get_token(http: aiohttp.ClientSession, code: str, verifier: str) -> dict:
    """Exchange the auth code at the Mimoto proxy (form-urlencoded, public client)."""
    data = {
        "grant_type": "authorization_code", "code": code,
        "client_id": config.PASS_CLIENT_ID,
        "redirect_uri": config.PASS_REDIRECT_URI,
        "code_verifier": verifier,
    }
    async with http.post(config.PASS_TOKEN_ENDPOINT, data=data,
                        headers={"content-type": "application/x-www-form-urlencoded",
                                 "accept": "application/json"}) as r:
        tok = await _json_step(r, "get-token")
    if not tok.get("access_token"):
        raise RuntimeError(f"get-token returned no access_token: {str(tok)[:120]}")
    return tok


def _build_proof(c_nonce: str) -> str:
    """The OpenID4VCI holder-binding proof JWT. A fresh EC P-256 key per request;
    the credential is bound to it (`cnf`). Signed ES256 with `cryptography`.

    `cryptography` is imported HERE, not at module top: a missing dependency then
    fails only Server-6 downloads instead of crashing the whole bot at import."""
    from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
    from cryptography.hazmat.primitives import hashes
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    x = _b64url(nums.x.to_bytes(32, "big"))
    y = _b64url(nums.y.to_bytes(32, "big"))
    jwk = {"kty": "EC", "crv": "P-256", "x": x, "y": y}
    header = {"alg": "ES256", "typ": "openid4vci-proof+jwt", "jwk": jwk}
    now = int(_now())
    payload = {"iss": config.PASS_CLIENT_ID, "nonce": c_nonce,
               "aud": config.PASS_PROOF_AUDIENCE, "iat": now, "exp": now + 18 * 3600}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r_int, s_int = asym_utils.decode_dss_signature(der)          # DER → raw R||S for JOSE
    sig = _b64url(r_int.to_bytes(32, "big") + s_int.to_bytes(32, "big"))
    return signing_input + "." + sig


# English only — byte-faithful to the working FaydaPass request. The issuer's own
# metadata advertises `en` only (the FaydaDigitalCredential config has no Amharic),
# so requesting `am` here cannot yield Amharic values and some issuers reject an
# unadvertised locale. If Fayda ever adds Amharic, to_record() already picks it up
# with no change here.
_CLAIM_DISPLAY = {
    "name": "Full Name", "birthdate": "Date of Birth", "gender": "Gender",
    "address": "Address", "phone_number": "Phone Number", "email": "Email",
    "picture": "Photo", "individual_id": "Fayda ID",
}


async def _get_credential(http: aiohttp.ClientSession, access_token: str, c_nonce: str) -> str:
    body = {
        "claims": {k: {"display": [{"name": v, "locale": "en"}]} for k, v in _CLAIM_DISPLAY.items()},
        "format": "vc+sd-jwt",
        "proof": {"jwt": _build_proof(c_nonce), "proof_type": "jwt"},
        "vct": config.PASS_VCT,
    }
    async with http.post(config.PASS_CREDENTIAL_ENDPOINT, json=body,
                        headers={"authorization": f"Bearer {access_token}",
                                 "content-type": "application/json; charset=utf-8",
                                 "accept": "application/json"},
                        timeout=_CRED_TIMEOUT) as r:
        cd = await _json_step(r, "credential")
    cred = cd.get("credential")
    if not cred or not isinstance(cred, str):
        raise RuntimeError(f"credential endpoint returned no credential: {str(cd)[:120]}")
    return cred


# ── Phase 9: parse the SD-JWT ────────────────────────────────────────────────
def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_sd_jwt(credential: str) -> dict:
    """SD-JWT (`<jwt>~<disclosure>~…`) → flat {claim: value}. Each disclosure is
    base64url([salt, key, value]); we keep key→value."""
    parts = credential.split("~")
    out: dict = {}
    for d in parts[1:]:
        if not d.strip():
            continue
        try:
            arr = json.loads(_b64url_decode(d))
        except Exception:
            continue
        if isinstance(arr, list) and len(arr) == 3:
            out[str(arr[1])] = arr[2]
    return out


_EMAIL_TEMPLATE = re.compile(r"\$\{.*\}")   # the VC carries a Velocity template when empty


def _lang(value, want: str) -> str:
    """Pull one language out of a claim value that may be a plain string, a
    bilingual array [{language, value}], or a {lang: value} dict. Unknown shapes
    fall back to str() for English and "" for Amharic."""
    if isinstance(value, list):
        for it in value:
            if isinstance(it, dict) and str(it.get("language", "")).lower().startswith(want[:2]):
                return str(it.get("value") or "").strip()
        return ""
    if isinstance(value, dict) and any(k in value for k in ("en", "am", "eng", "amh")):
        for k in ((("en", "eng")) if want == "en" else ("am", "amh")):
            if value.get(k):
                return str(value[k]).strip()
        return ""
    return str(value if value is not None else "").strip() if want == "en" else ""


def to_record(claims: dict, fallback_id: str = "") -> dict:
    """SD-JWT claims → the flat record the PDF/card/screenshot layers understand.

    The credential is normally English-only, so the *_amh fields stay blank — but
    if the issuer returns Amharic (bilingual value, or a `<claim>_am`/`#am`
    disclosure), it is picked up here."""
    def s(v):
        return str(v if v is not None else "").strip()

    def en(key):
        return _lang(claims.get(key), "en")

    def am(key):
        # explicit Amharic disclosure wins, else a bilingual value's am part
        for alt in (f"{key}_am", f"{key}_amh", f"{key}#am", f"{key}_AM"):
            if claims.get(alt):
                return s(claims[alt])
        return _lang(claims.get(key), "am")

    addr = claims.get("address")
    if isinstance(addr, str):
        try:
            addr = json.loads(addr)
        except Exception:
            addr = {}
    addr = addr if isinstance(addr, dict) else {}
    addr_am = claims.get("address_am") or claims.get("address_amh")
    if isinstance(addr_am, str):
        try:
            addr_am = json.loads(addr_am)
        except Exception:
            addr_am = {}
    addr_am = addr_am if isinstance(addr_am, dict) else {}
    aeng = lambda k: _lang(addr.get(k), "en")
    aamh = lambda k: s(addr_am.get(k)) or _lang(addr.get(k), "am")

    email = en("email")
    if _EMAIL_TEMPLATE.search(email):
        email = ""
    photo = s(claims.get("picture"))
    fan = en("individual_id") or fallback_id
    return {
        "fullName_eng": en("name"), "fullName_amh": am("name"),
        "gender_eng": en("gender"), "gender_amh": am("gender"),
        "dateOfBirth_eng": en("birthdate"),
        "citizenship_Eng": "Ethiopian", "citizenship_amh": "ኢትዮጵያዊ",
        "phone": en("phone_number"),
        "email": email,
        "region_eng": aeng("region"), "region_amh": aamh("region"),
        "zone_eng": aeng("zone"), "zone_amh": aamh("zone"),
        "woreda_eng": aeng("woreda"), "woreda_amh": aamh("woreda"),
        "fcn": fan, "fin": fan,
        "photo": re.sub(r"^data:image/\w+;base64,", "", photo),
    }


def _card_data(rec: dict) -> dict:
    return {
        "fullName_eng": rec["fullName_eng"], "fullName_amh": rec["fullName_amh"],
        "sex_eng": rec["gender_eng"], "sex_amh": rec["gender_amh"],
        "dobGc": rec["dateOfBirth_eng"], "fan": rec["fcn"], "fin": rec["fin"],
        "nationality_eng": rec["citizenship_Eng"], "nationality_amh": rec["citizenship_amh"],
        "region_eng": rec["region_eng"], "region_amh": rec["region_amh"],
        "zone_eng": rec["zone_eng"], "zone_amh": rec["zone_amh"],
        "woreda_eng": rec["woreda_eng"], "woreda_amh": rec["woreda_amh"],
        "phone": rec["phone"], "photo": rec["photo"],
    }


def _b64(b: bytes | None) -> str:
    return base64.b64encode(b).decode() if b else ""


class Server6Provider(FaydaProvider):
    name = "server6"

    async def send_otp(self, individual_id: str) -> dict:
        iid = str(individual_id or "").strip()
        if not FIN_FAN_RE.match(iid):
            return err("Please send a 12-digit FIN or a 16-digit FAN.")
        await _sweep()
        http = aiohttp.ClientSession(timeout=_TIMEOUT)
        try:
            sess = await _init(http)
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "individualId": iid,
                "otpChannels": config.PASS_OTP_CHANNELS, "captchaToken": None}}
            async with http.post(f"{config.PASS_ESIGNET_BASE}/v1/esignet/authorization/send-otp",
                                headers=_esignet_headers(sess), json=body) as r:
                d = await _json_step(r, "send-otp")
            if _esignet_error(d):
                await http.close()
                return err(_esignet_error(d))
            sid = secrets.token_hex(12)
            _SESSIONS[sid] = {"http": http, "sess": sess, "individual": iid, "at": _now()}
            masked = (d.get("response") or {}).get("maskedMobile")
            return ok(session=sid, masked_mobile=masked)
        except EsignetError as e:
            await http.close()
            return err(str(e))
        except Exception as e:
            await http.close()
            print("[server6] send-otp failed:", e)
            return err("Couldn't send the OTP — please try again.")

    async def verify_pdf(self, session, otp: str) -> dict:
        st = _SESSIONS.pop(str(session), None)
        if not st:
            return err("Session expired — send the FIN/FAN again.")
        http, sess, iid = st["http"], st["sess"], st["individual"]
        try:
            # authenticate v3 (OTP)
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "individualId": iid,
                "challengeList": [{"authFactorType": "OTP", "challenge": otp, "format": "alpha-numeric"}]}}
            async with http.post(f"{config.PASS_ESIGNET_BASE}/v1/esignet/authorization/v3/authenticate",
                                headers=_esignet_headers(sess), json=body) as r:
                ad = await _json_step(r, "authenticate")
            if _esignet_error(ad):
                return err(_esignet_error(ad))
            # claim-details (no consent for this client) then auth-code
            async with http.get(f"{config.PASS_ESIGNET_BASE}/v1/esignet/authorization/claim-details",
                                headers=_esignet_headers(sess)):
                pass
            body = {"requestTime": _iso(), "request": {
                "transactionId": sess["transaction_id"], "acceptedClaims": [],
                "permittedAuthorizeScopes": []}}
            async with http.post(f"{config.PASS_ESIGNET_BASE}/v1/esignet/authorization/auth-code",
                                headers=_esignet_headers(sess), json=body) as r:
                cd = await _json_step(r, "auth-code")
            if _esignet_error(cd):
                return err(_esignet_error(cd))
            code = (cd.get("response") or {}).get("code")
            if not code:
                return err("Couldn't complete the download — please try again.")
            # token → proof → credential
            tok = await _get_token(http, code, sess["pkce"]["verifier"])
            cred = await _get_credential(http, tok["access_token"], tok.get("c_nonce") or "")
            rec = to_record(parse_sd_jwt(cred), iid)
            if not (rec["fullName_eng"] or rec["fcn"]):
                return err("Couldn't retrieve your Fayda data — please try again.")

            # This API returns data, not card images — draw them (English only).
            drawn = await cards.build(_card_data(rec))
            if drawn["qr"]:
                rec["QRCodes"] = _b64(drawn["qr"])
            if drawn["front"]:
                rec["fronts"] = _b64(drawn["front"])
            if drawn["back"]:
                rec["backs"] = _b64(drawn["back"])

            payload = {"user": {"data": rec}}          # the shape the renderers require
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
            print("[server6] verify failed:", e)
            return err("Couldn't complete the download — please try again.")
        finally:
            await http.close()

    async def forgot_fan(self, name: str, phone: str) -> dict:
        return err("Forgot-FAN needs API mode configured (FAYDA_API_URL / FAYDA_API_KEY).")
