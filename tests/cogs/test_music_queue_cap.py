"""Unit tests for the per-guild queue cap (lot P2).

Nothing in the music package bounded the queue before this lot: no constant, no
check, no truncation. One guild could keep queueing until the process ran out of
memory, and the JSONB restore snapshot written from that queue grew with it - a
per-guild leak with no ceiling, which at 1000+ guilds is the scale gap the scout
flagged. :data:`cogs.music.music.MAX_QUEUE_TRACKS` closes it at 500 tracks per
guild, counted over BOTH lanes (the user lane and the hidden autoplay lane).

What is pinned here:

* the pure core - ``queue_room_left`` (both lanes, clamped, None-safe),
  ``fit_queue_additions`` (the head that fits plus how many were cut, order
  preserved, no hidden writes) and the two message helpers;
* every enqueue seam and the behaviour the design arbitrated for it: a SINGLE add
  over the cap REFUSES cleanly, a BULK add TRUNCATES and says so in the same
  grammar as the existing "could not be loaded" tail, and the best-effort filler
  paths (the radio refill) skip SILENTLY;
* the counter-tests that keep the guard honest - a queue with room is never
  touched and carries no cap line, and the cap is re-checked at the put on every
  path that awaits between its cheap pre-refusal and the actual enqueue.

Everything runs on fakes (no node, no database, no gateway).
"""

import types

import sonolink
import sonolink.models

from cogs.music import music, views
from cogs.music import playlists_shared as ps

CAP = music.MAX_QUEUE_TRACKS


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title, encoded=None):
        self.title = title
        self.author = "Artist"
        self.length = 125000
        self.is_stream = False
        self.identifier = title
        self.uri = "https://example.test/{0}".format(title)
        self.source_name = "youtube"
        self.encoded = encoded if encoded is not None else "enc-" + title
        self.extras = types.SimpleNamespace(requester=None, radio=False)

    def __repr__(self):
        return "<Track {0}>".format(self.title)


class _Queue:
    """The slice of a sonolink queue the cap reads and writes (BOTH lanes)."""

    def __init__(self, tracks=(), autoplay_tracks=(), history=()):
        self._items = list(tracks)
        self._autoplay_items = list(autoplay_tracks)
        self.history = list(history)
        self.mode = None
        self.current_track = None

    @property
    def tracks(self):
        return list(self._items)

    @property
    def autoplay_tracks(self):
        return list(self._autoplay_items)

    def put(self, item):
        # Mirrors sonolink: put takes ONE track or a list of them.
        if isinstance(item, list):
            self._items.extend(item)
        else:
            self._items.append(item)

    def get(self):
        return self._items.pop(0)

    def clear(self):
        self._items.clear()


class _Player(sonolink.Player):
    """A real ``sonolink.Player`` subclass (some seams isinstance-check it),
    built without a node or a gateway connection."""

    def __init__(self, queued=0, current=None, autoplay_queued=0, history=()):
        # Deliberately no super().__init__: a real Player wants a live node.
        # ``queue`` and ``current`` are read-only properties on the base, so the
        # fakes go in the private slots the base reads them from.
        self._queue = _Queue(
            [_Track("Q{0}".format(i)) for i in range(queued)],
            [_Track("A{0}".format(i)) for i in range(autoplay_queued)],
            history,
        )
        self._current = current
        self.channel = types.SimpleNamespace(
            name="General", guild=types.SimpleNamespace(id=99)
        )
        self.home = None
        self.dj = None
        self.radio_genre = "lofi"
        self.played_ids = []
        self.played = []

    @property
    def queue(self):
        return self._queue

    @property
    def current(self):
        return self._current

    async def play(self, track):
        self.played.append(track)
        self._current = track


class _Playlist(sonolink.models.Playlist):
    """A real ``Playlist`` for the isinstance branch, with no client or node."""

    def __init__(self, name, tracks):
        # Deliberately no super().__init__: it wants a client and raw data.
        self._fake_name = name
        self._fake_tracks = list(tracks)

    @property
    def name(self):
        return self._fake_name

    @property
    def tracks(self):
        return list(self._fake_tracks)


class _Result:
    def __init__(self, result):
        self.result = result

    def is_error(self):
        return False

    def is_empty(self):
        return self.result is None


