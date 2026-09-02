"""User-facing flows: download (FAN → OTP → PDF), wallet, add-balance, forgot-FAN.

Conversation state is aiogram FSM (in-memory); all persistent data is in Postgres.
"""
import asyncio
import base64
import hashlib
import logging
import os
import re
import time

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InputMediaPhoto

from .. import config, fayda, i18n
from ..db import pool, db_ready, db_down_policy, mark_db_down, recheck_if_down
from ..repo import users as users_repo, settings as settings_repo, payments as payments_repo
from ..services import billing, payment_verify, maintenance
from . import keyboards as kb

router = Router()
log = logging.getLogger("faydapdf-py.user")

FAN_RE = re.compile(r"^\d{12,16}$")
OTP_RE = re.compile(r"^\d{6}$")   # Fayda OTPs are exactly 6 digits (a 10-digit phone is NOT one)
PHONE_RE = re.compile(r"^(?:\+?251|0)?9\d{8}$")
_PHONE_ANY = re.compile(r"(?:\+?251|0)?(9\d{8})")

# How long the typed-FAN "unsigned QR" notice stays before it self-deletes. Long
# enough to read while entering the OTP, then it clears itself so the chat isn't
# left with a scary warning after the PDF arrives. Tune with UNSIGNED_NOTICE_TTL_S;
# set it to 0 to keep the notice permanently.
try:
    _UNSIGNED_NOTICE_TTL = float(os.getenv("UNSIGNED_NOTICE_TTL_S") or 60)
except ValueError:
    _UNSIGNED_NOTICE_TTL = 60.0


async def _send_temp(message: Message, text: str, ttl: float, parse_mode: str | None = None) -> None:
    """Send a message and delete it after `ttl` seconds. Best-effort: a send OR
    delete can fail (bad markup, message already gone, chat left, ttl<=0 disables)
    and must never disrupt the download flow, so every failure is swallowed. On a
    parse_mode send failure, retry once as plain text so the notice still shows."""
    try:
        sent = await message.answer(text, parse_mode=parse_mode)
    except Exception:
        if not parse_mode:
            return
        try:
            sent = await message.answer(text)   # markup rejected → send plain
        except Exception:
            return
    if ttl and ttl > 0:
        async def _reap():
            try:
                await asyncio.sleep(ttl)
                await sent.delete()
            except Exception:
                pass
        asyncio.create_task(_reap())


def _sanitize_name(raw: str) -> str:
    return re.sub(r"[<>]", "", re.sub(r"[\x00-\x1f\x7f]", "", str(raw or ""))).strip()[:100]


def _name_ok(n) -> bool:
    """A valid FULL name in ENGLISH only: ≥ 2 Latin-letter words (no Amharic/other script)."""
    return bool(n) and len(n.split()) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z '.\-]*", n) is not None


def _norm_phone(raw) -> "str | None":
    """0 / +251 / 251 / bare-9 (with spaces, dashes, parens) → 0XXXXXXXXX."""
    m = re.match(r"^(?:\+?251|0)?(9\d{8})$", re.sub(r"[\s\-()]", "", str(raw or "")))
    return "0" + m.group(1) if m else None


