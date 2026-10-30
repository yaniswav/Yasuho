"""24/7 playlist engine (Yasuho+ V3a-2): order, store, gate, refill, triggers.

Real ``Music`` methods on an ``object.__new__`` instance (the test_music_247
approach), a real sonolink ``Queue`` inside a ``Player`` stand-in, and a fake
pool that answers the three radio statements by their SQL. Every gate
condition has a negative control next to its positive one, and every race in
the design's "Races" section is either closed here or pinned to its benign
outcome.
"""

from __future__ import annotations

import asyncio
import types

import pytest
import sonolink
from sonolink.gateway.queue.queue import Queue
from sonolink.models.track import Playable

import cogs.music.music as music_mod
from cogs.music import always_on as ao
from cogs.music import playlists_shared as ps
from cogs.music import vibes
from cogs.music.player import Player

GID = 7
NORM = "chill"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _track(blob, *, radio=False, autoplay=False, title=None, length=180_000):
    """A real ``Playable`` (so the real Queue accepts it) with no node behind it."""
    track = Playable.__new__(Playable)
    track._data = types.SimpleNamespace(
        encoded=blob,
        info=types.SimpleNamespace(
            identifier=blob,
            title=title or blob,
            author="a",
            length=length,
            is_stream=False,
        ),
    )
    track._autoplay = autoplay
    track._playlist = None
    track._extras = types.SimpleNamespace()
    if radio:
        track._extras.radio247 = True
    return track


class _Member:
    def __init__(self, member_id, *, bot=False, guild=None):
        self.id = member_id
        self.bot = bot
        self.guild = guild


class _Player(Player):
    """``isinstance(x, Player)`` with a REAL sonolink queue; skips __init__."""

    def __init__(self, guild, *, humans=1, current=None, paused=False):
        self._queue = Queue()
        if current is not None:
            self._queue._current_track = current
        self._paused = paused
        members = [_Member(100 + i) for i in range(humans)] + [_Member(1, bot=True)]
        self.channel = types.SimpleNamespace(id=10, guild=guild, members=members)
        self.home = None
        self.controller = None
        self.dj = None
        self.idle_since = None
        self.no_listener_since = None
        self.radio247_hold = False
        self._radio247_refilling = False
        self.radio247_retry_at = 0.0
        self.played_ids = set()
        self._automix_skips = 0
        self.default_effect_done = True
        self.radio_genre = None
        self.plays = []
        self.stops = []

    async def play(self, track, *, paused=None, **kwargs):
        self.plays.append((track, paused))
        self._queue.current_track = track
        return track

    async def stop(self, *, clear_queue=False, clear_history=False):
        self.stops.append(clear_queue)
        self._queue.current_track = None
        if clear_queue:
            self._queue.clear()
            self._queue._autoplay_items.clear()

    async def skip(self, **kwargs):
        raise AssertionError("skip() must not run here")


class _RadioPool:
    """Answers the radio statements by SQL text, like the real tables would."""

    def __init__(self, tracks, *, pos=0, shuffle=False):
        self.tracks = tracks  # None = playlist gone
        self.pos = pos
        self.shuffle = shuffle
        self.norm = NORM
        self.calls = []
        self.on_fetchval = None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.tracks is None or args[1] != self.norm:
            return None
        return {
            "radio_pos": self.pos,
            "radio_shuffle": self.shuffle,
            "track_count": len(self.tracks),
        }

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if self.on_fetchval is not None:
            self.on_fetchval()
        return [
            self.tracks[i - 1] if 0 < i <= len(self.tracks) else None
            for i in args[2]
        ]

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if "radio_pos = $4" in query:
            _gid, norm, old, new = args
            if norm == self.norm and old == self.pos:
                self.pos = new
                return "UPDATE 1"
            return "UPDATE 0"
        return "UPDATE 1"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return []

    def kinds(self):
        return [call[0] for call in self.calls]


class _Bot:
    def __init__(self, pool):
        self.db_pool = pool
        self.guilds = {}
        self.voice_clients = []
        self.user = types.SimpleNamespace(id=1)
        self.decoded = []
        self.on_decode = None
        self.sl_client = types.SimpleNamespace(decode_tracks=self._decode)

    async def _decode(self, *blobs):
        self.decoded.append(blobs)
        if self.on_decode is not None:
            self.on_decode()
        return [_track(blob) for blob in blobs]

    def get_guild(self, guild_id):
        return self.guilds.get(guild_id)


