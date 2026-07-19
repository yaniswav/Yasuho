"""Unit tests for the dashboard->bot cache-sync cog (``cogs.system.dashboard_sync``).

These exercise the PURE invalidation dispatch - the part that turns a Postgres
NOTIFY payload into an update of the SAME in-memory structure the bot's own
commands mutate - with in-memory stand-ins for the only boundaries: a fake bot
exposing the ``prefixes`` / ``autoroles`` / ``muteroles`` caches (as the real bot
does), a fake asyncpg pool returning a row or ``None`` per table, and a fake
ModLog cog. The network / LISTEN connection and the reconnect supervisor are NOT
exercised here (they touch a real socket); only the dispatch is, which is where
all the cache-coherence logic lives.

Runs on the 3.7 box against discord.py 1.5.1: the cog module imports cleanly
there (``from discord.ext import commands`` + a ``commands.Cog`` subclass), and
the dispatch path never touches any 2.x-only Discord API, so it is fully
runnable here as well as on the 3.12 target.
"""

from __future__ import annotations

import pytest

from cogs.system import dashboard_sync
from tools import settings

# ---------------------------------------------------------------------------
# In-memory fakes (mirror the real bot's caches / pool shape).
# ---------------------------------------------------------------------------


class SyncPool:
    """Fake pool: fetchval returns the seeded value for the queried table;
    fetchrow returns the seeded ``starboard`` row (as a dict, ``row["col"]``
    style, like an asyncpg Record) or ``None``."""

    def __init__(self):
        self.calls = []
        self.prefixes = {}
        self.autorole = {}
        self.muterole = {}
        # gid -> (channel_id, threshold); absent => no starboard row.
        self.starboard = {}
        # gid -> {"antilink": bool, "antispam": bool}; absent => no automod row.
        self.automod = {}
        # Leveling stores read by the Leveling cog's refresh hooks (below).
        # gid -> dict row (Record-like) or absent => no level_config row.
        self.level_config = {}
        # gid -> list of {"kind", "target_id"} rows; absent => no rows.
        self.level_no_xp = {}
        # gid -> list of {"kind", "target_id", "factor"} rows; absent => no rows.
        self.xp_multipliers = {}

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        gid = args[0]
        if "FROM prefixes" in query:
            return self.prefixes.get(gid)
        if "FROM autorole" in query:
            return self.autorole.get(gid)
        if "FROM muterole" in query:
            return self.muterole.get(gid)
        raise AssertionError(f"unexpected fetchval: {query!r}")  # pragma: no cover

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        gid = args[0]
        if "FROM starboard" in query:
            cfg = self.starboard.get(gid)
            if cfg is None:
                return None
            channel_id, threshold = cfg
            return {"channel_id": channel_id, "threshold": threshold}
        if "FROM automod" in query:
            # dict (Record-like) or None, exactly as get_settings caches it.
            return self.automod.get(gid)
        if "FROM level_config" in query:
            return self.level_config.get(gid)
        raise AssertionError(f"unexpected fetchrow: {query!r}")  # pragma: no cover

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        gid = args[0]
        if "FROM level_no_xp" in query:
            return self.level_no_xp.get(gid, [])
        if "FROM xp_multipliers" in query:
            return self.xp_multipliers.get(gid, [])
        raise AssertionError(f"unexpected fetch: {query!r}")  # pragma: no cover


class FakeModLog:
    """Stand-in for the ModLog cog exposing only its ``_channels`` cache."""

    def __init__(self):
        self._channels = {}


class FakeStarboard:
    """Stand-in for the Starboard cog exposing only its ``_config`` cache.

    The real cog's ``_config`` is a NEGATIVE cache: ``(channel_id, threshold)``
    when configured, ``None`` when looked-up-and-empty.
    """

    def __init__(self):
        self._config = {}


