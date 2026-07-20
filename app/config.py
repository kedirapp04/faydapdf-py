"""Environment configuration. Fails fast on missing required vars."""
import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        raise SystemExit(f"Missing required env var: {name} (see .env.example)")
    return v


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _birr_to_cents(name: str, default: float = 0.0) -> int:
    try:
        return round(float(os.getenv(name) or default) * 100)
    except ValueError:
        return round(default * 100)


# ── Telegram (multi-bot) ────────────────────────────────────────────────────
# BOT_TOKENS = comma-separated full tokens (one per bot). Falls back to BOT_TOKEN.
# The bot id is the numeric prefix of the token, so no manual ids are needed.
def _bot_tokens() -> list[str]:
    raw = (os.getenv("BOT_TOKENS") or os.getenv("BOT_TOKEN") or "").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


BOT_TOKENS = _bot_tokens()
if not BOT_TOKENS:
    raise SystemExit("Missing BOT_TOKEN or BOT_TOKENS (see .env.example)")


def bot_id_of(token: str) -> int:
    return int(token.split(":", 1)[0])


# Registry every process knows about — so any bot/web can notify a user via the
# specific bot that user actually started.
BOT_REGISTRY = {bot_id_of(t): t for t in BOT_TOKENS}
BOT_TOKEN = BOT_TOKENS[0]  # primary (fallback for single-message sends)

# POLL_ONLY = comma of bot ids THIS process should long-poll (for per-process
# scaling: run several copies, each polling a subset, all sharing the DB). Empty
# = poll all of BOT_TOKENS.
_poll_only = {s.strip() for s in (os.getenv("POLL_ONLY") or "").split(",") if s.strip()}
POLL_TOKENS = [t for t in BOT_TOKENS if (not _poll_only or str(bot_id_of(t)) in _poll_only)]

ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_IDS") or "").split(",") if s.strip()}

# ── Database ────────────────────────────────────────────────────────────────
DATABASE_URL = _req("DATABASE_URL")
DB_POOL_MIN = _int("DB_POOL_MIN", 2)
DB_POOL_MAX = _int("DB_POOL_MAX", 12)

# ── Fayda integration ───────────────────────────────────────────────────────
# The active mode is stored in `settings` (admin-switchable); this is the seed.
FAYDA_MODE_DEFAULT = (os.getenv("FAYDA_MODE_DEFAULT") or "api").strip().lower()

FAYDA_API_URL = (os.getenv("FAYDA_API_URL") or "").rstrip("/")
FAYDA_API_KEY = (os.getenv("FAYDA_API_KEY") or "").strip()

SERVER4_TOKEN_API_URL = (os.getenv("SERVER4_TOKEN_API_URL") or "").strip()
SERVER4_TOKEN_API_CSRF = (os.getenv("SERVER4_TOKEN_API_CSRF") or "").strip()
SERVER4_TOKEN_MIN_SECONDS = _int("SERVER4_TOKEN_MIN_SECONDS", 90)
# Optional pool-health endpoint for the admin Tokens view. Blank = derive it from
# SERVER4_TOKEN_API_URL (last path segment → /stats).
SERVER4_TOKEN_STATS_URL = (os.getenv("SERVER4_TOKEN_STATS_URL") or "").strip()
# Native Server-4 endpoints (Fayda v1.1.9). Defaults mirror fayda-railway.
FAYDA_API_BASE = (os.getenv("FAYDA_API_BASE") or "https://fayda-app-backend.fayda.et").rstrip("/")
ESIGNET_BASE = (os.getenv("ESIGNET_BASE") or "https://auth.fayda.et").rstrip("/")
FAYDA_BACKEND_API_KEY = (os.getenv("FAYDA_BACKEND_API_KEY") or "ndC5mYXlkYS5ldCAoT0lEQ19QQVJUTkVSKTCCASIwDQ").strip()
FAYDA_OTP_CHANNELS = [c.strip() for c in (os.getenv("FAYDA_OTP_CHANNELS") or "PHONE").split(",") if c.strip()]
SERVER4_AUTHORIZE_CACHE_MS = _int("SERVER4_AUTHORIZE_CACHE_MS", 600000)
# PDF renderer for Server-4: "js" = the bundled faydapdf-railway pdfGenerator via Node
# (byte-for-byte the JS output; falls back to Python if Node/bundle missing); "py" =
# the in-process Python renderer.
PDF_ENGINE = (os.getenv("FAYDA_PDF_ENGINE") or "js").strip().lower()

# ── Billing ─────────────────────────────────────────────────────────────────
GLOBAL_PRICE_CENTS = _birr_to_cents("GLOBAL_PRICE_BIRR", 0)

# ── Payment verification (auto Telebirr/CBE receipt checking) ────────────────
VERIFYPAYMENT_BASE_URL = (os.getenv("VERIFYPAYMENT_BASE_URL") or "https://www.verifypayment.org.et").rstrip("/")
VERIFYPAYMENT_API_KEY = (os.getenv("VERIFYPAYMENT_API_KEY") or "").strip()
LEUL_VERIFY_BASE_URL = (os.getenv("LEUL_VERIFY_BASE_URL") or "https://verifyapi.leulzenebe.pro").rstrip("/")
LEUL_VERIFY_API_KEY = (os.getenv("LEUL_VERIFY_API_KEY") or "").strip()
RELAY_VERIFY_BASE_URL = (os.getenv("RELAY_VERIFY_BASE_URL") or "").rstrip("/")
RELAY_VERIFY_API_KEY = (os.getenv("RELAY_VERIFY_API_KEY") or "").strip()
# The merchant account the money must land in (Telebirr). Used to confirm the
# receiver so a receipt paid to someone else is never auto-approved.
PAYMENT_RECEIVER_NAME = (os.getenv("PAYMENT_RECEIVER_NAME") or "").strip()
PAYMENT_RECEIVER_ACCOUNT = (os.getenv("PAYMENT_RECEIVER_ACCOUNT") or "").strip()

# ── Web admin ───────────────────────────────────────────────────────────────
ADMIN_WEB_PASSWORD = (os.getenv("ADMIN_WEB_PASSWORD") or "").strip()
WEB_SECRET = (os.getenv("WEB_SECRET") or "").strip() or ("sig-" + (ADMIN_WEB_PASSWORD or "changeme"))
WEB_HOST = (os.getenv("WEB_HOST") or "0.0.0.0").strip()
# Railway/Heroku inject $PORT and route to it — that must win. Only an explicitly
# set WEB_PORT overrides it; otherwise use $PORT, else 8080 for local/VPS.
WEB_PORT = _int("WEB_PORT", 0) or _int("PORT", 8080)


def is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS
