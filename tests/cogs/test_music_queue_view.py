"""Unit tests for the queue browser's per-track actions (lot M1).

The queue view was read-only: ten upcoming tracks a page, Prev / Next / Add /
Clear. This lot adds the actions a listener actually wants while looking at that
list - jump to a track, drop one, shuffle the rest - without loosening the gate
split the surface already had:

* browsing (paging, reading) stays open to the room (``_ensure_in_voice``);
* everything that CHANGES playback or the queue (jump, remove, shuffle, clear)
  goes through ``_ensure_can_control`` in its own callback;
* every mutation is followed by a snapshot, so a restart restores what the room
  can see (shuffle included - a reordered queue that is not persisted comes back
  in the old order).

What is pinned here:

* the two new pure helpers - ``queued_track_at`` (the ONE stale re-check: range,
  then identity) and ``remove_queue_index`` (index-faithful removal, the
  duplicate-song counter-test);
* the rendered surface - one option per listed line, ABSOLUTE queue indexes as
  values, no select on an empty queue, the Shuffle button disabled under two
  tracks, and the timeout disabling the select as well as the buttons;
* the action wiring - the DJ gate on every action, the stale-click refusal
  (never an IndexError), the jump's ``pop_at`` + direct ``play()`` seam (the
  ``_play_previous`` shape: REPLACED end reason, no autoplay fire), the snapshot
  after every mutation, and the in-place re-render.

Live-only (needs Lavalink and a real Discord message, so exercised on the
server): the actual REPLACED end-reason event, the ephemeral panel's timeout
edit against a real interaction, and the visual layout. Everything here runs on
fakes, so it passes under the stub sonolink dev box and real-sonolink CI alike.
"""

import collections
import types

import discord
import pytest

from cogs.music import music, views

# ---------------------------------------------------------------------------
# Fakes: a queue shaped exactly like sonolink's (a ``_items`` deque behind a
# ``tracks`` list copy, a ``pop_at`` that removes AND promotes), tracks that
# compare by their ``encoded`` blob like ``Playable``, and a recording cog.
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title, encoded=None, author="Artist", length=125000):
        self.title = title
        self.author = author
        self.length = length
        self.is_stream = False
        # sonolink compares tracks by this blob; two copies of the same song
        # share it, which is exactly what makes a value-based remove ambiguous.
        self.encoded = encoded if encoded is not None else title
        self.extras = types.SimpleNamespace(requester=None)

    def __eq__(self, other):
        return getattr(other, "encoded", None) == self.encoded

    def __hash__(self):
        return hash(self.encoded)

    def __repr__(self):
        return "<Track {0}>".format(self.title)


class _Queue:
    """Mirrors the sonolink ``Queue`` surface the view touches."""

    def __init__(self, tracks=()):
        self._items = collections.deque(tracks)
        self._autoplay_items = collections.deque()
        self.current_track = None
        self.history = []

    @property
    def tracks(self):
        return list(self._items)

    @property
    def autoplay_tracks(self):
        return list(self._autoplay_items)

    def pop_at(self, index):
        track = self._items[index]
        del self._items[index]
        if self.current_track is not None:
            self.history.append(self.current_track)
        self.current_track = track
        return track

    def shuffle(self):
        # Deterministic stand-in for random.sample: any observable reorder.
        self._items.reverse()

    def clear(self):
        self._items.clear()


class _Player:
    def __init__(self, tracks=(), current=None, dj=None):
        self.queue = _Queue(tracks)
        self.current = current
        self.channel = types.SimpleNamespace(name="General")
        self.dj = dj
        self.played = []

    async def play(self, track):
        self.played.append(track)
        self.queue.current_track = track
        self.current = track


class _Cog:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.snapshots = []

    async def _can_control(self, player, actor):
        return self.allowed

    async def _snapshot(self, player, track=None):
        self.snapshots.append(track)


class _Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def _tracks(*titles):
    return [_Track(t) for t in titles]


def _view(player, cog=None, *, page=0):
    view = views.QueueView(cog if cog is not None else _Cog(), player)
    view.page = page
    view._build()
    view.message = _Message()
    return view


def _select(view):
    for child in view.walk_children():
        if isinstance(child, views._QueueTrackSelect):
            return child
    return None


