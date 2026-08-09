"""Unit tests for the /history card and its re-queue action (lot M2).

The player already kept a bounded history lane (it is what the controller's Back
button steps through, and what autoplay seeds from), but nothing ever showed it:
a member who wanted the song from twenty minutes ago had to remember its name.
This lot renders that lane as a Components V2 card, newest first, with one offer
per line - re-queue it.

The gate decision this file pins: a re-queue APPENDS to the end of the queue. It
does not interrupt the current track and it does not cut ahead of anyone, so it
carries the same gate as the queue view's "Add track" - same-voice only, open to
the room - and NOT the DJ gate that jump / remove / shuffle / clear carry. A
member who wants a played track NOW picks it from the queue card, which is gated
because that action IS destructive.

What is pinned here:

* the pure helpers - ``history_entries`` (most recent FIRST, the reverse of
  sonolink's natural order, None-safe) and ``history_entry_at`` (the stale
  re-check: range, then identity);
* the rendered surface - newest-first numbering, one option per listed line, the
  pager only past one page, the empty state, and the timeout disabling the select
  as well as the buttons;
* the action - append (never a jump), the requester stamp, playback started only
  on an idle player, the snapshot after the mutation, the stale-click refusal
  (never an IndexError), the payload-less entry refusal, and the deliberate
  ABSENCE of a re-render (a re-queue does not change the history lane).

Live-only (needs Lavalink and a real Discord message, so exercised on the
server): the real Playable objects in the lane and the visual layout.
"""

import types

import discord
import pytest

from cogs.music import music, views

# ---------------------------------------------------------------------------
# Fakes (the sonolink surfaces the card touches)
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title, encoded=None, author="Artist", length=125000):
        self.title = title
        self.author = author
        self.length = length
        self.is_stream = False
        self.encoded = encoded if encoded is not None else "enc-" + title
        self.extras = types.SimpleNamespace(requester=None)

    def __eq__(self, other):
        return getattr(other, "encoded", None) == self.encoded

    def __hash__(self):
        return hash(self.encoded)

    def __repr__(self):
        return "<Track {0}>".format(self.title)


class _Queue:
    """Mirrors the sonolink ``Queue`` surface the history card touches."""

    def __init__(self, history=(), tracks=()):
        self._items = list(tracks)
        self._autoplay_items = []
        # sonolink appends played tracks here, so the lane is OLDEST first.
        self.history = list(history)
        self.current_track = None

    @property
    def tracks(self):
        return list(self._items)

    def put(self, track):
        self._items.append(track)

    def get(self):
        return self._items.pop(0)


class _Player:
    def __init__(self, history=(), tracks=(), current=None):
        self.queue = _Queue(history, tracks)
        self.current = current
        self.channel = types.SimpleNamespace(name="General")
        self.radio_genre = "lofi"
        self.played = []

    async def play(self, track):
        self.played.append(track)
        self.current = track


class _Cog:
    def __init__(self):
        self.snapshots = []

    async def _snapshot(self, player, track=None):
        self.snapshots.append(player)


class _Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def _tracks(*titles):
    return [_Track(t) for t in titles]


def _card(player, cog=None, *, page=0, bind=True):
    card = views.HistoryCard(cog if cog is not None else _Cog(), player)
    card.page = page
    card._build()
    if bind:
        card.message = _Message()
    return card


def _select(card):
    for child in card.walk_children():
        if isinstance(child, views._HistoryTrackSelect):
            return child
    return None


def _buttons(card):
    return [c for c in card.walk_children() if isinstance(c, discord.ui.Button)]


def _interaction(make_interaction, **kwargs):
    itx = make_interaction(**kwargs)
    itx.channel = types.SimpleNamespace(id=77)
    return itx


# ---------------------------------------------------------------------------
# history_entries - newest first, bounded, None-safe
# ---------------------------------------------------------------------------


def test_history_entries_are_most_recent_first():
    # sonolink appends, so the lane reads oldest-first; the card reads the other
    # way round - "what did we just play" starts with the last thing played.
    queue = _Queue(history=_tracks("Oldest", "Middle", "Newest"))

    assert [t.title for t in music.history_entries(queue)] == [
        "Newest",
        "Middle",
        "Oldest",
    ]


def test_history_entries_is_empty_for_a_fresh_session():
    assert music.history_entries(_Queue()) == []


def test_history_entries_is_none_safe():
    assert music.history_entries(types.SimpleNamespace(history=None)) == []
    assert music.history_entries(types.SimpleNamespace()) == []


def test_history_entries_copies_the_lane():
    queue = _Queue(history=_tracks("A", "B"))
    entries = music.history_entries(queue)
    entries.clear()

    assert len(queue.history) == 2


