# Telebirr / CBE Payment Verification — Portable Implementation Spec

A **language-agnostic** spec for building automatic Telebirr (and CBE) receipt
verification into any project. Nothing here depends on a specific framework — you need
only: an HTTP client, a database with a **unique constraint**, and (optionally) an OCR
binary for screenshots. Reference implementation: faydapdf-py (`app/services/payment_verify.py`).

```
raw input ─▶ EXTRACT ref+bank ─▶ CANDIDATES (look-alike) ─▶ VERIFY (provider API)
                                                               │
                         ok + right receiver + amount>0 ───────┼─▶ CREDIT (auto-approve)
                         confirmed but wrong account ──────────┼─▶ REJECT
                         invalid / unverifiable ───────────────┴─▶ MANUAL review
```

---

## 0. Data model (minimum)

A `payments` table (or equivalent):

| column | notes |
|---|---|
| `id` | PK |
| `user_id` | who submitted |
| `receipt_id` | **`UNIQUE`** ← the idempotency guard; a receipt credits **once** |
| `bank` | `telebirr` \| `cbe` |
| `amount_cents` | integer cents (never floats for money) |
| `status` | `pending` \| `approved` \| `rejected` |
| `provider` | which verifier approved (or `manual`) |
| `created_at`, `decided_at`, `decided_by` | audit |

**The UNIQUE(receipt_id) is non-negotiable** — it's what makes double-submission
impossible even under races. Insert with `ON CONFLICT (receipt_id) DO NOTHING` and
treat "0 rows inserted" as "already submitted".

---

## 1. Extraction — `extract_reference(text) → (reference, bank)`

### The one rule that prevents false positives
```
is_telebirr_ref(v):
    v = uppercase(trim(v))
    return matches(v, /^[A-Z0-9]{10}$/) AND contains_letter(v)
```
A Telebirr reference is **exactly 10 alphanumerics with ≥1 letter**. This is what
rejects phone numbers, amounts, and 12-digit numbers (all-digit → no letter → not a
receipt). CBE is `FT` + digits (~12 chars).

### Patterns (use verbatim)
```
TELEBIRR_LINK = /transactioninfo\.ethiotelecom\.et\/receipt\/([A-Za-z0-9]+)/i
CBE_HOST      = /apps\.cbe\.com\.et|mbreciept\.cbe\.com\.et|mb\.cbe\.com\.et/i
CBE_REF       = /FT[A-Z0-9]{6,}/i
SMS_TXN       = /transaction\s*(?:number|no\.?)\s*(?:is|:)?\s*([A-Za-z0-9]{8,15})/i
BARE_CBE      = /^FT[A-Z0-9]{6,}(?:-\d{6,})?$/i
TOKEN_10      = /\b[A-Z0-9]{10}\b/    (over the uppercased text)
```

### Algorithm (first match wins)
```
extract_reference(text):
    t  = trim(text);  if empty → ("", "")
    up = uppercase(t)

    # 1. CBE: an app link, or a bare FT… reference
    if CBE_HOST matches t  OR  BARE_CBE matches up:
        m = search(CBE_REF, up); if m → (m, "cbe")

    # 2. Telebirr receipt link
    m = search(TELEBIRR_LINK, t); if m and is_telebirr_ref(m[1]) → (upper(m[1]), "telebirr")

    # 3. 127 SMS  ("...transaction number is XXXX...")
    m = search(SMS_TXN, t);       if m and is_telebirr_ref(m[1]) → (upper(m[1]), "telebirr")

    # 4. Bare code (the whole message IS the code)
    if is_telebirr_ref(up) → (up, "telebirr")

    # 5. Last resort: an FT… token anywhere, else a 10-char token that has a letter
    m = search(/\bFT[A-Z0-9]{6,}\b/, up); if m → (m, "cbe")
    for tok in find_all(TOKEN_10, up):
        if is_telebirr_ref(tok) → (tok, "telebirr")

    return ("", "")     # not a receipt
```
Use `looks_like_receipt(text) = extract_reference(text)[0] != ""` to decide whether an
arbitrary pasted message is a payment at all.

### Screenshot → txn (optional OCR)
`ocr_receipt(image) → (txn, amount, is_receipt)`:
1. Grayscale, **upscale to ≥1600px wide** if smaller, auto-contrast.
2. Run OCR (e.g. tesseract).
3. txn = first `Transaction Number: XXXX` (8–14 chars) → strip non-alnum; else the
   first `[A-Z0-9]{10}` token that has **both a letter and a digit**.
