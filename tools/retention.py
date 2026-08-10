"""Bounded avatar and departed-guild data retention.

Guild data is deleted only after a durable 30-day grace job becomes due. Every
guild purge is one PostgreSQL transaction over a fixed, reviewed query list.
Global user data is deliberately absent from that list.
"""

from __future__ import annotations

import datetime
import logging

from tools import privacy, settings
from tools.db import affected_rows

log = logging.getLogger(__name__)

AVATAR_MAX_PER_SERIES = 30
AVATAR_MIN_KEEP_PER_SERIES = 5
AVATAR_MAX_AGE_MONTHS = 18
AVATAR_PRUNE_BATCH_SIZE = 250
AVATAR_PRUNE_MAX_BATCHES = 20
GUILD_GRACE_DAYS = 30
GUILD_PURGES_PER_RUN = 5
# How long a TERMINAL user-scoped dashboard action stays readable before it is
# pruned. Those rows are the user side of the queue ("you asked for an export at
# T, it ended like this") and no guild purge can ever reach them - they carry no
# guild_id by construction - so this is the only thing that bounds them. Long
# enough that the dashboard can still show a recent history, short enough that
# the table is not a permanent log of when somebody exported their data.
USER_ACTION_AUDIT_DAYS = 30

# How long a presence aggregate may sit untouched before it is emptied, and how
# many rows one pass may empty.
#
# This number is NOT free to drift: it is the same 30 days
# cogs/community/profile/presence.PURGE_AFTER_DAYS applies at the merge and at
# the render, and PRIVACY.md promises as the presence window. It is restated
# here rather than imported because tools/ must not import a cog (the cog
# imports tools), and tests/tools/test_retention.py pins the two together so the
# restatement cannot become a second opinion.
PRESENCE_AGGREGATE_MAX_AGE_DAYS = 30
PRESENCE_PRUNE_BATCH_SIZE = 500

# How long a dashboard configuration-journal row is kept, and how many one pass
# may delete. The journal is guild data and dies with its guild like `cases`, but
# a guild that never leaves would otherwise keep every "who changed what" line it
# ever produced for ever - nothing else ages them out. 90 days is the same window
# the serverstats aggregates are held to: long enough to answer "who broke the
# leveling config last month", short enough that the table is not a permanent
# record of an admin's every click.
DASHBOARD_AUDIT_MAX_AGE_DAYS = 90
DASHBOARD_AUDIT_PRUNE_BATCH_SIZE = 500
# One pass DRAINS the backlog rather than deleting a single batch: the journal
# is an event stream (one row per config action across every guild), so above
# ~500 fleet-wide writes a day a single-batch-per-tick prune could never hold
# the 90-day window and the table would grow without bound - the exact failure
# this prune exists to prevent. So the pass loops the bounded delete until it
# catches up, capped at this many batches so a pathological first-run backlog
# (millions of rows a nobody ever pruned) cannot take an unbounded lock in one
# tick; the cap is the same 20 the serverstats and avatar drains use. Hitting
# it is logged and the rest is met on the next daily tick.
DASHBOARD_AUDIT_PRUNE_MAX_BATCHES = 20

