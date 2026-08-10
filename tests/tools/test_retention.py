import datetime
import inspect
import os
import re
import types

from tools import privacy, retention

# ---------------------------------------------------------------------------
# Structural guard: schema.sql is the source of truth for "what is guild data"
# ---------------------------------------------------------------------------
#
# The house lesson, encoded: a new guild-scoped table is added to schema.sql by
# one lot and forgotten by the purge list in ANOTHER file (season_podiums did
# exactly that). Reviewing every future table by hand is not a control, so the
# test below derives the expectation from the schema itself - every table with
# a guild_id column must be purged, or be on the short exemption list here with
# a justification that was verified in the code.

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
)

# Tables that carry a guild_id and are DELIBERATELY not in GUILD_DELETE_QUERIES.
# Adding an entry here is a claim about the code, so each one names where it was
# checked. Keep this list tiny: "it is not really guild data" is almost never
# true of a column literally named guild_id.
_PURGE_EXEMPT_TABLES = {
    # The purge JOB row itself, not guild content. purge_claimed_guild locks it
    # FOR UPDATE and deletes it explicitly at the END of the same transaction
    # (tools/retention.py), after the loop that deletes what it authorises;
    # inside the loop it would delete the very row being held.
    "guild_retention_jobs",
    # Ephemeral cache of LIVE AniList coalescing cards (message ids of a card
    # still being edited in place), not stored guild data - and self-expiring:
    # cogs/anilist/feed.py's _prune_coalesce_posts deletes every row whose last
    # edit is older than AGE_CAP + PRUNE_GRACE, i.e. hours, far inside the
    # 30-day purge grace. A departed guild's rows are long gone before the
    # purge job even becomes due.
    "anilist_feed_posts",
}


def _guild_scoped_tables():
    """Every table in schema.sql that has a ``guild_id`` column.

    Deliberately a small, dumb parser over the DDL rather than a live DB
    introspection: this must run in the offline suite, and schema.sql IS the
    source of truth (it is applied verbatim at startup). Comments are stripped
    first, then both ways a column can appear are collected - inside a CREATE
    TABLE body and via a later ADD COLUMN migration.
    """
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        ddl = re.sub(r"--[^\n]*", "", handle.read())

    tables = set()
    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\s*\)\s*;",
        ddl,
        re.S | re.I,
    ):
        if re.search(r"^\s*guild_id\b", match.group(2), re.M | re.I):
            tables.add(match.group(1))
    for match in re.finditer(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?guild_id\b",
        ddl,
        re.I,
    ):
        tables.add(match.group(1))
    return tables


def test_every_guild_scoped_table_is_purged_or_explicitly_exempt():
    tables = _guild_scoped_tables()
    # Sanity-check the parser itself before trusting its verdict: if it stopped
    # seeing tables, "nothing is missing" would be a false pass.
    assert {"levels", "xp_period", "season_podiums", "cases"} <= tables
    assert len(tables) > 25

    purged = {table for table, _query in retention.GUILD_DELETE_QUERIES}
    assert sorted(tables - purged - _PURGE_EXEMPT_TABLES) == []


def test_purge_exemptions_still_name_real_guild_scoped_tables():
    """A dropped or renamed table must not leave a silent exemption behind."""
    assert _PURGE_EXEMPT_TABLES <= _guild_scoped_tables()


def test_guild_purge_covers_support_tickets():
    """Ticket rows are metadata, but they still say who asked THIS server for
    help and when, so they die with the guild like every other guild record -
    and the guild has to be discoverable from them, or an orphaned ticket table
    would keep a departed guild's rows for ever with nothing scheduling them."""
    assert (
        dict(retention.GUILD_DELETE_QUERIES)["tickets"]
        == "DELETE FROM tickets WHERE guild_id = $1"
    )
    assert "FROM tickets" in retention.STORED_GUILD_IDS_QUERY


