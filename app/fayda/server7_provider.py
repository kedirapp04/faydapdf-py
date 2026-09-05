"""Server 7 — the otp.affiliate.pro.et hosted Fayda API.

A fully-managed third-party gateway that runs the whole Fayda flow on its own
(reachable) infrastructure. Two delivery types, admin-selectable (`server7_render`):

  'pdf'  — POST /verify-otp      -> a finished application/pdf, relayed as-is.
  'json' — POST /verify-otp-json -> identity JSON; we build the cards + PDF HERE
           (so screenshots and the same look as Server 4/5 are possible).

Shared:
  POST {BASE}/send-otp {fanNumber} -> {session, phone}
  GET  {BASE}/balance              -> {points, status, ...}

Header: x-api-key (admin setting `server7_api_key` first, env SERVER7_API_KEY as
seed). Each successful verify deducts 1 point; we call ONLY one verify endpoint
per OTP.
"""
import asyncio
import base64
import json
import re
import secrets
import time

import aiohttp

from .. import config
from ..repo import settings as settings_repo
from .base import FaydaProvider, ok, err

_SEND_TIMEOUT = aiohttp.ClientTimeout(total=60)
_VERIFY_TIMEOUT = aiohttp.ClientTimeout(total=120)   # remote work can take a few seconds

# token -> {"session": <opaque api session>, "fan": <fanNumber>, "at": <ts>}
_SESSIONS: dict[str, dict] = {}
_TTL = 600


def _sweep() -> None:
    cut = time.time() - _TTL
    for k in [k for k, v in _SESSIONS.items() if v["at"] < cut]:
        _SESSIONS.pop(k, None)


async def api_key() -> str:
    """Admin setting `server7_api_key` first, env SERVER7_API_KEY as the seed."""
    try:
        v = (await settings_repo.get("server7_api_key") or "").strip()
    except Exception:
        v = ""                        # DB down → fall back to the env value
    return v or config.SERVER7_API_KEY


async def render_mode() -> str:
    """'pdf' (relay the finished PDF) or 'json' (build the PDF here). Default 'pdf'."""
    try:
        v = (await settings_repo.get("server7_render") or "").strip().lower()
    except Exception:
        v = ""
    return v if v in ("pdf", "json") else "pdf"


