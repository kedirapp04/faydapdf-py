from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from .. import config

BTN_DOWNLOAD = "📥 Download PDF"
BTN_WALLET = "💳 My Wallet"
BTN_PAY = "💵 Add Balance"
BTN_FORGOT = "🔑 Forgot FAN / FIN"
BTN_HELP = "❓ Help"
BTN_ADMIN = "🛠 Admin"

BUTTONS = {BTN_DOWNLOAD, BTN_WALLET, BTN_PAY, BTN_FORGOT, BTN_HELP, BTN_ADMIN}


def main_kb(user_id) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_DOWNLOAD)],
        [KeyboardButton(text=BTN_WALLET), KeyboardButton(text=BTN_PAY)],
        [KeyboardButton(text=BTN_FORGOT), KeyboardButton(text=BTN_HELP)],
    ]
    if config.is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")]])
