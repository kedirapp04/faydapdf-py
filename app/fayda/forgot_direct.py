"""Forgot-FAN straight to id.et — so we can show the REAL reason.

The gateway API collapses every failure into a generic message and throws away id.et's
`retryAfter`, so the user could never be told how long to wait. Calling id.et ourselves
keeps the useful detail:

  200                      -> the FAN/FIN was SMSed to the registered phone
  429 + retryAfter         -> already requested for this ID in the last 24h (we show the wait)
  500 "Failed to send SMS" -> the phone has no Fayda record (id.et's way of saying not found)
  400/404                  -> not registered

id.et's body is PHONE-ONLY (`individualIdType: "Phone"`), verified across HAR captures — the
full name the web form collects is never sent upstream. Returns:
  {"ok": True,  "phone": "09…"}
  {"ok": False, "reason": …, "retry_after": <seconds|None>, "detail": <str|None>}
"""
import asyncio
import os
import re

import aiohttp

ENDPOINT = os.getenv("FORGOT_FAN_ENDPOINT",
                     "https://id.et/api/proxy/api/v2/user-features/resend-sms")
TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("FORGOT_FAN_TIMEOUT_S", "15")))

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://id.et",
    "Referer": "https://id.et/help",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"),
}


def normalize_phone(raw) -> "str | None":
    """0 / +251 / 251 / bare-9 (with spaces, dashes, parens) -> 09XXXXXXXX."""
    m = re.match(r"^(?:\+?251|0)?(9\d{8})$", re.sub(r"[\s\-()]", "", str(raw or "")))
    return "0" + m.group(1) if m else None


def _retry_after_seconds(body: dict, resp) -> "int | None":
    """id.et sends {"retryAfter": "7654 seconds"} (sometimes a bare number); fall back to the
    standard Retry-After header."""
    for val in (body.get("retryAfter"), body.get("retry_after"),
                resp.headers.get("Retry-After") if resp is not None else None):
        if val is None:
            continue
        m = re.search(r"\d+", str(val))
        if m:
            try:
                return max(0, int(m.group(0)))
            except ValueError:
                pass
    return None


async def request_fcn_sms(phone: str) -> dict:
    """Ask id.et to SMS the FAN/FIN to this phone."""
    normalized = normalize_phone(phone)
    if not normalized:
        return {"ok": False, "reason": "invalid_phone"}
    payload = {"individualIdType": "Phone", "individualId": normalized}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(ENDPOINT, json=payload, headers=_HEADERS) as r:
                try:
                    body = await r.json(content_type=None)
                except Exception:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                msg = str(body.get("message") or "")
                if r.status == 200 and not body.get("error"):
                    return {"ok": True, "phone": normalized, "detail": msg or None}
                if r.status == 429 or "last 24 hours" in msg.lower():
                    return {"ok": False, "reason": "rate_limited",
                            "retry_after": _retry_after_seconds(body, r), "detail": msg or None}
                # id.et answers 500 "Failed to send SMS" when the phone has no record.
                if r.status in (400, 404) or "failed to send sms" in msg.lower():
                    return {"ok": False, "reason": "not_registered", "detail": msg or None}
                return {"ok": False, "reason": "server_error",
                        "detail": msg or str(body.get("error") or f"HTTP {r.status}")}
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "network_error", "detail": "timeout"}
    except aiohttp.ClientError as e:
        return {"ok": False, "reason": "network_error", "detail": type(e).__name__}


def human_wait(seconds) -> str:
    """7654 -> '2 hours 7 minutes'; 1800 -> '30 minutes'; 45 -> 'less than a minute'."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    h, m = s // 3600, (s % 3600) // 60
    if h and m:
        return f"{h} hour{'s' if h > 1 else ''} {m} minute{'s' if m > 1 else ''}"
    if h:
        return f"{h} hour{'s' if h > 1 else ''}"
    if m:
        return f"{m} minute{'s' if m > 1 else ''}"
    return "less than a minute"
