"""Render the Fayda callback user-data into a PDF (reportlab).

Functional layout: header, photo, QR, and the decoded text fields. It reads the
callback JSON flexibly (userData / data / nested). Amharic text renders only if an
Ethiopic TTF is registered (see FAYDA_AMHARIC_FONT); otherwise English fields show.
NOTE: this is a clean, working document — not a pixel clone of the old card. The
exact card layout can be refined against a real payload.
"""
import base64
import io
import os
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_AMH_FONT = None
_font_path = os.getenv("FAYDA_AMHARIC_FONT", "")
if _font_path and os.path.exists(_font_path):
    try:
        pdfmetrics.registerFont(TTFont("Amharic", _font_path))
        _AMH_FONT = "Amharic"
    except Exception:
        _AMH_FONT = None


def _find_data(obj):
    if not isinstance(obj, dict):
        return {}
    for k in ("userData", "data", "response"):
        v = obj.get(k)
        if isinstance(v, dict):
            inner = _find_data(v)
            if inner:
                return inner
    return obj


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return str(d[k])
    return ""


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


def render(callback_json) -> tuple[bytes, str]:
    d = _find_data(callback_json)
    name = _pick(d, "fullName_eng", "fullNameEng", "fullName") or "fayda"

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    c.setFillColorRGB(0.12, 0.35, 0.62)
    c.rect(0, H - 30 * mm, W, 30 * mm, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(18 * mm, H - 20 * mm, "Fayda National ID")

    # photo top-right
    photo = _image(_pick(d, "photo"))
    if photo:
        try:
            c.drawImage(photo, W - 48 * mm, H - 78 * mm, 30 * mm, 40 * mm, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    fields = [
        ("Full name", _pick(d, "fullName_eng", "fullNameEng", "fullName")),
        ("ስም (Amharic)", _pick(d, "fullName_amh", "fullNameAmh")),
        ("Date of birth", _pick(d, "dateOfBirth_eng", "dateOfBirth", "dob")),
        ("Gender", _pick(d, "gender_eng", "gender")),
        ("Nationality", _pick(d, "citizenship_Eng", "nationality", "citizenship")),
        ("Phone", _pick(d, "phone", "phoneNumber")),
        ("Email", _pick(d, "email")),
        ("Region", _pick(d, "region_eng", "region")),
        ("Zone", _pick(d, "zone_eng", "zone")),
        ("Woreda", _pick(d, "woreda_eng", "woreda")),
        ("FCN / FAN", _pick(d, "fcn", "FCN", "fan")),
        ("FIN / UIN", _pick(d, "UIN", "uin", "fin")),
    ]

    y = H - 42 * mm
    for label, value in fields:
        if not value:
            continue
        c.setFillColorRGB(0.4, 0.45, 0.5)
        c.setFont("Helvetica", 9)
        c.drawString(18 * mm, y, label)
        c.setFillColorRGB(0.05, 0.08, 0.12)
        use_amh = _AMH_FONT and any(ord(ch) > 0x1200 for ch in value)
        c.setFont(_AMH_FONT if use_amh else "Helvetica-Bold", 12)
        c.drawString(60 * mm, y, value)
        y -= 9 * mm

    qr = _image(_pick(d, "QRCodes", "qrCodes", "qrCode", "qr"))
    if qr:
        try:
            c.drawImage(qr, 18 * mm, 22 * mm, 38 * mm, 38 * mm, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    c.setFillColorRGB(0.5, 0.55, 0.6)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(18 * mm, 12 * mm, "Generated via Fayda (Server-4).")
    c.showPage()
    c.save()
    return buf.getvalue(), _safe(name)
