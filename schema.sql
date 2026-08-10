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
-- Same shape, for the opt-in "vote again" reminder (cogs/community/votes.py):
-- cancel-then-reschedule on every real vote filters on (event, user), so this
-- keeps that scoped DELETE indexed instead of a scan over every timer row.
CREATE INDEX IF NOT EXISTS timers_vote_reminder_user_idx
    ON timers (event, (extra->>'user_id'), expires);

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
-- the legacy leveling_enabled JSONB value (cogs/community/leveling/engine.py resolve_config), so a
-- guild that had leveling on keeps it on until its next toggle writes a row - and a
-- row always wins, so switching leveling OFF via this table is never undone by a
-- stale JSONB true. `enabled`, `cooldown_seconds` and the `xp_min`/`xp_max` band
-- are wired into the grant path now; `announce_mode` (off|channel|dm|fixed)
-- picks where a level-up is announced, `announce_channel_id` is the target
-- channel for `announce_mode = 'fixed'`, and `announce_template` is an
-- optional custom level-up message (both set via set_announce_mode /
-- set_announce_template and read by the grant path). One row per guild;
-- lookups ride the PK.  leveling.py, cogs/config/settings.py
CREATE TABLE IF NOT EXISTS level_config (
    guild_id            BIGINT  PRIMARY KEY,
    enabled             BOOLEAN NOT NULL DEFAULT FALSE,
    cooldown_seconds    INTEGER NOT NULL DEFAULT 60,
    xp_min              INTEGER NOT NULL DEFAULT 15,
    xp_max              INTEGER NOT NULL DEFAULT 25,
    announce_mode       TEXT    NOT NULL DEFAULT 'channel',  -- off | channel | dm | fixed
    announce_channel_id BIGINT,                              -- target channel for announce_mode = 'fixed'
    announce_template   TEXT,                                -- custom level-up message template (NULL = default)
    rewards_mode        TEXT    NOT NULL DEFAULT 'stack',     -- stack | replace (level_rewards.py)
    voice_xp_enabled    BOOLEAN NOT NULL DEFAULT FALSE,       -- opt-in: earn XP for time in voice (voice_xp.py)
    voice_xp_per_minute INTEGER NOT NULL DEFAULT 5,           -- XP per eligible minute in voice (bounds 1..60)
    event_factor        REAL,                                 -- active timed double-XP event's multiplier, NULL = no event (L4)
    event_ends_at       TIMESTAMPTZ,                           -- when the event above expires; an expired row is ignored at read time and lazily nulled (no timer) (L4)
    season_champion_role_id BIGINT,                            -- optional "Season champion" role, REPLACE-moved to the closed month's #1; NULL = off (S1)
    season_announce     BOOLEAN NOT NULL DEFAULT FALSE         -- opt in to the season rollover announce; announce_mode still decides WHERE (S1)
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
-- Seasons (S1), read by cogs/community/leveling/seasons.py once per guild per month:
-- season_champion_role_id is the optional "Season champion" role handed to the
-- closed month's #1 in REPLACE mode (NULL = feature off, the default), and
-- season_announce opts the guild in to the rollover announce (default off, and
-- the guild's existing announce_mode still decides WHERE - see
-- cogs.community.leveling.engine.resolve_season_announce_channel).
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS season_champion_role_id BIGINT;
ALTER TABLE level_config ADD COLUMN IF NOT EXISTS season_announce BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-guild XP multipliers (L4, the Lurkr rule): boost or reduce XP globally,
-- per channel/category, or per role. ``kind = 'global'`` always uses
-- ``target_id = 0`` (cogs.community.leveling.engine.GLOBAL_MULTIPLIER_TARGET_ID), so the PK
-- caps a guild at exactly one global row; ``kind = 'channel'`` rows match
-- EITHER a text channel id OR a category id (same one-row-per-category design
-- as level_no_xp - see that table's comment); ``kind = 'role'`` rows match a
-- member's held roles. ``factor`` is bounded 0.0..5.0 in code
-- (cogs.community.leveling.engine.validate_multiplier_factor) - 0.0 is a valid, explicitly
-- supported "mute XP via multiplier" outcome. Capped at 25 rows/guild across
-- every kind (cogs.community.leveling.engine.MAX_MULTIPLIERS_PER_GUILD), enforced RACE-SAFELY
-- by the same WHERE-COUNT INSERT guard as level_rewards/level_no_xp. Stacking
-- (effective = global * channel * role * event, channel-beats-category,
-- highest-role-wins) is computed by cogs.community.leveling.engine.compute_multiplier against
-- a per-guild MultiplierSnapshot cached in-memory
-- (cogs/community/leveling/leveling.py's ``self._multipliers``, a BoundedLRU beside the
-- no-xp snapshot cache) - the hot paths (on_message, the voice sweep) never
-- query this table directly.  cogs/community/leveling/leveling.py,
-- cogs/community/leveling/voice_xp.py, cogs/community/leveling/level_config_ui.py
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
-- ('replace'). Capped at 25 rules per guild in code (cogs/community/leveling/reward_rules.py).
-- Reconciliation is on-demand only: a rule added for a level a member already
-- passed is granted the next time THEY level up, never by a retroactive sweep.
-- A grant that hits a since-deleted role prunes that role's row(s) lazily and
-- logs INFO (cogs/community/leveling/level_rewards.py).
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
-- cogs/community/leveling/engine.py NoXpSnapshot); ``kind = 'role'`` rows match any role the
-- message author holds. Capped at 50 entries/guild
-- (cogs.community.leveling.engine.MAX_NO_XP_PER_GUILD), enforced RACE-SAFELY by the same
-- WHERE-COUNT INSERT guard as level_rewards. HOT PATH: on_message never
-- queries this table directly - the Leveling cog loads a guild's rows once
-- (on its first grant-eligible message, or immediately after any write here)
-- into an in-memory NoXpSnapshot (two frozensets) capped to ~2048 guilds via
-- tools.lru_cache.BoundedLRU, so the steady-state per-message cost is pure set
-- membership, zero DB.  cogs/community/leveling/leveling.py, cogs/community/leveling/level_config_ui.py
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
-- multi-CTE SQL command - see cogs/community/leveling/leveling.py's on_message and
-- cogs/community/leveling/voice_xp.py's batched sweep upsert), never a separate query.
-- ``period_key`` is pure date maths from UTC "now" (cogs.community.leveling.engine.
-- current_period_keys): ``W<iso_year>-<iso_week>`` (ISO year-week, e.g.
-- 'W2026-28') for the weekly view, ``M<year>-<month>`` (e.g. 'M2026-07') for
-- the monthly view, both zero-padded so period keys of the same kind sort
-- lexically in chronological order - a grant writes BOTH keys every time.
-- Retention: rows older than ~3 periods (cogs.community.leveling.engine.PRUNE_PERIODS_BACK)
-- are dropped by a cheap DELETE piggybacked on the first grant/credit of a
-- NEW period per guild - decided by an in-memory "last seen period" marker
-- on the Leveling cog (cogs.community.leveling.engine.period_marker_changed), never a
-- background timer.  cogs/community/leveling/leveling.py, cogs/community/leveling/voice_xp.py
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

-- Leveling seasons (S1): one row per podium PLACE of a CLOSED month. A season
-- is simply a calendar month of the xp_period rollup above - nothing is reset
-- or destroyed when one ends (lifetime `levels` totals and member levels are
-- untouched); the month's top 3 are merely FROZEN here so they survive the
-- lazy xp_period prune. `period_key` is the monthly key of the CLOSED month
-- ('M<year>-<month>') - the month the guild's period marker says it last
-- earned XP in (cogs.community.leveling.engine.season_rollover_period_key), or, when that
-- marker is cold (every restart), the latest monthly key this guild has an
-- xp_period row for before the current month (the shared
-- cogs.community.leveling.engine.LATEST_CLOSED_MONTH_SQL). NEVER simply the month before now,
-- which would skip a guild that stayed silent a whole month. `rank` is 1..3 (cogs.community.leveling.engine.SEASON_PODIUM_SIZE), ties broken by
-- user_id so a re-run can never reshuffle a stored podium, and `xp` is that
-- member's XP for that month alone. Written EXACTLY ONCE per (guild, month) by
-- an INSERT ... ON CONFLICT DO NOTHING whose RETURNING is also what elects the
-- single caller allowed to run the one-shot side effects (champion role +
-- announce), so two concurrent triggers can never double-post. Trigger paths,
-- both converging on that same INSERT: the first XP grant of a new month for a
-- guild (detected by the leveling cog's existing in-memory period marker - no
-- timer, no sweep) and an on-demand ensure_season_snapshot() call from a read
-- surface. No extra index: the PK serves the exactly-once probe
-- (guild_id, period_key), a guild's season history (guild_id prefix) AND the
-- "who was the outgoing champion?" lookup the REPLACE role move needs
-- (guild_id, period_key < closed, rank = 1 -> backward index scan, verified on
-- the real Postgres) - which is read from HERE rather than from Role.members
-- precisely because the member cache is not populated (chunk_guilds_at_startup
-- is False).
-- cogs/community/leveling/seasons.py, cogs/community/leveling/leveling.py
CREATE TABLE IF NOT EXISTS season_podiums (
    guild_id    BIGINT      NOT NULL,
    period_key  TEXT        NOT NULL,   -- closed month, 'M<year>-<month>'
    rank        SMALLINT    NOT NULL,   -- 1..3
    user_id     BIGINT      NOT NULL,
    xp          BIGINT      NOT NULL,   -- that member's XP for that month only
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, period_key, rank)
);

