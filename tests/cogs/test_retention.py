import types

from cogs.system import retention as retention_cog


def _cog(bot):
    cog = object.__new__(retention_cog.DataRetention)
    cog.bot = bot
    return cog


def _gated_cog(*, ready, guilds, paused=False, last_healthy=None):
    """A cog wired for the maintenance health-gate tests (no __init__/loop)."""
    bot = types.SimpleNamespace(
        db_pool=object(),
        is_ready=lambda: ready,
        guilds=list(guilds),
    )
    cog = _cog(bot)
    cog._paused = paused
    cog._last_healthy_guild_count = last_healthy
    return cog


async def _run_maintenance_tick(cog):
    """Drive one maintenance tick, recording whether run_once fired."""
    ran = []

    async def run_once():
        ran.append(True)
        return {}

    cog.run_once = run_once
    await retention_cog.DataRetention.maintenance.coro(cog)
    return ran


async def test_maintenance_skips_when_not_ready():
    cog = _gated_cog(ready=False, guilds=[object()])
    assert await _run_maintenance_tick(cog) == []
    assert cog._last_healthy_guild_count is None


async def test_maintenance_skips_when_no_guilds():
    cog = _gated_cog(ready=True, guilds=[])
    assert await _run_maintenance_tick(cog) == []
    assert cog._last_healthy_guild_count is None


async def test_maintenance_skips_on_guild_count_collapse():
    # A reconnect that transiently reports 10 of 100 guilds must not act.
    cog = _gated_cog(ready=True, guilds=[object()] * 10, last_healthy=100)
    assert await _run_maintenance_tick(cog) == []
    assert cog._last_healthy_guild_count == 100  # baseline unchanged


async def test_maintenance_runs_when_healthy_and_records_count():
    cog = _gated_cog(ready=True, guilds=[object()] * 4)
    assert await _run_maintenance_tick(cog) == [True]
    assert cog._last_healthy_guild_count == 4


async def test_maintenance_skips_when_paused():
    cog = _gated_cog(ready=True, guilds=[object()] * 4, paused=True)
    assert await _run_maintenance_tick(cog) == []


async def test_active_guild_cancels_due_purge(monkeypatch):
    jobs = iter([{"guild_id": 42}, None])
    cancelled = []
    purged = []

    async def claim(_pool):
        return next(jobs)

    async def cancel(_pool, guild_id):
        cancelled.append(guild_id)

    async def purge(_pool, guild_id):
        purged.append(guild_id)

    async def prune(_pool):
        return 0, 0

    async def reconcile(_pool, _active):
        return 0

    monkeypatch.setattr(
        retention_cog.retention, "reconcile_guild_jobs", reconcile
    )
    monkeypatch.setattr(retention_cog.retention, "claim_due_guild", claim)
    monkeypatch.setattr(retention_cog.retention, "cancel_guild_purge", cancel)
    monkeypatch.setattr(retention_cog.retention, "purge_claimed_guild", purge)
    monkeypatch.setattr(
        retention_cog.retention, "prune_avatar_history_batch", prune
    )
    bot = types.SimpleNamespace(
        db_pool=object(),
        get_guild=lambda guild_id: object() if guild_id == 42 else None,
    )

    result = await _cog(bot).run_once()

    assert cancelled == [42]
    assert purged == []
    assert result["guilds"] == 0


async def test_due_departed_guild_is_purged_and_cache_invalidated(monkeypatch):
    jobs = iter([{"guild_id": 42}, None])
    invalidated = []

    async def claim(_pool):
        return next(jobs)

    async def purge(_pool, guild_id):
        return {"levels": 3, "cases": 2}

    async def prune(_pool):
        return 0, 0

    async def reconcile(_pool, _active):
        return 0

    monkeypatch.setattr(
        retention_cog.retention, "reconcile_guild_jobs", reconcile
    )
    monkeypatch.setattr(retention_cog.retention, "claim_due_guild", claim)
    monkeypatch.setattr(retention_cog.retention, "purge_claimed_guild", purge)
    monkeypatch.setattr(
        retention_cog.retention,
        "invalidate_guild_caches",
        lambda _bot, guild_id: invalidated.append(guild_id),
    )
    monkeypatch.setattr(
        retention_cog.retention, "prune_avatar_history_batch", prune
    )
    bot = types.SimpleNamespace(
        db_pool=object(),
        get_guild=lambda _guild_id: None,
    )

    result = await _cog(bot).run_once()

    assert invalidated == [42]
    assert result["guilds"] == 1


async def test_failed_guild_purge_releases_claim_for_retry(monkeypatch):
    jobs = iter([{"guild_id": 42}, None])
    released = []

    async def claim(_pool):
        return next(jobs)

    async def purge(_pool, _guild_id):
        raise RuntimeError("database unavailable")

    async def release(_pool, guild_id, error):
        released.append((guild_id, str(error)))

    async def prune(_pool):
        return 0, 0

    async def reconcile(_pool, _active):
        return 0

    monkeypatch.setattr(
        retention_cog.retention, "reconcile_guild_jobs", reconcile
    )
    monkeypatch.setattr(retention_cog.retention, "claim_due_guild", claim)
    monkeypatch.setattr(retention_cog.retention, "purge_claimed_guild", purge)
    monkeypatch.setattr(retention_cog.retention, "release_guild_claim", release)
    monkeypatch.setattr(
        retention_cog.retention, "prune_avatar_history_batch", prune
    )
    bot = types.SimpleNamespace(
        db_pool=object(),
        get_guild=lambda _guild_id: None,
    )

    result = await _cog(bot).run_once()

    assert released == [(42, "database unavailable")]
    assert result["guilds"] == 0


async def test_avatar_cleanup_stops_after_short_batch(monkeypatch):
    jobs = iter([None])
    batches = iter([(250, 1000), (3, 20)])

    async def claim(_pool):
        return next(jobs)

    async def prune(_pool):
        return next(batches)

    async def reconcile(_pool, _active):
        return 0

    monkeypatch.setattr(
        retention_cog.retention, "reconcile_guild_jobs", reconcile
    )
    monkeypatch.setattr(retention_cog.retention, "claim_due_guild", claim)
    monkeypatch.setattr(
        retention_cog.retention, "prune_avatar_history_batch", prune
    )
    bot = types.SimpleNamespace(
        db_pool=object(),
        get_guild=lambda _guild_id: None,
    )

    result = await _cog(bot).run_once()

    assert result == {
        "scheduled_guilds": 0,
        "guilds": 0,
        "avatar_rows": 253,
        "avatar_bytes": 1020,
    }