@pytest.fixture
def premium(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(
        music_mod,
        "resolve_guild_limits",
        lambda bot, gid: types.SimpleNamespace(music_247=state["on"]),
    )
    return state


async def _noop(*args, **kwargs):
    return None


def _env(premium, *, tracks=None, radio=True, humans=1, current=None, paused=False):
    pool = _RadioPool([f"b{i}" for i in range(25)] if tracks is None else tracks)
    bot = _Bot(pool)
    cog = object.__new__(music_mod.Music)
    cog.bot = bot
    cog._restore_done = True
    cog._refill_tasks = set()
    cog._controllers = {}
    cog._pending_watches = vibes.PendingVoiceWatches()
    cog.lyrics_sessions = types.SimpleNamespace(notify_track=_noop)
    cog.skip_votes = types.SimpleNamespace(notify_track=_noop)
    cog._snapshot = _noop
    cog._send_controller = _noop
    cog._persist_snapshots = _noop
    cog._last_quota_log = 10**12
    store = ao.AlwaysOnStore()
    store._loaded = True
    store._channels[GID] = 10
    if radio:
        store._radio[GID] = (NORM, False)
    cog.always_on = store
    guild = types.SimpleNamespace(id=GID)
    player = _Player(guild, humans=humans, current=current, paused=paused)
    guild.voice_client = player
    bot.guilds[GID] = guild
    bot.voice_clients.append(player)
    return cog, player, pool


async def _drain(cog):
    while cog._refill_tasks:
        await asyncio.gather(*list(cog._refill_tasks))


async def _refill(cog, player):
    player._radio247_refilling = True
    await music_mod.Music._radio247_refill(cog, player, GID)


def _kick(cog, player):
    return music_mod.Music._kick_radio247_if_idle(cog, player, GID)


# ---------------------------------------------------------------------------
# radio247_positions - the play order
# ---------------------------------------------------------------------------


def test_in_order_wraps_into_the_next_cycle():
    assert ao.radio247_positions(GID, 3, 2, 4, False) == [2, 0, 1, 2]


def test_shuffle_cycle_is_a_permutation_with_no_repeat():
    first = ao.radio247_positions(GID, 7, 0, 7, True)
    second = ao.radio247_positions(GID, 7, 7, 7, True)
    assert sorted(first) == list(range(7))
    assert sorted(second) == list(range(7))


def test_same_seed_same_order_and_batches_compose():
    # A restart keeps only radio_pos: the order must not depend on how the
    # positions were cut into batches.
    whole = ao.radio247_positions(GID, 9, 0, 10, True)
    split = ao.radio247_positions(GID, 9, 0, 4, True) + ao.radio247_positions(
        GID, 9, 4, 6, True
    )
    assert whole == split
    assert whole == ao.radio247_positions(GID, 9, 0, 10, True)


def test_shuffle_differs_per_guild():
    orders = {tuple(ao.radio247_positions(g, 20, 0, 20, True)) for g in range(5)}
    assert len(orders) > 1


def test_empty_playlist_and_short_playlist():
    assert ao.radio247_positions(GID, 0, 0, 10, True) == []
    assert ao.radio247_positions(GID, 5, 0, 0, True) == []
    short = ao.radio247_positions(GID, 2, 0, 10, False)
    assert short == [0, 1] * 5


# ---------------------------------------------------------------------------
# AlwaysOnStore - the radio choice cache
# ---------------------------------------------------------------------------


async def test_store_loads_the_radio_choice(fake_pool):
    fake_pool.fetch_return = [
        {"guild_id": 1, "channel_id": 10, "enabled_at": None,
         "radio_playlist": "chill", "radio_shuffle": False},
        {"guild_id": 2, "channel_id": 20, "enabled_at": None,
         "radio_playlist": None, "radio_shuffle": True},
    ]
    store = ao.AlwaysOnStore()
    await store.ensure_loaded(fake_pool)
    assert store.radio(1) == ("chill", False)
    assert store.radio(2) is None
    assert store.radio(None) is None
    assert "radio_playlist" in fake_pool.calls[0][1]


async def test_set_radio_needs_a_247_row(fake_pool):
    store = ao.AlwaysOnStore()
    fake_pool.execute_return = "UPDATE 0"
    assert await store.set_radio(fake_pool, 1, "chill", True) is False
    assert store.radio(1) is None
    fake_pool.execute_return = "UPDATE 1"
    assert await store.set_radio(fake_pool, 1, "chill", False) is True
    assert store.radio(1) == ("chill", False)
    query, args = fake_pool.calls[-1][1], fake_pool.calls[-1][2]
    assert "radio_pos = 0" in query
    assert args == (1, "chill", False)
    assert await store.set_radio(fake_pool, 1, None) is True
    assert store.radio(1) is None


def test_retarget_and_evict():
    store = ao.AlwaysOnStore()
    store._radio[1] = ("chill", True)
    store.retarget_radio(1, "other", "x")  # not the radio playlist: no-op
    assert store.radio(1) == ("chill", True)
    store.retarget_radio(1, "chill", "lofi")
    assert store.radio(1) == ("lofi", True)
    store.retarget_radio(1, "lofi", None)
    assert store.radio(1) is None
    store._radio[1] = ("chill", True)
    store._channels[1] = 10
    store.evict(1)
    assert store.radio(1) is None


class _PlaylistCog(ps.ServerPlaylistMixin):
    def __init__(self, pool):
        self.bot = types.SimpleNamespace(db_pool=pool)
        self.always_on = ao.AlwaysOnStore()
        self.always_on._radio[GID] = (NORM, True)


async def test_playlist_rename_moves_the_radio(fake_pool):
    cog = _PlaylistCog(fake_pool)
    await cog._rename_guild_playlist(GID, NORM, "Lofi", "lofi")
    assert cog.always_on.radio(GID) == ("lofi", True)
    radio_updates = [c for c in fake_pool.calls if "music_247" in c[1]]
    assert radio_updates and radio_updates[0][2] == (GID, NORM, "lofi")


async def test_playlist_case_only_rename_leaves_the_radio(fake_pool):
    cog = _PlaylistCog(fake_pool)
    await cog._rename_guild_playlist(GID, NORM, "CHILL", NORM)
    assert cog.always_on.radio(GID) == (NORM, True)
    assert not [c for c in fake_pool.calls if "music_247" in c[1]]


async def test_playlist_delete_clears_the_radio(fake_pool):
    fake_pool.execute_return = "DELETE 1"
    cog = _PlaylistCog(fake_pool)
    assert await cog._delete_guild_playlist(GID, NORM) is True
    assert cog.always_on.radio(GID) is None
    radio_updates = [c for c in fake_pool.calls if "music_247" in c[1]]
    assert radio_updates[0][2] == (GID, NORM, None)


async def test_deleting_another_playlist_keeps_the_radio(fake_pool):
    cog = _PlaylistCog(fake_pool)
    await cog._delete_guild_playlist(GID, "other")
    assert cog.always_on.radio(GID) == (NORM, True)


# ---------------------------------------------------------------------------
# Real sonolink queue: the lane the radio rides
# ---------------------------------------------------------------------------


def test_user_track_plays_before_the_radio_lane():
    queue = Queue()
    queue.put_autoplay([_track("r1", radio=True), _track("r2", radio=True)])
    user = _track("u1")
    queue.put(user)
    assert queue.get() is user
    assert queue.get().encoded == "r1"


def test_loop_all_cycles_history_and_never_reaches_the_radio():
    # Accepted by design: LOOP_ALL is the member's explicit choice.
    queue = Queue()
    queue.mode = sonolink.QueueMode.LOOP_ALL
    queue._history._push(_track("h1"))
    queue.put_autoplay([_track("r1", radio=True)])
    assert queue.get().encoded == "h1"
    assert [t.encoded for t in queue.autoplay_tracks] == ["r1"]


async def test_native_autoplay_never_fires_while_the_radio_lane_holds_tracks():
    from sonolink.gateway.player.handlers._playback import PlaybackHandler

    queue = Queue()
    queue.put_autoplay([_track("r1", radio=True)])
    played = []

    class _Handler(PlaybackHandler):
        __slots__ = ()

        async def play(self, track, **kwargs):
            played.append(track)
            return track

    async def _no_autoplay():
        raise AssertionError("native autoplay must not run")

    fake_player = types.SimpleNamespace(
        queue=queue,
        _autoplay_handler=types.SimpleNamespace(
            auto_play=_no_autoplay,
            _settings=types.SimpleNamespace(mode=sonolink.AutoPlayMode.ENABLED),
        ),
    )
    handler = _Handler(fake_player)
    track = await handler.skip()
    assert track.encoded == "r1"
    assert played == [track]


async def test_real_stop_with_clear_queue_empties_the_radio_lane():
    from sonolink.gateway.player.handlers._playback import PlaybackHandler

    queue = Queue()
    queue.put_autoplay([_track("r1", radio=True), _track("r2", radio=True)])
    fake_player = types.SimpleNamespace(
        node=types.SimpleNamespace(
            _resume_session="s",
            _manager=types.SimpleNamespace(update_player=_noop),
        ),
        guild=types.SimpleNamespace(id=GID),
        _queue=queue,
        _check_inactivity=lambda: None,
    )
    await PlaybackHandler(fake_player).stop(clear_queue=True)
    assert queue.autoplay_tracks == []


def test_drop_staged_autoplay_both_ways():
    queue = Queue()
    queue.put_autoplay([_track("a1"), _track("r1", radio=True), _track("a2")])
    music_mod.drop_staged_autoplay(queue, keep_radio247=True)
    assert [t.encoded for t in queue.autoplay_tracks] == ["r1"]
    queue.put_autoplay([_track("a3")])
    music_mod.drop_staged_autoplay(queue, keep_radio247=False)
    assert [t.encoded for t in queue.autoplay_tracks] == ["a3"]


# ---------------------------------------------------------------------------
# is_autoplay_track / should_start_now
# ---------------------------------------------------------------------------


def test_radio_track_is_not_an_autoplay_recommendation():
    queue = Queue()
    radio = _track("r1", radio=True, title="1 HOUR MIX", length=3_600_000)
    queue.put_autoplay([radio])
    assert radio.autoplay is True  # sonolink stamps the lane flag on it
    assert music_mod.is_autoplay_track(radio) is False
    assert music_mod.decide_anti_mix_skip(
        music_mod.is_autoplay_track(radio), True, 0
    ) == (False, 0)


def test_negative_control_plain_autoplay_is_still_flagged_and_mix_skipped():
    pick = _track("a1", autoplay=True)
    assert music_mod.is_autoplay_track(pick) is True
    assert music_mod.decide_anti_mix_skip(
        music_mod.is_autoplay_track(pick), True, 0
    ) == (True, 1)
    assert music_mod.is_autoplay_track(None) is False


def test_should_start_now():
    guild = types.SimpleNamespace(id=GID)
    assert music_mod.should_start_now(_Player(guild)) is True
    radio = _Player(guild, current=_track("r", radio=True))
    assert music_mod.should_start_now(radio) is True
    # Negative control: a member's track is never interrupted.
    user = _Player(guild, current=_track("u"))
    assert music_mod.should_start_now(user) is False


# ---------------------------------------------------------------------------
# The gate: every condition, positive and negative
# ---------------------------------------------------------------------------


async def test_gate_positive_control(premium):
    cog, player, _pool = _env(premium)
    assert _kick(cog, player) is True
    await _drain(cog)
    assert player.plays


@pytest.mark.parametrize(
    "breaker",
    [
        "not_247",
        "suspended",
        "lapsed",
        "held",
        "paused",
        "no_listener",
        "restore_not_done",
        "backoff",
        "no_playlist",
        "refilling",
        "playing",
    ],
)
async def test_gate_negative_controls(premium, breaker):
    cog, player, pool = _env(premium)
    if breaker == "not_247":
        cog.always_on._channels.clear()
    elif breaker == "suspended":
        cog.always_on.suspend(GID)
    elif breaker == "lapsed":
        premium["on"] = False
    elif breaker == "held":
        player.radio247_hold = True
    elif breaker == "paused":
        player._paused = True
    elif breaker == "no_listener":
        player.channel.members = [m for m in player.channel.members if m.bot]
    elif breaker == "restore_not_done":
        cog._restore_done = False
    elif breaker == "backoff":
        player.radio247_retry_at = 10**12
    elif breaker == "no_playlist":
        cog.always_on._radio.clear()
    elif breaker == "refilling":
        player._radio247_refilling = True
    elif breaker == "playing":
        player._queue._current_track = _track("u")
    assert _kick(cog, player) is False
    assert not cog._refill_tasks
    assert pool.calls == []


async def test_free_247_without_a_radio_playlist_never_refills_or_queries(premium):
    """Free behaviour unchanged: 24/7 with no playlist - every trigger site
    runs and none of them schedules anything or touches the pool."""
    cog, player, pool = _env(premium, radio=False)
    await music_mod.Music._idle_check.coro(cog)
    event = types.SimpleNamespace(track=_track("u1"))
    player._queue._current_track = event.track
    await music_mod.Music.on_sonolink_track_start(cog, player, event)
    player._queue._current_track = None
    member = _Member(500, guild=types.SimpleNamespace(id=GID, voice_client=player))
    player.channel.members.append(member)
    await music_mod.Music.on_voice_state_update(
        cog,
        member,
        types.SimpleNamespace(channel=None),
        types.SimpleNamespace(channel=player.channel),
    )
    assert not cog._refill_tasks
    assert pool.calls == []
    assert cog.bot.decoded == []
    assert player.plays == []


# ---------------------------------------------------------------------------
# The refill
# ---------------------------------------------------------------------------


async def test_refill_stages_a_batch_and_starts_an_idle_player(premium):
    cog, player, pool = _env(premium)
    player._queue.put_autoplay([_track("stale-pick")])  # native autoplay staged
    await _refill(cog, player)
    # Two reads, one compare-and-set write for the whole batch of 10.
    assert pool.kinds() == ["fetchrow", "fetchval", "execute"]
    assert pool.pos == music_mod.RADIO247_BATCH
    assert list(pool.calls[1][2][2]) == list(range(1, 11))
    (track, paused), = player.plays
    assert paused is False
    assert track.encoded == "b0"
    assert music_mod.is_radio247_track(track)
    lane = player.queue.autoplay_tracks
    assert [t.encoded for t in lane] == [f"b{i}" for i in range(1, 10)]
    assert all(music_mod.is_radio247_track(t) for t in lane)
    assert player._radio247_refilling is False
    assert player.radio247_retry_at == 0.0


async def test_refill_only_stages_when_something_is_playing(premium):
    cog, player, pool = _env(premium, current=_track("u"))
    await _refill(cog, player)
    assert player.plays == []
    assert len(player.queue.autoplay_tracks) == music_mod.RADIO247_BATCH


async def test_refill_only_stages_when_paused(premium):
    cog, player, pool = _env(premium, paused=True)
    await _refill(cog, player)
    assert player.plays == []
    assert player.current is None


@pytest.mark.parametrize("change", ["hold", "playlist", "lapse", "replaced"])
async def test_refill_discards_when_the_world_moves_mid_await(premium, change):
    cog, player, pool = _env(premium)

    def mutate():
        if change == "hold":
            player.radio247_hold = True  # /stop landed during the decode
        elif change == "playlist":
            cog.always_on.retarget_radio(GID, NORM, "other")
        elif change == "lapse":
            premium["on"] = False
        elif change == "replaced":
            cog.bot.guilds[GID].voice_client = object()

    cog.bot.on_decode = mutate
    await _refill(cog, player)
    assert player.plays == []
    assert player.queue.autoplay_tracks == []
    # Re-checked before the compare-and-set: no position is spent either.
    assert "execute" not in pool.kinds()
    assert pool.pos == 0
    # A discard is not a failure: no backoff.
    assert player.radio247_retry_at == 0.0


async def test_refill_discards_a_hold_set_during_the_cas(premium):
    cog, player, pool = _env(premium)
    real_execute = pool.execute

    async def execute(query, *args):
        result = await real_execute(query, *args)
        player.radio247_hold = True
        return result

    pool.execute = execute
    await _refill(cog, player)
    assert player.plays == []
    assert player.queue.autoplay_tracks == []
    # Benign: the batch's positions were spent (the radio skips ahead).
    assert pool.pos == music_mod.RADIO247_BATCH


async def test_refill_skips_when_radio_pos_moved(premium):
    cog, player, pool = _env(premium)

    def other_writer():
        pool.pos = 3

    pool.on_fetchval = other_writer
    await _refill(cog, player)
    assert player.plays == []
    assert player.queue.autoplay_tracks == []
    assert pool.pos == 3
    assert player.radio247_retry_at == 0.0


async def test_refill_respects_the_queue_cap(premium):
    cog, player, pool = _env(premium, current=_track("u"))
    room = 3
    player._queue._items.extend(
        _track(f"q{i}") for i in range(music_mod.MAX_QUEUE_TRACKS - room)
    )
    await _refill(cog, player)
    assert len(pool.calls[1][2][2]) == room
    assert music_mod.queued_track_count(player.queue) == music_mod.MAX_QUEUE_TRACKS
    assert pool.pos == room


async def test_full_queue_backs_off_with_no_io(premium):
    cog, player, pool = _env(premium, current=_track("u"))
    player._queue._items.extend(
        _track(f"q{i}") for i in range(music_mod.MAX_QUEUE_TRACKS)
    )
    await _refill(cog, player)
    assert pool.calls == []
    assert player.radio247_retry_at > 0


@pytest.mark.parametrize("tracks", [None, []])
async def test_missing_or_empty_playlist_backs_off(premium, tracks):
    cog, player, pool = _env(premium)
    pool.tracks = tracks  # None = the playlist row is gone
    await _refill(cog, player)
    assert pool.kinds() == ["fetchrow"]
    assert player.radio247_retry_at > 0
    assert player._radio247_refilling is False
    # ...and the gate then refuses until the deadline.
    assert _kick(cog, player) is False


async def test_undecodable_batch_advances_and_backs_off(premium):
    cog, player, pool = _env(premium)

    async def decode(*blobs):
        return [None for _ in blobs]

    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=decode)
    await _refill(cog, player)
    assert pool.pos == music_mod.RADIO247_BATCH  # never pinned on bad blobs
    assert player.plays == []
    assert player.radio247_retry_at > 0