def _parse_name_phone(text: str):
    """Accept name + phone in flexible layouts — two lines, one line mixed, phone
    anywhere, or just one piece. Returns (name|None, phone|None); the caller still
    requires BOTH. Mirrors faydapdf-railway's parseNameAndPhone."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None, None
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    name = phone = None
    if len(lines) >= 2:
        for ln in lines:
            if phone is None:
                mm = _PHONE_ANY.search(re.sub(r"[\s\-()]", "", ln))
                if mm:
                    phone = "0" + mm.group(1)
                    continue
            if name is None:
                name = _sanitize_name(ln)
    else:
        ln = lines[0]
        mm = _PHONE_ANY.search(ln)
        if mm:
            phone = "0" + mm.group(1)
            rest = (ln[:mm.start()] + " " + ln[mm.end():]).strip()
            if rest:
                name = _sanitize_name(rest)
        else:
            name = _sanitize_name(ln)
    return name, phone

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
        price = 0 if await billing.free_mode() else await billing.new_price_cents()
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
        if await billing.topup_bonus_enabled():      # tiered top-up bonus strategy
            bonus = await billing.topup_bonus_lines()
            if bonus:
                msg += "\n\n" + i18n.t("topup_bonus_intro") + "\n" + bonus
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
    # TOTAL across BOTH money wallets (old-price + new-price) minus any owed debt. Shown for
    # the money modes AND for anyone who actually holds money — a counter-mode user with a
    # balance previously saw no figure at all.
    money = u["balance_cents"] + u.get("balance_new_cents", 0)
    bonus = u.get("bonus_balance_cents", 0)
    if mode in ("prepaid", "postpaid") or money or bonus:
        lines.append(i18n.t("wallet_balance", balance=billing.birr(money - u["owed_cents"])))
    if bonus > 0:
        lines.append(i18n.t("wallet_bonus", bonus=billing.birr(bonus)))
    price = await billing.display_price_for(u)   # advertised (new) price; old balance is charged less
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
    # Server 5 works from the QR image ONLY. The QR is pinned to the id it was scanned
    # from, so a typed FAN (or a QR scanned for a different id) has none — and without
    # it the card could only carry a generated QR that no verifier accepts. Refuse
    # BEFORE the billing gate and the OTP, so nothing is charged or sent.
    data = await state.get_data()
    qr_b64 = data.get("qr_b64") if data.get("qr_fan") == fan else None
    _mode = await fayda.active_mode(m.bot.id)
    if not qr_b64 and _mode == "server5":
        # A typed FAN is allowed only if enabled for this user / bot / generally
        # (most-specific wins). The card is still drawn, but its QR is generated from
        # the identity data and will not verify — say so before charging anyone.
        if not await fayda.allow_typed_fan(m.bot.id, uid):
            await state.clear()
            return await m.answer(i18n.t("qr_required"), reply_markup=kb.main_kb(uid))
        unsigned_qr = True
    elif _mode == "api":
        # API mode routes through the gateway, which runs Server 5 (the card QR is
        # generated, not scanned, so it won't verify). Warn at OTP entry — same
        # notice, same timing as the native Server-5 typed-FAN path.
        unsigned_qr = True
    else:
        unsigned_qr = False
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
    # Server 5: the QR scanned from this user's screenshot, for THIS id (see above).
    fayda.set_qr_context(base64.b64decode(qr_b64) if qr_b64 else None)
    provider, _mode = await fayda.get_provider(m.bot.id)   # per-bot download mode
    res = await provider.send_otp(fan)
    if not res.get("ok"):
        # It failed, so let the user retry the SAME id immediately (clear the guard).
        _recent.pop(f"{uid}:send-otp:{fan}", None)
        await state.clear()
        return await wait.edit_text(i18n.t("otp_send_fail", error=res.get("error")))
    await state.set_state(Flow.otp)
    await state.update_data(unsigned_qr=unsigned_qr,
                           session=res.get("session"), price_cents=price, mode=u["billing_mode"],
                           fan_hash=_fan_hash(fan), queue=queue, delivery=delivery, db_free=db_free,
                           uid=uid, is_vip=bool(u.get("is_vip")))
    phone = _mask_phone(res.get("masked_mobile"))
    key = "otp_sent_to" if phone else "otp_sent"
    # Typed-FAN download: the card's QR is generated, so it will not verify. Show the
    # warning FIRST (as its own self-deleting message, so the chat isn't left with a
    # scary notice once the PDF arrives), then the OTP prompt ~1s later — so the user
    # reads the warning before being asked for the code. The download is never blocked.
    if unsigned_qr:
        try:
            await wait.delete()          # drop the "requesting" spinner so the warning sits on top
        except Exception:
            pass
        await _send_temp(m, i18n.t("unsigned_qr_notice"), _UNSIGNED_NOTICE_TTL, parse_mode="Markdown")
        await asyncio.sleep(1)
        await m.answer(i18n.t(key, phone=phone), reply_markup=kb.cancel_kb())
    else:
        await wait.edit_text(i18n.t(key, phone=phone), reply_markup=kb.cancel_kb())


@router.message(Flow.otp, F.text)
async def on_otp(m: Message, state: FSMContext):
    blk = await _maint_block_download(m.from_user.id)
    if blk:
        await state.clear()
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    otp = m.text.replace(" ", "")
    if not OTP_RE.match(otp):
        # A new FIN/FAN while we're waiting for the OTP means "forget that one, do this one
        # instead" — abandon the pending session and start a fresh download. Unambiguous: an
        # OTP is 4-10 digits and a FIN/FAN is 12-16, so the two can never collide.
        # RECEIPT FIRST (same rule as the other entry points): a Telebirr SMS embeds the
        # payer's phone as 251XXXXXXXXX = 12 digits, which would otherwise look like a FIN
        # and cancel the download. Credit it and STAY in the OTP step so the pending
        # download can still be finished. (A receipt PHOTO is handled in any state already.)
        if db_ready() and _looks_like_receipt(m.text):
            try:
                await users_repo.ensure(m.from_user.id, m.from_user.username)
            except Exception:
                mark_db_down()
            else:
                if await _submit_receipt_text(m, m.text):
                    return await m.answer(i18n.t("otp_enter_numeric"))   # nudge: OTP still pending
        # A new FIN/FAN means "forget that one, do this one instead".
        fans, _dropped = _parse_fans(m.text)
        if fans:
            delivery = (await state.get_data()).get("delivery") or "pdf"   # keep PDF/screenshot
            await state.clear()
            if len(fans) > 1:
                await m.answer(i18n.t("n_ids", n=len(fans)))
            return await _run_download(m, state, fans, delivery, m.from_user.id)
        return await m.answer(i18n.t("otp_enter_numeric"))
    # The typed-FAN "unsigned QR" notice is shown earlier, when we ASK for the OTP
    # (see send-otp handler), as a self-deleting message — not here on submission.
    await _finish_otp(m, state, otp)


async def _finish_otp(m: Message, state: FSMContext, otp: str, uid=None, bot=None) -> None:
    """Verify the OTP and deliver. Split out of on_otp so the confirmation step can
    resume the exact same path."""
    # On the button path m.from_user is the BOT and m.bot may be unbound, so the
    # caller hands us both explicitly; on the text path they come off the message.
    uid = uid or m.from_user.id
    bot = bot or m.bot
    data = await state.get_data()
    session, price_cents, mode, fan_hash = data.get("session"), data.get("price_cents", 0), data.get("mode"), data.get("fan_hash")
    db_free = bool(data.get("db_free"))
    queue = list(data.get("queue") or [])
    wait = await m.answer(i18n.t("verifying"))
    fayda.set_vip_context(bool(data.get("is_vip")))   # Server-4: regular vs VIP token pool
    provider, _mode = await fayda.get_provider(bot.id)   # per-bot download mode
    res = await provider.verify_pdf(session, otp)
    if not res.get("ok"):
        await state.clear()
        return await wait.edit_text(i18n.t("otp_send_fail", error=res.get("error")))

    await wait.edit_text(i18n.t("processing_delivery"))

    charge = None
    if not db_free:   # DB down → served free, nothing to charge or record
        try:
            charge = await billing.charge_and_log(uid, int(price_cents), mode, fan_hash)
        except Exception:  # never fail delivery on a billing hiccup, but surface it loudly
            log.exception("charge_and_log failed for user %s (price=%s mode=%s)", uid, price_cents, mode)
            mark_db_down()
    await wait.delete()

    # Deliver the ONE format the user chose (📄 Get PDF / 🖼 Get Screenshot): 'pdf' or
    # 'screenshot' — 'Both' was removed. Always falls back to whatever the provider
    # actually returned (API mode has no screenshots).
    delivery = data.get("delivery", "pdf")
    shots = res.get("screenshots") or []
    want_shots = bool(shots) and delivery in ("both", "screenshot")
    want_pdf = bool(res.get("pdf")) and (delivery in ("both", "pdf") or not want_shots)
    caption = (i18n.t("done_free") if db_free else i18n.t("done")) + (f" ({len(queue)} left)" if queue else "")
    if charge and charge.get("charged"):   # show what was deducted + the new net balance
        amt = billing.birr(charge["charged"])
        # TOTAL across both money wallets (old-price + new-price) minus any debt — the same
        # figure the wallet screen shows, so the two can never disagree.
        net = billing.birr((charge.get("balance") or 0) + (charge.get("balance_new") or 0)
                           - (charge.get("owed") or 0))
        key = "charged_postpaid" if charge["mode"] == "postpaid" else "charged_prepaid"
        caption += "\n" + i18n.t(key, charged=amt, balance=net)
        if charge.get("from_bonus"):   # part (or all) came from the bonus wallet
            caption += "\n" + i18n.t("charged_from_bonus",
                                     bonus_used=billing.birr(charge["from_bonus"]),
                                     bonus_left=billing.birr(charge.get("bonus_balance") or 0))
    captioned = False
    sent_shot = False
    sent_pdf = False

    if want_shots:
        files = []
        for s in shots:
            fn = s["filename"] if "." in s["filename"] else s["filename"] + ".png"
            files.append(BufferedInputFile(s["bytes"], filename=fn))
        cap = None if want_pdf else caption      # the PDF carries the caption when both go out
        try:
            if len(files) > 1:
                # ONE album (like faydapdf-railway) instead of separate photos. Telegram shows
                # the FIRST item's caption as the album caption. sendMediaGroup needs 2-10
                # items, so a single screenshot still goes as an ordinary photo.
                await m.answer_media_group(
                    [InputMediaPhoto(media=f, caption=cap if i == 0 else None)
                     for i, f in enumerate(files)])
            else:
                await m.answer_photo(files[0], caption=cap)
            sent_shot = True
            captioned = cap is not None
        except Exception:
            log.exception("media group failed for %s — falling back to single photos", uid)
            for i, f in enumerate(files):     # album rejected → send them one by one
                last = (i == len(files) - 1) and not want_pdf
                try:
                    await m.answer_photo(f, caption=caption if last else None)
                    sent_shot = True
                    captioned = captioned or last
                except Exception:
                    log.exception("failed to send screenshot %s for %s", i, uid)
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
        try:
            await m.answer_document(doc, caption=caption)
            sent_pdf = True
            captioned = True
        except Exception:
            # A failed upload (flaky link, ~1MB file) must NOT leave the user charged —
            # handled by the refund guard below.
            log.exception("failed to send PDF for %s", uid)
    # NOTHING reached the user (upload failed / all sends errored) → give the money back.
    # The charge happens before delivery so the caption can show the new balance, so this
    # guard is what keeps 'charged but no PDF' from ever sticking.
    if not (sent_shot or sent_pdf):
        refunded = False
        try:
            refunded = await billing.refund_download(uid, charge or {})
        except Exception:
            log.exception('refund failed for %s after undelivered download', uid)
        await m.answer(i18n.t('delivery_failed_refunded' if refunded else 'delivery_failed'))
        await state.clear()
        return
    if not captioned:
        await m.answer(caption)
    # (The generated-QR warning for API/Server-5 downloads is shown earlier, at the
    # OTP-ask stage — see the send-otp handler — not here on delivery.)
    # Multi-FAN: continue with the next queued id, keeping the chosen output format.
    if queue:
        nxt_u, nxt_free = _DBDOWN_USER, db_free
        if not db_free:
            try:
                nxt_u = await users_repo.get(uid) or _DBDOWN_USER
            except Exception:
                mark_db_down()
                nxt_u, nxt_free = _DBDOWN_USER, True
        await _begin_download(m, state, nxt_u, queue[0], queue[1:], delivery=delivery, db_free=nxt_free)
    else:
        await state.clear()


# ── forgot-FAN ───────────────────────────────────────────────────────────────
@router.message(Flow.forgot_name, F.text)
@router.message(Flow.forgot_phone, F.text)
async def forgot_collect(m: Message, state: FSMContext):
    """Flexible: accept the full name + phone together (any layout) or one at a
    time. BOTH are mandatory — we keep whatever's provided and ask for the rest."""
    data = await state.get_data()
    pn, pp = _parse_name_phone(m.text)
    # Only the PHONE matters: id.et's resend-sms body is phone-only, so the name is never
    # sent upstream. Any name is accepted, and a bare phone number works on its own
    # (a placeholder stands in for the name we keep for our own records).
    name = data.get("name") or pn or "A"
    phone = data.get("phone") or pp
    if not phone:
        await state.update_data(name=name, phone=None)
        await state.set_state(Flow.forgot_phone)
        return await m.answer(i18n.t("forgot_phone"), reply_markup=kb.cancel_kb())

    await state.clear()
    wait = await m.answer(i18n.t("forgot_requesting"))
    res = await fayda.forgot_fan_direct(phone)
    if res.get("ok"):
        return await wait.edit_text(i18n.t("forgot_done", phone=res.get("phone") or "your phone"))
    reason = res.get("reason")
    if reason == "rate_limited":
        from ..fayda.forgot_direct import human_wait
        wait_txt = human_wait(res.get("retry_after"))
        key = "forgot_wait" if wait_txt else "forgot_wait_unknown"
        return await wait.edit_text(i18n.t(key, wait=wait_txt))
    if reason == "not_registered":
        return await wait.edit_text(i18n.t("forgot_not_registered", phone=phone))
    if reason == "send_failed":     # Fayda's SMS gateway hiccuped — the number is fine
        return await wait.edit_text(i18n.t("forgot_send_failed"))
    if reason == "invalid_phone":
        return await wait.edit_text(i18n.t("forgot_bad_phone"))
    return await wait.edit_text(i18n.t("forgot_unavailable"))


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


