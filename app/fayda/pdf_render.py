"""Render the Fayda callback into the OFFICIAL digital-ID PDF.

Faithful port of faydapdf-railway/pdfGenerator.js: it stamps the text fields and
images (photo, QR, front, back) at the exact coordinates onto the bundled Fayda
template PDF, using the real fonts — Nyala (Amharic) + Barlow Semi Condensed
(English). pdf-lib and reportlab share a bottom-left origin, so the coordinates
transfer 1:1. If the template or pypdf is unavailable it falls back to a simple
generated page so a PDF is always produced.
"""
import base64
import io
import os
import re
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_ASSETS = Path(__file__).parent / "assets"
_TEMPLATE = _ASSETS / "template.pdf"


def _register(name: str, filename: str, env: str = "") -> str | None:
    """Register a TTF (env override → bundled asset). No network, ever."""
    path = os.getenv(env, "") if env else ""
    if not path or not os.path.exists(path):
        p = _ASSETS / filename
        path = str(p) if p.exists() else ""
    if path:
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            return None
    return None


_AMH_FONT = _register("Amharic", "nyala.ttf", "FAYDA_AMHARIC_FONT")   # Ethiopic
_ENG_FONT = _register("Barlow", "barlow.ttf", "FAYDA_ENGLISH_FONT") or "Helvetica"

# ── layout (verbatim from faydapdf-railway/pdfGenerator.js) ──────────────────
VALUE_FONT_SIZE = 9
VALUE_COLOR = (0.137, 0.364, 0.443)

# (key, font, x, y, format)
TEXT_LAYOUT = [
    ("dateOfBirth_et", "amharic", 59.6, 553.19, None),
    ("dateOfBirth_eng", "english", 59.6, 544.49, None),
    ("gender_amh", "amharic", 59.6, 517.99, None),
    ("gender_eng", "english", 59.6, 508.59, None),
    ("citizenship_amh", "amharic", 59.6, 487.29, None),
    ("citizenship_Eng", "english", 59.6, 477.59, None),
    ("phone", "english", 59.6, 455.29, None),
    ("region_amh", "amharic", 203.2, 553.19, None),
    ("region_eng", "english", 203.2, 544.49, None),
    ("zone_amh", "amharic", 203.2, 517.99, None),
    ("zone_eng", "english", 203.2, 508.59, None),
    ("woreda_amh", "amharic", 203.2, 487.29, None),
    ("woreda_eng", "english", 203.2, 477.59, None),
    ("fcn", "english", 73.6, 605.99, "fcn"),
    ("fullName_amh", "amharic", 170.7, 615.99, None),
    ("fullName_eng", "english", 170.7, 604.49, None),
]

# (key, x, y, width, height)
IMAGE_LAYOUT = [
    ("photo", 53.8, 624.69, 85, 117.5),
    ("QRCodes", 110, 268.89, 164, 162),
    ("fronts", 397.1, 511.89, 156.6, 240),
    ("backs", 397.1, 264.89, 156.6, 240),
]


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return str(d[k])
    return ""


_DATA_KEYS = ("fullName_eng", "fullNameEng", "fullName", "photo", "fcn", "FCN", "QRCodes")


def _looks_like_data(d) -> bool:
    return isinstance(d, dict) and any(k in d for k in _DATA_KEYS)


def _find_data(obj):
    """Locate the person-data dict. Handles the real Fayda callback shapes
    (user.data / data.user.data / data.data.user.data / data), the older
    userData/response nestings, and finally a recursive fallback."""
    if not isinstance(obj, dict):
        return {}
    o = {k: v for k, v in obj.items() if k != "homepage"}
    for path in (("user", "data"), ("data", "user", "data"), ("data", "data", "user", "data"),
                 ("data",), ("userData",), ("response",)):
        cur = o
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None
                break
        if _looks_like_data(cur):
            return cur

    def _dig(x, depth=0):
        if _looks_like_data(x):
            return x
        if isinstance(x, dict) and depth < 5:
            for v in x.values():
                r = _dig(v, depth + 1)
                if r:
                    return r
        return None

    return _dig(o) or o


