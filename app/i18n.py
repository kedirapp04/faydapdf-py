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
               "📥 How to Download a PDF file\n"
               "1. Send a 12–16 digit FAN/FIN (or tap 📄 Get PDF / 🖼 Get Screenshot first).\n"
               "2. Choose 📄 PDF or 🖼 Screenshot.\n"
               "3. Enter the OTP — the file is sent after verification.\n\n"
               "💵 How to Top Up\n"
               "1. Tap Add Payment, then pay to the receiver name/phone shown by the bot.\n"
               "2. Send the Telebirr receipt link, redirect link, or the 10-character transaction number "
               "(or a screenshot of the receipt).\n"
               "3. If auto-check is on, your balance is added after verification; otherwise wait for admin approval.\n"
               "4. Use My Payments to see your top-up history, and My Balance to check your balance.\n\n"
               "🔑 Forgot FAN / FIN — recover your number by SMS (free)."),
        "am": ("❓ አጠቃቀም\n\n"
               "📥 PDF ፋይል እንዴት ማውረድ ይቻላል\n"
               "1. ባለ 12–16 ዲጂት FAN/FIN ይላኩ (ወይም መጀመሪያ 📄 Get PDF / 🖼 Get Screenshot ይጫኑ)።\n"
               "2. 📄 PDF ወይም 🖼 Screenshot ይምረጡ።\n"
               "3. OTP ያስገቡ — ማረጋገጫ ከተሳካ ፋይሉ ይላካል።\n\n"
               "💵 ቀሪ ብር እንዴት መሙላት ይቻላል\n"
               "1. Add Payment ይጫኑ፣ ቦቱ ለሚያሳየው የተቀባይ ስም/ስልክ ክፍያ ይላኩ።\n"
               "2. የTelebirr receipt link፣ redirect link ወይም 10-ቁምፊ transaction number ይላኩ (ወይም የደረሰኝ ስክሪንሾት)።\n"
               "3. Auto-check ከተከፈተ ብሩ ከተረጋገጠ በኋላ ይጨመራል፤ አለበለዚያ የአስተዳዳሪ ፍቃድ ይጠብቁ።\n"
               "4. የክፍያ ታሪክ ለማየት My Payments፣ ቀሪ ሂሳብ ለማየት My Balance ይጠቀሙ።\n\n"
               "🔑 Forgot FAN / FIN — ቁጥርዎን በSMS ያግኙ (ነፃ)።"),
    },
    "price_per_pdf": {"en": "💵 Price per download: {price}", "am": "💵 ዋጋ በአንድ ማውረድ፦ {price}"},
    "price_free": {"en": "🆓 Downloads are currently FREE.", "am": "🆓 ማውረድ አሁን ነፃ ነው።"},
    "no_payments": {"en": "🧾 You have no top-ups yet.", "am": "🧾 እስካሁን ምንም ክፍያ የለዎትም።"},
    "payments_header": {"en": "🧾 Your recent top-ups:", "am": "🧾 የቅርብ ጊዜ ክፍያዎችዎ፦"},
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
    "one_id": {"en": "🆔 {fan}", "am": "🆔 {fan}"},
    "n_ids": {"en": "📥 {n} IDs — I'll do them one by one.",
              "am": "📥 {n} መታወቂያዎች — በተራ አከናውናለሁ።"},
    "dropped_note": {"en": "(Only the first {max} are processed per message; {dropped} ignored.)",
                     "am": "(በአንድ መልዕክት የመጀመሪያዎቹ {max} ብቻ ይከናወናሉ፤ {dropped} ተትተዋል።)"},
    "otp_requesting": {"en": "📩 Requesting OTP for {fan}…", "am": "📩 ለ {fan} OTP እየተጠየቀ ነው…"},
    "otp_sent": {"en": "📩 A 6-digit code has been sent to your phone. Please enter it here.\n❌ Send 'cancel' or tap Cancel to stop.",
                 "am": "📩 የ6 ዲጂት ኮድ በስልክዎ ተልኳል። እባክዎ ቁጥሩን እዚህ ያስገቡ።\n❌ ለመሰረዝ 'cancel' ይላኩ ወይም Cancel ይጫኑ።"},
    "otp_sent_to": {"en": "📩 OTP sent to {phone}. Please enter the 6-digit code here.\n❌ Send 'cancel' or tap Cancel to stop.",
                    "am": "📩 ወደ {phone} OTP ተልኳል። እባክዎ የ6 ዲጂት ኮድ እዚህ ያስገቡ።\n❌ ለመሰረዝ 'cancel' ይላኩ ወይም Cancel ይጫኑ።"},
    "otp_send_fail": {"en": "⚠️ {error}\n\nSend the FIN again to retry.",
                      "am": "⚠️ {error}\n\nለመድገም FIN እንደገና ይላኩ።"},
    "otp_enter_numeric": {"en": "Send the numeric OTP code, or tap Cancel.",
                          "am": "የOTP ቁጥር ኮድ ይላኩ፣ ወይም Cancel ይጫኑ።"},
    "verifying": {"en": "⏳ Verifying OTP & generating your file… (a few seconds)",
                  "am": "⏳ OTP እየተረጋገጠ እና ፋይልዎ እየተዘጋጀ ነው… (ጥቂት ሰከንዶች)"},
    "processing_delivery": {"en": "✅ Verified! Processing delivery…",
                            "am": "✅ ተረጋግጧል! በማድረስ ላይ…"},
    "done": {"en": "✅ PDF sent successfully.", "am": "✅ PDF በተሳካ ሁኔታ ተልኳል።"},
    "done_free": {"en": "✅ Sent · free (system recovering).",
                  "am": "✅ ተልኳል · ነፃ (ሲስተሙ በማገገም ላይ)።"},
    "charged_prepaid": {"en": "💵 {charged} deducted · Balance: {balance}",
                        "am": "💵 {charged} ተቀናሽ · ቀሪ ሂሳብ፦ {balance}"},
    "charged_postpaid": {"en": "💵 {charged} charged · Balance: {balance}",
                         "am": "💵 {charged} ተከፍሏል · ቀሪ ሂሳብ፦ {balance}"},
    "charged_from_bonus": {"en": "🎁 {bonus_used} from bonus · Bonus left: {bonus_left}",
                           "am": "🎁 {bonus_used} ከጉርሻ · ቀሪ ጉርሻ፦ {bonus_left}"},
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
    "addbalance_header": {"en": "💵 Add Payment", "am": "💵 ክፍያ ጨምር"},
    "send_txn": {"en": ("Pay to the receiver shown above, then send ONE of these:\n"
                        "• the Telebirr receipt link or redirect link\n"
                        "• the 10-character transaction number (e.g. DGI70RYNL7)\n"
                        "📷 …or just send a screenshot of your Telebirr receipt — I'll read it.\n\n"
                        "If auto-check is on, your balance is added after verification; otherwise an admin will approve it."),
                 "am": ("ከላይ ለሚታየው ተቀባይ ክፍያ ይላኩ፣ ከዚያ ከእነዚህ አንዱን ይላኩ፦\n"
                        "• የTelebirr receipt link ወይም redirect link\n"
                        "• 10-ቁምፊ transaction number (ለምሳሌ DGI70RYNL7)\n"
                        "📷 …ወይም የTelebirr ደረሰኝ ስክሪንሾት ይላኩ — አነባለሁ።\n\n"
                        "Auto-check ከተከፈተ ብሩ ከተረጋገጠ በኋላ ይጨመራል፤ አለበለዚያ አስተዳዳሪ ያጸድቀዋል።")},
    "send_txn_short": {"en": "Send the transaction number (8–14 characters), a screenshot, or tap Cancel.",
                       "am": "የግብይት ቁጥር (8–14 ቁምፊ)፣ ስክሪንሾት ይላኩ፣ ወይም Cancel ይጫኑ።"},
    # Full trilingual Add-Payment message. {recv} = the receiver bullet lines (shown
    # at the top AND repeated at the bottom). Single-language slot → rendered once.
    "addpay_full": {"en": (
        "💵 Add Payment\n💵 ክፍያ ጨምር\n💵 Kaffaltii Dabaladhaa\n\n"
        "💳 Pay to this account / ወደዚህ አካውንት ይክፈሉ / Gara herrega kanaatti kaffalaa:\n{recv}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 English Instructions:\n"
        "Pay to the receiver shown above, then send ONE of these to confirm:\n"
        "📷 A screenshot of your Telebirr receipt (Auto-read is fully fixed and working!)\n"
        "• The 10-character transaction number (e.g. DGI70RYNL7)\n"
        "• The Telebirr receipt link or redirect link\n"
        "• The full Telebirr SMS you received from 127\n\n"
        "⏳ Your balance will be added automatically right after verification. "
        "If auto-check is off, an admin will approve it shortly.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 የአማርኛ መመሪያ:\n"
        "ከላይ ለሚታየው ተቀባይ ክፍያ ይላኩ፣ ከዚያ ለማረጋገጥ ከእነዚህ ውስጥ አንዱን ብቻ ይላኩ፦\n"
        "📷 የTelebirr ደረሰኝ ስክሪንሾት ይላኩ (አውቶማቲክ ማንበቢያው ሙሉ በሙሉ ተስተካክሏል!)\n"
        "• ባለ 10-ቁምፊ የትራንዛክሽን ቁጥር (ለምሳሌ DGI70RYNL7)\n"
        "• የTelebirr ደረሰኝ ሊንክ (Receipt ወይም Redirect link)\n"
        "• ከ127 የደረሰዎትን ሙሉ የTelebirr መልዕክት (SMS)\n\n"
        "⏳ አውቶማቲክ ማረጋገጫው (Auto-check) ሲያነበው ወዲያውኑ ሂሳብዎ (Balance) ላይ ይጨመራል፤ "
        "ካልሆነ ደግሞ በአስተዳዳሪ (Admin) በፍጥነት ይጸድቃል።\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 Qajeelfama Afaan Oromoo:\n"
        "Kaffaltii teessoo olitti argamuuf erga kaffaltanii booda, mirkaneessuuf kanneen gadii keessaa TOKKO QUFA ergaa:\n"
        "📷 Screenshot risiitii Telebirr keessanii ergaa (Dubbisaan auto-check guutummaatti sirreeffamee hojjechaa jira!)\n"
        "• Lakkoofsa tiraanzaakshinii qubee fi lakkoofsa 10 qabu (fkn, DGI70RYNL7)\n"
        "• Liinkii risiitii Telebirr (Receipt ykn Redirect link)\n"
        "• Ergaa Telebirr guutuu 127 irraa isiniif dhufe (SMS)\n\n"
        "⏳ Auto-check yoo hojjete erga mirkanaa'ee booda battalumatti balance keessan irratti ni dabalama; "
        "yoo ta'uu baate ammoo admin dafeen ni mirkaneessa.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💳 Receiver / ተቀባይ / Fudhataa:\n{recv}")},
    "checking_payment": {"en": "🔎 Checking your payment…", "am": "🔎 ክፍያዎ እየተረጋገጠ ነው…"},
    "reading_screenshot": {"en": "🔎 Reading your screenshot…", "am": "🔎 ስክሪንሾትዎ እየተነበበ ነው…"},
    "couldnt_read_txn": {"en": ("⚠️ Couldn't read the receipt automatically.\n"
                                "Please send the 10-character transaction number (e.g. DGI70RYNL7), "
                                "the Telebirr receipt link, or the full 127 SMS as text — "
                                "or try a clearer screenshot."),
                         "am": ("⚠️ ደረሰኙን በራስ-ሰር ማንበብ አልተቻለም።\n"
                                "እባክዎ ባለ 10-ቁምፊ transaction number (ለምሳሌ DGI70RYNL7)፣ "
                                "የTelebirr receipt link፣ ወይም ሙሉ የ127 SMS በጽሑፍ ይላኩ — "
                                "ወይም ግልጽ ስክሪንሾት ይሞክሩ።")},
    "image_read_fail": {"en": "⚠️ Couldn't read that image. Send the transaction number as text instead.",
                        "am": "⚠️ ያንን ምስል ማንበብ አልተቻለም። ይልቁንም የግብይት ቁጥሩን በጽሑፍ ይላኩ።"},
    "payments_unavailable": {"en": "⚙️ Payments are temporarily unavailable. Please try again shortly.",
                             "am": "⚙️ ክፍያዎች ለጊዜው አይሰሩም። እባክዎ ከጥቂት ጊዜ በኋላ ይሞክሩ።"},
    "verified_added": {"en": "✅ Verified! {amount} added.\nNew balance: {balance}.",
                       "am": "✅ ተረጋግጧል! {amount} ተጨምሯል።\nአዲስ ቀሪ ሂሳብ፦ {balance}።"},
    "autoverify_note": {"en": "✅ Payments are verified automatically — your balance is added instantly.",
                        "am": "✅ ክፍያዎች በራስ-ሰር ይረጋገጣሉ — ቀሪ ሂሳብዎ ወዲያውኑ ይጨመራል።"},
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
    "bonus_notify": {"en": "🎁 You received a {amount} bonus!\nBonus balance: {bonus} (used before your normal balance).",
                     "am": "🎁 {amount} ጉርሻ አግኝተዋል!\nየጉርሻ ቀሪ ሂሳብ፦ {bonus} (ከመደበኛ ሂሳብዎ በፊት ይውላል)።"},
    # wallet
    "wallet_header": {"en": "💳 Wallet — {mode}", "am": "💳 ቀሪ ሂሳብ — {mode}"},
    "wallet_balance": {"en": "Balance: {balance}", "am": "ቀሪ ሂሳብ፦ {balance}"},
    "wallet_bonus": {"en": "🎁 Bonus: {bonus} (used first)", "am": "🎁 ጉርሻ፦ {bonus} (መጀመሪያ ይውላል)"},
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
    # maintenance mode (admin-toggleable; custom text overrides this default)
    "maintenance_default": {
        "en": ("🛠 The bot is under maintenance right now while we fix a payment issue.\n"
               "Meanwhile you can download your Fayda ID for FREE at @nid_downloader_free_bot.\n"
               "Thanks for your patience! 🙏"),
        "am": ("🛠 ቦቱ የክፍያ ችግር እየተስተካከለ ስለሆነ አሁን በጥገና ላይ ነው።\n"
               "እስከዚያ ድረስ የፋይዳ መታወቂያዎን በነጻ ከ @nid_downloader_free_bot ማውረድ ይችላሉ።\n"
               "ስለ ትዕግስትዎ እናመሰግናለን! 🙏"),
    },
}
