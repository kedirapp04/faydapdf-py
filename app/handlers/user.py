"""User-facing flows: download (FAN → OTP → PDF), wallet, add-balance, forgot-FAN.

Conversation state is aiogram FSM (in-memory); all persistent data is in Postgres.
"""
import asyncio
import hashlib
import logging
import re
import time

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from .. import config, fayda, i18n
from ..db import pool, db_ready, db_down_policy, mark_db_down
from ..repo import users as users_repo, settings as settings_repo, payments as payments_repo
from ..services import billing, payment_verify, maintenance
from . import keyboards as kb

router = Router()
log = logging.getLogger("faydapdf-py.user")

FAN_RE = re.compile(r"^\d{12,16}$")
OTP_RE = re.compile(r"^\d{4,10}$")
PHONE_RE = re.compile(r"^(?:\+?251|0)?9\d{8}$")

# One batch of ids is processed one-at-a-time; cap it so a single message can't
# fire off an unbounded run of pool-token pulls in Server-4 mode.
MAX_MULTI_FAN = 5

# Debounce recent actions (mirrors faydapdf-railway shouldSkipRecentAction): each
# Server-4 download pulls a single-use pool token, so we throttle rapid repeats to
# protect the pool. In-memory, per process (a user is on one bot/process).
_recent: dict[str, float] = {}


def _should_skip(key: str, ttl: float) -> bool:
    now = time.monotonic()
    for k in [k for k, t in _recent.items() if now - t > max(ttl, 10.0) * 4]:
        _recent.pop(k, None)
    prev = _recent.get(key)
    if prev is not None and now - prev < ttl:
        return True
    _recent[key] = now
    return False


class Flow(StatesGroup):
    await_fan = State()    # tapped Get PDF/Screenshot → awaiting the FIN/FAN
    choose_fmt = State()   # entered a FIN/FAN → awaiting the output choice
    otp = State()
    forgot_name = State()
    forgot_phone = State()
    receipt = State()


def _fan_hash(fan: str) -> str:
    return hashlib.sha256(fan.encode()).hexdigest()[:16]


def _mask_phone(masked) -> str:
    """Max-masked Ethiopian phone → +251*****#### (only the last 4 digits shown)."""
    digits = re.sub(r"\D", "", str(masked or ""))
    last4 = digits[-4:] if len(digits) >= 4 else digits
    return f"+251*****{last4}" if last4 else ""


async def _seen(chat_id, bot_id, first_name=None) -> None:
    """Record (user, bot) for broadcast, remember the bot the user last used so
    cross-bot notifications reach them, capture first_name for broadcast
    personalization, and clear any stale is_blocked flag (they're clearly reachable).
    Non-critical — never let a DB blip here break a flow."""
    try:
        await pool().execute(
            "INSERT INTO chats (telegram_id, bot_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            int(chat_id), int(bot_id),
        )
        await pool().execute(
            "UPDATE users SET last_bot_id=$1, "
            "first_name=COALESCE($3, first_name), "
            "is_blocked=false, "
            "unblocked_at=CASE WHEN is_blocked THEN now() ELSE unblocked_at END "
            "WHERE telegram_id=$2",
            int(bot_id), int(chat_id), (first_name or None))
    except Exception:
        mark_db_down()


# A stand-in user for the DB-down "free" path (no DB read possible).
_DBDOWN_USER = {
    "telegram_id": 0, "username": None, "status": "active", "billing_mode": "counter",
    "balance_cents": 0, "owed_cents": 0, "credit_limit_cents": 0, "price_override_cents": None,
    "is_vip": False, "daily_limit": 0, "total_limit": 0, "delivery_pref": "both",
}


def _parse_fans(text: str) -> tuple[list[str], int]:
    fans = list(dict.fromkeys(re.findall(r"\b\d{12,16}\b", text or "")))
    dropped = max(0, len(fans) - MAX_MULTI_FAN)
    return fans[:MAX_MULTI_FAN], dropped


async def _paused() -> bool:
    return await settings_repo.get_bool("paused", False)


