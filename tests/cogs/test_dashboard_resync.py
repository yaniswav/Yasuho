"""Connect-time cache resync + the bot_heartbeat row (``cogs.system.dashboard_sync``).

The invalidators in that cog are driven by NOTIFY, and Postgres DROPS a
notification whose listener is absent rather than queuing it. So every dashboard
write made while the dedicated listen connection was down is lost, and the bot
would serve the pre-gap value from memory until an LRU eviction or the next
restart. Boot has the same gap as a reconnect (the caches are primed in
setup_hook, the LISTEN is registered only after READY), so both resync. The tests
here cover the two halves of the answer:

* :func:`dashboard_sync.resync_all` - what a connect must rebuild, per cache
  and per KIND of cache. The eager maps (whose absence is an ANSWER, not a miss)
  are RELOADED from the database; the read-through caches are emptied; the
  derived hub index is REBUILT. The eager-map tests are written so that a
  clear-only implementation fails them, and the RELOAD seams are pinned to leave
  their map ALONE when the read fails rather than installing an empty one.
* the heartbeat row, which is what lets the dashboard tell "the bot is down" from
  "the bot is up but its dashboard listener is down" - it rides the MAIN pool, so
  it keeps beating during exactly the gap the resync exists for.

Only the boundaries are faked (an in-memory pool, cog stand-ins). Where the point
of a test is that the resync reuses a cog's OWN rebuild, the REAL method is
driven unbound against the fake - ``core.Yasuho.load_eager_caches``,
``Leveling.reload_configs``, ``TemporaryRooms.reload_hub_index`` - so a change to
any of them is felt here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import pytest

import core

from cogs.community.leveling.leveling import Leveling
from cogs.config.rooms import TemporaryRooms
from cogs.system import dashboard_sync
from tools import settings
from tools.lru_cache import BoundedLRU

# ---------------------------------------------------------------------------
# In-memory boundaries.
# ---------------------------------------------------------------------------


class ResyncPool:
    """Fake pool answering the exact cold-start queries the resync re-runs.

    Two-column reads come back as tuples and single-column reads as mappings,
    which is what asyncpg Records support and what the callers rely on
    (``dict(rows)`` for the prefix/autorole/muterole maps, ``row["member_id"]``
    for the blacklist).
    """

    def __init__(self):
        self.prefixes = {}
        self.blacklist = set()
        self.autoroles = {}
        self.muteroles = {}
        # gid -> settings blob (the guild_settings JSONB row).
        self.guild_settings = {}
        # gid -> level_config row mapping.
        self.level_config = {}
        self.queries = []
        self.executed = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "FROM prefixes" in query:
            return [(gid, prefix) for gid, prefix in self.prefixes.items()]
        if "FROM blbot" in query:
            return [{"member_id": uid} for uid in sorted(self.blacklist)]
        if "FROM autorole" in query:
            return [(gid, role) for gid, role in self.autoroles.items()]
        if "FROM muterole" in query:
            return [(gid, role) for gid, role in self.muteroles.items()]
        if "FROM guild_settings" in query and "leveling_enabled" in query:
            return [
                {"guild_id": gid}
                for gid, blob in self.guild_settings.items()
                if blob.get("leveling_enabled") is True
            ]
        if "FROM guild_settings" in query:
            return [
                {"guild_id": gid, "settings": blob}
                for gid, blob in self.guild_settings.items()
            ]
        if "FROM level_config" in query:
            return [dict(row, guild_id=gid) for gid, row in self.level_config.items()]
        raise AssertionError(f"unexpected fetch: {query!r}")  # pragma: no cover

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "FROM guild_settings" in query:  # settings.get_guild read-through
            return self.guild_settings.get(args[0])
        raise AssertionError(f"unexpected fetchval: {query!r}")  # pragma: no cover

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 0 1"


class FakeModLog:
    def __init__(self):
        self._channels = {}


class FakeStarboard:
    def __init__(self):
        self._config = {}


class FakeAutoMod:
    def __init__(self):
        self._settings = {}


class FakeLeveling:
    """Leveling stand-in whose ``reload_configs`` is the cog's REAL method.

    ``_configs`` is the trap this whole lot is about: ``get_config`` is a plain
    synchronous dict read where a missing guild MEANS "leveling is off", so the
    resync must rebuild it, never empty it. Driving the real rebuild here is what
    makes that assertion about production code.
    """

    def __init__(self, bot):
        self.bot = bot
        self._configs = {}
        self._no_xp = BoundedLRU(8)
        self._multipliers = BoundedLRU(8)
        self._rank_cards = BoundedLRU(8)
        self._period_markers = BoundedLRU(8)

    async def reload_configs(self):
        await Leveling.reload_configs(self)


class FakeCustomCommands:
    def __init__(self):
        self._cache = {}
        self._uses = {}
        self._cd = {}


class FakeRooms:
    """Rooms stand-in whose ``reload_hub_index`` is the cog's REAL method."""

    def __init__(self, bot):
        self.bot = bot
        self._hub_index = {}

    async def reload_hub_index(self):
        return await TemporaryRooms.reload_hub_index(self)