def _pdf_data(d: dict) -> dict:
    """Map the raw data to the template's field keys (mirrors sanitizeVerifyResponse)."""
    return {
        "fullName_eng": _pick(d, "fullName_eng", "fullNameEng", "fullName"),
        "fullName_amh": _pick(d, "fullName_amh", "fullNameAmh"),
        "dateOfBirth_eng": _pick(d, "dateOfBirth_eng", "dateOfBirthEng", "birthdate"),
        "dateOfBirth_et": _pick(d, "dateOfBirth_et", "dateOfBirthEt"),
        "gender_eng": _pick(d, "gender_eng", "genderEng"),
        "gender_amh": _pick(d, "gender_amh", "genderAmh"),
        "citizenship_Eng": _pick(d, "citizenship_Eng", "citizenship_eng", "citizenshipEng"),
        "citizenship_amh": _pick(d, "citizenship_amh", "citizenshipAmh"),
        "phone": _pick(d, "phone"),
        "region_eng": _pick(d, "region_eng", "regionEng"),
        "region_amh": _pick(d, "region_amh", "regionAmh"),
        "zone_eng": _pick(d, "zone_eng", "zoneEng"),
        "zone_amh": _pick(d, "zone_amh", "zoneAmh"),
        "woreda_eng": _pick(d, "woreda_eng", "woredaEng"),
        "woreda_amh": _pick(d, "woreda_amh", "woredaAmh"),
        "fcn": _pick(d, "vid", "VID", "fcn", "FCN"),
        "photo": _pick(d, "photo"),
        "QRCodes": _pick(d, "QRCodes", "qrCodes", "qrCode"),
        "fronts": _pick(d, "fronts", "front"),
        "backs": _pick(d, "backs", "back"),
    }


def _format_fcn(value) -> str:
    raw = re.sub(r"\s+", "", str(value or ""))
    if not raw:
        return ""
    return re.sub(r"(.{4})", r"\1 ", raw).strip()


def _image(b64):
    try:
        s = re.sub(r"^data:image/\w+;base64,", "", str(b64 or ""))
        if not s:
            return None
        return ImageReader(io.BytesIO(base64.b64decode(s)))
    except Exception:
        return None


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\r\n]+", " ", name).strip() or "fayda"


def _draw_fields(c, data: dict) -> None:
    c.setFillColorRGB(*VALUE_COLOR)
    for key, font, x, y, fmt in TEXT_LAYOUT:
        val = _format_fcn(data.get(key)) if fmt == "fcn" else str(data.get(key) or "")
        if not val.strip():
            continue
        f = _AMH_FONT if (font == "amharic" and _AMH_FONT) else _ENG_FONT
        c.setFont(f, VALUE_FONT_SIZE)
        c.drawString(x, y, val)
    for key, x, y, w, h in IMAGE_LAYOUT:
        img = _image(data.get(key))
        if img:
            try:
                c.drawImage(img, x, y, w, h, preserveAspectRatio=False, mask="auto")
            except Exception:
                pass


def _render_template(data: dict) -> bytes:
    """Stamp the fields onto the official template (page 0) via a reportlab overlay."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(_TEMPLATE))
    page = reader.pages[0]
    w, h = float(page.mediabox.width), float(page.mediabox.height)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    _draw_fields(c, data)
    c.showPage()
    c.save()
    buf.seek(0)

    overlay = PdfReader(buf).pages[0]
    page.merge_page(overlay)  # overlay drawn on TOP of the template
    writer = PdfWriter()
    writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _render_fallback(data: dict) -> bytes:
    """Simple generated page if the template/pypdf is unavailable."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    c.setFillColorRGB(0.12, 0.35, 0.62)
    c.rect(0, H - 30 * mm, W, 30 * mm, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(18 * mm, H - 20 * mm, "Fayda National ID")
    photo = _image(data.get("photo"))
    if photo:
        try:
            c.drawImage(photo, W - 48 * mm, H - 78 * mm, 30 * mm, 40 * mm, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass
    rows = [
        ("Full name", data.get("fullName_eng")), ("ስም", data.get("fullName_amh")),
        ("Date of birth", data.get("dateOfBirth_eng")), ("Gender", data.get("gender_eng")),
        ("Nationality", data.get("citizenship_Eng")), ("Phone", data.get("phone")),
        ("Region", data.get("region_eng")), ("Zone", data.get("zone_eng")),
        ("Woreda", data.get("woreda_eng")), ("FCN / FAN", _format_fcn(data.get("fcn"))),
    ]
    y = H - 42 * mm
    for label, value in rows:
        if not value:
            continue
        c.setFillColorRGB(0.4, 0.45, 0.5)
        c.setFont(_ENG_FONT, 9)
        c.drawString(18 * mm, y, label)
        c.setFillColorRGB(0.05, 0.08, 0.12)
        use_amh = _AMH_FONT and any(ord(ch) > 0x1200 for ch in value)
        c.setFont(_AMH_FONT if use_amh else _ENG_FONT, 12)
        c.drawString(60 * mm, y, value)
        y -= 9 * mm
    qr = _image(data.get("QRCodes"))
    if qr:
        try:
            c.drawImage(qr, 18 * mm, 22 * mm, 38 * mm, 38 * mm, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass
    c.showPage()
    c.save()
    return buf.getvalue()


def render(callback_json) -> tuple[bytes, str]:
    d = _find_data(callback_json)
    data = _pdf_data(d)
    name = data.get("fullName_eng") or "fayda"
    if _TEMPLATE.exists():
        try:
            return _render_template(data), _safe(name)
        except Exception:
            pass
    return _render_fallback(data), _safe(name)
