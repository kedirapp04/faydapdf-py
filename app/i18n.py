"""Bilingual (English + Amharic) message catalog — ported from
faydapdf-railway/language.js. Every user-facing message is rendered in
RENDER_ORDER (default "en,am"), showing both languages, de-duplicating identical
lines. Configure the order/set with the MESSAGE_LANGS env var (e.g. "am,en" or
"en"). Amharic strings are reused from faydapdf-railway where they map.

Usage:  from .. import i18n ;  await m.answer(i18n.t("welcome"))
        i18n.t("otp_sent", tail="1234", phone="+2519****")
Admin-facing text stays English (admins are technical).
"""
import os

SUPPORTED = ("en", "am")
RENDER_ORDER = [c.strip().lower() for c in os.getenv("MESSAGE_LANGS", "en,am").split(",")
                if c.strip().lower() in SUPPORTED] or ["en", "am"]


def _interp(text: str, params: dict) -> str:
    out = str(text if text is not None else "")
    for k, v in (params or {}).items():
        out = out.replace("{" + k + "}", str(v if v is not None else ""))
    return out


def t(key: str, **params) -> str:
    entry = CATALOG.get(key)
    if not entry:
        return f"[missing: {key}]"
    seen, parts = set(), []
    for code in RENDER_ORDER:
        raw = entry.get(code)
        if not raw:
            continue
        rendered = _interp(raw, params)
        norm = rendered.strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        parts.append(rendered)
    if not parts:
        return _interp(entry.get("en") or f"[empty: {key}]", params)
    return "\n".join(parts)