class Server7Provider(FaydaProvider):
    name = "server7"

    async def send_otp(self, individual_id: str) -> dict:
        key = await api_key()
        if not config.SERVER7_API_URL or not key:
            return err("Server 7 is not configured — set the API key in the admin panel.")
        _sweep()
        fan = "".join(str(individual_id or "").split())      # API strips spaces; do it here too
        try:
            async with aiohttp.ClientSession(timeout=_SEND_TIMEOUT) as http:
                async with http.post(f"{config.SERVER7_API_URL}/send-otp",
                                     headers=_headers(key), json={"fanNumber": fan}) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200 and isinstance(data, dict) and data.get("session"):
                        sid = secrets.token_hex(12)
                        _SESSIONS[sid] = {"session": data["session"], "fan": fan, "at": time.time()}
                        return ok(session=sid, masked_mobile=data.get("phone"))
                    msg = (data or {}).get("message") if isinstance(data, dict) else None
                    return err(str(msg or "Couldn't send the OTP."))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")

    async def verify_pdf(self, session, otp: str) -> dict:
        st = _SESSIONS.pop(str(session), None)
        if not st:
            return err("Session expired — send the FAN again.")
        key = await api_key()
        if (await render_mode()) == "json":
            return await self._verify_json_build(st, otp, key)
        return await self._verify_pdf_relay(st, otp, key)

    async def _verify_pdf_relay(self, st: dict, otp: str, key: str) -> dict:
        """type=pdf: relay the finished PDF the API renders."""
        body = {"otp": otp, "fanNumber": st["fan"], "session": st["session"]}
        try:
            async with aiohttp.ClientSession(timeout=_VERIFY_TIMEOUT) as http:
                async with http.post(f"{config.SERVER7_API_URL}/verify-otp",
                                     headers=_headers(key), json=body) as r:
                    if r.status == 200:
                        raw = await r.read()
                        if raw[:5] == b"%PDF-" and len(raw) >= 2000:
                            _log_points(r)
                            return ok(pdf=raw, filename=_filename(r, st["fan"]))
                        return _err_from_bytes(raw)               # 200 that isn't a PDF
                    return await _err_from_json(r)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")

    async def _verify_json_build(self, st: dict, otp: str, key: str) -> dict:
        """type=json: pull identity JSON, then draw the cards + render the PDF HERE."""
        from . import cards, js_render
        body = {"otp": otp, "fanNumber": st["fan"], "session": st["session"]}
        try:
            async with aiohttp.ClientSession(timeout=_VERIFY_TIMEOUT) as http:
                async with http.post(f"{config.SERVER7_API_URL}/verify-otp-json",
                                     headers=_headers(key), json=body) as r:
                    data = await r.json(content_type=None)
                    if r.status != 200 or not (isinstance(data, dict) and data.get("success")):
                        return err(str((data or {}).get("message") or "Couldn't verify the OTP."),
                                   status=r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")
        d = data.get("data") or {}
        rec = _s7_record(d, st["fan"])
        if not (rec["fullName_eng"] or rec["fullName_amh"] or rec["fcn"]):
            return err("Couldn't retrieve your Fayda data — please try again.")
        # Draw OUR OWN QR from the identity data (per the s5_qr_gen setting) — we do NOT
        # embed the API's qrCode image. With no qr_png, cards.build generates it exactly
        # like Server 5's by-FAN path; the PDF then renders through OUR generator
        # (js_render), so the whole document matches the Server 4/5 format.
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
        # The PDF shows only small thumbnails, so embed downscaled copies (cuts ~1 MB).
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
        rp = data.get("remainingPoints")
        if rp is not None:
            print(f"[server7] verify-json ok, remaining points: {rp}")
        return ok(pdf=pdf_bytes, filename=f"{name}.pdf")

    async def forgot_fan(self, name: str, phone: str) -> dict:
        return err("FAN recovery isn't available on this server.")

    async def balance(self) -> dict:
        """Remaining API points — for an admin points check."""
        key = await api_key()
        if not config.SERVER7_API_URL or not key:
            return err("Server 7 is not configured.")
        try:
            async with aiohttp.ClientSession(timeout=_SEND_TIMEOUT) as http:
                async with http.get(f"{config.SERVER7_API_URL}/balance",
                                    headers={"x-api-key": key}) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200 and isinstance(data, dict) and data.get("success"):
                        return ok(points=data.get("points"), status=data.get("status"),
                                  username=data.get("username"))
                    return err(str((data or {}).get("message") or f"HTTP {r.status}"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")


# ── helpers ──────────────────────────────────────────────────────────────────
def _headers(key: str) -> dict:
    return {"x-api-key": key, "Content-Type": "application/json"}


def _b64(b: bytes | None) -> str:
    return base64.b64encode(b).decode() if b else ""


def _strip_datauri(s: str) -> str:
    """'data:image/png;base64,AAAA' -> 'AAAA'; a bare base64 string is returned as-is."""
    s = (s or "").strip()
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[1].strip()
    return s


def _s7_record(d: dict, fan: str) -> dict:
    """Server 7's nested JSON -> the flat record the PDF/card/screenshot layers use
    (the exact shape Server 4/5 produce, so the whole delivery path is reused)."""
    def L(node: str, key: str) -> str:
        n = d.get(node)
        return str((n.get(key) if isinstance(n, dict) else "") or "").strip()

    def A(part: str, key: str) -> str:
        addr = d.get("address") or {}
        n = addr.get(part) if isinstance(addr, dict) else None
        return str((n.get(key) if isinstance(n, dict) else "") or "").strip()

    dob = d.get("dateOfBirth") if isinstance(d.get("dateOfBirth"), dict) else {}
    dob_gc = str(dob.get("gregorian") or "").strip()
    dob_et = str(dob.get("ethiopian") or "").strip()
    if not dob_et and dob_gc:
        try:
            from .etdate import to_ethiopian_date
            dob_et = to_ethiopian_date(dob_gc)
        except Exception:
            dob_et = ""
    return {
        "fullName_eng": L("name", "english"), "fullName_amh": L("name", "amharic"),
        "gender_eng": L("gender", "english"), "gender_amh": L("gender", "amharic"),
        "dateOfBirth_eng": dob_gc, "dateOfBirth_et": dob_et,
        "citizenship_Eng": L("nationality", "english"), "citizenship_amh": L("nationality", "amharic"),
        "phone": str(d.get("phone") or "").strip(),
        "region_eng": A("city", "english"), "region_amh": A("city", "amharic"),
        "zone_eng": A("subCity", "english"), "zone_amh": A("subCity", "amharic"),
        "woreda_eng": A("woreda", "english"), "woreda_amh": A("woreda", "amharic"),
        "fcn": str(d.get("fanNumber") or fan or "").strip(),
        "fin": str(d.get("fin") or "").strip(),
        "photo": _strip_datauri(str(d.get("photo") or "")),
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


def _filename(r: aiohttp.ClientResponse, fan: str) -> str:
    disp = r.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp)
    name = (m.group(1).strip() if m else "") or f"Fayda-{fan}"
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def _log_points(r: aiohttp.ClientResponse) -> None:
    pts = r.headers.get("X-Remaining-Points")
    if pts:
        print(f"[server7] verify ok, remaining points: {pts}")


def _err_from_bytes(raw: bytes) -> dict:
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        return err(str(d.get("message") or "Couldn't verify the OTP."))
    except Exception:
        return err("The document came back empty. Please try again.")


async def _err_from_json(r: aiohttp.ClientResponse) -> dict:
    try:
        data = await r.json(content_type=None)
        return err(str((data or {}).get("message") or f"Couldn't verify the OTP (HTTP {r.status})."),
                   status=r.status)
    except Exception:
        return err(f"Couldn't verify the OTP (HTTP {r.status}).", status=r.status)
