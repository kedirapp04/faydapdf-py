"""Server 8 — the Card Order Portal (card-order.fayda.et).

A Next.js App Router site whose backend is reached through React Server Actions
(the RSC "Flight" stream), NOT a REST API. Two actions, both POST / with a
`Next-Action` header and a text/plain body:

  /otpService/getToken       {fcn}                        -> {transactionId, maskedMobile}
  /otpService/validateToken  {token, fcn, transactionId}  -> identity ($2) + photo ($3)

The verify response is a Flight stream of chunks `<id>:<value>` where a binary
chunk is `T<hexBase64Len>,<base64>`: one chunk base64-decodes to the mosip.id.read
identity JSON (bilingual lang-arrays, FIN in its own UIN), another to the portrait
PNG. The portal's own qrCode is empty/unreliable, so we draw OUR OWN QR and render
the PDF locally — same path as Server 5. FAN-only (a 12-digit FIN is not a valid
login for this portal).

The `Next-Action` hash is part of Fayda's Next.js build and CHANGES whenever they
redeploy card-order.fayda.et. It's admin-editable (setting `server8_next_action`,
env SERVER8_NEXT_ACTION as the seed) so it can be refreshed with no code change —
see the doc's "Next.js Action Hash Invalidation".
"""
import asyncio
import base64
import datetime
import json
import re
import secrets
import time

import aiohttp

from .. import config
from ..repo import settings as settings_repo
from .base import FaydaProvider, ok, err

_SEND_TIMEOUT = aiohttp.ClientTimeout(total=25)
_VERIFY_TIMEOUT = aiohttp.ClientTimeout(total=45)   # the photo stream can be a few hundred KB

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
# The exact router-state-tree the portal's client sends (URL-encoded, as a header).
_ROUTER_STATE_TREE = ("%5B%22%22%2C%7B%22children%22%3A%5B%22home%22%2C%7B%22children%22%3A%5B%22"
                      "__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D")
_FAN_RE = re.compile(r"^\d{16}$")

# token -> {"txn": <transactionId>, "fan": <fcn>, "at": <ts>}
_SESSIONS: dict[str, dict] = {}
_TTL = 600


def _sweep() -> None:
    cut = time.time() - _TTL
    for k in [k for k, v in _SESSIONS.items() if v["at"] < cut]:
        _SESSIONS.pop(k, None)


async def next_action() -> str:
    """Admin setting `server8_next_action` first, env SERVER8_NEXT_ACTION as the seed."""
    try:
        v = (await settings_repo.get("server8_next_action") or "").strip()
    except Exception:
        v = ""
    return v or config.SERVER8_NEXT_ACTION


class Server8Provider(FaydaProvider):
    name = "server8"

    async def _headers(self) -> dict:
        return {
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "text/x-component",
            "Origin": config.SERVER8_BASE,
            "Referer": config.SERVER8_BASE + "/",
            "User-Agent": _UA,
            "Next-Action": await next_action(),
            "Next-Router-State-Tree": _ROUTER_STATE_TREE,
        }

    async def _action(self, body: list, timeout) -> tuple[int, str]:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(config.SERVER8_BASE + "/", headers=await self._headers(),
                                 data=json.dumps(body)) as r:
                return r.status, await r.text()

    async def send_otp(self, individual_id: str) -> dict:
        fan = "".join(str(individual_id or "").split())
        if not _FAN_RE.match(fan):
            return err("This needs a 16-digit FAN. Please send your 16-digit FAN.")
        body = [{"id": "", "version": "1.0.0", "requesttime": None, "metadata": {},
                 "request": {"fcn": fan, "promoCode": "", "isOrderEdit": False, "isReprint": False}},
                "/otpService/getToken", True]
        try:
            status, text = await self._action(body, _SEND_TIMEOUT)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")
        env = _envelope(_parse_rsc(text))
        resp = (env or {}).get("response") or {}
        txn = resp.get("transactionId")
        if status == 200 and txn:
            _sweep()
            sid = secrets.token_hex(12)
            _SESSIONS[sid] = {"txn": str(txn), "fan": fan, "at": time.time()}
            return ok(session=sid, masked_mobile=resp.get("maskedMobile"))
        return err(str((env or {}).get("message") or resp.get("message")
                       or _action_stale_hint(status, text) or "Couldn't send the OTP."))

    async def verify_pdf(self, session, otp: str) -> dict:
        st = _SESSIONS.pop(str(session), None)
        if not st:
            return err("Session expired — send the FAN again.")
        body = [{"id": "", "version": "1.0.0", "requesttime": _rsc_now(),
                 "request": {"token": otp, "fcn": st["fan"], "transactionId": st["txn"],
                             "isOrderEdit": False}},
                "/otpService/validateToken", True]
        try:
            status, text = await self._action(body, _VERIFY_TIMEOUT)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")
        chunks = _parse_rsc(text)
        identity, photo_b64 = _identity_and_photo(chunks)
        if not identity:
            env = _envelope(chunks) or {}
            resp = env.get("response") or {}
            return err(str(env.get("message") or resp.get("message")
                           or _action_stale_hint(status, text) or "Couldn't verify the OTP."))
        rec = _record(identity, st["fan"], photo_b64)
        if not (rec["fullName_eng"] or rec["fullName_amh"] or rec["fcn"]):
            return err("Couldn't retrieve your Fayda data — please try again.")
        return await _build_pdf(rec)

    async def forgot_fan(self, name: str, phone: str) -> dict:
        return err("FAN recovery isn't available on this server.")