class _SLClient:
    def __init__(self, answer=None):
        self.answer = answer
        self.searches = []

    async def search_track(self, query, source=None):
        self.searches.append(query)
        return _Result(self.answer)


def _cog(sl_client=None, player=None):
    """A Music cog with no ``__init__`` side effects (it starts a task loop)."""
    cog = music.Music.__new__(music.Music)
    cog.bot = types.SimpleNamespace(sl_client=sl_client or _SLClient(), db_pool=None)
    cog.snapshots = 0
    cog._nodes_available = lambda: True

    async def snapshot(_player, track=None):
        cog.snapshots += 1

    async def connect(_ctx):
        return player

    cog._snapshot = snapshot
    cog._connect_for_playlist = connect
    return cog


def _ctx(player=None, author_id=7):
    ctx = types.SimpleNamespace(
        author=types.SimpleNamespace(
            id=author_id, voice=types.SimpleNamespace(channel=object())
        ),
        voice_client=player,
        channel=types.SimpleNamespace(id=77),
        guild=types.SimpleNamespace(id=99),
        sends=[],
    )

    async def defer(*_a, **_kw):
        return None

    async def send(*args, **kwargs):
        ctx.sends.append((args, kwargs))

    ctx.defer = defer
    ctx.send = send
    return ctx


def _last_message(ctx):
    return ctx.sends[-1][0][0]


def _full(count=CAP):
    return [_Track("F{0}".format(i)) for i in range(count)]


# ---------------------------------------------------------------------------
# The arbitration itself
# ---------------------------------------------------------------------------


def test_the_cap_is_finite_and_generous():
    """Pinned so the arbitration stays a decision, not a refactor accident."""
    assert music.MAX_QUEUE_TRACKS == 500


# ---------------------------------------------------------------------------
# queue_room_left - the in-memory check
# ---------------------------------------------------------------------------


def test_room_left_is_the_whole_cap_on_an_empty_queue():
    assert music.queue_room_left(_Queue()) == CAP


def test_room_left_counts_the_hidden_autoplay_lane_too():
    """The cap cannot be walked around by filling the lane users never see."""
    queue = _Queue([_Track("a")], [_Track("r"), _Track("s")])

    assert music.queue_room_left(queue) == CAP - 3


def test_room_left_is_zero_exactly_at_the_cap():
    assert music.queue_room_left(_Queue(_full())) == 0


def test_room_left_clamps_instead_of_going_negative():
    """An old snapshot (or a cap lowered between versions) must read as "no
    room", never as a negative slice that would silently accept tracks."""
    assert music.queue_room_left(_Queue(_full(CAP + 25))) == 0


def test_room_left_is_none_safe():
    # A voice client with no queue at all reads as "all the room in the world",
    # so the cheap pre-refusals never fire on a session about to be born.
    assert music.queue_room_left(None) == CAP
    assert music.queue_room_left(types.SimpleNamespace()) == CAP
    assert music.queue_room_left(
        types.SimpleNamespace(tracks=None, autoplay_tracks=None)
    ) == CAP


# ---------------------------------------------------------------------------
# fit_queue_additions - the bulk split
# ---------------------------------------------------------------------------


def test_fit_keeps_everything_when_it_fits_and_preserves_order():
    tracks = [_Track("a"), _Track("b"), _Track("c")]

    accepted, dropped = music.fit_queue_additions(_Queue(), tracks)

    assert [t.title for t in accepted] == ["a", "b", "c"]
    assert dropped == 0


def test_fit_truncates_to_the_room_and_reports_the_cut():
    queue = _Queue(_full(CAP - 2))
    tracks = [_Track("a"), _Track("b"), _Track("c"), _Track("d")]

    accepted, dropped = music.fit_queue_additions(queue, tracks)

    assert [t.title for t in accepted] == ["a", "b"]  # the HEAD, in order
    assert dropped == 2


def test_fit_drops_everything_on_a_full_queue():
    accepted, dropped = music.fit_queue_additions(
        _Queue(_full()), [_Track("a"), _Track("b")]
    )

    assert accepted == []
    assert dropped == 2