_RENOTIFY_AFTER = 600.0          # seconds
_last_renotify: dict[int, float] = {}


def _should_renotify(payment_id: int) -> bool:
    """Throttle admin re-alerts for a re-sent receipt: a user tapping send five times in a
    row must not fire five DMs at every admin. In-memory on purpose — worst case a restart
    allows one extra ping, which is far better than losing the alert entirely."""
    import time as _t
    now = _t.monotonic()
    last = _last_renotify.get(payment_id)
    if last is not None and now - last < _RENOTIFY_AFTER:
        return False
    _last_renotify[payment_id] = now
    if len(_last_renotify) > 5000:                      # bound the dict
        for k in sorted(_last_renotify, key=_last_renotify.get)[:2500]:
            _last_renotify.pop(k, None)
    return True


async def _retry_pending(m: Message, wait: Message, payment: dict, receipt_id: str,
                         hint: str, flag: str, screenshot_file_id=None) -> None:
    """A receipt was re-sent while its row is still PENDING.

    The re-check already happened: every send runs the selected verifier (plus look-alike
    variants) BEFORE we get here, and _finalize_receipt credits the existing pending row
    the moment that verifier confirms it — no admin step. Reaching this point means the
    verifier said "not confirmed" again, so there is no amount to credit; approve() would
    refuse amount <= 0 rather than invent one.

    What we must not do is dead-end. The old code replied "already submitted (pending)"
    and stopped, and since the admins were only alerted when the row was NEW, a missed
    first alert could never resurface however many times the user re-sent. So: tell the
    user plainly it is being reviewed and they must not pay twice, and re-alert the
    admins (throttled)."""
    await wait.edit_text(i18n.t("receipt_pending_again", id=payment["id"]) + hint)
    if _should_renotify(payment["id"]):
        await _notify_admins_payment(m.bot, payment, m.from_user,
                                     (flag + " 🔁 RESUBMITTED by the user — auto-verify still "
                                      "cannot confirm it").strip(), screenshot_file_id)


