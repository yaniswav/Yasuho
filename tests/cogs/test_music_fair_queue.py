"""Fair queue: round-robin insertion, DJ priority, the per-member cap.

``_Queue`` mirrors the slice of sonolink 1.4.0's ``Queue`` the code touches
(``tracks`` copy, ``put`` at the end, ``put_at``, ``len``), verified against the
installed source. Every guard has a negative control: with the mode off the
order is plain FIFO.
"""

from __future__ import annotations

import types

import pytest

import cogs.music.music as music_mod
from cogs.music import fairqueue, guild_config
from tools import settings

GID = 7


class _Track:
    def __init__(self, name, requester=None, **extras):
        self.title = name
        self.extras = types.SimpleNamespace(requester=requester, **extras)

    def __repr__(self):
        return self.title


class _Queue:
    def __init__(self, tracks=()):
        self._items = list(tracks)
        self.autoplay_tracks = []
        self.puts = []

    @property
    def tracks(self):
        return list(self._items)

    def __len__(self):
        return len(self._items)

    def put(self, tracks):
        self.puts.append(tracks)
        self._items.extend([tracks] if isinstance(tracks, _Track) else list(tracks))

    def put_at(self, index, track):
        self._items.insert(index, track)


def _names(queue):
    return [t.title for t in queue.tracks]


def _batch(owner, n, queue_prefix=None):
    prefix = queue_prefix or "ABCDEFG"[owner - 1]
    return [_Track("{0}{1}".format(prefix, i + 1), owner) for i in range(n)]


FAIR = fairqueue.QueuePlan(fair=True)


def _add(queue, owner, n, plan=FAIR):
    tracks = _batch(owner, n)
    fairqueue.place(queue, tracks, owner, plan)
    return tracks


def test_two_requesters_interleave():
    q = _Queue()
    _add(q, 1, 3)
    _add(q, 2, 2)
    assert _names(q) == ["A1", "B1", "A2", "B2", "A3"]


def test_three_requesters_round_robin_in_arrival_order():
    q = _Queue()
    _add(q, 1, 1)
    _add(q, 2, 1)
    _add(q, 3, 1)
    _add(q, 1, 1)
    _add(q, 2, 1)
    assert _names(q) == ["A1", "B1", "C1", "A1", "B1"]
    q2 = _Queue()
    for owner in (1, 1, 1, 2, 3, 2, 1):
        _add(q2, owner, 1)
    assert [t.extras.requester for t in q2.tracks] == [1, 2, 3, 1, 2, 1, 1]


def test_bulk_add_is_counted_and_interleaved_with_the_lane():
    q = _Queue(_batch(2, 2))
    _add(q, 1, 3)
    assert _names(q) == ["B1", "A1", "B2", "A2", "A3"]


def test_two_bulk_adds_interleave_fully():
    q = _Queue()
    _add(q, 1, 3)
    _add(q, 2, 3)
    assert _names(q) == ["A1", "B1", "A2", "B2", "A3", "B3"]


def test_existing_fifo_content_is_never_reordered_only_inserted_into():
    legacy = _batch(1, 4) + _batch(2, 1)
    q = _Queue(legacy)
    _add(q, 1, 1)
    # A's new track lands after all of A's tracks, never ahead of one.
    assert _names(q) == ["A1", "A2", "A3", "A4", "B1", "A1"]
    q = _Queue(_batch(1, 4))
    _add(q, 3, 1)
    assert _names(q)[:2] == ["A1", "C1"]


def test_negative_control_fair_off_is_plain_fifo_with_the_legacy_put_shape():
    q = _Queue()
    plan = fairqueue.QueuePlan(fair=False)
    first, second = _batch(1, 3), _batch(2, 2)
    fairqueue.place(q, first, 1, plan)
    fairqueue.place(q, second, 2, plan)
    assert _names(q) == ["A1", "A2", "A3", "B1", "B2"]


def test_dj_priority_puts_manager_tracks_first_fifo_among_managers():
    q = _Queue()
    _add(q, 1, 2)
    manager = fairqueue.QueuePlan(fair=True, dj_priority=True, manager=True)
    first = [_Track("M1", 9)]
    fairqueue.place(q, first, 9, manager)
    second = [_Track("N1", 8)]
    fairqueue.place(q, second, 8, manager)
    assert _names(q) == ["M1", "N1", "A1", "A2"]
    # A regular member still interleaves BEHIND the manager block.
    _add(q, 2, 1)
    assert _names(q) == ["M1", "N1", "A1", "B1", "A2"]


def test_dj_priority_off_manager_is_an_ordinary_requester():
    q = _Queue()
    _add(q, 1, 2)
    plan = fairqueue.QueuePlan(fair=True, dj_priority=False, manager=True)
    fairqueue.place(q, [_Track("M1", 9)], 9, plan)
    assert _names(q) == ["A1", "M1", "A2"]