async def test_no_node_keeps_the_position(premium):
    cog, player, pool = _env(premium)

    async def decode(*blobs):
        raise RuntimeError("no node")

    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=decode)
    await _refill(cog, player)
    assert pool.pos == 0
    assert "execute" not in pool.kinds()
    assert player.radio247_retry_at > 0


async def test_two_triggers_schedule_one_refill(premium):
    cog, player, _pool = _env(premium)
    assert _kick(cog, player) is True
    assert _kick(cog, player) is False
    assert len(cog._refill_tasks) == 1
    await _drain(cog)


async def test_refill_vs_user_play_from_idle_is_benign(premium):
    """The documented race: the refill and a /play both start from idle. The
    user's track wins (Q1) and nothing is lost but the radio track that was
    popped - it went to history, the rest of the batch still waits."""
    cog, player, _pool = _env(premium)
    await _refill(cog, player)
    user = _track("u1")
    player.queue.put(user)
    assert music_mod.should_start_now(player) is True
    await player.play(player.queue.get(), paused=False)
    assert player.current is user
    assert len(player.queue.autoplay_tracks) == music_mod.RADIO247_BATCH - 1


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


async def _start(cog, player, track):
    player._queue._current_track = track
    await music_mod.Music.on_sonolink_track_start(
        cog, player, types.SimpleNamespace(track=track)
    )