-- Per-guild look of the /rank card: an optional background image and an optional
-- accent colour. One row per guild, created only when a guild customises the
-- card - no row means the stock card, which renders byte-for-byte as it did
-- before this table existed.
-- ``background`` is ALWAYS a bot-normalised WebP: cogs/community/leveling/rank_card.py decodes the
-- uploaded PNG/JPEG/WebP, cover-crops it to the card's EXACT pixel size and
-- re-encodes it under a hard size cap, so the stored blob is bounded (512 KiB
-- worst case) and the render never resizes a hostile image. That bound is
-- ENFORCED here, not merely documented: the CHECK below refuses anything larger,
-- because the dashboard is an INDEPENDENT writer (a separate Node process with
-- its own copy of the caps) and a blob that slipped past it would be re-read on
-- every /rank of that guild. It matches cogs/community/leveling/rank_card.MAX_STORED_BYTES.
-- ``background_format``
-- records that encoding ('webp') for future-proofing, exactly as avatar_history
-- does. ``accent`` is a packed 0xRRGGBB int (the same shape discord.Colour.value
-- uses), NULL to keep the member-colour default.
-- Owner: cogs/community/leveling/leveling.py (render seam) + cogs/community/leveling/rank_card.py (validation
-- and the write API). ALSO WRITTEN BY THE DASHBOARD (the separate Node process),
-- which fires pg_notify('yasuho_dashboard', {"kind": "rank_card", ...}) after its
-- write so cogs/system/dashboard_sync.py drops the bot's cached config.
CREATE TABLE IF NOT EXISTS rank_cards (
    guild_id          BIGINT      PRIMARY KEY,
    background        BYTEA,                 -- normalised WebP, NULL = no background
    background_format TEXT,                  -- 'webp' (NULL when no background)
    accent            INTEGER,               -- packed 0xRRGGBB, NULL = default
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rank_cards_accent_range CHECK (accent IS NULL OR (accent >= 0 AND accent <= 16777215)),
    CONSTRAINT rank_cards_background_size CHECK (background IS NULL OR octet_length(background) <= 524288)
);
-- Migrate pre-existing installs (no-op on a fresh database):
ALTER TABLE rank_cards ADD COLUMN IF NOT EXISTS background BYTEA;
ALTER TABLE rank_cards ADD COLUMN IF NOT EXISTS background_format TEXT;
ALTER TABLE rank_cards ADD COLUMN IF NOT EXISTS accent INTEGER;
ALTER TABLE rank_cards ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- PER-MEMBER look of the /rank card (lot U1): the same two knobs as rank_cards
-- above, chosen by the member for THEMSELVES, in every server at once. One row
-- per user, created only when that user customises their card; no row means
-- "fall back to the guild's card, then to the stock one".
-- Owner: cogs/community/leveling/rank_card_user.py (the /rankcard surface and
-- the render resolver) + cogs/community/leveling/rank_card.py (validation and
-- the write API, SHARED verbatim with rank_cards - the same sniff, the same
-- pixel ceiling, the same WebP ladder, so a member cannot store anything an
-- admin could not).
--
-- PRECEDENCE, resolved per render: user background > guild background > stock,
-- and user accent > guild accent > the member's role colour. A guild can turn
-- the whole per-user layer off for its own /rank cards with the
-- ``rank_card_allow_user_styles`` key in guild_settings (absent = allowed) -
-- that is the moderation tool for an unwanted image, and it needs no write
-- here, so it can never destroy what a member stored for their other servers.
--
-- BOTH columns are nullable and INDEPENDENT: a member may set only an accent,
-- only a background, or both. The CHECKs are the guild table's, verbatim - the
-- 512 KiB bound especially, because this blob is re-read on every /rank that
-- renders this member and, unlike the guild table, ANY member can write it.
-- No background_format column on purpose (the guild table's predates a single
-- stored encoding): every blob here is written by validate_and_downscale, which
-- only ever emits rank_card.STORED_FORMAT ('webp'), so the column would be a
-- constant.
-- USER-SCOPED, so no guild purge can ever reach it: it is exported by
-- tools/privacy.collect_user_export and erased by BOTH delete lists there.
CREATE TABLE IF NOT EXISTS user_rank_cards (
    user_id    BIGINT      PRIMARY KEY,
    background BYTEA,                 -- normalised WebP, NULL = no background
    accent     INTEGER,               -- packed 0xRRGGBB, NULL = default
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_rank_cards_accent_range CHECK (accent IS NULL OR (accent >= 0 AND accent <= 16777215)),
    CONSTRAINT user_rank_cards_background_size CHECK (background IS NULL OR octet_length(background) <= 524288)
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

-- LEGACY per-user gamer IDs / friend codes. Superseded by user_profiles.gaming_ids
-- (see below): the boot fixup `user_profiles_import_legacy_gaming_ids` copies
-- every row across once, after which nothing writes here. Kept - not dropped -
-- as the migration's safety net; it is still exported by /mydata and still
-- deleted by the profile-forget path while it holds anyone's data.
CREATE TABLE IF NOT EXISTS profiles (
    user_id    BIGINT PRIMARY KEY,
    switch_fc  TEXT,
    threeds_fc TEXT,
    battletag  TEXT,
    riotid     TEXT,
    steamid    TEXT
);

-- The social profile, one row per USER and no guild_id: a profile follows the
-- person, not the server. Owner: cogs/community/profile/ (registry.py is the
-- source of truth for field names and caps; the CHECKs below guard the OUTER
-- shape only - type, and how many custom pairs - for the second writer, the
-- dashboard, which lands later. The nested caps (label/value lengths, the
-- gamer-ID key whitelist) live in Python, so storage.py re-validates both JSONB
-- columns on READ and drops what fails instead of trusting the row).
-- custom_fields is a JSONB array of {"label": ..., "value": ...} objects because
-- it is deliberately heterogeneous user text; gaming_ids is a JSONB object keyed
-- by the registry's whitelist (switch / 3ds / battletag / riot / steam_id), so a new
-- gamer ID is a line of Python, not a migration.
-- User-scoped: no guild purge applies (see tools/retention.py); it joins the
-- USER paths instead - export and forget in tools/privacy.py.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id       BIGINT      PRIMARY KEY,
    bio           TEXT,
    pronouns      TEXT,
    accent        INTEGER,
    custom_fields JSONB       NOT NULL DEFAULT '[]'::jsonb,
    gaming_ids    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_accent_range
        CHECK (accent IS NULL OR (accent >= 0 AND accent <= 16777215)),
    CONSTRAINT user_profiles_bio_length
        CHECK (bio IS NULL OR char_length(bio) <= 300),
    CONSTRAINT user_profiles_pronouns_length
        CHECK (pronouns IS NULL OR char_length(pronouns) <= 40),
    CONSTRAINT user_profiles_custom_fields_shape
        CHECK (jsonb_typeof(custom_fields) = 'array'
               AND jsonb_array_length(custom_fields) <= 5),
    CONSTRAINT user_profiles_gaming_ids_shape
        CHECK (jsonb_typeof(gaming_ids) = 'object')
);