# ── maintenance gate (admins bypass) ─────────────────────────────────────────
async def _maint_block_download(user_id) -> str | None:
    """A DOWNLOAD attempt: blocked at BOTH low and high. Returns the notice or None."""
    if config.is_admin(user_id):
        return None
    if (await maintenance.level()) in ("low", "high"):
        return await maintenance.message()
    return None


async def _maint_block_action(user_id) -> str | None:
    """A general DB action (wallet, pay, forgot…): blocked at HIGH only."""
    if config.is_admin(user_id):
        return None
    if (await maintenance.level()) == "high":
        return await maintenance.message()
    return None


# ── commands ────────────────────────────────────────────────────────────────
async def _start_bg(uid, username, chat_id, bot_id, first_name):
    """Record the user + chat AFTER the welcome is sent, so /start feels instant."""
    try:
        await users_repo.ensure(uid, username)
    except Exception:
        mark_db_down()
    await _seen(chat_id, bot_id, first_name)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    # Price-per-download shown on start; reads the (cached) live price, so it always
    # reflects the admin's current setting / free mode.
    try:
        price = 0 if await billing.free_mode() else await billing.global_price_cents()
        price_line = i18n.t("price_free") if price <= 0 else i18n.t("price_per_pdf", price=billing.birr(price))
    except Exception:
        price_line = ""
    welcome = i18n.t("welcome") + (("\n\n" + price_line) if price_line else "")
    # Answer immediately; the DB writes (create user, welcome bonus, record chat) run
    # in the background so the user isn't waiting on remote-DB round-trips.
    await m.answer(welcome, reply_markup=kb.main_kb(m.from_user.id))
    asyncio.create_task(_start_bg(m.from_user.id, m.from_user.username,
                                  m.chat.id, m.bot.id, m.from_user.first_name))


@router.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(i18n.t("cancelled"), reply_markup=kb.main_kb(m.from_user.id))


