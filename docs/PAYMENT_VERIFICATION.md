# Payment Verification — Full Process (Telebirr + CBE)

The **complete** journey of a payment receipt, beginning to end: the moment a user
sends something → deciding it's a receipt → extracting a reference → correcting
typos/OCR errors → verifying it against the bank → checking the money reached **you** →
blocking replays → crediting, rejecting, or sending to manual review.

Two banks, two verification strategies, **one** decision pipeline:

- **Telebirr** → verified via a **third-party API** (VerifyPayment / Leul / relay).
- **CBE** → verified **directly from CBE's own servers** (see §5 + `CBE_VERIFIER_GUIDE.md`).

This doc is implementation-agnostic (pseudocode + contracts). Reference implementation:
faydapdf-py `app/services/payment_verify.py` + `app/handlers/user.py`.

---

## Table of contents
0. [The pipeline at a glance](#0-the-pipeline-at-a-glance)
1. [Stage 1 — Entry points](#1-stage-1--entry-points)
2. [Stage 2 — Bank detection & reference extraction](#2-stage-2--bank-detection--reference-extraction)
3. [Stage 3 — Look-alike double-checking](#3-stage-3--look-alike-double-checking)
4. [Stage 4a — Verify Telebirr (third-party)](#4-stage-4a--verify-telebirr-third-party)
5. [Stage 4b — Verify CBE (direct)](#5-stage-4b--verify-cbe-direct)
6. [Stage 5 — Receiver matching](#6-stage-5--receiver-matching)
7. [Stage 6 — Replay / already-used guard](#7-stage-6--replay--already-used-guard)
8. [Stage 7 — Finalization](#8-stage-7--finalization)
9. [Data model & idempotency](#9-data-model--idempotency)
10. [End-to-end walkthroughs](#10-end-to-end-walkthroughs)
11. [Error handling & the transient rule](#11-error-handling--the-transient-rule)
12. [Configuration reference](#12-configuration-reference)
13. [Port checklist](#13-port-checklist)

---

## 0. The pipeline at a glance

```
                 ┌─────────────────────────────────────────────────────────────┐
   user sends ──▶│ 1 ENTRY   text / link / 127-SMS / bare code / screenshot     │
                 └─────────────────────────────────────────────────────────────┘
                                        │
                 ┌──────────────────────▼──────────────────────┐
                 │ 2 EXTRACT   reference + bank                 │  telebirr | cbe | ("" = not a receipt)
                 └──────────────────────┬──────────────────────┘
                        telebirr ◄──────┴──────► cbe
                            │                     │
        ┌───────────────────▼───────┐   ┌─────────▼─────────────────────────┐
        │ 3 CANDIDATES (look-alike) │   │ (CBE ref used as-is)               │
        └───────────────────┬───────┘   └─────────┬─────────────────────────┘
        ┌───────────────────▼───────┐   ┌─────────▼─────────────────────────┐
        │ 4a VERIFY via provider API│   │ 4b VERIFY direct from CBE servers  │
        └───────────────────┬───────┘   └─────────┬─────────────────────────┘
                            └──────────┬───────────┘
                 ┌─────────────────────▼─────────────────────┐
                 │ 5 RECEIVER MATCH  (money went to YOU?)     │
                 │ 6 REPLAY GUARD    (receipt already used?)  │
                 │    amount > 0 / amount == expected?        │
                 └─────────────────────┬─────────────────────┘
              ┌───────────────┬────────┴────────┬───────────────┐
        ✅ CREDIT        🚫 REJECT         👤 MANUAL         ("already used")
     (approve+credit)  (wrong account)  (unverifiable)      → tell user
```

Every stage is below in order.

---

## 1. Stage 1 — Entry points

A receipt can arrive four ways. Each funnels into the same extractor (Stage 2).

| Entry | Trigger | Note |
|---|---|---|
| **Add-Payment step** | user tapped "Add Payment", then pastes | explicit |
| **Anytime auto-detect** | any pasted message where `looks_like_receipt(text)` is true | routed to payment instead of being treated as an ID/other text |
| **Screenshot** | user sends a photo | OCR first (§2b), then same pipeline |
| **Admin re-verify / bulk** | admin clicks 🔁 or bulk-approves pending receipts | re-runs Stages 3–4 on stored receipts |

`looks_like_receipt(text) = extract_reference(text)[0] != ""` — the extractor itself is
the gate for "is this even a receipt".

**Money-safety rule at entry:** a screenshot with **no readable transaction number**
must **not** create a payment — reply "couldn't read", nothing stored.

---

## 2. Stage 2 — Bank detection & reference extraction

`extract_reference(text) → (reference, bank)`. Returns `("", "")` when the text isn't a
recognizable receipt.

### 2.0 Bank detection
```
detect_bank(ref):  ref starts with "FT" → "cbe"   else → "telebirr"
```

### 2.1 Telebirr — the rule that prevents false positives
```
is_telebirr_ref(v):
    v = uppercase(trim(v))
    return matches(v, /^[A-Z0-9]{10}$/) AND contains_letter(v)
```
A Telebirr reference is **exactly 10 alphanumerics with ≥1 letter** — this rejects
phone numbers, amounts, and 12-digit numbers (all-digit → no letter → not a receipt).
An all-letters code is allowed (they occur).

### 2.2 Patterns (use verbatim)
```
TELEBIRR_LINK = /transactioninfo\.ethiotelecom\.et\/receipt\/([A-Za-z0-9]+)/i
CBE_HOST      = /apps\.cbe\.com\.et|mbreciept\.cbe\.com\.et|mb\.cbe\.com\.et/i
CBE_REF       = /FT[A-Z0-9]{6,}/i
SMS_TXN       = /transaction\s*(?:number|no\.?)\s*(?:is|:)?\s*([A-Za-z0-9]{8,15})/i
BARE_CBE      = /^FT[A-Z0-9]{6,}(?:-\d{6,})?$/i
TOKEN_10      = /\b[A-Z0-9]{10}\b/
```

### 2.3 Extraction algorithm (first match wins)
```
extract_reference(text):
    t  = trim(text);  if empty → ("", "")
    up = uppercase(t)

    # 1. CBE: an app link, or a bare FT… reference
    if CBE_HOST matches t OR BARE_CBE matches up:
        m = search(CBE_REF, up); if m → (m, "cbe")

    # 2. Telebirr receipt link
    m = search(TELEBIRR_LINK, t); if m and is_telebirr_ref(m[1]) → (upper(m[1]), "telebirr")

    # 3. 127 SMS  ("...transaction number is XXXX...")
    m = search(SMS_TXN, t);       if m and is_telebirr_ref(m[1]) → (upper(m[1]), "telebirr")

    # 4. Bare code (the whole message IS the code)
    if is_telebirr_ref(up) → (up, "telebirr")

    # 5. Last resort: FT token anywhere, else a 10-char token that has a letter
    m = search(/\bFT[A-Z0-9]{6,}\b/, up); if m → (m, "cbe")
    for tok in find_all(TOKEN_10, up):
        if is_telebirr_ref(tok) → (tok, "telebirr")

    return ("", "")
```
> **CBE note:** for a full CBE flow you also need the receiver **account** (see §5.2).
> The extractor above returns the FT number; the CBE fetcher pairs it with *your*
> account. `CBE_VERIFIER_GUIDE.md §7` lists every CBE input shape it accepts.

### 2b. Screenshot → transaction number (OCR)
`ocr_receipt(image) → (txn, amount, is_receipt)`:
1. Grayscale, **upscale to ≥1600px wide** if smaller, auto-contrast (sharper text).
2. Run OCR (tesseract).
3. txn = first `Transaction Number: XXXX` (8–14 chars) → strip non-alnum; else the
   first `[A-Z0-9]{10}` token with **both** a letter and a digit.
4. amount = first `\d{1,7}\.\d{2}` (feeds the amount-check in §7).
5. is_receipt = text contains `transaction|telebirr|successful|receipt|birr`.
6. No OCR available → `("", 0, false)` → "couldn't read", **no payment created**.

---

## 3. Stage 3 — Look-alike double-checking

*(Telebirr only. CBE references come from a scanned link, not hand-typed, so they're
used as-is.)* OCR and typing confuse a fixed character set — try the variants before
declaring a reference invalid.

### The flip table (bidirectional except `L→1`)
```
FLIP = { O↔0, S↔5, I↔1, B↔8, Z↔2, A↔4, L→1 }
```

### Candidate generation — `candidates(txn, cap=64)`
```
candidates(txn, cap=64):
    txn = uppercase(strip_non_alnum(txn))
    out = [txn]                                   # exact value FIRST
    pos = indices where txn[i] in FLIP
    for k in 1..len(pos):                         # all 1-flips, then 2-flips, then 3-...
        for combo in combinations(pos, k):
            v = txn with each combo index flipped via FLIP
            if v not in out: out.push(v)
            if len(out) >= cap: return out
    return out
```
`cap=64` covers **every** combination for up to 6 ambiguous characters (2⁶); for more,
it still covers all 1-, 2- and 3-char corrections. Example: `DGH3WU4015` → `DGH3WU4OI5`.

### Verify-with-candidates — `verify_candidates(cands, expected_cents=0)`
```
r0 = verify(cands[0])                              # exact first
if acceptable(r0, expected_cents): return r0
# A REAL receipt that just isn't creditable (wrong account / already used) is NOT a
# typo — do NOT hunt variants (a variant could be a different person's valid receipt).
if r0.receiver_mismatch or r0.already_used: return r0
# Only a genuinely INVALID exact gets look-alike correction:
run cands[1:] concurrently, ≤6 at a time, EARLY-EXIT on first acceptable → return it, else r0

acceptable(res, expected):
    res.ok AND res.amount_cents > 0
        AND (expected == 0 OR |res.amount_cents - expected| <= 50)   # 0.5-unit tolerance
```
Runs on: submission, admin re-verify, bulk approve.

---

## 4. Stage 4a — Verify Telebirr (third-party)

Telebirr does **not** expose a clean public verify API, so verification is delegated to
a provider that fetches the official `transactioninfo.ethiotelecom.et` receipt for you.

### The orchestrator — `verify(reference)`
```
verify(reference):
    if approver == "manual": return {ok:false, manual:true}
    bank = detect_bank(reference)
    want = primary_receiver(bank)                  # your merchant name/account
    last = {ok:false, error:"no verifier"}
    for provider in providers_for(approver):       # auto = all, in order
        res = provider.check(bank, reference, want)
        if res == null: continue                   # provider not configured
        if res.ok:
            if res.already_used: last = {ok:false, already_used:true}; continue
            if not receiver_ok_any(res, bank): last = {ok:false, receiver_mismatch:true}; continue
            res.bank = bank; return res            # ✅ ACCEPTED
        last = res
    return last
```

### `approver` (config)
`auto` (all providers in order) · `<provider>` (only that one) · `manual` (no
auto-approve — everything to review).

### Provider contract — VerifyPayment
```
POST {base}/api/check                       base: https://www.verifypayment.org.et
Header: X-API-Key: <key>
Body:   { "bank":"telebirr"|"cbe", "url":"<receipt id or link>",
          "receiver_name":"<opt>", "receiver_account":"<opt>" }
Timeout: 15s
```
On `body.status == "success"`, read `body.data`:

| response field | → |
|---|---|
| `transaction_id` \| `reference_no` | `receipt_id` (uppercased) |
| `amount` | `amount_cents` (× 100) |
| `receiver_name` | `receiver_name` |
| `receiver_account` | `receiver_account` |
| `already_used` (top level) | `already_used` |

Else → `{ok:false, error, transient: http∈{0,429,≥500}}`.

### Adding a provider
Implement `check(bank, ref, want) → normalized | null` (null = not configured), mapping
its fields to `receipt_id / amount_cents / receiver_* / already_used`. faydapdf-py ships
three: **VerifyPayment**, **Leul** (`/verify-telebirr`, `receiptNo`/`settledAmount`/
`creditedPartyName`), **phone-relay** (`/api/verify`).

---

## 5. Stage 4b — Verify CBE (direct)

CBE **does** expose its own official receipts, so CBE is verified **directly** — you
fetch CBE's own copy and read it. That direct fetch *is* the trust. Full copy-paste
module + all quirks: **`CBE_VERIFIER_GUIDE.md`**. Summary of the mechanism:

### 5.1 Two backends (which one depends on the link)
| Input | Backend | Transport |
|---|---|---|
| `FT…` + receiver last-8 (`apps.cbe.com.et:100/…`, or bare `FT…-account`) | **PDF** on `apps.cbe.com.et:100` | HTTPS on **port 100**, **self-signed cert** |
| short code (`mbreciept.cbe.com.et/v2-…`) | **JSON** on `mb.cbe.com.et` | ordinary HTTPS 443 |

```
FT + last8  ──► GET apps.cbe.com.et:100/BranchReceipt/<FT>&<last8>  ──► pdf-parse
short code  ──► GET mb.cbe.com.et/api/v1/transactions/public/transaction-detail/<code>
```

### 5.2 The non-negotiable CBE quirks
- **Port 100** — you need outbound TCP **100** (not just 443). If blocked, FT receipts
  fail `transient` → manual; short codes still work.
- **Self-signed TLS** on `:100` — disable cert validation **for that call**
  (`https.Agent({ rejectUnauthorized:false })`).
- **Keep the `v2-` prefix** of a short code — the JSON API is keyed by the *exact* path
  segment and returns **500** if you strip it.
- **The account is part of the key** — the FT PDF is keyed by `FT + last-8 of the
  RECEIVER account`. Since *you* are the receiver, you always know it → for a bare
  `FT…` you supply your own account.
- **`x-app-id` / `x-app-version` headers** impersonate the mbreciept frontend (public
  constants, env-overridable).

### 5.3 Parsing → the same normalized shape
Both backends map to `{payerName, payerAccount, receiverName, receiverAccount, date,
reference (the FT), amount}`, so the downstream (§6–§8) doesn't care which ran. CBE's
PDF glues labels to values (`ReceiverKEDIR SEID AMAN`) — whitespace-flatten and grab the
text *between* labels.

### 5.4 CBE's `already_used` is **always false** — YOU dedupe (§7).

---

## 6. Stage 5 — Receiver matching

**The money must have landed in YOUR account.** Both banks return the receiver; compare
it to your configured merchant.

```
receiver_ok(res, want_name, want_acct):
    if want_name == "" and want_acct == "": return true      # FAIL OPEN (nothing to check)
    if want_name:
        got = normalize(res.receiver_name)                   # lowercase, collapse spaces
        if got == "" or (want_name ⊄ got and got ⊄ want_name): return false   # FAIL CLOSED
    if want_acct:
        got_digits = digits(res.receiver_account)            # providers/CBE mask accounts
        if got_digits == "" or last4(want_acct) not in got_digits: return false
    return true
```

| Rule | Why |
|---|---|
| Name: substring-tolerant, case/space-insensitive | `"kedir seid" ⊂ "kedir seid aman"` |
| Account: match on **trailing digits** | receiver shown masked (`251****1234`, `1****0539`) |
| **Fail CLOSED** | receiver configured but provider returned none → refuse (→ manual) |
| **Fail OPEN** | no receiver configured → nothing to enforce → allow |
| Telebirr `receiver_ok_any` | accept a match against **any** of several configured Telebirr receivers; CBE matches the single one |

---

## 7. Stage 6 — Replay / already-used guard

**A receipt is a bearer token.** Anyone holding the link can paste it; the receipt
proves *a payment happened*, not *who submitted it*. The replay guard is what keeps that
honest.

- **Telebirr** — the provider may report `already_used`; honor it.
- **CBE** — the direct verifier **cannot** know; **you must dedupe** on `transaction_id`.
- **Both** — the ultimate guard is a **`UNIQUE(receipt_id)`** constraint in your DB
  (§9). Even if a provider's `already_used` is stale, the unique insert refuses the
  second credit.

Also enforce **amount > 0** and, when you have an expected price, `amount == expected`.

---

## 8. Stage 7 — Finalization

One decision function turns a verify result into money movement:

```
finalize(user, reference, res):
    if res.ok and res.amount_cents > 0:
        (payment, created) = submit(user, res.receipt_id, res.bank, res.amount_cents, res.provider)
        if not created: return "already submitted (" + payment.status + ")"
        approve(payment)                          # ✅ credit balance atomically
        return "verified, {amount} added"

    bank = res.bank or detect_bank(reference)

    if res.receiver_mismatch:                      # real payment, WRONG account
        (payment, created) = submit(user, reference, bank, 0, "auto")
        if not created: return "already submitted"
        reject(payment, "auto:receiver_mismatch")  # 🚫 auto-reject, don't bother admin
        return "this went to a different account"

    # already_used / unverifiable / transient / no verifier → 👤 MANUAL
    (payment, created) = submit(user, reference, bank, 0, "manual")
    if not created: return "already submitted"
    notify_admins(payment, flag = res.already_used ? "ALREADY USED" : "")
    return "submitted (#{payment.id}), an admin will review"
```

- `submit` = idempotent `INSERT … ON CONFLICT(receipt_id) DO NOTHING`.
- `approve` = flip to `approved` **and** credit balance in **one transaction** (lock the
  user row; atomic `balance = balance + amount`).
- Auto-reject fires **only** on `receiver_mismatch` (only set when a receiver is
  configured).
- **`transient` failures are NEVER a rejection** — route to manual/retry (§11).
- Admin re-verify: re-run `verify_candidates`; if it returns a **corrected** id, approve
  with that id (guard with a duplicate check).

---

## 9. Data model & idempotency

```sql
CREATE TABLE payments (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL,
  receipt_id   TEXT   NOT NULL,
  bank         TEXT   NOT NULL DEFAULT 'telebirr',   -- telebirr | cbe
  amount_cents BIGINT NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
  status       TEXT   NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
  provider     TEXT,                                  -- verifypayment | leul | relay | cbe | manual | auto
  decided_by   TEXT, created_at TIMESTAMPTZ DEFAULT now(), decided_at TIMESTAMPTZ,
  CONSTRAINT payments_receipt_uq UNIQUE (receipt_id)  -- ⭐ the anti-replay guarantee
);
```
- **`UNIQUE(receipt_id)`** makes double-credit impossible even under concurrent
  submissions. Submit is `ON CONFLICT DO NOTHING`; "0 inserted" ⇒ "already submitted".
- **Money is integer cents** — never floats.
- **`approve` is one transaction** — mark approved + credit together, or neither.

---

## 10. End-to-end walkthroughs

### A. Telebirr — pasted 127 SMS
```
"...you transferred ETB 100.00... transaction number is DGH3WU4OI5 on 24/07..."
 → extract_reference → ("DGH3WU4OI5","telebirr")
 → candidates(...) → verify_candidates → VerifyPayment /api/check {bank:telebirr,url:DGH3WU4OI5}
 → { ok, amount_cents:10000, receiver:"Kedir…", already_used:false }
 → receiver_ok_any ✓ → submit+approve → "Verified! 100 Birr added."
```

### B. Telebirr — screenshot with an OCR typo
```
photo → ocr_receipt → txn="DGH3WU4015", amount=100.0   (OCR read O/I as 0/1)
 → candidates("DGH3WU4015") includes "DGH3WU4OI5"
 → verify_candidates(expected_cents=10000): exact "…4015" invalid → variant "…4OI5" verifies,
   amount 100.00 within tolerance → accepted → credit.
```

### C. CBE — FT number + your account
```
"FT261714RH1P"  (+ your account 1000101484847)
 → detect_bank → cbe → fetch apps.cbe.com.et:100/BranchReceipt/FT261714RH1P&01484847
 → pdf-parse → receiver "KEDIR SEID AMAN" / 1****4847, amount 100
 → receiver match ✓ → dedupe FT261714RH1P (UNIQUE) → credit.
```

### D. CBE — mobile short code
```
"https://mbreciept.cbe.com.et/v2-hfHCxzvDiBDoiQs2c8Zw"
 → shortCode "v2-hfHCxzvDiBDoiQs2c8Zw"   (v2- KEPT)
 → GET mb.cbe.com.et/.../transaction-detail/v2-hfHCxzvDiBDoiQs2c8Zw
 → JSON → creditAccountHolder / amountCredited → receiver match ✓ → dedupe → credit.
```

### E. Wrong account (either bank)
```
verify → ok:true but receiver_ok = false → receiver_mismatch
 → finalize → auto-reject "paid to a different account" (no admin needed).
```

---

## 11. Error handling & the transient rule

| Situation | Result | Do |
|---|---|---|
| Receipt not found (404) | `{ok:false, status:404, transient:false}` | tell user to check the ref/link |
| Provider/CBE busy, timeout, 5xx | `{ok:false, transient:true}` | **manual/retry — NEVER reject** |
| Rate-limited (429) | `{ok:false, transient:true, rateLimited:true}` | back off, retry later |
| Fetched but unparseable | `{ok:false, status:502}` | manual (format changed) |
| No reference in input | `("","")` → 400 | ask for a proper receipt |
| Confirmed, wrong receiver | `{ok:true, receiver_mismatch:true}` | **auto-reject**, don't credit |
| Confirmed, already used | `already_used:true` | tell user; don't credit |

> **Golden rule:** `transient == true` means the *bank/provider* failed, **not** the
> *user*. Never convert a transient into a rejection.

---

## 12. Configuration reference

| Key | Purpose |
|---|---|
| `approver` | auto \| verifypayment \| leul \| relay \| manual |
| `vp_base_url`, `vp_api_key` | VerifyPayment endpoint + key |
| receiver name/account per bank (Telebirr may be a **list** with `show`/`verify` flags) | who money must land with |
| `show_autoverify` | advertise "auto-verified" to users |
| **CBE:** `CBE_MB_API_BASE`, `CBE_MB_APP_ID`, `CBE_MB_APP_VERSION`, `CBE_TIMEOUT_MS`, `CBE_MAX_ATTEMPTS` | see `CBE_VERIFIER_GUIDE.md §4` |

---

## 13. Port checklist

- [ ] `payments.receipt_id` has a DB **UNIQUE** index; submit is `ON CONFLICT DO NOTHING`.
- [ ] Telebirr ref = **10 alnum with a letter**; CBE = `FT…`. Reject all-digit input.
- [ ] Extraction order: CBE → Telebirr link → SMS → bare → last-resort token.
- [ ] Look-alike candidates (Telebirr): exact first, growing combos, cap 64.
- [ ] **Don't** hunt candidates on `receiver_mismatch` / `already_used`.
- [ ] Telebirr → provider API; **CBE → direct** (port 100 + self-signed + keep `v2-`).
- [ ] Receiver match fails **closed** (configured but missing) / **open** (unconfigured).
- [ ] **Dedupe every receipt** (`UNIQUE(receipt_id)`) — CBE's `already_used` is always false.
- [ ] Money is integer cents; approve credits in **one transaction**.
- [ ] Screenshot with no readable txn → **no payment**, just "couldn't read".
- [ ] `transient` → manual/retry, **never** reject.

---

### One-line summary
**Detect the bank and extract a valid reference from text / link / SMS / screenshot →
(Telebirr) correct look-alike typos and verify via a provider API, or (CBE) fetch the
official receipt directly from CBE → accept only if it's real, paid to *your* account,
unused, and amount > 0 → credit atomically; otherwise auto-reject a wrong-account
receipt or send an unverifiable one to manual review — with `UNIQUE(receipt_id)`
guaranteeing no receipt is ever credited twice.**
