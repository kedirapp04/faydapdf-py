-- faydapdf-py schema (PostgreSQL). Idempotent: safe to run on every boot.
-- Design goals: money is atomic + auditable, receipts are idempotent, and every
-- balance change is a ledger row so state can never silently drift.
-- Money is stored in INTEGER CENTS everywhere (no float rounding bugs).

CREATE TABLE IF NOT EXISTS users (
  telegram_id           BIGINT PRIMARY KEY,
  username              TEXT,
  status                TEXT   NOT NULL DEFAULT 'active',    -- active | pending | blocked
  billing_mode          TEXT   NOT NULL DEFAULT 'counter',   -- counter | prepaid | postpaid
  balance_cents         BIGINT NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),           -- cached; always == last ledger balance_after
  owed_cents            BIGINT NOT NULL DEFAULT 0 CHECK (owed_cents >= 0),           -- postpaid running bill
  credit_limit_cents    BIGINT NOT NULL DEFAULT 0,
  price_override_cents  BIGINT,                              -- NULL = use global price
  is_vip                BOOLEAN NOT NULL DEFAULT FALSE,
  daily_limit           INT    NOT NULL DEFAULT 0,           -- 0 = unlimited
  total_limit           INT    NOT NULL DEFAULT 0,
  last_bot_id           BIGINT,                              -- which bot the user last used (multi-bot notify)
  delivery_pref         TEXT   NOT NULL DEFAULT 'both',       -- what the user gets: both | pdf | screenshot
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at           TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_bot_id BIGINT;  -- backfill existing DBs
ALTER TABLE users ADD COLUMN IF NOT EXISTS delivery_pref TEXT NOT NULL DEFAULT 'both';

-- Payment receipts. UNIQUE(receipt_id) is the idempotency guard: a given bank
-- transaction can be recorded ONCE, so it can never double-credit.
CREATE TABLE IF NOT EXISTS payments (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  receipt_id    TEXT   NOT NULL,
  bank          TEXT   NOT NULL DEFAULT 'telebirr',
  amount_cents  BIGINT NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
  status        TEXT   NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  provider      TEXT,                                 -- verifypayment | leul | relay | manual
  reason        TEXT,
  decided_by    BIGINT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at    TIMESTAMPTZ,
  CONSTRAINT payments_receipt_uq UNIQUE (receipt_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_user   ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status) WHERE status = 'pending';

-- Append-only wallet ledger — the source of truth / audit trail for every cent.
CREATE TABLE IF NOT EXISTS wallet_ledger (
  id                  BIGSERIAL PRIMARY KEY,
  user_id             BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  kind                TEXT   NOT NULL,                 -- credit | debit
  amount_cents        BIGINT NOT NULL CHECK (amount_cents >= 0),                 -- always positive
  balance_after_cents BIGINT NOT NULL CHECK (balance_after_cents >= 0),
  reason              TEXT   NOT NULL,                 -- topup | download | refund | adjust
  ref_type            TEXT,                            -- payment | download | admin
  ref_id              BIGINT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON wallet_ledger(user_id, created_at DESC);

-- Download usage log (also drives counter-mode limits).
CREATE TABLE IF NOT EXISTS downloads (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  fan_hash    TEXT,                                    -- hashed FAN (never store the raw id)
  format      TEXT   NOT NULL DEFAULT 'pdf',
  cost_cents  BIGINT NOT NULL DEFAULT 0,
  day         DATE   NOT NULL DEFAULT current_date,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_downloads_user_day ON downloads(user_id, day);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Chats seen, per bot (broadcast recipients). A user may have started several
-- bots — broadcast/notify must go out from a bot they actually started.
CREATE TABLE IF NOT EXISTS chats (
  telegram_id BIGINT NOT NULL,
  bot_id      BIGINT NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (telegram_id, bot_id)
);