@router.callback_query(F.data == "cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer("Cancelled")
    # Replace the prompt in place (drops its inline buttons) instead of sending a new
    # message. The bottom reply keyboard persists on its own.
    try:
        await c.message.edit_text(i18n.t("cancelled"))
    except Exception:
        try:
            await c.message.answer(i18n.t("cancelled"), reply_markup=kb.main_kb(c.from_user.id))
        except Exception:
            pass


# ── reply-keyboard buttons (match in any state; reset the flow) ──────────────
@router.message(F.text.in_(kb.BUTTONS))
async def buttons(m: Message, state: FSMContext):
    await state.clear()
    text = kb.canonical(m.text)   # route old/aliased labels to their current action
    # These need no DB — they just set FSM state / show static text.
    if text == kb.BTN_HELP:
        return await m.answer(i18n.t("help"), reply_markup=kb.main_kb(m.from_user.id))
    # Maintenance gate. HIGH blocks every button but Help; the two download buttons
    # are also blocked at LOW. Admins bypass (checked inside the helpers).
    if text in (kb.BTN_GET_PDF, kb.BTN_GET_SHOT):
        blk = await _maint_block_download(m.from_user.id)
    else:
        blk = await _maint_block_action(m.from_user.id)
    if blk:
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    if text == kb.BTN_GET_PDF:   # pre-pick PDF, then await the FIN/FAN
        await state.set_state(Flow.await_fan)
        await state.update_data(dl_fmt="pdf")
        return await m.answer(i18n.t("get_pdf_prompt"), reply_markup=kb.cancel_kb())
    if text == kb.BTN_GET_SHOT:
        await state.set_state(Flow.await_fan)
        await state.update_data(dl_fmt="screenshot")
        return await m.answer(i18n.t("get_shot_prompt"), reply_markup=kb.cancel_kb())
    if text == kb.BTN_FORGOT:
        await state.set_state(Flow.forgot_name)
        return await m.answer(i18n.t("forgot_name"), reply_markup=kb.cancel_kb())
    # The rest need the DB.
    try:
        u = await users_repo.ensure(m.from_user.id, m.from_user.username)
    except Exception:
        mark_db_down()
        u = None
    await _seen(m.chat.id, m.bot.id, m.from_user.first_name)
    if u is None or not db_ready():
        return await m.answer(i18n.t("unavailable"), reply_markup=kb.main_kb(m.from_user.id))
    if u["status"] == "blocked" and not config.is_admin(m.from_user.id):
        return await m.answer(i18n.t("blocked"))
    if text == kb.BTN_WALLET:
        return await _show_wallet(m)
    if text == kb.BTN_PAYMENTS:
        return await _show_payments(m)
    if text == kb.BTN_PAY:
        await state.set_state(Flow.receipt)
        recv = await payment_verify.receiver_block()
        msg = i18n.t("addpay_full", recv=recv or "—")
        return await m.answer(msg, reply_markup=kb.cancel_kb())
    if text == kb.BTN_ADMIN:
        if config.is_admin(m.from_user.id):
            from . import admin
            return await admin.show_panel(m)
        return


async def _show_wallet(m: Message):
    u = await users_repo.get(m.from_user.id)
    if not u:
        u = await users_repo.ensure(m.from_user.id, m.from_user.username)
    mode = u["billing_mode"]
    lines = [i18n.t("wallet_header", mode=mode)]
    if mode in ("prepaid", "postpaid"):
        # single net figure: normal balance minus any owed debt
        net = u["balance_cents"] - u["owed_cents"]
        lines.append(i18n.t("wallet_balance", balance=billing.birr(net)))
    if u.get("bonus_balance_cents", 0) > 0 and mode in ("prepaid", "postpaid"):
        lines.append(i18n.t("wallet_bonus", bonus=billing.birr(u["bonus_balance_cents"])))
    price = await billing.price_for(u)
    lines.append(i18n.t("wallet_price", price=billing.birr(price)))
    await m.answer("\n".join(lines), reply_markup=kb.main_kb(m.from_user.id))


async def _show_payments(m: Message):
    """Recent top-up / payment history (the 'My Payments' button)."""
    rows = await pool().fetch(
        "SELECT receipt_id, bank, amount_cents, status, created_at FROM payments "
        "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10", m.from_user.id)
    if not rows:
        return await m.answer(i18n.t("no_payments"), reply_markup=kb.main_kb(m.from_user.id))
    icon = {"approved": "✅", "rejected": "🚫", "pending": "⏳"}
    lines = [i18n.t("payments_header")]
    for r in rows:
        amt = billing.birr(r["amount_cents"]) if r["amount_cents"] else "—"
        d = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else ""
        lines.append(f"{icon.get(r['status'], '•')} {r['receipt_id']} · {amt} · {d}")
    await m.answer("\n".join(lines), reply_markup=kb.main_kb(m.from_user.id))


# ── download: start (one FAN of the queue) + OTP step ───────────────────────
async def _begin_download(m: Message, state: FSMContext, u: dict, fan: str, queue: list[str],
                          delivery: str = "both", db_free: bool = False, uid=None):
    uid = uid or m.from_user.id   # callback path (format choice) passes the real user id
    # Don't pull a second pool token for the same id in quick succession (double-tap
    # / retry). Distinct ids in a queue use distinct keys, so the queue still flows.
    if _should_skip(f"{uid}:send-otp:{fan}", 10.0):
        return await m.answer(i18n.t("id_in_progress"))
    if db_free:
        price = 0   # DB down → free, no pre-flight gate
    else:
        ok, reason, price = await billing.can_download(u)
        if not ok:
            await state.clear()
            return await m.answer(i18n.t("gate_refused", reason=reason))
    wait = await m.answer(i18n.t("otp_requesting", fan=fan))   # show the full FAN/FIN
    fayda.set_vip_context(bool(u.get("is_vip")))   # Server-4: regular vs VIP token pool
    provider, _mode = await fayda.get_provider()
    res = await provider.send_otp(fan)
    if not res.get("ok"):
        # It failed, so let the user retry the SAME id immediately (clear the guard).
        _recent.pop(f"{uid}:send-otp:{fan}", None)
        await state.clear()
        return await wait.edit_text(i18n.t("otp_send_fail", error=res.get("error")))
    await state.set_state(Flow.otp)
    await state.update_data(session=res.get("session"), price_cents=price, mode=u["billing_mode"],
                           fan_hash=_fan_hash(fan), queue=queue, delivery=delivery, db_free=db_free,
                           uid=uid, is_vip=bool(u.get("is_vip")))
    phone = _mask_phone(res.get("masked_mobile"))
    key = "otp_sent_to" if phone else "otp_sent"
    await wait.edit_text(i18n.t(key, phone=phone), reply_markup=kb.cancel_kb())


@router.message(Flow.otp, F.text)
async def on_otp(m: Message, state: FSMContext):
    blk = await _maint_block_download(m.from_user.id)
    if blk:
        await state.clear()
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    otp = m.text.replace(" ", "")
    if not OTP_RE.match(otp):
        return await m.answer(i18n.t("otp_enter_numeric"))
    data = await state.get_data()
    session, price_cents, mode, fan_hash = data.get("session"), data.get("price_cents", 0), data.get("mode"), data.get("fan_hash")
    db_free = bool(data.get("db_free"))
    queue = list(data.get("queue") or [])
    wait = await m.answer(i18n.t("verifying"))
    fayda.set_vip_context(bool(data.get("is_vip")))   # Server-4: regular vs VIP token pool
    provider, _mode = await fayda.get_provider()
    res = await provider.verify_pdf(session, otp)
    if not res.get("ok"):
        await state.clear()
        return await wait.edit_text(i18n.t("otp_send_fail", error=res.get("error")))

    await wait.edit_text(i18n.t("processing_delivery"))

    charge = None
    if not db_free:   # DB down → served free, nothing to charge or record
        try:
            charge = await billing.charge_and_log(m.from_user.id, int(price_cents), mode, fan_hash)
        except Exception:  # never fail delivery on a billing hiccup, but surface it loudly
            log.exception("charge_and_log failed for user %s (price=%s mode=%s)", m.from_user.id, price_cents, mode)
            mark_db_down()
    await wait.delete()

    # Deliver per the USER's own preference (set via 📄 Get PDF / 🖼 Get Screenshot):
    # 'both' (default), 'pdf', or 'screenshot'. Always falls back to whatever the
    # provider actually returned (API mode has no screenshots).
    delivery = data.get("delivery", "both")
    shots = res.get("screenshots") or []
    want_shots = bool(shots) and delivery in ("both", "screenshot")
    want_pdf = bool(res.get("pdf")) and (delivery in ("both", "pdf") or not want_shots)
    caption = (i18n.t("done_free") if db_free else i18n.t("done")) + (f" ({len(queue)} left)" if queue else "")
    if charge and charge.get("charged"):   # show what was deducted + the new net balance
        amt = billing.birr(charge["charged"])
        net = billing.birr((charge.get("balance") or 0) - (charge.get("owed") or 0))
        key = "charged_postpaid" if charge["mode"] == "postpaid" else "charged_prepaid"
        caption += "\n" + i18n.t(key, charged=amt, balance=net)
        if charge.get("from_bonus"):   # part (or all) came from the bonus wallet
            caption += "\n" + i18n.t("charged_from_bonus",
                                     bonus_used=billing.birr(charge["from_bonus"]),
                                     bonus_left=billing.birr(charge.get("bonus_balance") or 0))
    captioned = False
    sent_shot = False

    if want_shots:
        for i, s in enumerate(shots):
            last = (i == len(shots) - 1) and not want_pdf
            fn = s["filename"] if "." in s["filename"] else s["filename"] + ".png"
            try:
                await m.answer_photo(BufferedInputFile(s["bytes"], filename=fn),
                                     caption=caption if last else None)
                sent_shot = True
                captioned = captioned or last
            except Exception:
                log.exception("failed to send %s screenshot for %s", s.get("label"), m.from_user.id)
    # If the user wanted screenshots but none could be sent, still give them the PDF.
    if want_shots and not sent_shot and res.get("pdf"):
        want_pdf = True
    if want_pdf:
        fn = res.get("filename") or "fayda.pdf"
        base = fn[:-4] if fn.lower().endswith(".pdf") else fn
        try:
            suffix = (await settings_repo.get("pdf_filename_suffix")) or ""
        except Exception:
            suffix = ""
        fn = f"{base} {suffix}".strip() + ".pdf" if suffix else base + ".pdf"
        doc = BufferedInputFile(res["pdf"], filename=fn)
        await m.answer_document(doc, caption=caption)
        captioned = True
    if not captioned:
        await m.answer(caption)
    # Multi-FAN: continue with the next queued id, keeping the chosen output format.
    if queue:
        nxt_u, nxt_free = _DBDOWN_USER, db_free
        if not db_free:
            try:
                nxt_u = await users_repo.get(m.from_user.id) or _DBDOWN_USER
            except Exception:
                mark_db_down()
                nxt_u, nxt_free = _DBDOWN_USER, True
        await _begin_download(m, state, nxt_u, queue[0], queue[1:], delivery=delivery, db_free=nxt_free)
    else:
        await state.clear()


# ── forgot-FAN ───────────────────────────────────────────────────────────────
@router.message(Flow.forgot_name, F.text)
async def forgot_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name.split()) < 2:
        return await m.answer(i18n.t("forgot_need_fullname"), reply_markup=kb.cancel_kb())
    await state.update_data(name=name)
    await state.set_state(Flow.forgot_phone)
    await m.answer(i18n.t("forgot_phone"), reply_markup=kb.cancel_kb())


