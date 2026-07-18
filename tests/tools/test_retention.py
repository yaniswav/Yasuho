import datetime
import types

from tools import retention


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