async def _finalize_receipt(m: Message, wait: Message, receipt_id: str, v: dict, screenshot_file_id=None) -> None:
    """Given a verify() result, auto-approve (right merchant, not used, amount > 0) or
    fall to manual admin review. Shared by the text and screenshot paths."""
    if v.get("ok") and int(v.get("amount_cents") or 0) > 0:
        payment, created = await payments_repo.submit(
            m.from_user.id, v.get("receipt_id") or receipt_id, v.get("bank", "telebirr"),
            int(v["amount_cents"]), v.get("provider", "auto"))
        # A receipt that verifies is credited even if the row already exists and is still
        # PENDING (user re-sent it while waiting for an admin) — approve() only ever touches
        # a row that is still 'pending' (guarded UPDATE), and receipt_id is UNIQUE, so a
        # payment can be credited exactly ONCE no matter how many times it is re-sent.
        if not created and payment["status"] != "pending":
            return await wait.edit_text(i18n.t("already_submitted", status=payment["status"]))
        res = await payments_repo.approve(payment["id"], f"auto:{v.get('provider')}", int(v["amount_cents"]))
        if res.get("ok"):
            if res.get("bonus_cents"):
                await m.answer(i18n.t("bonus_notify", amount=billing.birr(res["bonus_cents"]),
                                      bonus=billing.birr(res["bonus_balance_cents"])))
            return await wait.edit_text(i18n.t("verified_added", amount=billing.birr(res["amount_cents"]), balance=billing.birr(res["balance_cents"])))
        if not created:   # someone/something else decided it first — report that, don't retry
            fresh = await payments_repo.get(payment["id"]) or payment
            return await wait.edit_text(i18n.t("already_submitted", status=fresh["status"]))

    bank = v.get("bank") or payment_verify.detect_bank(receipt_id)

    # A provider CONFIRMED a real payment but to a DIFFERENT account (receiver was
    # extracted and doesn't match any of ours) → AUTO-REJECT, don't bother the admin.
    # receiver_mismatch only fires when a receiver IS configured (fails open otherwise).
    # A screenshot that couldn't be credited is usually a MISREAD transaction number, not a
    # bad payment — so point the user at the exact text they can send instead.
    hint = ("\n\n" + i18n.t("try_sms_or_link")) if screenshot_file_id else ""

    if v.get("receiver_mismatch"):
        payment, created = await payments_repo.submit(m.from_user.id, receipt_id, bank, 0, "auto")
        if not created:
            return await wait.edit_text(i18n.t("already_submitted", status=payment["status"]))
        await payments_repo.reject(payment["id"], "auto:receiver_mismatch", "paid to a different account")
        return await wait.edit_text(i18n.t("receipt_wrong_account") + hint)

    flag = ""
    if v.get("already_used"):
        flag = "⚠️ Auto-check: receipt reported ALREADY USED — verify before approving."
    payment, created = await payments_repo.submit(m.from_user.id, receipt_id, bank, 0, "manual")
    if not created:
        if payment["status"] != "pending":
            return await wait.edit_text(i18n.t("already_submitted", status=payment["status"]))
        return await _retry_pending(m, wait, payment, receipt_id, hint, flag, screenshot_file_id)
    await wait.edit_text(i18n.t("receipt_submitted", id=payment["id"]) + hint)
    # The admin still sees the image itself in their Telegram DM (attached below).
    await _notify_admins_payment(m.bot, payment, m.from_user, flag, screenshot_file_id)