def test_fit_materialises_a_generator_once():
    """Callers pass generators; counting must not consume the tracks away."""
    accepted, dropped = music.fit_queue_additions(
        _Queue(), (_Track(str(i)) for i in range(3))
    )

    assert len(accepted) == 3
    assert dropped == 0


def test_fit_never_mutates_the_queue():
    """It decides; the caller enqueues. Keeps the seam free of hidden writes."""
    queue = _Queue([_Track("x")])

    music.fit_queue_additions(queue, [_Track("a")])

    assert [t.title for t in queue.tracks] == ["x"]


# ---------------------------------------------------------------------------
# The messages
# ---------------------------------------------------------------------------


def test_queue_full_message_states_the_cap():
    message = music.queue_full_message()

    assert str(CAP) in message
    assert "full" in message


def test_queue_full_suffix_is_empty_when_nothing_was_cut():
    assert music.queue_full_suffix(0) == ""
    assert music.queue_full_suffix(-1) == ""


def test_queue_full_suffix_singular_and_plural():
    one = music.queue_full_suffix(1)
    many = music.queue_full_suffix(12)

    assert "1 track was not added" in one
    assert "12 tracks were not added" in many
    assert str(CAP) in one
    assert str(CAP) in many


def test_queue_full_suffix_composes_after_a_sentence():
    """Same shape as the "could not be loaded" tail, so ONE message carries both
    facts instead of inventing a second grammar for the same kind of bad news."""
    message = "Queued 3 tracks." + music.queue_full_suffix(4)

    assert message.startswith("Queued 3 tracks. ")
    assert message.endswith(".")


# ---------------------------------------------------------------------------
# Seam: /play <query> - a single add REFUSES, a playlist TRUNCATES
# ---------------------------------------------------------------------------


async def test_play_query_refuses_a_full_queue_before_it_even_searches():
    player = _Player(queued=CAP, current=_Track("Now"))
    client = _SLClient(answer=_Track("New"))
    cog = _cog(client)
    ctx = _ctx(player)

    await cog._play_query(ctx, "some song")

    assert client.searches == []  # no round trip spent for a caller with no room
    assert str(CAP) in _last_message(ctx)
    assert len(player.queue.tracks) == CAP


