"""Pure-Python Fayda QR scanner — reads the QR out of a Telebirr / National-ID
screenshot without any Node subprocess.

Replaces the old Node bridge (cards_node) for SCANNING. Motivation:
  * zxing-cpp (a compiled C++ wheel, no system libraries) decodes the dense
    new-format COSE QR that the Node @zxing/library could NOT read at all — every
    real screenshot tested decodes, including a 576px Telegram-compressed photo.
  * 60–160 ms per scan vs 5–13 s for the Node path, and no node_modules to install
    (so it works on Railway, where the card-drawing node_modules is absent).

Decoders tried in order: zxing-cpp (primary, self-contained), then pyzbar/zbar if
present (a different engine, extra robustness), then OpenCV. The identity data on
a download still comes from the Fayda API — the QR is scanned only for the FAN and
to put the user's REAL (verifiable) QR back on the card.

Logic ported from fayda-ocr-api/qr_decoder.py.
"""
import io
import re

import cv2
import numpy as np
import zxingcpp

try:                                   # optional: a second engine, needs libzbar
    from pyzbar.pyzbar import decode as _zbar_decode
except Exception:
    _zbar_decode = None

import cbor2
import qrcode

_SIGN_MARKER = ":SIGN:"
_DLT_MARKER = ":DLT:"
_COSE_TAG = 18
_GENDER = {0: "Unknown", 1: "Male", 2: "Female", 3: "Other"}
_SAMPLE_SIG = re.compile(r"INVALID_SIGNATURE_SAMPLE")


# ── decode the raw payload from the image ────────────────────────────────────
def _read_payload(image_bytes: bytes):
    """Return the decoded QR payload as (bytes, text) — either may be None — or
    (None, None) if nothing decodes. Upscales a little for small/compressed shots."""
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, None

    scales = (1, 2, 3)
    for scale in scales:
        im = img if scale == 1 else cv2.resize(img, None, fx=scale, fy=scale,
                                               interpolation=cv2.INTER_CUBIC)
        # zxing-cpp: gives clean bytes for a byte-mode QR (the COSE case).
        try:
            for r in zxingcpp.read_barcodes(im) or []:
                raw = bytes(r.bytes) if getattr(r, "bytes", None) else None
                text = r.text or None
                if raw or text:
                    return raw, text
        except Exception:
            pass

    # pyzbar (zbar) — a different engine; often reads what others miss. zbar UTF-8-
    # expands binary, so keep both the bytes and the text for recovery downstream.
    if _zbar_decode is not None:
        from PIL import Image
        pil = Image.open(io.BytesIO(image_bytes))
        for scale in scales:
            im2 = pil if scale == 1 else pil.resize((pil.width * scale, pil.height * scale))
            try:
                res = _zbar_decode(im2)
            except Exception:
                res = None
            if res:
                data = res[0].data
                try:
                    return data, data.decode("utf-8")
                except UnicodeDecodeError:
                    return data, None

    # OpenCV last (text only).
    try:
        det = cv2.QRCodeDetector()
        single, _, _ = det.detectAndDecode(img)
        if single:
            return None, single
    except Exception:
        pass
    return None, None


def _recover_cose(raw, text):
    """The COSE payload starts with 0xD2 (CBOR tag 18). zbar hands back UTF-8-expanded
    bytes, so re-encoding its text as latin-1 recovers the true payload."""
    for cand in (raw, (text.encode("latin1") if text else None)):
        if cand and cand[:1] == b"\xd2":
            return cand
    return None


# ── COSE_Sign1 → FAN + identity ──────────────────────────────────────────────
def _compact_date(v: str) -> str:
    v = str(v or "")
    if len(v) == 8 and v.isdigit():
        return f"{v[0:4]}/{v[4:6]}/{v[6:8]}"       # YYYY/MM/DD (matches the API)
    return v.replace("-", "/")


