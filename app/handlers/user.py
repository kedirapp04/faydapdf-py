"""User-facing flows: download (FAN → OTP → PDF), wallet, add-balance, forgot-FAN.

Conversation state is aiogram FSM (in-memory); all persistent data is in Postgres.
"""
import hashlib
import logging
import re
import time

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from .. import config, fayda
from ..db import pool
from ..repo import users as users_repo, settings as settings_repo, payments as payments_repo
from ..services import billing
from . import keyboards as kb

router = Router()
log = logging.getLogger("faydapdf-py.user")

FAN_RE = re.compile(r"^\d{12,16}$")
OTP_RE = re.compile(r"^\d{4,10}$")
PHONE_RE = re.compile(r"^(?:\+?251|0)?9\d{8}$")

# One batch of ids is processed one-at-a-time; cap it so a single message can't
# fire off an unbounded run of pool-token pulls in Server-4 mode.
MAX_MULTI_FAN = 5

# Per-user OTP-request rate limit (in-memory, per process). Each download pulls a
# single-use pool token in Server-4 mode, so this is the main guard against one
# user draining the pool. A user is on one bot/process, so per-process is enough.
_RATE_LIMIT = 12          # max OTP requests…
_RATE_WINDOW = 60.0       # …per this many seconds
_otp_hits: dict[int, list[float]] = {}