# ── auto-detect a payment receipt anywhere (link / txn number / 127 SMS) ──────
# Mirrors faydapdf-railway detectBank + extractTelebirrTransactionId. Key rule: a
# real Telebirr code is 10 alphanumerics that contains at least one LETTER — so a
# phone number / amount (all digits, e.g. 0982637420) or a 12-digit number is NEVER
# treated as a receipt, but a letter-heavy code still is. CBE is an FT… reference.
_TELEBIRR_LINK_RE = re.compile(r"transactioninfo\.ethiotelecom\.et/receipt/([A-Za-z0-9]+)", re.I)
_CBE_HOST_RE = re.compile(r"apps\.cbe\.com\.et|mbreciept\.cbe\.com\.et|mb\.cbe\.com\.et", re.I)
_CBE_REF_RE = re.compile(r"FT[A-Z0-9]{6,}", re.I)
_SMS_TXN_RE = re.compile(r"transaction\s*(?:number|no\.?)\s*(?:is|:)?\s*([A-Za-z0-9]{8,15})", re.I)


def _is_telebirr_ref(v: str) -> bool:
    """10 alphanumerics containing at least one LETTER (never an all-numeric phone
    number/amount). An all-letters code is allowed — those can occur."""
    v = (v or "").strip().upper()
    return bool(re.fullmatch(r"[A-Z0-9]{10}", v)) and bool(re.search(r"[A-Z]", v))