def test_manager_tracks_are_not_reordered_by_later_regular_adds():
    q = _Queue()
    manager = fairqueue.QueuePlan(fair=True, dj_priority=True, manager=True)
    fairqueue.place(q, [_Track("M1", 9), _Track("M2", 9)], 9, manager)
    _add(q, 1, 3)
    _add(q, 2, 1)
    assert _names(q)[:2] == ["M1", "M2"]


def test_priority_without_fair_still_keeps_regular_adds_fifo_behind_managers():
    q = _Queue()
    plan = fairqueue.QueuePlan(fair=False, dj_priority=True, manager=False)
    fairqueue.place(q, _batch(1, 2), 1, plan)
    mgr = fairqueue.QueuePlan(fair=False, dj_priority=True, manager=True)
    fairqueue.place(q, [_Track("M1", 9)], 9, mgr)
    fairqueue.place(q, _batch(2, 1), 2, plan)
    assert _names(q) == ["M1", "A1", "A2", "B1"]


def test_member_cap_counts_the_lane_and_trims_a_bulk_add():
    lane = _batch(1, 2)
    accepted, refused = fairqueue.apply_member_cap(lane, _batch(1, 3), 1, 4)
    assert len(accepted) == 2 and refused == 1
    accepted, refused = fairqueue.apply_member_cap(lane, _batch(2, 3), 2, 4)
    assert len(accepted) == 3 and refused == 0
    accepted, refused = fairqueue.apply_member_cap(lane, _batch(1, 3), 1, None)
    assert len(accepted) == 3 and refused == 0


def test_placed_messages():
    assert "maximum of 1 track" in fairqueue.Placed([], 1, 1).refusal()
    assert fairqueue.Placed([1], 0, 3).suffix() == ""
    assert "2 tracks were not added" in fairqueue.Placed([1], 2, 3).suffix()


def test_coerce_member_cap():
    c = guild_config.coerce_member_cap
    assert c(5) == 5 and c("7") == 7 and c(500) == 50
    assert c(0) is None and c(-1) is None and c(None) is None
    assert c(True) is None and c("abc") is None and c(float("nan")) is None
    assert c(float("inf")) is None


def test_keys_contract():
    assert guild_config.KEY_FAIR_QUEUE in guild_config.KEYS
    assert guild_config.KEY_DJ_PRIORITY in guild_config.KEYS
    assert guild_config.KEY_MEMBER_CAP in guild_config.KEYS
    assert guild_config.KEY_REQUEST_PANELS not in guild_config.KEYS


# ---------------------------------------------------------------------------
# The cog seam: plan resolution and _put_requested
# ---------------------------------------------------------------------------


@pytest.fixture
def blob(monkeypatch):
    data = {"reads": []}

    async def fake_get(pool, gid, key, default=None):
        data["reads"].append(key)
        return data.get(key, default)

    monkeypatch.setattr(settings, "get_guild", fake_get)
    return data