async def test_track_start_restocks_a_low_lane(premium):
    cog, player, pool = _env(premium)
    await _start(cog, player, _track("u1"))
    assert len(cog._refill_tasks) == 1
    await _drain(cog)
    assert len(player.queue.autoplay_tracks) == music_mod.RADIO247_BATCH
    assert player.plays == []  # something is playing: staged only


async def test_track_start_leaves_a_stocked_lane_alone(premium):
    cog, player, pool = _env(premium)
    player.queue.put_autoplay([_track("r1", radio=True), _track("r2", radio=True)])
    await _start(cog, player, _track("u1"))
    assert not cog._refill_tasks
    assert pool.calls == []


async def test_a_human_track_lifts_the_stop_hold(premium):
    cog, player, _pool = _env(premium)
    player.radio247_hold = True
    await _start(cog, player, _track("u1"))
    assert player.radio247_hold is False
    assert len(cog._refill_tasks) == 1
    await _drain(cog)


async def test_negative_control_a_radio_track_keeps_the_hold(premium):
    cog, player, pool = _env(premium)
    player.radio247_hold = True
    await _start(cog, player, _track("r1", radio=True))
    assert player.radio247_hold is True
    assert not cog._refill_tasks
    assert pool.calls == []


async def test_idle_tick_starts_the_playlist_for_a_listener(premium):
    cog, player, _pool = _env(premium)
    await music_mod.Music._idle_check.coro(cog)
    await _drain(cog)
    assert len(player.plays) == 1


