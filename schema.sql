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
    id         BIGSERIAL   PRIMARY KEY,
    event      TEXT        NOT NULL,
    expires    TIMESTAMPTZ NOT NULL,
    created    TIMESTAMPTZ NOT NULL DEFAULT now(),
    extra      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    attempts   INTEGER     NOT NULL DEFAULT 0,
    last_error TEXT,
    claimed_at TIMESTAMPTZ
);
-- Migrate pre-existing installs (no-ops on a fresh database):
ALTER TABLE timers ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE timers ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE timers ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS timers_expires_idx ON timers (expires);
CREATE INDEX IF NOT EXISTS timers_pending_expires_idx
    ON timers (expires) WHERE claimed_at IS NULL;
-- Serves the per-user "my pending reminders" list and the pending-count guard:
-- filter on (event, author) then read already-ordered by expires. Additive.
CREATE INDEX IF NOT EXISTS timers_reminder_author_idx
    ON timers (event, (extra->>'author_id'), expires);

-- Per-(guild, user) XP for the leveling system.  leveling.py
CREATE TABLE IF NOT EXISTS levels (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    xp       BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Per-guild leveling configuration: the knobs the XP grant path reads. Split out
-- of the guild_settings.leveling_enabled JSONB bool so leveling gains real
-- per-guild settings without bloating that shared blob. READ-THROUGH migration:
-- the Leveling cog prefers a row here when one exists and otherwise falls back to
-- the legacy leveling_enabled JSONB value (tools/leveling.py resolve_config), so a
-- guild that had leveling on keeps it on until its next toggle writes a row - and a
-- row always wins, so switching leveling OFF via this table is never undone by a
-- stale JSONB true. `enabled`, `cooldown_seconds` and the `xp_min`/`xp_max` band
-- are wired into the grant path now; `announce_mode` (off|channel|dm|fixed),
-- `announce_channel_id` and `announce_template` are reserved for later lots. One
-- row per guild; lookups ride the PK.  leveling.py, cogs/config/settings.py
CREATE TABLE IF NOT EXISTS level_config (
    guild_id            BIGINT  PRIMARY KEY,
    enabled             BOOLEAN NOT NULL DEFAULT FALSE,
    cooldown_seconds    INTEGER NOT NULL DEFAULT 60,
    xp_min              INTEGER NOT NULL DEFAULT 15,
    xp_max              INTEGER NOT NULL DEFAULT 25,
    announce_mode       TEXT    NOT NULL DEFAULT 'channel',  -- off | channel | dm | fixed
    announce_channel_id BIGINT,                              -- target channel for announce_mode = 'fixed' (later lot)
    announce_template   TEXT,                                -- custom level-up message template (later lot)
    rewards_mode        TEXT    NOT NULL DEFAULT 'stack',     -- stack | replace (level_rewards.py)
    voice_xp_enabled    BOOLEAN NOT NULL DEFAULT FALSE,       -- opt-in: earn XP for time in voice (voice_xp.py)
    voice_xp_per_minute INTEGER NOT NULL DEFAULT 5,           -- XP per eligible minute in voice (bounds 1..60)
    event_factor        REAL,                                 -- active timed double-XP event's multiplier, NULL = no event (L4)
    event_ends_at       TIMESTAMPTZ                            -- when the event above expires; an expired row is ignored at read time and lazily nulled (no timer) (L4)
);
-- Migrate pre-existing installs (no-op on a fresh database): level_config already
-- exists on any deploy that shipped the L0/L1 leveling lot, so CREATE TABLE IF NOT
-- EXISTS above never adds these later columns there - the ALTERs are what actually
-- install them on those databases (every read/write would error without them).
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS rewards_mode TEXT NOT NULL DEFAULT 'stack';
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS voice_xp_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS voice_xp_per_minute INTEGER NOT NULL DEFAULT 5;
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS event_factor REAL;
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS event_ends_at TIMESTAMPTZ;

-- Per-guild XP multipliers (L4, the Lurkr rule): boost or reduce XP globally,
-- per channel/category, or per role. ``kind = 'global'`` always uses
-- ``target_id = 0`` (tools.leveling.GLOBAL_MULTIPLIER_TARGET_ID), so the PK
-- caps a guild at exactly one global row; ``kind = 'channel'`` rows match
-- EITHER a text channel id OR a category id (same one-row-per-category design
-- as level_no_xp - see that table's comment); ``kind = 'role'`` rows match a
-- member's held roles. ``factor`` is bounded 0.0..5.0 in code
-- (tools.leveling.validate_multiplier_factor) - 0.0 is a valid, explicitly
-- supported "mute XP via multiplier" outcome. Capped at 25 rows/guild across
-- every kind (tools.leveling.MAX_MULTIPLIERS_PER_GUILD), enforced RACE-SAFELY
-- by the same WHERE-COUNT INSERT guard as level_rewards/level_no_xp. Stacking
-- (effective = global * channel * role * event, channel-beats-category,
-- highest-role-wins) is computed by tools.leveling.compute_multiplier against
-- a per-guild MultiplierSnapshot cached in-memory
-- (cogs/community/leveling.py's ``self._multipliers``, a BoundedLRU beside the
-- no-xp snapshot cache) - the hot paths (on_message, the voice sweep) never
-- query this table directly.  cogs/community/leveling.py,
-- cogs/community/voice_xp.py, cogs/community/level_config_ui.py
CREATE TABLE IF NOT EXISTS xp_multipliers (
    guild_id  BIGINT NOT NULL,
    kind      TEXT   NOT NULL,   -- 'global' | 'channel' | 'role'
    target_id BIGINT NOT NULL DEFAULT 0,  -- 0 for 'global'
    factor    REAL   NOT NULL,
    PRIMARY KEY (guild_id, kind, target_id)
);
CREATE INDEX IF NOT EXISTS xp_multipliers_guild_idx ON xp_multipliers (guild_id);

-- Level-up role rewards (L2): one row per (guild, level, role) rule. A member who
-- reaches `level` is owed `role_id`. `rewards_mode` on level_config (above)
-- decides whether a member keeps every earned reward role ('stack', the default)
-- or only the roles tied to the single highest level they have reached
-- ('replace'). Capped at 25 rules per guild in code (tools/level_rewards.py).
-- Reconciliation is on-demand only: a rule added for a level a member already
-- passed is granted the next time THEY level up, never by a retroactive sweep.
-- A grant that hits a since-deleted role prunes that role's row(s) lazily and
-- logs INFO (cogs/community/level_rewards.py).
CREATE TABLE IF NOT EXISTS level_rewards (
    guild_id BIGINT  NOT NULL,
    level    INTEGER NOT NULL,
    role_id  BIGINT  NOT NULL,
    PRIMARY KEY (guild_id, level, role_id)
);
CREATE INDEX IF NOT EXISTS level_rewards_guild_idx ON level_rewards (guild_id);

-- No-XP zones (L3): channels/categories and roles where messages never earn
-- XP. ``kind = 'channel'`` rows match EITHER a text channel id OR a category
-- id (a category is itself a channel on Discord's side, so muting a whole
-- category is one row, not one per channel inside it - see
-- tools/leveling.py NoXpSnapshot); ``kind = 'role'`` rows match any role the
-- message author holds. Capped at 50 entries/guild
-- (tools.leveling.MAX_NO_XP_PER_GUILD), enforced RACE-SAFELY by the same
-- WHERE-COUNT INSERT guard as level_rewards. HOT PATH: on_message never
-- queries this table directly - the Leveling cog loads a guild's rows once
-- (on its first grant-eligible message, or immediately after any write here)
-- into an in-memory NoXpSnapshot (two frozensets) capped to ~2048 guilds via
-- tools.lru_cache.BoundedLRU, so the steady-state per-message cost is pure set
-- membership, zero DB.  cogs/community/leveling.py, cogs/community/level_config_ui.py
CREATE TABLE IF NOT EXISTS level_no_xp (
    guild_id  BIGINT NOT NULL,
    kind      TEXT   NOT NULL,   -- 'channel' | 'role'
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, kind, target_id)
);
CREATE INDEX IF NOT EXISTS level_no_xp_guild_idx ON level_no_xp (guild_id);

-- Per-(guild, user, period) XP rollup (leveling L6): weekly/monthly
-- leaderboards alongside the lifetime `levels` table above. NO destructive
-- resets - a period simply rolls to a new key once it ends; old rows are
-- pruned LAZILY (see below), never wiped by a reset job. Written by the SAME
-- statements as every `levels` grant, IN THE SAME round trip (a single
-- multi-CTE SQL command - see cogs/community/leveling.py's on_message and
-- cogs/community/voice_xp.py's batched sweep upsert), never a separate query.
-- ``period_key`` is pure date maths from UTC "now" (tools.leveling.
-- current_period_keys): ``W<iso_year>-<iso_week>`` (ISO year-week, e.g.
-- 'W2026-28') for the weekly view, ``M<year>-<month>`` (e.g. 'M2026-07') for
-- the monthly view, both zero-padded so period keys of the same kind sort
-- lexically in chronological order - a grant writes BOTH keys every time.
-- Retention: rows older than ~3 periods (tools.leveling.PRUNE_PERIODS_BACK)
-- are dropped by a cheap DELETE piggybacked on the first grant/credit of a
-- NEW period per guild - decided by an in-memory "last seen period" marker
-- on the Leveling cog (tools.leveling.period_marker_changed), never a
-- background timer.  cogs/community/leveling.py, cogs/community/voice_xp.py
CREATE TABLE IF NOT EXISTS xp_period (
    guild_id   BIGINT  NOT NULL,
    user_id    BIGINT  NOT NULL,
    period_key TEXT    NOT NULL,   -- 'W<iso_year>-<iso_week>' | 'M<year>-<month>'
    xp         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, period_key)
);
-- /top weekly|monthly: WHERE guild_id AND period_key ORDER BY xp DESC LIMIT N
-- -> index range scan, no sort (mirrors levels_guild_xp_idx below).
CREATE INDEX IF NOT EXISTS xp_period_guild_period_xp_idx
    ON xp_period (guild_id, period_key, xp DESC);

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

-- Per-user image history: global avatars, per-guild avatars and banners.
-- New rows are bounded WebP; retention keeps at most 30 per series and prunes
-- rows older than 18 months while preserving the newest 5.  avatarhistory.py
CREATE TABLE IF NOT EXISTS avatar_history (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT,                                 -- NULL for global avatars & banners
    kind       TEXT        NOT NULL DEFAULT 'global',   -- 'global' | 'guild' | 'banner'
    ref        TEXT,                                    -- asset key/hash, for de-duplication
    avatar     BYTEA       NOT NULL,
    image_format TEXT      NOT NULL DEFAULT 'png',      -- png (legacy) | webp | original
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migrate pre-existing installs (no-ops on a fresh database):
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS guild_id BIGINT;
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'global';
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS ref TEXT;
ALTER TABLE avatar_history ADD COLUMN IF NOT EXISTS image_format TEXT NOT NULL DEFAULT 'png';
CREATE INDEX IF NOT EXISTS avatar_history_user_idx ON avatar_history (user_id, kind, changed_at DESC);
-- Retention/pagination path (avatarhistory.py): one image "series" is a
-- (user_id, kind, guild_id) tuple read newest-first; this composite serves the
-- keep-newest-N prune and the paged viewer without a sort.
CREATE INDEX IF NOT EXISTS avatar_history_series_idx
    ON avatar_history (user_id, kind, guild_id, changed_at DESC, id DESC);

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
-- Cross-guild "this user's history" reads (retention export/purge) filter by
-- user_id newest-first; this serves them without scanning the guild index.
CREATE INDEX IF NOT EXISTS cases_user_created_idx ON cases (user_id, created_at DESC);

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

-- Shared server playlists: a named snapshot of a guild's current track + queue
-- (Lavalink `encoded` strings, the music_state precedent) that any member can
-- load later. `name_norm` is a casefolded, whitespace-clean key so the primary
-- key enforces one playlist per name per guild, case-insensitively. Hard-capped
-- in code (25 playlists/guild, 200 tracks each), so the table and its stored
-- blobs stay bounded.  music/playlists_shared.py
CREATE TABLE IF NOT EXISTS guild_playlists (
    guild_id    BIGINT NOT NULL,
    name        TEXT   NOT NULL,             -- display name as typed
    name_norm   TEXT   NOT NULL,             -- casefolded uniqueness key
    creator_id  BIGINT NOT NULL,
    tracks      TEXT[] NOT NULL DEFAULT '{}',-- encoded blobs, in play order
    track_count INTEGER NOT NULL DEFAULT 0,
    total_ms    BIGINT  NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, name_norm)
);
CREATE INDEX IF NOT EXISTS guild_playlists_guild_idx ON guild_playlists (guild_id, created_at DESC);

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
    autoplay              BOOLEAN     NOT NULL DEFAULT TRUE,  -- session autoplay mode, restored on cold restart
    radio_genre           TEXT,                              -- active radio station genre key (NULL outside radio mode), restored on cold restart
    effect                TEXT,                              -- active audio-effect preset key (NULL = no effect), restored on cold restart
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migrate pre-existing installs (no-op on a fresh database):
ALTER TABLE music_state ADD COLUMN IF NOT EXISTS controller_message_id BIGINT;
ALTER TABLE music_state ADD COLUMN IF NOT EXISTS autoplay BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE music_state ADD COLUMN IF NOT EXISTS radio_genre TEXT;
ALTER TABLE music_state ADD COLUMN IF NOT EXISTS effect TEXT;

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
CREATE INDEX IF NOT EXISTS starboard_entries_guild_stars_idx
    ON starboard_entries (guild_id, star_count DESC);

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

-- AniList airing tracker opt-ins: users who chose to be DMed when a new episode
-- of a title on their CURRENT anime list airs (with a one-click Seen button that
-- bumps their AniList progress). One row per Discord user; ``anilist_user_id``
-- is their AniList numeric id, resolved once at opt-in from their token so the
-- poller can read their PUBLIC list unauthenticated (no token at poll time).
-- ``enabled`` is flipped off automatically when their DMs are closed (a
-- Forbidden on delivery) and they can simply re-run the toggle to turn it back
-- on. Lookups ride the PK.  cogs/anilist/airing.py
CREATE TABLE IF NOT EXISTS anilist_airing_optins (
    user_id         BIGINT      PRIMARY KEY,               -- Discord user id
    anilist_user_id INTEGER     NOT NULL,                  -- AniList numeric user id
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Global cursor for the AniList airing poller: a single row holding the newest
-- ``airingAt`` (unix seconds) already fanned out to opted-in users. AniList only
-- guarantees FUTURE airing data, so the poller scans a SHORT trailing window
-- (airingAt_greater = cursor .. airingAt_lesser = now, sort TIME ascending) and
-- advances the cursor to the max airingAt actually processed. Under page
-- truncation the unfetched tail has HIGHER airingAt, so the cursor stops at the
-- last fetched row and that tail rides the next tick (the strict airingAt_greater
-- filter then excludes only what was already handled). The cursor only ever
-- advances; the fixed id + CHECK keep this table to exactly one row.
-- cogs/anilist/airing.py
CREATE TABLE IF NOT EXISTS anilist_airing_state (
    id             SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_airing_at BIGINT      NOT NULL DEFAULT 0,   -- airingAt_greater cursor (unix seconds)
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- MangaDex chapter alerts (tools/mangadex.py + a later poller/cog lot)
-- ============================================================

-- MangaDex chapter-alert opt-ins: mirrors anilist_airing_optins exactly, chapter
-- flavour. Users who chose to be DMed when a new chapter of a title on their
-- MangaDex-mapped manga list drops (with a one-click Read button, a later lot).
-- One row per Discord user; ``anilist_user_id`` is their AniList numeric id,
-- resolved once at opt-in so the poller can read their PUBLIC manga list
-- unauthenticated (no token at poll time). ``enabled`` is flipped off
-- automatically when their DMs are closed (a Forbidden on delivery) and they can
-- re-run the toggle to turn it back on. Lookups ride the PK.
-- cogs/anilist (chapters, later lot)
CREATE TABLE IF NOT EXISTS anilist_chapter_optins (
    user_id         BIGINT      PRIMARY KEY,               -- Discord user id
    anilist_user_id INTEGER     NOT NULL,                  -- AniList numeric user id
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AniList media id -> MangaDex manga UUID mapping cache. MangaDex has NO
-- AniList-id filter on /manga, so a mapping is resolved by a title search whose
-- candidates are scanned for the exact attributes.links.al (see
-- tools.mangadex.pick_mapping). That search is expensive and often fruitless for
-- niche titles, so BOTH outcomes are cached: ``status = 'found'`` carries the
-- resolved ``mangadex_id`` (a UUID), ``status = 'missing'`` stores it NULL and
-- exists solely to STOP the poller re-searching that media every tick. A later
-- lot may retry stale 'missing' rows using ``checked_at`` as the staleness clock.
-- One row per AniList media; lookups ride the PK.  cogs/anilist (chapters lot)
CREATE TABLE IF NOT EXISTS mangadex_mapping (
    anilist_media_id INTEGER     PRIMARY KEY,               -- AniList numeric media id
    mangadex_id      TEXT,                                  -- MangaDex manga UUID; NULL when missing
    status           TEXT        NOT NULL DEFAULT 'missing', -- 'found' | 'missing'
    checked_at       TIMESTAMPTZ NOT NULL DEFAULT now()      -- last search time (staleness clock)
);

-- Per-manga chapter-poll cursor: the newest ``readableAt`` already processed for
-- a MangaDex manga. The MangaDex per-manga feed is ordered by readableAt desc;
-- the poller alerts chapters newer than this cursor and then advances it (see
-- tools.mangadex.plan_chapter_alerts). The cursor is stored as TEXT holding the
-- RAW readableAt string exactly as MangaDex returned it: the pure planner returns
-- that raw value and accepts it straight back, so a verbatim round-trip avoids any
-- lossy timestamp reparse at the seam. NULL means "never anchored" -> the next
-- poll is the anti-backfill first run (anchor the cursor, alert nothing). One row
-- per manga; lookups ride the PK.  cogs/anilist (chapters lot)
CREATE TABLE IF NOT EXISTS mangadex_chapter_state (
    mangadex_id      TEXT        PRIMARY KEY,               -- MangaDex manga UUID
    last_readable_at TEXT,                                  -- cursor: raw readableAt of the newest processed chapter
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bounded "already-alerted" memory that partners the cursor above. The SAME
-- logical chapter is uploaded once per scanlation group, and a LATER group upload
-- of an already-alerted chapter arrives with a NEWER readableAt - so the cursor
-- alone would re-alert it. This table remembers each alerted chapter identity so
-- plan_chapter_alerts never re-alerts one, whatever its readableAt says. Design:
-- one row PER SEEN CHAPTER (not a compact per-manga JSON blob) because that lets
-- the poller (a) upsert a single identity without a read-modify-write race on a
-- shared blob and (b) prune cheaply by age or per-manga count via the index below
-- - ``first_seen_at`` is that pruning key. ``chapter_key`` is the serialized
-- identity from tools.mangadex.chapter_key: the canonical chapter NUMBER (the
-- volume is excluded - groups disagree on it), id-fallback for numberless rows.
-- Stored as TEXT. Lookups/prunes ride the PK + index.
-- cogs/anilist (chapters lot)
CREATE TABLE IF NOT EXISTS mangadex_seen_chapters (
    mangadex_id   TEXT        NOT NULL,                     -- MangaDex manga UUID
    chapter_key   TEXT        NOT NULL,                     -- serialized chapter-number identity
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),       -- pruning key (prune by age / per-manga count)
    PRIMARY KEY (mangadex_id, chapter_key)
);
CREATE INDEX IF NOT EXISTS mangadex_seen_chapters_prune_idx
    ON mangadex_seen_chapters (mangadex_id, first_seen_at);

-- RESERVED / NO LONGER READ. These two per-feed booleans backed the original
-- in-channel-alerts model, where a feed derived its channel posts from its
-- FOLLOWED users' lists. That model was replaced by explicit per-feed title
-- subscriptions (anilist_channel_subs below): the airing/chapter pollers no
-- longer read these columns and the feed panel no longer writes them. The
-- columns are kept (not dropped) to avoid a destructive migration; they simply
-- sit unused. Do not reintroduce reads without reviving that circuit.  cogs/anilist
ALTER TABLE anilist_feeds ADD COLUMN IF NOT EXISTS chapters_in_channel BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE anilist_feeds ADD COLUMN IF NOT EXISTS airing_in_channel BOOLEAN NOT NULL DEFAULT FALSE;

-- Explicit per-feed title subscriptions: the tracked-releases circuit. A feed
-- channel SUBSCRIBES to specific AniList titles and the airing poller (media_type
-- 'ANIME') / chapter poller (media_type 'MANGA') posts each new episode/chapter of
-- a subscribed title once in that channel. This is fully INDEPENDENT of the DM
-- opt-ins and of who the feed follows: the two circuits share no rows. Capped at
-- 50 subscriptions per feed (MAX_SUBS_PER_FEED, enforced in code). ``title`` caches
-- the chosen display title so the manage panel renders the list without an AniList
-- call, and (for manga) seeds the MangaDex mapping search so a subscribed title the
-- poller has never otherwise seen can still be resolved. ``media_type`` is the
-- AniList MediaType ('ANIME' | 'MANGA'). Lookups by feed ride the
-- (guild_id, channel_id, ...) PK prefix.  cogs/anilist/feed.py
CREATE TABLE IF NOT EXISTS anilist_channel_subs (
    guild_id   BIGINT      NOT NULL,
    channel_id BIGINT      NOT NULL,                       -- a text channel OR thread id
    media_id   INTEGER     NOT NULL,                       -- AniList numeric media id
    media_type TEXT        NOT NULL,                       -- 'ANIME' | 'MANGA'
    title      TEXT,                                       -- cached display title for the panel/search
    added_by   BIGINT,                                     -- Discord user who subscribed it
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id, media_id)
);

-- ============================================================
-- Data retention: delayed purge of departed guilds
-- ============================================================
-- When the bot leaves a guild, a job is scheduled here and its stored data is
-- purged only after a cancellable grace period (the guild rejoining cancels it).
-- ``claimed_at`` lets a worker lease a due job (cleared on failure so it retries;
-- ``attempts``/``last_error`` record why). The inline CHECKs are safe: this is a
-- fresh table, so there are no legacy rows to grandfather.  tools/retention.py
CREATE TABLE IF NOT EXISTS guild_retention_jobs (
    guild_id    BIGINT      PRIMARY KEY,
    left_at     TIMESTAMPTZ NOT NULL,
    purge_after TIMESTAMPTZ NOT NULL,
    attempts    INTEGER     NOT NULL DEFAULT 0,
    last_error  TEXT,
    claimed_at  TIMESTAMPTZ,
    CONSTRAINT guild_retention_attempts_nonnegative CHECK (attempts >= 0),
    CONSTRAINT guild_retention_dates_ordered CHECK (purge_after >= left_at)
);
-- Due-job scan: WHERE purge_after <= now() AND claimed_at IS NULL ORDER BY
-- purge_after, guild_id -> partial index range scan, no sort.
CREATE INDEX IF NOT EXISTS guild_retention_jobs_due_idx
    ON guild_retention_jobs (purge_after, guild_id)
    WHERE claimed_at IS NULL;

-- ============================================================
-- One-shot data fixups bookkeeping (tools/fixups.py)
-- ============================================================
-- schema.sql (DDL) is applied every boot; a fixup is a one-shot DATA repair that
-- DDL alone cannot express. Each fixup runs at most once (its name is recorded
-- here on success) and MUST itself be idempotent. There are NO checksums and NO
-- ordering pins: a name in this table that the running code no longer knows about
-- is simply ignored, so rolling back to an older commit never fails to boot.
CREATE TABLE IF NOT EXISTS applied_fixups (
    name       TEXT        PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Guarded integrity constraints (added NOT VALID)
-- ============================================================
-- Every constraint below is added NOT VALID and is NEVER validated here: new
-- INSERT/UPDATE writes are enforced, but pre-existing ("legacy") rows are
-- grandfathered and are NOT scanned when the constraint is added. This is the
-- deliberate anti-brick posture - a single legacy row that predates a tightened
-- rule can never turn a boot into a crash-loop (which a validating scan would).
-- Each ADD is guarded by a pg_constraint lookup so re-applying schema.sql on
-- every boot is a no-op. Two constraints are intentionally looser than the
-- strictest possible rule to keep hot write paths brick-free (see inline notes).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'warns_count_nonnegative'
    ) THEN
        ALTER TABLE warns ADD CONSTRAINT warns_count_nonnegative
            CHECK (warns_count >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'timers_event_nonempty'
    ) THEN
        ALTER TABLE timers ADD CONSTRAINT timers_event_nonempty
            CHECK (btrim(event) <> '') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'timers_attempts_nonnegative'
    ) THEN
        ALTER TABLE timers ADD CONSTRAINT timers_attempts_nonnegative
            CHECK (attempts >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'level_config_values_valid'
    ) THEN
        ALTER TABLE level_config ADD CONSTRAINT level_config_values_valid CHECK (
            cooldown_seconds >= 1
            AND xp_min >= 0
            AND xp_max >= xp_min
            AND announce_mode IN ('off', 'channel', 'dm', 'fixed')
            AND rewards_mode IN ('stack', 'replace')
            AND voice_xp_per_minute BETWEEN 1 AND 60
            AND (
                event_factor IS NULL
                OR event_factor BETWEEN 0 AND 5
            )
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'xp_multipliers_values_valid'
    ) THEN
        ALTER TABLE xp_multipliers
            ADD CONSTRAINT xp_multipliers_values_valid CHECK (
                kind IN ('global', 'channel', 'role')
                AND factor BETWEEN 0 AND 5
                AND (
                    (kind = 'global' AND target_id = 0)
                    OR (kind <> 'global' AND target_id > 0)
                )
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'level_rewards_level_positive'
    ) THEN
        ALTER TABLE level_rewards
            ADD CONSTRAINT level_rewards_level_positive
            CHECK (level >= 1) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'starboard_threshold_positive'
    ) THEN
        ALTER TABLE starboard
            ADD CONSTRAINT starboard_threshold_positive
            CHECK (threshold >= 1) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'starboard_count_nonnegative'
    ) THEN
        ALTER TABLE starboard_entries
            ADD CONSTRAINT starboard_count_nonnegative
            CHECK (star_count >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'avatar_history_kind_valid'
    ) THEN
        ALTER TABLE avatar_history
            ADD CONSTRAINT avatar_history_kind_valid
            CHECK (kind IN ('global', 'guild', 'banner')) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'avatar_history_format_valid'
    ) THEN
        ALTER TABLE avatar_history
            ADD CONSTRAINT avatar_history_format_valid
            CHECK (image_format IN ('png', 'webp', 'original')) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cases_values_valid'
    ) THEN
        ALTER TABLE cases ADD CONSTRAINT cases_values_valid
            CHECK (case_number >= 1 AND btrim(action) <> '') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'button_roles_style_valid'
    ) THEN
        ALTER TABLE button_roles ADD CONSTRAINT button_roles_style_valid
            CHECK (style BETWEEN 1 AND 4) NOT VALID;
    END IF;
    -- guild_playlists: the strict form also asserted
    -- ``track_count = cardinality(tracks)``. That equality is DROPPED on purpose:
    -- it is a pure denormalisation-consistency assertion (a wrong count only
    -- misprints a list count) yet it would turn EVERY future partial write that
    -- touches only one of the two columns into a hard failure - a brick risk the
    -- review flagged as real (track_count drift). The cheap, safe range checks
    -- are kept.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'guild_playlists_values_valid'
    ) THEN
        ALTER TABLE guild_playlists
            ADD CONSTRAINT guild_playlists_values_valid CHECK (
                track_count >= 0
                AND total_ms >= 0
            ) NOT VALID;
    END IF;
    -- music_state: volume is bounded 0..1000, NOT the app's current 0..200 UI cap.
    -- The upper bound was historically 1000, so every legitimately-created legacy
    -- row is in [0, 1000]; using that union grandfathers all of them AND keeps a
    -- corruption backstop, while never bricking the very hot per-save UPDATE (which
    -- re-checks the row's volume on every position write). The app enforces the
    -- tighter 0..200 today; this constraint is only the DB-side floor/ceiling.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'music_state_values_valid'
    ) THEN
        ALTER TABLE music_state ADD CONSTRAINT music_state_values_valid CHECK (
            volume BETWEEN 0 AND 1000
            AND loop_mode BETWEEN 0 AND 2
            AND position_ms >= 0
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'anilist_feed_fail_count_valid'
    ) THEN
        ALTER TABLE anilist_feeds
            ADD CONSTRAINT anilist_feed_fail_count_valid
            CHECK (fail_count >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mangadex_mapping_status_valid'
    ) THEN
        ALTER TABLE mangadex_mapping
            ADD CONSTRAINT mangadex_mapping_status_valid CHECK (
                status IN ('found', 'missing')
                AND ((status = 'found') = (mangadex_id IS NOT NULL))
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'anilist_channel_media_type_valid'
    ) THEN
        ALTER TABLE anilist_channel_subs
            ADD CONSTRAINT anilist_channel_media_type_valid
            CHECK (media_type IN ('ANIME', 'MANGA')) NOT VALID;
    END IF;
END
$$;

-- Foreign keys, likewise added NOT VALID (orphan legacy rows are grandfathered;
-- the FK columns never change on the hot UPDATE paths, so grandfathered rows are
-- never re-checked). ON DELETE CASCADE gives clean config teardown going forward.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'starboard_entries_config_fk'
    ) THEN
        ALTER TABLE starboard_entries
            ADD CONSTRAINT starboard_entries_config_fk
            FOREIGN KEY (guild_id) REFERENCES starboard(guild_id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'anilist_follows_feed_fk'
    ) THEN
        ALTER TABLE anilist_follows
            ADD CONSTRAINT anilist_follows_feed_fk
            FOREIGN KEY (guild_id, channel_id)
            REFERENCES anilist_feeds(guild_id, channel_id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'anilist_subs_feed_fk'
    ) THEN
        ALTER TABLE anilist_channel_subs
            ADD CONSTRAINT anilist_subs_feed_fk
            FOREIGN KEY (guild_id, channel_id)
            REFERENCES anilist_feeds(guild_id, channel_id)
            ON DELETE CASCADE NOT VALID;
    END IF;
END
$$;