4. amount = first `\d{1,7}\.\d{2}`.
5. is_receipt = text contains `transaction|telebirr|successful|receipt|birr`.
6. No OCR binary → return `("", 0, false)` and tell the user "couldn't read" — **never
   create a payment from an unreadable image.**

---

## 2. Look-alike double-checking

Handwriting/OCR confuse a fixed set of characters. Before declaring a reference
invalid, try the variants.

### The flip table (bidirectional except `L→1`)
```
FLIP = { O↔0, S↔5, I↔1, B↔8, Z↔2, A↔4, L→1 }
```

### Candidate generation — `candidates(txn, cap=64)`
```
candidates(txn, cap=64):
    txn = uppercase(strip_non_alnum(txn))
    out = [txn]                                  # exact FIRST
    pos = indices where txn[i] in FLIP
    for k in 1..len(pos):                        # all 1-flips, then 2-flips, then 3-...
        for combo in combinations(pos, k):
            v = txn with each combo index flipped via FLIP
            if v not in out: out.push(v)
            if len(out) >= cap: return out
    return out
```
`cap=64` = every combination for **up to 6 ambiguous characters** (2⁶); for more, it
still covers all 1-, 2- and 3-character corrections. Example: `DGH3WU4015` →
`DGH3WU4OI5`.

### Verify-with-candidates — `verify_candidates(cands, expected_cents=0)`
```
verify_candidates(cands, expected_cents=0):
    r0 = verify(cands[0])                         # exact first
    if acceptable(r0, expected_cents): return r0
    # A REAL receipt that just isn't creditable is NOT a typo — do not hunt variants
    # (a variant could be a different person's valid receipt).
    if r0.receiver_mismatch or r0.already_used: return r0
    # Only an INVALID/not-found exact gets look-alike correction:
    run cands[1:] concurrently, max 6 at a time, EARLY-EXIT on first acceptable
    return that, else r0

acceptable(res, expected):
    res.ok AND res.amount_cents > 0
        AND (expected == 0 OR |res.amount_cents - expected| <= 50)   # 0.5 unit tolerance
```

Run `verify_candidates` on: submission, admin re-verify, and bulk approve.

---

## 3. The verify state machine — `verify(reference) → result`

Tries configured providers **in order**, returns the first **acceptable** success.

```
verify(reference):
    if approver == "manual": return {ok:false, manual:true}
    bank = detect_bank(reference)                # starts "FT" → cbe, else telebirr
    want = primary_receiver(bank)                # your merchant name/account
    last = {ok:false, error:"no verifier"}
    for provider in providers_for(approver):     # auto = all, in order
        res = provider.check(bank, reference, want)
        if res == null: continue                 # provider not configured → skip
        if res.ok:
            if res.already_used: last = {ok:false, already_used:true}; continue
            if not receiver_ok_any(res, bank): last = {ok:false, receiver_mismatch:true}; continue
            res.bank = bank; return res          # ✅ ACCEPTED
        last = res                               # remember last failure
    return last
```

### `approver` (config)
`auto` (all providers in order) · `<provider>` (only that one) · `manual` (no
auto-approve — everything to review).

### Receiver matching — money must land in YOUR account
```
receiver_ok(res, want_name, want_acct):
    if want_name == "" and want_acct == "": return true      # FAIL OPEN (nothing to check)
    if want_name:
        got = normalize(res.receiver_name)
        if got == "" or (want_name ⊄ got and got ⊄ want_name): return false   # FAIL CLOSED
    if want_acct:
        got_digits = digits(res.receiver_account)             # providers mask accounts
        if got_digits == "" or last4(want_acct) not in got_digits: return false
    return true
```
- Name: case/space-insensitive, substring-tolerant.
- Account: match on **trailing digits** (handles `251****1234`).
- **Fail CLOSED**: receiver configured but provider returned none → refuse (→ manual).
- **Fail OPEN**: no receiver configured → nothing to enforce → allow.
- `receiver_ok_any`: Telebirr accepts a match against **any** of several configured
  receivers; CBE matches the single one.

### Normalized provider result
```
{ ok, provider, receipt_id, amount_cents, receiver_name, receiver_account, status, bank,
  already_used?, receiver_mismatch?, transient?, error?, http_status? }
```

---

## 4. Provider contract — VerifyPayment (and shape for any provider)

