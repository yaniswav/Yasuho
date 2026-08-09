"""Unit tests for the favourites card and the two-path favourites loader (lot M2).

Favourites had two problems this lot fixes:

* the listing was a legacy embed paginator with no actions, and removing a track
  meant typing ``/playlist remove <n>`` against a numbering that could have moved
  since the page was rendered;
* ``/playlist play`` resolved every saved track with its OWN Lavalink search,
  awaited one after another - up to a hundred SERIAL round trips for one command.

The fix is grounded in a seam the repo already had: the shared server playlists
and the cold restore both rebuild tracks from stored ``encoded`` blobs in ONE
bulk ``decode_tracks`` call. Favourites now store that blob too, so a full list
costs a single round trip. Rows saved before the column existed keep working:
they are searched with BOUNDED concurrency, capped per run, and backfilled with
their blob, so the search path drains to nothing instead of being paid forever.

What is pinned here:

* the pure planners - ``plan_favourite_resolution`` (blob vs legacy vs deferred,
  order preserved) and ``pair_decoded_favourites`` (positional pairing over a
  result that may hold ``None`` or be short);
* the loader - ONE decode call and ZERO searches for a stored-blob list (the
  paper-cut counter-test), bounded-parallel searching for legacy rows, the
  backfill write, the per-row failure accounting and the stored-order result;
* the card - one option per listed row, actions only on your OWN list, removal
  addressed by IDENTIFIER (not by position), the local drop + re-render, and the
  play action queueing exactly one track through the shared connect seam.

Live-only (needs Lavalink and a real Discord message, so exercised on the
server): actual decode/search answers and the rendered layout. Everything here
runs on fakes, so it passes under the stub sonolink dev box and real-sonolink CI
alike.
"""

import asyncio
import types

import discord
import pytest

from cogs.music import music, views

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title, encoded=None, author="Artist", length=125000):
        self.title = title
        self.author = author
        self.length = length
        self.is_stream = False
        self.identifier = title
        self.uri = "https://example.test/{0}".format(title)
        self.source_name = "youtube"
        self.encoded = encoded if encoded is not None else "enc-" + title
        self.extras = types.SimpleNamespace(requester=None)

    def __eq__(self, other):
        return getattr(other, "encoded", None) == self.encoded

    def __hash__(self):
        return hash(self.encoded)


class _Result:
    """The shape ``_first_track`` normalises (a single-track search answer)."""

    def __init__(self, track):
        self.result = track

    def is_error(self):
        return False

    def is_empty(self):
        return self.result is None


class _Queue:
    def __init__(self, tracks=()):
        self._items = list(tracks)
        self._autoplay_items = []
        self.current_track = None
        self.history = []

    @property
    def tracks(self):
        return list(self._items)

    def put(self, track):
        self._items.append(track)

    def get(self):
        return self._items.pop(0)


class _Player:
    def __init__(self, current=None):
        self.queue = _Queue()
        self.current = current
        self.channel = types.SimpleNamespace(name="General")
        self.home = None
        self.radio_genre = "lofi"
        self.played = []

    async def play(self, track):
        self.played.append(track)
        self.current = track


class _SLClient:
    """Records every bulk decode; answers positionally like Lavalink does."""

    def __init__(self, answers=None, raises=False):
        self.calls = []
        self.answers = answers
        self.raises = raises

    async def decode_tracks(self, *blobs):
        self.calls.append(blobs)
        if self.raises:
            raise RuntimeError("no node")
        if self.answers is not None:
            return self.answers
        return [_Track(blob.replace("enc-", "")) for blob in blobs]


class _Bot:
    def __init__(self, pool, sl_client=None):
        self.db_pool = pool
        self.sl_client = sl_client or _SLClient()


def _cog(pool, sl_client=None):
    """A Music cog with no __init__ side effects (it starts a task loop)."""
    cog = music.Music.__new__(music.Music)
    cog.bot = _Bot(pool, sl_client)
    return cog


def _row(identifier, *, encoded=None, uri="https://example.test/x", title=None):
    return {
        "identifier": identifier,
        "title": title if title is not None else "Song " + identifier,
        "author": "Artist " + identifier,
        "uri": uri,
        "source_name": "youtube",
        "encoded": encoded,
    }