def _extract_reference(text: str) -> tuple[str, str]:
    """(reference, bank) pulled from a link / 127 SMS / bare code, or ('','') if the
    text isn't a valid receipt. Rejects phone numbers and 12-digit numbers."""
    t = (text or "").strip()
    if not t:
        return "", ""
    up = t.upper()
    # CBE: an app link, or a bare/embedded FT… reference (FT + digits, ~12 chars)
    if _CBE_HOST_RE.search(t) or re.fullmatch(r"FT[A-Z0-9]{6,}(?:-\d{6,})?", up):
        m = _CBE_REF_RE.search(up)
        if m:
            return m.group(0), "cbe"
    # Telebirr receipt link
    m = _TELEBIRR_LINK_RE.search(t)
    if m and _is_telebirr_ref(m.group(1)):
        return m.group(1).upper(), "telebirr"
    # 127 SMS: "transaction number is XXXXXXXXXX"
    m = _SMS_TXN_RE.search(t)
    if m and _is_telebirr_ref(m.group(1)):
        return m.group(1).upper(), "telebirr"
    # Bare Telebirr code (the whole message is the code)
    if _is_telebirr_ref(up):
        return up, "telebirr"
    # Last resort: an FT… (CBE) ref, else a 10-char Telebirr token (letter + digit)
    m = re.search(r"\bFT[A-Z0-9]{6,}\b", up)
    if m:
        return m.group(0), "cbe"
    for tok in re.findall(r"\b[A-Z0-9]{10}\b", up):
        if _is_telebirr_ref(tok):
            return tok, "telebirr"
    return "", ""


def _looks_like_receipt(text: str) -> bool:
    return bool(_extract_reference(text)[0])


async def _submit_receipt_text(m: Message, text: str) -> bool:
    """Extract a valid reference from a bare txn / link / 127 SMS, verify (with
    look-alike correction, like the screenshot path) and finalize. Returns False if no
    valid reference was found. Shared by the Add-Balance step and the anytime auto-detect."""
    ref, bank = _extract_reference(text)
    if not ref:
        return False
    wait = await m.answer(i18n.t("checking_payment"))
    if not await payment_verify.any_configured():
        v = {"ok": False}
    elif bank == "telebirr":
        # Correct ambiguous OCR/typo look-alikes (O↔0, I↔1, S↔5 …) and try each.
        v = await payment_verify.verify_candidates(payment_verify.telebirr_candidates(ref), 0)
    else:
        v = await payment_verify.verify(ref)
    await _finalize_receipt(m, wait, v.get("receipt_id") or ref, v)
    return True