# ---------------------------------------------------------------------------
# history_entry_at - the single stale re-check
# ---------------------------------------------------------------------------


def test_history_entry_at_resolves_the_index():
    entries = _tracks("Newest", "Older")

    assert music.history_entry_at(entries, 1).title == "Older"


def test_history_entry_at_refuses_an_index_past_the_end():
    # The lane rotated under a rendered page: None, never an IndexError.
    assert music.history_entry_at(_tracks("A"), 3) is None


def test_history_entry_at_refuses_a_negative_index():
    assert music.history_entry_at(_tracks("A"), -1) is None


def test_history_entry_at_refuses_a_non_integer_index():
    assert music.history_entry_at(_tracks("A"), "0") is None
    assert music.history_entry_at(_tracks("A"), True) is None


def test_history_entry_at_refuses_when_the_slot_holds_another_track():
    # A track finished while the card sat open, so index 0 is now a different
    # song than the one the member picked.
    entries = _tracks("Newest", "Older")
    picked = _Track("Older")

    assert music.history_entry_at(entries, 0, picked) is None


def test_history_entry_at_accepts_an_equal_replay_at_the_slot():
    # Replaying a song puts a second byte-identical entry in the lane; either one
    # IS the song the member picked, so the pick stands.
    first, second = _Track("X"), _Track("X")

    assert music.history_entry_at([second, _Track("A")], 0, first) is second


# ---------------------------------------------------------------------------
# The rendered card
# ---------------------------------------------------------------------------


def test_card_lists_the_lane_newest_first_with_one_option_per_line():
    player = _Player(history=_tracks("A", "B", "C"))
    card = _card(player)

    select = _select(card)
    assert [o.value for o in select.options] == ["0", "1", "2"]
    assert select.options[0].label.startswith("1. C")  # C played last
    assert select.options[2].label.startswith("3. A")


def test_card_pages_ten_at_a_time_with_page_absolute_values():
    player = _Player(history=_tracks(*[str(i) for i in range(12)]))
    card = _card(player, page=1)

    select = _select(card)
    assert [o.value for o in select.options] == ["10", "11"]
    assert select.options[0].label.startswith("11. ")


def test_card_pager_only_appears_past_one_page():
    assert _buttons(_card(_Player(history=_tracks("A")))) == []
    assert len(_buttons(_card(_Player(history=_tracks(*"ABCDEFGHIJK"))))) == 2


def test_card_clamps_a_page_that_no_longer_exists():
    player = _Player(history=_tracks(*[str(i) for i in range(12)]))
    card = _card(player, page=1)
    player.queue.history = _tracks("A")
    card._build()

    assert card.page == 0


def test_card_renders_an_empty_lane_without_a_select():
    card = _card(_Player())

    assert _select(card) is None
    assert _buttons(card) == []


def test_card_timeout_disables_the_select_as_well_as_the_buttons():
    card = _card(_Player(history=_tracks(*"ABCDEFGHIJK")))

    card._disable_all()

    assert all(c.disabled for c in card.walk_children() if hasattr(c, "disabled"))


# ---------------------------------------------------------------------------
# The re-queue action
# ---------------------------------------------------------------------------


async def test_requeue_appends_to_the_END_of_the_queue(make_interaction):
    """Re-queue is not a jump: it never cuts ahead of what is already waiting."""
    player = _Player(
        history=_tracks("Played"), tracks=_tracks("Waiting1", "Waiting2"),
        current=_Track("Now"),
    )
    cog = _Cog()
    card = _card(player, cog)
    picked = music.history_entries(player.queue)[0]
    interaction = _interaction(make_interaction)

    await card._requeue(interaction, 0, picked)

    assert [t.title for t in player.queue.tracks] == [
        "Waiting1",
        "Waiting2",
        "Played",
    ]
    assert player.played == []  # the current track keeps playing
    assert player.queue.tracks[-1].extras.requester == 1
    assert player.radio_genre is None
    assert cog.snapshots == [player]
    assert "Queued" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_requeue_starts_playback_on_an_idle_player(make_interaction):
    player = _Player(history=_tracks("Played"), current=None)
    card = _card(player)
    picked = music.history_entries(player.queue)[0]

    await card._requeue(_interaction(make_interaction), 0, picked)

    assert [t.title for t in player.played] == ["Played"]


async def test_requeue_does_not_re_render_the_card(make_interaction):
    # The history lane is unchanged by a re-queue, so the listing is still
    # accurate - spending a message edit on every click would be pure noise.
    player = _Player(history=_tracks("Played"), current=_Track("Now"))
    card = _card(player)

    await card._requeue(_interaction(make_interaction), 0, player.queue.history[0])

    assert card.message.edits == []


