#!/usr/bin/env python3
"""catholic-bot — daily mass readings + random bible verse + subscribe."""

import json
import logging
import os
import random
import re
import sys
import time
import threading
import asyncio
from datetime import date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, JobQueue,
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

_subs_lock = threading.Lock()
_mem_cache: dict = {}  # in-memory readings cache keyed by date ISO string

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
    with _subs_lock:
        if os.path.exists(SUBS_FILE):
            try:
                with open(SUBS_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}


def save_subs(subs: dict):
    with _subs_lock:
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

    # 0. in-memory cache (hot path — no disk I/O)
    if key in _mem_cache:
        return _mem_cache[key]

    # 1. check file cache
    cache = _load_cache()
    if key in cache:
        _mem_cache[key] = cache[key]
        return cache[key]

    # 2. try Universalis JSONP (structured, no noise)
    data = _fetch_universalis_json(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        _mem_cache[key] = data
        return data

    # 3. fall back to Universalis HTML scraper
    data = _fetch_universalis(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        _mem_cache[key] = data
        return data

    # 4. fall back to USCCB (NAB)
    data = _fetch_usccb(d)
    if data:
        cache[key] = data
        _save_cache(cache)
        _mem_cache[key] = data
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
    "scripture": [
        ('This is what we have heard from him and are declaring to you:\nGod is light, and there is no darkness in him at all.', '1 John 1:5'),
        ('But if we live in light, as he is in light, we have a share in another\'s life, \nand the blood of Jesus, his Son, cleanses us from all sin.', '1 John 1:7'),
        ('if we acknowledge our sins, he is trustworthy and upright, \nso that he will forgive our sins and will cleanse us from all evil.', '1 John 1:9'),
        ('My children, I am writing this to prevent you from sinning; but if anyone does sin, \nwe have an advocate with the Father, Jesus Christ, the upright.', '1 John 2:1'),
        ('Do not love the world or what is in the world. \nIf anyone does love the world, the love of the Father finds no place in him,', '1 John 2:15'),
        ('He is the sacrifice to expiate our sins, and not only ours,\n but also those of the whole world.', '1 John 2:2'),
        ('You must see what great love the Father has lavished on us \nby letting us be called God\'s children— which is what we are! \nThe reason why the world does not acknowledge us \nis that it did not acknowledge him.', '1 John 3:1'),
        ('This is the message which you heard from the beginning, that we must love one another.', '1 John 3:11'),
        ('This is the proof of love, that he laid down his life for us, \nand we too ought to lay down our lives for our brothers.', '1 John 3:16'),
        ('My dear friends, if our own feelings do not condemn us, \nwe can be fearless before God, and whatever we ask we shall receive from him, \nbecause we keep his commandments and do what is acceptable to him.', '1 John 3:21-22'),
        ('His commandment is this, that we should believe in the name of his Son Jesus Christ \nand that we should love one another as he commanded us.', '1 John 3:23'),
        ('Whoever keeps his commandments remains in God, and God in him. \nAnd this is the proof that he remains in us: the Spirit that he has given us.', '1 John 3:24'),
        ('Love consists in this: it is not we who loved God, \nbut God loved us and sent his Son to expiate our sins.', '1 John 4:10'),
        ('My dear friends, if God loved us so much, we too should love one another.', '1 John 4:11'),
        ('No one has ever seen God, but as long as we love one another \nGod remains in us and his love comes to its perfection in us.', '1 John 4:12'),
        ('This is the proof that we remain in him and he in us, \nthat he has given us a share in his Spirit.', '1 John 4:13'),
        ('We ourselves have seen and testify that the Father sent his Son as Saviour of the world.', '1 John 4:14'),
        ('Anyone who acknowledges that Jesus is the Son of God,\nGod remains in him and he in God.', '1 John 4:15'),
        ('We have recognised for ourselves, and put our faith in, the love God has for us. \nGod is love, and whoever remains in love remains in God and God in him.', '1 John 4:16'),
        ('Love comes to its perfection in us when we can face the Day of Judgement fearlessly, \nbecause even in this world we have become as he is.', '1 John 4:17'),
        ('In love there is no room for fear, but perfect love drives out fear,\nbecause fear implies punishment and no one who is afraid has come to perfection in love.', '1 John 4:18'),
        ('Let us love, then, because he first loved us.', '1 John 4:19'),
        ('Indeed this is the commandment we have received from him, \nthat whoever loves God, must also love his brother.', '1 John 4:21'),
        ('My dear friends, let us love one another, \nsince love is from God and everyone \nwho loves is a child of God and knows God.', '1 John 4:7'),
        ('This is the revelation of God\'s love for us, \nthat God sent his only Son into the world \nthat we might have life through him.', '1 John 4:9'),
        ('Whoever believes that Jesus is the Christ is a child of God, \nand whoever loves the father loves the son.', '1 John 5:1'),
        ('This is the testimony: God has given us eternal life, and this life is in his Son.', '1 John 5:11'),
        ('This is what the love of God is: keeping his commandments. \nNor are his commandments burdensome,\nbecause every child of God overcomes the world. \nAnd this is the victory that has overcome the world -- our faith.', '1 John 5:3-4'),
        ('Who can overcome the world but the one who believes that Jesus is the Son of God?', '1 John 5:5'),
        ('Seek the Lord and his strength; seek his presence continually!', '1 Chronicles 16:11'),
        ('Be strong and courageous and do it. Do not be afraid and do not be dismayed, \nfor the Lord God, even my God, is with you. He will not leave you or forsake you.', '1 Chronicles 28:20'),
        ('For in him you have been enriched in every way\n—with all kinds of speech and with all knowledge', '1 Corinthians 1:5'),
        ('Therefore you do not lack any spiritual gift \nas you eagerly wait for our Lord Jesus Christ to be revealed', '1 Corinthians 1:7'),
        ('No temptation has overtaken you that is not common to man. \nGod is faithful, and he will not let you be tempted beyond your ability, \nbut with the temptation he will also provide the way of escape, \nthat you may be able to endure it', '1 Corinthians 10:13'),
        ('Or do you not know that your body is a temple of the Holy Spirit within you, \nwhom you have from God? You are not your own, \nfor you were bought with a price. So glorify God in your body.', '1 Corinthians 6:19-20'),
        ('If we claim to have fellowship with him and yet walk in the darkness,\nwe lie and do not live out the truth', '1 John 1:6'),
        ('But if we walk in the light, as he is in the light, \nwe have fellowship with one another, \nand the blood of Jesus, his Son, purifies us from all sin.', '1 John 1:7'),
        ('But now the Lord my God has given me rest on every side. \nThere is neither adversary nor misfortune', '1 Kings 5:4'),
        ('Behold, I am laying in Zion a stone, a cornerstone chosen and precious, \nand he who believes in him will not be put to shame', '1 Peter 2:6'),
        ('But you are a chosen race, a royal priesthood, a holy nation, \nGod\'s own people, that you may declare the wonderful deeds of him who \ncalled you out of darkness into his marvelous light', '1 Peter 2:9'),
        ('If you speak, speak with the word of God. If you act, act with the word of God.', '1 Peter 4:11'),
        ('Therefore let those who suffer according to God\'s will \nentrust their souls to a faithful Creator while doing good', '1 Peter 4:19'),
        ('So as to live for the rest of the time in the flesh no longer \nfor human passions but for the will of God', '1 Peter 4:2'),
        ('The end of all things is near. Therefore be clear minded \nand self-controlled so that you can pray', '1 Peter 4:7-8'),
        ('And after you have suffered a little while, the God of all grace, \nwho has called you to his eternal glory in Christ, \nwill himself restore, confirm, strengthen, and establish you.', '1 Peter 5:10'),
        ('Clothe yourselves, all of you, with humility toward one another, \nfor “God opposes the proud but gives grace to the humble.”', '1 Peter 5:5'),
        ('Humble yourselves, therefore, under the mighty hand of God \nso that at the proper time he may exalt you, \ncasting all your anxieties on him, because he cares for you.', '1 Peter 5:6-7'),
        ('Be sober-minded; be watchful. Your adversary the devil \nprowls around like a roaring lion, seeking someone to devour.\nResist him, firm in your faith, knowing that the same kinds of \nsuffering are being experienced by your brotherhood throughout the world.', '1 Peter 5:8-9'),
        ('Who comforts us in all our affliction, so that we may be able to comfort those \nwho are in any affliction, with the comfort with\n which we ourselves are comforted by God', '2 Corinthians 1:4'),
        ('For the sake of Christ, then, I am content with weaknesses, \ninsults, hardships, persecutions, and calamities. \nFor when I am weak, then I am strong.', '2 Corinthians 12:10'),
        ('“My grace is sufficient for you, for my power is made perfect in weakness.” \nTherefore I will boast all the more gladly of my weaknesses, \nso that the power of Christ may rest upon me.', '2 Corinthians 12:9'),
        ('Therefore, if anyone is in Christ, he is a new creation.\nThe old has passed away; behold, the new has come', '2 Corinthians 5:17'),
        ('For God gave us a spirit not of fear but of power and love and self-control.', '2 Timothy 1:7'),
        ('In all things I have shown you that by working hard in this way \nwe must help the weak and remember the words of the Lord Jesus, \nhow he himself said, ‘It is more blessed to give than to receive.’”', 'Acts 20:35'),
        ('but you will receive the power of the Holy Spirit which will come on you,\nand then you will be my witnesses not only in Jerusalem \nbut throughout Judaea and Samaria, and indeed to earth\'s remotest end', 'Acts 1:8'),
        ('In the last days -- the Lord declares -- I shall pour out my Spirit on all humanity.\nYour sons and daughters shall prophesy, your young people shall see visions, \nyour old people dream dreams.', 'Acts 2:17'),
        ('Therefore, as God’s chosen people, holy and dearly loved, \nclothe yourselves with compassion, kindness, humility, gentleness and patience', 'Colossians 3:12'),
        ('Bear with each other and forgive one another if any of you \nhas a grievance against someone. Forgive as the Lord forgave you.', 'Colossians 3:13'),
        ('And over all these virtues put on love, \nwhich binds them all together in perfect unity', 'Colossians 3:14'),
        ('Let the peace of Christ rule in your hearts, \nsince as members of one body you were called to peace. And be thankful.', 'Colossians 3:15'),
        ('Let the message of Christ dwell among you richly as you teach and \nadmonish one another with all wisdom through psalms, hymns, \nand songs from the Spirit, singing to God with gratitude in your hearts.', 'Colossians 3:16'),
        ('And whatever you do, whether in word or deed, do it all \nin the name of the Lord Jesus, giving thanks to God the Father through him.', 'Colossians 3:17'),
        ('Be strong and courageous. Do not fear or be in dread of them, \nfor it is the Lord your God who goes with you. \nHe will not leave you or forsake you', 'Deuteronomy 31:6'),
        ('You shall love the Lord your God with all your heart and with \nall your soul and with all your might', 'Deuteronomy 6:5'),
        ('For by grace you have been saved through faith. \nAnd this is not your own doing; it is the gift of God', 'Ephesians 2:8'),
        ('Do not participate in the unfruitful deeds of darkness, \nbut instead even expose them', 'Ephesians 5:11'),
        ('But all things become visible when they are exposed by the light, \nfor everything that becomes visible is light.', 'Ephesians 5:13'),
        ('Awake, sleeper, And arise from the dead,\nAnd Christ will shine on you', 'Ephesians 5:14'),
        ('Let no one deceive you with empty words, for because of these things \nthe wrath of God comes upon the sons of disobedience', 'Ephesians 5:6'),
        ('For you were formerly darkness, but now you are Light in the Lord; \nwalk as children of Light; for the fruit of the Light \nconsists in all goodness and righteousness and truth', 'Ephesians 5:8-9'),
        ('Finally, be strong in the Lord and in the strength of his might.\nPut on the whole armor of God, that you may be able to \nstand against the schemes of the devil', 'Ephesians 6:10-11'),
        ('Therefore take up the whole armor of God, \nthat you may be able to withstand in the evil day, \nand having done all, to stand firm', 'Ephesians 6:13'),
        ('I have been crucified with Christ; and it is no longer I who live, \nbut Christ lives in me; and the life which I now live in the flesh\nI live by faith in the Son of God, who loved me and gave Himself up for me', 'Galatians 2:20'),
        ('And those who belong to Christ Jesus have \ncrucified the flesh with its passions and desires', 'Galatians 5:24'),
        ('Let us keep firm in the hope we profess, \nbecause the one who made the promise is trustworthy.', 'Hebrews 10:23'),
        ('Let us be concerned for each other, \nto stir a response in love and good works.', 'Hebrews 10:24'),
        ('Do not absent yourself from your own assemblies, as some do, \nbut encourage each other; the more so as you see the Day drawing near.', 'Hebrews 10:25'),
        ('You will need perseverance if you are to do God\'s will and gain what he has promised.\nOnly a little while now, a very little while, for come he certainly will before too long.', 'Hebrews 10:36-37'),
        ('Only faith can guarantee the blessings that we hope for, \nor prove the existence of realities that are unseen.', 'Hebrews 11:1'),
        ('And without faith it is impossible to please Him, for he who comes to God \nmust believe that He is and that He is a rewarder of those who seek Him.', 'Hebrews 11:6'),
        ('Of course, any discipline is at the time a matter for grief, not joy; but later, \nin those who have undergone it, it bears fruit in peace and uprightness.', 'Hebrews 12:11'),
        ('So steady all weary hands and trembling knees and make your crooked paths straight; \nthen the injured limb will not be maimed, it will get better instead.', 'Hebrews 12:12-13'),
        ('Be careful that no one is deprived of the grace of God and that no root of \nbitterness should begin to grow and make trouble; this can poison a large number.', 'Hebrews 12:15'),
        ('We have been given possession of an unshakeable kingdom.\nLet us therefore be grateful and use our gratitude to worship \nGod in the way that pleases him, in reverence and fear.', 'Hebrews 12:28'),
        ('Perseverance is part of your training; God is treating you as his sons.\nHas there ever been any son whose father did not train him?', 'Hebrews 12:7'),
        ('Let us also lay aside every weight, and sin which clings so closely, \nand let us run with endurance the race that is set before us', 'Hebrews 12:1'),
        ('Seek peace with all people, and the holiness without which no one can ever see the Lord.', 'Hebrews 12:14'),
        ('Let us keep our eyes fixed on Jesus, who leads us in our faith and brings it to perfection: \nfor the sake of the joy which lay ahead of him, he endured the cross, \ndisregarding the shame of it, and has taken his seat at the right of God\'s throne.', 'Hebrews 12:2'),
        ('Think of the way he persevered against such opposition from sinners\nand then you will not lose heart and come to grief.', 'Hebrews 12:3'),
        ('Have you forgotten that encouraging text in which you are addressed as sons? \nMy son, do not scorn correction from the Lord, do not resent his training,\n for the Lord trains those he loves, and chastises every son he accepts.', 'Hebrews 12:5-6'),
        ('Continue to love each other like brothers, and remember always to welcome strangers, \nfor by doing this, some people have entertained angels without knowing it.', 'Hebrews 13:1-2'),
        ('Through him then let us continually offer up a sacrifice of praise to God, \nthat is, the fruit of lips that acknowledge his name', 'Hebrews 13:15'),
        ('Keep doing good works and sharing your resources, \nfor these are the kinds of sacrifice that please God', 'Hebrews 13:16'),
        ('Keep in mind those who are in prison, as though you were in prison with them; \nand those who are being badly treated, since you too are in the body.', 'Hebrews 13:3'),
        ('I will never leave you nor forsake you', 'Hebrews 13:5'),
        ('and so we can say with confidence: With the Lord on my side, \nI fear nothing: what can human beings do to me?', 'Hebrews 13:6'),
        ('Remember your leaders, who preached the word of God to you, \nand as you reflect on the outcome of their lives, take their faith as your model.', 'Hebrews 13:7'),
        ('Jesus Christ is the same today as he was yesterday and as he will be for ever.', 'Hebrews 13:8'),
        ('In this saying: If only you would listen to him today; \ndo not harden your hearts, as at the Rebellion,', 'Hebrews 3:15'),
        ('Every house is built by someone, of course; but God built everything that exists.', 'Hebrews 3:4'),
        ('Since in Jesus, the Son of God, we have the supreme high priest who has gone\nthrough to the highest heaven, we must hold firm to our profession of faith', 'Hebrews 4:14'),
        ('Then a shoot will spring from the stem of Jesse,                           \nAnd a branch from his roots will bear fruit', 'Isaiah 11:1'),
        ('Behold, God is my salvation; I will trust, and will not be afraid; \nfor the Lord God is my strength and my song, and he has become my salvation', 'Isaiah 12:2'),
        ('Strengthen the feeble hands, steady the knees that give way;', 'Isaiah 35:3'),
        ('Say to those with fearful hearts,“Be strong, \nDo not fear; your God will come,\nhe will come with vengeance; \nwith divine retribution he will come to save you.”', 'Isaiah 35:4'),
        ('He tends his flock like a shepherd:\nHe gathers the lambs in his arms and carries them close to his heart; \nhe gently leads those that have young.', 'Isaiah 40:11'),
        ('He gives strength to the weary, he strengthens the powerless.', 'Isaiah 40:29'),
        ('In the wilderness prepare the way for the Lord;\nmake straight in the desert a highway for our God.', 'Isaiah 40:3'),
        ('those who hope in Yahweh will regain their strength, they will sprout wings like eagles, \nthough they run they will not grow weary, though they walk they will never tire.', 'Isaiah 40:31'),
        ('Every valley shall be raised up, every mountain and hill made low;\nthe rough ground shall become level, the rugged places a plain.', 'Isaiah 40:4'),
        ('The grass withers and the flowers fall,\nbut the word of our God endures forever.', 'Isaiah 40:8'),
        ('Fear not, for I am with you; be not dismayed, for I am your God;\nI will strengthen you, I will help you, I will uphold you with my righteous right hand', 'Isaiah 41:10'),
        ('I have blotted out your transgressions like a cloud and your sins like mist; \nreturn to me, for I have redeemed you', 'Isaiah 44:22'),
        ('We all, like sheep, have gone astray, each of us has turned to our own way;\nand the Lord has laid on him the iniquity of us all.', 'Isaiah 53:6'),
        ('Then I heard the voice of the Lord saying, \n“Whom shall I send? And who will go for us?”\nAnd I said, “Here am I. Send me!”', 'Isaiah 6:8'),
        ('Arise, shine, for your light has come,\nand the glory of the Lord rises upon you.', 'Isaiah 60:1'),
        ('See, darkness covers the earth and thick darkness is over the peoples,\nbut the Lord rises upon you and his glory appears over you.', 'Isaiah 60:2'),
        ('But now, O LORD, You are our Father, \nWe are the clay, and You our potter; \nAnd all of us are the work of Your hand.', 'Isaiah 64:8'),
        ('Ask the Lord your God for a sign, \nwhether in the deepest depths or in the highest heights.', 'Isaiah 7:11'),
        ('Therefore the Lord himself will give you a sign: \nThe virgin will conceive and give birth to a son, \nand will call him Immanuel.', 'Isaiah 7:14'),
        ('The people walking in darkness have seen a great light;\non those living in the land of deep darkness a light has dawned', 'Isaiah 9:2'),
        ('For as in the day of Midian’s defeat,\nyou have shattered the yoke that burdens them,\nthe bar across their shoulders, the rod of their oppressor.', 'Isaiah 9:4'),
        ('Humble yourselves before the Lord, and he will exalt you', 'James 4:10'),
        ('Submit yourselves therefore to God. \nResist the devil, and he will flee from you.', 'James 4:7'),
        ('Draw near to God, and he will draw near to you. Cleanse your hands, \nyou sinners, and purify your hearts, you double-minded.', 'James 4:8'),
        ('I know, O Lord, that the way of man is not in himself, \nthat it is not in man who walks to direct his steps.', 'Jeremiah 10:23'),
        ('But as for me, behold, I am in your hands. \nDo with me as seems good and right to you', 'Jeremiah 26:14'),
        ('For I know the plans I have for you, declares the Lord, \nplans for welfare and not for evil, to give you a future and a hope.', 'Jeremiah 29:11'),
        ('If you would direct your heart right And spread out your hand to Him', 'Job 11:13'),
        ('And rend your hearts and not your garments. Return to the Lord your God, \nfor he is gracious and merciful, slow to anger, \nand abounding in steadfast love; and he relents over disaster.', 'Joel 2:13'),
        ('Through him all things were made; \nwithout him nothing was made that has been made.', 'John 1:3'),
        ('In him was life, and that life was the light of all mankind. \nThe light shines in the darkness, and the darkness has not overcome it.', 'John 1:4-5'),
        ('He came as a witness to testify concerning that light, \nso that through him all might believe. \nHe himself was not the light; he came only as a witness to the light.', 'John 1:7-8'),
        ('Walk while you have the light, before darkness overtakes you. \nWhoever walks in the dark does not know where they are going.', 'John 12:35'),
        ('Let not your hearts be troubled. Believe in God; believe also in me', 'John 14:1'),
        ('If you love me, you will keep my commandments', 'John 14:15'),
        ('In that day you will know that I am in my Father, and you in me, and I in you', 'John 14:20'),
        ('Peace I leave with you; my peace I give to you. \nNot as the world gives do I give to you. \nLet not your hearts be troubled, neither let them be afraid.', 'John 14:27'),
        ('And if I go and prepare a place for you, I will come again \nand will take you to myself, that where I am you may be also.', 'John 14:3'),
        ('I am the way, and the truth, and the life. \nNo one comes to the Father except through me', 'John 14:6'),
        ('If you keep my commandments, you will abide in my love, \njust as I have kept my Father\'s commandments and abide in his love. \nThese things I have spoken to you, \nthat my joy may be in you, and that your joy may be full', 'John 15:10-11'),
        ('Greater love has no one than this, \nthat someone lay down his life for his friends. \nYou are my friends if you do what I command you', 'John 15:13-14'),
        ('Abide in me, and I in you. As the branch cannot bear fruit by itself, \nunless it abides in the vine, neither can you, unless you abide in me', 'John 15:4'),
        ('I am the vine; you are the branches. Whoever abides in me and I in him, \nhe it is that bears much fruit, for apart from me you can do nothing', 'John 15:5'),
        ('If you abide in me, and my words abide in you, \nask whatever you wish, and it will be done for you.', 'John 15:7'),
        ('I have said these things to you, that in me you may have peace. \nIn the world you will have tribulation. But take heart; I have overcome the world', 'John 16:33'),
        ('For God so loved the world that he gave his one and only Son, \nthat whoever believes in him shall not perish but have eternal life', 'John 3:16'),
        ('For God did not send his Son into the world to condemn the world, \nbut in order that the world might be saved through him', 'John 3:17'),
        ('Be strong and courageous. Do not be frightened, \nand do not be dismayed, for the Lord your God is with you wherever you go', 'Joshua 1:9'),
        ('Nothing is impossible for God', 'Luke 1:37'),
        ('And Mary said, “Behold, I am the servant of the Lord; \nlet it be to me according to your word.”', 'Luke 1:38'),
        ('So therefore, any one of you who does not renounce \nall that he has cannot be my disciple', 'Luke 14:33'),
        ('But while he was still a long way off, his father saw him and felt compassion. \nand ran and embraced him and kissed him.', 'Luke 15:20'),
        ('Just so, I tell you, there will be more joy in heaven over 1 sinner who repents\nthan over 99 righteous persons who need no repentance', 'Luke 15:7'),
        ('For everyone who exalts himself will be humbled, \nbut the one who humbles himself will be exalted', 'Luke 18:14'),
        ('Do not be afraid.                                                                          \nI bring you good news that will cause great joy for all the people', 'Luke 2:10'),
        ('Glory to God in the highest heaven,                                               \nand on earth peace to those on whom his favor rests.', 'Luke 2:14'),
        ('Father, into your hands I commit my spirit!', 'Luke 23:46'),
        ('If anyone would come after me, \nlet him deny himself and take up his cross daily and follow me. \nFor whoever would save his life will lose it, \nbut whoever loses his life for my sake will save it.', 'Luke 9:23-24'),
        ('Come to me, all you who labour and are overburdened, and I will give you rest.', 'Matthew 11:28'),
        ('Shoulder my yoke and learn from me, \nfor I am gentle and humble in heart, \nand you will find rest for your souls.', 'Matthew 11:29'),
        ('Anyone who does the will of my Father in heaven is my brother and sister and mother.\'', 'Matthew 12:50'),
        ('But blessed are your eyes because they see, your ears because they hear!', 'Matthew 13:16'),
        ('And the seed sown in rich soil is someone who hears the word and understands it; \nthis is the one who yields a harvest and produces now a hundredfold, now sixty, now thirty.', 'Matthew 13:23'),
        ('The kingdom of Heaven is like treasure hidden in a field which someone has found; \nhe hides it again, goes off in his joy, sells everything he owns and buys the field.', 'Matthew 13:44'),
        ('Again, the kingdom of Heaven is like a merchant looking for fine pearls;\nwhen he finds one of great value he goes and sells everything he owns and buys it.', 'Matthew 13:45-46'),
        ('But at once Jesus called out to them, saying, \'Courage! It\'s me! Don\'t be afraid.\'', 'Matthew 14:27'),
        ('But you,\' he said, \'who do you say I am?\'\nThen Simon Peter spoke up and said, \'You are the Christ, the Son of the living God.\'', 'Matthew 16:15-16'),
        ('...In truth I tell you, if your faith is the size of a mustard seed you will say to this mountain,\n"Move from here to there," and it will move; nothing will be impossible for you.\'', 'Matthew 17:20'),
        ('He was still speaking when suddenly a bright cloud covered them with shadow, \nand suddenly from the cloud there came a voice which said, \n\'This is my Son, the Beloved; he enjoys my favour. Listen to him.\'', 'Matthew 17:5'),
        ('But Jesus came up and touched them, saying, \'Stand up, do not be afraid.\'', 'Matthew 17:7'),
        ('Then Peter went up to him and said, \n\'Lord, how often must I forgive my brother if he wrongs me? As often as seven times?\'\nJesus answered, \'Not seven, I tell you, but seventy-seven times.', 'Matthew 18:21-22'),
        ('And anyone who wants to be first among you must be your slave,\n just as the Son of man came not to be served but to serve, \nand to give his life as a ransom for many.\'', 'Matthew 20:27-28'),
        ('Jesus stopped, called them over and said, \'What do you want me to do for you?\'', 'Matthew 20:32'),
        ('Jesus felt pity for them and touched their eyes, \nand at once their sight returned and they followed him.', 'Matthew 20:34'),
        ('Jesus answered, \'In truth I tell you, if you have faith and do not doubt at all, \nnot only will you do what I have done to the fig tree, \nbut even if you say to this mountain, "Be pulled up and thrown into the sea," it will be done.', 'Matthew 21:21'),
        ('Jesus said to him, \'You must love the Lord your God with all your heart, \nwith all your soul, and with all your mind.', 'Matthew 22:37'),
        ('..."In truth I tell you, in so far as you did this to one of \nthe least of these brothers of mine, you did it to me."', 'Matthew 25:40'),
        ('Blessed are those who are persecuted in the cause of uprightness: \nthe kingdom of Heaven is theirs.', 'Matthew 5:10'),
        ('Blessed are you when people abuse you and persecute you and \nspeak all kinds of calumny against you falsely on my account.\nRejoice and be glad, for your reward will be great in heaven; \nthis is how they persecuted the prophets before you.', 'Matthew 5:11-12'),
        ('You are light for the world. A city built on a hill-top cannot be hidden.', 'Matthew 5:14'),
        ('In the same way your light must shine in people\'s sight, so that, \nseeing your good works, they may give praise to your Father in heaven.', 'Matthew 5:16'),
        ('How blessed are the poor in spirit: the kingdom of Heaven is theirs.', 'Matthew 5:3'),
        ('Blessed are the gentle: they shall have the earth as inheritance.', 'Matthew 5:4'),
        ('Blessed are those who mourn: they shall be comforted.', 'Matthew 5:5'),
        ('Blessed are those who hunger and thirst for uprightness: they shall have their fill.', 'Matthew 5:6'),
        ('Blessed are the merciful: they shall have mercy shown them.', 'Matthew 5:7'),
        ('Blessed are the pure in heart: they shall see God.', 'Matthew 5:8'),
        ('Blessed are the peacemakers: they shall be recognised as children of God.', 'Matthew 5:9'),
        ('Do not store up treasures for yourselves on earth, \nwhere moth and woodworm destroy them and thieves can break in and steal.\nBut store up treasures for yourselves in heaven, \nwhere neither moth nor woodworm destroys them and thieves cannot break in and steal.', 'Matthew 6:19-20'),
        ('For wherever your treasure is, there will your heart be too.', 'Matthew 6:21'),
        ('Look at the birds in the sky. They do not sow or reap or gather into barns; \nyet your heavenly Father feeds them. Are you not worth much more than they are?', 'Matthew 6:26'),
        ('Set your hearts on his kingdom first, and on God\'s saving justice, \nand all these other things will be given you as well.', 'Matthew 6:33'),
        ('Ask, and it will be given to you; search, and you will find; \nknock, and the door will be opened to you.', 'Matthew 7:7'),
        ('And he said to them, \'Why are you so frightened, you who have so little faith?\' \nAnd then he stood up and rebuked the winds and the sea; and there was a great calm.', 'Matthew 8:26'),
        ('Go and learn the meaning of the words: Mercy is what pleases me, \nnot sacrifice. And indeed I came to call not the upright, but sinners.\'', 'Matthew 9:13'),
        ('And when Jesus reached the house the blind men came up to him and he said to them, \n\'Do you believe I can do this?\' They said, \'Lord, we do.\'\nThen he touched their eyes saying, \'According to your faith, let it be done to you.\'', 'Matthew 9:28-29'),
        ('Then he said to his disciples, \'The harvest is rich but the labourers are few, \nso ask the Lord of the harvest to send out labourers to his harvest.', 'Matthew 9:37'),
        ('Jesus, Son of David, have mercy on me', 'Mark 10:47'),
        ('Therefore keep watch because you do not know when\n the owner of the house will come back —whether in the evening,\nor at midnight, or when the rooster crows, or at dawn.', 'Mark 13:35'),
        ('If he comes suddenly, do not let him find you sleeping. \nWhat I say to you, I say to everyone: ‘Watch!’', 'Mark 13:36-37'),
        ('Abba, Father, all things are possible for you.\nRemove this cup from me. Yet not what I will, but what you will.', 'Mark 14:36'),
        ('Do not fear, only believe', 'Mark 5:36'),
        ('If anyone would come after me, let him deny himself and take up \nhis cross and follow me. For whoever would save his life will lose it, \nbut whoever loses his life for my sake and the gospel\'s will save it.', 'Mark 8:34-35'),
        ('For whoever wishes to save his life will lose it, \nbut whoever loses his life for My sake and the gospel\'s will save it', 'Mark 8:35'),
        ('Come to me, all who labor and are heavy laden, and I will give you rest.', 'Matthew 11:28'),
        ('Come to me, all who labor and are heavy laden, and I will give you rest. \nTake my yoke upon you, and learn from me, \nfor I am gentle and lowly in heart, and you will find rest for your souls.', 'Matthew 11:28-29'),
        ('Take my yoke upon you, and learn from me, \nfor I am gentle and lowly in heart, and you will find rest for your souls.', 'Matthew 11:29'),
        ('Blessed is anyone who does not stumble on account of me', 'Matthew 11:6'),
        ('He said, “Come.” So Peter got out of the boat and \nwalked on the water and came to Jesus', 'Matthew 14:29'),
        ('If anyone wishes to come after Me, \nhe must deny himself, and take up his cross and follow Me. \nFor whoever wishes to save his life will lose it;\nbut whoever loses his life for My sake will find it.', 'Matthew 16:24-25'),
        ('So the last will be first, and the first last', 'Matthew 20:16'),
        ('My Father, if it be possible, let this cup pass from me; \nnevertheless, not as I will, but as you will.', 'Matthew 26:39'),
        ('Repent, for the kingdom of heaven is at hand', 'Matthew 3:2'),
        ('Prepare the way for the Lord, make straight paths for him', 'Matthew 3:3'),
        ('But seek first the kingdom of God and his righteousness, \nand all these things will be added to you.', 'Matthew 6:33'),
        ('Not everyone who says to me, ‘Lord, Lord,’ will enter the kingdom of heaven, \nbut the one who does the will of my Father who is in heaven.', 'Matthew 7:21'),
        ('Do all things without grumbling or questioning, \nthat you may be blameless and innocent, children of God \nwithout blemish in the midst of a crooked and twisted generation, \namong whom you shine as lights in the world.', 'Philippians 2:14-15'),
        ('I can do all things through him who strengthens me', 'Philippians 4:13'),
        ('Do not be anxious about anything, but in everything \nby prayer and supplication with thanksgiving \nlet your requests be made known to God.', 'Philippians 4:6'),
        ('And the peace of God, which surpasses all understanding, \nwill guard your hearts and your minds in Christ Jesus', 'Philippians 4:7'),
        ('When a man\'s ways please the Lord,\n he makes even his enemies to be at peace with him', 'Proverbs 16:7'),
        ('My son, give me your heart, and let your eyes observe my ways', 'Proverbs 23:26'),
        ('Whoever conceals his transgressions will not prosper, \nbut he who confesses and forsakes them will obtain mercy.', 'Proverbs 28:13'),
        ('Trust in the Lord with all your heart, \nand do not lean on your own understanding. \nIn all your ways acknowledge him, and he will make straight your paths.', 'Proverbs 3:5-6'),
        ('He is not afraid of bad news; his heart is firm, trusting in the Lord', 'Psalms 112:7'),
        ('Your word is a lamp for my feet, a light on my path.', 'Psalms 119:105'),
        ('I have gone astray like a lost sheep; seek your servant, \nfor I do not forget your commandments', 'Psalms 119:176'),
        ('But I have trusted in your steadfast love; \nmy heart shall rejoice in your salvation.\nI will sing to the Lord, because he has dealt bountifully with me.', 'Psalms 13:5-6'),
        ('Blessed be the Lord, my rock, who trains my hands for war,\nand my fingers for battle', 'Psalms 144:1'),
        ('He is my steadfast love and my fortress, my stronghold and my deliverer,\nmy shield and he in whom I take refuge, who subdues peoples under me', 'Psalms 144:2'),
        ('Blessed are those whose help is the God of Jacob,\nwhose hope is in the Lord their God', 'Psalms 146:5'),
        ('The Lord sets prisoners free,\nthe Lord gives sight to the blind,\nthe Lord lifts up those who are bowed down,', 'Psalms 146:7-8'),
        ('He heals the brokenhearted and binds up their wounds', 'Psalms 147:3'),
        ('I have set the Lord always before me; \nbecause he is at my right hand, I shall not be shaken', 'Psalms 16:8'),
        ('The Lord is my rock and my fortress and my deliverer,\nmy God, my rock, in whom I take refuge,\nmy shield, and the horn of my salvation, my stronghold', 'Psalms 18:2'),
        ('For it is you who light my lamp;\nthe Lord my God lightens my darkness', 'Psalms 18:28'),
        ('For by you I can run against a troop,\nand by my God I can leap over a wall.', 'Psalms 18:29'),
        ('I call upon the Lord, who is worthy to be praised,\nand I am saved from my enemies', 'Psalms 18:3'),
        ('This God—his way is perfect; the word of the Lord proves true;\nhe is a shield for all those who take refuge in him.', 'Psalms 18:30'),
        ('You have given me the shield of your salvation, and your right hand supported me,\nand your gentleness made me great', 'Psalms 18:35'),
        ('For you equipped me with strength for the battle;\nyou made those who rise against me sink under me', 'Psalms 18:39'),
        ('In my distress I called upon the Lord; to my God I cried for help.\nFrom his temple he heard my voice, and my cry to him reached his ears.', 'Psalms 18:6'),
        ('Even though I walk through the valley of the shadow of death,\nI will fear no evil, for you are with me;\nyour rod and your staff, they comfort me.', 'Psalms 23:4'),
        ('The earth is the Lord’s, and everything in it,\nthe world, and all who live in it;', 'Psalms 24:1'),
        ('Who may ascend the mountain of the Lord? \nWho may stand in his holy place?\nThe one who has clean hands and a pure heart,\n who does not trust in an idol or swear by a false god.', 'Psalms 24:3-4'),
        ('The Lord is my light and my salvation— whom shall I fear?\nThe Lord is the stronghold of my life— of whom shall I be afraid?', 'Psalms 27:1'),
        ('Though an army besiege me, my heart will not fear;\nthough war break out against me, even then I will be confident.', 'Psalms 27:3'),
        ('The Lord is my strength and my shield; in him my heart trusts, and I am helped; \nmy heart exults, and with my song I give thanks to him.', 'Psalms 28:7'),
        ('Be strong, and let your heart take courage, all you who wait for the Lord!', 'Psalms 31:24'),
        ('You are my hiding place; you will protect me from trouble \nand surround me with songs of deliverance.', 'Psalms 32:7'),
        ('The Lord is near to the brokenhearted and saves the crushed in spirit', 'Psalms 34:18'),
        ('Rest in the LORD and wait patiently for Him; \nDo not fret because of him who prospers in his way, \nBecause of the man who carries out wicked schemes.', 'Psalms 37:7'),
        ('Why are you down in the dumps, dear soul?\nWhy are you crying the blues?\nFix my eyes on God— soon I’ll be praising again.\nHe puts a smile on my face. He’s my God.', 'Psalms 43:5'),
        ('And call upon me in the day of trouble; \nI will deliver you, and you shall glorify me.', 'Psalms 50:15'),
        ('When I am afraid, I put my trust in you. \nIn God, whose word I praise, in God I trust; I shall not be afraid.', 'Psalms 56:3-4'),
        ('When he calls to me, I will answer him; I will be with him in trouble;\nI will rescue him and honor him. With long life I will satisfy him\nand show him my salvation.', 'Psalms 9:15-16'),
        ('Because you have made the Lord your dwelling place— the Most High, \nwho is my refuge —  no evil shall be allowed to befall you,\nno plague come near your tent.', 'Psalms 9:9-10'),
        ('I will say to the Lord, “My refuge and my fortress,\nmy God, in whom I trust.”', 'Psalms 91:2'),
        ('For he will deliver you from the snare of the fowler\nand from the deadly pestilence.', 'Psalms 91:3'),
        ('He will cover you with his pinions,\nand under his wings you will find refuge;his faithfulness is a shield and buckler.', 'Psalms 91:4'),
        ('For he is our God, and we are the people of his pasture,\nand the sheep of his hand.', 'Psalms 95:7'),
        ('Today, if you hear his voice,\ndo not harden your hearts, as at Meribah,', 'Psalms 95:7-8'),
        ('He will wipe away every tear from their eyes, and death shall be no more,\n neither shall there be mourning, nor crying, nor pain anymore, \nfor the former things have passed away.', 'Revelation 21:4'),
        ('Therefore I urge you, brethren, by the mercies of God,\nto present your bodies a living and holy sacrifice, \nacceptable to God, which is your spiritual service of worship.', 'Romans 12:1'),
        ('Do not be conformed to this world, but be transformed by \nthe renewal of your mind, that by testing you may \ndiscern what is the will of God, \nwhat is good and acceptable and perfect.', 'Romans 12:2'),
        ('The hour has already come for you to wake up from your slumber, \nbecause our salvation is nearer now than when we first believed', 'Romans 13:11'),
        ('The night is nearly over; the day is almost here.\nSo let us put aside the deeds of darkness and put on the armor of light.', 'Romans 13:12'),
        ('Rather, clothe yourselves with the Lord Jesus Christ, \nand do not think about how to gratify the desires of the flesh.', 'Romans 13:14'),
        ('May the God of hope fill you with all joy and peace in believing, \nso that by the power of the Holy Spirit you may abound in hope', 'Romans 15:13'),
        ('Accept one another, then, just as Christ accepted you, \nin order to bring praise to God', 'Romans 15:7'),
        ('Let not sin therefore reign in your mortal body, to make you obey its passions', 'Romans 5:12'),
        ('For sin will have no dominion over you, \nsince you are not under law but under grace', 'Romans 5:14'),
        ('More than that, we rejoice in our sufferings, knowing that suffering \nproduces endurance, and endurance produces character, \nand character produces hope', 'Romans 5:3-4'),
        ('We rejoice in our sufferings,knowing that suffering produces endurance, \nand endurance produces character, and character produces hope, \nand hope does not put us to shame.', 'Romans 5:3-5'),
        ('But God shows his love for us in that while we were still sinners, \nChrist died for us.', 'Romans 5:8'),
        ('So you also must consider yourselves dead to sin \nand alive to God in Christ Jesus.', 'Romans 6:11'),
        ('Let not sin therefore reign in your mortal body, \nto make you obey its passions', 'Romans 6:12'),
        ('For sin will have no dominion over you, \nsince you are not under law but under grace', 'Romans 6:14'),
        ('For I consider that the sufferings of this present time \nare not worth comparing with the glory that is to be revealed to us.', 'Romans 8:18'),
        ('And we know that for those who love God all things work together for good, \nfor those who are called according to his purpose.', 'Romans 8:28'),
        ('For I am sure that neither death nor life, nor angels nor rulers, \nnor things present nor things to come, nor powers, nor height nor depth, \nnor anything else in all creation, will be able to separate us \nfrom the love of God in Christ Jesus our Lord.', 'Romans 8:38-39'),
        ('For the grace of God has appeared that offers salvation to all people.\nIt teaches us to say “No” to ungodliness and worldly passions, \nand to live self-controlled, upright and godly lives in this present age,', 'Titus 2:11-12'),
        ('Who gave himself for us to redeem us from all wickedness \nand to purify for himself a people that are his very own, \neager to do what is good.', 'Titus 2:14'),
        ('He saved us, not because of works done by us in righteousness, \nbut according to his own mercy, \nby the washing of regeneration and renewal of the Holy Spirit,', 'Titus 3:5'),
        ('The Lord has taken away your punishment, he has turned back your enemy.\nThe Lord, the King of Israel, is with you; never again will you fear any harm.', 'Zephaniah 3:15'),
        ('The Lord your God is with you, the Mighty Warrior who saves.\nHe will take great delight in you; in his love he will no longer rebuke you,\nbut will rejoice over you with singing', 'Zephaniah 3:17'),
        ('They are like trees planted by streams of water, which yield their fruit in its season, \nand their leaves do not wither. In all that they do, they prosper.', 'Psalms 1:3'),
        ('O Lord, you have searched me and known me. You know when I sit down and when I rise up; \nyou discern my thoughts from far away. You search out my path and my lying down, \nand are acquainted with all my ways.', 'Psalms 139:1-3'),
        ('Even before a word is on my tongue, O Lord, you know it completely. \nYou hem me in, behind and before, and lay your hand upon me. \nSuch knowledge is too wonderful for me; it is so high that I cannot attain it', 'Psalms 139:4-6'),
        ('Where can I go from your spirit? Or where can I flee from your presence?', 'Psalms 139:7'),
        ('For it was you who formed my inward parts; you knit me together in my mother’s womb.\nI praise you, for I am fearfully and wonderfully made. Wonderful are your works;\nthat I know very well.', 'Psalms 139:13-14'),
        ('Search me, O God, and know my heart; test me and know my thoughts. \nSee if there is any wicked way in me, and lead me in the way everlasting', 'Psalms 139:23-24'),
        ('Just as he chose us in Christ before the foundation of the world to be holy and \nblameless before him in love. He destined us for adoption as his children through \nJesus Christ, according to the good pleasure of his will, to the praise of his \nglorious grace that he freely bestowed on us in the Beloved.', 'Ephesians 1:4-6'),
        ('In him we have redemption through his blood, the forgiveness of our trespasses, \naccording to the riches of his grace that he lavished on us.', 'Ephesians 1:7-8'),
        ('In Christ we have also obtained an inheritance, having been destined according to the\npurpose of him who accomplishes all things according to his counsel and will, \nso that we, who were the first to set our hope on Christ, might live for the praise of his glory.', 'Ephesians 1:11-12'),
        ('Therefore be imitators of God, as beloved children, and live in love, \nas Christ loved us and gave himself up for us, a fragrant offering and sacrifice to God.', 'Ephesians 5:1-2'),
        ('Create in me a clean heart, O God, and put a new and right spirit within me.\nDo not cast me away from your presence, and do not take your holy spirit from me.\nRestore to me the joy of your salvation, and sustain in me a willing spirit.', 'Psalms 51:10-12'),
        ('You desire truth in the inward being; therefore teach me wisdom in my secret heart.', 'Psalms 51:6'),
        ('Always be ready to make your defense to anyone who demands from you an\n accounting for the hope that is in you; yet do it with gentleness and reverence.', '1 Peter 3:15-16'),
        ('I am confident of this, that the one who began a good work among \nyou will bring it to completion by the day of Jesus Christ.', 'Philippians 1:6'),
        ('This was in accordance with the eternal purpose that he has carried out in\nChrist Jesus our Lord, in whom we have access to God in boldness and\nconfidence through faith in him.', 'Ephesians 3:11-12'),
        ('For all who are led by the Spirit of God are children of God. \nFor you did not receive a spirit of slavery to fall back into fear, \nbut you have received a spirit of adoption. When we cry, “Abba! Father!”', 'Romans 8:14-15'),
        ('For in hope we were saved. Now hope that is seen is not hope. For who hopes for what is seen? \nBut if we hope for what we do not see, we wait for it with patience.', 'Romans 8:24-25'),
        ('Then your light shall break forth like the dawn, and your healing shall spring up quickly;\n your vindicator shall go before you, the glory of the Lord shall be your rear guard.', 'Isaiah 58:8'),
        ('Then you shall call, and the Lord will answer; you shall cry for help, and he will say, Here I am.', 'Isaiah 58:9'),
        ('The Lord will guide you continually, and satisfy your needs in parched places,\nand make your bones strong; and you shall be like a watered garden,\nlike a spring of water, whose waters never fail.', 'Isaiah 58:11'),
        ('I will greatly rejoice in the Lord, my whole being shall exult in my God;\nfor he has clothed me with the garments of salvation, \nhe has covered me with the robe of righteousness, as a bridegroom decks himself with a garland, \nand as a bride adorns herself with her jewels.', 'Isaiah 61:10'),
        ('Love is patient; love is kind; love is not envious or boastful or arrogant or rude.\nIt does not insist on its own way; it is not irritable or resentful;\nit does not rejoice in wrongdoing, but rejoices in the truth.\nIt bears all things, believes all things, hopes all things, endures all things. Love never ends.', '1 Corinthians 13:4-8'),
        ('God is our refuge and strength, a very present help in trouble. Therefore we will not fear.', 'Psalms 46:1-2'),
        ('“Be still, and know that I am God! I am exalted among the nations, I am exalted in the earth.” \nThe Lord of hosts is with us; the God of Jacob is our refuge.', 'Psalms 46:10-11'),
        ('Know that the Lord is God. It is he that made us, and we are his;\nwe are his people, and the sheep of his pasture.', 'Psalms 100:3'),
        ('For the Lord is good; his steadfast love endures forever, and his faithfulness to all generations.', 'Psalms 100:5'),
        ('I will walk with integrity of heart within my house;\nI will not set before my eyes anything that is base.', 'Psalms 101:2-3'),
        ('But my eyes are turned toward you, O God, my Lord; in you I seek refuge;\ndo not leave me defenseless.  Keep me from the trap that they have laid for me,\nand from the snares of evildoers.', 'Psalms 141:8-9'),
        ('So if you have been raised with Christ, seek the things that are above, \nwhere Christ is, seated at the right hand of God.', 'Colossians 3:1'),
        ('Set your minds on things that are above, not on things that are on earth, \nfor you have died, and your life is hidden with Christ in God.', 'Colossians 3:2-3'),
        ('When Christ who is your life is revealed, then you also will be revealed with him in glory.', 'Colossians 3:4'),
        ('My child, when you come to serve the Lord, prepare yourself for testing. \nSet your heart right and be steadfast, and do not be impetuous in time of calamity.', 'Sirach 2:1-2'),
        ('Accept whatever befalls you, and in times of humiliation be patient. \nFor gold is tested in the fire, and those found acceptable, in the furnace of humiliation.', 'Sirach 2:4-5'),
        ('Trust in him, and he will help you; make your ways straight, and hope in him.', 'Sirach 2:6'),
        ('You who fear the Lord, wait for his mercy; do not stray, or else you may fall.', 'Sirach 2:7'),
        ('In this you rejoice, even if now for a little while you have had to suffer various trials, \nso that the genuineness of your faith—being more precious than gold that,\nthough perishable, is tested by fire—may be found to result in praise\nand glory and honor when Jesus Christ is revealed.', '1 Peter 1:6-7'),
        ('Although you have not seen him, you love him; and even though you do not see him now, \nyou believe in him and rejoice with an indescribable and glorious joy.', '1 Peter 1:8'),
        ('for you are receiving the outcome of your faith, the salvation of your souls.', '1 Peter 1:9'),
        ('Therefore prepare your minds for action; discipline yourselves; \nset all your hope on the grace that Jesus Christ will bring you when he is revealed.', '1 Peter 1:13'),
        ('Instead, as he who called you is holy, be holy yourselves in all your conduct; \nfor it is written, “You shall be holy, for I am holy.”', '1 Peter 1:15-16'),
        ('You know that you were ransomed from the futile ways inherited from your ancestors, \nnot with perishable things like silver or gold, but with the precious blood of Christ, \nlike that of a lamb without defect or blemish.', '1 Peter 1:18-19'),
        ('Through him you have come to trust in God, who raised him from the dead and gave him glory, \nso that your faith and hope are set on God.', '1 Peter 1:21'),
        ('Now that you have purified your souls by your obedience to the truth \nso that you have genuine mutual love, love one another deeply from the heart.', '1 Peter 1:22'),
        ('You have been born anew, not of perishable but of imperishable seed, \nthrough the living and enduring word of God.', '1 Peter 1:23'),
        ('For “All flesh is like grass and all its glory like the flower of grass.\nThe grass withers, and the flower falls, but the word of the Lord endures forever.”', '1 Peter 1:24-25'),
        ('Come to him, a living stone, though rejected by mortals yet \nchosen and precious in God’s sight.', '1 Peter 2:4'),
        ('Like living stones, let yourselves be built into a spiritual house, to be a holy priesthood, \nto offer spiritual sacrifices acceptable to God through Jesus Christ.', '1 Peter 2:5'),
        ('Once you were not a people, but now you are God’s people; \nonce you had not received mercy, but now you have received mercy.', '1 Peter 2:10'),
        ('He himself bore our sins in his body on the cross, so that, free from sins, \nwe might live for righteousness; by his wounds you have been healed.', '1 Peter 2:24'),
        ('For you were going astray like sheep, but now you have returned to the\nshepherd and guardian of your souls.', '1 Peter 2:25'),
        ('His divine power has given us everything needed for life and godliness, \nthrough the knowledge of him who called us by his own glory and goodness.', '2 Peter 1:3'),
        ('For this very reason, you must make every effort to support your faith\nwith goodness, and goodness with knowledge, and knowledge with self-control,\nand self-control with endurance, and endurance with godliness,\nand godliness with mutual affection, and mutual affection with love.', '2 Peter 1:5-7'),
        ('For he received honor and glory from God the Father when that\nvoice was conveyed to him by the Majestic Glory, saying, \n“This is my Son, my Beloved, with whom I am well pleased.”', '2 Peter 1:17'),
        ('How lovely is your dwelling place, O Lord of hosts!\nMy soul longs, indeed it faints for the courts of the Lord; \nmy heart and my flesh sing for joy to the living God', 'Psalms 84:1-2'),
        ('Will you not revive us again, so that your people may rejoice in you? \nShow us your steadfast love, O Lord, and grant us your salvation.', 'Psalms 85:6-7'),
        ('Let me hear what God the Lord will speak, for he will speak peace to his people, \nto his faithful, to those who turn to him in their hearts.', 'Psalms 85:8'),
        ('But God, who is rich in mercy, out of the great love with which he loved us \neven when we were dead through our trespasses, made us alive together with Christ— \nby grace you have been saved— and raised us up with him and seated us with him\nin the heavenly places in Christ Jesus', 'Ephesians 2:4-6'),
        ('For it is by grace you have been saved, through faith—and this is not from yourselves,\nit is the gift of God not by works, so that no one can boast.', 'Ephesians 2:8-9'),
        ('For we are what he has made us, created in Christ Jesus for good works, \nwhich God prepared beforehand to be our way of life.', 'Ephesians 2:10'),
        ('Such is the confidence that we have through Christ toward God. \nNot that we are competent of ourselves to claim anything as coming from us;\nour competence is from God.', '2 Corinthians 3:4-5'),
        ('Now the Lord is the Spirit, and where the Spirit of the Lord is, there is freedom.', '2 Corinthians 3:17'),
        ('And all of us, with unveiled faces, seeing the glory of the Lord as though reflected in a mirror, \nare being transformed into the same image from one degree of glory to another;\nfor this comes from the Lord, the Spirit.', '2 Corinthians 3:18'),
        ('For the Lord does not see as mortals see; they look on the outward appearance,\nbut the Lord looks on the heart.', '1 Samuel 16:7'),
        ('By his great mercy he has given us a new birth into a living hope through the\nresurrection of Jesus Christ from the dead.', '1 Peter 1:3'),
        ('and into an inheritance that is imperishable, undefiled, and unfading,\nkept in heaven for you, who are being protected by the power of God through\nfaith for a salvation ready to be revealed in the last time.', '1 Peter 1:4-5'),
        ('You who fear the Lord, trust in him, and your reward will not be lost.', 'Sirach 2:8'),
        ('You who fear the Lord, hope for good things, for lasting joy and mercy.', 'Sirach 2:9'),
        ('Consider the generations of old and see: has anyone trusted in the Lord and been disappointed? \nOr has anyone persevered in the fear of the Lord and been forsaken? \nOr has anyone called upon him and been neglected? \nFor the Lord is compassionate and merciful; he forgives sins and saves in time of distress.', 'Sirach 2:10-11'),
        ('Those who fear the Lord prepare their hearts, \nand humble themselves before him.', 'Sirach 2:17'),
        ('For the Lord God is a sun and shield; he bestows favor and honor. \nNo good thing does the Lord withhold from those who walk uprightly.', 'Psalms 84:11'),
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
async def deliver_daily(app, chat_id: int, prefs: dict, data: dict | None = None):
    users = load_users()
    name = users.get(str(chat_id), "friend")
    greeting = random.choice(GREETINGS).format(name=name)

    if data is None:
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


_BATCH_SIZE = 30


async def daily_push(context: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    from datetime import datetime
    current_time = datetime.now(SGT).strftime("%H:%M")

    due = [(int(cid), prefs) for cid, prefs in subs.items()
           if prefs.get("time", "06:00") == current_time]
    if not due:
        return

    data = await asyncio.to_thread(fetch_readings)

    for i in range(0, len(due), _BATCH_SIZE):
        batch = due[i:i + _BATCH_SIZE]
        await asyncio.gather(
            *[deliver_daily(context.application, cid, prefs, data) for cid, prefs in batch],
            return_exceptions=True,
        )


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