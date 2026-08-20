"""Who may act ON a live music session, from outside its voice channel.

Two holes, both reachable by an ORDINARY MEMBER with no permissions and no need
to join anything, both closed here:

* HIJACKING THE OUTPUT CHANNEL. ``/nowplaying`` and a bare ``/play`` assigned
  ``player.home = ctx.channel`` unconditionally, so anyone who could type in any
  channel could move a live session's controller there - and with it every later
  announcement (track starts, failures, vote-skips) for the rest of the session.
  Dragging it into a channel the listeners never read is enough to blind them.
  The MOVE is now gated exactly like driving the room (same-voice + DJ/mod), and
  the two cases that are not a move (no home yet, invoked from the home channel)
  stay open to everyone so the everyday re-post is untouched.
* ENQUEUEING FROM OUTSIDE THE ROOM. ``/play <query>`` - and therefore every
  ``/music search`` pick, every ``/play`` picker pick and the vibe card's search
  modal, which all run the same ``_play_query`` body - mutated an existing
  session's queue with no same-voice check at all. A member who never joined
  could push tracks into a channel they are not in and cannot hear. STARTING a
  session stays open (that is how one begins, and the connect already requires
  the caller to be in a voice channel); adding to one that already exists does
  not.

Every test drives the real cog methods on fakes: no node, no gateway, no
database. The gate decision itself is the real ``Music._can_control``, so these
can never pass against a copy of the rule that has drifted from the one the
controller buttons use.
"""

import types

import sonolink

from cogs.music import music

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Channel:
    """A text channel that records what was sent to it."""

    def __init__(self, channel_id, mention=None):
        self.id = channel_id
        self.mention = mention or "<#{0}>".format(channel_id)
        self.sends = []

    async def send(self, content=None, **kwargs):
        self.sends.append((content, kwargs))


class _Track:
    def __init__(self, title="Now"):
        self.title = title
        self.author = "Artist"
        self.identifier = title
        self.uri = "https://example.test/{0}".format(title)
        self.length = 1000
        self.is_stream = False
        self.source_name = "youtube"
        self.encoded = "enc"
        self.extras = types.SimpleNamespace(requester=None, radio=False)


class _Queue:
    def __init__(self):
        self.tracks = []
        self.auto = []
        self.mode = None

    def put(self, item):
        if isinstance(item, list):
            self.tracks.extend(item)
        else:
            self.tracks.append(item)

    def get(self):
        return self.tracks.pop(0)

    def __len__(self):
        return len(self.tracks)


class _Player(sonolink.Player):
    """A real ``sonolink.Player`` subclass (the seams isinstance-check it)."""

    def __init__(self, *, voice_channel, home=None, current=None, dj=None):
        # Deliberately no super().__init__: a real Player wants a live node.
        self.channel = voice_channel
        self.home = home
        self.dj = dj
        self._current = current
        self._queue = _Queue()
        self.radio_genre = None
        # Enough state for a real MusicController to RENDER off this player -
        # the private-copy tests build one rather than a stand-in, so a change
        # that breaks the render cannot pass them.
        self._paused = False
        self._volume = 50
        self._position = 0
        self._autoplay_handler = types.SimpleNamespace(
            _settings=types.SimpleNamespace(
                mode=sonolink.AutoPlayMode.DISABLED
            )
        )

    @property
    def queue(self):
        return self._queue

    @property
    def current(self):
        return self._current

    async def play(self, track):
        self._current = track


class _SLClient:
    def __init__(self, answer=None):
        self.answer = answer if answer is not None else _Track("New")
        self.searches = []

    async def search_track(self, query, source=None):
        self.searches.append(query)
        return _Result(self.answer)


class _Result:
    def __init__(self, payload):
        self.result = payload

    def is_error(self):
        return False

    def is_empty(self):
        return False