async def test_play_query_counts_the_autoplay_lane_in_the_refusal():
    """Half the cap staged by autoplay still fills the queue."""
    player = _Player(queued=CAP // 2, autoplay_queued=CAP - CAP // 2)
    client = _SLClient(answer=_Track("New"))
    cog = _cog(client)
    ctx = _ctx(player)

    await cog._play_query(ctx, "some song")

    assert client.searches == []
    assert str(CAP) in _last_message(ctx)


async def test_play_query_still_queues_a_single_track_with_room_left():
    player = _Player(queued=CAP - 1, current=_Track("Now"))
    cog = _cog(_SLClient(answer=_Track("New")))
    ctx = _ctx(player)

    await cog._play_query(ctx, "some song")

    assert player.queue.tracks[-1].title == "New"
    assert "Added **New**" in _last_message(ctx)


async def test_play_query_truncates_a_playlist_to_the_room_and_says_so():
    player = _Player(queued=CAP - 3, current=_Track("Now"))
    playlist = _Playlist("Road Trip", [_Track("p{0}".format(i)) for i in range(10)])
    cog = _cog(_SLClient(answer=playlist))
    ctx = _ctx(player)

    await cog._play_query(ctx, "a playlist url")

    assert len(player.queue.tracks) == CAP  # filled exactly to the cap, never past
    message = _last_message(ctx)
    assert "(3 tracks)" in message
    assert "7 tracks were not added" in message
    assert str(CAP) in message


async def test_play_query_playlist_truncated_to_one_track_reads_singular():
    """The cap made "exactly one accepted track" an ordinary outcome, so the
    count line is plural-aware: "(1 track)", never "(1 tracks)"."""
    player = _Player(queued=CAP - 1, current=_Track("Now"))
    playlist = _Playlist("Road Trip", [_Track("p{0}".format(i)) for i in range(6)])
    cog = _cog(_SLClient(answer=playlist))
    ctx = _ctx(player)

    await cog._play_query(ctx, "a playlist url")

    message = _last_message(ctx)
    assert "(1 track)" in message
    assert "(1 tracks)" not in message
    # ... and the cap tail beside it stays plural for the 5 it cut.
    assert "5 tracks were not added" in message


async def test_play_query_playlist_that_fits_carries_no_cap_line():
    player = _Player(current=_Track("Now"))
    playlist = _Playlist("Road Trip", [_Track("p{0}".format(i)) for i in range(10)])
    cog = _cog(_SLClient(answer=playlist))
    ctx = _ctx(player)

    await cog._play_query(ctx, "a playlist url")

    assert len(player.queue.tracks) == 10
    assert "not added" not in _last_message(ctx)


async def test_play_query_only_tags_the_playlist_tracks_it_queued():
    """A dropped track must not come back later wearing a requester it never
    got - the tag goes on the accepted head only."""
    player = _Player(queued=CAP - 2, current=_Track("Now"))
    tracks = [_Track("p{0}".format(i)) for i in range(5)]
    cog = _cog(_SLClient(answer=_Playlist("Mix", tracks)))

    await cog._play_query(_ctx(player), "a playlist url")

    assert [t.extras.requester for t in tracks] == [7, 7, None, None, None]


# ---------------------------------------------------------------------------
# Seam: /playlist play (favourites) - cheap refusal, then truncation
# ---------------------------------------------------------------------------


def _fav_cog(player, tracks, *, skipped=0, deferred=0):
    cog = _cog(player=player)
    cog.resolved = []

    async def fetch(_user_id):
        return [{"identifier": "a"}]

    async def resolve(*args, **_kwargs):
        cog.resolved.append(args)
        return (list(tracks), skipped, deferred)

    cog._fetch_favourites = fetch
    cog.resolve_favourites = resolve
    return cog


async def test_favourites_play_refuses_a_full_queue_before_resolving():
    """The resolve can cost a batch of searches plus a backfill write - never
    spend it for a caller whose queue cannot take a single track."""
    player = _Player(queued=CAP, current=_Track("Now"))
    cog = _fav_cog(player, [_Track("A")])
    ctx = _ctx(player)

    await music.Music.playlist_play.callback(cog, ctx)

    assert cog.resolved == []
    assert str(CAP) in _last_message(ctx)


async def test_favourites_play_truncates_and_states_the_cut():
    player = _Player(queued=CAP - 2, current=_Track("Now"))
    cog = _fav_cog(player, [_Track("f{0}".format(i)) for i in range(6)])
    ctx = _ctx(player)

    await music.Music.playlist_play.callback(cog, ctx)

    assert len(player.queue.tracks) == CAP
    message = _last_message(ctx)
    assert "Queued 2 tracks from your favourites." in message
    assert "4 tracks were not added" in message


async def test_favourites_play_keeps_the_skipped_line_alongside_the_cap_line():
    """Both kinds of bad news fit in one message, in one grammar."""
    player = _Player(queued=CAP - 1, current=_Track("Now"))
    cog = _fav_cog(player, [_Track("A"), _Track("B")], skipped=3)
    ctx = _ctx(player)

    await music.Music.playlist_play.callback(cog, ctx)

    message = _last_message(ctx)
    assert "3 tracks were skipped" in message
    assert "1 track was not added" in message


async def test_favourites_play_with_room_carries_no_cap_line():
    player = _Player(current=_Track("Now"))
    cog = _fav_cog(player, [_Track("A"), _Track("B")])
    ctx = _ctx(player)

    await music.Music.playlist_play.callback(cog, ctx)

    assert len(player.queue.tracks) == 2
    assert "not added" not in _last_message(ctx)


async def test_favourites_play_refuses_when_the_queue_filled_during_the_resolve():
    """The cheap pre-refusal saw room; the put is what actually decides."""
    player = _Player(current=_Track("Now"))
    cog = _fav_cog(player, [_Track("A")])

    async def resolve(*_args, **_kwargs):
        # A bulk load lands while the favourites are being resolved.
        player.queue.put(_full())
        return ([_Track("A")], 0, 0)

    cog.resolve_favourites = resolve
    ctx = _ctx(player)

    await music.Music.playlist_play.callback(cog, ctx)

    assert len(player.queue.tracks) == CAP
    assert str(CAP) in _last_message(ctx)


# ---------------------------------------------------------------------------
# Seam: /serverplaylist play - the shared bulk load
# ---------------------------------------------------------------------------


def _shared_cog(player, stored):
    cog = _cog(player=player)

    async def decode_tracks(*_blobs):
        return list(stored)

    async def fetch(_guild_id, _norm):
        return {"name": "Road Trip", "tracks": ["enc"] * len(stored)}

    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=decode_tracks)
    cog._fetch_guild_playlist = fetch
    return cog