-- Per-FIELD visibility for a profile. An ABSENT ROW MEANS PRIVATE: the default is
-- never materialised, so "never decided" and "chose private" are one state and
-- both fail closed. 'field' is a free TEXT validated against the Python registry
-- (cogs/community/profile/registry.py) rather than a column or an enum, so P3/P4
-- connectors become addressable without a schema change; a name the running code
-- does not know is ignored on read.
-- User-scoped, like user_profiles: exported and forgotten via tools/privacy.py.
CREATE TABLE IF NOT EXISTS profile_visibility (
    user_id BIGINT NOT NULL,
    field   TEXT   NOT NULL,
    level   TEXT   NOT NULL CHECK (level IN ('public', 'server', 'private')),
    PRIMARY KEY (user_id, field)
);

-- One row per (user, external account) linked to a profile. Owner:
-- cogs/community/profile/connectors/ (base.py is the source of truth for the
-- caps and for which sections are linkable; the CHECKs below are the belt to
-- that module's suspenders, for the second writer - the dashboard - that lands
-- later).
--
-- NO CREDENTIAL LIVES HERE. AniList keeps its own encrypted `anilist_tokens`
-- row (Fernet); every other v1 connector is keyed by a PUBLIC handle - a
-- username, a SteamID64 - so there is nothing secret to store. A generic
-- connector-token table is deliberately NOT created in advance: the day a
-- second OAuth connector exists it will need its own scopes, refresh cadence
-- and revocation path, and guessing that shape now would be inventing a
-- security-sensitive schema for nobody (YAGNI). What is not negotiable is that
-- such a secret goes in its own encrypted table and never in `payload`, which
-- /mydata exports verbatim.
--
-- `payload` is a bounded CACHE of displayable data (counts, a few titles, an
-- avatar URL) refreshed by the P4 connectors, never the source of truth; it is
-- capped at 8 KiB in Python. `connector` is a fixed seven-value CHECK rather
-- than a free TEXT because, unlike profile_visibility.field, a row here costs
-- storage and a refresh budget: an unknown name must fail loudly. The two
-- presence sections are allowed by the CHECK so P5 can keep a marker row, but
-- they are NOT linkable by a typed handle (see base.LINKABLE).
--
-- User-scoped: no guild purge applies (see tools/retention.py); it joins the
-- USER paths instead - export and forget in tools/privacy.py.
CREATE TABLE IF NOT EXISTS profile_connections (
    user_id      BIGINT      NOT NULL,
    connector    TEXT        NOT NULL,
    external_id  TEXT        NOT NULL,
    display_name TEXT,
    linked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_refresh TIMESTAMPTZ,
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (user_id, connector),
    CONSTRAINT profile_connections_connector_known
        CHECK (connector IN ('anilist', 'steam', 'lastfm', 'osu', 'backloggd',
                             'presence_gaming', 'spotify_presence')),
    CONSTRAINT profile_connections_external_id_length
        CHECK (char_length(external_id) BETWEEN 1 AND 190),
    CONSTRAINT profile_connections_display_name_length
        CHECK (display_name IS NULL OR char_length(display_name) <= 190),
    CONSTRAINT profile_connections_payload_shape
        CHECK (jsonb_typeof(payload) = 'object'),
    -- The 8 KiB cap that the storage estimate leans on, in the one place a
    -- second writer (the dashboard) cannot route around. octet_length on the
    -- text form, NOT pg_column_size: the latter measures the COMPRESSED datum,
    -- so a blob would pass here and blow the estimate anyway. Probed against
    -- the local instance: a payload whose JSON text is exactly 8192 bytes is
    -- accepted, 8193 raises CheckViolationError.
    -- This measures the CANONICAL text jsonb re-serialises, not the bytes
    -- Python sent, so it is not always the number base.encode_payload checked
    -- (base.PAYLOAD_MAX_BYTES). Same probe: for strings, integers, booleans,
    -- nulls and nesting the two are byte-for-byte equal (json.dumps' default
    -- ', ' / ': ' separators are exactly what jsonb emits), and reordered keys
    -- keep the same length - but NUMBERS are canonicalised, so a float written
    -- in exponent form expands, e.g. 1e+50 becomes its 51 digits and a payload
    -- Python measured at 8163 bytes is rejected here. A P4 connector that
    -- caches raw numeric API data must therefore keep margin under 8 KiB (or
    -- store such values as strings), not aim at it.
    CONSTRAINT profile_connections_payload_size
        CHECK (octet_length(payload::text) <= 8192)
);
-- The P4 refresh loop asks "which accounts of THIS connector are the stalest?"
-- (one third-party API at a time, each with its own rate budget), so the index
-- leads on connector and orders by staleness with never-refreshed rows first.
-- Without it that scan is a seq scan over every user's every connection.
CREATE INDEX IF NOT EXISTS profile_connections_refresh_idx
    ON profile_connections (connector, last_refresh NULLS FIRST);

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