def _interaction(make_interaction, **kwargs):
    """A FakeInteraction with the extra seams these surfaces touch."""
    itx = make_interaction(**kwargs)
    itx.original_edits = []
    itx.channel = types.SimpleNamespace(id=77)
    itx.guild = types.SimpleNamespace(voice_client=None)

    async def edit_original_response(**kw):
        itx.original_edits.append(kw)

    itx.edit_original_response = edit_original_response
    return itx


def _member(user_id, name="Mio"):
    return types.SimpleNamespace(id=user_id, display_name=name)


def _select(card):
    for child in card.walk_children():
        if isinstance(child, views._FavouriteSelect):
            return child
    return None


def _buttons(card):
    return [c for c in card.walk_children() if isinstance(c, discord.ui.Button)]


# ---------------------------------------------------------------------------
# plan_favourite_resolution - which rows cost a round trip at all
# ---------------------------------------------------------------------------


def test_plan_splits_stored_blobs_from_legacy_rows():
    rows = [_row("a", encoded="enc-a"), _row("b"), _row("c", encoded="enc-c")]

    decodable, searchable, deferred = music.plan_favourite_resolution(rows)

    assert [r["identifier"] for r in decodable] == ["a", "c"]
    assert [r["identifier"] for r in searchable] == ["b"]
    assert deferred == 0


def test_plan_leaves_a_blob_carrying_list_with_nothing_to_search():
    """The point of the whole lot: a modern list never searches at all."""
    rows = [_row(str(i), encoded="enc-" + str(i)) for i in range(100)]

    decodable, searchable, deferred = music.plan_favourite_resolution(rows)

    assert len(decodable) == 100
    assert searchable == []
    assert deferred == 0


def test_plan_caps_the_legacy_search_batch_and_reports_the_rest():
    rows = [_row(str(i)) for i in range(100)]

    _decodable, searchable, deferred = music.plan_favourite_resolution(rows)

    assert len(searchable) == music.FAVOURITE_SEARCH_CAP
    assert deferred == 100 - music.FAVOURITE_SEARCH_CAP


def test_plan_honours_an_explicit_cap():
    rows = [_row(str(i)) for i in range(10)]

    _decodable, searchable, deferred = music.plan_favourite_resolution(rows, cap=4)

    assert len(searchable) == 4
    assert deferred == 6


def test_plan_drops_a_row_that_is_playable_by_neither_path():
    # No blob and no URI: nothing to decode, nothing to search.
    rows = [_row("a", uri=None), _row("b", encoded="enc-b")]

    decodable, searchable, deferred = music.plan_favourite_resolution(rows)

    assert [r["identifier"] for r in decodable] == ["b"]
    assert searchable == []
    assert deferred == 0


def test_plan_preserves_order_inside_each_bucket():
    rows = [_row("a"), _row("b", encoded="enc-b"), _row("c"), _row("d", encoded="enc-d")]

    decodable, searchable, _deferred = music.plan_favourite_resolution(rows)

    assert [r["identifier"] for r in decodable] == ["b", "d"]
    assert [r["identifier"] for r in searchable] == ["a", "c"]


# ---------------------------------------------------------------------------
# pair_decoded_favourites - positional pairing, failures counted from the rows
# ---------------------------------------------------------------------------


def test_pair_decoded_pairs_positionally():
    rows = [_row("a"), _row("b")]
    tracks = [_Track("A"), _Track("B")]

    pairs, skipped = music.pair_decoded_favourites(rows, tracks)

    assert [(r["identifier"], t.title) for r, t in pairs] == [("a", "A"), ("b", "B")]
    assert skipped == 0


def test_pair_decoded_skips_a_dead_blob_and_keeps_the_pairing_aligned():
    rows = [_row("a"), _row("b"), _row("c")]
    tracks = [_Track("A"), None, _Track("C")]

    pairs, skipped = music.pair_decoded_favourites(rows, tracks)

    assert [(r["identifier"], t.title) for r, t in pairs] == [("a", "A"), ("c", "C")]
    assert skipped == 1


def test_pair_decoded_counts_a_short_answer_as_skipped():
    rows = [_row("a"), _row("b"), _row("c")]

    pairs, skipped = music.pair_decoded_favourites(rows, [_Track("A")])

    assert len(pairs) == 1
    assert skipped == 2