@router.message(Flow.forgot_phone, F.text)
async def forgot_phone(m: Message, state: FSMContext):
    phone = m.text.replace(" ", "")
    if not PHONE_RE.match(phone):
        return await m.answer(i18n.t("forgot_bad_phone"), reply_markup=kb.cancel_kb())
    data = await state.get_data()
    await state.clear()
    wait = await m.answer(i18n.t("forgot_requesting"))
    # Recovery is independent of the download mode — always via API if configured.
    res = await fayda.forgot_fan(data.get("name", ""), phone)
    if res.get("ok"):
        await wait.edit_text(i18n.t("forgot_done", phone=res.get("phone") or "your phone"))
    else:
        await wait.edit_text(i18n.t("forgot_err", error=res.get("error")))


# ── add-balance: receipt submission (auto-verify → auto-approve, else manual) ─
async def _notify_admins_payment(bot, payment: dict, from_user, flag: str = "", screenshot_file_id=None) -> None:
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"pay_ok:{payment['id']}"),
        InlineKeyboardButton(text="🚫 Reject", callback_data=f"pay_no:{payment['id']}"),
    ]])
    who = "@" + from_user.username if from_user.username else f"#{from_user.id}"
    body = f"💳 Payment #{payment['id']} from {who}\nReceipt: {payment['receipt_id']}\nTap Approve to set the amount."
    if screenshot_file_id:
        body += "\n📷 Sent as a screenshot."
    if flag:
        body += f"\n\n{flag}"
    for aid in config.ADMIN_IDS:
        try:
            if screenshot_file_id:
                await bot.send_photo(int(aid), screenshot_file_id, caption=body, reply_markup=ikb)
            else:
                await bot.send_message(int(aid), body, reply_markup=ikb)
        except Exception:
            log.exception("failed to notify admin %s of payment %s", aid, payment.get("id"))


