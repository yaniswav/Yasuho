"""Unit tests for cogs.system.health: pure formatting + counters + the tick.

The tasks.loop-wrapped method is driven via its raw ``.coro`` (same pattern as
tests/cogs/test_retention.py) so no real background loop ever starts.
"""

import logging
import types

from cogs.anilist.throttle import AniListThrottle
from cogs.system import health


def _cog(bot):
    cog = object.__new__(health.Health)
    cog.bot = bot
    cog.gw_resumes = 0
    cog.gw_disconnects = 0
    return cog


class _Pool:
    def __init__(self, size, idle, max_size):
        self._size = size
        self._idle = idle
        self._max = max_size

    def get_size(self):
        return self._size

    def get_idle_size(self):
        return self._idle

    def get_max_size(self):
        return self._max


# --- format_load_line: pure formatting -------------------------------------


def test_format_load_line_renders_all_fields():
    line = health.format_load_line(
        pool_size=8,
        pool_idle=2,
        pool_max=30,
        quota_stats={"effects_guild": {"hits": 3, "rejections": 1}},
        webhook_stats={"hits": 10, "rejections": 0, "tracked": 2},
        anilist_stats={
            "throttled_429": 4,
            "global_hits": 12,
            "global_rejections": 1,
        },
        gw_resumes=1,
        gw_disconnects=0,
    )
    assert line == (
        "LOAD pool=8/30 idle=2 "
        "quotas=effects_guild(hits=3 rejections=1) "
        "webhook=hits=10 rejections=0 tracked=2 "
        "anilist=throttled_429=4 global_hits=12 global_rejections=1 "
        "gw_resumes=1 gw_disconnects=0"
    )


def test_format_load_line_handles_missing_subsystems():
    line = health.format_load_line(
        pool_size=5,
        pool_idle=5,
        pool_max=30,
        quota_stats=None,
        webhook_stats=None,
        anilist_stats=None,
        gw_resumes=0,
        gw_disconnects=0,
    )
    assert "quotas=n/a" in line
    assert "webhook=n/a" in line
    assert "anilist=n/a" in line


def test_format_load_line_multiple_quota_members_stable_order():
    line = health.format_load_line(
        pool_size=1,
        pool_idle=1,
        pool_max=30,
        quota_stats={
            "effects_guild": {"hits": 1},
            "lyrics_user": {"hits": 2},
        },
        webhook_stats=None,
        anilist_stats=None,
        gw_resumes=0,
        gw_disconnects=0,
    )
    assert "effects_guild(hits=1) lyrics_user(hits=2)" in line


# --- gateway counters --------------------------------------------------------


async def test_on_resumed_increments_counter_and_warns(caplog):
    cog = _cog(bot=types.SimpleNamespace())
    with caplog.at_level(logging.WARNING, logger=health.log.name):
        await cog.on_resumed()
        await cog.on_resumed()
    assert cog.gw_resumes == 2
    assert sum("resumed" in r.message for r in caplog.records) == 2


async def test_on_disconnect_increments_counter():
    cog = _cog(bot=types.SimpleNamespace())
    await cog.on_disconnect()
    await cog.on_disconnect()
    await cog.on_disconnect()
    assert cog.gw_disconnects == 3


# --- _quota_stats / _webhook_stats: defensive lookups ------------------------


def test_quota_stats_none_when_music_cog_absent():
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: None))
    assert cog._quota_stats() is None


def test_quota_stats_reads_registry_from_music_cog():
    class _Quotas:
        def stats(self):
            return {"effects_guild": {"hits": 1}}

    music = types.SimpleNamespace(quotas=_Quotas())
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: music))
    assert cog._quota_stats() == {"effects_guild": {"hits": 1}}


def test_webhook_stats_none_when_webstats_cog_absent():
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: None))
    assert cog._webhook_stats() is None


def test_webhook_stats_reads_limiter_from_webstats_cog():
    class _Limiter:
        def stats(self):
            return {"hits": 5, "rejections": 0, "tracked": 1}

    webstats = types.SimpleNamespace(_limiter=_Limiter())
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: webstats))
    assert cog._webhook_stats() == {"hits": 5, "rejections": 0, "tracked": 1}


def test_anilist_stats_none_when_anilist_cog_absent():
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: None))
    assert cog._anilist_stats() is None


def test_anilist_stats_none_when_cog_present_without_throttle():
    # Older/partial wiring: the cog loaded but never set up its throttle.
    anilist = types.SimpleNamespace()
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: anilist))
    assert cog._anilist_stats() is None


def test_anilist_stats_reads_throttled_count_and_global_window():
    throttle = AniListThrottle()
    throttle.note_throttled()
    throttle.note_throttled()
    throttle.allow_global()
    throttle.allow_global()

    anilist = types.SimpleNamespace(_throttle=throttle)
    cog = _cog(bot=types.SimpleNamespace(get_cog=lambda name: anilist))

    assert cog._anilist_stats() == {
        "throttled_429": 2,
        "global_hits": 2,
        "global_rejections": 0,
    }


# --- load_line tick -----------------------------------------------------------


async def _run_tick(cog):
    await health.Health.load_line.coro(cog)


async def test_load_line_logs_info_when_idle_present(caplog):
    cog = _cog(bot=types.SimpleNamespace(
        db_pool=_Pool(size=5, idle=3, max_size=30),
        get_cog=lambda name: None,
    ))
    with caplog.at_level(logging.INFO, logger=health.log.name):
        await _run_tick(cog)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(r.message.startswith("LOAD ") for r in infos)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


async def test_load_line_warns_when_zero_idle(caplog):
    cog = _cog(bot=types.SimpleNamespace(
        db_pool=_Pool(size=30, idle=0, max_size=30),
        get_cog=lambda name: None,
    ))
    with caplog.at_level(logging.INFO, logger=health.log.name):
        await _run_tick(cog)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "zero idle" in warnings[0].message


async def test_load_line_folds_in_anilist_throttle_segment(caplog):
    throttle = AniListThrottle()
    throttle.note_throttled()
    throttle.allow_global()
    anilist = types.SimpleNamespace(_throttle=throttle)

    def _get_cog(name):
        return anilist if name == "AniList" else None

    cog = _cog(bot=types.SimpleNamespace(
        db_pool=_Pool(size=5, idle=3, max_size=30),
        get_cog=_get_cog,
    ))
    with caplog.at_level(logging.INFO, logger=health.log.name):
        await _run_tick(cog)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "anilist=throttled_429=1 global_hits=1 global_rejections=0" in r.message
        for r in infos
    )


async def test_load_line_survives_a_bad_pool_read(caplog):
    class _BrokenPool:
        def get_size(self):
            raise RuntimeError("boom")

    cog = _cog(bot=types.SimpleNamespace(
        db_pool=_BrokenPool(), get_cog=lambda name: None,
    ))
    with caplog.at_level(logging.INFO, logger=health.log.name):
        await _run_tick(cog)  # must not raise
    assert any("load-line tick failed" in r.message for r in caplog.records)