class FakeAutoMod:
    """Stand-in for the AutoMod cog exposing only its ``_settings`` cache.

    The real cog's ``_settings`` is a NEGATIVE cache: the fetched ``automod``-table
    Record when configured, ``None`` when looked-up-and-empty (get_settings).
    """

    def __init__(self):
        self._settings = {}


class FakeLeveling:
    """Stand-in for the Leveling cog exposing its three refresh hooks + caches.

    The real cog keeps ``self._configs`` (a plain dict of LevelConfig), plus
    ``self._no_xp`` and ``self._multipliers`` (BoundedLRUs of snapshots); each is
    refreshed by the SAME public method ``level_config_ui.py`` calls after a
    write. Here each hook RE-READS from the fake pool and updates its cache (or
    pops it when the config row is gone), so the dispatch test proves the
    invalidator refreshes all three from the DB.
    """

    def __init__(self, pool):
        self._pool = pool
        self._configs = {}
        self._no_xp = {}
        self._multipliers = {}

    async def refresh_guild_config(self, gid):
        row = await self._pool.fetchrow(
            "SELECT enabled FROM level_config WHERE guild_id = $1", gid
        )
        if row is not None:
            self._configs[gid] = row
        else:
            self._configs.pop(gid, None)

    async def refresh_no_xp_snapshot(self, gid):
        rows = await self._pool.fetch(
            "SELECT kind, target_id FROM level_no_xp WHERE guild_id = $1", gid
        )
        snapshot = frozenset((r["kind"], r["target_id"]) for r in rows)
        self._no_xp[gid] = snapshot
        return snapshot

    async def refresh_multiplier_snapshot(self, gid):
        rows = await self._pool.fetch(
            "SELECT kind, target_id, factor FROM xp_multipliers WHERE guild_id = $1",
            gid,
        )
        snapshot = tuple((r["kind"], r["target_id"], r["factor"]) for r in rows)
        self._multipliers[gid] = snapshot
        return snapshot