async def _finalize_receipt(m: Message, wait: Message, receipt_id: str, v: dict, screenshot_file_id=None) -> None:
    """Given a verify() result, auto-approve (right merchant, not used, amount > 0) or
    fall to manual admin review. Shared by the text and screenshot paths."""
    flag = ""
    if v.get("ok") and int(v.get("amount_cents") or 0) > 0:
        payment, created = await payments_repo.submit(
            m.from_user.id, v.get("receipt_id") or receipt_id, v.get("bank", "telebirr"),
            int(v["amount_cents"]), v.get("provider", "auto"))
        if not created:
            return await wait.edit_text(i18n.t("already_submitted", status=payment["status"]))
        res = await payments_repo.approve(payment["id"], f"auto:{v.get('provider')}", int(v["amount_cents"]))
        if res.get("ok"):
            return await wait.edit_text(i18n.t("verified_added", amount=billing.birr(res["amount_cents"]), balance=billing.birr(res["balance_cents"])))
    elif v.get("receiver_mismatch"):
        flag = "⚠️ Auto-check: paid to a DIFFERENT account — verify before approving."
    elif v.get("already_used"):
        flag = "⚠️ Auto-check: receipt reported ALREADY USED — verify before approving."

    bank = v.get("bank") or payment_verify.detect_bank(receipt_id)
    payment, created = await payments_repo.submit(m.from_user.id, receipt_id, bank, 0, "manual")
    if not created:
        return await wait.edit_text(i18n.t("already_submitted", status=payment["status"]))
    await wait.edit_text(i18n.t("receipt_submitted", id=payment["id"]))
    await _notify_admins_payment(m.bot, payment, m.from_user, flag, screenshot_file_id)


