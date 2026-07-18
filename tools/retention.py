"""Bounded avatar and departed-guild data retention.

Guild data is deleted only after a durable 30-day grace job becomes due. Every
guild purge is one PostgreSQL transaction over a fixed, reviewed query list.
Global user data is deliberately absent from that list.
"""

from __future__ import annotations

import datetime
import logging

from tools import settings

log = logging.getLogger(__name__)

AVATAR_MAX_PER_SERIES = 30
AVATAR_MIN_KEEP_PER_SERIES = 5
AVATAR_MAX_AGE_MONTHS = 18
AVATAR_PRUNE_BATCH_SIZE = 250
AVATAR_PRUNE_MAX_BATCHES = 20
GUILD_GRACE_DAYS = 30
GUILD_PURGES_PER_RUN = 5

# Deletion order matters for the foreign keys installed by migration 0002.
# Every query is static: no table or column name comes from user input.
GUILD_DELETE_QUERIES = (
    (
        "anilist_channel_subs",
        "DELETE FROM anilist_channel_subs WHERE guild_id = $1",
    ),
    (
        "anilist_follows",
        "DELETE FROM anilist_follows WHERE guild_id = $1",
    ),
    (
        "anilist_feeds",
        "DELETE FROM anilist_feeds WHERE guild_id = $1",
    ),
    (
        "starboard_entries",
        "DELETE FROM starboard_entries WHERE guild_id = $1",
    ),
    ("starboard", "DELETE FROM starboard WHERE guild_id = $1"),
    ("prefixes", "DELETE FROM prefixes WHERE guild_id = $1"),
    ("autorole", "DELETE FROM autorole WHERE guild_id = $1"),
    ("muterole", "DELETE FROM muterole WHERE guild_id = $1"),
    ("mutedmembers", "DELETE FROM mutedmembers WHERE mguild_id = $1"),
    ("warns", "DELETE FROM warns WHERE guild_id = $1"),
    ("twitch_alert", "DELETE FROM twitch_alert WHERE guild_id = $1"),
    ("auto_room", "DELETE FROM auto_room WHERE guild_id = $1"),
    ("modlog", "DELETE FROM modlog WHERE guild_id = $1"),
    ("levels", "DELETE FROM levels WHERE guild_id = $1"),
    ("level_config", "DELETE FROM level_config WHERE guild_id = $1"),
    (
        "xp_multipliers",
        "DELETE FROM xp_multipliers WHERE guild_id = $1",
    ),
    ("level_rewards", "DELETE FROM level_rewards WHERE guild_id = $1"),
    ("level_no_xp", "DELETE FROM level_no_xp WHERE guild_id = $1"),
    ("xp_period", "DELETE FROM xp_period WHERE guild_id = $1"),
    ("welcome", "DELETE FROM welcome WHERE guild_id = $1"),
    (
        "reaction_roles",
        "DELETE FROM reaction_roles WHERE guild_id = $1",
    ),
    ("automod", "DELETE FROM automod WHERE guild_id = $1"),
    (
        "avatar_history",
        "DELETE FROM avatar_history WHERE guild_id = $1",
    ),
    (
        "guild_settings",
        "DELETE FROM guild_settings WHERE guild_id = $1",
    ),
    ("cases", "DELETE FROM cases WHERE guild_id = $1"),
    ("button_roles", "DELETE FROM button_roles WHERE guild_id = $1"),
    (
        "guild_playlists",
        "DELETE FROM guild_playlists WHERE guild_id = $1",
    ),
    ("music_state", "DELETE FROM music_state WHERE guild_id = $1"),
    (
        "custom_commands",
        "DELETE FROM custom_commands WHERE guild_id = $1",
    ),
    ("role_menus", "DELETE FROM role_menus WHERE guild_id = $1"),
    (
        # Reminders are user-owned even when created in a guild. A departed
        # guild must not collaterally delete a member's pending reminders; any
        # that are genuinely undeliverable die naturally at fire time via the
        # NotFound terminal ack. Every other guild-scoped timer is purged.
        "timers",
        "DELETE FROM timers "
        "WHERE extra->>'guild_id' = ($1::bigint)::text "
        "AND event <> 'reminder'",
    ),
)