def test_guild_purge_covers_the_season_podiums():
    """Season podiums are the one leveling artefact that outlives the xp_period
    prune, so a departed guild's are the one thing retention could forget."""
    assert "season_podiums" in dict(retention.GUILD_DELETE_QUERIES)
    assert "FROM season_podiums" in retention.STORED_GUILD_IDS_QUERY


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, job=True):
        self.job = job
        self.calls = []

    def transaction(self):
        return _Context(self)

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "guild_retention_jobs" in query and self.job:
            return {"guild_id": args[0]}
        return None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "DELETE 1"


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Context(self.connection)


async def test_schedule_guild_purge_uses_thirty_day_grace(fake_pool):
    left_at = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)

    purge_after = await retention.schedule_guild_purge(
        fake_pool, 42, left_at=left_at
    )

    assert purge_after == left_at + datetime.timedelta(days=30)
    _method, query, args = fake_pool.calls[0]
    assert "ON CONFLICT (guild_id)" in query
    assert args == (42, left_at, purge_after)


async def test_avatar_prune_query_pins_approved_policy(fake_pool):
    fake_pool.fetch_return = [{"bytes": 10}, {"bytes": 20}]

    count, size = await retention.prune_avatar_history_batch(
        fake_pool, batch_size=17
    )

    assert (count, size) == (2, 30)
    _method, query, args = fake_pool.calls[0]
    assert "make_interval(months => $4)" in query
    assert "PARTITION BY user_id, kind, guild_id" in query
    assert args == (30, 5, 17, 18)


def test_guild_purge_excludes_user_reminders():
    # Reminders are user-owned; a departed guild must not collaterally delete
    # them. Undeliverable ones die at fire time via the NotFound terminal ack.
    timers_query = dict(retention.GUILD_DELETE_QUERIES)["timers"]
    assert "event <> 'reminder'" in timers_query


async def test_list_guild_jobs_orders_by_due(fake_pool):
    fake_pool.fetch_return = [{"guild_id": 1}]

    rows = await retention.list_guild_jobs(fake_pool, limit=25)

    assert rows == [{"guild_id": 1}]
    _method, query, args = fake_pool.calls[0]
    assert "FROM guild_retention_jobs" in query
    assert "ORDER BY purge_after, guild_id" in query
    assert args == (25,)


async def test_failed_claim_is_delayed_before_retry(fake_pool):
    await retention.release_guild_claim(
        fake_pool, 42, RuntimeError("temporary failure")
    )

    _method, query, args = fake_pool.calls[0]
    assert "interval '1 hour'" in query
    assert args == (42, "temporary failure")


async def test_reconcile_schedules_only_orphaned_guilds():
    class _ReconcilePool:
        def __init__(self):
            self.calls = []

        async def fetch(self, query):
            assert query == retention.STORED_GUILD_IDS_QUERY
            return [
                {"guild_id": 1},
                {"guild_id": 2},
                {"guild_id": 3},
            ]

        async def execute(self, query, *args):
            self.calls.append((query, args))
            return "DELETE 1" if query.startswith("DELETE") else "INSERT 0 2"

    pool = _ReconcilePool()

    scheduled = await retention.reconcile_guild_jobs(pool, {2})

    assert scheduled == 2
    assert pool.calls[0][1] == ([2],)
    assert pool.calls[1][1] == ([1, 3], 30)
    assert "ON CONFLICT (guild_id) DO NOTHING" in pool.calls[1][0]


async def test_guild_purge_is_transactional_scoped_and_excludes_global_tables():
    connection = _Connection()

    counts = await retention.purge_claimed_guild(_Pool(connection), 987)

    assert set(counts) == {
        table for table, _query in retention.GUILD_DELETE_QUERIES
    }
    delete_calls = [
        (query, args)
        for method, query, args in connection.calls
        if method == "execute"
    ]
    for query, args in delete_calls:
        assert args == (987,)
        assert "WHERE" in query

    combined = "\n".join(query for query, _args in delete_calls)
    for global_table in (
        "user_settings",
        "profiles",
        "music_favorites",
        "anilist_tokens",
        "anilist_airing_optins",
        "anilist_chapter_optins",
        "afk",
        "blbot",
    ):
        assert f"DELETE FROM {global_table}" not in combined