def test_pair_decoded_handles_no_answer_at_all():
    rows = [_row("a"), _row("b")]

    pairs, skipped = music.pair_decoded_favourites(rows, None)

    assert pairs == []
    assert skipped == 2


# ---------------------------------------------------------------------------
# add_favourite / delete_favourite - the storage seam
# ---------------------------------------------------------------------------


async def test_add_favourite_stores_the_encoded_blob(fake_pool):
    cog = _cog(fake_pool)
    fake_pool.execute_return = "INSERT 0 1"

    result = await cog.add_favourite(7, _Track("Song"))

    assert result == "added"
    (_method, query, args) = fake_pool.calls[0]
    assert "encoded" in query
    # The blob travels with the metadata - that is what makes the bulk decode
    # possible later, and it must not shift the cap parameter.
    assert "enc-Song" in args
    assert music.MAX_FAVOURITES in args


async def test_add_favourite_still_reports_an_existing_row(fake_pool):
    cog = _cog(fake_pool)
    fake_pool.execute_return = "INSERT 0 0"
    fake_pool.fetchval_return = 1

    assert await cog.add_favourite(7, _Track("Song")) == "exists"


async def test_add_favourite_still_reports_a_full_list(fake_pool):
    # The cap guard skipped the insert and no row exists: the list is full, not
    # a duplicate. Pinned because the blob column shifted the cap placeholder.
    cog = _cog(fake_pool)
    fake_pool.execute_return = "INSERT 0 0"
    fake_pool.fetchval_return = None

    assert await cog.add_favourite(7, _Track("Song")) == "full"


async def test_add_favourite_tolerates_a_track_without_a_blob(fake_pool):
    cog = _cog(fake_pool)
    track = _Track("Song")
    del track.encoded

    assert await cog.add_favourite(7, track) == "added"
    assert None in fake_pool.calls[0][2]


async def test_delete_favourite_is_identifier_addressed(fake_pool):
    cog = _cog(fake_pool)
    fake_pool.execute_return = "DELETE 1"

    assert await cog.delete_favourite(7, "abc") is True
    (_method, query, args) = fake_pool.calls[0]
    assert "DELETE FROM music_favorites" in query
    assert args == (7, "abc")


async def test_delete_favourite_reports_a_row_that_was_already_gone(fake_pool):
    cog = _cog(fake_pool)
    fake_pool.execute_return = "DELETE 0"

    assert await cog.delete_favourite(7, "abc") is False


# ---------------------------------------------------------------------------
# resolve_favourites - the paper cut
# ---------------------------------------------------------------------------


async def test_a_full_stored_blob_list_costs_ONE_round_trip_and_no_search(fake_pool):
    """The counter-test for the hundred serial searches this lot removes."""
    client = _SLClient()
    cog = _cog(fake_pool, client)
    searched = []

    async def never(query, **kwargs):
        searched.append(query)
        return None

    cog._search = never
    rows = [_row(str(i), encoded="enc-" + str(i)) for i in range(100)]

    tracks, failed, deferred = await cog.resolve_favourites(7, rows)

    assert len(tracks) == 100
    assert len(client.calls) == 1  # ONE bulk decode for the whole list
    assert len(client.calls[0]) == 100
    assert searched == []
    assert (failed, deferred) == (0, 0)


async def test_resolve_keeps_the_stored_order_across_both_paths(fake_pool):
    client = _SLClient(answers=[_Track("B"), _Track("D")])
    cog = _cog(fake_pool, client)

    async def search(query, **kwargs):
        return _Result(_Track(query.rsplit("/", 1)[-1].upper()))

    cog._search = search
    rows = [
        _row("a", uri="https://x/a"),
        _row("b", encoded="enc-b"),
        _row("c", uri="https://x/c"),
        _row("d", encoded="enc-d"),
    ]

    tracks, failed, deferred = await cog.resolve_favourites(7, rows)

    # Decoded and searched tracks are re-threaded into the order the member sees.
    assert [t.title for t in tracks] == ["A", "B", "C", "D"]
    assert (failed, deferred) == (0, 0)


