"""Fayda screenshots — front card, back card, and a photo+QR composite.

Mirrors faydapdf-railway/screenshotGenerator.js: the front/back are full card
images the Fayda backend already returns (fields `fronts`/`backs`); the photo+QR
is composited here. Returns a list of assets so the bot can send each one.
"""
import base64
import io
import re

from PIL import Image

# photo+QR composite layout (matches the Node version)
_BORDER = 6
_PAD = 28
_GAP = 28
_MAX = 900


def _extract_data(obj) -> dict:
    """Same lookup order as faydapdf-railway extractVerifyResponseData:
    user.data / data.user.data / data.data.user.data / data / obj."""
    if not isinstance(obj, dict):
        return {}
    o = {k: v for k, v in obj.items() if k != "homepage"}
    for path in (("user", "data"), ("data", "user", "data"),
                 ("data", "data", "user", "data"), ("data",)):
        cur = o
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None
                break
        if isinstance(cur, dict) and cur:
            return cur
    return o


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return ""


def _to_bytes(value) -> bytes | None:
    try:
        s = re.sub(r"^data:[^;]+;base64,", "", str(value or "")).strip()
        s = re.sub(r"\s+", "", s)
        return base64.b64decode(s) if s else None
    except Exception:
        return None


def _pil(value):
    raw = _to_bytes(value)
    if not raw:
        return None
    try:
        return Image.open(io.BytesIO(raw))
    except Exception:
        return None


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\r\n]+", " ", str(name or "")).strip() or "fayda"


def _is_png(b: bytes) -> bool:
    return b[:8] == b"\x89PNG\r\n\x1a\n"


def _is_jpeg(b: bytes) -> bool:
    return b[:3] == b"\xff\xd8\xff"


def _sendable(label: str, value, base: str) -> dict | None:
    """A card image asset — sent as-is if it's already PNG/JPEG, else re-encoded to
    PNG (Telegram only previews those two)."""
    raw = _to_bytes(value)
    if not raw:
        return None
    if _is_png(raw):
        return {"label": label, "bytes": raw, "filename": f"{label}-{base}.png"}
    if _is_jpeg(raw):
        return {"label": label, "bytes": raw, "filename": f"{label}-{base}.jpg"}
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"label": label, "bytes": buf.getvalue(), "filename": f"{label}-{base}.png"}
    except Exception:
        return None


def _photo_qr(photo, qr, base: str) -> dict | None:
    try:
        for im in (photo, qr):
            im.thumbnail((_MAX, _MAX))
        inner_w = max(photo.width, qr.width) + _PAD * 2
        inner_h = _PAD + photo.height + _GAP + qr.height + _PAD
        outer = Image.new("RGBA", (inner_w + _BORDER * 2, inner_h + _BORDER * 2), (0, 0, 0, 255))
        outer.paste(Image.new("RGBA", (inner_w, inner_h), (255, 255, 255, 255)), (_BORDER, _BORDER))
        px = _BORDER + (inner_w - photo.width) // 2
        py = _BORDER + _PAD
        qx = _BORDER + (inner_w - qr.width) // 2
        qy = py + photo.height + _GAP
        outer.paste(photo, (px, py), photo if photo.mode == "RGBA" else None)
        outer.paste(qr, (qx, qy), qr if qr.mode == "RGBA" else None)
        buf = io.BytesIO()
        outer.save(buf, format="PNG")
        return {"label": "photo-qr", "bytes": buf.getvalue(), "filename": f"photo-qr-{base}.png"}
    except Exception:
        return None


def render(callback_json) -> list[dict]:
    """Return [{label, bytes, filename}, …] for whichever screenshots are available
    (front, back, photo-qr). Empty list if none — never raises to the caller."""
    try:
        d = _extract_data(callback_json)
        base = _safe(_pick(d, "fullName_eng", "fullNameEng", "fullName") or _pick(d, "fcn", "FCN") or "fayda")
        out = []
        for label, keys in (("front", ("fronts", "front")), ("back", ("backs", "back"))):
            asset = _sendable(label, _pick(d, *keys), base)
            if asset:
                out.append(asset)
        photo = _pil(_pick(d, "photo"))
        qr = _pil(_pick(d, "QRCodes", "qrCodes", "qrCode", "qr"))
        if photo and qr:
            asset = _photo_qr(photo, qr, base)
            if asset:
                out.append(asset)
        return out
    except Exception:
        return []