async def test_serverplaylist_play_truncates_and_states_the_cut():
    player = _Player(queued=CAP - 4, current=_Track("Now"))
    stored = [_Track("s{0}".format(i)) for i in range(10)]
    ctx = _ctx(player)

    await ps.ServerPlaylistMixin.serverplaylist_play.callback(
        _shared_cog(player, stored), ctx, name="Road"
    )

    assert len(player.queue.tracks) == CAP
    message = _last_message(ctx)
    assert "Queued 4 tracks from **Road Trip**." in message
    assert "6 tracks were not added" in message


async def test_serverplaylist_play_refuses_outright_on_a_full_queue():
    player = _Player(queued=CAP, current=_Track("Now"))
    ctx = _ctx(player)

    await ps.ServerPlaylistMixin.serverplaylist_play.callback(
        _shared_cog(player, [_Track("s0"), _Track("s1")]), ctx, name="Road"
    )

    assert len(player.queue.tracks) == CAP
    assert str(CAP) in _last_message(ctx)
    assert "Queued" not in _last_message(ctx)


async def test_serverplaylist_play_with_room_carries_no_cap_line():
    player = _Player(current=_Track("Now"))
    ctx = _ctx(player)

    await ps.ServerPlaylistMixin.serverplaylist_play.callback(
        _shared_cog(player, [_Track("s0"), _Track("s1")]), ctx, name="Road"
    )

    assert len(player.queue.tracks) == 2
    assert "not added" not in _last_message(ctx)


# ---------------------------------------------------------------------------
# Seam: the "Add a song" modal (controller + queue view) - a SINGLE add
# ---------------------------------------------------------------------------


class _Owner:
    def __init__(self, player):
        self.player = player
        self.rerenders = 0

    async def _rerender(self):
        self.rerenders += 1


def _modal(cog, owner, query="a song"):
    modal = views.AddSongModal(cog, owner)
    modal.song = types.SimpleNamespace(value=query)
    return modal


async def test_add_song_modal_refuses_a_full_queue_without_searching(
    make_interaction,
):
    player = _Player(queued=CAP, current=_Track("Now"))
    cog = _cog()
    searched = []

    async def search(query):
        searched.append(query)
        return _Result(_Track("New"))

    cog._search = search
    owner = _Owner(player)
    interaction = make_interaction(user_id=7)

    await _modal(cog, owner).on_submit(interaction)

    assert searched == []
    assert str(CAP) in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True
    assert len(player.queue.tracks) == CAP
    assert owner.rerenders == 0


async def test_add_song_modal_refuses_when_the_queue_filled_during_the_search(
    make_interaction,
):
    """The seam re-checks at the put, so one lucky ordering is not the guard."""
    player = _Player(queued=CAP - 1, current=_Track("Now"))
    cog = _cog()

    async def search(_query):
        player.queue.put(_Track("Late"))  # a bulk load takes the last slot
        return _Result(_Track("New"))

    cog._search = search
    interaction = make_interaction(user_id=7)

    await _modal(cog, _Owner(player)).on_submit(interaction)

    assert len(player.queue.tracks) == CAP
    assert player.queue.tracks[-1].title == "Late"
    assert str(CAP) in interaction.sent[0][0][0]


async def test_add_song_modal_still_queues_with_one_slot_left(make_interaction):
    player = _Player(queued=CAP - 1, current=_Track("Now"))
    cog = _cog()

    async def search(_query):
        return _Result(_Track("New"))

    cog._search = search
    owner = _Owner(player)

    await _modal(cog, owner).on_submit(make_interaction(user_id=7))

    assert player.queue.tracks[-1].title == "New"
    assert owner.rerenders == 1


# ---------------------------------------------------------------------------
# Seam: the history card's re-queue button - a SINGLE add
# ---------------------------------------------------------------------------


