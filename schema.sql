-- Yasuho — base database schema (PostgreSQL / asyncpg)
-- Reconstructed from every SQL query in the bot's cogs.
--
-- Apply to a fresh database, e.g.:
--   createdb yasuho_db
--   psql -d yasuho_db -f schema.sql
-- (matches the DSN in config/bot.ini -> [Database] PostgreSQL)
--
-- All Discord IDs (guild/user/role/channel/member) are 64-bit snowflakes -> BIGINT.

-- Per-guild command prefix.
-- core.py (load all), events.py (on_guild_join / on_guild_remove), settings.py
CREATE TABLE IF NOT EXISTS prefixes (
    guild_id BIGINT PRIMARY KEY,
    prefix   TEXT NOT NULL
);

-- Per-guild auto-role granted to members on join.
-- settings.py (set/remove/info), events.py (on_member_join)
CREATE TABLE IF NOT EXISTS autorole (
    guild_id BIGINT PRIMARY KEY,
    role_id  BIGINT NOT NULL
);

-- Per-guild "Muted" role id.
-- moderation.py (mute / unmute)
CREATE TABLE IF NOT EXISTS muterole (
    guild_id BIGINT PRIMARY KEY,
    role_id  BIGINT NOT NULL
);

-- Members currently muted (one row per muted member).
-- moderation.py (mute inserts, unmute deletes)
CREATE TABLE IF NOT EXISTS mutedmembers (
    mguild_id BIGINT NOT NULL,
    member_id BIGINT NOT NULL,
    PRIMARY KEY (mguild_id, member_id)
);

-- Warn counter per (guild, user); the bot auto-kicks at 3 warns.
-- moderation.py (warn / warninfo / delwarn)
CREATE TABLE IF NOT EXISTS warns (
    guild_id    BIGINT  NOT NULL,
    user_id     BIGINT  NOT NULL,
    warns_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Twitch "go live" alert config (message supports [url] / [game] placeholders).
-- twitch.py (add / remove / info, on_member_update)
CREATE TABLE IF NOT EXISTS twitch_alert (
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message    TEXT,
    PRIMARY KEY (guild_id, user_id, channel_id)
);

-- Auto temp-voice "hub" channels (max 3 per guild, enforced in code).
-- rooms.py (setup / remove / list, on_voice_state_update)
CREATE TABLE IF NOT EXISTS auto_room (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT PRIMARY KEY
);

-- Bot-wide blacklist: listed users are auto-banned when they join any guild.
-- events.py (on_member_join)
CREATE TABLE IF NOT EXISTS blbot (
    member_id BIGINT PRIMARY KEY
);

-- ============================================================
-- Feature tables (info.py / help.py need none)
-- ============================================================

-- Mod-action / server-event log channel per guild.  modlog.py
CREATE TABLE IF NOT EXISTS modlog (
    guild_id   BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL
);

-- Generic scheduled timers (reminders, tempban, ...).  reminders.py
CREATE TABLE IF NOT EXISTS timers (
    id      BIGSERIAL   PRIMARY KEY,
    event   TEXT        NOT NULL,
    expires TIMESTAMPTZ NOT NULL,
    created TIMESTAMPTZ NOT NULL DEFAULT now(),
    extra   JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS timers_expires_idx ON timers (expires);

-- Per-(guild, user) XP for the leveling system.  leveling.py
CREATE TABLE IF NOT EXISTS levels (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    xp       BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Starboard config + posted-entry mapping.  starboard.py
CREATE TABLE IF NOT EXISTS starboard (
    guild_id   BIGINT  PRIMARY KEY,
    channel_id BIGINT  NOT NULL,
    threshold  INTEGER NOT NULL DEFAULT 3
);
CREATE TABLE IF NOT EXISTS starboard_entries (
    message_id      BIGINT PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    star_message_id BIGINT
);

-- Configurable welcome message per guild.  welcome.py
CREATE TABLE IF NOT EXISTS welcome (
    guild_id   BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message    TEXT
);

-- Emoji -> role mappings bound to a message.  reactionroles.py
CREATE TABLE IF NOT EXISTS reaction_roles (
    message_id BIGINT NOT NULL,
    emoji      TEXT   NOT NULL,
    role_id    BIGINT NOT NULL,
    guild_id   BIGINT NOT NULL,
    PRIMARY KEY (message_id, emoji)
);

-- Per-user AFK status.  afk.py
CREATE TABLE IF NOT EXISTS afk (
    user_id BIGINT PRIMARY KEY,
    message TEXT,
    since   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-guild automod toggles.  automod.py
CREATE TABLE IF NOT EXISTS automod (
    guild_id BIGINT PRIMARY KEY,
    antilink BOOLEAN NOT NULL DEFAULT FALSE,
    antispam BOOLEAN NOT NULL DEFAULT FALSE
);

-- Per-user gamer IDs / friend codes.  profiles.py
CREATE TABLE IF NOT EXISTS profiles (
    user_id    BIGINT PRIMARY KEY,
    switch_fc  TEXT,
    threeds_fc TEXT,
    battletag  TEXT,
    riotid     TEXT,
    steamid    TEXT
);

-- Per-user avatar change history (raw PNG bytes, capped to ~50/user in code).  avatarhistory.py
CREATE TABLE IF NOT EXISTS avatar_history (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    avatar     BYTEA       NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS avatar_history_user_idx ON avatar_history (user_id, changed_at DESC);

-- Per-user AniList OAuth access token, encrypted at rest (Fernet ciphertext;
-- the key lives in config, never in the DB).  anilist.py
CREATE TABLE IF NOT EXISTS anilist_tokens (
    user_id BIGINT      PRIMARY KEY,
    token   TEXT        NOT NULL,
    expires TIMESTAMPTZ
);

-- Per-user preferences (JSONB blob).  tools/settings.py, usersettings.py, help.py
CREATE TABLE IF NOT EXISTS user_settings (
    user_id  BIGINT PRIMARY KEY,
    settings JSONB  NOT NULL DEFAULT '{}'::jsonb
);

-- Per-guild feature toggles & preferences (JSONB blob).  tools/settings.py, settings.py
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    settings JSONB  NOT NULL DEFAULT '{}'::jsonb
);
