"""Card + QR generation for Server 5, via the bundled Node generators.

The resident identity API returns a photo and identity fields but NOT the card
images or QR the PDF/screenshot layer expects — unlike the Server-4 callback,
which hands them over ready-made. Rather than re-implement the card layout, this
shells out to the SAME generators the Node bot uses (cards_node/), so the output
is pixel-identical: same templates, same fonts, same Ethiopian-calendar logic.

Unlike js_render there is no in-process fallback: nothing in Python draws these
cards. If Node or node_modules is missing, `available()` is False and Server 5
delivers the PDF without card screenshots rather than failing the download.
"""
import asyncio
import base64
import json
import os
import shutil
import sys
from pathlib import Path

# On Windows, spawning the console app node.exe pops a terminal window every time.
# CREATE_NO_WINDOW keeps it hidden. No-op / absent off Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

_DIR = Path(__file__).parent / "cards_node"
_ENTRY = _DIR / "cards.js"
_MODULES = _DIR / "node_modules"
_NODE = os.getenv("NODE_BIN") or shutil.which("node") or "node"
# Two ~600 KB JPEGs get drawn at 1968x3150; 60 s is generous but this runs once
# per download and a timeout costs the user their screenshots.
_TIMEOUT = float(os.getenv("CARDS_TIMEOUT_S") or 60)
_warned = False


def available() -> bool:
    """Everything needed to draw cards is present."""
    return (_ENTRY.exists() and _MODULES.exists()
            and bool(shutil.which(_NODE) or os.path.exists(_NODE)))


def why_unavailable() -> str:
    """Human-readable reason, for the admin UI — 'it silently does nothing' is a
    bad way to find out Node is missing on the host."""
    if not _ENTRY.exists():
        return f"cards.js missing at {_ENTRY}"
    if not _MODULES.exists():
        return f"node_modules missing — run: npm install --omit=dev  (in {_DIR})"
    if not (shutil.which(_NODE) or os.path.exists(_NODE)):
        return f"node not found (tried {_NODE!r}; set NODE_BIN)"
    return ""


async def _run(payload_obj: dict, empty: dict) -> dict:
    """One request/response round trip to the Node bridge."""
    global _warned
    if not available():
        if not _warned:
            print("[cards] disabled:", why_unavailable())
            _warned = True
        return empty
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            _NODE, str(_ENTRY),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_DIR),
            **_NO_WINDOW,
        )
    except OSError as e:
        print("[cards] cannot start node:", e)
        return empty
    try:
        out, errb = await asyncio.wait_for(proc.communicate(payload), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        print(f"[cards] timed out after {_TIMEOUT}s")
        return empty
    err_txt = errb.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not out:
        print(f"[cards] rc={proc.returncode}: {err_txt[:300]}")
        return empty
    if err_txt:                      # partial failure — cards.js reports per-piece
        print("[cards]", err_txt[:300])
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        print("[cards] non-JSON reply:", out[:120])
        return empty


def _bytes(d: dict, key):
    return base64.b64decode(d[key]) if d.get(key) else None


def shrink_jpeg(jpeg_bytes: bytes, max_width: int = 1000, quality: int = 80) -> bytes:
    """Downscale a card JPEG for EMBEDDING IN THE PDF, where it only appears as a
    small thumbnail on the margin — a 1968-wide card there wastes ~0.6 MB each. The
    full-resolution card is still sent as a separate screenshot, so nothing the user
    zooms into is degraded. 1000px/q80 keeps the PDF ~1.9 MB (800/72 gave ~1.6 MB).
    Returns the original on any error."""
    if not jpeg_bytes:
        return jpeg_bytes
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(jpeg_bytes))
        if im.width > max_width:
            h = round(im.height * max_width / im.width)
            im = im.resize((max_width, h), Image.LANCZOS)
        out = io.BytesIO()
        im.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        return jpeg_bytes


async def scan(image: bytes) -> dict:
    """Read the QR out of the user's Telebirr Fayda (National ID) screenshot.

    Runs entirely in Python now (zxing-cpp) — NO Node subprocess. It reads the dense
    new-format QR the Node scanner could not, is ~30x faster, and needs no
    node_modules, so QR input works even where the card-drawing bridge is absent.
    The returned `qr` is regenerated from the ORIGINAL payload, so its Fayda
    signature survives and the card's QR still verifies.

    ok=False with a reason on an unreadable image — an ordinary outcome (blurry
    photo, wrong screenshot), not an error to raise at the user.
    """
    if not image:
        return {"ok": False, "error": "empty image"}
    try:
        from . import qr_scan
    except Exception as e:                 # a missing wheel fails only scanning
        print("[qr_scan] unavailable:", e)
        return {"ok": False, "error": "QR scanner is not available on this host."}
    # zxing-cpp/opencv are CPU-bound C extensions — run off the event loop.
    return await asyncio.to_thread(qr_scan.scan, image)


async def build(card_data: dict, qr_png: bytes | None = None, qr_gen: str = "data") -> dict:
    """Draw both cards.

    Pass `qr_png` (the scanned QR) and it is used as-is, so the card carries the
    real, verifiable QR. Without it a QR is built per `qr_gen`:
      * "data"  — a legacy QR from the identity data with a sample signature (scans,
                  shows data, does NOT verify).
      * "nosig" — the same data QR but with an empty signature (no signature).
      * "blank" — a QR that decodes to nothing and carries no signature.

    Never raises: a card failure must not cost the user their PDF, so every
    problem comes back as None and is logged once.
    """
    empty = {"qr_text": None, "qr": None, "front": None, "back": None, "qr_from_scan": False}
    payload = dict(card_data)
    payload["qrGen"] = qr_gen if qr_gen in ("data", "nosig", "blank", "unscannable") else "data"
    if qr_png:
        payload["qrPngB64"] = base64.b64encode(qr_png).decode()
    d = await _run(payload, empty)
    return {"qr_text": d.get("qrText"), "qr": _bytes(d, "qr"),
            "front": _bytes(d, "front"), "back": _bytes(d, "back"),
            "qr_from_scan": bool(d.get("qrFromScan"))}
