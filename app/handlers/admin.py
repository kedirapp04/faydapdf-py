"""Admin: pending-payment approve/reject (atomic), direct top-up, Fayda-mode
switch, pause. Registered BEFORE the user router so admin FSM states win."""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from .. import config, fayda, notify, i18n
from ..db import pool, db_ready, db_down_policy, set_db_down_policy
from ..repo import payments as payments_repo, users as users_repo, settings as settings_repo, wallet
from ..services import billing, maintenance

DBPOLICY_LABELS = {"refuse": "Refuse (block)", "free": "Serve free", "fallback": "Serve free (fallback)"}
DBPOLICY_CYCLE = {"refuse": "free", "free": "fallback", "fallback": "refuse"}


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        return default

router = Router()


class AdminFlow(StatesGroup):
    pay_amount = State()    # data: payment_id
    broadcast = State()     # awaiting broadcast text
    broadcast_go = State()  # data: text — awaiting confirm
    set_recv = State()      # data: bank — awaiting "Name | Account"
    maint_msg = State()     # awaiting the maintenance notice text


def _birr_to_cents(text: str) -> int | None:
    try:
        return round(float(text.replace(",", "").strip()) * 100)
    except ValueError:
        return None


def _parse_uid(text: str) -> int | None:
    t = (text or "").strip().lstrip("@")
    return int(t) if t.isdigit() else None


async def _panel_text() -> str:
    # Defensive reads so the panel still opens while the DB is down.
    pending = await _safe(payments_repo.count_pending(), "?")
    users = await _safe(users_repo.count(), "?")
    mode = await fayda.active_mode()
    paused = await _safe(settings_repo.get_bool("paused", False), False)
    mlvl = await _safe(maintenance.level(), "off")
    db_line = "🗄 DB: ✅ up" if db_ready() else f"🗄 DB: ⚠️ DOWN — policy: {DBPOLICY_LABELS[db_down_policy()]}"
    return (
        "🛠 Admin panel"
        + ("\n⏸ SERVICE PAUSED" if paused else "")
        + (f"\n🛠 MAINTENANCE: {maintenance.LABELS[mlvl]}" if mlvl != "off" else "")
        + f"\n👥 Users: {users}"
        + f"\n💳 Pending payments: {pending}"
        + f"\n🔀 Fayda mode: {mode}"
        + f"\n{db_line}"
    )