def _history_card(player):
    card = views.HistoryCard(_cog(), player)
    card.page = 0
    card._build()
    card.message = types.SimpleNamespace()
    return card


async def test_history_requeue_refuses_a_full_queue(make_interaction):
    played = _Track("Old")
    player = _Player(queued=CAP, current=_Track("Now"), history=[played])
    card = _history_card(player)
    interaction = make_interaction(user_id=7)

    await card._requeue(interaction, 0, played)

    assert len(player.queue.tracks) == CAP
    assert str(CAP) in interaction.sent[0][0][0]
    assert player.radio_genre == "lofi"  # a refused add does not end the radio


async def test_history_requeue_still_works_with_room_left(make_interaction):
    played = _Track("Old")
    player = _Player(queued=CAP - 1, current=_Track("Now"), history=[played])
    card = _history_card(player)

    await card._requeue(make_interaction(user_id=7), 0, played)

    assert player.queue.tracks[-1].title == "Old"


# ---------------------------------------------------------------------------
# Seam: the favourites card's play button - a SINGLE add
# ---------------------------------------------------------------------------


def _fav_row(identifier="a"):
    return {
        "identifier": identifier,
        "title": "Song " + identifier,
        "author": "Artist",
        "uri": "https://example.test/x",
        "source_name": "youtube",
        "encoded": "enc-" + identifier,
    }


def _card_interaction(make_interaction, player=None, **kwargs):
    itx = make_interaction(**kwargs)
    itx.channel = types.SimpleNamespace(id=77)
    itx.guild = types.SimpleNamespace(voice_client=player)
    itx.original_edits = []

    async def edit_original_response(**kw):
        itx.original_edits.append(kw)

    itx.edit_original_response = edit_original_response
    return itx


async def test_favourites_card_play_refuses_a_full_queue_before_resolving(
    make_interaction,
):
    player = _Player(queued=CAP, current=_Track("Now"))
    cog = _cog(player=player)
    resolved = []

    async def resolve(*args, **_kwargs):
        resolved.append(args)
        return ([_Track("A")], 0, 0)

    cog.resolve_favourites = resolve
    row = _fav_row()
    card = views.FavouritesCard(cog, 7, types.SimpleNamespace(id=7, display_name="Mio"), [row])
    interaction = _card_interaction(make_interaction, player, user_id=7)

    played = await card._play_one(interaction, row)

    assert played is False
    assert resolved == []
    assert str(CAP) in interaction.followups[0][0][0]


async def test_favourites_card_play_refuses_when_the_queue_filled_mid_resolve(
    make_interaction,
):
    player = _Player(current=_Track("Now"))
    cog = _cog(player=player)

    async def resolve(*_args, **_kwargs):
        player.queue.put(_full())
        return ([_Track("A")], 0, 0)

    cog.resolve_favourites = resolve
    row = _fav_row()
    card = views.FavouritesCard(cog, 7, types.SimpleNamespace(id=7, display_name="Mio"), [row])
    interaction = _card_interaction(make_interaction, None, user_id=7)

    played = await card._play_one(interaction, row)

    assert played is False
    assert len(player.queue.tracks) == CAP
    assert str(CAP) in interaction.followups[0][0][0]


# ---------------------------------------------------------------------------
# Seam: the radio - a seed TRUNCATES, a refill skips SILENTLY
# ---------------------------------------------------------------------------


class _Genre:
    key = "lofi"
    label = "Lofi"
    query_trending = "q1"
    query_alltime = "q2"


async def test_genre_seed_truncates_to_the_room_and_returns_only_what_it_queued():
    """Both callers print "({count} track(s))" from the returned list, so the
    seed must return what it QUEUED for that line to stay honest."""
    player = _Player(queued=CAP - 2, current=_Track("Now"))
    cog = _cog()
    found = [_Track("g{0}".format(i)) for i in range(5)]

    async def search_genre(_genre, _seen):
        return 1, list(found)

    cog._search_genre_tracks = search_genre

    _tier, queued = await cog._apply_genre(player, _Genre(), 7, replace=False)

    assert len(queued) == 2
    assert len(player.queue.tracks) == CAP


