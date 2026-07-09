-- Yasuho - base database schema (PostgreSQL / asyncpg)
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
    message_id      BIGINT  PRIMARY KEY,
    guild_id        BIGINT  NOT NULL,
    star_message_id BIGINT,
    channel_id      BIGINT,   -- channel the star post lives in (for stable jump links)
    star_count      INTEGER NOT NULL DEFAULT 0
);
-- Migrate pre-existing installs (no-op on a fresh database):
ALTER TABLE starboard_entries ADD COLUMN IF NOT EXISTS star_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE starboard_entries ADD COLUMN IF NOT EXISTS channel_id BIGINT;

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

-- Per-user image history: global avatars, per-guild avatars and banners
-- (raw PNG bytes, capped to ~50 per user/guild/kind in code).  avatarhistory.py
CREATE TABLE IF NOT EXISTS avatar_history (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT,                                 -- NULL for global avatars & banners
    kind       TEXT        NOT NULL DEFAULT 'global',   -- 'global' | 'guild' | 'banner'
    ref        TEXT,                                    -- asset key/hash, for de-duplication
    avatar     BYTEA       NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migrate pre-existing installs (no-ops on a fresh database):
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS guild_id BIGINT;
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'global';
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS ref TEXT;
CREATE INDEX IF NOT EXISTS avatar_history_user_idx ON avatar_history (user_id, kind, changed_at DESC);

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

-- Moderation cases / infractions: one row per mod action, numbered per guild.
-- moderation.py (ban/kick/mute/warn create cases; case/cases/reason read/edit)
CREATE TABLE IF NOT EXISTS cases (
    id           BIGSERIAL   PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    case_number  INTEGER     NOT NULL,            -- sequential per guild (#1, #2, ...)
    user_id      BIGINT      NOT NULL,            -- the target
    moderator_id BIGINT      NOT NULL,
    action       TEXT        NOT NULL,            -- ban / kick / mute / warn / unban / ...
    reason       TEXT,
    expires      TIMESTAMPTZ,                     -- for tempban/tempmute (NULL = permanent)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, case_number)
);
CREATE INDEX IF NOT EXISTS cases_guild_user_idx ON cases (guild_id, user_id);

-- Self-assignable button roles: one row per (message, role) button on a panel.
-- buttonroles.py (admin builds a panel; persistent views toggle the roles).
-- style is a discord.ButtonStyle int (1 primary / 2 secondary / 3 success /
-- 4 danger); the builder lets each button pick its own label, emoji and style.
CREATE TABLE IF NOT EXISTS button_roles (
    message_id BIGINT   NOT NULL,
    guild_id   BIGINT   NOT NULL,
    channel_id BIGINT   NOT NULL,
    role_id    BIGINT   NOT NULL,
    label      TEXT,
    emoji      TEXT,
    style      SMALLINT NOT NULL DEFAULT 2,
    PRIMARY KEY (message_id, role_id)
);
CREATE INDEX IF NOT EXISTS button_roles_guild_idx ON button_roles (guild_id);
-- Migrate pre-existing installs (no-op on a fresh database):
ALTER TABLE button_roles ADD COLUMN IF NOT EXISTS style SMALLINT NOT NULL DEFAULT 2;

-- Per-user favourite tracks (a personal playlist).  music/music.py
CREATE TABLE IF NOT EXISTS music_favorites (
    user_id     BIGINT NOT NULL,
    identifier  TEXT   NOT NULL,   -- Lavalink track identifier (dedup key)
    title       TEXT,
    author      TEXT,
    uri         TEXT,
    source_name TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, identifier)
);
CREATE INDEX IF NOT EXISTS music_favorites_user_idx ON music_favorites (user_id, added_at DESC);