def _cog(*, manager=False, sl_client=None):
    """A Music cog with no ``__init__`` side effects, real gate methods.

    ``_can_control`` / ``_may_rebind_home`` are the REAL implementations; only
    ``_has_manage_guild`` (which needs a live Member) and the settings pool (no
    configured DJ role) are stubbed, exactly as ``test_music_gate`` does.
    """
    cog = music.Music.__new__(music.Music)
    cog.bot = types.SimpleNamespace(sl_client=sl_client or _SLClient(), db_pool=None)
    cog._has_manage_guild = lambda actor: manager
    cog._settings_pool = lambda: None
    cog._nodes_available = lambda: True
    cog.controllers_sent = []

    async def send_controller(player, track=None, dedupe=False):
        cog.controllers_sent.append(player)

    async def snapshot(_player, track=None):
        pass

    async def init_session(_player, _author):
        pass

    cog._send_controller = send_controller
    cog._snapshot = snapshot
    cog._init_session = init_session
    return cog


def _member(member_id, voice_channel=None):
    return types.SimpleNamespace(
        id=member_id,
        mention="<@{0}>".format(member_id),
        voice=(
            None
            if voice_channel is None
            else types.SimpleNamespace(channel=voice_channel)
        ),
    )


def _ctx(*, author, channel, player=None, interaction=None):
    ctx = types.SimpleNamespace(
        author=author,
        channel=channel,
        voice_client=player,
        guild=types.SimpleNamespace(id=99),
        interaction=interaction,
        sends=[],
    )

    async def defer(*_a, **_kw):
        return None

    async def send(*args, **kwargs):
        ctx.sends.append((args, kwargs))

    ctx.defer = defer
    ctx.send = send
    return ctx


def _last(ctx):
    return ctx.sends[-1][0][0] if ctx.sends else None


# ---------------------------------------------------------------------------
# is_in_player_voice - the one shared "are you in the room?" predicate
# ---------------------------------------------------------------------------


def test_in_player_voice_true_for_a_listener_in_the_channel():
    room = object()
    player = types.SimpleNamespace(channel=room)
    assert music.is_in_player_voice(player, _member(1, room))


def test_in_player_voice_false_from_another_channel():
    player = types.SimpleNamespace(channel=object())
    assert not music.is_in_player_voice(player, _member(1, object()))


def test_in_player_voice_false_for_a_member_in_no_voice_channel():
    player = types.SimpleNamespace(channel=object())
    assert not music.is_in_player_voice(player, _member(1, None))


def test_in_player_voice_false_when_the_player_has_no_channel():
    assert not music.is_in_player_voice(
        types.SimpleNamespace(channel=None), _member(1, object())
    )


def test_in_player_voice_false_for_an_actor_with_no_voice_state_at_all():
    # A discord.User (DM actor, stale object): no ``voice`` attribute -> refused,
    # the same verdict the isinstance(Member) check used to give.
    player = types.SimpleNamespace(channel=object())
    assert not music.is_in_player_voice(player, object())


def test_require_player_still_reuses_the_shared_predicate():
    # Guard against the same-voice rule being re-inlined into _require_player and
    # drifting from the copy _may_rebind_home uses.
    import inspect

    source = inspect.getsource(music.Music._require_player)
    assert "is_in_player_voice" in source


# ---------------------------------------------------------------------------
# _may_rebind_home - same-voice AND the DJ/mod gate, silently
# ---------------------------------------------------------------------------


async def test_may_rebind_requires_being_in_the_voice_channel():
    room = object()
    player = _Player(voice_channel=room, dj=_member(5, room))
    # The DJ themself, but standing outside the room: no move.
    assert not await _cog()._may_rebind_home(player, _member(5, None))


async def test_may_rebind_requires_passing_the_dj_gate():
    room = object()
    player = _Player(voice_channel=room, dj=_member(5, room))
    # In the room, but a plain listener while a DJ is set: no move.
    assert not await _cog()._may_rebind_home(player, _member(9, room))


