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
from pathlib import Path

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


async def scan(image: bytes) -> dict:
    """Read the QR out of the user's Telebirr Fayda (National ID) screenshot.

    Returns {ok, fan, fan_valid, full_name, birth_date, gender, signed, qr}. `qr`
    is the QR REGENERATED FROM THE SCANNED TEXT — the payload is byte-identical, so
    it keeps the real signature and still verifies. That is the reason to scan
    rather than build: a generated QR carries a sample signature no verifier
    accepts.

    ok=False with a reason on an unreadable image — an ordinary outcome (blurry
    photo, wrong screenshot), not an error to raise at the user.
    """
    if not image:
        return {"ok": False, "error": "empty image"}
    empty = {"ok": False, "error": why_unavailable() or "scanner unavailable"}
    d = await _run({"op": "scan", "image": base64.b64encode(image).decode()}, empty)
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error") or "could not read the QR"}
    return {"ok": True, "fan": d.get("fan") or "", "fan_valid": bool(d.get("fanValid")),
            "full_name": d.get("fullName") or "", "birth_date": d.get("birthDate") or "",
            "gender": d.get("gender") or "", "signed": bool(d.get("signed")),
            "qr": _bytes(d, "qr")}


async def build(card_data: dict, qr_png: bytes | None = None) -> dict:
    """Draw both cards.

    Pass `qr_png` (the scanned QR) and it is used as-is, so the card carries the
    real, verifiable QR. Without it a QR is built from the identity data — that
    one renders and scans but will NOT pass signature verification.

    Never raises: a card failure must not cost the user their PDF, so every
    problem comes back as None and is logged once.
    """
    empty = {"qr_text": None, "qr": None, "front": None, "back": None, "qr_from_scan": False}
    payload = dict(card_data)
    if qr_png:
        payload["qrPngB64"] = base64.b64encode(qr_png).decode()
    d = await _run(payload, empty)
    return {"qr_text": d.get("qrText"), "qr": _bytes(d, "qr"),
            "front": _bytes(d, "front"), "back": _bytes(d, "back"),
            "qr_from_scan": bool(d.get("qrFromScan"))}
