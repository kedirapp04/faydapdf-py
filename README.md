# faydapdf-py

A fast, concurrent **Python** (aiogram 3 + PostgreSQL) rewrite of the Fayda PDF
bot — built around a **correct, transactional money model** so the old payment
bugs (balance added but receipt still pending, double-credits, receipts removed
inconsistently) can't happen.

## Why the money is now correct

- **Everything in cents** (integers) — no float rounding.
- **Append-only `wallet_ledger`** — every credit/debit is a row with the resulting
  balance; `users.balance_cents` is just a cache kept in sync in the same tx.
- **Atomic approve** — approving a payment marks it `approved` **and** credits the
  balance **and** writes the ledger in **one transaction**, guarded by
  `SELECT … FOR UPDATE` + `WHERE status='pending'`. So you can never get a partial
  state or a double-approve race.
- **Idempotent receipts** — `payments.receipt_id` is `UNIQUE`; a transaction can be
  submitted once, so it can never double-credit.
- **Async everywhere** — one process serves many users concurrently via an asyncpg
  pool; no blocking, no lateness on top-up / approve / deduct.

## Multi-bot (3+ bots, one shared DB)

Run several bots to spread load and grow past one bot's limits. Because all state
is in Postgres and money is DB-atomic, this is **race-free**: a user is the *same
account* on every bot, and a payment approving on bot B can't corrupt a balance
being read on bot A.

- Set **`BOT_TOKENS`** = all bot tokens (comma-separated). One process polls them
  all (aiogram multi-bot).
- To spread load across **processes/machines**, run several copies and set
  **`POLL_ONLY`** per copy to the bot ids it should poll — every copy keeps the
  full registry, so notifications/broadcast reach a user via **the bot they
  actually started** (tracked as `users.last_bot_id` + `chats(telegram_id, bot_id)`).

## Fayda flow — admin-selectable

The admin switches the source at runtime (Admin panel → *Mode*):
- **`api`** — calls your existing **fayda-railway** HTTP API (working now).
- **`server4`** — native Server-4 flow in Python: pulls a fresh single-use App Check
  token from the ntknpro pool per download, runs the full eSignet chain
  (authorize → oauth-details → send-OTP → authenticate → auth-code → callback) and
  renders the returned data to a PDF in-process (reportlab). No fayda-railway needed.

## Layout
```
schema.sql                 # Postgres schema (idempotent, runs on boot)
app/
  config.py  db.py         # env + asyncpg pool + migrations
  repo/  users wallet payments settings stats   # data layer (wallet/payments = atomic money)
  services/billing.py      # price + pre-flight gate + atomic per-download charge
  fayda/  api_provider  server4_provider        # the two selectable modes
  handlers/ user.py admin.py keyboards.py       # aiogram routers (the bot)
  web.py  web_admin.html                        # FastAPI web admin dashboard
  main.py                  # bot entrypoint
```

## Web admin dashboard

