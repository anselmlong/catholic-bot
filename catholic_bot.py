#!/usr/bin/env python3
"""catholic-bot — daily mass readings + random bible verse + subscribe."""

import json
import logging
import os
import random
import re
import sys
import time
import asyncio
from datetime import date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

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
USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

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


# ── users ────────────────────────────────────────────────────────────────────
GREETINGS = [
    "Jesus loves you, {name}! 🕊",
    "Peace be with you, {name}! ☮️",
    "Shine the light of Christ, {name}! ✨",
    "The Lord be with you, {name}! 🙏",
    "Walk in faith, {name}! 🌟",
]


def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_users(users: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


# ── readings ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
USCCB_URL = "https://bible.usccb.org/bible/readings/{mmdd}.cfm"
UNIV_URL = "https://universalis.com/asia.singapore/readings.htm/{ymd}/mass.htm"
CLEANUP_PHRASES = [
    "The word of the Lord.", "Thanks be to God.",
    "The Gospel of the Lord.", "Praise to you, Lord Jesus Christ.",
]
COOKIE_FILE = os.path.join(BASE_DIR, "obolus_cookie.json")
CACHE_FILE = os.path.join(BASE_DIR, "readings_cache.json")

# mapping universalis headers to consistent names
UNIV_HEADERS = {
    "first reading": "Reading I",
    "second reading": "Reading II",
    "responsorial psalm": "Responsorial Psalm",
    "gospel": "Gospel",
}


def _mmdd(d: date) -> str:
    return d.strftime("%m%d%y")


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ── obolus cookie ──────────────────────────────────────────────────────────
def _load_obolus() -> dict | None:
    try:
        with open(COOKIE_FILE) as f:
            data = json.load(f)
        c = data.get("X_Obolus_Proof")
        return {"X_Obolus_Proof": c} if c else None
    except Exception:
        return None


# ── usccb scraper (fallback) ────────────────────────────────────────────────
def _parse_usccb(html: str, d: date) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = (title_tag.get_text(strip=True).split("|")[0].strip()
             if title_tag else "Daily Mass Readings")
    sections = []
    for block in soup.select(".wr-block.b-verse"):
        name_el = block.select_one(".name")
        if not name_el:
            continue
        header = name_el.get_text(strip=True)
        if re.match(r"alleluia", header, re.IGNORECASE):
            continue
        address_el = block.select_one(".address a")
        citation = address_el.get_text(strip=True) if address_el else ""
        body_el = block.select_one(".content-body")
        raw_text = body_el.get_text("\n", strip=True) if body_el else ""
        for phrase in CLEANUP_PHRASES:
            raw_text = raw_text.replace(phrase, "")
        text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
        if text:
            sections.append({"header": header, "citation": citation, "text": text})
    if sections:
        return {"title": title, "date": d.isoformat(), "readings": sections, "source": "usccb"}
    return None


def _fetch_usccb(d: date) -> dict | None:
    url = USCCB_URL.format(mmdd=_mmdd(d))
    for attempt in range(3):
        cookie = _load_obolus()
        if cookie:
            try:
                r = requests.get(url, timeout=20, cookies=cookie,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and "Checking connection" not in r.text:
                    parsed = _parse_usccb(r.text, d)
                    if parsed:
                        return parsed
            except Exception:
                pass
        log.warning(f"USCCB failed for {d} (attempt {attempt + 1})")
        if attempt < 2:
            time.sleep(3 + attempt * 3)
    return None


# ── universalis JSONP API (preferred) ───────────────────────────────────────
def _fetch_universalis_json(d: date) -> dict | None:
    """Fetch readings from the Universalis JSONP API. Returns structured data."""
    import html
    import re
    url = f"https://universalis.com/asia.singapore/{_ymd(d)}/jsonpmass.js?callback=x"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Universalis JSONP error for {d}: {e}")
        return None

    # Strip callback wrapper: x({...});
    raw = r.text.strip()
    if raw.startswith("x(") and raw.endswith(");"):
        raw = raw[2:-2]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Parse feast title from the 'day' HTML field
    title = "Daily Mass Readings"
    day_html = data.get("day", "")
    if day_html:
        m = re.search(r"<b>(.*?)</b>", day_html)
        if m:
            title = html.unescape(m.group(1)).strip()

    def _strip_html(s: str) -> str:
        """Strip HTML tags, convert entities, clean spacing."""
        s = html.unescape(s)
        # Insert newlines at block boundaries
        s = re.sub(r"</(div|p|blockquote|h[1-6])>\s*", "\n", s)
        s = re.sub(r"<br\s*/?>", "\n", s)
        s = re.sub(r"<[^>]+>", "", s)
        # Normalize whitespace: collapse triple+ newlines, trim per line
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = re.sub(r"^[ \t]+|[ \t]+$", "", s, flags=re.MULTILINE)
        return s.strip()

    sections = []
    header_name = {
        "Mass_R1": ("Reading I", "📜"),
        "Mass_R2": ("Reading II", "📜"),
        "Mass_Ps": ("Responsorial Psalm", "🎵"),
        "Mass_G": ("Gospel", "✝️"),
    }

    reading_order = ["Mass_R1", "Mass_Ps", "Mass_R2", "Mass_G"]
    for key in reading_order:
        item = data.get(key)
        if not item:
            continue
        heading = ""
        if item.get("heading"):
            heading = _strip_html(item["heading"])
        citation = _strip_html(item.get("source", ""))
        text = _strip_html(item.get("text", ""))
        if not text:
            continue
        hdr = header_name.get(key, (key, "📜"))[0]
        sections.append({"header": hdr, "citation": citation, "text": text})

    if not sections:
        return None
    return {"title": title, "date": d.isoformat(), "readings": sections, "source": "universalis-json"}


# ── universalis HTML scraper (fallback) ────────────────────────────────────
def _fetch_universalis(d: date) -> dict | None:
    "Fetch readings from Universalis (Jerusalem Bible translation)."
    url = UNIV_URL.format(ymd=_ymd(d))
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        if r.url != url:
            return None
    except Exception as e:
        log.warning(f"Universalis HTTP error for {d}: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    feast = soup.select_one("#feastname strong")
    title = feast.get_text(strip=True) if feast else "Daily Mass Readings"

    sections = []
    seen_first_set = False

    for table in soup.select("table.each"):
        ths = table.find_all("th")
        if not ths:
            continue
        raw_header = ths[0].get_text(strip=True).lower().strip()

        # skip Gospel Acclamation / Alleluia / Or: / Sequence / Canticle
        if any(skip in raw_header for skip in ["alleluia", "gospel acclamation", "or:", "sequence", "canticle"]):
            continue
        if raw_header == "first reading" and seen_first_set:
            break
        if raw_header == "first reading":
            seen_first_set = True

        header = UNIV_HEADERS.get(raw_header, ths[0].get_text(strip=True))
        citation = ths[1].get_text(strip=True) if len(ths) > 1 else ""

        # collect text after this table until next hr or table.each
        texts = []
        el = table.find_next_sibling()
        _limit = 100
        while el and el.name != "hr" and _limit > 0:
            _limit -= 1
            if el.name in ("p", "div", "h4", "blockquote"):
                t = el.get_text("\n", strip=True)
                for phrase in CLEANUP_PHRASES:
                    t = t.replace(phrase, "")
                t = re.sub(r"\n{3,}", "\n\n", t).strip()
                if t.lower() in ("how to listen", "continue", "listen to the podcast!"):
                    el = el.find_next_sibling()
                    continue
                t_lower = t.lower()
                if any(kw in t_lower for kw in (
                    "you can also view this page", "the christian art website",
                    "each day,", "the readings on this page", "universalis apps",
                    "new american bible", "english standard version", "set this page to",
                    "universalis podcast", "episode notes",
                )):
                    el = el.find_next_sibling()
                    continue
                if el.name == "table" and "each" in (el.get("class") or []):
                    break
                if t:
                    texts.append(t)
            el = el.find_next_sibling()

        full_text = "\n\n".join(texts)
        if full_text:
            sections.append({"header": header, "citation": citation, "text": full_text})

    if sections:
        data = {"title": title, "date": d.isoformat(), "readings": sections, "source": "universalis"}
        return data
    return None


# ── cache ───────────────────────────────────────────────────────────────────
def _load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def cache_date(d: date) -> dict | None:
    """Fetch and cache readings for a given date from Universalis."""
    key = d.isoformat()
    cache = _load_cache()
    if key in cache:
        return cache[key]  # already cached

    data = _fetch_universalis(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        log.info(f"cached {key} from Universalis")
    return data


# ── main fetch (cache → universalis jsonp → universalis html → usccb) ────
def fetch_readings(d: date | None = None) -> dict | None:
    if d is None:
        from datetime import datetime
        d = datetime.now(SGT).date()
    key = d.isoformat()

    # 1. check cache
    cache = _load_cache()
    if key in cache:
        return cache[key]

    # 2. try Universalis JSONP (structured, no noise)
    data = _fetch_universalis_json(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        return data

    # 3. fall back to Universalis HTML scraper
    data = _fetch_universalis(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        return data

    # 4. fall back to USCCB (NAB)
    data = _fetch_usccb(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        return data

    return None


def format_readings(data: dict, mode: str = "full") -> list[str]:
    """Return formatted readings as a list of message parts, each under 4000 chars."""
    MAX_LEN = 4000
    sections = data.get("readings", [])
    title = data.get("title", "Daily Mass Readings")
    date_str = data.get("date", date.today().isoformat())

    if mode == "gospel":
        gospel = next((s for s in sections if s["header"] == "Gospel"), None)
        if gospel:
            return [f"✝️ *Gospel of the Day* — {gospel['citation']}\n\n{gospel['text']}"]
        return ["No gospel reading found."]

    parts = [f"📖 *{title}*", f"🗓 {date_str}\n"]
    for s in sections:
        icon = "✝️" if s["header"] == "Gospel" else "🎵" if s["header"] == "Responsorial Psalm" else "📜"
        parts.append(f"{icon} *{s['header']}* — {s['citation']}")
        parts.append(f"{s['text']}\n")

    full = "\n".join(parts)
    # If under limit, return as single message
    if len(full) <= MAX_LEN:
        return [full]

    # Split at reading boundaries (every 2 parts after the intro)
    messages = []
    current = parts[0]  # title
    current += "\n" + parts[1]  # date
    for i in range(2, len(parts), 2):
        # parts[i] = header line, parts[i+1] = text
        header = parts[i] if i < len(parts) else ""
        text = parts[i + 1] if i + 1 < len(parts) else ""
        chunk = f"\n\n{header}\n{text}"
        if len(current) + len(chunk) > MAX_LEN:
            messages.append(current)
            current = f"📖 *{title}* (cont.)\n\n{header}\n{text}"
        else:
            current += chunk
    if current:
        messages.append(current)
    return messages


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


# ── chat type detection ────────────────────────────────────────────────────
def is_group(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type in ("group", "supergroup")


# ── daily push ──────────────────────────────────────────────────────────────
async def deliver_daily(app, chat_id: int, prefs: dict):
    users = load_users()
    name = users.get(str(chat_id), "friend")
    greeting = random.choice(GREETINGS).format(name=name)

    data = await asyncio.to_thread(fetch_readings)
    if data:
        texts = format_readings(data, mode=prefs.get("readings", "full"))
        for i, t in enumerate(texts):
            text = f"*{greeting}*\n\n{t}" if i == 0 else t
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
        await query.edit_message_text("Subscription cancelled. No changes made.", reply_markup=None)
        return
    elif data == "sub_confirm":
        prefs = subs.setdefault(key, default_prefs())
        save_subs(subs)
        summary = (
            f"✅ *Subscribed!*\n\n"
            f"Readings: {'Full' if prefs['readings'] == 'full' else 'Gospel only'}\n"
            f"Daily truth: {'Yes' if prefs['truth'] else 'No'}\n"
            f"Time: {prefs['time']} SGT\n\n"
            f"✨ You'll receive your first message at {prefs['time']} SGT 🌅"
        )
        await query.edit_message_text(summary, parse_mode="Markdown", reply_markup=None)
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text(
            "🙏 *Catholic Daily Bot*\n\n"
            "Daily mass readings and Bible verses, right here.\n\n"
            "📖 `/today` — today's mass readings\n"
            "🙏 `/truth` — random Bible verse\n"
            "⚙️ `/subscribe` — set up daily push for this group\n"
            "ℹ️ `/help` — all commands",
            parse_mode="Markdown",
        )
        return

    users = load_users()
    key = str(update.message.chat_id)
    if key not in users:
        context.user_data["awaiting_name"] = True
        await update.message.reply_text(
            "🙏 *Catholic Daily Bot*\n\n"
            "Welcome! Before we begin — what's your name?",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

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
        "`/today` — Today's mass readings\n"
        "`/truth` — Random Bible verse from a curated list\n"
        "`/subscribe` — Set up daily push at 6/7/8am SGT\n"
        "   • Pick full readings or gospel-only\n"
        "   • Toggle daily truth on/off\n"
        "`/unsubscribe` — Stop daily messages\n"
        "`/help` — This message\n\n"
        "*Data source:*\n"
        "📖 Mass readings: Jerusalem Bible via Universalis API (Singapore calendar)\n"
        "🙏 Random verses: curated from NIV/NLT\n\n"
        "🤖 *About*\n"
        "Created by @anselmlong — DM for feedback or suggestions!",
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
        await update.message.reply_text("Unsubscribed. No more daily messages.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text("You're not subscribed.", reply_markup=MAIN_KEYBOARD)


async def today(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📖 fetching...")
    data = await asyncio.to_thread(fetch_readings)
    if not data:
        await msg.edit_text("Couldn't reach the readings source right now. Try again in a bit. 🙏")
        return
    texts = format_readings(data)
    await msg.edit_text(texts[0], parse_mode="Markdown")
    for t in texts[1:]:
        await update.message.reply_text(t, parse_mode="Markdown")


async def truth(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    verse = pick_truth()
    await update.message.reply_text(verse, parse_mode="Markdown")


# ── admin ─────────────────────────────────────────────────────────────────────
ALLOWED_USERS = {"495290408"}


async def users_cmd(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.chat_id)
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("You don't have permission to use this command.")
        return
    users = load_users()
    subs = load_subs()
    msg = (
        f"👥 *Users: {len(users)}*\n\n"
        + "\n".join(f"• {name}" for name in sorted(users.values()))
        + f"\n\n📬 *Subscribed: {len(subs)}*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── keyboard button router ──────────────────────────────────────────────────
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # name capture mode — any text is treated as a name
    if context.user_data.get("awaiting_name"):
        name = text.strip()
        if not name or len(name) > 50:
            await update.message.reply_text("Please send me your name (max 50 chars):")
            return
        users = load_users()
        key = str(update.message.chat_id)
        users[key] = name
        save_users(users)
        context.user_data["awaiting_name"] = False
        await update.message.reply_text(
            f"Thanks, {name}! 🙏\n\n"
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
        return

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
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CallbackQueryHandler(sub_callback, pattern="^sub_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    jq = app.job_queue
    jq.run_repeating(daily_push, interval=60, first=10, name="daily_check")

    log.info("starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()