async def test_may_rebind_allows_the_dj_in_the_room():
    room = object()
    dj = _member(5, room)
    player = _Player(voice_channel=room, dj=dj)
    assert await _cog()._may_rebind_home(player, dj)


async def test_may_rebind_allows_a_manager_in_the_room():
    room = object()
    player = _Player(voice_channel=room, dj=_member(5, room))
    assert await _cog(manager=True)._may_rebind_home(player, _member(9, room))


async def test_may_rebind_opens_for_the_room_when_no_dj_is_set():
    room = object()
    player = _Player(voice_channel=room, dj=None)
    assert await _cog()._may_rebind_home(player, _member(9, room))


# ---------------------------------------------------------------------------
# Finding 1: /nowplaying and bare /play cannot MOVE a live session
# ---------------------------------------------------------------------------


async def test_a_bystander_cannot_move_the_session_to_their_channel():
    """THE HIJACK. Not in voice, not the DJ, typing in some other channel."""
    room = object()
    home = _Channel(1, mention="#music")
    elsewhere = _Channel(2)
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    cog = _cog()
    ctx = _ctx(author=_member(9, None), channel=elsewhere, player=player)

    await cog._repost_controller(ctx, player)

    assert player.home is home  # the live session did NOT budge
    assert cog.controllers_sent == []  # and no controller was re-posted for them


async def test_the_refused_bystander_is_still_answered_where_they_asked():
    """Refusing the MOVE must not refuse the person - they get a pointer."""
    room = object()
    home = _Channel(1, mention="#music")
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    ctx = _ctx(author=_member(9, None), channel=_Channel(2), player=player)

    await _cog()._repost_controller(ctx, player)

    assert _last(ctx) == "The player is already active in #music."
    assert ctx.sends[-1][1].get("ephemeral") is True


async def test_a_listener_in_the_room_who_is_not_the_dj_still_cannot_move_it():
    room = object()
    home = _Channel(1)
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    ctx = _ctx(author=_member(9, room), channel=_Channel(2), player=player)

    await _cog()._repost_controller(ctx, player)

    assert player.home is home


# --- the MOVE is gated; the READ is not ------------------------------------
# Gating both cost a legitimate listener - somebody sitting in the voice channel
# who simply is not the DJ - the controller they had every right to look at,
# from anywhere but the home channel. /nowplaying is a read; a bare /play is a
# request to take the room over.


async def test_a_refused_reader_still_gets_a_private_controller():
    room = types.SimpleNamespace(name="General")
    home = _Channel(1, mention="#music")
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    ctx = _ctx(
        author=_member(9, room),
        channel=_Channel(2),
        player=player,
        interaction=object(),
    )

    await _cog()._repost_controller(ctx, player, may_read_elsewhere=True)

    assert player.home is home  # nothing moved
    views_sent = [kw.get("view") for _a, kw in ctx.sends if kw.get("view")]
    assert len(views_sent) == 1
    assert all(kw.get("ephemeral") is True for _a, kw in ctx.sends)


async def test_the_private_copy_is_never_the_sessions_controller():
    """It must not be registered anywhere, or a track change would refresh THIS
    message and the live one in the home channel would go stale instead."""
    room = types.SimpleNamespace(name="General")
    home = _Channel(1, mention="#music")
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    cog = _cog()
    ctx = _ctx(
        author=_member(9, room),
        channel=_Channel(2),
        player=player,
        interaction=object(),
    )

    await cog._repost_controller(ctx, player, may_read_elsewhere=True)

    assert cog.controllers_sent == []  # the public controller was not re-posted
    assert getattr(player, "controller", None) is None

    # ... and it is a READ: every control is disabled. The copy never
    # re-renders (nothing points at it), so a live button here would drive the
    # session off a panel showing a track that stopped playing minutes ago.
    import discord

    private = [kw["view"] for _a, kw in ctx.sends if kw.get("view")][0]
    controls = [
        child
        for child in private.walk_children()
        if isinstance(child, (discord.ui.Button, discord.ui.Select))
    ]
    assert controls
    assert all(child.disabled for child in controls)