async def test_genre_seed_returns_nothing_on_a_full_queue_and_leaves_the_player():
    player = _Player(queued=CAP, current=_Track("Now"))
    cog = _cog()

    async def search_genre(_genre, _seen):
        return 1, [_Track("g0")]

    cog._search_genre_tracks = search_genre

    _tier, queued = await cog._apply_genre(player, _Genre(), 7, replace=False)

    assert queued == []
    assert len(player.queue.tracks) == CAP
    assert player.played == []
    assert cog.snapshots == 0


async def test_a_zap_is_never_blocked_by_a_full_queue():
    """replace=True purges BOTH lanes first, so the station always has room -
    a full queue must not strand a guild on its old station."""
    player = _Player(queued=CAP, autoplay_queued=10, current=_Track("Now"))
    cog = _cog()
    found = [_Track("g{0}".format(i)) for i in range(8)]

    async def search_genre(_genre, _seen):
        return 1, list(found)

    cog._search_genre_tracks = search_genre

    _tier, queued = await cog._apply_genre(player, _Genre(), 7, replace=True)

    assert len(queued) == 8
    assert player.radio_genre == "lofi"
    assert player.played  # the new station actually started


async def test_radio_refill_skips_a_full_queue_silently_and_without_searching():
    """A refill is best-effort filler with no invoker to tell: it costs nothing
    and says nothing, and the next track-start simply tries again."""
    player = _Player(queued=CAP, current=_Track("Now"))
    cog = _cog()
    searched = []

    async def search_genre(_genre, _seen):
        searched.append(1)
        return 1, [_Track("g0")]

    cog._search_genre_tracks = search_genre

    await cog._radio_refill(player)

    assert searched == []
    assert len(player.queue.tracks) == CAP
    assert cog.snapshots == 0
    assert player._radio_refilling is False


async def test_radio_refill_truncates_when_the_queue_fills_mid_search():
    player = _Player(queued=CAP - 3, current=_Track("Now"))
    cog = _cog()

    async def search_genre(_genre, _seen):
        return 1, [_Track("g{0}".format(i)) for i in range(6)]

    cog._search_genre_tracks = search_genre

    await cog._radio_refill(player)

    assert len(player.queue.tracks) == CAP


async def test_radio_refill_still_fills_a_queue_with_room():
    player = _Player(queued=10, current=_Track("Now"))
    cog = _cog()

    async def search_genre(_genre, _seen):
        return 1, [_Track("g{0}".format(i)) for i in range(4)]

    cog._search_genre_tracks = search_genre

    await cog._radio_refill(player)

    assert len(player.queue.tracks) == 14
    assert cog.snapshots == 1


# ---------------------------------------------------------------------------
# Seam: the COLD RESTORE truncates a pre-cap snapshot, silently
# ---------------------------------------------------------------------------
#
# This is the one seam that exists purely as a defence against state no other
# path can still produce: a music_state row written by a build from BEFORE
# MAX_QUEUE_TRACKS existed can carry more queued tracks than the cap now allows.
# Every live path is capped at the put, so nothing in the running process can
# create such a row any more - which is exactly why it needs a test. Without one,
# a refactor could drop the truncation and nothing would fail, quietly bringing
# the unbounded queue back through the restore door.
#
# Silent by design: a cold restore speaks through the controller it reposts, and
# there is no invoker standing there to tell. It logs, it does not chat.


class _RestorePlayer(_Player):
    """A ``_Player`` drivable by ``_restore_one``.

    ``autoplay`` is redeclared as a plain class attribute so it shadows the real
    ``sonolink.Player.autoplay`` property, whose setter reaches into a live
    history handler this fake has no business growing.
    """

    autoplay = None
    controller = object()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.play_calls = []

    async def play(self, track, **kwargs):
        self.play_calls.append((track, kwargs))
        self._current = track


class _RestoreChannel:
    """A voice channel with one human in it, so the restore rejoins."""

    def __init__(self):
        self.members = [types.SimpleNamespace(bot=False)]
        self.guild = types.SimpleNamespace(id=99)


