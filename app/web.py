"""Web admin dashboard (FastAPI) over the SAME Postgres as the bot.

Every money action goes through the same atomic repos (payments.approve,
wallet.credit) as the Telegram bot — so a web approve/top-up is exactly as
crash-safe and race-safe. Runs as its own process: `python -m app.web`.

Auth: a single admin password (ADMIN_WEB_PASSWORD) → stateless HMAC-signed cookie.
Live: the dashboard polls /api/stats + /api/payments every few seconds.
"""
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse

from . import config, fayda, notify, i18n
from .db import init_pool, close_pool, pool, db_ready, db_down_policy, set_db_down_policy
from .repo import (
    users as users_repo,
    payments as payments_repo,
    settings as settings_repo,
    stats as stats_repo,
    broadcast as broadcast_repo,
    wallet,
)
from .services import billing, maintenance

COOKIE = "fadmin"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="faydapdf-py admin", lifespan=lifespan)

_HTML = (Path(__file__).parent / "web_admin.html").read_text(encoding="utf-8")


# ── auth (stateless signed cookie) ──────────────────────────────────────────
def _sign(payload: str) -> str:
    return hmac.new(config.WEB_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_token() -> str:
    exp = str(int(time.time()) + 7 * 86400)
    return f"{exp}.{_sign(exp)}"


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def require_admin(request: Request):
    if not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="unauthorized")


async def _notify(chat_id, text: str) -> None:
    # Multi-bot aware: reaches the user via the bot they last used.
    await notify.notify_user(chat_id, text)


def _user_dto(u: dict) -> dict:
    return {
        "telegram_id": u["telegram_id"],
        "username": u["username"],
        "status": u["status"],
        "billing_mode": u["billing_mode"],
        "balance_cents": u["balance_cents"],
        "bonus_cents": u.get("bonus_cents", 0),               # lifetime granted (record)
        "bonus_balance_cents": u.get("bonus_balance_cents", 0),  # current spendable bonus wallet
        "owed_cents": u["owed_cents"],
        "credit_limit_cents": u["credit_limit_cents"],
        "price_override_cents": u["price_override_cents"],
        "discount_cents": u.get("discount_cents", 0),
        "is_vip": u["is_vip"],
        "role": u.get("role"),
        "tag": u.get("tag"),
        "allow_pdf": u.get("allow_pdf", True),
        "allow_screenshot": u.get("allow_screenshot", True),
        "daily_limit": u["daily_limit"],
        "total_limit": u["total_limit"],
        "created_at": u["created_at"].isoformat() if u.get("created_at") else None,
    }


def _cents(v) -> int | None:
    if v in (None, "", "clear"):
        return None
    return round(float(v) * 100)


# ── routes ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    pw = str(body.get("password") or "")
    if not config.ADMIN_WEB_PASSWORD or not hmac.compare_digest(pw, config.ADMIN_WEB_PASSWORD):
        raise HTTPException(status_code=401, detail="wrong password")
    response.set_cookie(COOKIE, _make_token(), httponly=True, samesite="lax", max_age=7 * 86400)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        return default


@app.get("/api/stats", dependencies=[Depends(require_admin)])
async def api_stats():
    from .services import payment_verify
    d = await _safe(stats_repo.dashboard(), {})
    d["mode"] = await fayda.active_mode()
    d["paused"] = await _safe(settings_repo.get_bool("paused", False), False)
    d["global_price_cents"] = await _safe(billing.global_price_cents(), 0)
    d["vip_price_cents"] = int(await _safe(settings_repo.get("vip_price_cents"), None) or 0)
    d["accounts"] = await _safe(_accounts_dto(), {})
    d["db_ready"] = db_ready()
    d["db_down_policy"] = db_down_policy()
    d["maintenance_level"] = await _safe(maintenance.level(), "off")
    d["maintenance_message"] = await _safe(settings_repo.get("maintenance_message"), "") or ""
    d["approver"] = await _safe(payment_verify.approver(), "auto")
    d["show_autoverify"] = await _safe(payment_verify.show_autoverify(), False)
    wb = await _safe(settings_repo.get("welcome_bonus_cents"), None)
    d["welcome_bonus_cents"] = int(wb) if wb is not None and str(wb).lstrip("-").isdigit() else users_repo.DEFAULT_WELCOME_BONUS_CENTS
    d["free_mode"] = await _safe(settings_repo.get_bool("free_mode", False), False)
    d["pdf_filename_suffix"] = await _safe(settings_repo.get("pdf_filename_suffix"), "") or ""
    d["telebirr_receivers"] = await _safe(payment_verify.telebirr_receivers(), [])
    d["s4_csrf_regular"] = await _safe(settings_repo.get("s4_csrf_regular"), "") or ""
    d["s4_csrf_vip"] = await _safe(settings_repo.get("s4_csrf_vip"), "") or ""
    d["s4_appcheck"] = await _safe(settings_repo.get("s4_appcheck"), "") or ""
    d["vp_base_url"] = await _safe(payment_verify.vp_base_url(), "") or ""
    d["vp_api_key"] = await _safe(payment_verify.vp_api_key(), "") or ""
    return d