-- The rate limit on "give me all my data", as ONE authoritative clock rather
-- than a per-process one.  tools/privacy.claim_export_slot
--
-- A personal-data export is the most expensive thing a user can ask for (it
-- reads a dozen tables and packs every stored avatar), and it can now be asked
-- for from TWO places: `?mydata export` in Discord and the dashboard's
-- `mydata_export` queue action, which run in the same process but never see each
-- other's in-memory cooldown bucket - and a restart wiped that bucket anyway.
-- One row per user, holding the moment the last export was GRANTED; both callers
-- claim their slot with the same single atomic statement, so the hour is shared
-- and survives a restart.
--
-- Deliberately NOT a key in user_settings above: that blob is written by the
-- /preferences panel AND by the dashboard, so anything stored there is something
-- the rate-limited party can rewrite. This table has no writer but the claim
-- itself. For the same reason it is absent from privacy.PROFILE_DELETE_QUERIES -
-- being able to reset your own limiter by deleting your profile is the exact
-- hole the table exists to close - but it IS in the export (it is your data, and
-- one timestamp is all it holds).
--
-- Lifecycle: the daily maintenance pass deletes rows whose window has already
-- elapsed (tools/retention.prune_expired_export_slots). Those are exactly the
-- rows that grant on sight, so the prune cannot weaken the limiter, and without
-- it the table would keep "this person exported once, on this date" for ever.
CREATE TABLE IF NOT EXISTS mydata_export_cooldown (
    user_id        BIGINT      PRIMARY KEY,
    last_export_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The top.gg vote ledger: ONE row per user who has ever voted for the bot.
-- cogs/community/votes.py
--
-- Exactly ONE writer, ever: the on_dbl_vote listener in that cog. top.gg POSTs a
-- vote to the hardened webhook in cogs/system/webstats.py, which dispatches the
-- event; the listener answers it with ONE atomic upsert that does the whole
-- bookkeeping (streak, lifetime count and the boost deadline) in a single
-- statement, so there is no read-modify-write for two deliveries to race on.
-- Every other reader is READ-ONLY - the leveling cog's boot read, the export,
-- and the dashboard.
--
-- `streak` counts CONSECUTIVE votes. top.gg itself enforces the 12h floor
-- between two votes, so the rule here only has to say when a streak BREAKS: a
-- vote landing more than 24h after the previous one starts over at 1, anything
-- inside that window continues.
-- The same 12h floor is what makes the row self-deduplicating: the writer's
-- statement leaves EVERY column alone when the previous vote is under an hour
-- old, so a redelivered vote (the v0 payload carries no vote id to key on)
-- cannot add a step to a streak nobody voted for. `last_vote_at` is therefore
-- the time of the vote itself, never of the delivery that reported it.
-- `boost_expires_at` is when the XP boost this vote armed runs out - 12h
-- normally, 24h when top.gg flags the vote as a weekend one (it counts weekend
-- votes double, so the boost lasts double). It is stored rather than derived so
-- the deadline a voter was promised survives a restart AND a later change to the
-- durations: what was armed stays armed for exactly as long as it was armed for.
--
-- User-scoped, no guild_id: the guild purge (tools/retention.py) can never see
-- these rows, so /mydata is what covers them - the row IS in the export and IS
-- in privacy.USER_DELETE_QUERIES. That is the opposite call from
-- mydata_export_cooldown above, and deliberately so: erasing this row can only
-- ever COST its owner (streak back to 1, boost gone), so putting it on the
-- forget path opens no hole, while leaving it would keep "this person votes for
-- us, and last did on this date" after they asked to be forgotten.
-- USER_DELETE_QUERIES, not PROFILE_DELETE_QUERIES: `/profile clear` has no
-- confirmation step, and a streak is the one thing here its owner cannot type
-- back in. Only the confirmed `?mydata deleteprofile` reaches this row.
-- Nothing prunes it, on purpose: the row IS the streak and the lifetime count,
-- so an age-out would silently take a reward away. PRIVACY.md's Retention
-- section states that ("kept until you delete it"). It is one row per LIFETIME
-- voter, which is what makes that affordable.
--
-- No index beyond the primary key: the only non-PK read is the leveling cog's
-- ONE boot-time scan for still-running boosts, over a table with one row per
-- LIFETIME voter (thousands, not millions). An index on boost_expires_at would
-- tax every vote write to serve a query that runs once per process.
CREATE TABLE IF NOT EXISTS topgg_votes (
    user_id          BIGINT      PRIMARY KEY,
    last_vote_at     TIMESTAMPTZ NOT NULL,
    streak           INTEGER     NOT NULL DEFAULT 1,
    total_votes      INTEGER     NOT NULL DEFAULT 1,
    boost_expires_at TIMESTAMPTZ NOT NULL,
    -- TRUE while last_vote_at was stamped by the lazy /vote catch-up poll
    -- rather than by a webhook delivery (cogs/community/votes.py, RECORD_VOTE).
    -- A catch-up only knows "top.gg says they voted some time in the last 12h",
    -- so it stamps now() for a vote that may be hours old; the flag tells the
    -- next webhook delivery that the timestamp under it is that soft evidence,
    -- so the one-hour replay floor must not swallow a genuine new vote. The
    -- first webhook after a catch-up counts and clears the flag.
    caught_up        BOOLEAN     NOT NULL DEFAULT FALSE
);
-- Migrate pre-existing installs (no-op on a fresh database):
ALTER TABLE topgg_votes ADD COLUMN IF NOT EXISTS caught_up BOOLEAN NOT NULL DEFAULT FALSE;

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
    encoded     TEXT,              -- Lavalink `encoded` blob (bulk-decode seam)
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, identifier)
);
CREATE INDEX IF NOT EXISTS music_favorites_user_idx ON music_favorites (user_id, added_at DESC);
-- Migrate pre-existing installs (no-op on a fresh database). `encoded` lets
-- `/playlist play` rebuild a whole favourites list in ONE bulk decode round trip
-- instead of one search per track - the exact seam guild_playlists and the cold
-- restore already use. NULL on rows saved before this column existed; those are
-- resolved once by search and backfilled, so the search path drains to nothing.
ALTER TABLE music_favorites ADD COLUMN IF NOT EXISTS encoded TEXT;

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
-- cogs/anilist/feed.py (owner cog)
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
-- prefix.  cogs/anilist/feed.py
CREATE TABLE IF NOT EXISTS anilist_follows (
    guild_id         BIGINT      NOT NULL,
    channel_id       BIGINT      NOT NULL,
    anilist_user_id  INTEGER     NOT NULL,                 -- AniList numeric user id
    anilist_username TEXT,                                 -- cached name for the panel
    added_by         BIGINT,                               -- Discord user who added them
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id, anilist_user_id)
);