async def _panel_kb() -> InlineKeyboardMarkup:
    pending = await _safe(payments_repo.count_pending(), "?")
    mode = await fayda.active_mode()
    paused = await _safe(settings_repo.get_bool("paused", False), False)
    rows = [
        [InlineKeyboardButton(text=f"💳 Pending ({pending})", callback_data="pay_list:0")],
        [InlineKeyboardButton(text=f"🔀 Mode: {mode} — switch", callback_data="mode_toggle")],
    ]
    if mode == "server4":
        rows.append([InlineKeyboardButton(text="🎫 Tokens (pool health)", callback_data="tokens")])
    mlvl = await _safe(maintenance.level(), "off")
    from ..services import payment_verify
    appr = await _safe(payment_verify.approver(), "auto")
    rows += [
        [InlineKeyboardButton(text="💳 Payment accounts", callback_data="accounts")],
        [InlineKeyboardButton(text=f"🧾 Approver: {payment_verify.APPROVER_LABELS[appr]} — cycle", callback_data="approver_cycle")],
        [InlineKeyboardButton(text=f"🗄 DB-down: {DBPOLICY_LABELS[db_down_policy()]} — switch", callback_data="dbpolicy_toggle")],
        [InlineKeyboardButton(text=f"🛠 Maintenance: {maintenance.LABELS[mlvl]} — cycle", callback_data="maint_cycle")],
        [InlineKeyboardButton(text="✏️ Maintenance message", callback_data="maint_msg")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="bc_start")],
        [InlineKeyboardButton(text=("▶️ Resume service" if paused else "⏸ Pause service"), callback_data="pause_toggle")],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_panel(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    await m.answer(await _panel_text(), reply_markup=await _panel_kb())


@router.message(Command("admin"))
async def admin_cmd(m: Message):
    await show_panel(m)


@router.callback_query(F.data == "panel")
async def cb_panel(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    await c.answer()
    await c.message.edit_text(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "mode_toggle")
async def cb_mode(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    cur = await fayda.active_mode()
    new = await fayda.set_mode("server4" if cur == "api" else "api")
    await c.answer(f"Mode → {new}")
    await c.message.edit_text(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "pause_toggle")
async def cb_pause(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    now = not await settings_repo.get_bool("paused", False)
    await settings_repo.set_bool("paused", now)
    await c.answer("⏸ Paused" if now else "▶️ Resumed")
    await c.message.edit_text(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "dbpolicy_toggle")
async def cb_dbpolicy(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    new = DBPOLICY_CYCLE[db_down_policy()]
    await set_db_down_policy(new)
    await c.answer(f"DB-down → {DBPOLICY_LABELS[new]}")
    await c.message.edit_text(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "approver_cycle")
async def cb_approver_cycle(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    from ..services import payment_verify
    new = await payment_verify.set_approver(payment_verify.APPROVER_CYCLE[await payment_verify.approver()])
    await c.answer(f"Approver → {payment_verify.APPROVER_LABELS[new]}")
    await c.message.edit_text(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "maint_cycle")
async def cb_maint_cycle(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    new = await maintenance.set_level(maintenance.CYCLE[await maintenance.level()])
    await c.answer(f"Maintenance → {maintenance.LABELS[new]}")
    await c.message.edit_text(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "maint_msg")
async def cb_maint_msg(c: CallbackQuery, state: FSMContext):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    await state.set_state(AdminFlow.maint_msg)
    await c.answer()
    cur = await _safe(settings_repo.get("maintenance_message"), None)
    body = ("✏️ Send the maintenance notice users should see (any language — you can "
            "include both English and Amharic in one message).\n\n"
            "Send  -  to reset to the default bilingual notice, or /cancel to abort.")
    if cur:
        body = f"Current notice:\n\n{cur}\n\n" + body
    await c.message.answer(body)


@router.message(AdminFlow.maint_msg, F.text)
async def on_maint_msg(m: Message, state: FSMContext):
    raw = m.text.strip()
    if raw.lower() in ("/cancel", "cancel"):
        await state.clear()
        return await m.answer("✖️ Cancelled.")
    await state.clear()
    if raw == "-":
        await maintenance.set_message("")
        await m.answer("✅ Reset to the default bilingual notice.")
    else:
        await maintenance.set_message(raw)
        await m.answer("✅ Maintenance notice saved.")
    await m.answer(await _panel_text(), reply_markup=await _panel_kb())


@router.callback_query(F.data == "tokens")
async def cb_tokens(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    await c.answer("Checking pool…")
    configured = bool(config.SERVER4_TOKEN_API_URL and config.SERVER4_TOKEN_API_CSRF)
    lines = ["🎫 Server-4 token pool",
             f"Configured: {'yes' if configured else 'NO — set SERVER4_TOKEN_API_URL/CSRF'}"]
    st = await fayda.pool_status()
    if st.get("ok"):
        data = st.get("data") or {}
        if data:
            for k, v in list(data.items())[:12]:
                if isinstance(v, (str, int, float, bool)):
                    lines.append(f"• {k}: {v}")
        else:
            lines.append("• (endpoint reachable, no fields)")
    else:
        lines.append(f"⚠️ {st.get('error', 'unavailable')}")
        lines.append("(No stats endpoint? Set SERVER4_TOKEN_STATS_URL.)")
    back = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Panel", callback_data="panel")]])
    await c.message.edit_text("\n".join(lines), reply_markup=back)


# ── payment accounts (admin-set merchant receiver, per bank) ─────────────────
async def _accounts_text() -> str:
    from ..services import payment_verify
    lines = ["💳 Payment accounts (where users pay)", ""]
    for bank in ("telebirr", "cbe"):
        name, acct = await payment_verify.receiver_for(bank)
        label = payment_verify.BANK_LABELS[bank]
        if name or acct:
            lines.append(f"{label}: {name or '—'}  ·  {acct or '—'}")
        else:
            lines.append(f"{label}: (not set)")
    from ..services import payment_verify
    show_av = await _safe(payment_verify.show_autoverify(), False)
    lines.append("")
    lines.append(f"🔎 'Auto-verified' note in pay text: {'ON' if show_av else 'off'}")
    lines.append("")
    lines.append("These are shown to users and used to auto-verify that a receipt was paid to you.")
    return "\n".join(lines)


async def _accounts_kb() -> InlineKeyboardMarkup:
    from ..services import payment_verify
    show_av = await _safe(payment_verify.show_autoverify(), False)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Set Telebirr", callback_data="recv:telebirr"),
         InlineKeyboardButton(text="✏️ Set CBE", callback_data="recv:cbe")],
        [InlineKeyboardButton(text=f"🔎 Auto-verify note: {'ON' if show_av else 'off'} — toggle", callback_data="av_toggle")],
        [InlineKeyboardButton(text="⬅ Panel", callback_data="panel")],
    ])


@router.callback_query(F.data == "accounts")
async def cb_accounts(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    await c.answer()
    await c.message.edit_text(await _accounts_text(), reply_markup=await _accounts_kb())


@router.callback_query(F.data == "av_toggle")
async def cb_av_toggle(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    from ..services import payment_verify
    now = not await _safe(payment_verify.show_autoverify(), False)
    await settings_repo.set_bool("pay_show_autoverify", now)
    await c.answer("Auto-verify note ON" if now else "Auto-verify note off")
    await c.message.edit_text(await _accounts_text(), reply_markup=await _accounts_kb())


@router.callback_query(F.data.startswith("recv:"))
async def cb_recv(c: CallbackQuery, state: FSMContext):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    bank = c.data.split(":")[1]
    from ..services import payment_verify
    label = payment_verify.BANK_LABELS.get(bank, bank)
    await state.set_state(AdminFlow.set_recv)
    await state.update_data(bank=bank)
    await c.answer()
    await c.message.answer(
        f"✏️ Send the {label} receiver as:  Name | Account\n"
        f"e.g.  Abebe Kebede | 251912345678\n"
        f"(Send  -  to clear it.)"
    )


@router.message(AdminFlow.set_recv, F.text)
async def on_set_recv(m: Message, state: FSMContext):
    from ..services import payment_verify
    data = await state.get_data()
    bank = data.get("bank", "telebirr")
    raw = m.text.strip()
    if raw.lower() in ("/cancel", "cancel"):
        await state.clear()
        return await m.answer("✖️ Cancelled.")
    await state.clear()
    if raw == "-":
        name, acct = "", ""
    else:
        parts = [p.strip() for p in raw.split("|", 1)]
        name = parts[0]
        acct = parts[1] if len(parts) > 1 else ""
    await settings_repo.set(f"pay_{bank}_name", name)
    await settings_repo.set(f"pay_{bank}_account", acct)
    label = payment_verify.BANK_LABELS.get(bank, bank)
    await m.answer(f"✅ {label} receiver saved:\n{name or '—'}  ·  {acct or '—'}")
    await m.answer(await _accounts_text(), reply_markup=await _accounts_kb())


# ── pending payments ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("pay_list:"))
async def cb_pay_list(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    await c.answer()
    rows = await payments_repo.list_pending(limit=10)
    if not rows:
        return await c.message.edit_text("💳 No pending payments.", reply_markup=await _panel_kb())
    kb_rows = []
    for p in rows:
        kb_rows.append([
            InlineKeyboardButton(text=f"#{p['id']} · {p['receipt_id']} · {p['bank']}", callback_data=f"pay_open:{p['id']}"),
        ])
    kb_rows.append([InlineKeyboardButton(text="⬅ Panel", callback_data="panel")])
    await c.message.edit_text("💳 Pending payments — tap one:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("pay_open:"))
async def cb_pay_open(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    pid = int(c.data.split(":")[1])
    p = await payments_repo.get(pid)
    if not p:
        return await c.answer("Gone")
    await c.answer()
    await c.message.edit_text(
        f"💳 Payment #{p['id']}\nUser: {p['user_id']}\nReceipt: {p['receipt_id']}\nBank: {p['bank']}\nStatus: {p['status']}",
        reply_markup=_pay_open_kb(pid),
    )


def _pay_open_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"pay_ok:{pid}"),
         InlineKeyboardButton(text="🚫 Reject", callback_data=f"pay_no:{pid}")],
        [InlineKeyboardButton(text="🔎 Verify receipt", callback_data=f"pay_v:{pid}")],
        [InlineKeyboardButton(text="⬅ Back", callback_data="pay_list:0")],
    ])


@router.callback_query(F.data.startswith("pay_v:"))
async def cb_pay_verify(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    pid = int(c.data.split(":")[1])
    p = await payments_repo.get(pid)
    if not p:
        return await c.answer("Gone")
    from ..services import payment_verify
    if not await payment_verify.any_configured():
        return await c.answer("No auto-verifier configured", show_alert=True)
    await c.answer("Checking…")
    v = await payment_verify.verify(p["receipt_id"])
    head = f"💳 Payment #{p['id']}\nUser: {p['user_id']}\nReceipt: {p['receipt_id']}\nBank: {p['bank']}\nStatus: {p['status']}\n\n🔎 Auto-check:"
    if v.get("ok"):
        body = (f"\n✅ VERIFIED via {v.get('provider')}"
                f"\nAmount: {billing.birr(int(v.get('amount_cents') or 0))}"
                f"\nTo: {v.get('receiver_name') or '—'}  ·  {v.get('receiver_account') or '—'}"
                f"\n→ Tap Approve, then enter {billing.birr(int(v.get('amount_cents') or 0))}.")
    elif v.get("receiver_mismatch"):
        body = "\n⚠️ Paid to a DIFFERENT account than your configured receiver. Do NOT approve unless you're sure."
    elif v.get("already_used"):
        body = "\n⚠️ Provider says this receipt was ALREADY USED."
    else:
        body = f"\n❔ Not confirmed: {v.get('error', 'unknown')}. Verify manually before approving."
    await c.message.edit_text(head + body, reply_markup=_pay_open_kb(pid))


@router.callback_query(F.data.startswith("pay_ok:"))
async def cb_pay_ok(c: CallbackQuery, state: FSMContext):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    pid = int(c.data.split(":")[1])
    await state.set_state(AdminFlow.pay_amount)
    await state.update_data(payment_id=pid)
    await c.answer()
    await c.message.answer(f"✏️ Enter the amount to credit for payment #{pid} (in Birr):")


@router.message(AdminFlow.pay_amount, F.text)
async def on_pay_amount(m: Message, state: FSMContext):
    cents = _birr_to_cents(m.text)
    if cents is None or cents <= 0:
        return await m.answer("Send a positive amount in Birr, e.g. 50")
    data = await state.get_data()
    await state.clear()
    res = await payments_repo.approve(int(data["payment_id"]), m.from_user.id, cents)
    if not res.get("ok"):
        return await m.answer(f"⚠️ Couldn't approve: {res.get('error')}")
    await m.answer(f"✅ Approved. Credited {billing.birr(res['amount_cents'])}. New balance: {billing.birr(res['balance_cents'])}.")
    await notify.notify_user(res["user_id"], i18n.t("approved_notify", amount=billing.birr(res["amount_cents"]), balance=billing.birr(res["balance_cents"])))


@router.callback_query(F.data.startswith("pay_no:"))
async def cb_pay_no(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    pid = int(c.data.split(":")[1])
    p = await payments_repo.get(pid)
    res = await payments_repo.reject(pid, c.from_user.id, "rejected by admin")
    if not res.get("ok"):
        return await c.answer(res.get("error", "error"))
    await c.answer("Rejected")
    await c.message.edit_text(f"🚫 Payment #{pid} rejected.")
    if p:
        await notify.notify_user(p["user_id"], i18n.t("rejected_notify"))


# ── VIP + global price commands ─────────────────────────────────────────────
@router.message(Command("vip"))
async def vip_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Usage: /vip <user_id> [on|off]")
    target = _parse_uid(parts[1])
    if target is None:
        return await m.answer("User id must be a number, e.g. /vip 123456789 on")
    on = True if len(parts) < 3 else parts[2].lower() in ("on", "1", "true", "yes")
    await users_repo.ensure(target)
    await users_repo.set_vip(target, on)
    await m.answer(f"⭐ VIP for {target} → {'ON' if on else 'OFF'}")


@router.message(Command("vipprice"))
async def vipprice_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or _birr_to_cents(parts[1]) is None:
        return await m.answer("Usage: /vipprice <birr>")
    await settings_repo.set("vip_price_cents", str(_birr_to_cents(parts[1])))
    await m.answer(f"⭐ VIP price → {billing.birr(_birr_to_cents(parts[1]))}")


@router.message(Command("gprice"))
async def gprice_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or _birr_to_cents(parts[1]) is None:
        return await m.answer("Usage: /gprice <birr>")
    await settings_repo.set("global_price_cents", str(_birr_to_cents(parts[1])))
    await m.answer(f"🌐 Global price → {billing.birr(_birr_to_cents(parts[1]))}")


@router.message(Command("setprice"))     # alias of /gprice
async def setprice_cmd(m: Message):
    await gprice_cmd(m)


@router.message(Command("freemode"))
async def freemode_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off", "1", "0", "true", "false"):
        cur = await settings_repo.get_bool("free_mode", False)
        return await m.answer(f"Free mode is {'ON' if cur else 'off'}. Usage: /freemode on|off")
    on = parts[1].lower() in ("on", "1", "true")
    await settings_repo.set_bool("free_mode", on)
    await m.answer(f"🆓 Free mode → {'ON (all downloads free)' if on else 'off'}")


@router.message(Command("setpayment"))
async def setpayment_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or parts[2].lower() not in ("counter", "prepaid", "postpaid"):
        return await m.answer("Usage: /setpayment <user_id> counter|prepaid|postpaid")
    target = _parse_uid(parts[1])
    if target is None:
        return await m.answer("User id must be a number.")
    await users_repo.ensure(target)
    await users_repo.set_billing_mode(target, parts[2].lower())
    await m.answer(f"💳 {target} → {parts[2].lower()}")


@router.message(Command("recharge"))      # like /topup but allows a negative (deduct)
async def recharge_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3:
        return await m.answer("Usage: /recharge <user_id> <±birr>")
    target = _parse_uid(parts[1])
    cents = _birr_to_cents(parts[2])
    if target is None or cents is None or cents == 0:
        return await m.answer("Usage: /recharge <user_id> <±birr>  (e.g. /recharge 123 50 or /recharge 123 -20)")
    await users_repo.ensure(target)
    async with pool().acquire() as conn:
        async with conn.transaction():
            if cents > 0:
                nb = await wallet.credit(conn, int(target), cents, "adjust", ref_type="admin", ref_id=int(m.from_user.id))
            else:
                row = await conn.fetchrow("SELECT balance_cents FROM users WHERE telegram_id=$1 FOR UPDATE", int(target))
                take = min(row["balance_cents"], -cents)
                nb = row["balance_cents"]
                if take > 0:
                    nb = await wallet.debit(conn, int(target), take, "adjust", ref_type="admin", ref_id=int(m.from_user.id))
    await m.answer(f"{'💵 Credited' if cents>0 else '➖ Deducted'} {billing.birr(abs(cents))} · {target} balance: {billing.birr(nb)}")


@router.message(Command("setdiscount"))
async def setdiscount_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or _parse_uid(parts[1]) is None or _birr_to_cents(parts[2]) is None:
        return await m.answer("Usage: /setdiscount <user_id> <birr>")
    await pool().execute("UPDATE users SET discount_cents=$1, updated_at=now() WHERE telegram_id=$2",
                         max(0, _birr_to_cents(parts[2])), _parse_uid(parts[1]))
    await m.answer(f"🏷 Discount for {_parse_uid(parts[1])} → {billing.birr(_birr_to_cents(parts[2]))}")


@router.message(Command("setlimit"))
async def setlimit_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or _parse_uid(parts[1]) is None or _birr_to_cents(parts[2]) is None:
        return await m.answer("Usage: /setlimit <user_id> <birr>   (postpaid credit limit)")
    await users_repo.set_credit_limit(_parse_uid(parts[1]), _birr_to_cents(parts[2]))
    await m.answer(f"📉 Postpaid limit for {_parse_uid(parts[1])} → {billing.birr(_birr_to_cents(parts[2]))}")


@router.message(Command("downloadstats"))
async def downloadstats_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    total = await _safe(pool().fetchval("SELECT count(*)::int FROM downloads"), 0)
    today = await _safe(pool().fetchval("SELECT count(*)::int FROM downloads WHERE day=current_date"), 0)
    rows = await _safe(pool().fetch(
        "SELECT day::text d, count(*)::int n FROM downloads WHERE day > current_date - 7 GROUP BY day ORDER BY day DESC"), [])
    lines = [f"📥 Downloads — total {total}, today {today}", ""]
    lines += [f"{r['d']}: {r['n']}" for r in rows]
    await m.answer("\n".join(lines))


# ── broadcast ────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "bc_start")
async def cb_bc_start(c: CallbackQuery, state: FSMContext):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    await state.set_state(AdminFlow.broadcast)
    await c.answer()
    await c.message.answer("📢 Send the announcement text (plain text + emoji). /cancel to abort.")


@router.message(AdminFlow.broadcast, F.text)
async def bc_text(m: Message, state: FSMContext):
    if m.text.strip().lower() in ("/cancel", "cancel"):
        await state.clear()
        return await m.answer("✖️ Cancelled.")
    total = await pool().fetchval("SELECT count(DISTINCT telegram_id)::int FROM chats")
    await state.update_data(text=m.text)
    await state.set_state(AdminFlow.broadcast_go)
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Send to {total}", callback_data="bc_send"),
        InlineKeyboardButton(text="✖️ Cancel", callback_data="panel"),
    ]])
    await m.answer(f"📢 Preview:\n\n{m.text}\n\nSend to {total} user(s)?", reply_markup=ikb)


@router.callback_query(F.data == "bc_send", AdminFlow.broadcast_go)
async def cb_bc_send(c: CallbackQuery, state: FSMContext):
    if not config.is_admin(c.from_user.id):
        return await c.answer("Admins only")
    data = await state.get_data()
    await state.clear()
    text = data.get("text")
    if not text:
        return await c.answer("Draft expired")
    await c.answer("Broadcasting…")
    await c.message.edit_text("📢 Broadcasting… I'll report when done.")
    import asyncio
    # Send each recipient from ONE bot THEY started (multi-bot aware).
    # DISTINCT ON ensures a user doesn't get duplicate messages if they started multiple bots.
    rows = await pool().fetch("SELECT DISTINCT ON (telegram_id) telegram_id, bot_id FROM chats")
    sent = failed = 0
    for r in rows:
        if await notify.send(r["bot_id"], r["telegram_id"], text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.045)  # ~22/s, under Telegram's flood cap
    await c.message.answer(f"📢 Done.\n✅ Sent: {sent}\n⚠️ Failed: {failed}\n👥 Total: {len(rows)}")


# ── direct top-up: /topup <user_id> <birr> ──────────────────────────────────
@router.message(Command("topup"))
async def topup_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3:
        return await m.answer("Usage: /topup <user_id> <birr>")
    target = _parse_uid(parts[1])
    if target is None:
        return await m.answer("User id must be a number, e.g. /topup 123456789 50")
    cents = _birr_to_cents(parts[2])
    if cents is None or cents <= 0:
        return await m.answer("Amount must be a positive number.")
    await users_repo.ensure(target)
    async with pool().acquire() as conn:
        async with conn.transaction():
            new_balance = await wallet.credit(conn, int(target), cents, "adjust", ref_type="admin", ref_id=int(m.from_user.id))
    await m.answer(f"💵 Credited {billing.birr(cents)} to {target}. New balance: {billing.birr(new_balance)}.")
    await notify.notify_user(target, i18n.t("credited_notify", amount=billing.birr(cents), balance=billing.birr(new_balance)))


# ── personal bonus: /bonus <user_id> <birr> (credits balance + tracks bonus) ──
@router.message(Command("bonus"))
async def bonus_cmd(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3:
        return await m.answer("Usage: /bonus <user_id> <birr>")
    target = _parse_uid(parts[1])
    if target is None:
        return await m.answer("User id must be a number, e.g. /bonus 123456789 25")
    cents = _birr_to_cents(parts[2])
    if cents is None or cents <= 0:
        return await m.answer("Amount must be a positive number.")
    await users_repo.ensure(target)
    async with pool().acquire() as conn:
        async with conn.transaction():
            new_bonus = await wallet.credit_bonus(conn, int(target), cents)   # separate wallet, spent first
    await m.answer(f"🎁 Bonus {billing.birr(cents)} granted to {target}. Bonus wallet: {billing.birr(new_bonus)}.")
    await notify.notify_user(target, i18n.t("bonus_notify", amount=billing.birr(cents), bonus=billing.birr(new_bonus)))
