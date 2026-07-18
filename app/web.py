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

from . import config, fayda, notify
from .db import init_pool, close_pool, pool
from .repo import (
    users as users_repo,
    payments as payments_repo,
    settings as settings_repo,
    stats as stats_repo,
    wallet,
)
from .services import billing

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
        "owed_cents": u["owed_cents"],
        "credit_limit_cents": u["credit_limit_cents"],
        "price_override_cents": u["price_override_cents"],
        "is_vip": u["is_vip"],
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


@app.get("/api/stats", dependencies=[Depends(require_admin)])
async def api_stats():
    from .services import payment_verify
    d = await stats_repo.dashboard()
    d["mode"] = await fayda.active_mode()
    d["paused"] = await settings_repo.get_bool("paused", False)
    d["global_price_cents"] = await billing.global_price_cents()
    d["accounts"] = {
        b: dict(zip(("name", "account"), await payment_verify.receiver_for(b)))
        for b in ("telebirr", "cbe")
    }
    return d


@app.get("/api/users", dependencies=[Depends(require_admin)])
async def api_users(page: int = 1, q: str = "", status: str = "", vip: str = "", mode: str = ""):
    limit, page = 20, max(1, page)
    is_vip = True if vip == "1" else False if vip == "0" else None
    rows, total = await users_repo.page(
        status or None, q or None, limit, (page - 1) * limit,
        is_vip=is_vip, mode=(mode or None),
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
        await _notify(uid, f"💵 {billing.birr(cents)} added to your balance by the admin. New balance: {billing.birr(nb)}.")
    else:
        raise HTTPException(400, "unknown action")
    return _user_dto(await users_repo.get(uid))


@app.get("/api/payments", dependencies=[Depends(require_admin)])
async def api_payments():
    rows = await payments_repo.list_pending(limit=50)
    return {"payments": [
        {"id": p["id"], "user_id": p["user_id"], "receipt_id": p["receipt_id"],
         "bank": p["bank"], "amount_cents": p["amount_cents"], "created_at": p["created_at"].isoformat()}
        for p in rows
    ]}


@app.post("/api/payments/{pid}/approve", dependencies=[Depends(require_admin)])
async def api_approve(pid: int, request: Request):
    body = await request.json()
    cents = _cents(body.get("amount_birr"))
    res = await payments_repo.approve(pid, "web-admin", cents)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    await _notify(res["user_id"], f"✅ Your payment was approved. {billing.birr(res['amount_cents'])} added. New balance: {billing.birr(res['balance_cents'])}.")
    return res


@app.post("/api/payments/{pid}/reject", dependencies=[Depends(require_admin)])
async def api_reject(pid: int):
    p = await payments_repo.get(pid)
    res = await payments_repo.reject(pid, "web-admin", "rejected via web")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    if p:
        await _notify(p["user_id"], "🚫 Your payment was rejected. Please check the receipt and resubmit.")
    return res


@app.post("/api/settings", dependencies=[Depends(require_admin)])
async def api_settings(request: Request):
    body = await request.json()
    if "mode" in body:
        await fayda.set_mode(str(body["mode"]))
    if "paused" in body:
        await settings_repo.set_bool("paused", bool(body["paused"]))
    if "global_price_birr" in body:
        await settings_repo.set("global_price_cents", str(_cents(body["global_price_birr"]) or 0))
    # Admin-set merchant receiver (per bank) — used for auto-verify + shown to users.
    for bank in ("telebirr", "cbe"):
        if f"{bank}_name" in body:
            await settings_repo.set(f"pay_{bank}_name", str(body[f"{bank}_name"] or "").strip())
        if f"{bank}_account" in body:
            await settings_repo.set(f"pay_{bank}_account", str(body[f"{bank}_account"] or "").strip())
    return {"ok": True}


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")


if __name__ == "__main__":
    run()
