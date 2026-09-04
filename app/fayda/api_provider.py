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
        body = {"otp": otp, "format": "pdf"}
        # Forward the scanned QR (if any) so the gateway embeds the REAL, verifiable
        # QR on the card instead of generating one.
        qr = _QR_BY_SESSION.pop(str(session), None)
        if qr:
            body["qr"] = base64.b64encode(qr).decode()
        try:
            http = await self._http()
            async with http.post(f"/api/session/{session}/verify", json=body) as r:
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
