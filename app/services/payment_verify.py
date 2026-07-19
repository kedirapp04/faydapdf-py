"""Auto Telebirr/CBE receipt verification across three providers, ported from
faydapdf-railway (verifypayment / Leul / phone-relay).

Each provider returns a normalized dict. The orchestrator tries the configured
providers in order and returns the first success, so a valid receipt auto-approves
without waiting for a human.

The merchant receiver (name + account) is **admin-set per bank** in the settings
table (keys pay_telebirr_name/account, pay_cbe_name/account), NOT env — see
`receiver_for()`. It's passed to providers that support it and enforced by
`receiver_ok()` so a receipt paid to someone else can't be auto-approved. Env
PAYMENT_RECEIVER_* stays only as a one-time fallback for Telebirr.
"""
import io
import re
import asyncio

import aiohttp

from .. import config
from ..repo import settings as settings_repo

BANK_LABELS = {"telebirr": "Telebirr", "cbe": "CBE"}


def _amount_to_cents(v) -> int:
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return round(float(m.group(0)) * 100) if m else 0


def detect_bank(receipt: str) -> str:
    """Best-effort bank from the receipt shape: CBE refs are ~12 chars starting
    'FT'; Telebirr refs are ~10-char alphanumeric. Only affects which receiver we
    check against and which provider hint we send."""
    r = (receipt or "").strip().upper()
    if r.startswith("FT"):
        return "cbe"
    return "telebirr"


async def receiver_for(bank: str) -> tuple[str, str]:
    """Admin-configured (name, account) for a bank, from the settings table.
    Telebirr falls back to the legacy env vars if settings are empty."""
    name = (await settings_repo.get(f"pay_{bank}_name")) or ""
    acct = (await settings_repo.get(f"pay_{bank}_account")) or ""
    if bank == "telebirr" and not name and not acct:
        name, acct = config.PAYMENT_RECEIVER_NAME, config.PAYMENT_RECEIVER_ACCOUNT
    return name.strip(), acct.strip()


async def all_receivers() -> dict:
    """{bank: (name, account)} for every bank that has anything configured."""
    out = {}
    for b in ("telebirr", "cbe"):
        n, a = await receiver_for(b)
        if n or a:
            out[b] = (n, a)
    return out


async def instructions() -> str:
    """User-facing 'pay to' text, showing every configured bank account."""
    recs = await all_receivers()
    if not recs:
        return ""
    lines = ["💳 Pay to one of these accounts:"]
    for b, (n, a) in recs.items():
        detail = " — ".join(p for p in (n, a) if p)
        lines.append(f"• {BANK_LABELS.get(b, b)}: {detail}")
    return "\n".join(lines)


def _norm(ok, provider, **kw) -> dict:
    d = {"ok": ok, "provider": provider}
    d.update(kw)
    return d


async def _post(url: str, headers: dict, body: dict, timeout: int) -> tuple[int, dict]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.post(url, headers=headers, json=body) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {}
            return r.status, (data or {})


# ── providers ────────────────────────────────────────────────────────────────
async def _verifypayment(bank: str, receipt: str, recv_name: str = "", recv_acct: str = "") -> dict | None:
    if not config.VERIFYPAYMENT_API_KEY:
        return None
    body = {"bank": bank, "url": receipt}
    if recv_name:
        body["receiver_name"] = recv_name
    if recv_acct:
        body["receiver_account"] = recv_acct
    try:
        status, d = await _post(f"{config.VERIFYPAYMENT_BASE_URL}/api/check",
                                {"X-API-Key": config.VERIFYPAYMENT_API_KEY}, body, 15)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return _norm(False, "verifypayment", transient=True, error="unreachable")
    data = d.get("data") or {}
    if d.get("status") == "success":
        return _norm(True, "verifypayment",
                     receipt_id=str(data.get("transaction_id") or data.get("reference_no") or receipt).upper(),
                     amount_cents=_amount_to_cents(data.get("amount")),
                     receiver_name=str(data.get("receiver_name") or ""),
                     receiver_account=str(data.get("receiver_account") or ""),
                     status=str(data.get("status") or ""),
                     already_used=bool(d.get("already_used")))
    return _norm(False, "verifypayment", transient=(status in (0, 429) or status >= 500),
                 error=str(d.get("error") or d.get("detail") or "not verified"), status=status)


