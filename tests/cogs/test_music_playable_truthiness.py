"""Regression tests for the Playable-truthiness bug across cogs/music.

sonolink's ``Playable`` (``sonolink/models/track.py``) defines ``__len__`` -
the track length in milliseconds - and no ``__bool__``. Python falls back from
``__bool__`` to ``__len__``, so ``bool(track)`` is really
``track.length != 0``: a genuinely playing track whose length is 0 (a stream
mid-probe, or the partially built track from the cold-restore race - see
``views.py``'s "length-less tracks") is FALSY. Code that asked "is something
playing?" with ``if player.current:`` / ``if not player.current:`` got that
case wrong - see the fixed sites in music.py / views.py / player.py /
playlists_shared.py for the full list.

Every fake track here implements ``__len__`` exactly like the real
``Playable``, so a ``length=0`` fake is ACTUALLY falsy - unlike a bare
``types.SimpleNamespace(length=0)`` (Python looks up ``__len__`` on the type,
never on an instance's own attributes, so a SimpleNamespace is always truthy
regardless of any ``length`` it carries). Without that, these tests would
pass whether or not the bug was present - the empty-result-is-not-absence
trap this package has already been bitten by once.
"""

from __future__ import annotations

import types

import sonolink

from cogs.music import music, voteskip
from cogs.system import dashboard_music_actions as dma
from tools import settings

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title="song", length=1000, identifier=None, encoded=None):
        self.title = title
        self.author = "Artist"
        self.length = length
        self.is_stream = False
        self.identifier = identifier or title
        self.uri = "https://example.test/" + title
        self.source_name = "youtube"
        self.encoded = encoded if encoded is not None else "enc-" + title
        self.extras = types.SimpleNamespace(requester=None, radio=False)

    def __len__(self):
        # The one line that makes a zero-length fake behave like the real
        # Playable: falsy exactly when the track is zero-length.
        return self.length

    def __repr__(self):
        return "<_Track {0} len={1}>".format(self.title, self.length)


class _Queue:
    def __init__(self, tracks=()):
        self._items = list(tracks)
        self.mode = sonolink.QueueMode.NORMAL

    @property
    def tracks(self):
        return list(self._items)

    @property
    def autoplay_tracks(self):
        return []

    def put(self, item):
        if isinstance(item, list):
            self._items.extend(item)
        else:
            self._items.append(item)

    def get(self):
        return self._items.pop(0)

    def __len__(self):
        return len(self._items)


class _Player(sonolink.Player):
    """A real ``sonolink.Player`` subclass (some seams isinstance-check it)."""

    def __init__(self, *, current=None, queued=()):
        # Deliberately no super().__init__: a real Player wants a live node.
        self._current = current
        self._queue = _Queue(queued)
        self.channel = types.SimpleNamespace(
            name="General", guild=types.SimpleNamespace(id=99)
        )
        self.home = types.SimpleNamespace(id=77)
        self.dj = None
        self.radio_genre = None
        self.played = []
        self.controller = None
        # resume_after_track_change reads the real Player.paused property,
        # which reads this backing attribute.
        self._paused = False

    @property
    def queue(self):
        return self._queue

    @property
    def current(self):
        return self._current

    async def play(self, track):
        self.played.append(track)
        self._current = track

    async def skip(self):
        # Overridden per-test via monkeypatch-free assignment below.
        raise NotImplementedError


class _SLClient:
    def __init__(self, answer=None):
        self.answer = answer
        self.searches = []

    async def search_track(self, query, source=None):
        self.searches.append(query)
        return _Result(self.answer)


class _Result:
    def __init__(self, result):
        self.result = result

    def is_error(self):
        return False

    def is_empty(self):
        return self.result is None


def _cog(sl_client=None, player=None):
    """A Music cog with no ``__init__`` side effects (it starts a task loop)."""
    cog = music.Music.__new__(music.Music)
    cog.bot = types.SimpleNamespace(sl_client=sl_client or _SLClient(), db_pool=None)
    cog.snapshots = 0
    cog.cleared = []
    cog._nodes_available = lambda: True

    async def snapshot(_player, track=None):
        cog.snapshots += 1

    async def clear(guild_id):
        cog.cleared.append(guild_id)

    async def connect(_ctx):
        return player

    cog._snapshot = snapshot
    cog._clear = clear
    cog._connect_for_playlist = connect
    return cog


