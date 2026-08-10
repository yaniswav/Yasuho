"""The idle tick must persist player state bounded-concurrently, not one by one.

Every 60 seconds ``Music._idle_check`` refreshes the persisted snapshot of every
PLAYING player (volume / loop / pause / position drift between the event-driven
snapshots). It used to ``await self._snapshot(player)`` inside the loop, so a tick
cost the SUM of its database round trips: at a few hundred active players a
single tick could still be writing when the next one was due, and the idle
disconnects behind it were held up by the writes in front of them.

The writes now go out together through ``Music._persist_snapshots``, the same
shape ``refresh_progress_bars`` already used for the panel edits: bounded fan-out,
isolated per player.

The invariant that made the reordering delicate, and the one these tests care
most about: a player can be BOTH snapshotted and torn down in the same pass (an
idle-expired paused player still has a ``current``), and ``_teardown`` deletes its
music_state row. Writing a snapshot collected before the teardown would resurrect
a dead player and the next restart would rejoin its voice channel and start
playing. The teardown always wins.

Everything here is sonolink-free (fake players, fake controllers), so it runs on
the stub-sonolink dev box and real-sonolink CI alike.
"""

import asyncio
import time
import types

import pytest

from cogs.music import music


class _FakePlayer:
    """Stands in for cogs.music.player.Player - only what the tick reads."""

    def __init__(self, guild_id, *, current="track", paused=False, listeners=1):
        self.guild_id = guild_id
        self.current = current
        self.paused = paused
        self.idle_since = None
        self.controller = None
        self.queue = types.SimpleNamespace(tracks=[])
        members = [types.SimpleNamespace(bot=False) for _ in range(listeners)]
        self.channel = types.SimpleNamespace(
            id=guild_id + 1,
            guild=types.SimpleNamespace(id=guild_id),
            members=members,
        )


def _cog(bot=None, players=()):
    cog = music.Music.__new__(music.Music)
    cog.bot = bot or types.SimpleNamespace(voice_clients=list(players))
    cog._last_quota_log = time.monotonic()
    cog.quotas = types.SimpleNamespace(stats=lambda: {})
    return cog


@pytest.fixture(autouse=True)
def _player_is_our_fake(monkeypatch):
    """The tick's isinstance gate must accept the fakes above."""
    monkeypatch.setattr(music, "Player", _FakePlayer)


@pytest.fixture(autouse=True)
def _no_panel_edits(monkeypatch):
    async def _refresh(controllers):
        return 0

    monkeypatch.setattr(music, "refresh_progress_bars", _refresh)


# ---------------------------------------------------------------------------
# _persist_snapshots: the fan-out policy.
# ---------------------------------------------------------------------------


async def test_an_empty_batch_costs_nothing():
    cog = _cog()
    calls = []

    async def _snapshot(player):
        calls.append(player)

    cog._snapshot = _snapshot

    await cog._persist_snapshots([])

    assert calls == []


async def test_every_player_is_snapshotted_exactly_once():
    cog = _cog()
    players = [_FakePlayer(i) for i in range(7)]
    seen = []

    async def _snapshot(player):
        seen.append(player)

    cog._snapshot = _snapshot

    await cog._persist_snapshots(players)

    assert sorted(player.guild_id for player in seen) == list(range(7))


async def test_the_writes_are_concurrent_not_serial():
    """The regression: serial awaits made a tick as long as the sum of its writes."""
    cog = _cog()
    players = [_FakePlayer(i) for i in range(4)]
    live = 0
    peak = 0

    async def _snapshot(_player):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1

    cog._snapshot = _snapshot

    await cog._persist_snapshots(players)

    assert peak > 1


async def test_the_fan_out_stays_under_the_ceiling():
    cog = _cog()
    players = [_FakePlayer(i) for i in range(20)]
    live = 0
    peak = 0

    async def _snapshot(_player):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.001)
        live -= 1

    cog._snapshot = _snapshot

    await cog._persist_snapshots(players, concurrency=3)

    assert peak <= 3
    # ... and the shipped ceiling is a real, modest bound on the shared pool.
    assert 0 < music.SNAPSHOT_CONCURRENCY <= 10


async def test_one_bad_player_cannot_sink_the_others():
    """Per-player isolation: the batch survives a snapshot that raises."""
    cog = _cog()
    players = [_FakePlayer(i) for i in range(5)]
    done = []

    async def _snapshot(player):
        if player is players[2]:
            raise RuntimeError("this guild's write blew up")
        done.append(player.guild_id)

    cog._snapshot = _snapshot

    await cog._persist_snapshots(players)

    assert sorted(done) == [0, 1, 3, 4]


# ---------------------------------------------------------------------------
# _idle_check: what gets collected, and what must NOT be written back.
# ---------------------------------------------------------------------------


async def test_the_tick_batches_every_playing_player_in_one_call():
    playing = [_FakePlayer(1), _FakePlayer(2)]
    silent = _FakePlayer(3, current=None)
    cog = _cog(players=[*playing, silent])
    batches = []

    async def _persist(players):
        batches.append(list(players))

    cog._persist_snapshots = _persist

    await cog._idle_check()

    assert len(batches) == 1
    assert batches[0] == playing


async def test_a_non_player_voice_client_is_ignored():
    cog = _cog(players=[object(), _FakePlayer(1)])
    batches = []

    async def _persist(players):
        batches.append(list(players))

    cog._persist_snapshots = _persist

    await cog._idle_check()

    assert [p.guild_id for p in batches[0]] == [1]


async def test_a_player_torn_down_this_tick_is_never_written_back():
    """The resurrect bug: _teardown DELETED its row, so the snapshot must be dropped.

    A paused player is idle by definition yet still has a ``current``, so it is
    collected for a snapshot and then disconnected in the very same pass.
    """
    expired = _FakePlayer(1, paused=True)
    expired.idle_since = time.monotonic() - (music.IDLE_TIMEOUT + 1)
    healthy = _FakePlayer(2)
    cog = _cog(players=[expired, healthy])
    batches = []
    torn = []

    async def _persist(players):
        batches.append(list(players))

    async def _teardown(player):
        torn.append(player)

    cog._persist_snapshots = _persist
    cog._teardown = _teardown

    await cog._idle_check()

    assert torn == [expired]
    assert batches == [[healthy]]


async def test_an_idle_player_not_yet_expired_is_still_snapshotted():
    """Idle only starts the clock; nothing is cleared, so the row stays fresh."""
    idle = _FakePlayer(1, paused=True)
    cog = _cog(players=[idle])
    batches = []

    async def _persist(players):
        batches.append(list(players))

    async def _teardown(_player):  # pragma: no cover - must not be reached
        raise AssertionError("a player idle for one tick must not be torn down")

    cog._persist_snapshots = _persist
    cog._teardown = _teardown

    await cog._idle_check()

    assert batches == [[idle]]
    assert idle.idle_since is not None


async def test_a_failing_snapshot_batch_cannot_crash_the_tick():
    """The loop's own guard: an exploding batch must not stop the idle disconnects."""
    cog = _cog(players=[_FakePlayer(1)])

    async def _persist(_players):
        raise RuntimeError("the whole batch blew up")

    cog._persist_snapshots = _persist

    # The tick swallows and logs; the tasks.Loop keeps running.
    await cog._idle_check()