# ── RSC (React Flight) stream parsing ────────────────────────────────────────
def _parse_rsc(text: str) -> dict:
    """Chunks are `<id>:<value>`. A binary chunk is `T<hexBase64Len>,<base64>` (read
    that exact many chars, NOT newline-delimited); a JSON chunk is a brace-balanced
    value; anything else runs to end-of-line."""
    chunks: dict[str, tuple[str, str]] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in "\r\n":
            i += 1
        if i >= n:
            break
        c = text.find(":", i)
        if c < 0:
            break
        cid = text[i:c]
        if not cid.isdigit():
            break
        i = c + 1
        if i < n and text[i] == "T":
            comma = text.find(",", i)
            if comma < 0:
                break
            try:
                ln = int(text[i + 1:comma], 16)
            except ValueError:
                break
            i = comma + 1
            chunks[cid] = ("bin", text[i:i + ln])
            i += ln
        elif i < n and text[i] in "{[":
            end = _scan_json_end(text, i)
            chunks[cid] = ("json", text[i:end])
            i = end
        else:
            nl = text.find("\n", i)
            end = nl if nl >= 0 else n
            chunks[cid] = ("val", text[i:end])
            i = end
    return chunks


def _scan_json_end(text: str, start: int) -> int:
    depth = 0
    instr = esc = False
    for k in range(start, len(text)):
        ch = text[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return k + 1
    return len(text)


def _envelope(chunks: dict) -> dict | None:
    """The status envelope chunk (has `status`/`response`, and is not the `{"a":..}` root)."""
    for _cid, (kind, val) in chunks.items():
        if kind != "json":
            continue
        try:
            j = json.loads(val)
        except Exception:
            continue
        if isinstance(j, dict) and "a" not in j and ("response" in j or "status" in j):
            return j
    return None


def _identity_and_photo(chunks: dict):
    """Find the identity (a bin chunk that base64-decodes to mosip.id.read JSON) and the
    portrait (a bin chunk that decodes to PNG/JPEG). Located by content, so a shift in
    chunk ids can't break it."""
    identity = None
    photo_b64 = None
    for _cid, (kind, val) in chunks.items():
        if kind != "bin":
            continue
        raw = _b64d(val)
        if not raw:
            continue
        if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
            photo_b64 = base64.b64encode(raw).decode()
            continue
        if raw[:1] in (b"{", b"["):
            try:
                j = json.loads(raw)
            except Exception:
                continue
            ident = ((j.get("response") or {}).get("identity")) if isinstance(j, dict) else None
            if isinstance(ident, dict):
                identity = ident
    return identity, photo_b64


# ── identity → record → PDF (same path Server 5/7-JSON use) ──────────────────
def _lang(arr, code: str) -> str:
    if isinstance(arr, list):
        return str(next((x.get("value") for x in arr
                         if isinstance(x, dict) and str(x.get("language", "")).startswith(code)), "") or "").strip()
    return str(arr or "").strip()


def _record(identity: dict, fan: str, photo_b64: str | None) -> dict:
    dob = str(identity.get("dateOfBirth") or "").strip()          # "2001/01/13"
    uin = str(identity.get("UIN") or identity.get("uin") or "").strip()
    return {
        "fullName_eng": _lang(identity.get("fullName"), "eng"),
        "fullName_amh": _lang(identity.get("fullName"), "amh"),
        "gender_eng": _lang(identity.get("gender"), "eng"),
        "gender_amh": _lang(identity.get("gender"), "amh"),
        "dateOfBirth_eng": dob.replace("-", "/"),
        "dateOfBirth_et": _ethiopian(dob),
        "citizenship_Eng": _lang(identity.get("residenceStatus"), "eng"),
        "citizenship_amh": _lang(identity.get("residenceStatus"), "amh"),
        "phone": str(identity.get("phone") or "").strip(),
        "region_eng": _lang(identity.get("region"), "eng"), "region_amh": _lang(identity.get("region"), "amh"),
        "zone_eng": _lang(identity.get("zone"), "eng"), "zone_amh": _lang(identity.get("zone"), "amh"),
        "woreda_eng": _lang(identity.get("woreda"), "eng"), "woreda_amh": _lang(identity.get("woreda"), "amh"),
        "fcn": fan, "fin": uin,
        "photo": photo_b64 or "",
    }


def _card_data(rec: dict) -> dict:
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


async def _build_pdf(rec: dict) -> dict:
    """Draw OUR QR from the identity (per s5_qr_gen) + cards, then render via OUR PDF
    generator — identical to Server 5's local path."""
    from . import cards, js_render
    try:
        qr_gen = (await settings_repo.get("s5_qr_gen") or "data").strip()
        drawn = await cards.build(_card_data(rec), qr_gen=qr_gen)
    except Exception:
        drawn = {"qr": None, "front": None, "back": None}
    if drawn.get("qr"):
        rec["QRCodes"] = _b64(drawn["qr"])
    if drawn.get("front"):
        rec["fronts"] = _b64(drawn["front"])
    if drawn.get("back"):
        rec["backs"] = _b64(drawn["back"])
    pdf_rec = dict(rec)
    for k in ("fronts", "backs"):
        if pdf_rec.get(k):
            pdf_rec[k] = _b64(cards.shrink_jpeg(base64.b64decode(pdf_rec[k])))
    try:
        pdf_bytes, name = await js_render.render({"user": {"data": pdf_rec}}, engine=config.PDF_ENGINE)
    except Exception:
        return err("Couldn't render the document — please try again.")
    if not pdf_bytes or pdf_bytes[:5] != b"%PDF-":
        return err("The document came back empty. Please try again.")
    return ok(pdf=pdf_bytes, filename=f"{name}.pdf")


# ── small helpers ────────────────────────────────────────────────────────────
def _b64(b: bytes | None) -> str:
    return base64.b64encode(b).decode() if b else ""


def _b64d(s: str) -> bytes:
    # The Flight stream uses URL-safe base64 (- and _), so CONVERT those to +/ rather
    # than stripping them (stripping shifts the alignment and corrupts the tail). Then
    # drop whitespace/padding and re-pad to a clean multiple of 4 before decoding.
    s = str(s).replace("-", "+").replace("_", "/")
    s = re.sub(r"[^A-Za-z0-9+/]", "", s)
    if not s:
        return b""
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        return b""


def _rsc_now() -> str:
    """React Flight Date marker: a "$D<iso>" string."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return "$D" + now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _ethiopian(dob: str) -> str:
    d = (dob or "").replace("/", "-").strip()
    if not d:
        return ""
    try:
        from .etdate import to_ethiopian_date
        return to_ethiopian_date(d)
    except Exception:
        return ""


def _action_stale_hint(status: int, text: str) -> str | None:
    """A redeployed portal answers a stale Next-Action with HTML/404 rather than a Flight
    stream — turn that into an actionable message instead of a blank failure."""
    low = (text or "")[:400].lower()
    if status != 200 or "<!doctype" in low or "<html" in low or "text/x-component" not in low and "0:" not in (text or "")[:8]:
        return ("The Card Order portal changed — the Server 8 action key needs updating "
                "(admin panel → Server 8 Next-Action).")
    return None