@router.message(Flow.receipt, F.text)
async def on_receipt(m: Message, state: FSMContext):
    blk = await _maint_block_action(m.from_user.id)   # HIGH closes payments too
    if blk:
        await state.clear()
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    if not db_ready():   # payments need the DB — can't record money while it's down
        await state.clear()
        return await m.answer(i18n.t("payments_unavailable"), reply_markup=kb.main_kb(m.from_user.id))
    # RECEIPT FIRST — this is the Add-Payment step, and a Telebirr SMS embeds the payer's
    # phone in international format (251XXXXXXXXX = 12 digits) which would otherwise be
    # mistaken for a 12-digit FIN and start a download instead of crediting the payment.
    if await _submit_receipt_text(m, m.text):
        return await state.clear()
    # Not a receipt: a pasted FIN/FAN means they meant to DOWNLOAD → switch flows.
    fans, dropped = _parse_fans(m.text)
    if fans:
        blk = await _maint_block_download(m.from_user.id)
        await state.clear()
        if blk:
            return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
        if len(fans) > 1:
            await m.answer(i18n.t("n_ids", n=len(fans)))
        return await _run_download(m, state, fans, "pdf", m.from_user.id)   # default PDF, no prompt
    # Nothing readable — stay in the step so they can try again.
    await m.answer(i18n.t("send_txn_short"), reply_markup=kb.cancel_kb())


# ── add-balance via a Telebirr screenshot (OCR → look-alike correction → verify) ─
@router.message(F.photo)
async def on_payment_photo(m: Message, state: FSMContext):
    # A photo is normally a payment screenshot. The one exception is a Fayda (National ID) QR
    # screenshot taken from Telebirr, which starts a DOWNLOAD instead — so try the QR first
    # and fall through to the receipt path when it isn't one.
    #
    # Safe to try on every photo: the scanner only accepts the legacy Fayda QR (it
    # requires the :DLT: and :SIGN: markers), so a Telebirr receipt — QR on it or
    # not — never matches and still reaches the payment code below.
    in_receipt = (await state.get_state()) == Flow.receipt.state
    if not in_receipt and await _try_qr_download(m, state):
        return
    blk = await _maint_block_action(m.from_user.id)   # HIGH closes payments
    if blk:
        if in_receipt:
            await state.clear()
        return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
    if not db_ready():   # payments need the DB
        if in_receipt:
            await state.clear()
        return await m.answer(i18n.t("payments_unavailable"), reply_markup=kb.main_kb(m.from_user.id))
    try:
        u = await users_repo.ensure(m.from_user.id, m.from_user.username)
    except Exception:
        mark_db_down()
        return await m.answer(i18n.t("payments_unavailable"), reply_markup=kb.main_kb(m.from_user.id))
    if u["status"] == "blocked" and not config.is_admin(m.from_user.id):
        return
    try:
        bio = await m.bot.download(m.photo[-1].file_id)
        raw = bio.read()
    except Exception:
        return await m.answer(i18n.t("image_read_fail"), reply_markup=kb.main_kb(m.from_user.id))
    wait = await m.answer(i18n.t("reading_screenshot"))
    txn, amount, _is_receipt = await asyncio.to_thread(payment_verify.ocr_telebirr, raw)
    if not txn:
        # No readable transaction number → this is NOT a valid receipt. Do not create a
        # payment from an unreadable image; ask for the number (or a clearer photo).
        if not in_receipt:
            await state.clear()
        # In Server 5 a photo is just as likely to be a QR attempt that Telegram
        # compressed. Telling the user only "couldn't read the receipt" sends them
        # hunting for a transaction number they never had.
        if not in_receipt and await fayda.active_mode(m.bot.id) == "server5":
            return await wait.edit_text(
                i18n.t("couldnt_read_txn") + "\n\n" + i18n.t("qr_photo_compressed"))
        return await wait.edit_text(i18n.t("couldnt_read_txn"))
    await state.clear()
    await wait.edit_text(i18n.t("checking_payment"))
    if await payment_verify.any_configured():
        v = await payment_verify.verify_candidates(payment_verify.telebirr_candidates(txn), round((amount or 0) * 100))
    else:
        v = {"ok": False}
    # A real number was read → verify, else manual review. The admin also gets the
    # image in their Telegram DM for the manual case (not stored in the DB).
    await _finalize_receipt(m, wait, v.get("receipt_id") or txn, v, screenshot_file_id=m.photo[-1].file_id)


