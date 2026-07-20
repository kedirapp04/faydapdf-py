"""Broadcast merge tokens: {name}, {balance}, {bonus}, {username}, {id} with an
optional fallback via {token|fallback}. Values are escaped for the message's parse
mode so a user's name can't break HTML/Markdown formatting.
"""
import re

from . import billing

_TOKEN = re.compile(r"\{([a-zA-Z_]+)(?:\|([^}]*))?\}")


def _esc(v: str, parse_mode: str | None) -> str:
    v = str(v)
    if parse_mode == "HTML":
        return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if parse_mode in ("Markdown", "MarkdownV2"):
        return re.sub(r"([_*`\[\]])", r"\\\1", v)
    return v


def render(text: str, user: dict, parse_mode: str | None = None) -> str:
    """Fill merge tokens for one user. Unknown tokens keep their fallback (or empty)."""
    if not text or "{" not in text:
        return text
    name = user.get("first_name") or user.get("username") or ""
    values = {
        "name": name, "firstname": name, "fullname": name,
        "username": user.get("username") or "",
        "id": str(user.get("telegram_id") or ""),
        "balance": billing.birr(user.get("balance_cents") or 0),
        "bonus": billing.birr(user.get("bonus_balance_cents") or 0),
    }
    defaults = {"name": "there", "firstname": "there", "fullname": "there", "username": "friend"}

    def sub(m):
        key = m.group(1).lower()
        fb = m.group(2)
        val = values.get(key)
        if not val:
            val = fb if fb is not None else defaults.get(key, "")
        return _esc(val, parse_mode)

    return _TOKEN.sub(sub, text)