def _rate_ok(user_id: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _otp_hits.get(user_id, []) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        _otp_hits[user_id] = hits
        return False
    hits.append(now)
    _otp_hits[user_id] = hits
    return True


class Flow(StatesGroup):
    otp = State()
    forgot_name = State()
    forgot_phone = State()
    receipt = State()


def _fan_hash(fan: str) -> str:
    return hashlib.sha256(fan.encode()).hexdigest()[:16]


async def _seen(chat_id, bot_id) -> None:
    """Record (user, bot) for broadcast, and remember the bot the user last used
    so cross-bot notifications reach them via a bot they actually started."""
    await pool().execute(
        "INSERT INTO chats (telegram_id, bot_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        int(chat_id), int(bot_id),
    )
    await pool().execute("UPDATE users SET last_bot_id=$1 WHERE telegram_id=$2", int(bot_id), int(chat_id))


async def _paused() -> bool:
    return await settings_repo.get_bool("paused", False)


WELCOME = "👋 Welcome! Send a 12–16 digit FIN/FAN to download its Fayda PDF, or use the buttons below."
HELP = (
    "❓ How to use\n\n"
    "📥 Download — send a 12–16 digit FIN/FAN; enter the OTP sent to the registered phone; get the PDF.\n"
    "💳 My Wallet — your balance / billing.\n"
    "💵 Add Balance — send a payment receipt for admin approval.\n"
    "🔑 Forgot FAN / FIN — recover your number by SMS (free)."
)


# ── commands ────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await users_repo.ensure(m.from_user.id, m.from_user.username)
    await _seen(m.chat.id, m.bot.id)
    await m.answer(WELCOME, reply_markup=kb.main_kb(m.from_user.id))


@router.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("✖️ Cancelled.", reply_markup=kb.main_kb(m.from_user.id))


@router.callback_query(F.data == "cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer("Cancelled")
    await c.message.answer("✖️ Cancelled.", reply_markup=kb.main_kb(c.from_user.id))


# ── reply-keyboard buttons (match in any state; reset the flow) ──────────────
@router.message(F.text.in_(kb.BUTTONS))
async def buttons(m: Message, state: FSMContext):
    await state.clear()
    u = await users_repo.ensure(m.from_user.id, m.from_user.username)
    await _seen(m.chat.id, m.bot.id)
    text = m.text
    # Blocked users can read Help but can't act (download / pay / forgot / etc.).
    if u["status"] == "blocked" and not config.is_admin(m.from_user.id) and text != kb.BTN_HELP:
        return await m.answer("🚫 Your access is blocked. Contact the admin.")
    if text == kb.BTN_HELP:
        return await m.answer(HELP, reply_markup=kb.main_kb(m.from_user.id))
    if text == kb.BTN_DOWNLOAD:
        return await m.answer("📥 Send a 12–16 digit FIN/FAN to download its Fayda PDF.")
    if text == kb.BTN_FORGOT:
        await state.set_state(Flow.forgot_name)
        return await m.answer("🔑 Forgot FAN/FIN — free.\n\nSend your FULL NAME (e.g. Abebe Kebede Alemu):", reply_markup=kb.cancel_kb())
    if text == kb.BTN_WALLET:
        return await _show_wallet(m)
    if text == kb.BTN_PAY:
        from ..services import payment_verify
        await state.set_state(Flow.receipt)
        instr = await payment_verify.instructions()
        msg = "💵 Add Balance\n"
        if instr:
            msg += f"\n{instr}\n"
        msg += "\nThen send the transaction number here (Telebirr or CBE, e.g. DGI70RYNL7)."
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
    lines = [f"💳 Wallet — {mode}"]
    if mode == "prepaid":
        lines.append(f"Balance: {billing.birr(u['balance_cents'])}")
    elif mode == "postpaid":
        lines.append(f"Owed: {billing.birr(u['owed_cents'])} / {billing.birr(u['credit_limit_cents'])}")
    price = await billing.price_for(u)
    lines.append(f"Price per download: {billing.birr(price)}")
    await m.answer("\n".join(lines), reply_markup=kb.main_kb(m.from_user.id))


# ── download: start (one FAN of the queue) + OTP step ───────────────────────
async def _begin_download(m: Message, state: FSMContext, u: dict, fan: str, queue: list[str]):
    if not _rate_ok(m.from_user.id):
        await state.clear()
        return await m.answer("⏳ Too many requests in a short time. Please wait a minute and try again.")
    ok, reason, price = await billing.can_download(u)
    if not ok:
        await state.clear()
        return await m.answer(f"🚫 {reason}")
    wait = await m.answer(f"📩 Sending OTP for …{fan[-4:]}…")
    provider, _mode = await fayda.get_provider()
    res = await provider.send_otp(fan)
    if not res.get("ok"):
        await state.clear()
        return await wait.edit_text(f"⚠️ {res.get('error')}\n\nSend the FIN again to retry.")
    await state.set_state(Flow.otp)
    await state.update_data(session=res.get("session"), price_cents=price, mode=u["billing_mode"], fan_hash=_fan_hash(fan), queue=queue)
    masked = res.get("masked_mobile")
    await wait.edit_text(
        f"📨 OTP sent{(' to ' + masked) if masked else ''} for …{fan[-4:]}.\nEnter the code you received:",
        reply_markup=kb.cancel_kb(),
    )


@router.message(Flow.otp, F.text)
async def on_otp(m: Message, state: FSMContext):
    otp = m.text.replace(" ", "")
    if not OTP_RE.match(otp):
        return await m.answer("Send the numeric OTP code, or tap Cancel.")
    data = await state.get_data()
    session, price_cents, mode, fan_hash = data.get("session"), data.get("price_cents", 0), data.get("mode"), data.get("fan_hash")
    queue = list(data.get("queue") or [])
    wait = await m.answer("⏳ Verifying & generating the PDF…")
    provider, _mode = await fayda.get_provider()
    res = await provider.verify_pdf(session, otp)
    if not res.get("ok"):
        await state.clear()
        return await wait.edit_text(f"⚠️ {res.get('error')}\n\nSend the FIN again to retry.")
    try:
        await billing.charge_and_log(m.from_user.id, int(price_cents), mode, fan_hash)
    except Exception:  # never fail delivery on a billing hiccup, but surface it loudly
        log.exception("charge_and_log failed for user %s (price=%s mode=%s)", m.from_user.id, price_cents, mode)
    await wait.delete()
    doc = BufferedInputFile(res["pdf"], filename=res.get("filename") or "fayda.pdf")
    await m.answer_document(doc, caption="✅ Done." + (f" ({len(queue)} left)" if queue else ""))
    # Multi-FAN: continue with the next queued id.
    if queue:
        u = await users_repo.get(m.from_user.id)
        await _begin_download(m, state, u, queue[0], queue[1:])
    else:
        await state.clear()


# ── forgot-FAN ───────────────────────────────────────────────────────────────
@router.message(Flow.forgot_name, F.text)
async def forgot_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name.split()) < 2:
        return await m.answer("Please send your FULL name (e.g. Abebe Kebede Alemu), or tap Cancel.", reply_markup=kb.cancel_kb())
    await state.update_data(name=name)
    await state.set_state(Flow.forgot_phone)
    await m.answer("📱 Now send your REGISTERED phone number (e.g. 0911223344):", reply_markup=kb.cancel_kb())


@router.message(Flow.forgot_phone, F.text)
async def forgot_phone(m: Message, state: FSMContext):
    phone = m.text.replace(" ", "")
    if not PHONE_RE.match(phone):
        return await m.answer("Send a valid Ethiopian phone (e.g. 0911223344), or tap Cancel.", reply_markup=kb.cancel_kb())
    data = await state.get_data()
    await state.clear()
    wait = await m.answer("📩 Requesting your FAN + FIN by SMS…")
    # Recovery is independent of the download mode — always via API if configured.
    res = await fayda.forgot_fan(data.get("name", ""), phone)
    if res.get("ok"):
        await wait.edit_text(f"✅ Done. Your FAN and FIN were sent by SMS to {res.get('phone') or 'your phone'}.")
    else:
        await wait.edit_text(f"⚠️ {res.get('error')}")


# ── add-balance: receipt submission (auto-verify → auto-approve, else manual) ─
async def _notify_admins_payment(bot, payment: dict, from_user, flag: str = "") -> None:
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"pay_ok:{payment['id']}"),
        InlineKeyboardButton(text="🚫 Reject", callback_data=f"pay_no:{payment['id']}"),
    ]])
    who = "@" + from_user.username if from_user.username else f"#{from_user.id}"
    body = f"💳 Payment #{payment['id']} from {who}\nReceipt: {payment['receipt_id']}\nTap Approve to set the amount."
    if flag:
        body += f"\n\n{flag}"
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_message(int(aid), body, reply_markup=ikb)
        except Exception:
            log.exception("failed to notify admin %s of payment %s", aid, payment.get("id"))