@router.message(F.document)
async def on_document(m: Message, state: FSMContext):
    """An image sent as a FILE. This is the reliable way to receive a Fayda QR:
    Telegram compresses photos, and the legacy QR is dense enough that compression
    destroys it — a 572x1280 re-encode does not decode at any scale."""
    doc = m.document
    mime = (getattr(doc, "mime_type", "") or "").lower()
    name = (getattr(doc, "file_name", "") or "").lower()
    is_image = mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))
    log.info("document from %s: mime=%r name=%r size=%s image=%s",
             m.from_user.id, mime, name, getattr(doc, "file_size", "?"), is_image)
    if not is_image:
        return
    if await _try_qr_download(m, state, file_id=doc.file_id):
        return
    # An image file that isn't a Fayda QR: say so plainly. It is not routed to the
    # payment path — receipts are sent as photos, and silently OCR-ing a file the
    # user meant as a QR would only produce a confusing receipt error.
    if await fayda.active_mode(m.bot.id) == "server5":
        await m.answer(i18n.t("qr_photo_compressed"))


async def _try_qr_download(m: Message, state: FSMContext, file_id: str | None = None) -> bool:
    """Is this photo a Fayda QR screenshot? If so, start a download from it.

    Returns True when handled. Anything else — a payment receipt, a blurry shot, a
    photo of a cat — returns False and the caller carries on to the receipt path,
    so this can never swallow a payment.

    The scanned QR is kept for the card: it carries the REAL signature, so the
    finished card verifies. Rebuilding one from the identity data cannot.
    """
    from ..fayda import cards
    # Server 5 ONLY. It is the one mode that draws the card itself, so it is the only
    # one a scanned QR can reach; the others get their images from upstream. Checked
    # first because it is the cheapest test — no file download, no subprocess.
    mode = await fayda.active_mode(m.bot.id)
    if mode != "server5":
        log.info("qr: skipped, mode=%s (bot %s)", mode, m.bot.id)
        return False
    # NOTE: scanning is pure Python now (no cards.available() gate) — it works even
    # where the Node card-drawing bridge is absent. Only card IMAGES need Node.
    try:
        bio = await m.bot.download(file_id or m.photo[-1].file_id)
        raw = bio.read()
    except Exception as e:
        log.warning("qr: could not download the image: %s", e)
        return False
    scan = await cards.scan(raw)
    log.info("qr: %s bytes, ok=%s fan_valid=%s signed=%s %s",
             len(raw), scan.get("ok"), scan.get("fan_valid"), scan.get("signed"),
             ("" if scan.get("ok") else "err=" + str(scan.get("error"))[:80]))
    if not scan.get("ok") or not scan.get("fan_valid"):
        return False                       # not a Fayda QR → let the receipt path have it
    fan = scan["fan"]
    blk = await _maint_block_download(m.from_user.id)
    if blk:
        await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
        return True
    if _should_skip(f"{m.from_user.id}:qr-scan:{fan}", 4.0):
        return True
    await m.answer(i18n.t("qr_read_ok", name=scan.get("full_name") or fan, fan=fan))
    # Pin the QR to THIS id so a later typed download can't inherit it.
    await state.update_data(qr_b64=base64.b64encode(scan["qr"]).decode() if scan.get("qr") else "",
                            qr_fan=fan, qr_signed=bool(scan.get("signed")))
    await _run_download(m, state, [fan], "pdf", m.from_user.id)
    return True


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
    if not await recheck_if_down():   # self-heals a stuck 'down' if the DB is actually reachable
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
    if fmt not in ("pdf", "screenshot"):   # 'Both' removed — one format per download
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
    fmt = data.get("dl_fmt", "pdf")
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
    # Auto-detect a payment receipt (Telebirr link / receipt number / 127 SMS) pasted
    # at any time — no need to tap Add Payment first (mirrors faydapdf-railway).
    # Receipt detection runs FIRST, even when digits look like an ID: a Telebirr SMS embeds
    # the payer's phone in international format (251XXXXXXXXX = 12 digits), which would
    # otherwise be mistaken for a 12-digit FIN and start a download instead of crediting the
    # payment. Safe both ways — a bare FIN/FAN is only digits, so it never looks like a receipt.
    if db_ready() and _looks_like_receipt(m.text):
        blk = await _maint_block_action(m.from_user.id)   # HIGH closes payments
        if blk:
            return await m.answer(blk, reply_markup=kb.main_kb(m.from_user.id))
        try:
            await users_repo.ensure(m.from_user.id, m.from_user.username)
        except Exception:
            mark_db_down()
            return await m.answer(i18n.t("payments_unavailable"), reply_markup=kb.main_kb(m.from_user.id))
        await state.clear()
        if await _submit_receipt_text(m, m.text):
            return
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
    # Default output is PDF → go straight to OTP, no PDF/Screenshot prompt. Users who want
    # a screenshot tap the "🖼 Get Screenshot" button first.
    if len(fans) > 1:
        await m.answer(i18n.t("n_ids", n=len(fans)))
    await _run_download(m, state, fans, "pdf", m.from_user.id)
