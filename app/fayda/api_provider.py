"""API mode — calls the fayda-railway HTTP API (the proven Server-4 engine).

POST {BASE}/api/session            {individualId}       -> {sessionId, maskedMobile}
POST {BASE}/api/session/:id/verify {otp, format:pdf}    -> application/pdf bytes
POST {BASE}/api/forgot-fan         {name, phone}        -> {phone, message}
Header: x-api-key
"""
import asyncio
import base64
import contextvars
from urllib.parse import unquote

import aiohttp

from .. import config
from .base import FaydaProvider, ok, err

_TIMEOUT = aiohttp.ClientTimeout(total=60)

# A QR scanned off the user's screenshot, set per-download before send_otp. The
# gateway's Server 5 embeds this REAL, verifiable QR on the card instead of
# generating one. Captured at send_otp keyed by the gateway sessionId, then sent at
# verify — the gateway builds the card at verify time.
_qr_ctx = contextvars.ContextVar("api_qr", default=None)
_QR_BY_SESSION: dict[str, bytes] = {}


def set_scanned_qr(qr_png: bytes | None) -> None:
    _qr_ctx.set(qr_png or None)


def _card_data_from_json(d: dict, photo: str) -> dict:
    """Map the gateway's JSON identity fields to the card generator's field names."""
    g = lambda *ks: next((d[k] for k in ks if d.get(k)), "")
    return {
        "fullName_eng": g("fullName_eng", "fullNameEng", "fullName"),
        "fullName_amh": g("fullName_amh", "fullNameAmh"),
        "sex_eng": g("gender_eng", "genderEng"),
        "sex_amh": g("gender_amh", "genderAmh"),
        "dobGc": g("dateOfBirth_eng", "dateOfBirthEng", "birthdate"),
        "fan": g("fcn", "vid", "VID", "FCN"),
        "fin": g("fin", "uin", "UIN"),
        "nationality_eng": g("citizenship_Eng", "citizenship_eng"),
        "nationality_amh": g("citizenship_amh"),
        "region_eng": g("region_eng"), "region_amh": g("region_amh"),
        "zone_eng": g("zone_eng"), "zone_amh": g("zone_amh"),
        "woreda_eng": g("woreda_eng"), "woreda_amh": g("woreda_amh"),
        "phone": g("phone"),
        "photo": photo,
    }


class ApiProvider(FaydaProvider):
    name = "api"

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=config.FAYDA_API_URL,
                headers={"x-api-key": config.FAYDA_API_KEY},
                timeout=_TIMEOUT,
            )
        return self._session

    async def send_otp(self, individual_id: str) -> dict:
        if not config.FAYDA_API_URL or not config.FAYDA_API_KEY:
            return err("API mode is not configured (FAYDA_API_URL / FAYDA_API_KEY).")
        try:
            http = await self._http()
            async with http.post("/api/session", json={"individualId": individual_id}) as r:
                data = await r.json(content_type=None)
                if r.status == 200 and data.get("ok"):
                    sid = data.get("sessionId")
                    qr = _qr_ctx.get()          # scanned QR for this download, if any
                    if sid and qr:
                        _QR_BY_SESSION[str(sid)] = qr
                    return ok(session=sid, masked_mobile=data.get("maskedMobile"))
                return err(str(data.get("error") or "Couldn't send the OTP."))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")

    async def verify_pdf(self, session, otp: str) -> dict:
        # Screenshot flow: a scanned QR → pull the identity DATA from the gateway
        # (Server 5, render:false — skip its card/PDF render) and build the PDF HERE
        # with the user's REAL scanned QR. Otherwise the gateway renders the PDF.
        qr = _QR_BY_SESSION.pop(str(session), None)
        if qr:
            return await self._verify_local_render(session, otp, qr)
        try:
            http = await self._http()
            async with http.post(f"/api/session/{session}/verify",
                                  json={"otp": otp, "format": "pdf"}) as r:
                if r.status == 200 and r.content_type == "application/pdf":
                    body = await r.read()
                    # Reject an empty/truncated document — never deliver + charge for it.
                    if not body or len(body) < 2000 or not body[:5].startswith(b"%PDF"):
                        return err("The document came back empty. Please try again.")
                    name = "fayda"
                    hdr = r.headers.get("X-Person-Name")
                    if hdr:
                        try:
                            name = unquote(hdr)
                        except Exception:
                            pass
                    return ok(pdf=body, filename=f"{name}.pdf")
                data = await r.json(content_type=None)
                return err(str(data.get("error") or "Couldn't verify the OTP."))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")

    async def _verify_local_render(self, session, otp: str, qr_png: bytes) -> dict:
        """api-mode screenshot flow: verify via the gateway in JSON mode to get the
        identity data (the gateway's Server 5 reaches the geo-restricted resident portal
        through its relay agents), then build the card + PDF HERE with the user's REAL
        scanned QR. The gateway needs no changes — plain format=json."""
        from . import cards, js_render
        try:
            http = await self._http()
            async with http.post(f"/api/session/{session}/verify",
                                  json={"otp": otp, "format": "json"}) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")
        if not (isinstance(data, dict) and data.get("ok")):
            return err(str((data or {}).get("error") or "Couldn't verify the OTP."))
        d = data.get("data") or {}
        photo = data.get("photo") or d.get("photo") or ""
        if not (d.get("fullName_eng") or d.get("fullName_amh") or d.get("fcn")):
            return err("Couldn't retrieve your Fayda data — please try again.")
        # Draw the card with the SCANNED QR, then render the PDF — both locally.
        try:
            drawn = await cards.build(_card_data_from_json(d, photo), qr_png=qr_png)
        except Exception:
            drawn = {"qr": None, "front": None, "back": None}
        b = lambda buf: base64.b64encode(buf).decode() if buf else ""
        pdf_rec = dict(d)
        pdf_rec["photo"] = photo
        if drawn.get("qr"):
            pdf_rec["QRCodes"] = b(drawn["qr"])
        for dst, src in (("fronts", "front"), ("backs", "back")):
            if drawn.get(src):
                pdf_rec[dst] = b(cards.shrink_jpeg(drawn[src]))
        try:
            pdf_bytes, name = await js_render.render({"user": {"data": pdf_rec}}, engine=config.PDF_ENGINE)
        except Exception:
            return err("Couldn't render the document — please try again.")
        if not pdf_bytes or not pdf_bytes[:5].startswith(b"%PDF"):
            return err("The document came back empty. Please try again.")
        return ok(pdf=pdf_bytes, filename=f"{name}.pdf")

    async def forgot_fan(self, name: str, phone: str) -> dict:
        try:
            http = await self._http()
            async with http.post("/api/forgot-fan", json={"name": name, "phone": phone}) as r:
                data = await r.json(content_type=None)
                if r.status == 200 and data.get("ok"):
                    return ok(phone=data.get("phone"), message=data.get("message"))
                return err(str(data.get("error") or "Couldn't send the recovery SMS."), status=r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")