-- Live player state, persisted so playback survives a (fast) bot restart.
-- One row per guild with an active player; cleared on disconnect/stop. Tracks
-- are stored as Lavalink `encoded` strings so they restore exactly (decoded via
-- the node, no re-search). The position is extrapolated from position_ms +
-- (now - updated_at) at restore time; only recent snapshots are resumed, so the
-- bot never barges back into a channel after a long downtime.  music/music.py
CREATE TABLE IF NOT EXISTS music_state (
    guild_id              BIGINT      PRIMARY KEY,
    voice_channel_id      BIGINT      NOT NULL,
    home_channel_id       BIGINT,
    dj_id                 BIGINT,
    volume                INTEGER     NOT NULL DEFAULT 100,
    loop_mode             SMALLINT    NOT NULL DEFAULT 0,    -- 0 off / 1 track / 2 queue
    position_ms           BIGINT      NOT NULL DEFAULT 0,
    paused                BOOLEAN     NOT NULL DEFAULT FALSE,
    current_track         TEXT,                              -- Lavalink encoded string
    queue                 TEXT[]      NOT NULL DEFAULT '{}', -- upcoming tracks, encoded
    controller_message_id BIGINT,                            -- now-playing controller, to delete the stale one on restore
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migrate pre-existing installs (no-op on a fresh database):
ALTER TABLE music_state ADD COLUMN IF NOT EXISTS controller_message_id BIGINT;

-- Lavalink session id per node, so a restarting bot can resume the SAME Lavalink
-- session (players kept alive by resume_timeout) instead of a fresh one - the
-- basis for gap-free restarts.  core.py, tools/music_state.py, music/music.py
CREATE TABLE IF NOT EXISTS music_node_session (
    node_id    TEXT        PRIMARY KEY,
    session_id TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Secondary-column indexes for non-PK lookups (see DB audit)
-- ============================================================
-- Leaderboard: WHERE guild_id ORDER BY xp DESC LIMIT N -> index range scan, no sort.
CREATE INDEX IF NOT EXISTS levels_guild_xp_idx ON levels (guild_id, xp DESC);
-- These tables are looked up / cleaned by guild_id, which is not their primary key.
CREATE INDEX IF NOT EXISTS auto_room_guild_idx ON auto_room (guild_id);
CREATE INDEX IF NOT EXISTS reaction_roles_guild_idx ON reaction_roles (guild_id);
CREATE INDEX IF NOT EXISTS starboard_entries_guild_idx ON starboard_entries (guild_id);

-- Per-guild custom (canned) commands invoked by the guild prefix. The response
-- is a JSONB blob: {"type":"text","content":"..."} or
-- {"type":"embed","embed":{...}} (an embed_creator blob).  cogs/config/customcommands.py
CREATE TABLE IF NOT EXISTS custom_commands (
    guild_id   BIGINT      NOT NULL,
    name       TEXT        NOT NULL,             -- lowercase, one token
    response   JSONB       NOT NULL,
    created_by BIGINT,
    uses       BIGINT      NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, name)
);

-- Self-assignable role menus (Components V2). One row per posted menu message;
-- config is a JSONB blob holding the menu kind (buttons/select), its options
-- (role_id/label/emoji/description) and its rules (min/max, exclusive).
-- cogs/config/rolemenus.py
CREATE TABLE IF NOT EXISTS role_menus (
    message_id BIGINT      PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    channel_id BIGINT      NOT NULL,
    config     JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS role_menus_guild_idx ON role_menus (guild_id);

-- AniList activity feed: per-guild feed channels that mirror followed AniList
-- users' new activities (list progress + text posts). A guild may configure up
-- to 2 feed channels (MAX_FEEDS_PER_GUILD, enforced in code). ``types`` selects
-- which activity kinds are posted (the private MESSAGE type is never mirrored);
-- ``self_add`` lets a member with a linked AniList account add themselves;
-- ``enabled``/``fail_count`` back the auto-disable of a feed whose channel keeps
-- erroring. Guild lookups ride the (guild_id, ...) PK prefix, so no extra index.
-- cogs/anilist/feed.py (owner cog, later lot)
CREATE TABLE IF NOT EXISTS anilist_feeds (
    guild_id   BIGINT      NOT NULL,
    channel_id BIGINT      NOT NULL,                       -- a text channel OR thread id
    types      TEXT[]      NOT NULL DEFAULT '{ANIME_LIST,MANGA_LIST,TEXT}',
    self_add   BOOLEAN     NOT NULL DEFAULT FALSE,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    fail_count INTEGER     NOT NULL DEFAULT 0,              -- consecutive delivery failures
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
);

-- The AniList users a feed follows (max 25 per feed, MAX_FOLLOWS_PER_FEED,
-- enforced in code). One row per (feed, AniList user); ``anilist_user_id`` is
-- AniList's numeric user id and ``anilist_username`` a cached display name for
-- the setup panel. Lookups by feed ride the (guild_id, channel_id, ...) PK
-- prefix.  cogs/anilist/feed.py (later lot)
CREATE TABLE IF NOT EXISTS anilist_follows (
    guild_id         BIGINT      NOT NULL,
    channel_id       BIGINT      NOT NULL,
    anilist_user_id  INTEGER     NOT NULL,                 -- AniList numeric user id
    anilist_username TEXT,                                 -- cached name for the panel
    added_by         BIGINT,                               -- Discord user who added them
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id, anilist_user_id)
);

-- Global high-water mark for the AniList activity poller: a single row holding
-- the newest activity already fanned out. AniList's Page.activities has NO
-- id_greater argument, so the poller cursors on ``last_created_at``
-- (createdAt_greater in unix seconds, the server-side filter) PLUS a client-side
-- id high-water mark (``last_activity_id``): two activities can share the same
-- createdAt second, so createdAt alone can duplicate or skip at the boundary -
-- the real dedup is dropping ids <= last_activity_id. Both marks only ever
-- advance, never regress. The fixed id + CHECK keep this table to exactly one
-- row.  cogs/anilist/feed.py
CREATE TABLE IF NOT EXISTS anilist_feed_state (
    id               SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_activity_id BIGINT      NOT NULL DEFAULT 0,
    last_created_at  BIGINT      NOT NULL DEFAULT 0,   -- createdAt_greater cursor (unix seconds)
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