class FakeBot:
    """Bot stand-in whose eager-cache reload is core's REAL seam."""

    def __init__(self, pool, **cogs):
        self.db_pool = pool
        self.prefixes = {}
        self.blacklist = set()
        self.autoroles = {}
        self.muteroles = {}
        # The real seam takes this lock; the real invalidators take it too.
        self.eager_cache_lock = asyncio.Lock()
        self._cogs = {name: cog for name, cog in cogs.items() if cog is not None}

    def get_cog(self, name):
        return self._cogs.get(name)

    async def load_eager_caches(self):
        await core.Yasuho.load_eager_caches(self)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The tools.settings LRU is process-global; keep it from leaking across tests."""
    settings._cache.clear()
    yield
    settings._cache.clear()


def _full_bot(pool):
    """A bot with every cache-owning cog loaded, wired to ``pool``."""
    bot = FakeBot(pool)
    bot._cogs = {
        "ModLog": FakeModLog(),
        "Starboard": FakeStarboard(),
        "AutoMod": FakeAutoMod(),
        "Leveling": FakeLeveling(bot),
        "CustomCommands": FakeCustomCommands(),
        "TemporaryRooms": FakeRooms(bot),
    }
    return bot


# ---------------------------------------------------------------------------
# The eager maps: RELOADED from the DB, never cleared.
# ---------------------------------------------------------------------------


async def test_resync_reloads_the_eager_maps_from_the_database():
    """The bug in one test: a write made during the gap must be picked up.

    Guild 100's prefix changed on the dashboard while the listener was down,
    guild 200's was deleted, guild 300 got its first one, and a user was
    blacklisted. None of that emitted a notification this process ever saw.
    """
    pool = ResyncPool()
    pool.prefixes = {100: "!!", 300: "?"}
    pool.autoroles = {100: 11}
    pool.muteroles = {100: 22}
    pool.blacklist = {900}
    bot = FakeBot(pool)
    bot.prefixes = {100: "$", 200: "%"}  # pre-gap memory: stale + deleted
    bot.autoroles = {100: 99}
    bot.muteroles = {}
    bot.blacklist = set()

    await dashboard_sync.resync_all(bot)

    assert bot.prefixes == {100: "!!", 300: "?"}
    assert bot.autoroles == {100: 11}
    assert bot.muteroles == {100: 22}
    assert bot.blacklist == {900}


async def test_resync_never_empties_the_prefix_map_the_db_still_fills():
    """The anti-clear guard, stated on its own because the failure is silent.

    ``get_prefix`` reads ``bot.prefixes`` synchronously with no read-through, so
    an emptied map does not re-read - it answers "no custom prefix" and every
    custom-prefix guild silently falls back to the default until a restart. A
    resync that clears instead of reloading passes every other assertion in this
    file and fails this one.
    """
    pool = ResyncPool()
    pool.prefixes = {100: "!!", 200: "??"}
    bot = FakeBot(pool)
    bot.prefixes = {100: "!!", 200: "??"}

    await dashboard_sync.resync_all(bot)

    assert bot.prefixes == {100: "!!", 200: "??"}
    assert bot.prefixes  # never empty while the DB has rows


async def test_resync_swaps_the_map_only_after_every_read():
    """A reader mid-resync sees a complete map, never a half-filled one.

    The maps are rebuilt aside and rebound; nothing aliases them, so a concurrent
    ``get_prefix`` gets the old map or the new one.
    """
    pool = ResyncPool()
    pool.prefixes = {100: "!!"}
    bot = FakeBot(pool)
    bot.prefixes = {100: "$"}
    seen = []

    original = pool.fetch

    async def _watching_fetch(query, *args):
        seen.append(dict(bot.prefixes))
        return await original(query, *args)

    pool.fetch = _watching_fetch

    await dashboard_sync.resync_all(bot)

    # Every read during the reload observed the OLD complete map...
    assert all(snapshot == {100: "$"} for snapshot in seen[:4])
    # ...and the new one is in place afterwards.
    assert bot.prefixes == {100: "!!"}


async def test_a_notify_landing_mid_reload_is_not_lost():
    """The lost-update window between the resync's rebind and an invalidator.

    Both write the same four maps, but differently: the reload REBINDS the
    attribute to a map it fetched earlier, while an invalidator mutates the map
    OBJECT in place. Unsynchronised, a notify arriving mid-reload writes into the
    dict the rebind is about to discard, and the dashboard's change is silently
    gone until somebody writes that guild again - which directly contradicts the
    guarantee that a notify arriving DURING the resync is delivered. Both sides
    take bot.eager_cache_lock, so the write either precedes the fetch or follows
    the rebind.
    """
    pool = ResyncPool()
    pool.prefixes = {100: "!"}  # what the reload's fetch will see
    bot = FakeBot(pool)
    bot.prefixes = {100: "!"}

    async def _fetchval(query, *args):
        return pool.prefixes.get(args[0])

    pool.fetchval = _fetchval

    original = pool.fetch
    notified = []

    async def _fetch_then_notify(query, *args):
        rows = await original(query, *args)
        if "FROM prefixes" in query and not notified:
            # The dashboard writes "?" and notifies while the reload is still
            # fetching its other three maps.
            pool.prefixes[100] = "?"
            notified.append(
                asyncio.create_task(dashboard_sync._invalidate_prefix(bot, 100))
            )
            await asyncio.sleep(0)  # let the handler task actually start
        return rows

    pool.fetch = _fetch_then_notify

    await dashboard_sync.resync_all(bot)
    await asyncio.gather(*notified)

    assert bot.prefixes == {100: "?"}


async def test_resync_tolerates_a_bot_without_the_eager_seam():
    """A bot object with no load_eager_caches (an older core, a stand-in) skips it."""
    pool = ResyncPool()

    class Minimal:
        def __init__(self):
            self.db_pool = pool

        def get_cog(self, name):
            return None

    done = await dashboard_sync.resync_all(Minimal())

    assert done == [name for name, _ in dashboard_sync._RESYNC_STEPS]
    assert not pool.queries  # nothing was reloaded, and nothing crashed


# ---------------------------------------------------------------------------
# The read-through caches: emptied, so the next access reloads.
# ---------------------------------------------------------------------------


async def test_resync_empties_the_settings_lru_in_both_scopes():
    pool = ResyncPool()
    settings._cache[(settings._GUILD[0], 100)] = {"welcome": "stale"}
    settings._cache[(settings._USER[0], 900)] = {"locale": "stale"}
    bot = FakeBot(pool)

    await dashboard_sync.resync_all(bot)

    assert (settings._GUILD[0], 100) not in settings._cache
    assert (settings._USER[0], 900) not in settings._cache


async def test_resync_empties_every_read_through_cog_cache():
    pool = ResyncPool()
    bot = _full_bot(pool)
    bot.get_cog("ModLog")._channels[100] = None  # negative cache
    bot.get_cog("Starboard")._config[100] = (7, 3)
    bot.get_cog("AutoMod")._settings[100] = {"antilink": True}
    leveling = bot.get_cog("Leveling")
    leveling._no_xp[100] = "stale"
    leveling._multipliers[100] = "stale"
    leveling._rank_cards[100] = "stale"
    cc = bot.get_cog("CustomCommands")
    cc._cache[100] = {"hi": {}}
    cc._uses[100] = {"hi": 5}

    await dashboard_sync.resync_all(bot)

    assert bot.get_cog("ModLog")._channels == {}
    assert bot.get_cog("Starboard")._config == {}
    assert bot.get_cog("AutoMod")._settings == {}
    assert len(leveling._no_xp) == 0
    assert len(leveling._multipliers) == 0
    assert len(leveling._rank_cards) == 0
    assert cc._cache == {} and cc._uses == {}


async def test_resync_keeps_the_custom_command_cooldown_clocks():
    """``_cd`` holds cooldown CLOCKS, not configuration.

    The per-guild invalidator drops a guild's clocks because that guild's
    commands just changed. A reconnect says nothing of the sort, and wiping every
    clock would hand every member of every guild a free re-use.
    """
    pool = ResyncPool()
    bot = _full_bot(pool)
    cc = bot.get_cog("CustomCommands")
    cc._cd[(100, "hi", 900)] = 12345.0

    await dashboard_sync.resync_all(bot)

    assert cc._cd == {(100, "hi", 900): 12345.0}


async def test_resync_keeps_the_leveling_period_markers():
    """Season rollover bookkeeping is not dashboard state (and is cold-safe)."""
    pool = ResyncPool()
    bot = _full_bot(pool)
    bot.get_cog("Leveling")._period_markers[100] = "2026-W32"

    await dashboard_sync.resync_all(bot)

    assert bot.get_cog("Leveling")._period_markers.get(100) == "2026-W32"


# ---------------------------------------------------------------------------
# Leveling's _configs: an eager map wearing a cache's clothes.
# ---------------------------------------------------------------------------


async def test_resync_rebuilds_the_leveling_config_map_from_the_db():
    pool = ResyncPool()
    pool.level_config = {
        100: {"enabled": True, "cooldown_seconds": 60},
        200: {"enabled": False},  # switched off on the dashboard during the gap
    }
    pool.guild_settings = {300: {"leveling_enabled": True}}  # legacy fallback
    bot = _full_bot(pool)
    leveling = bot.get_cog("Leveling")
    leveling._configs = {200: "stale-enabled"}

    await dashboard_sync.resync_all(bot)

    # Enabled guilds are PRESENT (a cleared map would have made leveling dead
    # bot-wide, since get_config's absence means "off").
    assert set(leveling._configs) == {100, 300}
    assert leveling._configs[100].cooldown_seconds == 60
    assert leveling._configs[100].enabled is True


# ---------------------------------------------------------------------------
# The derived hub index: rebuilt the way the cog builds it.
# ---------------------------------------------------------------------------


def _hub(hub_channel_id, label="Hub"):
    return {"hub_channel_id": hub_channel_id, "label": label, "id": "abc12345"}


async def test_resync_rebuilds_the_hub_index_including_a_brand_new_guild():
    """Rebuilt whole, not per known guild.

    Guild 300 configured its FIRST hub during the gap, so it is absent from the
    index: iterating over the current index (or evicting it) could never surface
    it. Guild 200's hubs were removed and must disappear.
    """
    pool = ResyncPool()
    pool.guild_settings = {
        100: {"autorooms": [_hub(1111)]},
        200: {"autorooms": []},
        300: {"autorooms": [_hub(3333)]},
    }
    bot = _full_bot(pool)
    rooms = bot.get_cog("TemporaryRooms")
    rooms._hub_index = {100: {9999: _hub(9999)}, 200: {2222: _hub(2222)}}

    await dashboard_sync.resync_all(bot)

    assert set(rooms._hub_index) == {100, 300}
    assert set(rooms._hub_index[100]) == {1111}
    assert set(rooms._hub_index[300]) == {3333}


async def test_a_failed_hub_read_never_installs_an_empty_index():
    """The regression this lot could have shipped, in one test.

    ``on_voice_state_update`` reads ``_hub_index`` synchronously and treats an
    absent guild as "no hubs here", and NOTHING re-derives the index - only a
    per-guild write, a rejoin or another resync. So an empty index is a WRONG
    ANSWER for every guild at once, not a miss: a single failed read would kill
    join-to-create fleet-wide until a restart. And the resync runs right after a
    listen connection died, i.e. when the pool is most likely failing too.

    The step must therefore leave the live index untouched AND be reported as not
    done - a log line naming "rooms" among the resynced steps would assert the
    exact opposite of what happened.
    """

    class HubReadFails(ResyncPool):
        async def fetch(self, query, *args):
            if "FROM guild_settings" in query and "leveling_enabled" not in query:
                raise RuntimeError("connection reset")
            return await super().fetch(query, *args)

    bot = _full_bot(HubReadFails())
    rooms = bot.get_cog("TemporaryRooms")
    live = {100: {1111: _hub(1111)}}
    rooms._hub_index = live

    done = await dashboard_sync.resync_all(bot)

    assert rooms._hub_index == live
    assert rooms._hub_index is live  # not even rebuilt from a partial read
    assert "rooms" not in done


async def test_a_failed_leveling_read_never_empties_the_config_map():
    """Same shape as the hub index: ``get_config`` is a bare dict read.

    A missing guild MEANS "leveling is off", so a config map emptied by a DB
    blip stops XP bot-wide with nothing to heal it.
    """

    class ConfigReadFails(ResyncPool):
        async def fetch(self, query, *args):
            if "FROM level_config" in query:
                raise RuntimeError("connection reset")
            return await super().fetch(query, *args)

    bot = _full_bot(ConfigReadFails())
    leveling_cog = bot.get_cog("Leveling")
    live = {100: object()}
    leveling_cog._configs = live

    done = await dashboard_sync.resync_all(bot)

    assert leveling_cog._configs is live
    assert "leveling" not in done


async def test_the_hub_rebuild_runs_after_the_settings_lru_is_emptied():
    """Pin the step order, without the rationale it does NOT have.

    Emptying the settings LRU first is load-bearing for the PER-GUILD autorooms
    invalidator (``_load_hubs`` -> ``settings.get_guild``), not for this
    whole-index rebuild, which reads ``guild_settings`` straight off the pool.
    Pinned anyway so the two paths keep the same shape, and stated here so a
    later refactor does not inherit a claim that was never true of this step.
    """
    pool = ResyncPool()
    pool.guild_settings = {100: {"autorooms": [_hub(1111)]}}
    bot = _full_bot(pool)
    seen = []

    rooms = bot.get_cog("TemporaryRooms")
    real_reload = rooms.reload_hub_index

    async def _recording_reload():
        seen.append((settings._GUILD[0], 100) in settings._cache)
        return await real_reload()

    rooms.reload_hub_index = _recording_reload
    settings._cache[(settings._GUILD[0], 100)] = {"autorooms": [_hub(9999)]}

    await dashboard_sync.resync_all(bot)

    assert seen == [False]  # the stale blob was already gone


# ---------------------------------------------------------------------------
# Defensiveness: missing cogs, failing steps.
# ---------------------------------------------------------------------------


async def test_resync_tolerates_every_cog_being_absent():
    pool = ResyncPool()
    bot = FakeBot(pool)  # no cogs at all

    done = await dashboard_sync.resync_all(bot)

    assert done == [name for name, _ in dashboard_sync._RESYNC_STEPS]


async def test_a_failing_step_does_not_stop_the_others():
    pool = ResyncPool()
    bot = _full_bot(pool)

    async def _boom():
        raise RuntimeError("db blip")

    bot.get_cog("Leveling").reload_configs = _boom
    bot.get_cog("ModLog")._channels[100] = None

    done = await dashboard_sync.resync_all(bot)

    assert "leveling" not in done
    assert {"eager_caches", "settings", "modlog", "rooms"} <= set(done)
    assert bot.get_cog("ModLog")._channels == {}


async def test_resync_never_raises_when_the_pool_is_dead():
    """Every DB-backed step fails, no step raises, and no map is wiped."""

    class DeadPool:
        async def fetch(self, *args):
            raise RuntimeError("connection reset")

    bot = _full_bot(DeadPool())
    bot.prefixes = {100: "!"}
    rooms = bot.get_cog("TemporaryRooms")
    rooms._hub_index = {100: {1111: _hub(1111)}}
    done = await dashboard_sync.resync_all(bot)  # must not raise

    assert {"eager_caches", "leveling", "rooms"}.isdisjoint(done)
    assert "settings" in done  # the pure clears do not need the pool
    assert bot.prefixes == {100: "!"}
    assert rooms._hub_index == {100: {1111: _hub(1111)}}


# ---------------------------------------------------------------------------
# The supervisor: first connection vs reconnect.
# ---------------------------------------------------------------------------


class StubSync(dashboard_sync.DashboardSync):
    """The REAL supervisor loop with only the socket-touching seams stubbed.

    ``__init__`` is bypassed on purpose (it opens a connection and starts the
    heartbeat); every attribute the loop touches is set here instead, so what is
    under test is the actual ``_supervise`` / ``_connect_and_listen`` /
    ``_watch_connection`` wiring.
    """

    def __init__(self, bot, cycles=1):
        self.bot = bot
        self._conn = None
        self._closing = False
        self._supervisor = None
        self._handlers = set()
        self._connected_once = False
        self._resync_task = None
        self._listening = False
        self._version = "abc1234"
        self._dsn = "postgresql://stub"
        self._cycles = cycles
        self.events = []

    async def _connect_and_listen(self):
        self.events.append("listen")
        self._conn = object()
        self._connected_once = True
        self._listening = True

    async def _watch_connection(self):
        self.events.append("watch")
        self._listening = False  # the real loop drops the flag before backoff
        self._cycles -= 1
        if self._cycles <= 0:
            self._closing = True

    async def _teardown_connection(self):
        self._listening = False
        self._conn = None


class SupervisorBot:
    def __init__(self):
        self.db_pool = ResyncPool()
        self.loop = None

    async def wait_until_ready(self):
        return None

    def get_cog(self, name):
        return None


@pytest.fixture
async def supervisor_bot():
    bot = SupervisorBot()
    bot.loop = asyncio.get_running_loop()
    return bot


async def _drain(cog):
    """Let the tasks the supervisor scheduled finish."""
    if cog._handlers:
        await asyncio.gather(*list(cog._handlers))


async def test_the_first_connection_resyncs_too_because_boot_has_the_same_gap(
    monkeypatch, supervisor_bot, caplog
):
    """The BOOT window, which continuous deploy reopens on every push.

    The caches are primed inside setup_hook - before the gateway connects - and
    this LISTEN is only registered after wait_until_ready returns, i.e. after
    IDENTIFY, the guild stream and member chunking. Postgres drops every
    notification emitted in between exactly as it does during a reconnect, so
    skipping the resync on the first connect leaves that whole window uncovered
    with nothing to close it until the next restart.
    """
    calls = []

    async def _fake_resync(bot):
        calls.append(bot)
        return ["eager_caches", "settings"]

    monkeypatch.setattr(dashboard_sync, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_sync, "resync_all", _fake_resync)

    cog = StubSync(supervisor_bot, cycles=1)
    with caplog.at_level(logging.INFO, logger=dashboard_sync.log.name):
        await cog._supervise()
        await _drain(cog)

    assert cog.events == ["listen", "watch"]
    assert calls == [supervisor_bot]
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "established (boot)" in message
    assert "eager_caches, settings" in message


async def test_a_reconnect_resyncs_and_says_so(monkeypatch, supervisor_bot, caplog):
    """The confirmed bug: the gap dropped notifications, so rebuild everything."""
    calls = []

    async def _fake_resync(bot):
        calls.append(bot)
        return ["eager_caches", "settings"]

    monkeypatch.setattr(dashboard_sync, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_sync, "resync_all", _fake_resync)

    cog = StubSync(supervisor_bot, cycles=2)
    with caplog.at_level(logging.INFO, logger=dashboard_sync.log.name):
        await cog._supervise()
        await _drain(cog)

    assert cog.events == ["listen", "watch", "listen", "watch"]
    assert calls == [supervisor_bot, supervisor_bot]
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "established (boot)" in message
    assert "established (reconnect)" in message


async def test_a_connect_then_die_loop_never_piles_up_resyncs(
    monkeypatch, supervisor_bot
):
    """One sweep in flight at a time, whatever the reconnect rate.

    The backoff resets on every SUCCESSFUL connect, so a server that accepts a
    connection then immediately kills it (pgbouncer refusing LISTEN, connection
    churn) cycles about once a second. Each cycle scheduling another full sweep -
    four eager queries, a level_config scan and two whole guild_settings scans -
    would be a self-inflicted storm precisely when the database is least able to
    take it.
    """
    started = 0
    release = asyncio.Event()

    async def _slow_resync(bot):
        nonlocal started
        started += 1
        await release.wait()
        return []

    monkeypatch.setattr(dashboard_sync, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_sync, "resync_all", _slow_resync)

    cog = StubSync(supervisor_bot, cycles=5)
    await cog._supervise()

    assert cog.events.count("listen") == 5
    assert started == 1  # the four later connects found one already running

    release.set()
    await _drain(cog)
    assert started == 1


async def test_the_resync_is_scheduled_after_the_listen_is_attached(
    monkeypatch, supervisor_bot
):
    """Order matters: a notify arriving during the resync must not be lost too."""
    order = []

    async def _fake_resync(bot):
        order.append("resync")
        return []

    monkeypatch.setattr(dashboard_sync, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_sync, "resync_all", _fake_resync)

    class Ordered(StubSync):
        async def _connect_and_listen(self):
            order.append("listen")
            await super()._connect_and_listen()

    cog = Ordered(supervisor_bot, cycles=1)
    await cog._supervise()
    await _drain(cog)

    assert order == ["listen", "resync"]


async def test_a_failed_connect_is_not_counted_as_a_connection(
    monkeypatch, supervisor_bot, caplog
):
    """``_connected_once`` flips only after add_listener actually succeeded, so a
    bot that never reached Postgres still calls its first real connect a BOOT."""
    monkeypatch.setattr(dashboard_sync, "_BACKOFF_START", 0.0)

    async def _fake_resync(bot):
        return []

    monkeypatch.setattr(dashboard_sync, "resync_all", _fake_resync)

    class Flaky(StubSync):
        async def _connect_and_listen(self):
            if not self.events:
                self.events.append("failed")
                raise OSError("connection refused")
            await super()._connect_and_listen()

    cog = Flaky(supervisor_bot, cycles=1)
    with caplog.at_level(logging.INFO, logger=dashboard_sync.log.name):
        await cog._supervise()
        await _drain(cog)

    assert cog.events == ["failed", "listen", "watch"]
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "established (boot)" in message
    assert "established (reconnect)" not in message


# ---------------------------------------------------------------------------
# Heartbeat.
# ---------------------------------------------------------------------------


class HeartbeatPool:
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 0 1"


async def test_write_heartbeat_is_one_upsert_of_the_single_row():
    pool = HeartbeatPool()

    await dashboard_sync.write_heartbeat(pool, True, "abc1234")

    assert len(pool.executed) == 1
    query, args = pool.executed[0]
    assert query.count(";") == 0  # asyncpg is mono-statement
    assert "INSERT INTO bot_heartbeat" in query
    assert "ON CONFLICT (id) DO UPDATE" in query
    assert "VALUES (1, now(), $1, $2)" in query
    assert args == (True, "abc1234")


async def test_write_heartbeat_coerces_listening_and_accepts_no_version():
    pool = HeartbeatPool()

    await dashboard_sync.write_heartbeat(pool, 0, None)

    assert pool.executed[0][1] == (False, None)


async def test_listening_tracks_the_listen_connection(monkeypatch, supervisor_bot):
    """false -> true on add_listener -> false the moment the watch loop gives up."""
    cog = StubSync(supervisor_bot, cycles=1)
    assert cog._listening is False

    await cog._connect_and_listen()
    assert cog._listening is True

    await cog._watch_connection()
    assert cog._listening is False


async def test_the_real_watch_loop_drops_listening_on_a_dead_connection(
    supervisor_bot,
):
    """The stub above mirrors the real loop; this pins the real one."""

    class DeadConn:
        def is_closed(self):
            return True

    cog = StubSync(supervisor_bot)
    cog._listening = True
    cog._conn = DeadConn()

    await dashboard_sync.DashboardSync._watch_connection(cog)

    assert cog._listening is False


async def test_the_real_watch_loop_drops_listening_when_the_keepalive_fails(
    supervisor_bot,
):
    class FlakyConn:
        def is_closed(self):
            return False

        async def execute(self, query):
            raise OSError("connection reset by peer")

    cog = StubSync(supervisor_bot)
    cog._listening = True
    cog._conn = FlakyConn()

    await dashboard_sync.DashboardSync._watch_connection(cog)

    assert cog._listening is False


async def test_teardown_marks_the_listener_gone(supervisor_bot):
    cog = StubSync(supervisor_bot)
    cog._listening = True

    await dashboard_sync.DashboardSync._teardown_connection(cog)

    assert cog._listening is False


async def test_a_beat_during_the_gap_says_not_listening(supervisor_bot):
    """The point of the whole row: the bot is alive, its dashboard link is not.

    The beat rides the MAIN pool, so it lands while the dedicated listen
    connection is down.
    """
    pool = HeartbeatPool()
    supervisor_bot.db_pool = pool
    cog = StubSync(supervisor_bot)
    cog._listening = False

    await dashboard_sync.DashboardSync._heartbeat.coro(cog)

    assert pool.executed[0][1] == (False, "abc1234")


async def test_a_beat_while_listening_says_so(supervisor_bot):
    pool = HeartbeatPool()
    supervisor_bot.db_pool = pool
    cog = StubSync(supervisor_bot)
    cog._listening = True

    await dashboard_sync.DashboardSync._heartbeat.coro(cog)

    assert pool.executed[0][1] == (True, "abc1234")


async def test_a_failed_beat_never_escapes_the_loop(supervisor_bot):
    """An unhandled exception would STOP the tasks.Loop, turning one blip into a
    permanent 'bot offline' badge."""

    class DeadPool:
        async def execute(self, *args):
            raise OSError("connection reset")

    supervisor_bot.db_pool = DeadPool()
    cog = StubSync(supervisor_bot)

    await dashboard_sync.DashboardSync._heartbeat.coro(cog)  # must not raise


async def test_cog_unload_writes_a_final_not_listening_beat(supervisor_bot):
    pool = HeartbeatPool()
    supervisor_bot.db_pool = pool
    cog = StubSync(supervisor_bot)
    cog._listening = True

    await dashboard_sync.DashboardSync.cog_unload(cog)

    assert cog._closing is True
    assert cog._listening is False
    assert pool.executed[-1][1] == (False, "abc1234")


async def test_cog_unload_survives_a_dead_pool(supervisor_bot):
    class DeadPool:
        async def execute(self, *args):
            raise OSError("connection reset")

    supervisor_bot.db_pool = DeadPool()
    cog = StubSync(supervisor_bot)

    await dashboard_sync.DashboardSync.cog_unload(cog)  # best effort, never raises


async def test_cog_unload_does_not_hang_on_a_wedged_pool(supervisor_bot, monkeypatch):
    """A dead pool RAISES; a wedged one does neither, which is the worse case.

    ``pool.execute`` acquires a connection first, and ``acquire(timeout=None)``
    waits on the pool queue with NO bound; only once acquired is the statement
    capped by command_timeout (core.main: 60s). So an unbounded final beat can
    hold a clean shutdown open indefinitely over a liveness row whose loss costs
    nothing - the row just ages past the staleness threshold and the dashboard
    says "offline", which by then is true. Same call botstats already makes for
    its final flush.
    """
    monkeypatch.setattr(dashboard_sync, "UNLOAD_BEAT_TIMEOUT", 0.01)

    class WedgedPool:
        async def execute(self, *args):
            await asyncio.Event().wait()  # never resolves

    supervisor_bot.db_pool = WedgedPool()
    cog = StubSync(supervisor_bot)

    await asyncio.wait_for(
        dashboard_sync.DashboardSync.cog_unload(cog), 5
    )  # the inner bound is what makes this return at all


def test_git_short_hash_returns_a_hash_or_none():
    value = dashboard_sync._git_short_hash()
    assert value is None or re.fullmatch(r"[0-9a-f]{7}", value)


def test_git_short_hash_never_forks_a_subprocess():
    """The house rule: nothing blocks the event loop.

    This runs in __init__, which is harmless at boot (setup_hook precedes the
    websocket) but runs on a LIVE loop under ``?reload dashboard_sync``. Every
    other git/pg_dump call in the tree goes through create_subprocess_exec for
    exactly that reason; two file reads need no seam at all.
    """
    assert not hasattr(dashboard_sync, "subprocess")


def test_git_short_hash_never_raises(monkeypatch, tmp_path):
    """No .git at all (a tarball deploy) is an accepted answer, not a crash."""
    monkeypatch.setattr(dashboard_sync, "__file__", str(tmp_path / "a/b/c/d.py"))
    assert dashboard_sync._git_short_hash() is None


@pytest.mark.parametrize(
    "head",
    [
        "ref: refs/heads/main",  # loose ref
        "ref: refs/heads/packed-only",  # packed-refs
        "0123456789abcdef0123456789abcdef01234567",  # detached HEAD
    ],
)
def test_git_short_hash_reads_every_shape_of_head(monkeypatch, tmp_path, head):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text(head + "\n", encoding="utf-8")
    sha = "0123456789abcdef0123456789abcdef01234567"
    (git_dir / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{sha} refs/heads/packed-only\n",
        encoding="utf-8",
    )
    # _git_short_hash walks three dirnames up from its own __file__.
    monkeypatch.setattr(dashboard_sync, "__file__", str(repo / "a/b/c.py"))

    assert dashboard_sync._git_short_hash() == sha[:7]


def test_git_short_hash_rejects_a_value_that_is_not_a_sha(monkeypatch, tmp_path):
    """A ref that resolves to something else must yield NULL, not garbage."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("not a sha at all\n", encoding="utf-8")
    monkeypatch.setattr(dashboard_sync, "__file__", str(repo / "a/b/c.py"))

    assert dashboard_sync._git_short_hash() is None