STORED_GUILD_IDS_QUERY = """
SELECT guild_id FROM prefixes
UNION SELECT guild_id FROM autorole
UNION SELECT guild_id FROM muterole
UNION SELECT mguild_id FROM mutedmembers
UNION SELECT guild_id FROM warns
UNION SELECT guild_id FROM twitch_alert
UNION SELECT guild_id FROM auto_room
UNION SELECT guild_id FROM modlog
UNION SELECT guild_id FROM levels
UNION SELECT guild_id FROM level_config
UNION SELECT guild_id FROM xp_multipliers
UNION SELECT guild_id FROM level_rewards
UNION SELECT guild_id FROM level_no_xp
UNION SELECT guild_id FROM xp_period
UNION SELECT guild_id FROM starboard
UNION SELECT guild_id FROM starboard_entries
UNION SELECT guild_id FROM welcome
UNION SELECT guild_id FROM reaction_roles
UNION SELECT guild_id FROM automod
UNION SELECT guild_id FROM avatar_history WHERE guild_id IS NOT NULL
UNION SELECT guild_id FROM guild_settings
UNION SELECT guild_id FROM cases
UNION SELECT guild_id FROM button_roles
UNION SELECT guild_id FROM guild_playlists
UNION SELECT guild_id FROM music_state
UNION SELECT guild_id FROM custom_commands
UNION SELECT guild_id FROM role_menus
UNION SELECT guild_id FROM anilist_feeds
UNION SELECT guild_id FROM anilist_follows
UNION SELECT guild_id FROM anilist_channel_subs
UNION
SELECT (extra->>'guild_id')::bigint FROM timers
WHERE extra->>'guild_id' ~ '^[0-9]+$'
"""


