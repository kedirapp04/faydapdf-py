"""JS PDF renderer — shells out to the bundled faydapdf-railway pdfGenerator (pdf-lib)
via Node so the ID PDF is byte-for-byte the JS output the user asked for.

The render.bundle.js file is a self-contained esbuild bundle (no node_modules needed);
it reads the callback JSON on stdin and writes the PDF to stdout (name on stderr as
NAME:<base64>). If Node or the bundle is unavailable — or the subprocess fails — this
transparently falls back to the in-process Python renderer (pdf_render), so a download
never breaks over the renderer choice.
"""
import asyncio
import base64
import json
import os
import shutil
from pathlib import Path

from . import pdf_render

_DIR = Path(__file__).parent / "js_render"
_BUNDLE = _DIR / "render.bundle.js"
_NODE = os.getenv("NODE_BIN") or shutil.which("node") or "node"
_TIMEOUT = 30.0
_warned = False


def js_available() -> bool:
    if not _BUNDLE.exists():
        return False
    return bool(shutil.which(_NODE) or os.path.exists(_NODE))


async def _render_js(user: dict) -> tuple[bytes, str]:
    payload = json.dumps(user, ensure_ascii=False).encode("utf-8")
    proc = await asyncio.create_subprocess_exec(
        _NODE, str(_BUNDLE),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_DIR),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("js render timed out")
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"js render rc={proc.returncode}: {err.decode('utf-8', 'replace')[:200]}")
    name = "fayda"
    for line in err.decode("utf-8", "replace").splitlines():
        if line.startswith("NAME:"):
            try:
                name = base64.b64decode(line[5:]).decode("utf-8", "replace") or "fayda"
            except Exception:
                name = "fayda"
    return out, pdf_render._safe(name)


async def render(user: dict, engine: str = "js") -> tuple[bytes, str]:
    """Render the ID PDF. engine='js' → the Node/pdf-lib renderer (auto-falls back to
    the Python renderer on any error); anything else → the Python renderer."""
    global _warned
    if (engine or "js").lower() == "js" and js_available():
        try:
            return await _render_js(user)
        except Exception as e:
            if not _warned:
                print("[js_render] falling back to the Python renderer:", e)
                _warned = True
    return await asyncio.to_thread(pdf_render.render, user)