CATALOG = {
    "welcome": {
        "en": "👋 Welcome!\nSend a 12–16 digit FIN/FAN, choose 📄 PDF or 🖼 Screenshot, then enter the OTP.\n(Or tap 📄 Get PDF / 🖼 Get Screenshot first to skip the choice.)",
        "am": "👋 እንኳን ደህና መጡ!\nባለ 12–16 ዲጂት FAN/FIN ይላኩ፣ 📄 PDF ወይም 🖼 Screenshot ይምረጡ፣ ከዚያም OTP ያስገቡ።\n(ወይም መጀመሪያ 📄 Get PDF / 🖼 Get Screenshot ይጫኑ።)",
    },
    "help": {
        "en": ("❓ How to use\n\n"
               "📥 Send a 12–16 digit FIN/FAN → pick 📄 PDF / 🖼 Screenshot / 📦 Both → enter the OTP → get it.\n"
               "📄 Get PDF / 🖼 Get Screenshot — pre-pick the output, then just send FIN/FANs.\n"
               "🖼 Screenshot = the card images (front, back, photo + QR).\n"
               "💳 My Wallet — your balance / billing.\n"
               "💵 Add Balance — send a payment receipt (text or screenshot) for approval.\n"
               "🔑 Forgot FAN / FIN — recover your number by SMS (free)."),
        "am": ("❓ አጠቃቀም\n\n"
               "📥 ባለ 12–16 ዲጂት FAN/FIN ይላኩ → 📄 PDF / 🖼 Screenshot / 📦 ሁለቱም ይምረጡ → OTP ያስገቡ → ይቀበሉ።\n"
               "📄 Get PDF / 🖼 Get Screenshot — ውጤቱን አስቀድመው ይምረጡ፣ ከዚያ FAN/FIN ብቻ ይላኩ።\n"
               "🖼 Screenshot = የካርድ ምስሎች (ፊት፣ ጀርባ፣ ፎቶ + QR)።\n"
               "💳 My Wallet — ቀሪ ሂሳብዎ።\n"
               "💵 Add Balance — የክፍያ ደረሰኝ (ጽሑፍ ወይም ስክሪንሾት) ይላኩ።\n"
               "🔑 Forgot FAN / FIN — ቁጥርዎን በSMS ያግኙ (ነፃ)።"),
    },
    "cancelled": {"en": "✖️ Cancelled.", "am": "✖️ ተሰርዟል።"},
    "blocked": {"en": "🚫 Your access is blocked. Contact the admin.",
                "am": "🚫 መዳረሻዎ ታግዷል። አስተዳዳሪውን ያነጋግሩ።"},
    "unavailable": {"en": "⚙️ Temporarily unavailable — please try again shortly.",
                    "am": "⚙️ ለጊዜው አይሰራም — እባክዎ ከጥቂት ጊዜ በኋላ ይሞክሩ።"},
    "system_unavailable": {"en": "⚙️ The system is temporarily unavailable. Please try again shortly.",
                           "am": "⚙️ ሲስተሙ ለጊዜው ስራ አቁሟል። እባክዎ ከጥቂት ጊዜ በኋላ ይሞክሩ።"},
    "send_fan": {"en": "Send a 12–16 digit FIN/FAN to download its Fayda PDF.",
                 "am": "የFayda PDF ለማውረድ ባለ 12–16 ዲጂት FAN/FIN ይላኩ።"},
    "send_fan_or_cancel": {"en": "Send a 12–16 digit FIN/FAN, or tap Cancel.",
                           "am": "ባለ 12–16 ዲጂት FAN/FIN ይላኩ፣ ወይም Cancel ይጫኑ።"},
    "get_pdf_prompt": {"en": "📄 Send a 12–16 digit FIN/FAN — you'll get its PDF.",
                       "am": "📄 ባለ 12–16 ዲጂት FAN/FIN ይላኩ — PDF ያገኛሉ።"},
    "get_shot_prompt": {"en": "🖼 Send a 12–16 digit FIN/FAN — you'll get its screenshots (front, back, photo + QR).",
                        "am": "🖼 ባለ 12–16 ዲጂት FAN/FIN ይላኩ — ስክሪንሾቶች ያገኛሉ (ፊት፣ ጀርባ፣ ፎቶ + QR)።"},
    "choose_output": {"en": "Choose the output for this download:",
                      "am": "ለዚህ ማውረድ ውጤቱን ይምረጡ፦"},
    "one_id": {"en": "🆔 …{tail}", "am": "🆔 …{tail}"},
    "n_ids": {"en": "📥 {n} IDs — I'll do them one by one.",
              "am": "📥 {n} መታወቂያዎች — በተራ አከናውናለሁ።"},
    "dropped_note": {"en": "(Only the first {max} are processed per message; {dropped} ignored.)",
                     "am": "(በአንድ መልዕክት የመጀመሪያዎቹ {max} ብቻ ይከናወናሉ፤ {dropped} ተትተዋል።)"},
    "otp_requesting": {"en": "📩 Requesting OTP for …{tail}…", "am": "📩 ለ…{tail} OTP እየተጠየቀ ነው…"},
    "otp_sent": {"en": "📨 OTP sent for …{tail}.\nEnter the code you received:",
                 "am": "📨 ለ…{tail} OTP ተልኳል።\nየደረሰዎትን ኮድ ያስገቡ፦"},
    "otp_sent_to": {"en": "📨 OTP sent to {phone} for …{tail}.\nEnter the code you received:",
                    "am": "📨 ወደ {phone} ለ…{tail} OTP ተልኳል።\nየደረሰዎትን ኮድ ያስገቡ፦"},
    "otp_send_fail": {"en": "⚠️ {error}\n\nSend the FIN again to retry.",
                      "am": "⚠️ {error}\n\nለመድገም FIN እንደገና ይላኩ።"},
    "otp_enter_numeric": {"en": "Send the numeric OTP code, or tap Cancel.",
                          "am": "የOTP ቁጥር ኮድ ይላኩ፣ ወይም Cancel ይጫኑ።"},
    "verifying": {"en": "⏳ Verifying OTP & generating your document… (a few seconds)",
                  "am": "⏳ OTP እየተረጋገጠ እና ሰነድዎ እየተዘጋጀ ነው… (ጥቂት ሰከንዶች)"},
    "processing_delivery": {"en": "✅ Verified! Processing delivery…",
                            "am": "✅ ተረጋግጧል! በማድረስ ላይ…"},
    "done": {"en": "✅ Done.", "am": "✅ ተጠናቀቀ።"},
    "done_free": {"en": "✅ Done. · free (system recovering)",
                  "am": "✅ ተጠናቀቀ። · ነፃ (ሲስተሙ በማገገም ላይ)"},
    "id_in_progress": {"en": "⏳ That ID is already being processed — please wait a moment.",
                       "am": "⏳ ይህ መታወቂያ አስቀድሞ በሂደት ላይ ነው — እባክዎ ትንሽ ይጠብቁ።"},
    "recovering_free": {"en": "⚠️ System is recovering — this download is free and won't be recorded.",
                        "am": "⚠️ ሲስተሙ በማገገም ላይ ነው — ይህ ማውረድ ነፃ ነው እና አይመዘገብም።"},
    "paused": {"en": "⏸ The service is paused for maintenance. Please try again later.",
               "am": "⏸ አገልግሎቱ ለጥገና ቆሟል። እባክዎ ቆየት ብለው ይሞክሩ።"},
    # forgot-FAN
    "forgot_name": {"en": "🔑 Forgot FAN/FIN — free.\n\nSend your FULL NAME (e.g. Abebe Kebede Alemu):",
                    "am": "🔑 FAN/FIN ረሱ — ነፃ።\n\nሙሉ ስምዎን ይላኩ (ለምሳሌ፦ አበበ ከበደ አለሙ)፦"},
    "forgot_need_fullname": {"en": "Please send your FULL name (e.g. Abebe Kebede Alemu), or tap Cancel.",
                             "am": "እባክዎ ሙሉ ስምዎን ይላኩ (ለምሳሌ፦ አበበ ከበደ አለሙ)፣ ወይም Cancel ይጫኑ።"},
    "forgot_phone": {"en": "📱 Now send your REGISTERED phone number (e.g. 0911223344):",
                     "am": "📱 አሁን የተመዘገበ ስልክ ቁጥርዎን ይላኩ (ለምሳሌ፦ 0911223344)፦"},
    "forgot_bad_phone": {"en": "Send a valid Ethiopian phone (e.g. 0911223344), or tap Cancel.",
                         "am": "ትክክለኛ የኢትዮጵያ ስልክ ይላኩ (ለምሳሌ፦ 0911223344)፣ ወይም Cancel ይጫኑ።"},
    "forgot_requesting": {"en": "📩 Requesting your FAN + FIN by SMS…",
                          "am": "📩 FAN + FIN በSMS እየተጠየቀ ነው…"},
    "forgot_done": {"en": "✅ Done. Your FAN and FIN were sent by SMS to {phone}.",
                    "am": "✅ ተጠናቀቀ። FAN እና FIN በSMS ወደ {phone} ተልኳል።"},
    "forgot_err": {"en": "⚠️ {error}", "am": "⚠️ {error}"},
    # add-balance
    "addbalance_header": {"en": "💵 Add Balance", "am": "💵 ሂሳብ ይሙሉ"},
    "send_txn": {"en": "Then send the transaction number here (Telebirr or CBE, e.g. DGI70RYNL7)\n📷 …or just send a screenshot of your Telebirr receipt — I'll read it.",
                 "am": "ከዚያ የግብይት ቁጥሩን እዚህ ይላኩ (Telebirr ወይም CBE፣ ለምሳሌ DGI70RYNL7)\n📷 …ወይም የTelebirr ደረሰኝ ስክሪንሾት ይላኩ — አነባለሁ።"},
    "send_txn_short": {"en": "Send the transaction number (8–14 characters), a screenshot, or tap Cancel.",
                       "am": "የግብይት ቁጥር (8–14 ቁምፊ)፣ ስክሪንሾት ይላኩ፣ ወይም Cancel ይጫኑ።"},
    "checking_payment": {"en": "🔎 Checking your payment…", "am": "🔎 ክፍያዎ እየተረጋገጠ ነው…"},
    "reading_screenshot": {"en": "🔎 Reading your screenshot…", "am": "🔎 ስክሪንሾትዎ እየተነበበ ነው…"},
    "couldnt_read_txn": {"en": "⚠️ Couldn't read the transaction number.\nSend it as text, or a clearer screenshot.",
                         "am": "⚠️ የግብይት ቁጥሩን ማንበብ አልተቻለም።\nበጽሑፍ ይላኩ፣ ወይም ግልጽ ስክሪንሾት።"},
    "image_read_fail": {"en": "⚠️ Couldn't read that image. Send the transaction number as text instead.",
                        "am": "⚠️ ያንን ምስል ማንበብ አልተቻለም። ይልቁንም የግብይት ቁጥሩን በጽሑፍ ይላኩ።"},
    "payments_unavailable": {"en": "⚙️ Payments are temporarily unavailable. Please try again shortly.",
                             "am": "⚙️ ክፍያዎች ለጊዜው አይሰሩም። እባክዎ ከጥቂት ጊዜ በኋላ ይሞክሩ።"},
    "verified_added": {"en": "✅ Verified! {amount} added.\nNew balance: {balance}.",
                       "am": "✅ ተረጋግጧል! {amount} ተጨምሯል።\nአዲስ ቀሪ ሂሳብ፦ {balance}።"},
    "already_submitted": {"en": "This receipt was already submitted (status: {status}).",
                          "am": "ይህ ደረሰኝ አስቀድሞ ቀርቧል (ሁኔታ፦ {status})።"},
    "receipt_submitted": {"en": "✅ Receipt submitted (#{id}). An admin will review it shortly.",
                          "am": "✅ ደረሰኝ ቀርቧል (#{id})። አስተዳዳሪ በቅርቡ ይገመግማል።"},
    "approved_notify": {"en": "✅ Your payment was approved. {amount} added.\nNew balance: {balance}.",
                        "am": "✅ ክፍያዎ ጸድቋል። {amount} ተጨምሯል።\nአዲስ ቀሪ ሂሳብ፦ {balance}።"},
    "rejected_notify": {"en": "🚫 Your payment was rejected. Please check the receipt and resubmit.",
                        "am": "🚫 ክፍያዎ ውድቅ ተደርጓል። እባክዎ ደረሰኙን አረጋግጠው እንደገና ያቅርቡ።"},
    "credited_notify": {"en": "💵 {amount} was added to your balance by the admin.\nNew balance: {balance}.",
                        "am": "💵 {amount} በአስተዳዳሪው ወደ ቀሪ ሂሳብዎ ተጨምሯል።\nአዲስ ቀሪ ሂሳብ፦ {balance}።"},
    # wallet
    "wallet_header": {"en": "💳 Wallet — {mode}", "am": "💳 ቀሪ ሂሳብ — {mode}"},
    "wallet_balance": {"en": "Balance: {balance}", "am": "ቀሪ ሂሳብ፦ {balance}"},
    "wallet_owed": {"en": "Owed: {owed} / {limit}", "am": "ዕዳ፦ {owed} / {limit}"},
    "wallet_price": {"en": "Price per download: {price}", "am": "ዋጋ በአንድ ማውረድ፦ {price}"},
    # billing gate reasons (used by services/billing.py)
    "reason_insufficient": {"en": "Insufficient balance (need {need}, have {have}). Please top up.",
                            "am": "ቀሪ ሂሳብ በቂ አይደለም ({need} ያስፈልጋል፣ {have} አለዎት)። እባክዎ ይሙሉ።"},
    "reason_postpaid_limit": {"en": "Postpaid credit limit / balance reached (need {need}). Please top up.",
                              "am": "የክሬዲት ገደብ / ቀሪ ሂሳብ ተሟጧል ({need} ያስፈልጋል)። እባክዎ ይሙሉ።"},
    "reason_total_limit": {"en": "Total limit reached for this account.",
                           "am": "የዚህ መለያ አጠቃላይ ገደብ ተሟልቷል።"},
    "reason_daily_limit": {"en": "Daily limit reached. Try again tomorrow.",
                           "am": "የቀኑ ገደብ ተሟልቷል። ነገ እንደገና ይሞክሩ።"},
    "gate_refused": {"en": "🚫 {reason}", "am": "🚫 {reason}"},
}