async def test_a_bare_play_gets_no_private_copy():
    """/play is not a read: refusing it must stay a refusal."""
    room = object()
    home = _Channel(1, mention="#music")
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    ctx = _ctx(
        author=_member(9, room),
        channel=_Channel(2),
        player=player,
        interaction=object(),
    )

    await _cog()._repost_controller(ctx, player)

    assert [kw.get("view") for _a, kw in ctx.sends if kw.get("view")] == []


async def test_a_prefix_read_gets_the_pointer_only():
    """``ephemeral`` means nothing without an interaction, so a private copy
    would in fact be a second PUBLIC controller in the refused channel."""
    room = object()
    home = _Channel(1, mention="#music")
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    ctx = _ctx(author=_member(9, room), channel=_Channel(2), player=player)

    await _cog()._repost_controller(ctx, player, may_read_elsewhere=True)

    assert [kw.get("view") for _a, kw in ctx.sends if kw.get("view")] == []
    assert _last(ctx) == "The player is already active in #music."


def test_nowplaying_asks_for_the_read_and_bare_play_does_not():
    import inspect

    np_source = inspect.getsource(music.Music.nowplaying.callback)
    play_source = inspect.getsource(music.Music._play_no_query)

    assert "may_read_elsewhere=True" in np_source
    assert "may_read_elsewhere" not in play_source


async def test_the_dj_in_the_room_may_move_the_session():
    room = object()
    home = _Channel(1)
    target = _Channel(2)
    dj = _member(5, room)
    player = _Player(voice_channel=room, home=home, current=_Track(), dj=dj)
    cog = _cog()
    ctx = _ctx(author=dj, channel=target, player=player)

    await cog._repost_controller(ctx, player)

    assert player.home is target
    assert cog.controllers_sent == [player]


async def test_a_manager_may_move_the_session():
    room = object()
    target = _Channel(2)
    player = _Player(
        voice_channel=room, home=_Channel(1), current=_Track(), dj=_member(5, room)
    )
    cog = _cog(manager=True)
    ctx = _ctx(author=_member(9, room), channel=target, player=player)

    await cog._repost_controller(ctx, player)

    assert player.home is target


async def test_reposting_in_the_home_channel_stays_open_to_everyone():
    """The everyday case: no MOVE happens, so no gate applies."""
    room = object()
    home = _Channel(1)
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    cog = _cog()
    ctx = _ctx(author=_member(9, None), channel=home, player=player)

    await cog._repost_controller(ctx, player)

    assert player.home is home
    assert cog.controllers_sent == [player]


async def test_a_session_with_no_home_yet_binds_to_whoever_asks():
    """Nothing to steal: an unbound session is not a hijack target."""
    room = object()
    target = _Channel(2)
    player = _Player(
        voice_channel=room, home=None, current=_Track(), dj=_member(5, room)
    )
    cog = _cog()
    ctx = _ctx(author=_member(9, None), channel=target, player=player)

    await cog._repost_controller(ctx, player)

    assert player.home is target


async def test_nowplaying_and_bare_play_share_the_gated_repost_seam():
    # Both commands must route through _repost_controller; a future edit that
    # re-inlines "player.home = ctx.channel" into either one re-opens the hole.
    import inspect

    for func in (music.Music.nowplaying.callback, music.Music._play_no_query):
        source = inspect.getsource(func)
        assert "_repost_controller" in source
        assert "player.home = ctx.channel" not in source


async def test_bare_play_on_a_live_session_does_not_move_it_for_a_bystander():
    """End to end through the real /play no-query body."""
    room = object()
    home = _Channel(1, mention="#music")
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    cog = _cog()
    ctx = _ctx(author=_member(9, None), channel=_Channel(2), player=player)

    await cog._play_no_query(ctx)

    assert player.home is home
    assert _last(ctx) == "The player is already active in #music."