@router.message(Flow.receipt, F.text)
async def on_receipt(m: Message, state: FSMContext):
    blk = await _maint_block_action(m.from_user.id)   # HIGH closes payments too
    if blk:
        await state.clear()
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    if not db_ready():   # payments need the DB — can't record money while it's down
        await state.clear()
        return await m.answer(i18n.t("payments_unavailable"), reply_markup=kb.main_kb(m.from_user.id))
    # Accept a bare txn number, a receipt link, OR the full Telebirr SMS from 127.
    # Prefer a token that has BOTH letters and digits (the Telebirr/CBE reference
    # shape) so an SMS/link doesn't pick a phone number, amount, or a word by mistake.
    tokens = re.findall(r"\b[A-Z0-9]{8,14}\b", m.text.strip().upper())
    mixed = [t for t in tokens if re.search(r"[A-Z]", t) and re.search(r"\d", t)]
    receipt_id = (mixed or tokens or [""])[0]
    if not receipt_id:
        return await m.answer(i18n.t("send_txn_short"), reply_markup=kb.cancel_kb())
    await state.clear()
    wait = await m.answer(i18n.t("checking_payment"))
    v = await payment_verify.verify(receipt_id) if await payment_verify.any_configured() else {"ok": False}
    await _finalize_receipt(m, wait, v.get("receipt_id") or receipt_id, v)


# ── add-balance via a Telebirr screenshot (OCR → look-alike correction → verify) ─
@router.message(F.photo)
async def on_payment_photo(m: Message, state: FSMContext):
    in_receipt = (await state.get_state()) == Flow.receipt.state
    if in_receipt:   # HIGH maintenance closes the Add-Balance receipt path too
        blk = await _maint_block_action(m.from_user.id)
        if blk:
            await state.clear()
            return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    if not db_ready():   # payments need the DB
        if in_receipt:
            await state.clear()
            await m.answer(i18n.t("payments_unavailable"), reply_markup=kb.main_kb(m.from_user.id))
        return
    try:
        u = await users_repo.ensure(m.from_user.id, m.from_user.username)
    except Exception:
        mark_db_down()
        return
    if u["status"] == "blocked" and not config.is_admin(m.from_user.id):
        return
    # Outside the Add-Balance step we only react to receipt-looking photos, and only
    # when an auto-verifier is configured (OCR is pointless without one).
    if not in_receipt and not await payment_verify.any_configured():
        return
    try:
        bio = await m.bot.download(m.photo[-1].file_id)
        raw = bio.read()
    except Exception:
        if in_receipt:
            await m.answer(i18n.t("image_read_fail"), reply_markup=kb.cancel_kb())
        return
    wait = await m.answer(i18n.t("reading_screenshot"))
    txn, amount, is_receipt = await asyncio.to_thread(payment_verify.ocr_telebirr, raw)
    if not txn:
        if in_receipt or is_receipt:
            return await wait.edit_text(i18n.t("couldnt_read_txn"))
        try:
            await wait.delete()
        except Exception:
            pass
        return
    if not in_receipt and not is_receipt:   # idle photo that isn't a receipt → ignore
        try:
            await wait.delete()
        except Exception:
            pass
        return
    await state.clear()
    await wait.edit_text(i18n.t("checking_payment"))
    if not await payment_verify.any_configured():
        v = {"ok": False}
    else:
        v = await payment_verify.verify_candidates(payment_verify.telebirr_candidates(txn), round((amount or 0) * 100))
    await _finalize_receipt(m, wait, v.get("receipt_id") or txn, v, screenshot_file_id=m.photo[-1].file_id)