@router.message(Flow.receipt, F.text)
async def on_receipt(m: Message, state: FSMContext):
    from ..services import payment_verify
    match = re.search(r"\b[A-Z0-9]{8,14}\b", m.text.strip().upper())
    if not match:
        return await m.answer("Send the transaction number (8–14 characters), or tap Cancel.", reply_markup=kb.cancel_kb())
    receipt_id = match.group(0)
    await state.clear()
    wait = await m.answer("🔎 Checking your payment…")

    # 1) Try auto-verification. On a confirmed receipt (right merchant, not already
    #    used, with an amount), submit + approve atomically → instant top-up.
    flag = ""  # a note for admins if auto-verify rejected the receipt
    if payment_verify.any_configured():
        v = await payment_verify.verify(receipt_id)
        if v.get("ok") and int(v.get("amount_cents") or 0) > 0:
            payment, created = await payments_repo.submit(m.from_user.id, v["receipt_id"], v.get("bank", "telebirr"), int(v["amount_cents"]), v["provider"])
            if not created:
                return await wait.edit_text(f"This receipt was already submitted (status: {payment['status']}).")
            res = await payments_repo.approve(payment["id"], f"auto:{v['provider']}", int(v["amount_cents"]))
            if res.get("ok"):
                return await wait.edit_text(f"✅ Verified! {billing.birr(res['amount_cents'])} added.\nNew balance: {billing.birr(res['balance_cents'])}.")
        elif v.get("receiver_mismatch"):
            flag = "⚠️ Auto-check: paid to a DIFFERENT account — verify before approving."
        elif v.get("already_used"):
            flag = "⚠️ Auto-check: receipt reported ALREADY USED — verify before approving."

    # 2) Manual fallback — record as pending and hand it to the admins.
    payment, created = await payments_repo.submit(m.from_user.id, receipt_id, payment_verify.detect_bank(receipt_id), 0, "manual")
    if not created:
        return await wait.edit_text(f"This receipt was already submitted (status: {payment['status']}).")
    await wait.edit_text(f"✅ Receipt submitted (#{payment['id']}). An admin will review it shortly.")
    await _notify_admins_payment(m.bot, payment, m.from_user, flag)


# ── default: one or MORE bare FANs → start the download queue ────────────────
@router.message(F.text)
async def maybe_fan(m: Message, state: FSMContext):
    u = await users_repo.ensure(m.from_user.id, m.from_user.username)
    await _seen(m.chat.id, m.bot.id)
    if u["status"] == "blocked":
        return await m.answer("🚫 Your access is blocked. Contact the admin.")

    # Accept multiple ids in one message (space/newline separated), dedup, keep order.
    fans = list(dict.fromkeys(re.findall(r"\b\d{12,16}\b", m.text)))
    if not fans:
        return await m.answer("Send a 12–16 digit FIN/FAN to download its Fayda PDF.", reply_markup=kb.main_kb(m.from_user.id))

    if await _paused() and not config.is_admin(m.from_user.id):
        return await m.answer("⏸ The service is paused for maintenance. Please try again later.")

    # Cap the batch so one message can't kick off an unbounded run of downloads.
    dropped = 0
    if len(fans) > MAX_MULTI_FAN:
        dropped = len(fans) - MAX_MULTI_FAN
        fans = fans[:MAX_MULTI_FAN]
    if len(fans) > 1:
        note = f"📥 {len(fans)} IDs received — I'll do them one by one."
        if dropped:
            note += f"\n(Only the first {MAX_MULTI_FAN} are processed per message; {dropped} ignored — send the rest after.)"
        await m.answer(note)
    await _begin_download(m, state, u, fans[0], fans[1:])