# Deletion order matters for the foreign keys installed by migration 0002.
# Every query is static: no table or column name comes from user input.
GUILD_DELETE_QUERIES = (
    (
        "anilist_channel_subs",
        "DELETE FROM anilist_channel_subs WHERE guild_id = $1",
    ),
    (
        "anilist_feed_mutes",
        "DELETE FROM anilist_feed_mutes WHERE guild_id = $1",
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
    ("season_podiums", "DELETE FROM season_podiums WHERE guild_id = $1"),
    ("rank_cards", "DELETE FROM rank_cards WHERE guild_id = $1"),
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
    (
        # Support-ticket bookkeeping (cogs/config/tickets/). Metadata only - the
        # conversations were Discord threads and left with the guild - but the
        # rows still say who asked this server for help and when, so they die
        # with it like every other guild record.
        "tickets",
        "DELETE FROM tickets WHERE guild_id = $1",
    ),
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
        # Server statistics are aggregates (no content, no user id), but they
        # are still this guild's activity history and die with it. The 90-day
        # collector prune only ages rows out; it never covers a departure.
        "server_stats_messages",
        "DELETE FROM server_stats_messages WHERE guild_id = $1",
    ),
    (
        "server_stats_days",
        "DELETE FROM server_stats_days WHERE guild_id = $1",
    ),
    (
        # Weekly-digest delivery state (which ISO week this guild was last sent
        # a digest for). Not statistics and not personal data, but it is keyed
        # by guild and would otherwise outlive the guild for ever - nothing else
        # ever deletes a row here.
        "serverstats_digest_state",
        "DELETE FROM serverstats_digest_state WHERE guild_id = $1",
    ),
    (
        # The dashboard -> bot action queue. Rows are terminal audit records
        # (kind, payload, the requesting user's id) that nothing else ever
        # deletes, so without this they would outlive the guild for good.
        # Since the queue gained a USER scope this only ever matches the
        # guild-scoped rows - a user row carries guild_id NULL, which no
        # equality matches, so a departing guild cannot take a member's own
        # export requests with it.
        "dashboard_actions",
        "DELETE FROM dashboard_actions WHERE guild_id = $1",
    ),
    (
        # The dashboard's configuration journal (who changed what, when). Written
        # only by the dashboard, never by the bot. guild_id is NOT NULL by
        # construction, so every row is guild data and dies with the guild -
        # the same call as `cases` and `warns`: the trail belongs to the server,
        # not to the admin who clicked, which is also why no user erasure path
        # touches it.
        "dashboard_audit",
        "DELETE FROM dashboard_audit WHERE guild_id = $1",
    ),
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
UNION SELECT guild_id FROM season_podiums
UNION SELECT guild_id FROM rank_cards
UNION SELECT guild_id FROM starboard
UNION SELECT guild_id FROM starboard_entries
UNION SELECT guild_id FROM welcome
UNION SELECT guild_id FROM reaction_roles
UNION SELECT guild_id FROM automod
UNION SELECT guild_id FROM avatar_history WHERE guild_id IS NOT NULL
UNION SELECT guild_id FROM guild_settings
UNION SELECT guild_id FROM cases
UNION SELECT guild_id FROM tickets
UNION SELECT guild_id FROM button_roles
UNION SELECT guild_id FROM guild_playlists
UNION SELECT guild_id FROM music_state
UNION SELECT guild_id FROM custom_commands
UNION SELECT guild_id FROM role_menus
UNION SELECT guild_id FROM anilist_feeds
UNION SELECT guild_id FROM anilist_follows
UNION SELECT guild_id FROM anilist_feed_mutes
UNION SELECT guild_id FROM anilist_channel_subs
UNION SELECT guild_id FROM dashboard_actions WHERE guild_id IS NOT NULL
UNION SELECT guild_id FROM dashboard_audit
UNION SELECT guild_id FROM server_stats_messages
UNION SELECT guild_id FROM server_stats_days
UNION SELECT guild_id FROM serverstats_digest_state
UNION
SELECT (extra->>'guild_id')::bigint FROM timers
WHERE extra->>'guild_id' ~ '^[0-9]+$'
"""


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
    return affected_rows(status) > 0


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
    return affected_rows(status)


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
                counts[table] = affected_rows(status)

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


async def prune_user_scoped_actions(pool, max_age_days=USER_ACTION_AUDIT_DAYS):
    """Delete TERMINAL user-scoped queue rows older than the audit window.

    The user side of ``dashboard_actions``. Guild rows die with their guild
    (``GUILD_DELETE_QUERIES``); user rows carry ``guild_id NULL``, so that purge
    cannot see them by construction and nothing else deletes them - they are
    personal data (who asked for their own export, when, and how it ended) that
    would otherwise be kept for good.

    Only ``done``/``failed`` rows are pruned: a ``pending``/``running`` row is
    live work, and the queue's own boot reconciliation is what resolves a stale
    one. ``updated_at`` is the terminal-write time, so the window starts when the
    action ENDED, not when it was queued.
    """
    status = await pool.execute(
        "DELETE FROM dashboard_actions "
        "WHERE user_id IS NOT NULL "
        "AND status IN ('done', 'failed') "
        "AND updated_at < now() - $1 * INTERVAL '1 day'",
        max_age_days,
    )
    return affected_rows(status)


async def prune_expired_export_slots(
    pool, window=privacy.EXPORT_COOLDOWN_SECONDS
):
    """Delete ``mydata_export_cooldown`` rows whose window has already elapsed.

    The limiter keeps one timestamp per user who ever exported, and that
    timestamp means nothing once the window has passed: ``claim_export_slot``
    grants such a user immediately, whether the row is there or absent. Deleting
    it therefore CANNOT weaken the limiter (the deleted rows are exactly the
    grantable ones), while leaving it there would keep "this person exported
    their data once, on this date" for ever - data minimisation on a table that
    otherwise only grows.

    The window is ``privacy.EXPORT_COOLDOWN_SECONDS`` itself rather than a copy,
    so the prune cannot drift ahead of the limiter it serves and start eating
    rows that are still enforcing something.
    """
    status = await pool.execute(
        "DELETE FROM mydata_export_cooldown "
        "WHERE last_export_at < now() - $1 * INTERVAL '1 second'",
        window,
    )
    return affected_rows(status)


async def prune_stale_dashboard_audit(
    pool,
    max_age_days=DASHBOARD_AUDIT_MAX_AGE_DAYS,
    batch_size=DASHBOARD_AUDIT_PRUNE_BATCH_SIZE,
    max_batches=DASHBOARD_AUDIT_PRUNE_MAX_BATCHES,
):
    """Delete dashboard journal rows older than the retention window.

    THE GAP THIS CLOSES. Every row of ``dashboard_audit`` is guild data
    (``guild_id NOT NULL``), so the guild purge above covers a DEPARTURE - but a
    guild that never leaves is the normal case, and for it nothing else ever
    deletes a row. The bot does not write this table at all (the dashboard does),
    so there is no writer-side prune either: without this pass the journal is an
    unbounded, permanent record of every configuration click an admin ever made.
    Guild-scoped storage is not the same promise as bounded storage, and this is
    the half the purge cannot make.

    NOT A USER PATH. The window ages rows out on their own age, never on who
    wrote them: the actor cannot erase their trail early (see the note in
    schema.sql and the `cases` precedent it follows), and no user erasure list
    reaches this table.

    DRAINS, NOT ONE BATCH. The journal is an event stream - one row per config
    action across every guild - so its daily inflow past the window is the whole
    fleet's write rate, not a handful of rows. A single bounded delete per tick
    would cap the pass at ``batch_size`` rows a day and, above that many
    fleet-wide writes, the table would grow without bound: the very failure this
    prune exists to prevent. So the pass LOOPS the bounded delete until a batch
    comes back short (the backlog is drained), the same drain shape the avatar
    and serverstats prunes use. ``max_batches`` caps the loop so a first run over
    a journal nobody has ever pruned - millions of rows - cannot take an
    unbounded lock in one tick; if the cap is reached the table is still behind,
    which is logged, and the remainder is taken on the next daily tick.

    OLDEST FIRST. Each batch's LIMITed ``ctid`` sub-select is ``ORDER BY
    created_at``, so it deletes the OLDEST rows, not an arbitrary tail - the same
    oldest-first drain ``prune_stale_presence_aggregates`` does with ``ORDER BY
    last_refresh``. Without the ordering the retention floor would be "90 days
    plus an unpredictable tail of whatever the scan happened to reach".

    BOUNDED PER BATCH by the LIMITed ``ctid`` sub-select, the same shape as
    cogs/community/serverstats/queries.PRUNE and cogs/system/usage_stats.PRUNE:
    it runs as an InitPlan (verified on the 11.22 dev server, where the outer
    half is a filtered scan rather than the Tid Scan 14+ gives - the LIMIT bounds
    the work either way), so the number of rows deleted - and therefore locked -
    is capped at ``batch_size`` per statement whatever state the table is in, and
    no single batch can take a wide lock for minutes. The scan runs once a day
    over a table the window itself keeps small.
    """
    deleted = 0
    for _batch in range(max_batches):
        status = await pool.execute(
            "DELETE FROM dashboard_audit "
            "WHERE ctid = ANY(ARRAY("
            "SELECT ctid FROM dashboard_audit "
            "WHERE created_at < now() - $1 * INTERVAL '1 day' "
            "ORDER BY created_at "
            "LIMIT $2"
            "))",
            max_age_days,
            batch_size,
        )
        batch = affected_rows(status)
        deleted += batch
        if batch < batch_size:
            break
    else:
        log.warning(
            "dashboard_audit prune hit its per-pass cap of %s batches x %s rows "
            "and is still behind; the remaining backlog will be drained on the "
            "next daily tick",
            max_batches,
            batch_size,
        )
    return deleted


async def prune_stale_presence_aggregates(
    pool,
    max_age_days=PRESENCE_AGGREGATE_MAX_AGE_DAYS,
    batch_size=PRESENCE_PRUNE_BATCH_SIZE,
):
    """Empty ``presence_gaming`` payloads nobody has added to for the window.

    THE GAP THIS CLOSES. The 30 days presence promises were a DISPLAY window,
    not a storage one, and only half of that was even a promise about the data:
    ``merge_games`` drops stale entries of a row it is already writing, and the
    renderer filters what it draws. Both only fire for a member who is still
    being seen. Someone who opted in, played for a week and then stopped - or
    left, or simply fell out of the member cache - is never flushed again, so
    their row sat at its last state for ever while the card politely showed
    nothing. The card said 30 days; the database said "always".

    THE PREDICATE IS ``last_refresh``, deliberately, and not a scan into the
    JSON for the newest ``last_played``. Every write to this row goes through
    ``connectors.storage.set_payload``, which stamps ``last_refresh = now()``,
    and the only writer for this connector is the presence flush - so a row
    untouched for the window CANNOT contain a play newer than the window
    (``last_played`` is stamped at the flush that wrote it, which is
    ``last_refresh`` at the latest). Reading the timestamps out of the payload
    would mean casting user-reachable text to ``timestamptz`` inside a DELETE,
    where one malformed value aborts the whole pass. This way the pass cannot
    remove anything the card would still be drawing, and it needs no cast at
    all. It is also the query the ``(connector, last_refresh)`` index in
    schema.sql already exists to serve.

    THE ROW SURVIVES; only its payload is emptied. The row IS the opt-in (see
    presence.py: no row, no collection, no section), so deleting it would turn a
    retention pass into a silent withdrawal of somebody's consent - the exact
    inversion of what this is for. Emptying the aggregate is the whole of what
    the promise requires.

    Bounded per pass (``batch_size``) and self-limiting: an emptied row no
    longer matches ``payload <> '{}'``, so a pass that finds nothing new does no
    work, and no row is ever rewritten twice.
    """
    status = await pool.execute(
        "WITH stale AS ("
        "SELECT user_id FROM profile_connections "
        "WHERE connector = 'presence_gaming' "
        "AND last_refresh IS NOT NULL "
        "AND last_refresh < now() - $1 * INTERVAL '1 day' "
        "AND payload <> '{}'::jsonb "
        "ORDER BY last_refresh LIMIT $2"
        ") UPDATE profile_connections AS connections "
        "SET payload = '{}'::jsonb FROM stale "
        "WHERE connections.user_id = stale.user_id "
        "AND connections.connector = 'presence_gaming'",
        max_age_days,
        batch_size,
    )
    return affected_rows(status)


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