-- Per-channel mutes: a FOLLOWED AniList user whose activity this ONE feed does
-- not want. The follow itself is untouched, so the poller keeps fetching that
-- user globally and a second feed following them still receives everything -
-- a mute is a DELIVERY filter (cogs/anilist/feed_policy.py route_activities),
-- never a fetch or cursor filter. Muting must not change what the global
-- cursor (anilist_feed_state) sees, or a mute in one channel would silently
-- skip that activity for every other channel too.
-- Capped at MAX_FOLLOWS_PER_FEED per feed (enforced in code): a feed can at
-- most mute everyone it follows. ``anilist_username`` is a cached display name
-- for the panel, like anilist_follows. Lookups by feed ride the
-- (guild_id, channel_id, ...) PK prefix.  cogs/anilist/feed.py
CREATE TABLE IF NOT EXISTS anilist_feed_mutes (
    guild_id         BIGINT      NOT NULL,
    channel_id       BIGINT      NOT NULL,
    anilist_user_id  INTEGER     NOT NULL,                 -- AniList numeric user id
    anilist_username TEXT,                                 -- cached name for the panel
    muted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
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
-- MangaDex chapter alerts (tools/mangadex.py + cogs/anilist/chapters.py)
-- ============================================================

-- MangaDex chapter-alert opt-ins: mirrors anilist_airing_optins exactly, chapter
-- flavour. Users who chose to be DMed when a new chapter of a title on their
-- MangaDex-mapped manga list drops (with a one-click Read button).
-- One row per Discord user; ``anilist_user_id`` is their AniList numeric id,
-- resolved once at opt-in so the poller can read their PUBLIC manga list
-- unauthenticated (no token at poll time). ``enabled`` is flipped off
-- automatically when their DMs are closed (a Forbidden on delivery) and they can
-- re-run the toggle to turn it back on. Lookups ride the PK.
-- cogs/anilist/chapters.py
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
-- exists solely to STOP the poller re-searching that media every tick. ``missing``
-- is NOT permanent though: ``checked_at`` is the staleness clock, and a 'missing'
-- row older than MISSING_RETRY_DAYS (7) becomes a search candidate again, so a
-- niche title added to MangaDex after we first looked stops being invisible for
-- good. Retries are strictly second-class - they only spend the per-tick search
-- budget that never-searched media leave - and every completed retry re-stamps
-- checked_at (found or still missing), so an absent title costs at most one
-- search per week.
-- One row per AniList media; lookups ride the PK.  cogs/anilist/chapters.py
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
-- per manga; lookups ride the PK.  cogs/anilist/chapters.py
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
-- cogs/anilist/chapters.py
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

-- Coalescing state for the AniList activity feed: at most ONE live card per
-- (feed channel, AniList user, media). AniList emits a separate activity every
-- time a user saves progress, so a reader who saves ch.50 then ch.54 on the same
-- manga produces two activities where the second supersedes the first. The
-- delivery layer folds consecutive same-status progress increments into a SINGLE
-- card that is EDITED in place (a Discord edit is silent = zero notification),
-- keyed by this table. Two clocks bound the fold, both pure comparisons in
-- cogs.anilist.feed_coalesce: ``updated_at`` is the last-edit time (the
-- SESSION_GAP = 30 min clock - a longer quiet gap opens a fresh card) and doubles
-- as the sweep's prune key; ``created_at`` is the first-post time (the AGE_CAP =
-- 6 h clock - an unbroken session still gets a fresh card once the current one is
-- this old). ``last_progress`` caches the raw AniList progress of the newest fold
-- and ``status`` the list status being coalesced, so a backwards jump or a status
-- change also opens a fresh card. ``activity_id`` is the newest activity folded
-- in (the card's interactive buttons carry it). One row per slot; lookups ride
-- the PK, the sweep rides the updated_at index.  cogs/anilist/feed.py
CREATE TABLE IF NOT EXISTS anilist_feed_posts (
    guild_id      BIGINT      NOT NULL,
    channel_id    BIGINT      NOT NULL,                       -- feed text channel OR thread id
    user_id       BIGINT      NOT NULL,                       -- AniList numeric user id
    media_id      INTEGER     NOT NULL,                       -- AniList numeric media id
    message_id    BIGINT      NOT NULL,                       -- the live coalescing card's message
    activity_id   BIGINT      NOT NULL,                       -- newest AniList activity folded in
    last_progress TEXT,                                       -- raw AniList progress of the newest fold
    status        TEXT,                                       -- AniList list status the card coalesces
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),         -- first-post time (AGE_CAP clock)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),         -- last-edit time (SESSION_GAP clock + prune key)
    PRIMARY KEY (channel_id, user_id, media_id)
);
CREATE INDEX IF NOT EXISTS anilist_feed_posts_prune_idx ON anilist_feed_posts (updated_at);

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
-- Dashboard -> bot action queue (in-bot executor via pg_notify)
-- ============================================================
-- A durable work queue for actions the dashboard (a SEPARATE Node process)
-- wants the LIVE bot to perform on Discord - things the dashboard itself cannot
-- do because it has no gateway connection (e.g. posting the Verify button into a
-- channel). The dashboard INSERTs a row here and fires
-- ``SELECT pg_notify('yasuho_dashboard_action', <id>)`` on a channel DEDICATED
-- to this queue (distinct from the 'yasuho_dashboard' cache-invalidation
-- channel); the bot's cogs/system/dashboard_actions.py LISTENs on that channel
-- and, per notification, atomically CLAIMs the row (UPDATE ... WHERE
-- status='pending' RETURNING - single-flight, so a duplicate notify is a no-op),
-- runs the matching executor and writes back status + result. The bot also
-- reconciles at boot so an action enqueued while it was restarting is not lost.
-- ``kind`` selects the executor; ``payload`` is the (bot-revalidated, never
-- trusted) arguments; ``result`` is the JSON outcome the dashboard polls to show
-- the user; ``requested_by`` is the Discord id of the user who asked (audit),
-- written under the dashboard's requireManageGuild gate.  cogs/system/dashboard_actions.py
--
-- SCOPE: a row names EITHER a guild OR a user, never both and never neither -
-- asserted by the dashboard_actions_scope_valid CHECK at the end of this file.
-- Guild rows are the original ones (written under requireManageGuild); user rows
-- exist for the kinds that act on somebody's OWN data (``mydata_export``), where
-- the dashboard's gate is "this is your session", not "you manage this guild".
-- Which column a kind reads is decided by the kind itself
-- (``dashboard_actions._USER_KINDS``), never by which column happens to be set,
-- so a guild kind smuggled onto a user row (or the reverse) is refused instead of
-- acting on the wrong scope.
--
-- Lifecycle, per scope: guild rows die with their guild
-- (tools/retention.GUILD_DELETE_QUERIES); user rows carry no guild_id, so that
-- purge cannot reach them and the daily pass ages the TERMINAL ones out instead
-- (tools/retention.prune_user_scoped_actions). They are also in the personal-data
-- export, under `dashboard_requests` - a row saying "you asked for this, then"
-- is that user's data and they must be able to see it.
CREATE TABLE IF NOT EXISTS dashboard_actions (
    id           BIGSERIAL   PRIMARY KEY,
    guild_id     BIGINT,                                   -- NULL on a user-scoped row
    user_id      BIGINT,                                   -- NULL on a guild-scoped row
    kind         TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT        NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
    result       JSONB,
    requested_by BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migrate pre-existing installs (no-ops on a fresh database). DROP NOT NULL is
-- itself idempotent: dropping it again is accepted and does nothing.
ALTER TABLE dashboard_actions ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE dashboard_actions ALTER COLUMN guild_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS dashboard_actions_guild_idx ON dashboard_actions (guild_id, status);
-- The user-scoped twin of the index above, and for the same single reader: the
-- dashboard polls "the actions of THIS scope, by status" to render progress. That
-- read MUST be scoped by user_id (a status read keyed on the action id alone
-- would let anyone poll anyone's export), so give it the same index shape the
-- guild side has. PARTIAL because the two scopes are exclusive: guild rows all
-- carry user_id IS NULL and would otherwise fill this index with dead entries.
-- Nothing on the bot side needs it - the claim is by primary key and the boot
-- reconciliation sweeps by status across every scope.
CREATE INDEX IF NOT EXISTS dashboard_actions_user_idx
    ON dashboard_actions (user_id, status) WHERE user_id IS NOT NULL;

-- ============================================================
-- Dashboard configuration journal (written by the dashboard, never by the bot)
-- ============================================================
-- "Who changed what, in which section of the dashboard, and when." The DASHBOARD
-- (the separate Node process) is the only writer: the bot NEVER INSERTs here,
-- and grepping the repo for `INTO dashboard_audit` returning nothing is the
-- check. The bot only ever PURGES this table (retention) and READS it (the
-- courtesy export below), which is why it is declared here rather than left to
-- the dashboard's own migrations - schema.sql is the one place that says what
-- this database contains.
--
-- VALUES ARE NEVER STORED. `section` is the dashboard page (e.g. 'leveling'),
-- `action` is the verb ('update', 'reset'), and `detail` is a short free-text
-- note about WHICH knob moved - never the old or the new value. A journal that
-- carried values would quietly become a second copy of the guild's whole
-- configuration, with none of the deletion paths the real tables have.
--
-- LIFECYCLE, decided on the `cases`/`warns` precedent and nothing else:
--   * guild_id is NOT NULL, so every row is guild data and the row DIES WITH THE
--     GUILD (tools/retention.GUILD_DELETE_QUERIES, plus the discovery UNION so an
--     orphaned journal schedules its own purge). Guild-scoped is the whole
--     governance model here: the audit trail belongs to the server, not to the
--     admin who happened to click.
--   * actor_user_id is NOT ERASABLE by its own subject. It is on NO user
--     erasure list, deliberately: an admin who leaves a staff team must not be
--     able to blank the record of the changes they made, exactly as they cannot
--     erase the `cases` they filed as a moderator. The 90-day age prune
--     (retention.prune_stale_dashboard_audit) is what bounds it instead.
--   * the actor MAY EXPORT their own rows, as a courtesy, under
--     `dashboard_audit` in /mydata - the same reduction `cases` gets on the
--     moderator side (tools/privacy.collect_user_export): their own action
--     facts, scoped WHERE actor_user_id, never another actor's rows.
CREATE TABLE IF NOT EXISTS dashboard_audit (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    actor_user_id BIGINT      NOT NULL,
    section       TEXT        NOT NULL,   -- dashboard page, never a value
    action        TEXT        NOT NULL,   -- the verb: update, reset, ...
    detail        TEXT,                   -- which knob moved, never what to
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The dashboard's own read: "this guild's journal, newest first". id DESC rather
-- than created_at DESC because id is the tiebreaker anyway (BIGSERIAL is
-- monotonic per row) and it keeps the index one column narrower.
CREATE INDEX IF NOT EXISTS dashboard_audit_guild_idx ON dashboard_audit (guild_id, id DESC);
-- The actor's own read, and the ONE query the courtesy export runs. Same shape,
-- other key: without it that export would seq-scan the whole journal of every
-- guild to find one person's rows.
CREATE INDEX IF NOT EXISTS dashboard_audit_actor_idx ON dashboard_audit (actor_user_id, id DESC);

-- ============================================================
-- Bot liveness heartbeat (read by the dashboard, written only by the bot)
-- ============================================================
-- ONE row, forever: the id column is a smallint pinned to 1 by a CHECK, so the
-- table cannot grow a second row however it is written to - "the bot" is a
-- singleton and the schema says so rather than hoping every writer remembers.
--
-- Owner: cogs/system/dashboard_sync.py, which upserts it every 30s over the
-- MAIN connection pool - deliberately NOT over its dedicated LISTEN connection,
-- because the interesting case is precisely when that connection is down.
-- ``listening`` is that connection's state: true once a LISTEN is registered on
-- 'yasuho_dashboard', false from the moment the watch loop calls it dead (and on
-- a clean unload), so the dashboard can tell "the bot is offline" from "the bot
-- is up but its dashboard link is broken - your changes may not apply live".
-- ``version`` is the running commit's git short hash, or NULL when it cannot be
-- read (no git, no checkout); it is diagnostics only, never a gate.
--
-- CONTRACT with the dashboard: it READS this row and NEVER writes it. That is
-- CONVENTION, not permission - both processes connect as the same database role,
-- so nothing at the grant level stops a dashboard write; say so in the handoff
-- rather than letting the comment read as an enforced guarantee. Freshness
-- threshold: updated_at older than 90 seconds (three missed beats) means the bot
-- is offline. A shorter threshold would flap on a single slow write.
--
-- No guild_id and no user_id, by construction: this is process state, not
-- anybody's data. That is also why neither structural guard applies to it - the
-- guild purge guard (tests/tools/test_retention.py) enumerates tables with a
-- guild_id column and the personal-export guard (tests/tools/test_privacy.py)
-- tables with a user_id column, and this table has neither.
CREATE TABLE IF NOT EXISTS bot_heartbeat (
    id         SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    updated_at TIMESTAMPTZ NOT NULL,
    listening  BOOLEAN     NOT NULL,
    version    TEXT
);

-- ============================================================
-- Server statistics (aggregates only)
-- ============================================================
-- Owner: cogs/community/serverstats. Collected for every guild, with NO message
-- content and NO user id of any kind: only counts per channel-day and per
-- guild-day. Both tables are pruned to the last 90 days by the collector's own
-- lazy prune, so they have a fixed steady-state size.
CREATE TABLE IF NOT EXISTS server_stats_messages (
    guild_id   BIGINT  NOT NULL,
    channel_id BIGINT  NOT NULL,   -- thread messages roll up to the parent
    day        DATE    NOT NULL,   -- UTC day the messages were sent
    messages   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id, day)
);
-- Reads are "this guild, this window", and the PK already leads with guild_id:
-- a guild holds at most (its channels x 90) rows, so the PK range scan plus a
-- day filter is enough and a (guild_id, day) index would only be dead weight.
-- The 90-day prune, on the other hand, scans by day across every guild.
CREATE INDEX IF NOT EXISTS server_stats_messages_day_idx
    ON server_stats_messages (day);

-- One row per guild-day: the day's join/leave counters (humans only, since bots
-- are infrastructure rather than growth) plus a snapshot of Discord's member_count
-- taken on the first flush of that UTC day (NULL until that first flush).
CREATE TABLE IF NOT EXISTS server_stats_days (
    guild_id     BIGINT  NOT NULL,
    day          DATE    NOT NULL,   -- UTC day
    member_count INTEGER,            -- daily snapshot, NULL if not taken yet
    joins        INTEGER NOT NULL DEFAULT 0,
    leaves       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, day)
);
CREATE INDEX IF NOT EXISTS server_stats_days_day_idx ON server_stats_days (day);