async def _ask_format(m: Message, state: FSMContext, fans: list[str], dropped: int = 0) -> None:
    """FIN/FAN(s) entered → ask which output before pulling the OTP."""
    await state.set_state(Flow.choose_fmt)
    await state.update_data(fans=fans)
    head = (i18n.t("one_id", fan=fans[0]) if len(fans) == 1 else i18n.t("n_ids", n=len(fans))) + "\n"
    if dropped:
        head += i18n.t("dropped_note", max=MAX_MULTI_FAN, dropped=dropped) + "\n"
    await m.answer(head + i18n.t("choose_output"), reply_markup=kb.format_kb())


async def _run_download(m: Message, state: FSMContext, fans: list[str], delivery: str, uid) -> None:
    """Gate (DB / blocked / paused) then start the queue with the chosen format."""
    if not db_ready():
        if db_down_policy() == "refuse" and not config.is_admin(uid):
            return await m.answer(i18n.t("system_unavailable"))
        await m.answer(i18n.t("recovering_free"))
        return await _begin_download(m, state, _DBDOWN_USER, fans[0], fans[1:], delivery=delivery, db_free=True, uid=uid)
    try:
        u = await users_repo.ensure(uid, None)
    except Exception:
        mark_db_down()
        await m.answer(i18n.t("recovering_free"))
        return await _begin_download(m, state, _DBDOWN_USER, fans[0], fans[1:], delivery=delivery, db_free=True, uid=uid)
    if u["status"] == "blocked" and not config.is_admin(uid):
        return await m.answer(i18n.t("blocked"))
    if await _paused() and not config.is_admin(uid):
        return await m.answer(i18n.t("paused"))
    await _begin_download(m, state, u, fans[0], fans[1:], delivery=delivery, uid=uid)


@router.callback_query(F.data.startswith("dl:"))
async def on_choose_fmt(c: CallbackQuery, state: FSMContext):
    fmt = c.data.split(":", 1)[1]
    if fmt not in ("pdf", "screenshot", "both"):
        return await c.answer()
    blk = await _maint_block_download(c.from_user.id)
    if blk:
        await c.answer()
        await state.clear()
        return await c.message.answer(blk, reply_markup=kb.main_kb(c.from_user.id))
    data = await state.get_data()
    fans = list(data.get("fans") or [])
    if not fans:
        return await c.answer("Expired — send the FIN again.", show_alert=True)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)   # drop the choice buttons
    except Exception:
        pass
    await state.clear()
    await _run_download(c.message, state, fans, fmt, c.from_user.id)


# ── tapped Get PDF / Get Screenshot first → the FIN/FAN arrives here ──────────
@router.message(Flow.await_fan, F.text)
async def on_fan_awaited(m: Message, state: FSMContext):
    blk = await _maint_block_download(m.from_user.id)
    if blk:
        await state.clear()
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    data = await state.get_data()
    fmt = data.get("dl_fmt", "both")
    fans, _dropped = _parse_fans(m.text)
    if not fans:
        return await m.answer(i18n.t("send_fan_or_cancel"), reply_markup=kb.cancel_kb())
    if _should_skip(f"{m.from_user.id}:typed-fan", 4.0):
        return
    await state.clear()
    if len(fans) > 1:
        await m.answer(i18n.t("n_ids", n=len(fans)))
    await _run_download(m, state, fans, fmt, m.from_user.id)


# ── default: bare FIN/FAN(s) → ask output, then OTP ──────────────────────────
@router.message(F.text)
async def maybe_fan(m: Message, state: FSMContext):
    fans, dropped = _parse_fans(m.text)
    # Maintenance: a FIN/FAN is a download attempt (blocked at low+high); any other
    # stray text is a general action (blocked only at high, so the "send a FAN" hint
    # still shows at low).
    blk = await (_maint_block_download if fans else _maint_block_action)(m.from_user.id)
    if blk:
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    if not fans:
        return await m.answer(i18n.t("send_fan"), reply_markup=kb.main_kb(m.from_user.id))
    # Throttle rapid typed-FAN messages (double-taps / spam) — one every few seconds.
    if _should_skip(f"{m.from_user.id}:typed-fan", 4.0):
        return
    await _ask_format(m, state, fans, dropped)