async def test_the_beat_reuses_the_hash_captured_at_load(supervisor_bot, monkeypatch):
    """The hash is read ONCE at cog load, never per beat."""
    calls = []

    def _record():
        calls.append(1)
        return None

    monkeypatch.setattr(dashboard_sync, "_git_short_hash", _record)

    pool = HeartbeatPool()
    supervisor_bot.db_pool = pool
    cog = StubSync(supervisor_bot)

    await dashboard_sync.DashboardSync._heartbeat.coro(cog)
    await dashboard_sync.DashboardSync._heartbeat.coro(cog)

    assert calls == []  # the hash is _version, read at load
    assert [args for _, args in pool.executed] == [(False, "abc1234")] * 2


# ---------------------------------------------------------------------------
# The DDL contract.
# ---------------------------------------------------------------------------

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
)


def _heartbeat_ddl():
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        ddl = re.sub(r"--[^\n]*", "", handle.read())
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+bot_heartbeat\s*\((.*?)\n\s*\)\s*;",
        ddl,
        re.S | re.I,
    )
    assert match is not None, "bot_heartbeat is missing from schema.sql"
    return match.group(1)


def test_the_heartbeat_table_is_a_pinned_singleton():
    body = _heartbeat_ddl()
    assert re.search(r"id\s+SMALLINT\s+PRIMARY KEY\s+DEFAULT 1\s+CHECK \(id = 1\)", body)
    assert re.search(r"updated_at\s+TIMESTAMPTZ\s+NOT NULL", body)
    assert re.search(r"listening\s+BOOLEAN\s+NOT NULL", body)
    assert re.search(r"version\s+TEXT", body)