@pytest.fixture
def plus(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(
        music_mod,
        "resolve_guild_limits",
        lambda bot, gid: types.SimpleNamespace(music_queue_controls=state["on"]),
    )
    return state


def _cog(manager=False):
    cog = object.__new__(music_mod.Music)
    cog.bot = types.SimpleNamespace(db_pool=object())

    async def is_manager(player, actor):
        return manager

    cog._is_music_manager = is_manager
    return cog


def _player(tracks=()):
    return types.SimpleNamespace(
        queue=_Queue(tracks), guild=types.SimpleNamespace(id=GID), channel=None
    )


def _actor(uid):
    return types.SimpleNamespace(id=uid)


async def test_off_costs_one_cached_read_and_no_manager_lookup(blob, plus):
    plus["on"] = False
    cog = _cog()
    player = _player()
    assert await cog._queue_plan(player, _actor(1)) is None
    assert blob["reads"] == [guild_config.KEY_FAIR_QUEUE]


async def test_off_is_byte_identical_fifo_with_the_legacy_put(blob, plus):
    plus["on"] = False
    cog = _cog()
    player = _player()
    one = _Track("A1")
    placed = await cog._put_requested(player, [one], _actor(1))
    assert player.queue.puts == [one]  # a lone track goes in as itself
    batch = [_Track("B1"), _Track("B2")]
    await cog._put_requested(player, batch, _actor(2))
    assert player.queue.puts[-1] == batch
    assert placed.accepted == [one] and one.extras.requester == 1
    assert _names(player.queue) == ["A1", "B1", "B2"]


async def test_fair_mode_on_interleaves_through_the_seam(blob, plus):
    plus["on"] = False
    blob[guild_config.KEY_FAIR_QUEUE] = True
    cog = _cog()
    player = _player()
    await cog._put_requested(player, [_Track("A1"), _Track("A2"), _Track("A3")], _actor(1))
    await cog._put_requested(player, [_Track("B1")], _actor(2))
    assert _names(player.queue) == ["A1", "B1", "A2", "A3"]


async def test_member_cap_refuses_and_exempts_managers(blob, plus):
    blob[guild_config.KEY_MEMBER_CAP] = 2
    player = _player()
    cog = _cog(manager=False)
    placed = await cog._put_requested(player, [_Track("A1"), _Track("A2"), _Track("A3")], _actor(1))
    assert len(placed.accepted) == 2 and placed.capped == 1 and placed.cap == 2
    refused = await cog._put_requested(player, [_Track("A4")], _actor(1))
    assert refused.accepted == [] and refused.capped == 1
    assert "maximum of 2 tracks" in refused.refusal()
    assert _names(player.queue) == ["A1", "A2"]
    boss = _cog(manager=True)
    ok = await boss._put_requested(player, [_Track("M1"), _Track("M2"), _Track("M3")], _actor(9))
    assert len(ok.accepted) == 3 and ok.capped == 0


async def test_lapsed_plus_ignores_stored_priority_and_cap(blob, plus):
    plus["on"] = False
    blob[guild_config.KEY_MEMBER_CAP] = 1
    blob[guild_config.KEY_DJ_PRIORITY] = True
    cog = _cog(manager=True)
    player = _player()
    placed = await cog._put_requested(player, [_Track("A1"), _Track("A2")], _actor(1))
    assert len(placed.accepted) == 2 and placed.capped == 0
    assert guild_config.KEY_MEMBER_CAP not in blob["reads"]
    assert await cog._queue_plan(player, _actor(1)) is None


async def test_dj_priority_through_the_seam_and_autoplay_lane_untouched(blob, plus):
    blob[guild_config.KEY_FAIR_QUEUE] = True
    blob[guild_config.KEY_DJ_PRIORITY] = True
    player = _player()
    player.queue.autoplay_tracks = [_Track("AUTO")]
    await _cog(manager=False)._put_requested(player, [_Track("A1")], _actor(1))
    await _cog(manager=True)._put_requested(player, [_Track("M1")], _actor(9))
    assert _names(player.queue) == ["M1", "A1"]
    assert [t.title for t in player.queue.autoplay_tracks] == ["AUTO"]


async def test_room_is_refitted_after_the_plan_read(blob, plus, monkeypatch):
    plus["on"] = False
    monkeypatch.setattr(music_mod, "MAX_QUEUE_TRACKS", 2)
    cog = _cog()
    player = _player([_Track("X1", 5)])
    placed = await cog._put_requested(player, [_Track("A1"), _Track("A2")], _actor(1))
    assert len(placed.accepted) == 1 and placed.capped == 0


def test_restored_requesterless_queue_is_never_jumped():
    restored = [_Track("R1"), _Track("R2"), _Track("R3")]
    q = _Queue(restored)
    _add(q, 1, 2)
    _add(q, 2, 1)
    assert _names(q) == ["R1", "R2", "R3", "A1", "B1", "A2"]


def test_mixed_restored_queue_keeps_restored_order_and_interleaves_after():
    q = _Queue([_Track("A1", 1), _Track("R1"), _Track("A2", 1)])
    _add(q, 2, 1)
    # Lands after the requester-less track, then interleaves with what follows.
    assert _names(q) == ["A1", "R1", "B1", "A2"]


def test_requesterless_tracks_never_count_toward_a_cap():
    lane = [_Track("R1"), _Track("R2"), _Track("A1", 1)]
    assert fairqueue.count_queued_by(lane, 1) == 1
    assert fairqueue.count_queued_by(lane, None) == 0
    accepted, refused = fairqueue.apply_member_cap(lane, _batch(1, 3), 1, 2)
    assert len(accepted) == 1 and refused == 2


def test_radio_tracks_neither_count_nor_shape_rounds():
    radio = [_Track("S1", 1, radio=True), _Track("S2", 1, radio247=True)]
    assert fairqueue.count_queued_by(radio, 1) == 0
    q = _Queue(radio)
    _add(q, 1, 2)
    _add(q, 2, 1)
    assert _names(q) == ["S1", "S2", "A1", "B1", "A2"]


def test_place_clears_a_stale_dj_flag_on_requeued_tracks():
    stale = _Track("H1", 1, dj_priority=True)
    q = _Queue()
    fairqueue.place(q, [stale], 1, FAIR)
    assert stale.extras.dj_priority is False
    again = _Track("H2", 1, dj_priority=True)
    fairqueue.place(_Queue(), [again], 1, fairqueue.QueuePlan(fair=False))
    assert again.extras.dj_priority is False
    mgr = _Track("M1", 9)
    fairqueue.place(_Queue(), [mgr], 9, fairqueue.QueuePlan(dj_priority=True, manager=True))
    assert mgr.extras.dj_priority is True


async def test_room_refit_drops_are_reported_in_the_suffix(blob, plus, monkeypatch):
    plus["on"] = False
    monkeypatch.setattr(music_mod, "MAX_QUEUE_TRACKS", 2)
    cog = _cog()
    player = _player([_Track("X1", 5)])
    placed = await cog._put_requested(player, [_Track("A1"), _Track("A2"), _Track("A3")], _actor(1))
    assert len(placed.accepted) == 1 and placed.dropped == 2
    assert "2 tracks were not added - the queue is full at 2." in placed.suffix()