def _affected_rows(status):
    """Extract asyncpg's affected-row count from ``DELETE n``."""
    try:
        return int((status or "").rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


async def schedule_guild_purge(pool, guild_id, *, left_at=None):
    """Create or reset a guild's 30-day grace-period purge job."""
    left_at = left_at or datetime.datetime.now(datetime.timezone.utc)
    purge_after = left_at + datetime.timedelta(days=GUILD_GRACE_DAYS)
    await pool.execute(
        "INSERT INTO guild_retention_jobs "
        "(guild_id, left_at, purge_after, attempts, last_error, claimed_at) "
        "VALUES ($1, $2, $3, 0, NULL, NULL) "
        "ON CONFLICT (guild_id) DO UPDATE SET "
        "left_at = EXCLUDED.left_at, purge_after = EXCLUDED.purge_after, "
        "attempts = 0, last_error = NULL, claimed_at = NULL",
        guild_id,
        left_at,
        purge_after,
    )
    return purge_after


async def list_guild_jobs(pool, limit=50):
    """Return the soonest-due scheduled purge jobs for operator inspection."""
    return await pool.fetch(
        "SELECT guild_id, left_at, purge_after, attempts, last_error, "
        "claimed_at FROM guild_retention_jobs "
        "ORDER BY purge_after, guild_id LIMIT $1",
        limit,
    )


async def cancel_guild_purge(pool, guild_id):
    """Cancel a pending purge because the bot rejoined the guild."""
    status = await pool.execute(
        "DELETE FROM guild_retention_jobs WHERE guild_id = $1",
        guild_id,
    )
    return _affected_rows(status) > 0


async def reconcile_guild_jobs(pool, active_guild_ids):
    """Schedule previously orphaned guild data and protect active guilds.

    Departure timestamps for pre-existing orphan rows are unknown, so discovery
    starts a fresh 30-day grace period rather than deleting them immediately.
    Existing pending jobs keep their original deadline.
    """
    active = {int(guild_id) for guild_id in active_guild_ids}
    rows = await pool.fetch(STORED_GUILD_IDS_QUERY)
    stored = {int(row["guild_id"]) for row in rows if row["guild_id"] is not None}

    if active:
        await pool.execute(
            "DELETE FROM guild_retention_jobs "
            "WHERE guild_id = ANY($1::bigint[])",
            sorted(active),
        )

    orphaned = sorted(stored - active)
    if not orphaned:
        return 0
    status = await pool.execute(
        "INSERT INTO guild_retention_jobs (guild_id, left_at, purge_after) "
        "SELECT guild_id, now(), now() + make_interval(days => $2) "
        "FROM unnest($1::bigint[]) AS guild_id "
        "ON CONFLICT (guild_id) DO NOTHING",
        orphaned,
        GUILD_GRACE_DAYS,
    )
    return _affected_rows(status)


async def claim_due_guild(pool):
    """Durably claim the oldest due guild purge, recovering stale claims."""
    return await pool.fetchrow(
        "WITH candidate AS ("
        "SELECT guild_id FROM guild_retention_jobs "
        "WHERE purge_after <= now() AND (claimed_at IS NULL OR "
        "claimed_at < now() - interval '6 hours') "
        "ORDER BY purge_after, guild_id FOR UPDATE SKIP LOCKED LIMIT 1"
        ") "
        "UPDATE guild_retention_jobs AS jobs SET claimed_at = now() "
        "FROM candidate WHERE jobs.guild_id = candidate.guild_id "
        "RETURNING jobs.guild_id, jobs.left_at, jobs.purge_after, jobs.attempts"
    )


async def release_guild_claim(pool, guild_id, error):
    """Release a failed claim and preserve a bounded diagnostic for retry."""
    await pool.execute(
        "UPDATE guild_retention_jobs SET claimed_at = NULL, "
        "attempts = attempts + 1, last_error = $2, "
        "purge_after = GREATEST("
        "purge_after, now() + interval '1 hour'"
        ") "
        "WHERE guild_id = $1",
        guild_id,
        str(error)[:500],
    )


async def purge_claimed_guild(pool, guild_id):
    """Delete all guild-scoped data transactionally and return per-table counts.

    The job row is locked and rechecked inside the same transaction. A cancelled,
    not-yet-due or missing job therefore produces ``None`` and deletes nothing.
    """
    counts = {}
    async with pool.acquire() as connection:
        async with connection.transaction():
            job = await connection.fetchrow(
                "SELECT guild_id FROM guild_retention_jobs "
                "WHERE guild_id = $1 AND purge_after <= now() "
                "AND claimed_at IS NOT NULL FOR UPDATE",
                guild_id,
            )
            if job is None:
                return None

            for table, query in GUILD_DELETE_QUERIES:
                status = await connection.execute(query, guild_id)
                counts[table] = _affected_rows(status)

            await connection.execute(
                "DELETE FROM guild_retention_jobs WHERE guild_id = $1",
                guild_id,
            )
    return counts


async def prune_avatar_history_batch(pool, batch_size=AVATAR_PRUNE_BATCH_SIZE):
    """Delete one bounded batch under the 30/18-month/keep-5 policy."""
    rows = await pool.fetch(
        "WITH ranked AS ("
        "SELECT id, changed_at, row_number() OVER ("
        "PARTITION BY user_id, kind, guild_id "
        "ORDER BY changed_at DESC, id DESC"
        ") AS series_rank FROM avatar_history"
        "), victims AS ("
        "SELECT id FROM ranked WHERE series_rank > $1 "
        "OR (series_rank > $2 "
        "AND changed_at < now() - make_interval(months => $4)) "
        "ORDER BY changed_at, id LIMIT $3"
        "), deleted AS ("
        "DELETE FROM avatar_history AS history USING victims "
        "WHERE history.id = victims.id "
        "RETURNING octet_length(history.avatar) AS bytes"
        ") SELECT bytes FROM deleted",
        AVATAR_MAX_PER_SERIES,
        AVATAR_MIN_KEEP_PER_SERIES,
        batch_size,
        AVATAR_MAX_AGE_MONTHS,
    )
    return len(rows), sum(int(row["bytes"] or 0) for row in rows)


def invalidate_guild_caches(bot, guild_id):
    """Remove known in-memory mirrors after a guild purge or departure."""
    for attr in ("prefixes", "autoroles", "muteroles"):
        cache = getattr(bot, attr, None)
        if cache is not None:
            cache.pop(guild_id, None)
    settings.invalidate_guild(guild_id)

    leveling = bot.get_cog("Leveling")
    if leveling is not None:
        leveling._configs.pop(guild_id, None)
        leveling._no_xp.discard(guild_id)
        leveling._multipliers.discard(guild_id)
        leveling._period_markers.discard(guild_id)

    automod = bot.get_cog("AutoMod")
    if automod is not None:
        automod._settings.pop(guild_id, None)
        automod._spam = {
            key: value
            for key, value in automod._spam.items()
            if key[0] != guild_id
        }

    modlog = bot.get_cog("ModLog")
    if modlog is not None:
        modlog._channels.pop(guild_id, None)
        modlog._recent_bans = {
            key for key in modlog._recent_bans if key[0] != guild_id
        }
        modlog._suppressed = {
            key for key in modlog._suppressed if key[0] != guild_id
        }

    starboard = bot.get_cog("Starboard")
    if starboard is not None:
        starboard._config.pop(guild_id, None)

    custom_commands = bot.get_cog("CustomCommands")
    if custom_commands is not None:
        custom_commands._cache.pop(guild_id, None)
        custom_commands._uses.pop(guild_id, None)
        custom_commands._cd = {
            key: value
            for key, value in custom_commands._cd.items()
            if key[0] != guild_id
        }

    rooms = bot.get_cog("TemporaryRooms")
    if rooms is not None:
        rooms._hub_index.pop(guild_id, None)