async def _leul(receipt: str) -> dict | None:
    if not config.LEUL_VERIFY_API_KEY:
        return None
    try:
        status, d = await _post(f"{config.LEUL_VERIFY_BASE_URL}/verify-telebirr",
                                {"x-api-key": config.LEUL_VERIFY_API_KEY}, {"reference": receipt}, 15)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return _norm(False, "leul", transient=True, error="unreachable")
    if d.get("success") and d.get("data"):
        x = d["data"]
        return _norm(True, "leul",
                     receipt_id=str(x.get("receiptNo") or receipt).upper(),
                     amount_cents=_amount_to_cents(x.get("settledAmount")),
                     receiver_name=str(x.get("creditedPartyName") or ""),
                     receiver_account=str(x.get("creditedPartyAccountNo") or ""),
                     status=str(x.get("transactionStatus") or ""))
    return _norm(False, "leul", transient=(status in (0, 429) or status >= 500),
                 error=str(d.get("error") or "not found"), status=status)


async def _relay(receipt: str, recv_name: str = "", recv_acct: str = "") -> dict | None:
    if not config.RELAY_VERIFY_BASE_URL or not config.RELAY_VERIFY_API_KEY:
        return None
    body = {"receipt": receipt}
    if recv_name:
        body["receiver_name"] = recv_name
    if recv_acct:
        body["receiver_account"] = recv_acct
    try:
        status, d = await _post(f"{config.RELAY_VERIFY_BASE_URL}/api/verify",
                                {"X-API-Key": config.RELAY_VERIFY_API_KEY}, body, 15)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return _norm(False, "relay", transient=True, error="unreachable")
    if d.get("ok") and d.get("data"):
        x = d["data"]
        v = d.get("verification") or {}
        return _norm(True, "relay",
                     receipt_id=str(x.get("transaction_id") or receipt).upper(),
                     amount_cents=_amount_to_cents(x.get("amount")),
                     receiver_name=str(x.get("receiver_name") or ""),
                     receiver_account=str(x.get("receiver_account") or ""),
                     status=str(x.get("status") or ""),
                     verified=(v.get("verified") is True))
    return _norm(False, "relay", transient=(status in (0, 429) or status >= 500),
                 error=str(d.get("error") or "not found"), status=status)


def any_configured() -> bool:
    return bool(config.VERIFYPAYMENT_API_KEY or config.LEUL_VERIFY_API_KEY or config.RELAY_VERIFY_API_KEY)


