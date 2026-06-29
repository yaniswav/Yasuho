# Yasuho

A feature-rich Discord bot (moderation, leveling, fun, AniList, music, and more), built on discord.py 2.7.

You can add the bot to your server using this [invite link](https://invite.yasuho.xyz).

## Features

- **Moderation 🛡️** - kick/ban/mute (durable), warns, purge, automod (anti-link/spam), modlog, blacklist.
- **Leveling 📈** - message XP, rank and a paginated leaderboard.
- **AniList 🍥** - anime/manga/character/studio lookup, trending/seasonal browse, account linking (OAuth) and list editing - all interactive (select menus, buttons, modals).
- **Engagement 🎭** - starboard, reaction roles, welcome messages, AFK, temp voice rooms.
- **Fun & tools 🎲** - games, image commands, polls, snipe, translate, wiki/lyrics, avatar history, and more.
- **Music 🎶** - playback controls (currently on hold; wavelink is EOL).

Commands work as both **prefix** (`y!`) and **slash** (`/`) where possible. Type `y!help` to browse.

## Setup (no Docker)

Yasuho ships a one-command bootstrap. On a fresh Linux host:

```bash
git clone https://github.com/yaniswav/Yasuho.git
cd Yasuho
./setup.sh        # idempotent, safe to re-run
./run.sh          # starts the bot (auto-restart loop)
```

`setup.sh` creates a virtualenv + installs dependencies, copies `config/*.template.ini` → `config/*.ini`,
**auto-generates the Fernet encryption key**, creates the local PostgreSQL role/database and wires the DSN
into `config/bot.ini`, and prompts for your Discord bot token. The **database schema is created automatically**
on first start (`schema.sql`).

> PostgreSQL must be installed on the host first: `sudo apt install postgresql`. Run it on **localhost** (same host as the bot).

### Manual configuration

The real `config/bot.ini` and `config/tokens.ini` are **gitignored** (they hold secrets) - copy them from the
`*.template.ini` files and fill in:

- `bot.ini` → `[Bot_Token] Token`, `[Database] PostgreSQL`, the cog list under `[Extension] Extensions`.
- `tokens.ini` → optional feature keys: AniList (`clientId`/`clientSecret`), Genius lyrics, OpenWeather, NASA, top.gg...

## Built With

* [discord.py](https://github.com/Rapptz/discord.py)
* [asyncpg](https://github.com/MagicStack/asyncpg) + PostgreSQL
* [Pillow](https://python-pillow.org/), [cryptography](https://github.com/pyca/cryptography), and the [AniList GraphQL API](https://anilist.gitbook.io/anilist-apiv2-docs/)

## Author

* **yaniswav** - _Development and Hosting_ - [yaniswav](https://github.com/yaniswav)

[![Yasuho on top.gg](https://top.gg/api/widget/498580306773934081.svg)](https://top.gg/bot/498580306773934081)