async def _accounts_dto():
    from .services import payment_verify
    return {b: dict(zip(("name", "account"), await payment_verify.receiver_for(b)))
            for b in ("telebirr", "cbe")}


@app.get("/api/users", dependencies=[Depends(require_admin)])
async def api_users(page: int = 1, q: str = "", status: str = "", vip: str = "", mode: str = "", bonus: str = ""):
    limit, page = 20, max(1, page)
    is_vip = True if vip == "1" else False if vip == "0" else None
    rows, total = await users_repo.page(
        status or None, q or None, limit, (page - 1) * limit,
        is_vip=is_vip, mode=(mode or None), bonus=(bonus or None),
    )
    pages = max(1, -(-total // limit))
    return {"users": [_user_dto(u) for u in rows], "page": page, "pages": pages, "total": total}


@app.get("/api/users/{uid}", dependencies=[Depends(require_admin)])
async def api_user(uid: int):
    u = await users_repo.get(uid)
    if not u:
        raise HTTPException(404, "not found")
    dto = _user_dto(u)
    dto["usage"] = await users_repo.usage(uid)
    return dto


@app.post("/api/users/{uid}/action", dependencies=[Depends(require_admin)])
async def api_user_action(uid: int, request: Request):
    body = await request.json()
    action = str(body.get("action"))
    if not await users_repo.get(uid):
        raise HTTPException(404, "not found")
    if action == "block":
        await users_repo.set_status(uid, "blocked")
    elif action == "unblock":
        await users_repo.set_status(uid, "active")
    elif action == "mode":
        m = str(body.get("value"))
        if m in ("counter", "prepaid", "postpaid"):
            await users_repo.set_billing_mode(uid, m)
    elif action == "vip":
        await users_repo.set_vip(uid, bool(body.get("value")))
    elif action == "price":
        await users_repo.set_price_override(uid, _cents(body.get("value")))
    elif action == "credit":
        await users_repo.set_credit_limit(uid, _cents(body.get("value")) or 0)
    elif action == "topup":
        cents = _cents(body.get("value")) or 0
        if cents <= 0:
            raise HTTPException(400, "amount must be positive")
        async with pool().acquire() as conn:
            async with conn.transaction():
                nb = await wallet.credit(conn, uid, cents, "adjust", ref_type="admin", ref_id=0)
        await _notify(uid, i18n.t("credited_notify", amount=billing.birr(cents), balance=billing.birr(nb)))
    elif action == "deduct":
        cents = _cents(body.get("value")) or 0
        if cents <= 0:
            raise HTTPException(400, "amount must be positive")
        async with pool().acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT balance_cents FROM users WHERE telegram_id=$1 FOR UPDATE", uid)
                take = min(row["balance_cents"], cents)   # never below zero
                if take > 0:
                    await wallet.debit(conn, uid, take, "adjust", ref_type="admin", ref_id=0)
    elif action == "bonus":
        cents = _cents(body.get("value")) or 0
        if cents <= 0:
            raise HTTPException(400, "amount must be positive")
        async with pool().acquire() as conn:
            async with conn.transaction():
                nb = await wallet.credit_bonus(conn, uid, cents)   # separate wallet, spent first
        await _notify(uid, i18n.t("bonus_notify", amount=billing.birr(cents), bonus=billing.birr(nb)))
    elif action == "tag":
        await pool().execute("UPDATE users SET tag=$1, updated_at=now() WHERE telegram_id=$2", (str(body.get("value") or "").strip() or None), uid)
    elif action == "allow":
        which = "allow_pdf" if body.get("which") == "pdf" else "allow_screenshot"
        await pool().execute(f"UPDATE users SET {which}=$1, updated_at=now() WHERE telegram_id=$2", bool(body.get("value")), uid)
    elif action == "discount":
        await pool().execute("UPDATE users SET discount_cents=$1, updated_at=now() WHERE telegram_id=$2", _cents(body.get("value")) or 0, uid)
    elif action == "dm":
        msg = str(body.get("value") or "").strip()
        if not msg:
            raise HTTPException(400, "message required")
        res = await notify.notify_user_ex(uid, msg)
        if not res.get("ok"):
            if res.get("blocked"):
                raise HTTPException(400, "User has blocked the bot — can't DM them.")
            desc = (res.get("error") or "").strip()
            raise HTTPException(400, "Can't reach this user — they haven't started/messaged the bot yet."
                                + (f" ({desc})" if desc else ""))
    else:
        raise HTTPException(400, "unknown action")
    return _user_dto(await users_repo.get(uid))


def _pay_dto(p: dict) -> dict:
    return {"id": p["id"], "user_id": p["user_id"], "receipt_id": p["receipt_id"],
            "bank": p["bank"], "amount_cents": p["amount_cents"], "status": p["status"],
            "provider": p.get("provider"), "reason": p.get("reason"),
            "decided_by": p.get("decided_by"),
            "created_at": p["created_at"].isoformat() if p.get("created_at") else None,
            "decided_at": p["decided_at"].isoformat() if p.get("decided_at") else None}


@app.get("/api/payments", dependencies=[Depends(require_admin)])
async def api_payments(page: int = 1, q: str = "", status: str = ""):
    limit, page = 20, max(1, page)
    rows, total, counts = await payments_repo.page(status or None, q or None, limit, (page - 1) * limit)
    pages = max(1, -(-total // limit))
    return {"payments": [_pay_dto(p) for p in rows], "page": page, "pages": pages,
            "total": total, "counts": counts}


@app.get("/api/downloads", dependencies=[Depends(require_admin)])
async def api_downloads():
    import asyncio
    p = pool()
    # independent queries → fire concurrently (one round-trip's wall-time, not four)
    counts, daily, top = await asyncio.gather(
        _safe(p.fetchrow(
            "SELECT (SELECT count(*)::int FROM downloads) AS total, "
            "(SELECT count(*)::int FROM downloads WHERE day=current_date) AS today"), None),
        _safe(p.fetch(
            "SELECT day::text AS day, format, count(*)::int AS n FROM downloads "
            "WHERE day > current_date - 30 GROUP BY day, format ORDER BY day DESC"), []),
        _safe(p.fetch(
            "SELECT d.user_id, u.username, count(*)::int AS n FROM downloads d "
            "LEFT JOIN users u ON u.telegram_id = d.user_id "
            "GROUP BY d.user_id, u.username ORDER BY n DESC LIMIT 20"), []),
    )
    total = counts["total"] if counts else 0
    today = counts["today"] if counts else 0
    # fold per-(day,format) rows into one row per day with a format breakdown
    days: dict = {}
    for r in daily:
        d = days.setdefault(r["day"], {"day": r["day"], "pdf": 0, "screenshot": 0, "other": 0, "n": 0})
        fmt = r["format"] if r["format"] in ("pdf", "screenshot") else "other"
        d[fmt] += r["n"]; d["n"] += r["n"]
    return {"total": total, "today": today,
            "daily": sorted(days.values(), key=lambda x: x["day"], reverse=True),
            "top": [{"user_id": r["user_id"], "username": r["username"], "n": r["n"]} for r in top]}


@app.get("/api/tracked", dependencies=[Depends(require_admin)])
async def api_tracked():
    """Money-tracking totals (mirrors faydapdf-railway's Payment Approvals summary).
      recharge = approved top-ups + granted bonuses;  net used = recharge - current balances."""
    row = await _safe(pool().fetchrow("""
        SELECT (SELECT COALESCE(sum(amount_cents),0)::bigint FROM payments WHERE status='approved') AS approved,
               (SELECT COALESCE(sum(bonus_cents),0)::bigint FROM users)          AS bonuses,
               (SELECT COALESCE(sum(bonus_balance_cents),0)::bigint FROM users)  AS bonus_wallet,
               (SELECT COALESCE(sum(balance_cents),0)::bigint FROM users)        AS balances,
               (SELECT COALESCE(sum(owed_cents),0)::bigint FROM users)           AS owed
    """), None)
    approved = row["approved"] if row else 0
    bonuses = row["bonuses"] if row else 0
    bonus_wallet = row["bonus_wallet"] if row else 0
    balances = row["balances"] if row else 0
    owed = row["owed"] if row else 0
    recharge = approved + bonuses
    return {
        "approved_topups_cents": approved,
        "tracked_bonuses_cents": bonuses,               # lifetime bonus granted (record)
        "current_bonus_wallet_cents": bonus_wallet,     # bonus still spendable (separate wallet)
        "tracked_recharge_cents": recharge,
        "current_balances_cents": balances,
        "net_used_cents": recharge - balances,
        "balance_wo_bonuses_cents": balances - bonuses,
        "owed_cents": owed,
        "accounts": await _safe(_accounts_dto(), {}),
    }


@app.get("/api/users/{uid}/history", dependencies=[Depends(require_admin)])
async def api_user_history(uid: int):
    """Full per-user history for the History modal: profile + receipts + ledger + downloads."""
    u = await users_repo.get(uid)
    if not u:
        raise HTTPException(404, "user not found")
    p = pool()
    pays = await _safe(p.fetch(
        "SELECT * FROM payments WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50", uid), [])
    ledger = await _safe(p.fetch(
        "SELECT kind, amount_cents, balance_after_cents, reason, ref_type, created_at "
        "FROM wallet_ledger WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50", uid), [])
    dl_total = await _safe(p.fetchval("SELECT count(*)::int FROM downloads WHERE user_id=$1", uid), 0)
    dl_today = await _safe(p.fetchval(
        "SELECT count(*)::int FROM downloads WHERE user_id=$1 AND day=current_date", uid), 0)
    dl_recent = await _safe(p.fetch(
        "SELECT format, cost_cents, day::text AS day, created_at FROM downloads "
        "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20", uid), [])
    return {
        "user": _user_dto(u),
        "payments": [_pay_dto(r) for r in pays],
        "ledger": [{"kind": r["kind"], "amount_cents": r["amount_cents"],
                    "balance_after_cents": r["balance_after_cents"], "reason": r["reason"],
                    "ref_type": r["ref_type"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in ledger],
        "downloads": {"total": dl_total, "today": dl_today,
                      "recent": [{"format": r["format"], "cost_cents": r["cost_cents"],
                                  "day": r["day"],
                                  "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                                 for r in dl_recent]},
    }


# ── broadcast (persistent campaigns + rich filters) ──────────────────────────
def _bcast_where(segment: str) -> str:
    """Simple named segment → a fixed (injection-safe) WHERE clause. Also used by the
    bulk-bonus grant."""
    if segment == "active":
        return "status = 'active'"
    if segment == "blocked":
        return "status = 'blocked'"
    if segment == "vip":
        return "is_vip = true AND status <> 'blocked'"
    if segment in ("counter", "prepaid", "postpaid"):
        return f"billing_mode = '{segment}' AND status <> 'blocked'"
    if segment == "with_balance":
        return "balance_cents > 0 AND status <> 'blocked'"
    if segment == "charged":   # has at least one approved top-up
        return ("EXISTS (SELECT 1 FROM payments p WHERE p.user_id = users.telegram_id "
                "AND p.status = 'approved') AND status <> 'blocked'")
    return "status <> 'blocked'"   # 'all' (non-blocked)


def _bcast_filter(segment: str, extra: dict) -> tuple[str, list]:
    """Combine a named segment with advanced, parameterised filters (tag include /
    exclude, min/max balance, role). Everything user-supplied is bound as a parameter
    so a tag like o'brien can't inject SQL."""
    where = [f"({_bcast_where(segment)})"]
    args: list = []
    tag = str(extra.get("tag") or "").strip()
    if tag:
        args.append(tag); where.append(f"tag = ${len(args)}")
    ex = str(extra.get("exclude_tag") or "").strip()
    if ex:
        args.append(ex); where.append(f"(tag IS NULL OR tag <> ${len(args)})")
    role = str(extra.get("role") or "").strip()
    if role:
        args.append(role); where.append(f"role = ${len(args)}")
    minb, maxb = extra.get("min_balance_birr"), extra.get("max_balance_birr")
    if minb not in (None, ""):
        args.append(round(float(minb) * 100)); where.append(f"balance_cents >= ${len(args)}")
    if maxb not in (None, ""):
        args.append(round(float(maxb) * 100)); where.append(f"balance_cents <= ${len(args)}")
    return " AND ".join(where), args


def _extra_from(src) -> dict:
    return {"tag": (src.get("tag") if hasattr(src, "get") else None),
            "exclude_tag": src.get("exclude_tag"), "role": src.get("role"),
            "min_balance_birr": src.get("min_balance"), "max_balance_birr": src.get("max_balance")}


@app.get("/api/broadcast/count", dependencies=[Depends(require_admin)])
async def api_broadcast_count(segment: str = "all", tag: str = "", exclude_tag: str = "",
                              role: str = "", min_balance: str = "", max_balance: str = "",
                              user_id: str = ""):
    if user_id.strip().isdigit():   # single-user target (a DM) — count is 0 or 1
        return {"count": await _safe(pool().fetchval(
            "SELECT count(*)::int FROM users WHERE telegram_id=$1", int(user_id.strip())), 0)}
    where, args = _bcast_filter(segment, {
        "tag": tag, "exclude_tag": exclude_tag, "role": role,
        "min_balance_birr": min_balance or None, "max_balance_birr": max_balance or None})
    return {"count": await _safe(pool().fetchval(
        f"SELECT count(*)::int FROM users WHERE ({where}) AND COALESCE(is_blocked,false)=false", *args), 0)}


@app.post("/api/broadcast", dependencies=[Depends(require_admin)])
async def api_broadcast(request: Request):
    """Create a persistent campaign: snapshot recipients now, the delivery worker
    sends them. A `user_id` targets exactly one user (a DM via the composer); otherwise
    the segment/filters select the audience and bot-blocked users are excluded."""
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    parse_mode = body.get("parse_mode") if body.get("parse_mode") in ("HTML", "Markdown", "MarkdownV2") else None
    buttons = [{"text": str(b.get("text") or "").strip(), "url": str(b.get("url") or "").strip()}
               for b in (body.get("buttons") or []) if b.get("text") and b.get("url")][:6]
    title = str(body.get("title") or "").strip() or None
    uid_raw = str(body.get("user_id") or "").strip()
    if uid_raw.isdigit():           # single-user DM (explicit — don't exclude blocked)
        segment, extra = f"user #{uid_raw}", {}
        rows = await pool().fetch(
            "SELECT telegram_id, last_bot_id FROM users WHERE telegram_id=$1", int(uid_raw))
        if not rows:
            raise HTTPException(404, "user not found")
    else:
        segment = str(body.get("segment") or "all")
        extra = _extra_from(body)
        where, args = _bcast_filter(segment, extra)
        rows = await pool().fetch(
            f"SELECT telegram_id, last_bot_id FROM users WHERE ({where}) AND COALESCE(is_blocked,false)=false", *args)
    cid = await broadcast_repo.create(title, segment, extra, text, parse_mode, buttons)
    n = await broadcast_repo.snapshot(cid, [(r["telegram_id"], r["last_bot_id"]) for r in rows])
    return {"ok": True, "campaign_id": cid, "total": n}


@app.get("/api/broadcast/campaigns", dependencies=[Depends(require_admin)])
async def api_broadcast_campaigns():
    camps = await _safe(broadcast_repo.list_campaigns(50), [])
    return {"campaigns": [broadcast_repo._dto(c) for c in camps]}


@app.get("/api/broadcast/blocked", dependencies=[Depends(require_admin)])
async def api_broadcast_blocked():
    return await _safe(broadcast_repo.blocked_summary(), {"blocked": 0, "reactivated_30d": 0})


@app.post("/api/broadcast/{cid}/control", dependencies=[Depends(require_admin)])
async def api_broadcast_control(cid: int, request: Request):
    body = await request.json()
    action = str(body.get("action") or "")
    if action not in ("pause", "resume", "cancel", "delete"):
        raise HTTPException(400, "action must be pause|resume|cancel|delete")
    res = await broadcast_repo.control(cid, action)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@app.get("/api/broadcast/{cid}/report", dependencies=[Depends(require_admin)])
async def api_broadcast_report(cid: int):
    r = await broadcast_repo.report(cid)
    if not r:
        raise HTTPException(404, "not found")
    return r


@app.get("/api/broadcast/{cid}/recipients.csv", dependencies=[Depends(require_admin)])
async def api_broadcast_csv(cid: int):
    from fastapi.responses import PlainTextResponse
    rows = await pool().fetch(
        "SELECT r.user_id, u.username, r.status, r.error, r.tried_at, r.retries "
        "FROM broadcast_recipients r LEFT JOIN users u ON u.telegram_id=r.user_id "
        "WHERE r.campaign_id=$1 ORDER BY r.id LIMIT 200000", cid)

    def esc(v):
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"' if any(c in s for c in ',"\n') else s

    lines = ["user_id,username,status,error,tried_at,retries"]
    for r in rows:
        lines.append(",".join(esc(x) for x in [
            r["user_id"], r["username"], r["status"], r["error"],
            r["tried_at"].isoformat() if r["tried_at"] else "", r["retries"]]))
    return PlainTextResponse("\r\n".join(lines), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="campaign_{cid}.csv"'})


@app.post("/api/payments/{pid}/approve", dependencies=[Depends(require_admin)])
async def api_approve(pid: int, request: Request):
    body = await request.json()
    cents = _cents(body.get("amount_birr"))
    res = await payments_repo.approve(pid, "web-admin", cents)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    await _notify(res["user_id"], i18n.t("approved_notify", amount=billing.birr(res["amount_cents"]), balance=billing.birr(res["balance_cents"])))
    return res


@app.post("/api/payments/{pid}/reject", dependencies=[Depends(require_admin)])
async def api_reject(pid: int):
    p = await payments_repo.get(pid)
    res = await payments_repo.reject(pid, "web-admin", "rejected via web")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    if p:
        await _notify(p["user_id"], i18n.t("rejected_notify"))
    return res


@app.post("/api/payments/{pid}/reverify", dependencies=[Depends(require_admin)])
async def api_reverify(pid: int):
    """Re-run the auto-verifier on a pending receipt. Returns the verdict + amount so
    the admin can approve with confidence (money isn't moved here)."""
    from .services import payment_verify
    p = await payments_repo.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    if not await payment_verify.any_configured():
        return {"ok": False, "error": "no auto-verifier configured"}
    v = await payment_verify.verify(p["receipt_id"])
    return {
        "ok": bool(v.get("ok")),
        "provider": v.get("provider"),
        "amount_cents": int(v.get("amount_cents") or 0),
        "receiver_name": v.get("receiver_name"),
        "receiver_account": v.get("receiver_account"),
        "receiver_mismatch": bool(v.get("receiver_mismatch")),
        "already_used": bool(v.get("already_used")),
        "manual": bool(v.get("manual")),
        "error": v.get("error"),
    }


@app.post("/api/payments/bulk_approve", dependencies=[Depends(require_admin)])
async def api_bulk_approve(request: Request):
    """Auto-verify + approve many pending receipts at once. For each: re-verify to get
    a trusted amount (receiver-match + not-used are enforced inside verify()), then
    approve. Receipts that don't verify to a positive amount are skipped, never guessed."""
    from .services import payment_verify
    body = await request.json()
    ids = [int(x) for x in (body.get("ids") or [])][:200]
    approved, skipped = [], []
    configured = await payment_verify.any_configured()
    for pid in ids:
        p = await payments_repo.get(pid)
        if not p or p["status"] != "pending":
            skipped.append({"id": pid, "reason": "not pending"}); continue
        amt = int(p.get("amount_cents") or 0)
        if amt <= 0 and configured:
            v = await payment_verify.verify(p["receipt_id"])
            if v.get("ok") and int(v.get("amount_cents") or 0) > 0:
                amt = int(v["amount_cents"])
        if amt <= 0:
            skipped.append({"id": pid, "reason": "no verified amount"}); continue
        res = await payments_repo.approve(pid, "web-bulk", amt)
        if res.get("ok"):
            approved.append({"id": pid, "amount_cents": amt})
            await _notify(res["user_id"], i18n.t("approved_notify",
                          amount=billing.birr(res["amount_cents"]), balance=billing.birr(res["balance_cents"])))
        else:
            skipped.append({"id": pid, "reason": res.get("error")})
    return {"approved": approved, "skipped": skipped}


@app.post("/api/settings", dependencies=[Depends(require_admin)])
async def api_settings(request: Request):
    body = await request.json()
    if "mode" in body:
        await fayda.set_mode(str(body["mode"]))
    if "paused" in body:
        await settings_repo.set_bool("paused", bool(body["paused"]))
    if "global_price_birr" in body:
        await settings_repo.set("global_price_cents", str(_cents(body["global_price_birr"]) or 0))
    if "vip_price_birr" in body:
        await settings_repo.set("vip_price_cents", str(_cents(body["vip_price_birr"]) or 0))
    if "db_down_policy" in body:
        await set_db_down_policy(str(body["db_down_policy"]))
    if "maintenance_level" in body:
        await maintenance.set_level(str(body["maintenance_level"]))
    if "maintenance_message" in body:
        await maintenance.set_message(str(body["maintenance_message"] or ""))
    if "approver" in body:
        from .services import payment_verify
        await payment_verify.set_approver(str(body["approver"]))
    if "show_autoverify" in body:
        await settings_repo.set_bool("pay_show_autoverify", bool(body["show_autoverify"]))
    if "welcome_bonus_birr" in body:
        await settings_repo.set("welcome_bonus_cents", str(_cents(body["welcome_bonus_birr"]) or 0))
    if "free_mode" in body:
        await settings_repo.set_bool("free_mode", bool(body["free_mode"]))
    if "pdf_filename_suffix" in body:
        await settings_repo.set("pdf_filename_suffix", str(body["pdf_filename_suffix"] or "").strip())
    for _k in ("s4_csrf_regular", "s4_csrf_vip", "s4_appcheck", "vp_base_url", "vp_api_key"):
        if _k in body:
            await settings_repo.set(_k, str(body[_k] or "").strip())
    if "telebirr_list" in body:
        import json as _json
        clean = [{"name": str(r.get("name", "")).strip(), "account": str(r.get("account", "")).strip(),
                  "show": bool(r.get("show", True)), "verify": bool(r.get("verify", True))}
                 for r in (body.get("telebirr_list") or []) if (r.get("name") or r.get("account"))]
        await settings_repo.set("pay_telebirr_list", _json.dumps(clean))
    # Admin-set merchant receiver (per bank) — used for auto-verify + shown to users.
    for bank in ("telebirr", "cbe"):
        if f"{bank}_name" in body:
            await settings_repo.set(f"pay_{bank}_name", str(body[f"{bank}_name"] or "").strip())
        if f"{bank}_account" in body:
            await settings_repo.set(f"pay_{bank}_account", str(body[f"{bank}_account"] or "").strip())
    return {"ok": True}


@app.post("/api/bonus/bulk", dependencies=[Depends(require_admin)])
async def api_bonus_bulk(request: Request):
    """Grant a bonus to a whole segment at once. Adds to each user's separate BONUS
    WALLET (bonus_balance_cents, spent before normal balance) AND the lifetime
    bonus_cents record, writing one ledger row per user — all in a single atomic
    statement so a 15k-user grant can't half-apply. No per-user DM (would be a mass
    blast); the admin can broadcast separately."""
    body = await request.json()
    cents = _cents(body.get("amount_birr")) or 0
    if cents <= 0:
        raise HTTPException(400, "amount must be positive")
    segment = str(body.get("segment") or "all")
    where = _bcast_where(segment)   # fixed, injection-safe (maps to a known clause)
    async with pool().acquire() as conn:
        async with conn.transaction():
            n = await conn.fetchval(f"""
                WITH upd AS (
                    UPDATE users SET bonus_balance_cents = bonus_balance_cents + $1,
                                     bonus_cents         = bonus_cents + $1,
                                     updated_at          = now()
                    WHERE {where}
                    RETURNING telegram_id, balance_cents
                ), ins AS (
                    INSERT INTO wallet_ledger (user_id, kind, amount_cents,
                                               balance_after_cents, reason, ref_type, ref_id)
                    SELECT telegram_id, 'credit', $1, balance_cents, 'bonus', 'bonus', 0 FROM upd
                    RETURNING 1
                )
                SELECT count(*)::int FROM upd
            """, cents)
    return {"ok": True, "count": int(n or 0), "amount_cents": cents}


@app.get("/api/topups/bonus_threshold/count", dependencies=[Depends(require_admin)])
async def api_bonus_threshold_count(threshold_birr: str = "0"):
    tc = _cents(threshold_birr) or 0
    n = await _safe(pool().fetchval(
        "SELECT count(*)::int FROM users u WHERE u.status <> 'blocked' AND "
        "(SELECT COALESCE(sum(amount_cents),0) FROM payments p WHERE p.user_id=u.telegram_id AND p.status='approved') >= $1",
        tc), 0)
    return {"count": n, "threshold_cents": tc}


@app.post("/api/topups/bonus_threshold", dependencies=[Depends(require_admin)])
async def api_bonus_threshold(request: Request):
    """Bonus grant to users whose total APPROVED top-ups reach a threshold (ports
    faydapdf-railway's bonus-topup). Credits the separate bonus wallet, atomically."""
    body = await request.json()
    bonus_cents = _cents(body.get("amount_birr")) or 0
    threshold_cents = _cents(body.get("threshold_birr")) or 0
    if bonus_cents <= 0:
        raise HTTPException(400, "bonus amount must be positive")
    async with pool().acquire() as conn:
        async with conn.transaction():
            n = await conn.fetchval("""
                WITH elig AS (
                    SELECT u.telegram_id FROM users u
                    WHERE u.status <> 'blocked'
                      AND (SELECT COALESCE(sum(amount_cents),0) FROM payments p
                           WHERE p.user_id=u.telegram_id AND p.status='approved') >= $2
                ), upd AS (
                    UPDATE users SET bonus_balance_cents=bonus_balance_cents+$1,
                                     bonus_cents=bonus_cents+$1, updated_at=now()
                    WHERE telegram_id IN (SELECT telegram_id FROM elig)
                    RETURNING telegram_id, balance_cents
                ), ins AS (
                    INSERT INTO wallet_ledger (user_id, kind, amount_cents, balance_after_cents, reason, ref_type, ref_id)
                    SELECT telegram_id, 'credit', $1, balance_cents, 'bonus', 'bonus', 0 FROM upd RETURNING 1
                )
                SELECT count(*)::int FROM upd
            """, bonus_cents, threshold_cents)
    return {"ok": True, "count": int(n or 0)}


@app.post("/api/billing/bulk_mode", dependencies=[Depends(require_admin)])
async def api_bulk_mode(request: Request):
    """Bulk-set billing mode across a segment. Postpaid also sets the credit limit."""
    body = await request.json()
    mode = str(body.get("mode") or "")
    if mode not in ("counter", "prepaid", "postpaid"):
        raise HTTPException(400, "mode must be counter|prepaid|postpaid")
    where = _bcast_where(str(body.get("segment") or "all"))
    if mode == "postpaid":
        limit_cents = _cents(body.get("limit_birr")) or 0
        res = await pool().execute(
            f"UPDATE users SET billing_mode='postpaid', credit_limit_cents=$1, updated_at=now() WHERE {where}",
            limit_cents)
    else:
        res = await pool().execute(
            f"UPDATE users SET billing_mode=$1, updated_at=now() WHERE {where}", mode)
    n = int(res.split()[-1]) if res and res.split()[-1].isdigit() else 0
    return {"ok": True, "count": n}


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")


if __name__ == "__main__":
    run()