class FakeBot:
    def __init__(self, pool, modlog=None, starboard=None, automod=None, leveling=None):
        self.db_pool = pool
        self.prefixes = {}
        self.autoroles = {}
        self.muteroles = {}
        self._cogs = {}
        if modlog is not None:
            self._cogs["ModLog"] = modlog
        if starboard is not None:
            self._cogs["Starboard"] = starboard
        if automod is not None:
            self._cogs["AutoMod"] = automod
        if leveling is not None:
            self._cogs["Leveling"] = leveling

    def get_cog(self, name):
        return self._cogs.get(name)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The tools.settings LRU is process-global; keep it from leaking across tests."""
    settings._cache.clear()
    yield
    settings._cache.clear()


def _payload(kind, guild_id):
    import json

    return json.dumps({"kind": kind, "guildId": str(guild_id)})


# ---------------------------------------------------------------------------
# _parse_payload: defensive parsing.
# ---------------------------------------------------------------------------


def test_parse_valid_numeric_string_guild_id():
    assert dashboard_sync._parse_payload(_payload("prefix", 100)) == ("prefix", 100)


def test_parse_valid_int_guild_id():
    import json

    assert dashboard_sync._parse_payload(
        json.dumps({"kind": "modlog", "guildId": 42})
    ) == ("modlog", 42)


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "",
        "[1, 2, 3]",  # valid JSON but not an object
        "42",  # valid JSON but not an object
        '{"kind": "prefix"}',  # missing guildId
        '{"guildId": "100"}',  # missing kind
        '{"kind": "unknown", "guildId": "100"}',  # unknown kind
        '{"kind": "prefix", "guildId": "abc"}',  # non-numeric guild id
        '{"kind": "prefix", "guildId": null}',  # null guild id
        None,  # not even a string
        123,  # not a string
    ],
)
def test_parse_rejects_bad_payloads(payload):
    assert dashboard_sync._parse_payload(payload) is None


# ---------------------------------------------------------------------------
# dispatch: prefix / autorole / muterole re-read + set-or-pop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, cache_attr, pool_attr",
    [
        ("prefix", "prefixes", "prefixes"),
        ("autorole", "autoroles", "autorole"),
        ("muterole", "muteroles", "muterole"),
    ],
)
async def test_dispatch_sets_cache_from_db_row(kind, cache_attr, pool_attr):
    pool = SyncPool()
    bot = FakeBot(pool)
    # DB has an authoritative value for guild 100.
    getattr(pool, pool_attr)[100] = "yo!" if kind == "prefix" else 555

    handled = await dashboard_sync.dispatch(bot, _payload(kind, 100))

    assert handled == kind
    expected = "yo!" if kind == "prefix" else 555
    assert getattr(bot, cache_attr)[100] == expected


@pytest.mark.parametrize(
    "kind, cache_attr",
    [
        ("prefix", "prefixes"),
        ("autorole", "autoroles"),
        ("muterole", "muteroles"),
    ],
)
async def test_dispatch_pops_cache_when_db_empty(kind, cache_attr):
    pool = SyncPool()  # DB has no row for guild 100
    bot = FakeBot(pool)
    # Seed a stale in-memory value the dashboard just deleted from the DB.
    getattr(bot, cache_attr)[100] = "stale" if kind == "prefix" else 999

    handled = await dashboard_sync.dispatch(bot, _payload(kind, 100))

    assert handled == kind
    assert 100 not in getattr(bot, cache_attr)


# ---------------------------------------------------------------------------
# dispatch: modlog invalidation (pop the ModLog cog's _channels entry AND evict
# the settings LRU blob, since the dashboard also writes modlog_events there).
# ---------------------------------------------------------------------------


async def test_dispatch_modlog_pops_channels_entry():
    modlog = FakeModLog()
    modlog._channels[100] = 202  # negative/positive cached entry to evict
    bot = FakeBot(SyncPool(), modlog=modlog)

    handled = await dashboard_sync.dispatch(bot, _payload("modlog", 100))

    assert handled == "modlog"
    assert 100 not in modlog._channels


async def test_dispatch_modlog_evicts_settings_blob():
    # modlog_events lives in the guild_settings JSONB blob (served by the
    # tools.settings LRU, not the ModLog cog's _channels dict), so a
    # dashboard events-only change must evict it too or it never applies.
    modlog = FakeModLog()
    bot = FakeBot(SyncPool(), modlog=modlog)
    key = (settings._GUILD[0], 100)
    settings._cache[key] = {"modlog_events": ["join"]}

    handled = await dashboard_sync.dispatch(bot, _payload("modlog", 100))

    assert handled == "modlog"
    assert key not in settings._cache


async def test_dispatch_modlog_noop_without_cog():
    # No ModLog cog loaded: the _channels pop is skipped, but the settings
    # eviction is unconditional, and dispatch still reports handled.
    bot = FakeBot(SyncPool())
    key = (settings._GUILD[0], 100)
    settings._cache[key] = {"modlog_events": ["join"]}

    handled = await dashboard_sync.dispatch(bot, _payload("modlog", 100))

    assert handled == "modlog"
    assert key not in settings._cache


# ---------------------------------------------------------------------------
# dispatch: welcome invalidation (evict the settings LRU blob for the guild).
# ---------------------------------------------------------------------------


async def test_dispatch_welcome_evicts_settings_blob():
    bot = FakeBot(SyncPool())
    # Seed the process-global settings cache with a stale guild blob.
    key = (settings._GUILD[0], 100)
    settings._cache[key] = {"welcome": {"channel_id": 201, "enabled": True}}
    assert key in settings._cache

    handled = await dashboard_sync.dispatch(bot, _payload("welcome", 100))

    assert handled == "welcome"
    # Evicted: the next settings.get_guild would re-read the authoritative row.
    assert key not in settings._cache


# ---------------------------------------------------------------------------
# dispatch: starboard invalidation (refresh the Starboard cog's _config entry).
# ---------------------------------------------------------------------------


async def test_dispatch_starboard_sets_config_from_db_row():
    pool = SyncPool()
    pool.starboard[100] = (555, 7)  # authoritative (channel_id, threshold)
    sb = FakeStarboard()
    bot = FakeBot(pool, starboard=sb)

    handled = await dashboard_sync.dispatch(bot, _payload("starboard", 100))

    assert handled == "starboard"
    # Mirrors the cog's own _apply_set: the tuple lands in _config.
    assert sb._config[100] == (555, 7)


async def test_dispatch_starboard_sets_none_when_db_empty():
    # DB has no starboard row (a dashboard "disable" deleted it): the negative
    # cache must store None, exactly as the cog's starboard_disable does.
    pool = SyncPool()
    sb = FakeStarboard()
    sb._config[100] = (999, 3)  # stale value the dashboard just deleted
    bot = FakeBot(pool, starboard=sb)

    handled = await dashboard_sync.dispatch(bot, _payload("starboard", 100))

    assert handled == "starboard"
    assert sb._config[100] is None


async def test_dispatch_starboard_noop_without_cog():
    # No Starboard cog loaded: safe no-op, still reports handled.
    bot = FakeBot(SyncPool())
    handled = await dashboard_sync.dispatch(bot, _payload("starboard", 100))
    assert handled == "starboard"


# ---------------------------------------------------------------------------
# dispatch: automod invalidation (BOTH stores - the cog's _settings table cache
# AND the tools.settings LRU blob for the JSONB keys).
# ---------------------------------------------------------------------------


async def test_dispatch_automod_refreshes_both_stores():
    # DB has authoritative automod-table booleans for guild 100...
    pool = SyncPool()
    pool.automod[100] = {"antilink": True, "antispam": False}
    am = FakeAutoMod()
    am._settings[100] = {"antilink": False, "antispam": False}  # stale
    bot = FakeBot(pool, automod=am)
    # ...and a stale JSONB blob sits in the settings LRU (antiinvite/action/etc).
    key = (settings._GUILD[0], 100)
    settings._cache[key] = {"antiinvite": True, "automod_action": "kick"}

    handled = await dashboard_sync.dispatch(bot, _payload("automod", 100))

    assert handled == "automod"
    # Store 1: the cog's table cache is refreshed with the re-read row.
    assert am._settings[100] == {"antilink": True, "antispam": False}
    # Store 2: the JSONB blob is evicted so the next get_guild re-reads it.
    assert key not in settings._cache


async def test_dispatch_automod_sets_none_when_table_empty():
    # No automod row (dashboard saved both toggles off, which upserts a row -
    # but a guild that never had one reads None): the negative cache stores None,
    # exactly as get_settings does on a cold miss.
    pool = SyncPool()
    am = FakeAutoMod()
    am._settings[100] = {"antilink": True, "antispam": True}  # stale
    bot = FakeBot(pool, automod=am)

    handled = await dashboard_sync.dispatch(bot, _payload("automod", 100))

    assert handled == "automod"
    assert am._settings[100] is None


async def test_dispatch_automod_evicts_settings_blob_without_cog():
    # No AutoMod cog loaded: the JSONB eviction is UNCONDITIONAL (the blob is
    # cached independently of the cog object), and dispatch still reports handled.
    bot = FakeBot(SyncPool())
    key = (settings._GUILD[0], 100)
    settings._cache[key] = {"automod_exempt_roles": [1, 2, 3]}

    handled = await dashboard_sync.dispatch(bot, _payload("automod", 100))

    assert handled == "automod"
    assert key not in settings._cache


# ---------------------------------------------------------------------------
# dispatch: leveling invalidation (refresh ALL THREE Leveling cog caches via
# the cog's own public refresh hooks).
# ---------------------------------------------------------------------------


async def test_dispatch_leveling_refreshes_all_three_caches():
    pool = SyncPool()
    pool.level_config[100] = {"enabled": True}
    pool.level_no_xp[100] = [{"kind": "role", "target_id": 7}]
    pool.xp_multipliers[100] = [{"kind": "global", "target_id": 0, "factor": 2.0}]
    lv = FakeLeveling(pool)
    # Seed stale caches the dashboard just changed, to prove they are overwritten.
    lv._configs[100] = {"enabled": False}
    lv._no_xp[100] = frozenset()
    lv._multipliers[100] = ()
    bot = FakeBot(pool, leveling=lv)

    handled = await dashboard_sync.dispatch(bot, _payload("leveling", 100))

    assert handled == "leveling"
    # Each of the three caches is refreshed from the authoritative DB rows.
    assert lv._configs[100] == {"enabled": True}
    assert lv._no_xp[100] == frozenset({("role", 7)})
    assert lv._multipliers[100] == (("global", 0, 2.0),)


async def test_dispatch_leveling_pops_config_when_disabled():
    # No level_config row (dashboard turned leveling off / deleted the row): the
    # config mirror drops the guild, exactly as refresh_guild_config does.
    pool = SyncPool()
    lv = FakeLeveling(pool)
    lv._configs[100] = {"enabled": True}  # stale
    bot = FakeBot(pool, leveling=lv)

    handled = await dashboard_sync.dispatch(bot, _payload("leveling", 100))

    assert handled == "leveling"
    assert 100 not in lv._configs


async def test_dispatch_leveling_noop_without_cog():
    # No Leveling cog loaded: safe no-op, still reports handled.
    bot = FakeBot(SyncPool())
    handled = await dashboard_sync.dispatch(bot, _payload("leveling", 100))
    assert handled == "leveling"


# ---------------------------------------------------------------------------
# dispatch: warn_escalation invalidation (evict the settings LRU blob).
# ---------------------------------------------------------------------------


async def test_dispatch_warn_escalation_evicts_settings_blob():
    bot = FakeBot(SyncPool())
    # Seed the process-global settings cache with a stale policy for guild 100.
    key = (settings._GUILD[0], 100)
    settings._cache[key] = {
        "warn_escalation": [{"threshold": 3, "action": "kick", "duration": None}]
    }
    assert key in settings._cache

    handled = await dashboard_sync.dispatch(bot, _payload("warn_escalation", 100))

    assert handled == "warn_escalation"
    # Evicted: the next settings.get_guild would re-read the authoritative row.
    assert key not in settings._cache


# ---------------------------------------------------------------------------
# dispatch: malformed / unknown payloads are ignored (no cache mutation).
# ---------------------------------------------------------------------------


async def test_dispatch_ignores_unknown_kind():
    pool = SyncPool()
    bot = FakeBot(pool)
    handled = await dashboard_sync.dispatch(
        bot, '{"kind": "banword", "guildId": "100"}'
    )
    assert handled is None
    assert not pool.calls  # no DB read attempted


async def test_dispatch_ignores_malformed_json():
    pool = SyncPool()
    bot = FakeBot(pool)
    handled = await dashboard_sync.dispatch(bot, "definitely-not-json")
    assert handled is None
    assert not pool.calls


async def test_dispatch_swallows_invalidator_error():
    """A DB error inside an invalidator is logged and swallowed (returns None)."""

    class BoomPool(SyncPool):
        async def fetchval(self, query, *args):
            raise RuntimeError("db down")

    bot = FakeBot(BoomPool())
    # Must not raise; a bad notification can never take down the listener.
    handled = await dashboard_sync.dispatch(bot, _payload("prefix", 100))
    assert handled is None


# ---------------------------------------------------------------------------
# Invalidator/kind registry stays in sync.
# ---------------------------------------------------------------------------


def test_valid_kinds_match_invalidators():
    assert set(dashboard_sync._INVALIDATORS) == set(dashboard_sync.VALID_KINDS)
    assert dashboard_sync.VALID_KINDS == {
        "prefix",
        "autorole",
        "modlog",
        "muterole",
        "welcome",
        "starboard",
        "automod",
        "leveling",
        "warn_escalation",
    }