-- Weekly digest delivery bookkeeping: which ISO week a guild last received its
-- digest for. Owner: cogs/community/serverstats/digest.py.
--
-- One row per guild that has EVER been delivered a digest, and the whole point
-- of the row is exactly-once-per-week: the loop CLAIMS the week here (an
-- INSERT ... ON CONFLICT DO UPDATE ... WHERE last_iso_week <> EXCLUDED, whose
-- RETURNING is the permission to send) before it posts anything, so a restart
-- mid-fan-out, a duplicated tick or two concurrent ticks can never post the same
-- week twice. It is DELIVERY state, not statistics: nothing here is a count, and
-- it holds no user id and no content.
--
-- last_iso_week is the 'W2026-31' key cogs.community.leveling.engine.
-- iso_week_period_key builds - the same vocabulary xp_period and the retention
-- block already speak, so the state row names a week exactly as every other
-- weekly artefact in this database does.
--
-- No index beyond the primary key: every access is by guild_id (the claim) or
-- as the anti-join side of the candidate query, which is bounded by the number
-- of guilds that opted in.
CREATE TABLE IF NOT EXISTS serverstats_digest_state (
    guild_id      BIGINT PRIMARY KEY,
    last_iso_week TEXT   NOT NULL
);

-- The digest's opt-in switch lives in the guild_settings JSONB blob (absent =
-- off, never materialised), and the hourly delivery tick has to find the guilds
-- that carry it. A PARTIAL index whose predicate IS that presence test holds
-- only the opted-in guilds, so the scan is proportional to how many servers
-- turned the digest on rather than to how many servers exist.
--
-- MEASURED with psql (NOT with asyncpg: it caches prepared statements by query
-- text, so a naive before/after EXPLAIN of the same string replays the stale
-- plan and reports a Seq Scan with the index in place) in a rolled-back
-- transaction on a 50k-row guild_settings fixture with 1250 guilds opted in:
-- without it, `Seq Scan ... Rows Removed by Filter: 48760`, 1334 buffers,
-- ~9 ms, growing with the FLEET; with it, `Index Scan using
-- guild_settings_digest_channel_idx`, 30 buffers, 0.16 ms, flat in fleet size.
-- The drained case - a later tick of a week in which every opted-in guild has
-- already been delivered - still walks this index whole and returns nothing:
-- 681 buffers, 1.3 ms, proportional to the OPTED-IN count and not to the fleet,
-- which is the whole point.
-- The write side pays only on guild_settings writes (a manager changing a
-- setting), which are rare by construction.
CREATE INDEX IF NOT EXISTS guild_settings_digest_channel_idx
    ON guild_settings (guild_id)
    WHERE settings ? 'serverstats_digest_channel';