async def test_idle_tick_negative_control_empty_room(premium):
    cog, player, pool = _env(premium, humans=0)
    await music_mod.Music._idle_check.coro(cog)
    assert not cog._refill_tasks
    assert pool.calls == []


async def test_human_joining_an_idle_room_starts_the_playlist(premium):
    cog, player, _pool = _env(premium, humans=0)
    member = _Member(500, guild=types.SimpleNamespace(id=GID, voice_client=player))
    player.channel.members.append(member)
    await music_mod.Music.on_voice_state_update(
        cog,
        member,
        types.SimpleNamespace(channel=None),
        types.SimpleNamespace(channel=player.channel),
    )
    assert len(cog._refill_tasks) == 1
    await _drain(cog)
    assert len(player.plays) == 1


async def test_stop_holds_the_playlist(premium):
    cog, player, _pool = _env(premium, current=_track("r", radio=True))

    async def require(ctx, **kwargs):
        return player

    sent = []

    async def send(*args, **kwargs):
        sent.append(args)

    cog._require_player = require
    cog._clear = _noop
    ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=GID), send=send)
    await music_mod.Music.stop.callback(cog, ctx)
    assert player.radio247_hold is True
    assert player.stops == [True]
    # The idle room then stays silent until a human starts something.
    assert _kick(cog, player) is False