def test_the_heartbeat_table_is_invisible_to_both_structural_guards():
    """No guild_id and no user_id: it is process state, not anybody's data.

    The guild-purge guard (tests/tools/test_retention.py) enumerates tables with
    a guild_id column and the personal-export guard (tests/tools/test_privacy.py)
    tables with a user_id column. Adding either column here would silently enlist
    this table in both, so pin their absence.
    """
    body = _heartbeat_ddl()
    assert not re.search(r"^\s*guild_id\b", body, re.M)
    assert not re.search(r"^\s*user_id\b", body, re.M)


def test_the_staleness_threshold_is_three_beats():
    """The contract published to the dashboard: >90s means offline."""
    assert dashboard_sync.HEARTBEAT_SECONDS == 30.0
    assert dashboard_sync.HEARTBEAT_STALE_SECONDS == 90
    assert (
        dashboard_sync.HEARTBEAT_STALE_SECONDS
        == 3 * dashboard_sync.HEARTBEAT_SECONDS
    )


# ---------------------------------------------------------------------------
# Load-time wiring (the real __init__, with only the supervisor stubbed out so
# nothing touches a socket).
# ---------------------------------------------------------------------------


async def test_the_cog_starts_the_heartbeat_at_load(monkeypatch):
    """The beat is wired at load and is INDEPENDENT of the listen connection.

    It starts before (and regardless of) the supervisor, because "the bot is up
    but not listening" is exactly the state the dashboard needs told.
    """

    async def _no_supervise(self):
        return None

    monkeypatch.setattr(dashboard_sync.DashboardSync, "_supervise", _no_supervise)

    pool = HeartbeatPool()
    bot = SupervisorBot()
    bot.db_pool = pool
    bot.loop = asyncio.get_running_loop()

    cog = dashboard_sync.DashboardSync(bot)
    try:
        assert cog._listening is False  # nothing is listening yet
        assert cog._version == dashboard_sync._git_short_hash()
        for _ in range(20):  # let the first beat land
            if pool.executed:
                break
            await asyncio.sleep(0)
        assert pool.executed
        assert pool.executed[0][1] == (False, cog._version)
    finally:
        await cog.cog_unload()

    # The unload cancelled the loop and left one final not-listening beat.
    assert pool.executed[-1][1] == (False, cog._version)
    await asyncio.sleep(0)
    assert cog._heartbeat.is_running() is False