async def test_guild_purge_missing_or_cancelled_job_deletes_nothing():
    connection = _Connection(job=False)

    result = await retention.purge_claimed_guild(_Pool(connection), 987)

    assert result is None
    assert not [
        call
        for call in connection.calls
        if call[0] == "execute"
    ]


def test_invalidate_guild_caches_clears_primary_bot_maps(monkeypatch):
    bot = types.SimpleNamespace(
        prefixes={1: "!", 2: "?"},
        autoroles={1: 10, 2: 20},
        muteroles={1: 11, 2: 21},
        get_cog=lambda _name: None,
    )
    invalidated = []
    monkeypatch.setattr(
        retention.settings, "invalidate_guild", invalidated.append
    )

    retention.invalidate_guild_caches(bot, 1)

    assert bot.prefixes == {2: "?"}
    assert bot.autoroles == {2: 20}
    assert bot.muteroles == {2: 21}
    assert invalidated == [1]


def test_the_guild_purge_leaves_user_scoped_queue_rows_alone():
    """The action queue now holds BOTH guild-scoped and user-scoped rows.

    A user row carries ``guild_id NULL``, so ``WHERE guild_id = $1`` can never
    match it - a guild departure must not collaterally erase a member's own
    ``mydata_export`` history, exactly like the timers carve-out above. The
    discovery UNION is guarded the same way (a NULL guild id is not a guild).
    """
    query = dict(retention.GUILD_DELETE_QUERIES)["dashboard_actions"]
    assert query == "DELETE FROM dashboard_actions WHERE guild_id = $1"
    assert (
        "SELECT guild_id FROM dashboard_actions WHERE guild_id IS NOT NULL"
        in retention.STORED_GUILD_IDS_QUERY
    )


# ---------------------------------------------------------------------------
# The USER side of the lifecycle: rows no guild purge can reach
# ---------------------------------------------------------------------------


class _PrunePool:
    """Records the single statement a prune runs and replays a row count."""

    def __init__(self, status="DELETE 3"):
        self.status = status
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return self.status


async def test_terminal_user_scoped_queue_rows_are_aged_out():
    """Nothing else deletes them: the guild purge cannot see a row with
    ``guild_id NULL``, and the user has no delete surface for the queue. Without
    this prune, "so-and-so exported their data on this date" is kept for ever."""
    pool = _PrunePool()

    deleted = await retention.prune_user_scoped_actions(pool)

    assert deleted == 3
    query, args = pool.calls[0]
    assert args == (retention.USER_ACTION_AUDIT_DAYS,)
    assert "DELETE FROM dashboard_actions" in query
    # The user rows ONLY: a guild row is the guild's, and dies with it.
    assert "user_id IS NOT NULL" in query
    # ... and only the TERMINAL ones: a pending/running row is live work that the
    # queue's own boot reconciliation resolves.
    assert "status IN ('done', 'failed')" in query
    # Aged on the terminal write, not on the request time.
    assert "updated_at < now() - $1 * INTERVAL '1 day'" in query


async def test_elapsed_export_slots_are_pruned_with_the_limiters_own_window():
    """A row older than the window grants on sight, so deleting it cannot hand
    anybody an export they owed a wait for. Taking the window from the limiter
    itself is what stops the prune drifting ahead of it and eating rows that are
    still enforcing something."""
    pool = _PrunePool(status="DELETE 12")

    deleted = await retention.prune_expired_export_slots(pool)

    assert deleted == 12
    query, args = pool.calls[0]
    assert args == (privacy.EXPORT_COOLDOWN_SECONDS,)
    assert "DELETE FROM mydata_export_cooldown" in query
    assert "last_export_at < now() - $1 * INTERVAL '1 second'" in query