async def test_refresh_drops_the_old_playlist_and_restocks(premium):
    cog, player, _pool = _env(premium, current=_track("r0", radio=True))
    player.queue.put_autoplay([_track("old", radio=True), _track("pick")])
    player.radio247_hold = True
    player.radio247_retry_at = 10**12
    assert music_mod.Music.radio247_refresh(cog, GID) is True
    # Old playlist tracks gone, a native pick kept until the refill replaces it.
    await _drain(cog)
    lane = [t.encoded for t in player.queue.autoplay_tracks]
    assert "old" not in lane and "pick" not in lane
    assert len(lane) == music_mod.RADIO247_BATCH
    assert player.radio247_hold is False


async def test_refresh_with_the_playlist_off_only_drops(premium):
    cog, player, pool = _env(premium, current=_track("r0", radio=True))
    cog.always_on._radio.clear()
    player.queue.put_autoplay([_track("old", radio=True)])
    assert music_mod.Music.radio247_refresh(cog, GID) is False
    assert player.queue.autoplay_tracks == []
    assert pool.calls == []


async def test_maybe_restore_opens_the_gate_only_after_the_bare_joins(premium):
    cog, player, _pool = _env(premium)
    cog._restore_done = False
    cog._restored = False
    seen = []

    async def players():
        seen.append(("players", cog._restore_done))

    async def bare():
        seen.append(("bare", cog._restore_done))
        raise RuntimeError("boom")  # the gate still opens (finally)

    cog._restore_players = players
    cog._restore_247_bare_joins = bare
    cog._nodes_connected = lambda: True
    cog.bot.is_ready = lambda: True
    await music_mod.Music._maybe_restore(cog)
    assert seen == [("players", False), ("bare", False)]
    assert cog._restore_done is True