async def test_legacy_rows_are_searched_bounded_parallel_not_serially(fake_pool):
    cog = _cog(fake_pool)
    state = {"inflight": 0, "peak": 0}

    async def search(query, **kwargs):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        state["inflight"] -= 1
        return _Result(_Track(query))

    cog._search = search
    rows = [_row(str(i), uri="u{0}".format(i)) for i in range(10)]

    tracks, _failed, _deferred = await cog.resolve_favourites(7, rows)

    assert len(tracks) == 10
    # Bounded: never more than the semaphore allows...
    assert state["peak"] <= music.FAVOURITE_SEARCH_CONCURRENCY
    # ...and genuinely concurrent: the old loop's peak was exactly 1.
    assert state["peak"] > 1


async def test_a_legacy_list_is_capped_and_the_rest_reported_as_deferred(fake_pool):
    cog = _cog(fake_pool)
    calls = []

    async def search(query, **kwargs):
        calls.append(query)
        return _Result(_Track(query))

    cog._search = search
    rows = [_row(str(i), uri="u{0}".format(i)) for i in range(100)]

    tracks, failed, deferred = await cog.resolve_favourites(7, rows)

    assert len(calls) == music.FAVOURITE_SEARCH_CAP
    assert len(tracks) == music.FAVOURITE_SEARCH_CAP
    # Deferred, NOT failed: they are still saved and load on the next run.
    assert deferred == 100 - music.FAVOURITE_SEARCH_CAP
    assert failed == 0


async def test_searched_rows_are_backfilled_with_their_blobs(fake_pool):
    """The self-healing half: a legacy list is searched once, then never again."""
    cog = _cog(fake_pool)

    async def search(query, **kwargs):
        return _Result(_Track(query))

    cog._search = search
    rows = [_row("a", uri="u-a"), _row("b", uri="u-b")]

    await cog.resolve_favourites(7, rows)

    updates = [c for c in fake_pool.calls if "UPDATE music_favorites" in c[1]]
    assert len(updates) == 1  # one statement for the batch, not one per row
    (_method, query, args) = updates[0]
    assert "encoded IS NULL" in query  # pure backfill, never an overwrite
    assert args[0] == 7
    assert list(args[1]) == ["a", "b"]
    assert list(args[2]) == ["enc-u-a", "enc-u-b"]


async def test_nothing_is_backfilled_when_every_row_had_its_blob(fake_pool):
    cog = _cog(fake_pool)
    rows = [_row("a", encoded="enc-a")]

    await cog.resolve_favourites(7, rows)

    assert [c for c in fake_pool.calls if "UPDATE" in c[1]] == []


async def test_a_dead_blob_is_counted_as_failed_not_silently_dropped(fake_pool):
    client = _SLClient(answers=[_Track("A"), None])
    cog = _cog(fake_pool, client)
    rows = [_row("a", encoded="enc-a"), _row("b", encoded="enc-b")]

    tracks, failed, deferred = await cog.resolve_favourites(7, rows)

    assert [t.title for t in tracks] == ["A"]
    assert (failed, deferred) == (1, 0)


async def test_a_row_playable_by_neither_path_counts_as_failed(fake_pool):
    cog = _cog(fake_pool)
    rows = [_row("a", uri=None)]

    tracks, failed, _deferred = await cog.resolve_favourites(7, rows)

    assert tracks == []
    assert failed == 1


async def test_a_lost_node_during_decode_does_not_lose_the_legacy_rows(fake_pool):
    # decode_tracks raises when no node is available; the searchable half must
    # still be attempted and the decodable half counted as failed.
    cog = _cog(fake_pool, _SLClient(raises=True))

    async def search(query, **kwargs):
        return _Result(_Track("SEARCHED"))

    cog._search = search
    rows = [_row("a", encoded="enc-a"), _row("b", uri="u-b")]

    tracks, failed, _deferred = await cog.resolve_favourites(7, rows)

    assert [t.title for t in tracks] == ["SEARCHED"]
    assert failed == 1


async def test_one_failing_search_never_kills_the_whole_batch(fake_pool):
    cog = _cog(fake_pool)

    async def search(query, **kwargs):
        if query == "u-b":
            raise RuntimeError("boom")
        return _Result(_Track(query))

    cog._search = search
    rows = [_row("a", uri="u-a"), _row("b", uri="u-b"), _row("c", uri="u-c")]

    tracks, failed, _deferred = await cog.resolve_favourites(7, rows)

    assert [t.title for t in tracks] == ["u-a", "u-c"]
    assert failed == 1