async def test_the_export_slot_prune_can_never_shorten_a_live_window():
    """The property behind the previous test, stated as arithmetic rather than
    as a string: the deleted set is exactly the set the claim already grants."""
    assert (
        inspect.signature(retention.prune_expired_export_slots)
        .parameters["window"]
        .default
        == privacy.EXPORT_COOLDOWN_SECONDS
    )


# ---------------------------------------------------------------------------
# The presence aggregates: making a DISPLAY window a STORAGE window
# ---------------------------------------------------------------------------
#
# presence.PURGE_AFTER_DAYS was enforced in two places and neither of them was
# storage: the merge drops stale entries of a row it is already writing, and
# the renderer filters what it draws. Both only ever fire for a member who is
# still being seen - so somebody who opted in, played for a week and stopped
# kept their aggregate for ever while the card politely showed nothing. The
# card said 30 days; the table said always.


async def test_presence_aggregates_stop_being_kept_past_their_own_window():
    pool = _PrunePool(status="UPDATE 7")

    emptied = await retention.prune_stale_presence_aggregates(pool)

    assert emptied == 7
    query, args = pool.calls[0]
    assert args == (
        retention.PRESENCE_AGGREGATE_MAX_AGE_DAYS,
        retention.PRESENCE_PRUNE_BATCH_SIZE,
    )
    assert "connector = 'presence_gaming'" in query
    assert "last_refresh < now() - $1 * INTERVAL '1 day'" in query
    # Bounded per pass, like every other statement on the daily tick.
    assert "LIMIT $2" in query


def test_the_presence_prune_empties_the_payload_and_keeps_the_opt_in():
    """The row IS the consent (presence.py: no row, no collection, no section).

    Deleting it would turn a retention pass into a silent withdrawal of
    somebody's consent, which is the exact inversion of what the pass is for.
    Only the aggregate goes.
    """
    query = inspect.getsource(retention.prune_stale_presence_aggregates)
    assert "SET payload = '{}'::jsonb" in query
    assert "DELETE FROM profile_connections" not in query
    # ... and it is self-limiting: an emptied row no longer matches, so no row
    # is ever rewritten twice and a quiet pass does no work at all.
    assert "payload <> '{}'::jsonb" in query


def test_the_presence_prune_never_reaches_a_row_the_card_would_still_draw():
    """Why the predicate is ``last_refresh`` and not the JSON timestamps.

    Every write to this row goes through connectors.storage.set_payload, which
    stamps ``last_refresh = now()``, and the presence flush is its only writer
    for this connector - so a row untouched for the window cannot hold a play
    NEWER than the window. Reading the stamps out of the payload would instead
    mean casting user-reachable text to timestamptz inside the statement, where
    one malformed value aborts the whole pass.
    """
    source = inspect.getsource(retention.prune_stale_presence_aggregates)
    assert "::timestamptz" not in source
    assert "jsonb_array_elements" not in source
    # A row the flush never wrote has no aggregate to empty and no age to
    # measure, so it is left alone rather than aged from its opt-in date.
    assert "last_refresh IS NOT NULL" in source


def test_the_prune_window_is_the_one_the_feature_and_the_policy_state():
    """Restated, never forked. tools/ cannot import a cog (the import runs the
    other way), so the number lives in both files - and this is what makes the
    copy a copy instead of a second opinion."""
    from cogs.community.profile import presence

    assert (
        retention.PRESENCE_AGGREGATE_MAX_AGE_DAYS == presence.PURGE_AFTER_DAYS == 30
    )

    policy = open(
        os.path.join(os.path.dirname(_SCHEMA_PATH), "PRIVACY.md"), encoding="utf-8"
    ).read()
    assert "Presence aggregates: 30 days" in policy
