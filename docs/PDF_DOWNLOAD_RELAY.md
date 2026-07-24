# faydapdf-py — PDF download flow & relay usage per API

How a user downloads a Fayda ID PDF, and **which calls must go through the relay (a phone on
a residential IP) vs stay direct**. Read alongside the code in `app/handlers/user.py` (the
bot flow) and `app/fayda/server4_provider.py` (the API calls).

---

## 1. Two backend modes (admin-selectable: setting `fayda_mode`)

- **`server4`** — faydapdf-py makes the Fayda API calls **itself** (this doc). Relay usage
  matters here: the Fayda hosts are WAF-blocked, so those calls must exit a residential IP.
- **`api`** — faydapdf-py delegates to an external rent API (`ApiProvider` → `POST /session`,
  `POST /session/:id/verify`). faydapdf-py makes **no Fayda calls**; the relay (if any) lives
  in that rent service.

Everything below is **`server4` mode**.

---

## 2. The user-facing flow (Telegram)

```
1. User taps  Get PDF / Get Screenshot        → state await_fan
2. Sends FIN/FAN (12–16 digits)               → state choose_fmt
3. Picks format (pdf | screenshot | json | pdf_json)
4. Bot PRE-CHECKS billing (balance / limits)  ← no charge yet
5. send_otp(...)  → Fayda SMSes an OTP to the user's phone   ← API calls #1–#6 below
6. User sends the OTP                          → state otp
7. verify_pdf(...) → user data → render PDF    ← API calls #7–#9 below
8. CHARGE billing once (only after a successful render)
9. Bot delivers the PDF (+ screenshots)
```

Steps 5 and 7 are where the Fayda API calls happen.

---

## 3. Every API call, in order, with its relay path

`{backend}` = `fayda-app-backend.fayda.et`  ·  `{esignet}` = `auth.fayda.et`
`{pool}` = your Server-4 App-Check token pool (your own host).

### `send_otp()` — steps 5 above
| # | Call | Host | Relay? | Why |
|---|------|------|:---:|-----|
| 1 | `GET {pool}/token?min_seconds=90`  (`X-CSRF-Token`) → App-Check token | **your server** | **DIRECT** | your own token pool — not WAF-blocked, and it must be fast/reliable |
| 2 | `GET {backend}/api/v2/auth/authorize`  (`X-Firebase-AppCheck`) → eSignet URL | `{backend}` | **RELAY** | Fayda backend — WAF-blocks datacenter IPs. *(Cached ~10 min: only a cache-miss spends a token + makes this call.)* |
| 3 | `GET {esignet}<authorize path>`  → sets cookies | `{esignet}` | **RELAY** | eSignet — WAF-blocked |
| 4 | `GET {esignet}/v1/esignet/csrf/token` → `XSRF-TOKEN` cookie | `{esignet}` | **RELAY** | eSignet — WAF-blocked |
| 5 | `POST {esignet}/v1/esignet/authorization/v2/oauth-details` (`X-XSRF-TOKEN` + cookie) → `transactionId` | `{esignet}` | **RELAY** | eSignet — WAF-blocked; **CSRF-protected** (needs the cookie from #4) |
| 6 | `POST {esignet}/v1/esignet/authorization/send-otp` → `maskedMobile`, Fayda SMSes the OTP | `{esignet}` | **RELAY** | eSignet — WAF-blocked |

### `verify_pdf()` — steps 7 above
| # | Call | Host | Relay? | Why |
|---|------|------|:---:|-----|
| 7 | `POST {esignet}/v1/esignet/authorization/v2/authenticate` (`{OTP}`) | `{esignet}` | **RELAY** | eSignet — WAF-blocked |
| 8 | `POST {esignet}/v1/esignet/authorization/auth-code` → `{ code }` | `{esignet}` | **RELAY** | eSignet — WAF-blocked |
| 9 | `GET {pool}/token` → a **fresh** App-Check token | **your server** | **DIRECT** | single-use token for the callback (reusing #1's would replay-fail) |
| 10 | `POST {backend}/api/v2/auth/callback` `{code, codeVerifier, state}` (`X-Firebase-AppCheck`) → **ID JSON** | `{backend}` | **RELAY** | Fayda backend — WAF-blocked; returns the person's data |
| 11 | render the JSON → PDF (+ screenshots) | **local** | **none** | pure CPU (Node `pdfGenerator` or reportlab) — no HTTP |

**Forgot-FAN** (separate flow, not part of download): `POST https://id.et/…/resend-sms` —
**DIRECT** (public endpoint, not WAF-blocked; needs the phone number as input, no agent).

---

## 4. The rule (why some are relay, some direct)

- **WAF-blocked Fayda hosts → RELAY:** `fayda-app-backend.fayda.et` (#2, #10) and
  `auth.fayda.et` (#3–#8). Their WAF blocks datacenter IPs; a phone on an Ethiopian
  residential/mobile IP is accepted. **6–8 of the ~10 calls per download are relayed.**
- **Your own / public hosts → DIRECT:** the App-Check token pool (#1, #9) is your server, and
  `id.et` forgot-FAN is public — routing these through a phone only adds latency and a failure
  point, so they stay direct.
- **Local → none:** the PDF render (#11) is CPU-only.

---

## 5. Sticky session — all relayed calls of one download go to ONE phone

Calls #2–#8 (and #10) belong to **one eSignet session**: cookies (`XSRF-TOKEN`) and the
`transactionId` established in #3–#5 must be reused in #6–#10, from the **same IP**. The relay
pins a whole download (one `session_id`) to a single device (sticky routing), so send-OTP and
verify happen on the same phone. If that phone goes offline mid-flow, the session breaks and
the user restarts — so keep enough phones online and don't yank one during a flow.

Because a full download is these ~8 sequential relayed calls on one phone, it's slower than a
direct call. The relay mitigates this with **long-poll + per-flow pacing** (the internal
eSignet steps run back-to-back; only a *new* download pays the rate-limit gap).

---

## 6. How faydapdf-py currently avoids the WAF (relay vs IP-spoof)

Today `server4_provider.py` uses **IP spoofing** — it stamps a random `X-Forwarded-For` /
`X-Real-IP` on the Fayda calls (`_spoof_ip()`, toggle `s4_ip_spoof`). That's a *header* trick:
it works only if Fayda's WAF trusts the forwarded header — many WAFs ignore it and block on
the real TCP source IP, so it's unreliable.

The **relay is the real fix**: the request physically originates from a residential IP (the
phone's). To switch faydapdf-py from IP-spoof to relay, route calls #2–#8 and #10 through the
relay's `_relay_session` and keep #1, #9, and forgot-FAN on `_direct_session` (see the relay
build guide). Nothing else in the flow changes.

---

## 7. Billing & delivery (where money moves)

- **Pre-check** at step 4 *and* re-check before verify — never send an OTP a user can't pay
  for. No charge here.
- **Charge once** at step 8, **after** a confirmed render (#11) — prepaid: `balance -= price`;
  postpaid: `owed += price`; counter: just increments the success counter. A failed verify
  charges nothing.
- **Deliver** the PDF bytes (and any screenshots) to the user; the render also validates the
  callback actually carried person data, so a blank template is never delivered or charged.