-- ============================================================
-- Command usage (GLOBAL aggregates only)
-- ============================================================
-- Owner: cogs/system/botstats.py + cogs/system/usage_stats.py. One row per
-- (UTC day, command name) for the WHOLE bot: how many times a command completed,
-- split by the surface it was invoked from. This is what lets ?botstats answer
-- "what was used today / this week / this month" across restarts, since the
-- in-memory counters reset with the process.
--
-- There is deliberately NO user_id and NO guild_id here - not "not yet", never:
-- the table answers a fleet-wide operational question, so nothing in it is
-- personal data and nothing in it belongs to a guild. That is also why it needs
-- no entry in the /mydata export nor in the departed-guild purge (both of those
-- are driven structurally, off the columns above).
--
-- ``command`` is a qualified_name defined in this repository's own source, never
-- user text: custom commands are dispatched by cogs/config/customcommands.py
-- without ever becoming discord.py commands, so they cannot reach this table.
-- Cardinality is therefore the number of distinct commands used in a day (a few
-- hundred at most), and the collector prunes past 400 days, so the table has a
-- fixed steady-state size of a few tens of thousands of rows at worst.
--
-- Counts are BIGINT because they are cumulative per day for the whole fleet, and
-- the flush ADDS onto them (ON CONFLICT DO UPDATE ... + EXCLUDED) every 5
-- minutes: an INTEGER would be a ceiling nobody would notice until it wrapped.
CREATE TABLE IF NOT EXISTS command_usage (
    day          DATE   NOT NULL,          -- UTC day the command completed on
    command      TEXT   NOT NULL,          -- qualified_name, e.g. 'level rank'
    prefix_count BIGINT NOT NULL DEFAULT 0,
    slash_count  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, command)
);
-- No extra index on purpose: every read here is "the last N days" and the PK
-- already LEADS with day, so the window scan and the daily prune both ride the
-- primary key. (server_stats_messages needs its own day index only because its
-- PK leads with guild_id.)

-- The WHEN of the same completions: a rolling (UTC weekday, UTC hour) profile of
-- how much this bot is used, so ?botstats can answer "which hours of the week is
-- a restart cheapest in". Fed by the SAME flush as command_usage, in the same
-- statement (cogs/system/usage_stats.FLUSH), and kept SEPARATE from it because
-- command_usage has no hour dimension and must not grow one: adding hours there
-- would multiply its cardinality by 24 to answer a question asked of the whole
-- fleet at once, never of a single command.
--
-- FIXED SIZE, FOR EVER: 7 weekdays x 24 hours = 168 rows on every install, from
-- the first flush to the last. There is nothing to prune and no retention window
-- here - the ageing is done by HALVING (see below), not by deleting.
--
-- ``dow`` is 0 = Monday ... 6 = Sunday, i.e. Python's date.weekday(), computed
-- in Python from a UTC day (cogs/system/usage_stats.day_of_week). Postgres has
-- two other conventions for the same seven days (EXTRACT(DOW) is Sunday = 0,
-- EXTRACT(ISODOW) is Monday = 1), so nothing ever derives this column in SQL.
-- ``hour`` is the UTC hour 0..23, captured when the command completed.
--
-- Counts are BIGINT and cumulative like command_usage's, for the same reason.
-- They are also DECAYED: once every 7 days the whole table is halved
-- (cogs/system/usage_stats.DECAY_HOURLY), which makes the profile an exponential
-- moving average with a one-week half-life instead of a lifetime average no
-- recent habit could ever move. Integer division floors, so a slot that stops
-- being used fades to 0 rather than lingering for ever. The halving is
-- at-most-once per pass rather than once per elapsed week (halved_on is set to
-- today, not to halved_on + 7), so an outage of any length costs one halving.
--
-- Like command_usage: NO user_id and NO guild_id, so no /mydata entry and no
-- departed-guild purge entry (both are driven structurally off those columns).
CREATE TABLE IF NOT EXISTS command_usage_hourly (
    dow   SMALLINT NOT NULL,          -- 0 = Monday ... 6 = Sunday (UTC)
    hour  SMALLINT NOT NULL,          -- 0..23 (UTC)
    count BIGINT   NOT NULL DEFAULT 0,
    PRIMARY KEY (dow, hour),
    CONSTRAINT command_usage_hourly_slot_valid
        CHECK (dow BETWEEN 0 AND 6 AND hour BETWEEN 0 AND 23 AND count >= 0)
);