async def test_a_backfill_failure_never_costs_the_member_their_tracks(fake_pool):
    cog = _cog(fake_pool)

    async def search(query, **kwargs):
        return _Result(_Track(query))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    cog._search = search
    fake_pool.execute = boom
    rows = [_row("a", uri="u-a")]

    tracks, failed, _deferred = await cog.resolve_favourites(7, rows)

    assert len(tracks) == 1
    assert failed == 0


# ---------------------------------------------------------------------------
# FavouritesCard - the rendered surface
# ---------------------------------------------------------------------------


def test_card_lists_one_option_per_row_with_page_absolute_values(fake_pool):
    rows = [_row(str(i)) for i in range(12)]
    card = views.FavouritesCard(_cog(fake_pool), 7, _member(7), rows)
    card.page = 1
    card._build()

    select = _select(card)
    assert [o.value for o in select.options] == ["10", "11"]
    # The number on the line and the number in the option agree.
    assert select.options[0].label.startswith("11. ")


def test_card_offers_no_actions_on_someone_elses_list(fake_pool):
    rows = [_row("a")]
    card = views.FavouritesCard(_cog(fake_pool), 7, _member(9, "Yuki"), rows)

    assert card.is_own_list is False
    assert _select(card) is None


def test_card_pager_only_appears_past_one_page(fake_pool):
    single = views.FavouritesCard(_cog(fake_pool), 7, _member(7), [_row("a")])
    assert _buttons(single) == []

    many = views.FavouritesCard(
        _cog(fake_pool), 7, _member(7), [_row(str(i)) for i in range(11)]
    )
    assert len(_buttons(many)) == 2


def test_card_clamps_a_page_that_no_longer_exists(fake_pool):
    card = views.FavouritesCard(
        _cog(fake_pool), 7, _member(7), [_row(str(i)) for i in range(11)]
    )
    card.page = 1
    card.rows = [_row("a")]
    card._build()

    assert card.page == 0


def test_card_timeout_disables_the_select_as_well_as_the_buttons(fake_pool):
    card = views.FavouritesCard(
        _cog(fake_pool), 7, _member(7), [_row(str(i)) for i in range(11)]
    )

    card._disable_all()

    assert all(c.disabled for c in card.walk_children() if hasattr(c, "disabled"))


# ---------------------------------------------------------------------------
# FavouritesCard actions
# ---------------------------------------------------------------------------