# ---------------------------------------------------------------------------
# Finding 2: enqueueing into an EXISTING session needs the same-voice gate
# ---------------------------------------------------------------------------


async def test_an_outsider_cannot_push_a_track_into_a_live_session():
    """THE HOLE. Same text channel, not in the voice channel, queue mutated."""
    room = object()
    home = _Channel(1)
    player = _Player(voice_channel=room, home=home, current=_Track())
    client = _SLClient()
    cog = _cog(sl_client=client)
    ctx = _ctx(author=_member(9, None), channel=home, player=player)

    await cog._play_query(ctx, "air horn")

    assert player.queue.tracks == []  # nothing landed in a room they are not in
    assert client.searches == []  # and the node was never even asked
    assert _last(ctx) == "You must be in my voice channel to do that."


async def test_an_outsider_in_another_voice_channel_is_refused_too():
    room = object()
    home = _Channel(1)
    player = _Player(voice_channel=room, home=home, current=_Track())
    cog = _cog()
    ctx = _ctx(author=_member(9, object()), channel=home, player=player)

    await cog._play_query(ctx, "air horn")

    assert player.queue.tracks == []


async def test_a_listener_in_the_room_may_still_queue():
    """The counter-test: adding a song stays open to the people listening."""
    room = object()
    home = _Channel(1)
    player = _Player(voice_channel=room, home=home, current=_Track())
    cog = _cog()
    ctx = _ctx(author=_member(9, room), channel=home, player=player)

    await cog._play_query(ctx, "a real song")

    assert [t.title for t in player.queue.tracks] == ["New"]


async def test_a_listener_in_the_room_needs_no_dj_role_to_queue():
    """Adding is a ROOM action, not a DJ one - the tier must not have crept up."""
    room = object()
    home = _Channel(1)
    player = _Player(
        voice_channel=room, home=home, current=_Track(), dj=_member(5, room)
    )
    cog = _cog()
    ctx = _ctx(author=_member(9, room), channel=home, player=player)

    await cog._play_query(ctx, "a real song")

    assert len(player.queue.tracks) == 1


async def test_a_playlist_load_cannot_reach_a_session_from_outside_either():
    """The sibling seam: ``/serverplaylist play`` and the favourites card.

    Worse than a single ``/play`` if left open - a server playlist is up to 200
    tracks pushed into a room the caller is not in.
    """
    from cogs.music import playlists_shared as ps

    room = object()
    player = _Player(voice_channel=room, home=_Channel(1), current=_Track())
    mixin = ps.ServerPlaylistMixin.__new__(ps.ServerPlaylistMixin)
    ctx = _ctx(author=_member(9, None), channel=_Channel(1), player=player)

    assert await mixin._connect_for_playlist(ctx) is None
    assert _last(ctx) == "You must be in my voice channel to do that."


async def test_a_playlist_load_from_inside_the_room_still_works():
    from cogs.music import playlists_shared as ps

    room = object()
    home = _Channel(1)
    player = _Player(voice_channel=room, home=home, current=_Track())
    mixin = ps.ServerPlaylistMixin.__new__(ps.ServerPlaylistMixin)
    ctx = _ctx(author=_member(9, room), channel=home, player=player)

    assert await mixin._connect_for_playlist(ctx) is player


async def test_starting_a_fresh_session_stays_open():
    """No player yet: the connect branch is how a session begins, ungated."""
    room = _Channel(3)
    home = _Channel(1)
    connected = {}

    async def connect(cls=None):
        connected["player"] = _Player(voice_channel=room)
        return connected["player"]

    author = types.SimpleNamespace(
        id=9,
        mention="<@9>",
        voice=types.SimpleNamespace(channel=types.SimpleNamespace(connect=connect)),
    )
    cog = _cog()
    ctx = _ctx(author=author, channel=home, player=None)

    await cog._play_query(ctx, "a real song")

    assert [t.title for t in connected["player"].queue.tracks] == []
    assert connected["player"].current.title == "New"
    assert connected["player"].home is home