def _buttons(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def _dj(dj_id=5):
    return types.SimpleNamespace(id=dj_id, mention="<@{0}>".format(dj_id))


# ---------------------------------------------------------------------------
# queued_track_at - the single stale re-check
# ---------------------------------------------------------------------------


def test_queued_track_at_resolves_the_index():
    queue = _Queue(_tracks("A", "B", "C"))
    assert music.queued_track_at(queue, 1).title == "B"


def test_queued_track_at_refuses_an_index_past_the_end():
    # The queue shrank under a rendered page: None, never an IndexError.
    queue = _Queue(_tracks("A", "B"))
    assert music.queued_track_at(queue, 2) is None


def test_queued_track_at_refuses_a_negative_index():
    queue = _Queue(_tracks("A", "B"))
    assert music.queued_track_at(queue, -1) is None


def test_queued_track_at_refuses_a_non_integer_index():
    queue = _Queue(_tracks("A"))
    assert music.queued_track_at(queue, "0") is None
    assert music.queued_track_at(queue, True) is None


def test_queued_track_at_is_none_safe_on_an_empty_queue():
    assert music.queued_track_at(types.SimpleNamespace(tracks=None), 0) is None
    assert music.queued_track_at(_Queue(), 0) is None


def test_queued_track_at_refuses_when_the_slot_holds_another_track():
    # The queue advanced: index 1 is still addressable but now holds a DIFFERENT
    # song, so acting on it would hit a track the member never picked.
    queue = _Queue(_tracks("A", "B", "C"))
    picked = _Track("B")
    queue._items.popleft()
    assert music.queued_track_at(queue, 1, picked) is None


def test_queued_track_at_accepts_an_equal_duplicate_at_the_slot():
    # Two byte-identical copies compare equal (Playable.__eq__ on ``encoded``);
    # the slot still holds exactly the song the member picked, so it passes.
    first, second = _Track("X"), _Track("X")
    queue = _Queue([_Track("A"), second])
    assert music.queued_track_at(queue, 1, first) is second


# ---------------------------------------------------------------------------
# remove_queue_index - index-faithful removal (the duplicate-song counter-test)
# ---------------------------------------------------------------------------


def test_remove_queue_index_removes_the_picked_index():
    queue = _Queue(_tracks("A", "B", "C"))
    removed = music.remove_queue_index(queue, 1)
    assert removed.title == "B"
    assert [t.title for t in queue.tracks] == ["A", "C"]


def test_remove_queue_index_is_index_faithful_with_duplicate_songs():
    """The whole reason a value-based remove could not be used.

    ``Queue.remove(track, remove_all=False)`` drops the FIRST equal item, so a
    click on the SECOND copy of a song would delete the first - leaving a
    different playback ORDER, not just a different object.
    """
    first, second = _Track("X"), _Track("X")
    queue = _Queue([_Track("A"), _Track("B"), first, _Track("C"), _Track("D"), second])

    removed = music.remove_queue_index(queue, 5, second)

    assert removed is second
    assert [t.title for t in queue.tracks] == ["A", "B", "X", "C", "D"]
    # What a first-occurrence removal would have produced - a different order.
    assert [t.title for t in queue.tracks] != ["A", "B", "C", "D", "X"]
    assert queue.tracks[2] is first


def test_remove_queue_index_refuses_a_stale_index_without_mutating():
    queue = _Queue(_tracks("A", "B"))
    assert music.remove_queue_index(queue, 7) is None
    assert [t.title for t in queue.tracks] == ["A", "B"]


def test_remove_queue_index_refuses_when_the_slot_moved_on():
    # The member picked "B" at index 1; the queue then advanced, so index 1 now
    # holds "C". Removing it would drop a song nobody asked to drop.
    queue = _Queue(_tracks("A", "B", "C"))
    picked = queue.tracks[1]
    queue._items.popleft()
    assert music.remove_queue_index(queue, 1, picked) is None
    assert [t.title for t in queue.tracks] == ["B", "C"]


def test_remove_queue_index_leaves_the_current_track_and_autoplay_lane_alone():
    queue = _Queue(_tracks("A", "B"))
    queue.current_track = _Track("Playing")
    queue._autoplay_items.append(_Track("Staged"))

    music.remove_queue_index(queue, 0)

    assert queue.current_track.title == "Playing"
    assert [t.title for t in queue.autoplay_tracks] == ["Staged"]


# ---------------------------------------------------------------------------
# Render: the manage select and the Shuffle button
# ---------------------------------------------------------------------------


def test_queue_view_renders_one_option_per_listed_track():
    player = _Player(_tracks("A", "B", "C"))
    select = _select(_view(player))
    assert [o.value for o in select.options] == ["0", "1", "2"]
    assert [o.label for o in select.options] == ["1. A", "2. B", "3. C"]


def test_queue_view_select_values_are_absolute_indexes_on_a_later_page():
    # Page 2 lists tracks 11..20; the values must be the ABSOLUTE indexes 10..19
    # so an action never acts on the first page's tracks.
    player = _Player([_Track("T{0}".format(i)) for i in range(25)])
    select = _select(_view(player, page=1))
    assert [o.value for o in select.options] == [str(i) for i in range(10, 20)]
    assert select.options[0].label == "11. T10"


def test_queue_view_select_labels_are_truncated_to_the_discord_limit():
    player = _Player([_Track("L" * 200)])
    option = _select(_view(player)).options[0]
    assert len(option.label) <= views.SELECT_TEXT_MAX
    assert option.label.endswith("...")


def test_queue_view_has_no_select_when_the_queue_is_empty():
    assert _select(_view(_Player())) is None


def test_queue_view_shuffle_button_is_disabled_under_two_tracks():
    labels = {b.label: b.disabled for b in _buttons(_view(_Player(_tracks("A"))))}
    assert labels["Shuffle"] is True


def test_queue_view_shuffle_button_is_enabled_from_two_tracks():
    labels = {b.label: b.disabled for b in _buttons(_view(_Player(_tracks("A", "B"))))}
    assert labels["Shuffle"] is False


def test_queue_view_keeps_its_original_controls():
    labels = [b.label for b in _buttons(_view(_Player(_tracks("A", "B"))))]
    assert labels == ["Prev", "Next", "Shuffle", "Add track", "Clear queue"]


def test_queue_view_timeout_disables_the_select_too():
    view = _view(_Player(_tracks("A", "B")))
    view._disable_all()
    assert _select(view).disabled is True
    assert all(b.disabled for b in _buttons(view))


# ---------------------------------------------------------------------------
# _manage - opening the ephemeral Jump / Remove panel
# ---------------------------------------------------------------------------


async def test_manage_opens_the_panel_for_the_picked_track(make_interaction):
    player = _Player(_tracks("A", "B", "C"))
    view = _view(player)
    interaction = make_interaction()

    await view._manage(interaction, 1, player.queue.tracks[1])

    assert len(interaction.sent) == 1
    (args, kwargs) = interaction.sent[0]
    assert kwargs["ephemeral"] is True
    panel = kwargs["view"]
    assert isinstance(panel, views._QueueTrackActions)
    assert panel.track_index == 1
    assert panel.track.title == "B"
    assert panel.origin is interaction
    assert [b.label for b in panel.children] == ["Play now", "Remove"]


async def test_manage_does_not_re_render_the_public_listing(make_interaction):
    # Opening the panel mutates nothing: no message edit is spent on a browse.
    player = _Player(_tracks("A", "B"))
    view = _view(player)
    await view._manage(make_interaction(), 0, player.queue.tracks[0])
    assert view.message.edits == []


async def test_manage_is_dj_gated(make_interaction):
    player = _Player(_tracks("A", "B"), dj=_dj())
    view = _view(player, _Cog(allowed=False))
    interaction = make_interaction(user_id=9)

    await view._manage(interaction, 0, player.queue.tracks[0])

    assert len(interaction.sent) == 1
    assert "DJ" in interaction.sent[0][0][0]
    assert interaction.sent[0][1].get("view") is None


async def test_manage_refuses_a_stale_index_and_resyncs(make_interaction):
    player = _Player(_tracks("A", "B"))
    view = _view(player)
    picked = player.queue.tracks[1]
    player.queue._items.clear()  # the queue was cleared under the viewer
    interaction = make_interaction()

    await view._manage(interaction, 1, picked)

    assert "no longer in the queue" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True
    assert view.message.edits  # the listing was refreshed off the live queue


# ---------------------------------------------------------------------------
# _jump - the pop_at + direct play() seam
# ---------------------------------------------------------------------------


async def test_jump_promotes_the_picked_track_and_plays_it(make_interaction):
    player = _Player(_tracks("A", "B", "C"), current=_Track("Playing"))
    player.queue.current_track = player.current
    cog = _Cog()
    view = _view(player, cog)
    picked = player.queue.tracks[2]
    interaction = make_interaction()

    moved = await view._jump(interaction, 2, picked)

    assert moved is True
    # pop_at removed the entry AND promoted it; play() then dispatches it
    # directly - the _play_previous seam (REPLACED end reason, no autoplay fire).
    assert [t.title for t in player.queue.tracks] == ["A", "B"]
    assert player.played == [picked]
    assert player.queue.current_track is picked
    assert [t.title for t in player.queue.history] == ["Playing"]
    # Queue + current track changed -> persisted, then the surfaces refresh.
    assert len(cog.snapshots) == 1
    assert "Jumped to" in interaction.edits[0][1]["content"]
    assert interaction.edits[0][1]["view"] is None
    assert view.message.edits


async def test_jump_with_duplicates_takes_the_picked_copy(make_interaction):
    first, second = _Track("X"), _Track("X")
    player = _Player([_Track("A"), first, _Track("B"), second])
    view = _view(player)

    await view._jump(make_interaction(), 3, second)

    assert player.played == [second]
    assert [t.title for t in player.queue.tracks] == ["A", "X", "B"]
    assert player.queue.tracks[1] is first


async def test_jump_is_dj_gated_and_changes_nothing(make_interaction):
    player = _Player(_tracks("A", "B"), dj=_dj())
    cog = _Cog(allowed=False)
    view = _view(player, cog)

    moved = await view._jump(make_interaction(user_id=9), 1, player.queue.tracks[1])

    assert moved is False
    assert player.played == []
    assert [t.title for t in player.queue.tracks] == ["A", "B"]
    assert cog.snapshots == []


async def test_jump_refuses_a_stale_pick_without_playing(make_interaction):
    player = _Player(_tracks("A", "B", "C"))
    cog = _Cog()
    view = _view(player, cog)
    picked = player.queue.tracks[2]
    player.queue._items.popleft()  # everything shifted; index 2 is gone
    interaction = make_interaction()

    moved = await view._jump(interaction, 2, picked)

    assert moved is False
    assert player.played == []
    assert cog.snapshots == []
    assert "no longer in the queue" in interaction.sent[0][0][0]


# ---------------------------------------------------------------------------
# _remove
# ---------------------------------------------------------------------------


async def test_remove_drops_the_picked_track_and_persists(make_interaction):
    player = _Player(_tracks("A", "B", "C"), current=_Track("Playing"))
    cog = _Cog()
    view = _view(player, cog)
    interaction = make_interaction()

    removed = await view._remove(interaction, 1, player.queue.tracks[1])

    assert removed is True
    assert [t.title for t in player.queue.tracks] == ["A", "C"]
    assert player.current.title == "Playing"  # playback untouched
    assert player.played == []
    assert len(cog.snapshots) == 1
    assert "Removed" in interaction.edits[0][1]["content"]
    assert view.message.edits


async def test_remove_is_dj_gated_and_changes_nothing(make_interaction):
    player = _Player(_tracks("A", "B"), dj=_dj())
    cog = _Cog(allowed=False)
    view = _view(player, cog)

    removed = await view._remove(make_interaction(user_id=9), 0, player.queue.tracks[0])

    assert removed is False
    assert [t.title for t in player.queue.tracks] == ["A", "B"]
    assert cog.snapshots == []


async def test_remove_refuses_a_stale_pick(make_interaction):
    player = _Player(_tracks("A", "B"))
    cog = _Cog()
    view = _view(player, cog)
    picked = player.queue.tracks[1]
    player.queue._items.clear()
    interaction = make_interaction()

    removed = await view._remove(interaction, 1, picked)

    assert removed is False
    assert cog.snapshots == []
    assert "no longer in the queue" in interaction.sent[0][0][0]


# ---------------------------------------------------------------------------
# _shuffle - the controller's semantics plus the snapshot the reorder needs
# ---------------------------------------------------------------------------


async def test_shuffle_reorders_persists_and_refreshes(make_interaction):
    player = _Player(_tracks("A", "B", "C"))
    cog = _Cog()
    view = _view(player, cog)
    interaction = make_interaction()

    await view._shuffle(interaction)

    assert [t.title for t in player.queue.tracks] == ["C", "B", "A"]
    # A reorder that is not snapshotted comes back in the old order on restart.
    assert len(cog.snapshots) == 1
    assert interaction.edits  # the listing was rebuilt in place
    assert "Shuffled the queue." in interaction.followups[0][0][0]


async def test_shuffle_is_dj_gated(make_interaction):
    player = _Player(_tracks("A", "B"), dj=_dj())
    cog = _Cog(allowed=False)
    view = _view(player, cog)

    await view._shuffle(make_interaction(user_id=9))

    assert [t.title for t in player.queue.tracks] == ["A", "B"]
    assert cog.snapshots == []


async def test_shuffle_refuses_under_two_tracks_even_from_a_stale_view(
    make_interaction,
):
    # The button renders disabled, but a view built when the queue was longer can
    # still deliver the click - the runtime count is what refuses.
    player = _Player(_tracks("A"))
    cog = _Cog()
    view = _view(player, cog)
    interaction = make_interaction()

    await view._shuffle(interaction)

    assert "Add a few more tracks before shuffling." in interaction.sent[0][0][0]
    assert cog.snapshots == []


# ---------------------------------------------------------------------------
# The ephemeral panel
# ---------------------------------------------------------------------------


async def test_panel_re_runs_the_room_gate(make_interaction):
    # The fake clicker is not a discord.Member in the player's channel, so the
    # same-voice gate the queue view uses refuses the panel click too.
    player = _Player(_tracks("A"))
    panel = views._QueueTrackActions(_view(player), 0, player.queue.tracks[0])
    interaction = make_interaction()

    assert await panel.interaction_check(interaction) is False
    assert interaction.sent


async def test_panel_stays_live_after_a_refused_action(make_interaction):
    player = _Player(_tracks("A", "B"), dj=_dj())
    view = _view(player, _Cog(allowed=False))
    panel = views._QueueTrackActions(view, 0, player.queue.tracks[0])

    await panel._remove(make_interaction(user_id=9))

    assert not panel.is_finished()  # the member can retry or pick the other action


async def test_panel_closes_after_a_completed_action(make_interaction):
    player = _Player(_tracks("A", "B"))
    view = _view(player)
    panel = views._QueueTrackActions(view, 0, player.queue.tracks[0])

    await panel._remove(make_interaction())

    assert panel.is_finished()


async def test_panel_timeout_disables_its_buttons():
    player = _Player(_tracks("A"))
    panel = views._QueueTrackActions(_view(player), 0, player.queue.tracks[0])
    edits = []

    class _Origin:
        async def edit_original_response(self, **kwargs):
            edits.append(kwargs)

    panel.origin = _Origin()
    await panel.on_timeout()

    assert all(child.disabled for child in panel.children)
    assert edits and edits[0]["view"] is panel


async def test_panel_timeout_without_an_origin_is_a_no_op():
    player = _Player(_tracks("A"))
    panel = views._QueueTrackActions(_view(player), 0, player.queue.tracks[0])
    await panel.on_timeout()  # must not raise
    assert all(child.disabled for child in panel.children)


# ---------------------------------------------------------------------------
# The select callback routes to _manage with the rendered index and track
# ---------------------------------------------------------------------------


async def test_select_callback_routes_the_absolute_index_and_track(
    make_interaction, monkeypatch
):
    player = _Player([_Track("T{0}".format(i)) for i in range(25)])
    view = _view(player, page=1)
    select = _select(view)
    calls = []

    async def _fake_manage(interaction, index, track):
        calls.append((index, track))

    monkeypatch.setattr(view, "_manage", _fake_manage)
    select._values = ["13"]
    await select.callback(make_interaction())

    assert calls == [(13, player.queue.tracks[13])]
    assert calls[0][1].title == "T13"


@pytest.mark.parametrize("page", [0, 1, 2])
def test_select_never_forks_the_pager(page):
    # The select is built from the SAME slice queue_page resolves - one pager,
    # so the numbering a member reads can never drift from the options.
    player = _Player([_Track("T{0}".format(i)) for i in range(25)])
    view = _view(player, page=page)
    clamped, _pages, start, end = music.queue_page(25, page)
    assert [o.value for o in _select(view).options] == [
        str(i) for i in range(start, end)
    ]
    assert view.page == clamped