def _restore_row(queued, **overrides):
    """A music_state row for guild 99 carrying ``queued`` encoded queue tracks."""
    row = {
        "guild_id": 99,
        "updated_at": music.datetime(2026, 1, 1, tzinfo=music.timezone.utc),
        "current_track": "enc-Now",
        "queue": ["enc-R{0}".format(i) for i in range(queued)],
        "voice_channel_id": 1,
        "home_channel_id": None,  # -> the voice-channel fallback, no stale delete
        "controller_message_id": None,
        "dj_id": None,
        "loop_mode": 0,
        "position_ms": 0,
        "paused": False,
        "volume": 100,
        "autoplay": True,
        "radio_genre": None,
        "effect": None,
    }
    row.update(overrides)
    return row


def _restore_cog(player, monkeypatch):
    """A Music cog wired for ``_restore_one`` against ``player`` in guild 99."""

    channel = _RestoreChannel()
    guild = types.SimpleNamespace(
        id=99,
        voice_client=player,
        get_channel=lambda _cid: channel,
        get_member=lambda _mid: None,
    )

    class _DecodingClient:
        async def decode_tracks(self, *encoded):
            return [_Track(e.replace("enc-", ""), encoded=e) for e in encoded]

    cog = music.Music.__new__(music.Music)
    cog.bot = types.SimpleNamespace(
        get_guild=lambda _gid: guild, sl_client=_DecodingClient(), db_pool=None
    )
    cog.cleared = []
    cog.controllers = []

    async def clear(guild_id):
        cog.cleared.append(guild_id)

    async def send_controller(_player, dedupe=False):
        cog.controllers.append(dedupe)

    cog._clear = clear
    cog._send_controller = send_controller
    cog._settings_pool = lambda: None

    # isinstance gates: the saved channel must read as a voice channel, and the
    # existing voice_client as one of OUR players (so no connect is attempted).
    monkeypatch.setattr(music.discord, "VoiceChannel", _RestoreChannel)
    monkeypatch.setattr(music.discord, "StageChannel", _RestoreChannel)
    monkeypatch.setattr(music, "Player", type(player))

    async def sponsorblock_enabled(_pool, _gid):
        return False

    monkeypatch.setattr(
        music.guild_config, "sponsorblock_enabled", sponsorblock_enabled
    )
    return cog


def _restore_now():
    """A ``now`` a few seconds after the row, so the age check passes."""
    return music.datetime(2026, 1, 1, 0, 0, 30, tzinfo=music.timezone.utc)


async def test_restore_truncates_a_pre_cap_snapshot_to_the_cap(monkeypatch, caplog):
    player = _RestorePlayer()
    cog = _restore_cog(player, monkeypatch)
    # A snapshot from before the cap existed: 120 tracks over it.
    row = _restore_row(CAP + 120)

    with caplog.at_level("WARNING", logger="cogs.music.music"):
        await cog._restore_one(row, _restore_now())

    # Exactly the cap came back, in order, and the session really resumed.
    assert len(player.queue.tracks) == CAP
    assert player.queue.tracks[0].title == "R0"
    assert cog.cleared == []
    assert len(player.play_calls) == 1
    assert cog.controllers == [True]
    # Silent to the guild (no chat seam is even wired here), loud in the log.
    assert any(
        "dropped" in r.getMessage() and str(CAP) in r.getMessage()
        for r in caplog.records
    )


async def test_restore_leaves_a_snapshot_under_the_cap_untouched(monkeypatch, caplog):
    """Counter-test: the guard is a truncation, not a resize - a normal restore
    comes back whole and logs no cap warning."""
    player = _RestorePlayer()
    cog = _restore_cog(player, monkeypatch)
    row = _restore_row(12)

    with caplog.at_level("WARNING", logger="cogs.music.music"):
        await cog._restore_one(row, _restore_now())

    assert len(player.queue.tracks) == 12
    assert not any("dropped" in r.getMessage() for r in caplog.records)


async def test_restore_at_exactly_the_cap_keeps_everything_and_stays_quiet(
    monkeypatch, caplog
):
    player = _RestorePlayer()
    cog = _restore_cog(player, monkeypatch)
    row = _restore_row(CAP)

    with caplog.at_level("WARNING", logger="cogs.music.music"):
        await cog._restore_one(row, _restore_now())

    assert len(player.queue.tracks) == CAP
    assert not any("dropped" in r.getMessage() for r in caplog.records)
