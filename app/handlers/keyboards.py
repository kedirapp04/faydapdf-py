from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from .. import config

BTN_GET_PDF = "📄 Get PDF"
BTN_GET_SHOT = "🖼 Get Screenshot"
BTN_WALLET = "💳 My Wallet"
BTN_PAY = "💵 Add Balance"
BTN_FORGOT = "🔑 Forgot FAN / FIN"
BTN_HELP = "❓ Help"
BTN_ADMIN = "🛠 Admin"

BUTTONS = {BTN_GET_PDF, BTN_GET_SHOT, BTN_WALLET, BTN_PAY, BTN_FORGOT, BTN_HELP, BTN_ADMIN}


def main_kb(user_id) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_GET_PDF), KeyboardButton(text=BTN_GET_SHOT)],
        [KeyboardButton(text=BTN_WALLET), KeyboardButton(text=BTN_PAY)],
        [KeyboardButton(text=BTN_FORGOT), KeyboardButton(text=BTN_HELP)],
    ]
    if config.is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")]])


def format_kb() -> InlineKeyboardMarkup:
    """Per-download output choice, shown right after a FIN/FAN is entered."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 PDF", callback_data="dl:pdf"),
         InlineKeyboardButton(text="🖼 Screenshot", callback_data="dl:screenshot")],
        [InlineKeyboardButton(text="📦 Both", callback_data="dl:both")],
        [InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")],
    ])
