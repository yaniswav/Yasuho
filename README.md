# Yasuho

A feature-rich, multilingual Discord bot (moderation, leveling, music, AniList,
engagement tools and more), built on discord.py 2.7 with a heavily interactive,
component-driven UX (select menus, buttons, modals, rich embeds).

You can add the bot to your server using this [invite link](https://invite.yasuho.xyz).

## Features

- **Moderation 🛡️** - kick / ban / mute (durable), `massban` for raid cleanup, a
  numbered case system with warns, purge / clean, a hybrid AutoMod (anti-link /
  anti-invite / anti-spam, native + custom), an interactive mod-log panel, and a
  blacklist.
- **Leveling 📈** - message XP, generated rank cards, and a paginated leaderboard
  (off by default, toggled per server).
- **AniList 🍥** - anime / manga / character / studio lookup, trending and
  seasonal browse, OAuth account linking and list editing - a fully interactive
  package (result pickers, section buttons, edit modals, your stats).
- **Engagement 🎭** - a starboard (with a `top` leaderboard of the most-starred
  messages), reaction roles, customizable **button roles**, a full **welcome
  embed builder**, a **Twitch live-alert builder** (embed or plain message),
  AFK, temporary voice rooms, and avatar / banner history.
- **Fun & tools 🎲** - games, **native Discord polls**, image commands, snipe,
  translate, wiki / lyrics, gaming profiles, reminders, and more.
- **Music 🎶** - full playback with a Components V2 now-playing controller
  (pause / skip / volume / loop / shuffle / queue / add), per-user favourites and
  playlists, and idle auto-disconnect, via [sonolink](https://github.com/sonolink/sonolink)
  + a Lavalink v4 server (set it up with `./setup-lavalink.sh`).
- **Guided commands 💬** - run a command without its arguments and Yasuho opens a
  small form (role / channel / member menus + a modal) to fill them in, instead
  of just printing a usage line.

Commands work as both **prefix** (`y!`) and **slash** (`/`) where possible.
Type `y!help` to browse.

## Localization 🌍

Yasuho replies in the user's language. Pick yours with `/language` (or set a
server default with `/language server <code>`); it also follows your Discord
client language automatically. Translations use GNU gettext (one shared catalog
compiled per locale), with English as the source and automatic fallback for
anything not yet translated.

Shipped languages: **English, French, Japanese, Greek**. Adding another is
zero-code - translate a catalog and compile it (see the i18n notes in
`tools/i18n.py` and `locales/build.py`). Slash command descriptions are localized
too via an `app_commands.Translator` (`tools/translator.py`).

## Setup (no Docker)

Yasuho ships a one-command bootstrap. On a fresh Linux host:

```bash
git clone https://github.com/yaniswav/Yasuho.git
cd Yasuho
./setup.sh        # idempotent, safe to re-run
./run.sh          # starts the bot (auto-restart loop)
```

`setup.sh` creates a virtualenv + installs dependencies, copies
`config/*.template.ini` -> `config/*.ini`, **auto-generates the Fernet encryption
key**, creates the local PostgreSQL role/database and wires the DSN into
`config/bot.ini`, and prompts for your Discord bot token. The **database schema is
created automatically** on first start (`schema.sql`).

> PostgreSQL must be installed on the host first: `sudo apt install postgresql`.
> Run it on **localhost** (same host as the bot).

> Music is optional: run `./setup-lavalink.sh` for a Lavalink v4 server, then
> uncomment the `[Lavalink]` section in `config/bot.ini`. Without it the bot runs
> fine, just without music.

### Manual configuration

The real `config/bot.ini` and `config/tokens.ini` are **gitignored** (they hold
secrets) - copy them from the `*.template.ini` files and fill in:

- `bot.ini` -> `[Bot_Token] Token`, `[Database] PostgreSQL`, and optionally
  `[Lavalink]` for music. Cogs are **auto-discovered** from the `cogs/` folders -
  there is no extension list to maintain.
- `tokens.ini` -> optional feature keys: AniList (`clientId` / `clientSecret`),
  Genius lyrics, OpenWeather, NASA, top.gg...

## Built With

* [discord.py](https://github.com/Rapptz/discord.py) 2.7
* [asyncpg](https://github.com/MagicStack/asyncpg) + PostgreSQL
* [sonolink](https://github.com/sonolink/sonolink) + Lavalink v4 (music)
* [Babel](https://babel.pocoo.org/) / gettext (localization)
* [Pillow](https://python-pillow.org/), [cryptography](https://github.com/pyca/cryptography), and the [AniList GraphQL API](https://anilist.gitbook.io/anilist-apiv2-docs/)

## Author

* **yaniswav** - _Development and Hosting_ - [yaniswav](https://github.com/yaniswav)

[![Yasuho on top.gg](https://top.gg/api/widget/498580306773934081.svg)](https://top.gg/bot/498580306773934081)
