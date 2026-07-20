-- faydapdf-py schema (PostgreSQL). Idempotent: safe to run on every boot.
-- Design goals: money is atomic + auditable, receipts are idempotent, and every
-- balance change is a ledger row so state can never silently drift.
-- Money is stored in INTEGER CENTS everywhere (no float rounding bugs).

CREATE TABLE IF NOT EXISTS users (
  telegram_id           BIGINT PRIMARY KEY,
  username              TEXT,
  status                TEXT   NOT NULL DEFAULT 'active',    -- active | pending | blocked
  billing_mode          TEXT   NOT NULL DEFAULT 'prepaid',   -- counter | prepaid | postpaid
  balance_cents         BIGINT NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),           -- cached; always == last ledger balance_after
  owed_cents            BIGINT NOT NULL DEFAULT 0 CHECK (owed_cents >= 0),           -- postpaid running bill
  credit_limit_cents    BIGINT NOT NULL DEFAULT 0,
  price_override_cents  BIGINT,                              -- NULL = use global price
  is_vip                BOOLEAN NOT NULL DEFAULT FALSE,
  daily_limit           INT    NOT NULL DEFAULT 0,           -- 0 = unlimited
  total_limit           INT    NOT NULL DEFAULT 0,
  last_bot_id           BIGINT,                              -- which bot the user last used (multi-bot notify)
  delivery_pref         TEXT   NOT NULL DEFAULT 'both',       -- what the user gets: both | pdf | screenshot
  role                  TEXT,                                -- user | admin | superadmin (from railway; informational)
  tag                   TEXT,                                -- admin label / segment
  discount_cents        BIGINT NOT NULL DEFAULT 0,           -- per-user discount off the price
  allow_pdf             BOOLEAN NOT NULL DEFAULT TRUE,       -- may receive the PDF
  allow_screenshot      BOOLEAN NOT NULL DEFAULT TRUE,       -- may receive screenshots
  bonus_cents           BIGINT NOT NULL DEFAULT 0 CHECK (bonus_cents >= 0),  -- LIFETIME bonus granted (welcome + admin); historical record
  bonus_balance_cents   BIGINT NOT NULL DEFAULT 0 CHECK (bonus_balance_cents >= 0),  -- CURRENT spendable bonus wallet (separate from balance; spent first)
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at           TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- New users default to prepaid (only affects rows inserted from now on, not existing members).
ALTER TABLE users ALTER COLUMN billing_mode SET DEFAULT 'prepaid';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_bot_id BIGINT;  -- backfill existing DBs
ALTER TABLE users ADD COLUMN IF NOT EXISTS delivery_pref TEXT NOT NULL DEFAULT 'both';
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tag TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS discount_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS allow_pdf BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS allow_screenshot BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_balance_cents BIGINT NOT NULL DEFAULT 0;  -- separate spendable bonus wallet (0 for pre-existing users = no change)

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
  decided_by    TEXT,                                 -- admin id OR 'web-admin'/'web-bulk'/'auto:<provider>'
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at    TIMESTAMPTZ,
  CONSTRAINT payments_receipt_uq UNIQUE (receipt_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_user   ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status) WHERE status = 'pending';
-- Migrate decided_by BIGINT → TEXT (it stores 'web-admin'/'web-bulk'/admin id).
-- Guarded so it only rewrites once, not on every boot.
DO $$ BEGIN
  IF (SELECT data_type FROM information_schema.columns
      WHERE table_name='payments' AND column_name='decided_by') <> 'text' THEN
    ALTER TABLE payments ALTER COLUMN decided_by TYPE TEXT USING decided_by::text;
  END IF;
END $$;

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

-- Blocked-user + personalization columns (broadcast reliability).
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_reason TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS unblocked_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;   -- broadcast personalization {name}
CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(is_blocked);

-- Persistent broadcast campaigns. A campaign snapshots one recipient row per user so
-- a big blast can be paused/resumed and survives a restart (the worker re-scans).
CREATE TABLE IF NOT EXISTS broadcast_campaigns (
  id           BIGSERIAL PRIMARY KEY,
  title        TEXT,
  segment      TEXT   NOT NULL DEFAULT 'all',
  filter_json  TEXT,                                   -- advanced filter params (tag/role/min/max)
  message      TEXT   NOT NULL,
  parse_mode   TEXT,                                   -- HTML | Markdown | NULL
  buttons_json TEXT,                                   -- inline url buttons
  status       TEXT   NOT NULL DEFAULT 'draft',        -- draft|sending|paused|completed|cancelled
  total        INT    NOT NULL DEFAULT 0,
  sent         INT    NOT NULL DEFAULT 0,
  failed       INT    NOT NULL DEFAULT 0,
  blocked      INT    NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bcast_status ON broadcast_campaigns(status);

CREATE TABLE IF NOT EXISTS broadcast_recipients (
  id          BIGSERIAL PRIMARY KEY,
  campaign_id BIGINT NOT NULL REFERENCES broadcast_campaigns(id) ON DELETE CASCADE,
  user_id     BIGINT NOT NULL,
  bot_id      BIGINT,
  status      TEXT   NOT NULL DEFAULT 'pending',       -- pending|sending|sent|failed|blocked
  error       TEXT,
  tried_at    TIMESTAMPTZ,
  retries     INT    NOT NULL DEFAULT 0,
  UNIQUE (campaign_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_bcast_recip ON broadcast_recipients(campaign_id, status);

-- Saved custom broadcast filters (raw WHERE fragment, validated on write).
CREATE TABLE IF NOT EXISTS broadcast_filters (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT UNIQUE NOT NULL,
  where_clause TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