async def test_manage_opens_the_play_remove_panel(fake_pool, make_interaction):
    rows = [_row("a")]
    card = views.FavouritesCard(_cog(fake_pool), 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    await card._manage(interaction, rows[0])

    (_args, kwargs) = interaction.sent[0]
    panel = kwargs["view"]
    assert isinstance(panel, views._FavouriteActions)
    assert kwargs["ephemeral"] is True
    assert panel.row is rows[0]
    assert panel.origin is interaction


async def test_manage_does_not_re_render_the_public_card(fake_pool, make_interaction):
    rows = [_row("a")]
    card = views.FavouritesCard(_cog(fake_pool), 7, _member(7), rows)
    card.message = types.SimpleNamespace(edits=[])

    async def edit(**kwargs):
        card.message.edits.append(kwargs)

    card.message.edit = edit

    await card._manage(_interaction(make_interaction, user_id=7), rows[0])

    assert card.message.edits == []


async def test_remove_deletes_by_identifier_not_by_position(
    fake_pool, make_interaction
):
    """The end of the index-versus-listing footgun."""
    cog = _cog(fake_pool)
    fake_pool.execute_return = "DELETE 1"
    rows = [_row("a"), _row("b"), _row("c")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    removed = await card._remove(interaction, rows[2])

    assert removed is True
    (_method, _query, args) = fake_pool.calls[0]
    assert args == (7, "c")
    assert [r["identifier"] for r in card.rows] == ["a", "b"]
    assert "Removed" in interaction.edits[0][1]["content"]
    assert interaction.edits[0][1]["view"] is None


async def test_remove_of_a_row_that_was_already_gone_still_leaves_the_card(
    fake_pool, make_interaction
):
    cog = _cog(fake_pool)
    fake_pool.execute_return = "DELETE 0"
    rows = [_row("a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    await card._remove(interaction, rows[0])

    assert card.rows == []
    assert "no longer" in interaction.edits[0][1]["content"]


async def test_removing_the_last_favourite_renders_the_empty_state(
    fake_pool, make_interaction
):
    cog = _cog(fake_pool)
    fake_pool.execute_return = "DELETE 1"
    rows = [_row("a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)

    await card._remove(_interaction(make_interaction, user_id=7), rows[0])

    assert _select(card) is None


async def test_play_queues_exactly_the_picked_track(fake_pool, make_interaction):
    client = _SLClient(answers=[_Track("A")])
    cog = _cog(fake_pool, client)
    cog._nodes_available = lambda: True
    player = _Player(current=_Track("Playing"))
    snapshots = []

    async def snapshot(p, track=None):
        snapshots.append(p)

    async def connect(ctx):
        return player

    cog._snapshot = snapshot
    cog._connect_for_playlist = connect
    rows = [_row("a", encoded="enc-a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    played = await card._play_one(interaction, rows[0])

    assert played is True
    assert [t.title for t in player.queue.tracks] == ["A"]
    assert player.played == []  # something was already playing: no interruption
    assert player.queue.tracks[0].extras.requester == 7
    assert player.radio_genre is None  # an explicit pick ends the radio
    assert snapshots == [player]
    assert "Queued" in interaction.original_edits[0]["content"]
    assert interaction.original_edits[0]["view"] is None


async def test_play_starts_playback_on_an_idle_player(fake_pool, make_interaction):
    client = _SLClient(answers=[_Track("A")])
    cog = _cog(fake_pool, client)
    cog._nodes_available = lambda: True
    player = _Player(current=None)

    async def snapshot(p, track=None):
        pass

    async def connect(ctx):
        return player

    cog._snapshot = snapshot
    cog._connect_for_playlist = connect
    rows = [_row("a", encoded="enc-a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)

    await card._play_one(_interaction(make_interaction, user_id=7), rows[0])

    assert [t.title for t in player.played] == ["A"]


async def test_play_refuses_when_no_node_is_connected(fake_pool, make_interaction):
    cog = _cog(fake_pool)
    cog._nodes_available = lambda: False
    connected = []

    async def connect(ctx):
        connected.append(ctx)
        return None

    cog._connect_for_playlist = connect
    rows = [_row("a", encoded="enc-a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    played = await card._play_one(interaction, rows[0])

    assert played is False
    assert connected == []  # never joins a channel it cannot play into
    assert "unavailable" in interaction.followups[0][0][0]


async def test_play_says_so_when_the_track_cannot_be_loaded(
    fake_pool, make_interaction
):
    cog = _cog(fake_pool, _SLClient(answers=[None]))
    cog._nodes_available = lambda: True
    connected = []

    async def connect(ctx):
        connected.append(ctx)
        return None

    cog._connect_for_playlist = connect
    rows = [_row("a", encoded="enc-a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    played = await card._play_one(interaction, rows[0])

    assert played is False
    assert connected == []
    assert "could not be loaded" in interaction.followups[0][0][0]


async def test_play_keeps_the_panel_live_when_the_connect_seam_refuses(
    fake_pool, make_interaction
):
    cog = _cog(fake_pool, _SLClient(answers=[_Track("A")]))
    cog._nodes_available = lambda: True

    async def connect(ctx):
        return None  # e.g. the member is not in a voice channel

    cog._connect_for_playlist = connect
    rows = [_row("a", encoded="enc-a")]
    card = views.FavouritesCard(cog, 7, _member(7), rows)
    interaction = _interaction(make_interaction, user_id=7)

    assert await card._play_one(interaction, rows[0]) is False
    assert interaction.original_edits == []


@pytest.mark.parametrize("clicker,allowed", [(7, True), (9, False)])
async def test_card_is_author_gated(fake_pool, make_interaction, clicker, allowed):
    card = views.FavouritesCard(_cog(fake_pool), 7, _member(7), [_row("a")])

    assert (
        await card.interaction_check(_interaction(make_interaction, user_id=clicker))
        is allowed
    )


# ---------------------------------------------------------------------------
# Re-render hygiene: a Components V2 edit resends the text it carries
# ---------------------------------------------------------------------------


def _fav_ctx(author_id=7, in_voice=True, voice_client=None):
    """The slice of ``commands.Context`` /playlist play actually touches."""
    voice = types.SimpleNamespace(channel=object()) if in_voice else None
    ctx = types.SimpleNamespace(
        author=types.SimpleNamespace(id=author_id, voice=voice),
        voice_client=voice_client,
        channel=types.SimpleNamespace(id=77),
        sends=[],
        defers=0,
    )

    async def defer(*_a, **_kw):
        ctx.defers += 1

    async def send(*args, **kwargs):
        ctx.sends.append((args, kwargs))

    ctx.defer = defer
    ctx.send = send
    return ctx


async def test_card_paging_never_re_pings_the_names_it_reprints(
    fake_pool, make_interaction
):
    """A nickname (or track title) shaped like <@id> must not ping on a flip."""
    card = views.FavouritesCard(
        _cog(fake_pool), 7, _member(7), [_row(str(i)) for i in range(15)]
    )

    forward = _interaction(make_interaction, user_id=7)
    await card._next(forward)
    back = _interaction(make_interaction, user_id=7)
    await card._prev(back)

    for itx in (forward, back):
        (_args, kwargs) = itx.edits[0]
        assert kwargs["allowed_mentions"].users is False


async def test_card_rerender_never_re_pings_the_names_it_reprints(fake_pool):
    card = views.FavouritesCard(_cog(fake_pool), 7, _member(7), [_row("a")])
    edits = []

    async def edit(**kwargs):
        edits.append(kwargs)

    card.message = types.SimpleNamespace(edit=edit)

    await card._rerender()

    assert edits[0]["allowed_mentions"].users is False


# ---------------------------------------------------------------------------
# /playlist play - the refusals that must come BEFORE the resolve
# ---------------------------------------------------------------------------


async def test_playlist_play_refuses_a_caller_out_of_voice_before_resolving(fake_pool):
    """No searches, no backfill write: the caller could not have heard them."""
    cog = _cog(fake_pool)
    cog._nodes_available = lambda: True
    resolved = []

    async def fetch(_user_id):
        return [_row(str(i)) for i in range(100)]

    async def resolve(*args, **kwargs):
        resolved.append(args)
        return ([], 0, 0)

    cog._fetch_favourites = fetch
    cog.resolve_favourites = resolve
    ctx = _fav_ctx(in_voice=False)

    await music.Music.playlist_play.callback(cog, ctx)

    assert resolved == []
    assert "voice channel" in ctx.sends[0][0][0]


async def test_playlist_play_still_resolves_when_the_bot_is_already_connected(
    fake_pool,
):
    """The bail is `no player AND not in voice` - a live player is enough."""
    cog = _cog(fake_pool)
    cog._nodes_available = lambda: True
    player = _Player()
    resolved = []

    async def fetch(_user_id):
        return [_row("a", encoded="enc-a")]

    async def resolve(*args, **kwargs):
        resolved.append(args)
        return ([_Track("A")], 0, 0)

    async def connect(_ctx):
        return player

    async def snapshot(_player):
        return None

    cog._fetch_favourites = fetch
    cog.resolve_favourites = resolve
    cog._connect_for_playlist = connect
    cog._snapshot = snapshot

    ctx = _fav_ctx(in_voice=False, voice_client=player)
    await music.Music.playlist_play.callback(cog, ctx)

    assert len(resolved) == 1
    assert player.played  # it actually started


async def test_playlist_play_states_the_deferred_rest_even_when_the_batch_failed(
    fake_pool,
):
    """75 rows left over must not read as "your favourites cannot be loaded"."""
    cog = _cog(fake_pool)
    cog._nodes_available = lambda: True

    async def fetch(_user_id):
        return [_row(str(i)) for i in range(100)]

    async def resolve(*_args, **_kwargs):
        return ([], 25, 75)

    cog._fetch_favourites = fetch
    cog.resolve_favourites = resolve

    ctx = _fav_ctx()
    await music.Music.playlist_play.callback(cog, ctx)

    message = ctx.sends[0][0][0]
    assert message.startswith("None of your favourites could be loaded")
    assert "75 older favourites are still waiting" in message
