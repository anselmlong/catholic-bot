# Catholic Daily Bot 🙏

A Telegram bot for daily Catholic mass readings and random Bible verses. Serves the Roman Catholic lectionary with configurable daily push delivery.

## Features

- **📖 Today's Readings** — Full mass readings (First Reading, Psalm, Second Reading, Gospel) or gospel-only
- **🙏 Random Truth** — A random Bible verse from anywhere in scripture
- **⚙️ Subscriptions** — Configurable daily push at 6am, 7am, or 8am SGT
- **✂️ Customizable** — Choose between full readings or gospel-only, toggle daily truth on/off
- **Name onboarding** — Greets you by name on `/start`

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Onboarding & main menu |
| `/today` | Today's mass readings |
| `/truth` | Random Bible verse |
| `/subscribe` | Set up daily push with inline preferences |
| `/help` | All commands |
| `/users` | (admin only) List registered users |

Or use the persistent keyboard: **📖 Today**, **🙏 Truth**, **⚙️ Subscribe**, **ℹ️ Help**

## Data Sources

- **Mass readings**: scraped from [Universalis](https://universalis.com) (Jerusalem Bible) with automatic fallback to [USCCB](https://bible.usccb.org/bible/readings/) (NAB)
- **Random verses**: NET Bible API ([labs.bible.org](https://labs.bible.org/)) by the Society of Biblical Literature

## Tech

- Python 3, `python-telegram-bot` v22
- Runs as a long-polling service on a VPS
- Subscriptions and users stored as local JSON
- Daily delivery via APScheduler job queue
- On-disk LRU cache for readings

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) and get a token
2. Clone the repo and `cd` into it
3. Create `.env` with `BOT_TOKEN=your_token_here`
4. Install deps: `pip install python-telegram-bot beautifulsoup4 lxml requests python-dotenv`
5. Run: `python3 catholic_bot.py`

## License

MIT