def _norm_name(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def receiver_ok(res: dict, want_name: str, want_acct: str) -> bool:
    """True if the receipt was paid to the expected merchant (name/account for the
    detected bank). Fails CLOSED: if a receiver is configured but the provider
    returned none (e.g. Leul, which we can't ask to filter), we refuse to
    auto-approve. If nothing is configured for that bank, there's nothing to check
    against so we allow it."""
    want_name = _norm_name(want_name)
    want_acct = re.sub(r"\D", "", want_acct or "")
    if not want_name and not want_acct:
        return True
    got_name = _norm_name(res.get("receiver_name"))
    got_acct = re.sub(r"\D", "", str(res.get("receiver_account") or ""))
    if want_name:
        if not got_name or (want_name not in got_name and got_name not in want_name):
            return False
    if want_acct:
        # accounts are often masked (251****1234) — match on the trailing digits.
        if not got_acct or want_acct[-4:] not in got_acct:
            return False
    return True


async def verify(receipt_id: str) -> dict:
    """Try each configured provider IN ORDER; return the first *acceptable* success
    and stop (so only one provider is hit for a good receipt). A success is accepted
    only when the money went to the admin-set merchant for the detected bank and the
    receipt wasn't already used — otherwise it falls through to manual admin review.
    Returns {ok, provider, receipt_id, amount_cents, receiver_*, status, bank} or
    {ok:False, error, ...}."""
    bank = detect_bank(receipt_id)
    want_name, want_acct = await receiver_for(bank)
    last = _norm(False, "none", error="No verifier configured.", transient=False)
    order = [
        lambda: _verifypayment(bank, receipt_id, want_name, want_acct),
        lambda: _leul(receipt_id),
        lambda: _relay(receipt_id, want_name, want_acct),
    ]
    for make in order:
        res = await make()
        if res is None:
            continue
        if res.get("ok"):
            if res.get("already_used"):
                last = _norm(False, res["provider"], error="receipt already used", already_used=True)
                continue
            if not receiver_ok(res, want_name, want_acct):
                last = _norm(False, res["provider"], error="paid to a different account", receiver_mismatch=True)
                continue
            res["bank"] = bank
            return res
        last = res
    return last


# ── Telebirr screenshot OCR + look-alike correction (ported from fbot/bot.py) ──
def ocr_telebirr(image_bytes: bytes) -> tuple[str, float, bool]:
    """OCR a Telebirr success screenshot → (txn_number, amount, is_receipt).
    Best-effort: needs pytesseract + the tesseract binary on the host; returns
    ('', 0.0, False) if unavailable. OCR confuses 0/O and 5/S etc. — telebirr_
    candidates() fixes that afterwards."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except Exception:
        return "", 0.0, False
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        if img.width < 1600:                       # upscale for sharper small text
            s = 1600.0 / img.width
            img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        img = ImageOps.autocontrast(img)           # crisper black/white → better OCR
        txt = pytesseract.image_to_string(img)
    except Exception:
        return "", 0.0, False
    low = txt.lower()
    is_receipt = any(k in low for k in ("transaction", "telebirr", "successful", "receipt", "birr"))
    txn = ""
    m = re.search(r"Transaction\s*Number[:\s]*([A-Z0-9 ]{8,18})", txt, re.I)
    if m:
        cand = re.sub(r"[^A-Z0-9]", "", m.group(1).upper())
        if 8 <= len(cand) <= 14:
            txn = cand
    if not txn:
        for tok in re.findall(r"\b([A-Z0-9]{10})\b", txt.upper()):
            if re.search(r"[A-Z]", tok) and re.search(r"\d", tok):
                txn = tok
                break
    amount = 0.0
    ma = re.search(r"-?\s*(\d{1,7}\.\d{2})", txt)
    if ma:
        try:
            amount = float(ma.group(1))
        except Exception:
            amount = 0.0
    return txn, amount, is_receipt


# digit ↔ look-alike letter pairs OCR gets wrong, both directions.
_OCR_FLIP = {"O": "0", "0": "O", "S": "5", "5": "S", "I": "1", "1": "I",
             "L": "1", "B": "8", "8": "B", "Z": "2", "2": "Z", "A": "4", "4": "A"}


def telebirr_candidates(txn: str, cap: int = 18) -> list[str]:
    """Ordered OCR-correction candidates: the exact value first, then flip look-alike
    chars (0↔O, 5↔S, 1↔I, 8↔B, 2↔Z, 4↔A) in growing combinations. Bounded to `cap`
    so the follow-up verification stays fast. e.g. 'DGSOKNFF9S' → … → 'DG50KNFF9S'."""
    import itertools
    txn = re.sub(r"[^A-Z0-9]", "", (txn or "").upper())
    if not txn:
        return []
    out = [txn]
    pos = [i for i, c in enumerate(txn) if c in _OCR_FLIP]
    for k in range(1, len(pos) + 1):
        for combo in itertools.combinations(pos, k):
            chars = list(txn)
            for i in combo:
                chars[i] = _OCR_FLIP[chars[i]]
            v = "".join(chars)
            if v not in out:
                out.append(v)
                if len(out) >= cap:
                    return out
    return out


async def verify_candidates(candidates: list[str], expected_cents: int = 0) -> dict:
    """Return verify()'s result for the first candidate that is a real, acceptable
    payment (ok + amount > 0, and amount ≈ expected_cents when the screenshot gave
    one). Tries the exact value first, then the rest concurrently with an early exit,
    so a correction costs a few parallel calls, not a slow sequence. Reuses verify(),
    so the receiver-match + already-used guards still apply to every candidate."""
    def _ok(res: dict) -> bool:
        if not (res and res.get("ok")):
            return False
        amt = int(res.get("amount_cents") or 0)
        if amt <= 0:
            return False
        if expected_cents > 0 and abs(amt - expected_cents) > 50:   # 0.5 ETB tolerance
            return False
        return True

    if not candidates:
        return {"ok": False, "error": "no candidates"}
    exact = candidates[0]
    r0 = await verify(exact)
    if _ok(r0):
        return r0
    rest = candidates[1:]
    if rest:
        sem = asyncio.Semaphore(6)                  # bounded fan-out → stays fast/light

        async def _try(c):
            async with sem:
                r = await verify(c)
                return r if _ok(r) else None

        tasks = [asyncio.create_task(_try(c)) for c in rest]
        try:
            for fut in asyncio.as_completed(tasks):
                res = await fut
                if res:
                    return res
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
    return r0                                        # nothing matched → exact (manual review)