### Request
```
POST {base}/api/check                      base default: https://www.verifypayment.org.et
Header: X-API-Key: <api key>
Body:   { "bank": "telebirr"|"cbe", "url": "<receipt id or link>",
          "receiver_name": "<optional>", "receiver_account": "<optional>" }
Timeout: 15s
```

### Response → normalized result
On `body.status == "success"`, read `body.data`:
| response field | → |
|---|---|
| `transaction_id` \| `reference_no` | `receipt_id` (uppercased) |
| `amount` | `amount_cents` (× 100) |
| `receiver_name` | `receiver_name` |
| `receiver_account` | `receiver_account` |
| `already_used` (top level) | `already_used` |

Else → `{ok:false, error, transient: http∈{0,429,≥500}}`. **Transient** = network /
rate-limit / 5xx → retryable, not "invalid".

### Adding another provider
Implement `check(bank, reference, want) → normalized result | null` (null = not
configured). Map its fields to `receipt_id / amount_cents / receiver_* / already_used`.
`auto` mode just calls each configured provider until one is accepted. (faydapdf-py
ships three: VerifyPayment, Leul `/verify-telebirr`, phone-relay `/api/verify`.)

---

## 5. Finalization — decide the money

```
finalize(user, reference, res):
    if res.ok and res.amount_cents > 0:
        (payment, created) = submit(user, res.receipt_id, res.bank, res.amount_cents, res.provider)
        if not created: return "already submitted (" + payment.status + ")"
        approve(payment)                          # ✅ credit the balance atomically
        return "verified, {amount} added"

    bank = res.bank or detect_bank(reference)

    if res.receiver_mismatch:                      # real payment, WRONG account
        (payment, created) = submit(user, reference, bank, 0, "auto")
        if not created: return "already submitted"
        reject(payment, "auto:receiver_mismatch")  # 🚫 no bother to admin
        return "this went to a different account"

    # already_used / unverifiable / no verifier → 👤 MANUAL
    (payment, created) = submit(user, reference, bank, 0, "manual")
    if not created: return "already submitted"
    notify_admins(payment, flag = res.already_used ? "ALREADY USED" : "")
    return "submitted (#{payment.id}), an admin will review"
```
- `submit` = the idempotent `INSERT … ON CONFLICT(receipt_id) DO NOTHING`.
- `approve` must credit the balance and flip status to `approved` in **one
  transaction** (lock the user row / atomic `balance = balance + amount`).
- Auto-reject only ever fires when `receiver_mismatch` is set — which only happens when
  a receiver is configured (else the check fails open).
- Admin re-verify: re-run `verify_candidates`; if it returns a **corrected** id,
  approve with that id (guard with a duplicate check so you never double-credit).

---

## 6. Config surface

| key | purpose |
|---|---|
| `approver` | auto \| verifypayment \| leul \| relay \| manual |
| `vp_base_url`, `vp_api_key` | VerifyPayment endpoint + key |
| receiver name/account per bank (Telebirr may be a **list** with `show`/`verify` flags) | who money must land with |
| `show_autoverify` | advertise "auto-verified" to users |

---

## 7. Correctness checklist (port this exactly)

- [ ] `receipt_id` has a DB **UNIQUE** constraint; submit is `ON CONFLICT DO NOTHING`.
- [ ] Telebirr ref = **10 alnum with a letter**; CBE = `FT…`. Reject all-digit input.
- [ ] Extraction order: CBE → Telebirr link → SMS → bare → last-resort token.
- [ ] Look-alike candidates: exact first, growing combinations, cap 64.
- [ ] **Don't** hunt candidates on `receiver_mismatch` / `already_used`.
- [ ] Receiver match fails **closed** (configured but missing) / **open** (unconfigured).
- [ ] Money is integer cents; approve credits in **one transaction**.
- [ ] Screenshot with no readable txn → **no payment**, just "couldn't read".
- [ ] Wrong-account, provider-confirmed → **auto-reject**; unverifiable → **manual**.

---

### One-line summary
**Extract a valid reference (letter-bearing 10-char Telebirr or `FT…` CBE) from text /
link / SMS / screenshot → try look-alike corrections → verify against a provider API →
accept only if real, to *your* account, unused, amount > 0 → credit atomically;
otherwise auto-reject a wrong-account receipt or send an unverifiable one to review —
with `UNIQUE(receipt_id)` guaranteeing no receipt is ever credited twice.**