A **live** web dashboard over the **same Postgres**, reusing the same atomic repos
(so a web approve/top-up is exactly as safe as the bot's). Runs as its own process:

```bash
python -m app.web        # serves on WEB_HOST:WEB_PORT (default 0.0.0.0:8080)
```

- **Login** with `ADMIN_WEB_PASSWORD` → signed cookie (put nginx + HTTPS in front).
- **Live stat cards** (users / active / blocked / VIP / pending / downloads / balances)
  auto-refresh every 5 s.
- **Pending payments** — approve (set amount) / reject → the same atomic
  `payments.approve()`; the user is notified on Telegram.
- **Users** — search, filter, paginate; per-user block / VIP / mode / **add balance** /
  price / postpaid-limit.
- **Settings** — Fayda mode toggle, pause, global price, **payment accounts**
  (Telebirr + CBE receiver name/account, admin-set — shown to users & used to verify).

Run the bot and the web admin as two services (systemd/pm2); they share the DB.

## Deploy on Railway (bot + web admin in one service)

Railway runs one process per service, so this repo ships a **combined entrypoint**
`app/run.py` that runs the bot polling **and** the web admin together on one asyncio
loop and one DB pool. The web admin binds Railway's injected `$PORT` and gets the
service's public HTTPS URL; you don't run two services or manage a port.

`railway.json` already sets the start command to `python -m app.run` (there's also a
`Procfile` for portability, and `.python-version` pins Python 3.12). Steps:

1. **Push this repo to GitHub** (Railway deploys from git — no `.env` is committed,
   it's git-ignored). Then in Railway: **New Project → Deploy from GitHub repo →**
   pick this repo. Nixpacks auto-detects Python and installs `requirements.txt`.

2. **Add the database:** in the same project, **New → Database → Add PostgreSQL**.
   Railway provisions it on the project's **private network** (no egress fees, low
   latency) and exposes `DATABASE_URL` on the Postgres service.

3. **Wire the app to the DB + set env vars.** Open the **app service → Variables**
   and add (use a *reference* for the DB so it uses the private URL):
   ```
   DATABASE_URL = ${{Postgres.DATABASE_URL}}
   BOT_TOKENS   = 111:AAA,222:BBB,333:CCC     # one or many, comma-separated
   ADMIN_IDS    = 123456789
   ADMIN_WEB_PASSWORD = <a strong password>
   WEB_SECRET   = <long random string>
   FAYDA_MODE_DEFAULT = api                    # or server4
   # API mode:      FAYDA_API_URL, FAYDA_API_KEY
   # server4 mode:  SERVER4_TOKEN_API_URL, SERVER4_TOKEN_API_CSRF
   # optional auto-verify: VERIFYPAYMENT_API_KEY / LEUL_VERIFY_API_KEY / RELAY_*
   ```
   **Don't set `WEB_PORT`** — leave it unset so the app binds Railway's `$PORT`.

4. **Expose the web admin:** app service → **Settings → Networking → Generate
   Domain**. That URL is the dashboard; log in with `ADMIN_WEB_PASSWORD`.

5. **Deploy.** On boot the schema **auto-creates** (idempotent `schema.sql`), so
   there's no manual migration. Redeploys keep all data — it lives in Railway
   Postgres, not the container.

### Keeping the bot always active

The service is built to stay up 24/7:
- **Self-healing process** — `app/run.py` supervises the bot and the web admin
  separately; if either throws, it's restarted with backoff and the other keeps
  running. `railway.json` sets `restartPolicyType: ALWAYS` and `sleepApplication:
  false`, so Railway also restarts the process if it ever dies and never idles it.
- **Use the Hobby plan (or a plan with enough resources), not the free trial.** A
  Telegram bot polls continuously, so it runs 24/7 — the trial's one-time credit
  will run out and Railway then **pauses the whole project** (the #1 reason a bot
  goes silent). The paid plan keeps it running; add the bot + Postgres and it stays
  up as long as the plan has resources.
- **Never poll the same bot token from two places.** Telegram allows only one
  `getUpdates` consumer per token — if you also run `app.main` (or a second Railway
  service) with a token that's already polled here, both break with a 409 conflict.
  For real scale-out, give each extra service a **different** `POLL_ONLY` subset.

**Scaling past one bot's load:** just add more tokens to `BOT_TOKENS` — the single
service polls them all. To spread across *machines*, deploy a second Railway service
from the same repo with `POLL_ONLY` set to a subset of bot ids and the **same**
`DATABASE_URL`; every copy keeps the full registry so notifications still reach a
user on whichever bot they used.

**Using an external DB instead** (your VPS Postgres, or Supabase): set `DATABASE_URL`
to that DSN. For Supabase use the **transaction pooler (port 6543)** and append
`?sslmode=require`; for a VPS reachable over the internet, add `?sslmode=require`
too. (Railway's own Postgres over the private network needs no SSL.)

## Setup (VPS)

1. **Postgres** on the VPS:
   ```bash
   sudo -u postgres psql -c "CREATE USER fayda WITH PASSWORD 'secret';"
   sudo -u postgres psql -c "CREATE DATABASE faydapdf OWNER fayda;"
   ```
2. **Bot**:
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env         # fill BOT_TOKEN, ADMIN_IDS, DATABASE_URL, FAYDA_API_*
   python -m app.main
   ```
   Keep it running with **systemd** or `pm2 start "python -m app.main" --name faydapdf-py`.

The schema auto-creates on first boot. No data is lost on redeploy — it lives in
Postgres on your VPS.

## Status

**Done & runnable (API mode):**
- Users + download (FAN → OTP → PDF), **multi-FAN queue** (send several ids, get each in turn)
- **Atomic money**: wallet ledger, add-balance → approve, `/topup`
- **Auto receipt verification** (verifypayment → Leul → phone-relay): a confirmed
  receipt **auto-approves instantly**; unverified ones fall to admin manual approve.
  The merchant **receiver is admin-set per bank (Telebirr + CBE)** — shown to users
  in Add-Balance and enforced on auto-approve (fails closed / rejects already-used).
  Admins get a **🔎 Verify** button to run the check on any pending receipt on demand
- **VIP** (`/vip`, `/vipprice`) + VIP pricing, **global price** (`/gprice`)
- **Broadcast** (Telegram, throttled)
- **Web admin dashboard** (live) — stats, users, payments, settings
- Forgot-FAN, pause, Fayda-mode switch

**Native Server-4 mode — code-complete:** the whole flow is ported to Python —
fresh single-use pool token → eSignet (authorize → oauth-details → send-OTP →
authenticate → auth-code) → backend callback → in-process PDF render (reportlab,
smoke-tested with photo/QR/fields). Switch to it live from the Admin panel (*Mode*)
or set `FAYDA_MODE_DEFAULT=server4`.

> ⚠️ **Needs one live pass.** The eSignet request/header shapes and the PDF card
> layout were ported from `faydapdf-railway` but can only be *confirmed* against a
> real pool token + the Fayda backend. Do the first `server4` download with an admin
> account watching logs; keep `api` as the instant fallback if anything is off. The
> PDF is a clean, correct document — refine the exact card layout against a real
> payload if you want a pixel match. Amharic name renders only if `FAYDA_AMHARIC_FONT`
> points at an Ethiopic TTF.

**Skipped by request:** QR-code receipt scanning.