async def test_requeue_refuses_a_stale_pick_and_resyncs(make_interaction):
    player = _Player(history=_tracks("A", "B"), current=_Track("Now"))
    card = _card(player)
    picked = _Track("B")
    # Another track finished: index 0 now holds it, not the picked "B".
    player.queue.history.append(_Track("C"))
    interaction = _interaction(make_interaction)

    await card._requeue(interaction, 0, picked)

    assert player.queue.tracks == []
    assert "no longer in the history" in interaction.sent[0][0][0]
    assert card.message.edits  # the listing was resynced off the live lane


async def test_requeue_refuses_an_index_past_the_end_without_crashing(
    make_interaction,
):
    player = _Player(history=_tracks("A"), current=_Track("Now"))
    card = _card(player)
    interaction = _interaction(make_interaction)

    await card._requeue(interaction, 9, None)

    assert player.queue.tracks == []
    assert "no longer in the history" in interaction.sent[0][0][0]


async def test_requeue_refuses_an_entry_with_no_payload(make_interaction):
    player = _Player(history=[_Track("Ghost", encoded="")], current=_Track("Now"))
    cog = _Cog()
    card = _card(player, cog)

    await card._requeue(
        _interaction(make_interaction), 0, player.queue.history[0]
    )

    assert player.queue.tracks == []
    assert cog.snapshots == []


async def test_requeue_takes_the_picked_copy_of_a_replayed_song(make_interaction):
    # The same song played twice sits in the lane twice; picking either one
    # re-queues exactly that song (they are byte-identical, so both are right).
    first, second = _Track("X"), _Track("X")
    player = _Player(history=[first, _Track("Y"), second], current=_Track("Now"))
    card = _card(player)

    await card._requeue(_interaction(make_interaction), 0, _Track("X"))

    assert [t.title for t in player.queue.tracks] == ["X"]


# ---------------------------------------------------------------------------
# The gate: room-open like Add track, NOT DJ-gated like jump / remove
# ---------------------------------------------------------------------------


class _VoiceMember:
    def __init__(self, user_id, channel):
        self.id = user_id
        self.voice = types.SimpleNamespace(channel=channel)


async def test_the_card_is_gated_on_being_in_the_voice_channel(make_interaction):
    player = _Player(history=_tracks("A"))
    card = _card(player)
    interaction = _interaction(make_interaction, user_id=9)
    interaction.user = types.SimpleNamespace(id=9, voice=None)

    assert await card.interaction_check(interaction) is False
    assert interaction.sent


async def test_a_non_dj_listener_may_re_queue(make_interaction):
    """The gate decision: appending is not a control action.

    Nothing on this path consults ``_can_control`` - the fake cog does not even
    define it, so a DJ gate anywhere in the flow would raise instead of pass.
    """
    player = _Player(history=_tracks("Played"), current=_Track("Now"))
    player.dj = types.SimpleNamespace(id=1, mention="<@1>")
    card = _card(player)

    await card._requeue(
        _interaction(make_interaction, user_id=999), 0, player.queue.history[0]
    )

    assert [t.title for t in player.queue.tracks] == ["Played"]


@pytest.mark.parametrize("attribute", ["_manage", "_jump", "_remove", "_clear"])
def test_the_history_card_offers_no_destructive_action(attribute):
    """Read-only by construction: the only mutation it can make is an append."""
    assert not hasattr(views.HistoryCard, attribute)


# ---------------------------------------------------------------------------
# Re-render hygiene: a Components V2 edit resends the listing it carries
# ---------------------------------------------------------------------------


async def test_every_history_edit_suppresses_mentions():
    """A track TITLE is attacker-supplied text and can be shaped like <@id>.

    Components V2 puts that text INSIDE the view, so every edit resends it, and
    discord.py folds the client default (users=True) into an edit that stays
    silent. The first send already passes AllowedMentions.none(); the refresh
    and the timeout must too, or a page flip pings whoever that id names.
    """
    card = _card(_Player(history=_tracks(*"ABC")))

    await card._rerender()
    await card.on_timeout()

    assert len(card.message.edits) == 2
    assert all(kw["allowed_mentions"].users is False for kw in card.message.edits)


async def test_history_paging_suppresses_mentions(make_interaction):
    card = _card(_Player(history=_tracks(*"ABCDEFGHIJKL")))
    forward = _interaction(make_interaction)
    await card._next(forward)
    back = _interaction(make_interaction)
    await card._prev(back)

    for itx in (forward, back):
        (_args, kwargs) = itx.edits[0]
        assert kwargs["allowed_mentions"].users is False
