"""API mode — calls the fayda-railway HTTP API (the proven Server-4 engine).

POST {BASE}/api/session            {individualId}       -> {sessionId, maskedMobile}
POST {BASE}/api/session/:id/verify {otp, format:pdf}    -> application/pdf bytes
POST {BASE}/api/forgot-fan         {name, phone}        -> {phone, message}
Header: x-api-key
"""
import asyncio
from urllib.parse import unquote

import aiohttp

from .. import config
from .base import FaydaProvider, ok, err

_TIMEOUT = aiohttp.ClientTimeout(total=60)


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
                    return ok(session=data.get("sessionId"), masked_mobile=data.get("maskedMobile"))
                return err(str(data.get("error") or "Couldn't send the OTP."))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return err(f"Service is unreachable right now. ({type(e).__name__})")

    async def verify_pdf(self, session, otp: str) -> dict:
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
