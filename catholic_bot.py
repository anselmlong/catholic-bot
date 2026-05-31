#!/usr/bin/env python3
"""catholic-bot — daily mass readings + random bible verse + subscribe."""

import json
import logging
import os
import subprocess
import tempfile
from datetime import date
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes,
)

SGT = ZoneInfo("Asia/Singapore")
TOKEN = "REVOKED_TOKEN"
SUBS_FILE = "/home/ubuntu/catholic-bot/subscriptions.json"
PYTHON = "/home/ubuntu/confessit-scraper/venv/bin/python3"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── persistent keyboard ─────────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📖 Today", "🙏 Truth"],
        ["⚙️ Subscribe", "ℹ️ Help"],
    ],
    resize_keyboard=True,
)


# ── subscriptions ───────────────────────────────────────────────────────────
def load_subs() -> dict:
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_subs(subs: dict):
    with open(SUBS_FILE, "w") as f:
        json.dump(subs, f, indent=2)


def default_prefs():
    return {"readings": "full", "truth": True, "time": "06:00"}


# ── readings ────────────────────────────────────────────────────────────────
def fetch_readings(d: date | None = None) -> dict | None:
    if d is None:
        d = date.today()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            [PYTHON, "-m", "catholic_mass_readings", "get-mass",
             "--date", d.isoformat(), "--type", "DEFAULT", "--save", tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            log.warning(f"CLI failed for {d}: {proc.stderr[:200]}")
            return None
        with open(tmp_path) as f:
            raw = json.load(f)
        sections = raw.get("sections", [])
        curated = []
        for sec in sections:
            if sec.get("type") == "ALLELUIA":
                continue
            for r in sec.get("readings", []):
                verses = r.get("verses", [])
                citation = verses[0]["text"] if verses else ""
                text = r.get("text", "").strip()
                for phrase in [
                    "The word of the Lord.", "Thanks be to God.",
                    "The Gospel of the Lord.", "Praise to you, Lord Jesus Christ.",
                ]:
                    text = text.replace(phrase, "")
                curated.append({
                    "header": sec.get("header", ""),
                    "citation": citation,
                    "text": text.strip(),
                })
        return {"title": raw.get("title", "Daily Mass Readings"), "date": d.isoformat(), "readings": curated}
    except Exception as e:
        log.error(f"fetch_readings error: {e}")
        return None
    finally:
        subprocess.run(["rm", "-f", tmp_path])


def format_readings(data: dict, mode: str = "full") -> str:
    sections = data.get("readings", [])
    title = data.get("title", "Daily Mass Readings")
    date_str = data.get("date", date.today().isoformat())

    if mode == "gospel":
        gospel = next((s for s in sections if s["header"] == "Gospel"), None)
        if gospel:
            return f"✝️ *Gospel of the Day* — {gospel['citation']}\n\n{gospel['text']}"
        return "No gospel reading found."

    parts = [f"📖 *{title}*", f"🗓 {date_str}\n"]
    for s in sections:
        icon = "✝️" if s["header"] == "Gospel" else "🎵" if s["header"] == "Responsorial Psalm" else "📜"
        parts.append(f"{icon} *{s['header']}* — {s['citation']}")
        parts.append(f"{s['text']}\n")
    return "\n".join(parts)


# ── truth ───────────────────────────────────────────────────────────────────
async def fetch_truth() -> str | None:
    tries = [
        ("https://labs.bible.org/api/", {"passage": "random", "type": "json"}),
        ("https://bible-api.com/", {"random": "verse"}),
    ]
    for url, params in tries:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                if r.status_code != 200:
                    continue
                data = r.json()
                if isinstance(data, list) and data:
                    v = data[0]
                    return f'*{v["bookname"]} {v["chapter"]}:{v["verse"]}*\n\n{v["text"]}'
                if isinstance(data, dict) and "reference" in data:
                    return f'*{data["reference"]}*\n\n{data["text"]}'
        except Exception:
            continue
    return None


# ── daily push ──────────────────────────────────────────────────────────────
async def deliver_daily(app, chat_id: int, prefs: dict):
    data = fetch_readings()
    if data:
        text = format_readings(data, mode=prefs.get("readings", "full"))
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    else:
        await app.bot.send_message(chat_id=chat_id, text="Couldn't fetch today's readings. 🙏")

    if prefs.get("truth", True):
        verse = await fetch_truth()
        if verse:
            await app.bot.send_message(chat_id=chat_id, text=f"*Truth for the day:*\n\n{verse}", parse_mode="Markdown")


async def daily_push(context: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    from datetime import datetime
    current_time = datetime.now(SGT).strftime("%H:%M")
    for chat_id_str, prefs in subs.items():
        pref_time = prefs.get("time", "06:00")
        if pref_time == current_time:
            await deliver_daily(context.application, int(chat_id_str), prefs)


# ── callback: subscribe menu ────────────────────────────────────────────────
async def sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    subs = load_subs()
    key = str(chat_id)

    if data == "sub_readings_full":
        subs.setdefault(key, default_prefs())["readings"] = "full"
    elif data == "sub_readings_gospel":
        subs.setdefault(key, default_prefs())["readings"] = "gospel"
    elif data == "sub_truth_on":
        subs.setdefault(key, default_prefs())["truth"] = True
    elif data == "sub_truth_off":
        subs.setdefault(key, default_prefs())["truth"] = False
    elif data == "sub_time_6":
        subs.setdefault(key, default_prefs())["time"] = "06:00"
    elif data == "sub_time_7":
        subs.setdefault(key, default_prefs())["time"] = "07:00"
    elif data == "sub_time_8":
        subs.setdefault(key, default_prefs())["time"] = "08:00"
    elif data == "sub_cancel":
        await query.edit_message_text("Subscription cancelled. No changes made.", reply_markup=MAIN_KEYBOARD)
        return
    elif data == "sub_confirm":
        prefs = subs.get(key, default_prefs())
        save_subs(subs)
        summary = (
            f"✅ *Subscribed!*\n\n"
            f"Readings: {'Full' if prefs['readings'] == 'full' else 'Gospel only'}\n"
            f"Daily truth: {'Yes' if prefs['truth'] else 'No'}\n"
            f"Time: {prefs['time']} SGT"
        )
        await query.edit_message_text(summary, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        data = fetch_readings()
        if data:
            await query.message.reply_text(
                format_readings(data, mode=prefs["readings"]), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD
            )
        return

    save_subs(subs)
    prefs = subs.get(key, default_prefs())

    def btn(label, active, cbd):
        return InlineKeyboardButton(f"{'✓' if active else '○'} {label}", callback_data=cbd)

    kb = [
        [btn("Full Readings", prefs['readings'] == 'full', "sub_readings_full"),
         btn("Gospel Only", prefs['readings'] == 'gospel', "sub_readings_gospel")],
        [btn("Include Truth", prefs['truth'], "sub_truth_on"),
         btn("No Truth", not prefs['truth'], "sub_truth_off")],
        [
            InlineKeyboardButton(f"{'🕐' if prefs['time'] == '06:00' else '  '} 6am", callback_data="sub_time_6"),
            InlineKeyboardButton(f"{'🕐' if prefs['time'] == '07:00' else '  '} 7am", callback_data="sub_time_7"),
            InlineKeyboardButton(f"{'🕐' if prefs['time'] == '08:00' else '  '} 8am", callback_data="sub_time_8"),
        ],
        [InlineKeyboardButton("✅ Confirm", callback_data="sub_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="sub_cancel")],
    ]
    await query.edit_message_text(
        "Customize your daily delivery:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── handlers ────────────────────────────────────────────────────────────────
async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🙏 *Catholic Daily Bot*\n\n"
        "Daily mass readings and Bible verses, right here.\n\n"
        "📖 `/today` — today's mass readings\n"
        "🙏 `/truth` — random Bible verse\n"
        "⚙️ `/subscribe` — daily push at your preferred time\n"
        "ℹ️ `/help` — all commands\n\n"
        "Use the buttons below to get started!",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_cmd(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "`/today` — Today's mass readings (Reading 1, Psalm, Reading 2, Gospel)\n"
        "`/truth` — Random Bible verse from anywhere in scripture\n"
        "`/subscribe` — Set up daily push at 6/7/8am SGT\n"
        "   • Pick full readings or gospel-only\n"
        "   • Toggle daily truth on/off\n"
        "`/unsubscribe` — Stop daily readings\n"
        "`/mysub` — See your current preferences\n"
        "`/help` — This message\n\n"
        "*Data sources:*\n"
        "📜 Readings: USCCB (United States Conference of Catholic Bishops)\n"
        "📖 Verses: NET Bible via labs.bible.org\n\n"
        "Daily push runs at your chosen time (Singapore time).",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def subscribe(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    subs = load_subs()
    key = str(chat_id)
    prefs = subs.get(key, default_prefs())

    def btn(label, active, cbd):
        return InlineKeyboardButton(f"{'✓' if active else '○'} {label}", callback_data=cbd)

    kb = [
        [btn("Full Readings", prefs['readings'] == 'full', "sub_readings_full"),
         btn("Gospel Only", prefs['readings'] == 'gospel', "sub_readings_gospel")],
        [btn("Include Truth", prefs['truth'], "sub_truth_on"),
         btn("No Truth", not prefs['truth'], "sub_truth_off")],
        [
            InlineKeyboardButton(f"{'🕐' if prefs['time'] == '06:00' else '  '} 6am", callback_data="sub_time_6"),
            InlineKeyboardButton(f"{'🕐' if prefs['time'] == '07:00' else '  '} 7am", callback_data="sub_time_7"),
            InlineKeyboardButton(f"{'🕐' if prefs['time'] == '08:00' else '  '} 8am", callback_data="sub_time_8"),
        ],
        [InlineKeyboardButton("✅ Confirm", callback_data="sub_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="sub_cancel")],
    ]
    await update.message.reply_text(
        "Customize your daily delivery:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def unsubscribe(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    key = str(update.message.chat_id)
    if key in subs:
        del subs[key]
        save_subs(subs)
        await update.message.reply_text("Unsubscribed. No more daily readings.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text("You're not subscribed.", reply_markup=MAIN_KEYBOARD)


async def mysub(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    prefs = subs.get(str(update.message.chat_id))
    if not prefs:
        await update.message.reply_text("Not subscribed. Use `/subscribe` to start.", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return
    await update.message.reply_text(
        f"📋 *Your Subscription*\n\n"
        f"Readings: {'Full' if prefs['readings'] == 'full' else 'Gospel only'}\n"
        f"Daily truth: {'Yes' if prefs['truth'] else 'No'}\n"
        f"Time: {prefs.get('time', '06:00')} SGT",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def today(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📖 fetching...")
    data = fetch_readings()
    if not data:
        await msg.edit_text("Couldn't reach USCCB right now. Try again in a bit. 🙏")
        return
    await msg.edit_text(format_readings(data), parse_mode="Markdown")


async def truth(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🙏 one sec...")
    verse = await fetch_truth()
    if not verse:
        await msg.edit_text("Couldn't fetch a verse. Try again later 🙏")
        return
    await msg.edit_text(verse, parse_mode="Markdown")


# ── keyboard button router ──────────────────────────────────────────────────
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📖 Today":
        await today(update, context)
    elif text == "🙏 Truth":
        await truth(update, context)
    elif text == "⚙️ Subscribe":
        await subscribe(update, context)
    elif text == "ℹ️ Help":
        await help_cmd(update, context)


# ── main ────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("truth", truth))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("mysub", mysub))
    app.add_handler(CallbackQueryHandler(sub_callback, pattern="^sub_"))
    app.add_handler(MessageHandler(filters.Text(["📖 Today", "🙏 Truth", "⚙️ Subscribe", "ℹ️ Help"]), handle_buttons))

    jq = app.job_queue
    jq.run_repeating(daily_push, interval=60, first=10, name="daily_check")

    log.info("starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()