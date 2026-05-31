# Catholic Daily Bot 🙏

A Telegram bot for daily Catholic mass readings and random Bible verses. Serves the full Roman Catholic lectionary from USCCB with automatic daily delivery at your preferred time.

## Features

- **📖 Today's Readings** — Full mass readings (First Reading, Psalm, Second Reading, Gospel) from the USCCB
- **🙏 Random Truth** — A random Bible verse from anywhere in scripture via the NET Bible API
- **⚙️ Subscriptions** — Configurable daily push at 6am, 7am, or 8am SGT
- **✂️ Customizable** — Choose between full readings or gospel-only, toggle daily truth on/off

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message with keyboard shortcuts |
| `/today` | Today's mass readings |
| `/truth` | Random Bible verse |
| `/subscribe` | Set up daily push with inline preferences |
| `/unsubscribe` | Stop daily readings |
| `/mysub` | View your current subscription |
| `/help` | All commands and info |

Or use the persistent keyboard: **📖 Today**, **🙏 Truth**, **⚙️ Subscribe**, **ℹ️ Help**

## Data Sources

- **Mass readings**: scraped from [USCCB](https://bible.usccb.org/bible/readings/) via `catholic-mass-readings`
- **Random verses**: [NET Bible API](https://labs.bible.org/) by the Society of Biblical Literature

## Tech

- Python 3, `python-telegram-bot` v22
- Runs as a long-polling service on a VPS
- Subscriptions stored as local JSON
- Daily delivery via APScheduler job queue

## License

MIT