-- The profile's own bookkeeping, one row, id = 1 (the bot_heartbeat singleton
-- idiom). Two dates, each carrying something the profile itself cannot:
--
-- ``started_on`` - when the profile began collecting. The dashboard needs it to
-- know whether every one of the 168 slots has been LIVED THROUGH yet: on an
-- existing install command_usage may hold a year of days on the very day this
-- table is created, so coverage read from there would be a lie.
-- ``halved_on``  - when the weekly decay last ran. It is in the DATABASE, unlike
-- the daily retention prune's cadence marker (which is in memory on the cog and
-- says so), because re-running the prune after a restart re-deletes already-
-- expired rows and costs nothing, while re-running the halving DESTROYS data -
-- and this deployment restarts on every deploy.
CREATE TABLE IF NOT EXISTS command_usage_hourly_state (
    id         SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    started_on DATE     NOT NULL,     -- first maintenance pass that saw this table
    halved_on  DATE     NOT NULL      -- last weekly decay
);

-- ============================================================
-- Support tickets (METADATA ONLY)
-- ============================================================
-- Owner: cogs/config/tickets/. A ticket is a PRIVATE THREAD on the panel
-- channel; the conversation lives in Discord and NOTHING of it is ever mirrored
-- here. This table holds only the bookkeeping the bot cannot re-derive: which
-- thread is which ticket, who opened it, when, and whether it is still open.
-- There is deliberately no subject, no transcript and no message column - the
-- subject the opener types goes into the thread's opening message and nowhere
-- else, so a ticket's CONTENT can only ever be read where its participants can
-- read it (and dies with the thread).
--
-- ticket_number is the per-guild human label (#1, #2, ...), computed as
-- MAX + 1 INSIDE the INSERT exactly like `cases`. UNIQUE (guild_id,
-- ticket_number) is what makes that safe under concurrency: two simultaneous
-- opens in one guild compute the same number, so the second blocks on this
-- index and then fails, and the caller's bounded retry recomputes both the
-- number AND the per-user cap against the winner's now-visible row. That is
-- also why the cap can never be exceeded by a double click - see
-- cogs/config/tickets/storage.py.
CREATE TABLE IF NOT EXISTS tickets (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    ticket_number INTEGER     NOT NULL,          -- sequential per guild (#1, #2, ...)
    thread_id     BIGINT      NOT NULL UNIQUE,   -- the private thread; the ticket IS it
    opener_id     BIGINT      NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'open',
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ,                   -- NULL while open
    closed_by     BIGINT,                        -- NULL while open
    UNIQUE (guild_id, ticket_number)
);
-- PARTIAL on purpose: every recurring read is about the OPEN ones (the per-user
-- cap at click time, and the inactivity sweep). Closed rows are history and only
-- ever read one at a time by thread id, which the UNIQUE above already serves,
-- so keeping them out of this index keeps it the size of the live workload
-- rather than of the guild's whole ticket history.
CREATE INDEX IF NOT EXISTS tickets_guild_open_idx
    ON tickets (guild_id) WHERE status = 'open';
-- "every ticket this user opened", across guilds: the /mydata export path.
CREATE INDEX IF NOT EXISTS tickets_opener_idx ON tickets (opener_id);
-- Lot T2. Which staff member took the ticket, NULL while nobody has. Additive
-- and nullable on purpose: NULL is a real answer ("unclaimed"), so no backfill
-- and no default can be right here.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS claimed_by BIGINT;
-- The inactivity sweep's index. It reads the open set in ID order from a
-- rotating cursor (cogs/config/tickets/lifecycle.py), which the guild_id partial
-- index above cannot serve and the primary key can only serve by walking every
-- CLOSED row in between - and closed rows are the ones that grow without bound.
-- PARTIAL for the same reason as its sibling: it stays the size of the live
-- workload rather than of the guild's whole ticket history. Probed on
-- PostgreSQL: a 50-row pass over 2300 rows (2000 of them closed) is an index
-- scan touching 2 shared buffers.
CREATE INDEX IF NOT EXISTS tickets_open_sweep_idx ON tickets (id) WHERE status = 'open';

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
    -- A row of topgg_votes exists BECAUSE a vote was recorded, so both counters
    -- start at 1 and only ever go up; zero or negative would mean the upsert's
    -- streak CASE (or its DEFAULTs) had been broken.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'topgg_votes_counters_positive'
    ) THEN
        ALTER TABLE topgg_votes
            ADD CONSTRAINT topgg_votes_counters_positive
            CHECK (streak >= 1 AND total_votes >= 1) NOT VALID;
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
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'dashboard_actions_status_valid'
    ) THEN
        ALTER TABLE dashboard_actions
            ADD CONSTRAINT dashboard_actions_status_valid
            CHECK (status IN ('pending', 'running', 'done', 'failed')) NOT VALID;
    END IF;
    -- Exactly one scope per row. `<>` on two booleans is XOR, so this rejects
    -- both a row naming a guild AND a user (which scope would the executor act
    -- on?) and a row naming neither (an action with nothing to act on). Added
    -- NOT VALID like every constraint here, though every legacy row already
    -- satisfies it: before this lot guild_id was NOT NULL and user_id did not
    -- exist, so every one of them is (guild set, user NULL).
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'dashboard_actions_scope_valid'
    ) THEN
        ALTER TABLE dashboard_actions
            ADD CONSTRAINT dashboard_actions_scope_valid
            CHECK ((guild_id IS NULL) <> (user_id IS NULL)) NOT VALID;
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
        SELECT 1 FROM pg_constraint WHERE conname = 'anilist_feed_mutes_feed_fk'
    ) THEN
        ALTER TABLE anilist_feed_mutes
            ADD CONSTRAINT anilist_feed_mutes_feed_fk
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
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'anilist_feed_posts_feed_fk'
    ) THEN
        ALTER TABLE anilist_feed_posts
            ADD CONSTRAINT anilist_feed_posts_feed_fk
            FOREIGN KEY (guild_id, channel_id)
            REFERENCES anilist_feeds(guild_id, channel_id)
            ON DELETE CASCADE NOT VALID;
    END IF;
END
$$;