def _parse_cose(cose_bytes: bytes) -> dict:
    outer = cbor2.loads(cose_bytes)
    arr = outer.value if isinstance(outer, cbor2.CBORTag) else outer
    if not (isinstance(arr, (list, tuple)) and len(arr) == 4):   # cbor2 yields tuples
        raise ValueError("not a COSE_Sign1 array")
    payload = cbor2.loads(arr[2])                  # element 2 = the CWT claims
    if not isinstance(payload, dict):
        raise ValueError("COSE payload is not a map")
    claims = payload.get(169) or {}
    fan = str(payload.get(2) or "")
    return {
        "fan": fan,
        "full_name": str(claims.get(4) or ""),
        "birth_date": _compact_date(claims.get(8) or ""),
        "gender": _GENDER.get(claims.get(9), ""),
        "signed": True,                            # a COSE QR is signed by construction
    }


# ── legacy DLT/SIGN text QR ──────────────────────────────────────────────────
def _parse_legacy(text: str) -> dict:
    sign_i = text.rfind(_SIGN_MARKER)
    signature = text[sign_i + len(_SIGN_MARKER):].strip() if sign_i >= 0 else ""
    pre = text[:sign_i] if sign_i >= 0 else text
    dlt_i = pre.find(_DLT_MARKER)
    dlt = pre[dlt_i + len(_DLT_MARKER):] if dlt_i >= 0 else pre
    tokens = dlt.split(":")
    name = tokens[0] if tokens else ""
    fields, i = {}, 1
    while i < len(tokens):
        t = tokens[i]
        if t.isupper() and 1 <= len(t) <= 5 and i + 1 < len(tokens):
            fields[t] = tokens[i + 1]; i += 2
        else:
            i += 1
    g = (fields.get("G") or "").upper()
    return {
        "fan": str(fields.get("A") or "").strip(),
        "full_name": name.strip(),
        "birth_date": _compact_date(fields.get("D") or ""),
        "gender": "Male" if g.startswith("M") else "Female" if g.startswith("F") else "",
        "signed": bool(signature) and not _SAMPLE_SIG.search(signature),
    }


# ── QR regeneration (put the user's REAL QR back on the card) ────────────────
def _regen_qr(data) -> bytes:
    """Rebuild the QR PNG from the ORIGINAL payload so its Fayda signature survives.
    `data` is the COSE bytes (byte mode) or the legacy text string."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2, box_size=4)
    if isinstance(data, (bytes, bytearray)):
        qr.add_data(bytes(data))                   # byte mode → binary-safe
    else:
        qr.add_data(str(data))
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def available() -> bool:
    return True                                     # pure Python — always available


# ── entry point (same shape the Node bridge returned) ────────────────────────
def scan(image_bytes: bytes) -> dict:
    """Read a Fayda QR from a screenshot. Returns
    {ok, fan, fan_valid, full_name, birth_date, gender, signed, qr(bytes)} or
    {ok: False, error}. Never raises."""
    if not image_bytes:
        return {"ok": False, "error": "empty image"}
    try:
        raw, text = _read_payload(image_bytes)
    except Exception as e:
        return {"ok": False, "error": f"decode error: {type(e).__name__}"}
    if not raw and not text:
        return {"ok": False, "error": "No QR code could be decoded from the image."}

    # legacy text QR?
    if text and _DLT_MARKER in text and _SIGN_MARKER in text:
        info = _parse_legacy(text)
        info["qr"] = _regen_qr(text)
    else:
        cose = _recover_cose(raw, text)
        if not cose:
            return {"ok": False, "error": "QR is not a recognised Fayda QR."}
        try:
            info = _parse_cose(cose)
        except Exception as e:
            return {"ok": False, "error": f"could not read the QR data ({type(e).__name__})"}
        info["qr"] = _regen_qr(cose)

    fan = info.get("fan") or ""
    return {
        "ok": True,
        "fan": fan,
        "fan_valid": bool(re.fullmatch(r"\d{16}", fan)),
        "full_name": info.get("full_name") or "",
        "birth_date": info.get("birth_date") or "",
        "gender": info.get("gender") or "",
        "signed": bool(info.get("signed")),
        "qr": info.get("qr"),
    }
