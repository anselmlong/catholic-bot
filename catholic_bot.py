#!/usr/bin/env python3
"""catholic-bot — daily mass readings + random bible verse + subscribe."""

import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes,
)

SGT = ZoneInfo("Asia/Singapore")
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("ERROR: BOT_TOKEN not set in .env", file=sys.stderr)
    sys.exit(1)
SUBS_FILE = os.path.join(os.path.dirname(__file__), "subscriptions.json")
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
        from datetime import datetime
        d = datetime.now(SGT).date()
    for attempt in range(3):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                [PYTHON, "-m", "catholic_mass_readings", "get-mass",
                 "--date", d.isoformat(), "--type", "DEFAULT", "--save", tmp_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                log.warning(f"CLI failed for {d} (attempt {attempt + 1}): {proc.stderr[:200]}")
                if attempt < 2:
                    time.sleep(3 + attempt * 3)
                    continue
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
            log.error(f"fetch_readings error (attempt {attempt + 1}): {e}")
            if attempt < 2:
                time.sleep(3 + attempt * 3)
                continue
            return None
        finally:
            subprocess.run(["rm", "-f", tmp_path])
    return None


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


# ── truth (curated verses) ─────────────────────────────────────────────────
TRUTHS = {
    "peace": [
        ('"Do not be anxious about anything, but in every situation, by prayer and petition, with thanksgiving, present your requests to God. And the peace of God, which transcends all understanding, will guard your hearts and your minds in Christ Jesus."', "Philippians 4:6-7"),
        ('"So do not fear, for I am with you; do not be dismayed, for I am your God. I will strengthen you and help you; I will uphold you with my righteous right hand."', "Isaiah 41:10"),
        ('"Peace I leave with you; my peace I give you. I do not give to you as the world gives. Do not let your hearts be troubled and do not be afraid."', "John 14:27"),
        ('"I sought the Lord, and he answered me; he delivered me from all my fears."', "Psalm 34:4"),
        ('"Therefore do not worry about tomorrow, for tomorrow will worry about itself. Each day has enough trouble of its own."', "Matthew 6:34"),
    ],
    "strength": [
        ('"Be strong and courageous. Do not be afraid; do not be discouraged, for the Lord your God will be with you wherever you go."', "Joshua 1:9"),
        ('"But those who hope in the Lord will renew their strength. They will soar on wings like eagles; they will run and not grow weary, they will walk and not be faint."', "Isaiah 40:31"),
        ('"Be strong and courageous. Do not be afraid or terrified because of them, for the Lord your God goes with you; he will never leave you nor forsake you."', "Deuteronomy 31:6"),
        ('"The Lord is my light and my salvation — whom shall I fear? The Lord is the stronghold of my life — of whom shall I be afraid?"', "Psalm 27:1"),
        ('"For God has not given us a spirit of fear, but of power and of love and of a sound mind."', "2 Timothy 1:7"),
    ],
    "hope": [
        ('"For I know the plans I have for you," declares the Lord, "plans to prosper you and not to harm you, plans to give you hope and a future."', "Jeremiah 29:11"),
        ('"May the God of hope fill you with all joy and peace as you trust in him, so that you may overflow with hope by the power of the Holy Spirit."', "Romans 15:13"),
        ('"Yes, my soul, find rest in God; my hope comes from him."', "Psalm 62:5"),
        ('"Because of the Lord\'s great love we are not consumed, for his compassions never fail. They are new every morning; great is your faithfulness."', "Lamentations 3:22-23"),
        ('"And we know that in all things God works for the good of those who love him, who have been called according to his purpose."', "Romans 8:28"),
    ],
    "love": [
        ('"Love is patient, love is kind. It does not envy, it does not boast, it is not proud. It does not dishonor others, it is not self-seeking, it is not easily angered, it keeps no record of wrongs."', "1 Corinthians 13:4-5"),
        ('"We love because he first loved us."', "1 John 4:19"),
        ('"For God so loved the world that he gave his one and only Son, that whoever believes in him shall not perish but have eternal life."', "John 3:16"),
        ('"For I am convinced that neither death nor life, neither angels nor demons, neither the present nor the future, nor any powers, nor anything else in all creation, will be able to separate us from the love of God that is in Christ Jesus our Lord."', "Romans 8:38-39"),
        ('"There is no fear in love. But perfect love drives out fear."', "1 John 4:18"),
    ],
    "comfort": [
        ('"The Lord is near to the brokenhearted and saves those who are crushed in spirit."', "Psalm 34:18"),
        ('"Praise be to the God and Father of our Lord Jesus Christ, the Father of compassion and the God of all comfort, who comforts us in all our troubles."', "2 Corinthians 1:3-4"),
        ('"Come to me, all you who are weary and burdened, and I will give you rest."', "Matthew 11:28"),
        ('"He heals the brokenhearted and binds up their wounds."', "Psalm 147:3"),
        ('"He will wipe every tear from their eyes. There will be no more death or mourning or crying or pain."', "Revelation 21:4"),
    ],
    "faith": [
        ('"Trust in the Lord with all your heart and lean not on your own understanding; in all your ways submit to him, and he will make your paths straight."', "Proverbs 3:5-6"),
        ('"Now faith is confidence in what we hope for and assurance about what we do not see."', "Hebrews 11:1"),
        ('"For we live by faith, not by sight."', "2 Corinthians 5:7"),
        ('"Be still, and know that I am God."', "Psalm 46:10"),
        ('"If you have faith as small as a mustard seed, you can say to this mountain, \'Move from here to there,\' and it will move. Nothing will be impossible for you."', "Matthew 17:20"),
    ],
}

ALL_TRUTHS = [item for items in TRUTHS.values() for item in items]


def pick_truth() -> str:
    """Return a random curated verse formatted for Telegram."""
    text, ref = random.choice(ALL_TRUTHS)
    return f"*{ref}*\n\n{text}"


# ── daily push ──────────────────────────────────────────────────────────────
async def deliver_daily(app, chat_id: int, prefs: dict):
    data = fetch_readings()
    if data:
        text = format_readings(data, mode=prefs.get("readings", "full"))
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    else:
        await app.bot.send_message(chat_id=chat_id, text="Couldn't fetch today's readings. 🙏")

    if prefs.get("truth", True):
        verse = pick_truth()
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
        "`/readings 2026-06-01` — Readings for any past date\n"
        "`/truth` — Random Bible verse from a curated list\n"
        "`/subscribe` — Set up daily push at 6/7/8am SGT\n"
        "   • Pick full readings or gospel-only\n"
        "   • Toggle daily truth on/off\n"
        "`/unsubscribe` — Stop daily readings\n"
        "`/mysub` — See your current preferences\n"
        "`/help` — This message\n\n"
        "*Data source:*\n"
        "📖 Verses curated from NIV/NLT — the most shared scriptures\n\n"
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


async def readings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch readings for a specific date: /readings 2026-05-25"""
    if not context.args:
        await update.message.reply_text("Usage: `/readings 2026-05-25`", parse_mode="Markdown")
        return
    try:
        d = date.fromisoformat(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD format.")
        return

    msg = await update.message.reply_text(f"📖 fetching for {d.isoformat()}...")
    data = fetch_readings(d)
    if not data:
        await msg.edit_text(f"No readings found for {d.isoformat()}. USCCB might not have it published.")
        return
    await msg.edit_text(format_readings(data), parse_mode="Markdown")


async def truth(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    verse = pick_truth()
    await update.message.reply_text(verse, parse_mode="Markdown")


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
    app.add_handler(CommandHandler("readings", readings))
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