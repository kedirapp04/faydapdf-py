from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from .. import config

# Labels match the OLD (faydapdf-railway) bot's reply keyboard so a user whose
# Telegram still shows the old cached keyboard keeps hitting the right handler.
# The keyboard refreshes automatically because every bot reply re-attaches main_kb.
BTN_START = "/start"   # sends the /start command (refresh); handled by CommandStart
BTN_GET_PDF = "📄 Get PDF / ፒዲኤፍ"
BTN_GET_SHOT = "🖼️ Get Screenshot / ስክሪንሾት"
BTN_WALLET = "My Balance / ቀሪ ሂሳብ"
BTN_PAYMENTS = "My Payments / ክፍያዎቼ"
BTN_PAY = "Add Payment / ክፍያ ጨምር"
BTN_FORGOT = "🔑 Forgot FAN / FIN"
BTN_HELP = "Help / እገዛ"
BTN_ADMIN = "🛠 Admin"

# Older/alternate labels that must still route to the same action (transition safety).
_ALIASES = {
    "📄 Get PDF": BTN_GET_PDF, "Get PDF": BTN_GET_PDF,
    "🖼 Get Screenshot": BTN_GET_SHOT, "🖼️ Get Screenshot": BTN_GET_SHOT, "Get Screenshot": BTN_GET_SHOT,
    "💳 My Wallet": BTN_WALLET, "My Balance": BTN_WALLET, "My Balance/Downloads": BTN_WALLET,
    "My Downloads": BTN_WALLET,
    "My Payments": BTN_PAYMENTS,
    "💵 Add Balance": BTN_PAY, "Add Payment": BTN_PAY, "Add Balance": BTN_PAY,
    "❓ Help": BTN_HELP, "Help": BTN_HELP,
}

BUTTONS = ({BTN_GET_PDF, BTN_GET_SHOT, BTN_WALLET, BTN_PAYMENTS, BTN_PAY,
            BTN_FORGOT, BTN_HELP, BTN_ADMIN} | set(_ALIASES.keys()))


def canonical(text: str) -> str:
    """Map any known (old or new) button label to its current canonical label."""
    return _ALIASES.get(text, text)


def main_kb(user_id) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_START)],
        [KeyboardButton(text=BTN_GET_PDF), KeyboardButton(text=BTN_GET_SHOT)],
        [KeyboardButton(text=BTN_WALLET), KeyboardButton(text=BTN_PAY)],
        [KeyboardButton(text=BTN_FORGOT)],
        [KeyboardButton(text=BTN_HELP)],
    ]   # BTN_PAYMENTS hidden for now (handler + alias kept so it still routes if tapped)
    if config.is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")]])


def format_kb() -> InlineKeyboardMarkup:
    """Per-download output choice, shown right after a FIN/FAN is entered.
    One format per download — 'Both' was removed on request."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 PDF", callback_data="dl:pdf"),
         InlineKeyboardButton(text="🖼 Screenshot", callback_data="dl:screenshot")],
        [InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")],
    ])