def _ctx(player, author_id=7):
    voice_channel = getattr(player, "channel", None) or object()
    ctx = types.SimpleNamespace(
        author=types.SimpleNamespace(
            id=author_id, voice=types.SimpleNamespace(channel=voice_channel)
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


# ---------------------------------------------------------------------------
# /play with a zero-length current track: the new track is queued, the
# current one is NOT replaced (music.py's ``_play_query``, the fixed site at
# "if player.current is None:" just before the play() call).
# ---------------------------------------------------------------------------


async def test_play_query_zero_length_current_is_not_replaced():
    current = _Track("Now Playing", length=0)
    player = _Player(current=current)
    client = _SLClient(answer=_Track("New"))
    cog = _cog(client)
    ctx = _ctx(player)

    await cog._play_query(ctx, "some song")

    # The zero-length track is still genuinely playing: it must not be
    # silently swapped out by a queue add.
    assert player.current is current
    assert player.played == []
    assert player.queue.tracks[-1].title == "New"
    assert "Added **New**" in _last_message(ctx)


async def test_play_query_none_current_still_starts_playback():
    """Counter-test: with nothing at all playing, the add DOES start it."""
    player = _Player(current=None)
    client = _SLClient(answer=_Track("New"))
    cog = _cog(client)
    ctx = _ctx(player)

    await cog._play_query(ctx, "some song")

    assert player.played == [player.current]
    assert player.current.title == "New"


# ---------------------------------------------------------------------------
# _execute_skip with a zero-length next track: reported as an advance,
# music_state NOT cleared (music.py:3216, "if track is not None:").
# ---------------------------------------------------------------------------


async def test_execute_skip_zero_length_next_track_is_an_advance():
    next_track = _Track("Next", length=0)
    player = _Player(current=_Track("Now"), queued=[_Track("Next")])

    async def skip():
        return next_track

    player.skip = skip
    cog = _cog(player=player)

    result, track = await cog._execute_skip(player)

    assert result == voteskip.SKIP_RESULT_ADVANCED
    assert track is next_track
    assert cog.cleared == []  # music_state must NOT be torn down


async def test_execute_skip_empty_queue_is_still_reported_as_ended():
    """Counter-test: a real end (skip() returns None) still clears state."""
    player = _Player(current=_Track("Now"), queued=[_Track("Filler")])

    async def skip():
        return None

    player.skip = skip
    cog = _cog(player=player)

    result, track = await cog._execute_skip(player)

    assert result == voteskip.SKIP_RESULT_ENDED
    assert track is None
    assert cog.cleared == [99]  # playerinfo.guild_id_of(player) via player.channel


# ---------------------------------------------------------------------------
# /nowplaying with a zero-length current track: shows it, never says nothing
# is playing (music.py:3481 / nowplaying, "... or player.current is None:").
# ---------------------------------------------------------------------------


async def test_nowplaying_zero_length_current_is_shown_not_nothing_playing():
    player = _Player(current=_Track("Now Playing", length=0))
    cog = music.Music.__new__(music.Music)
    reposted = []

    async def repost(ctx, player_arg, **kwargs):
        reposted.append((player_arg, kwargs))

    cog._repost_controller = repost
    ctx = _ctx(player)

    await music.Music.nowplaying.callback(cog, ctx)

    assert reposted == [(player, {"may_read_elsewhere": True})]
    assert ctx.sends == []  # never claimed nothing is playing


async def test_nowplaying_with_nothing_playing_says_so():
    """Counter-test: a real empty player still gets the refusal message."""
    player = _Player(current=None)
    cog = music.Music.__new__(music.Music)
    cog._repost_controller = None  # must never be reached
    ctx = _ctx(player)

    await music.Music.nowplaying.callback(cog, ctx)

    assert "Nothing is playing right now." in _last_message(ctx)


# ---------------------------------------------------------------------------
# Dashboard skip executor: does not report ended=True for a zero-length next
# track (cogs/system/dashboard_music_actions._exec_music_skip, routed
# entirely through the real ``Music._execute_skip`` above).
# ---------------------------------------------------------------------------


class _FakeGuild:
    def __init__(self, guild_id, voice_client, preferred_locale="en"):
        self.id = guild_id
        self.voice_client = voice_client
        self.preferred_locale = preferred_locale


class _Pool:
    async def fetchval(self, *_a, **_kw):
        return None


class _FakeBot:
    def __init__(self, guild, cog):
        self.db_pool = _Pool()
        self._guilds = {guild.id: guild}
        self._cogs = {"Music": cog}

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)

    def get_cog(self, name):
        return self._cogs.get(name)


def _dashboard_cog(player):
    """A Music cog whose ``_execute_skip`` is the REAL engine (not a stand-in),
    so the dashboard executor is exercised against the actual fixed code path,
    not a fake that would hide the regression."""
    cog = _cog(player=player)
    cog.skip_votes = voteskip.SkipVotes()
    return cog


async def test_dashboard_skip_zero_length_next_track_is_not_reported_ended(
    monkeypatch,
):
    next_track = _Track("Next", length=0)
    player = _Player(current=_Track("Now"), queued=[_Track("Next")])

    async def skip():
        return next_track

    player.skip = skip
    cog = _dashboard_cog(player)
    guild_id = 99
    bot = _FakeBot(_FakeGuild(guild_id, player), cog)
    monkeypatch.setattr(dma, "_player_cls", lambda: sonolink.Player)
    dma._MUSIC_LOCKS.clear()
    settings._cache.clear()

    result = await dma._exec_music_skip(bot, guild_id, {})

    assert result == {"ok": True, "skipped": True, "ended": False}
    assert cog.cleared == []


async def test_dashboard_skip_real_end_still_reports_ended(monkeypatch):
    """Counter-test: a genuine queue-emptying skip still reports ended=True."""
    player = _Player(current=_Track("Now"), queued=[_Track("Filler")])

    async def skip():
        return None

    player.skip = skip
    cog = _dashboard_cog(player)
    guild_id = 99
    bot = _FakeBot(_FakeGuild(guild_id, player), cog)
    monkeypatch.setattr(dma, "_player_cls", lambda: sonolink.Player)
    dma._MUSIC_LOCKS.clear()
    settings._cache.clear()

    result = await dma._exec_music_skip(bot, guild_id, {})

    assert result == {"ok": True, "skipped": True, "ended": True}
    assert cog.cleared == [guild_id]
