"""Unit tests for the DJ/mod playback-control gate (issue G1).

Before this, every controller button and mirror command only required being in
the player's voice channel, so any listener could pause, change the volume, apply
effects or disconnect the bot. The gate locks the *destructive / disruptive*
controls to the session DJ or a Manage-Server member, while leaving the room-open
surfaces (Add, Queue view, Favorite, Skip's vote flow) untouched.

The whole matrix funnels through ONE decision - :func:`effects.can_control_playback`
- reused by the cog's :meth:`Music._can_control`, the ``control=True`` mirror
commands (via ``_require_player``) and the view gate (``_ensure_can_control``). So
these tests pin:

* the pure decision table for every role x (DJ set / no DJ);
* that ``Music._can_control`` threads ``player.dj`` and ``_has_manage_guild``
  into that one predicate (no duplicated rule);
* that the view gate ``_ensure_can_control`` permits / refuses correctly, sends
  the exact ephemeral refusal, and OPENS when the session has no DJ;
* that the gate reuses the effects predicate rather than copying it.

Everything here is sonolink-free (the predicate is pure and the cog/view helpers
are driven with fakes), so it runs on the stub-sonolink dev box and real-sonolink
CI alike.
"""

import types

from cogs.music import effects, music, views

# ---------------------------------------------------------------------------
# Pure decision table: effects.can_control_playback
# ---------------------------------------------------------------------------


def test_control_dj_is_allowed():
    assert effects.can_control_playback(dj_id=5, actor_id=5, has_manage_guild=False)


def test_control_manager_is_allowed_even_when_not_dj():
    assert effects.can_control_playback(dj_id=5, actor_id=9, has_manage_guild=True)


def test_control_dj_and_manager_is_allowed():
    assert effects.can_control_playback(dj_id=5, actor_id=5, has_manage_guild=True)


def test_control_plain_listener_is_refused_when_a_dj_exists():
    assert not effects.can_control_playback(dj_id=5, actor_id=9, has_manage_guild=False)


def test_control_no_dj_opens_to_a_plain_listener():
    # The radio / vote precedent: no DJ -> no gate (same-voice still enforced by
    # the caller), so a restored session whose DJ left stays controllable.
    assert effects.can_control_playback(dj_id=None, actor_id=9, has_manage_guild=False)


def test_control_no_dj_opens_to_a_manager_too():
    assert effects.can_control_playback(dj_id=None, actor_id=9, has_manage_guild=True)


def test_control_no_dj_opens_even_for_actor_id_zero():
    # actor_id defaults to 0 when the actor has no id; a None DJ must still open.
    assert effects.can_control_playback(dj_id=None, actor_id=0, has_manage_guild=False)


def test_control_matches_effects_exemption_whenever_a_dj_exists():
    # With a DJ set, the control gate is EXACTLY the effects "trusted to drive the
    # room" predicate - the two never diverge, so a single rule governs both.
    for dj_id, actor_id, mg in [
        (5, 5, False),
        (5, 9, False),
        (5, 9, True),
        (5, 5, True),
    ]:
        assert effects.can_control_playback(dj_id, actor_id, mg) is effects.is_effect_exempt(
            dj_id, actor_id, mg
        )


# ---------------------------------------------------------------------------
# Music._can_control - threads player.dj + _has_manage_guild into the predicate
# ---------------------------------------------------------------------------


class _Cog:
    """A minimal cog stand-in exposing just the gate's collaborators.

    ``_can_control`` goes through the REAL ``Music._is_music_manager``, which
    checks ``self._has_manage_guild(actor)`` first and only then reads the guild's
    configured DJ role through ``self._settings_pool()``. Stubbing the static
    check lets the manager path be exercised without a real ``discord.Member``
    (the real check is ``isinstance(actor, discord.Member) and ...``), and a
    ``None`` pool means "no DJ role configured" - i.e. an untouched guild, whose
    decision table must be exactly what it was before the role existed.
    """

    def __init__(self, manager):
        self._has_manage_guild = lambda actor: manager

    def _settings_pool(self):
        return None

    async def _is_music_manager(self, player, actor):
        return await music.Music._is_music_manager(self, player, actor)

    async def _privileged(self, predicate, player, actor):
        return await music.Music._privileged(self, predicate, player, actor)


def _cog(*, manager: bool):
    return _Cog(manager)


def _player(dj_id):
    dj = (
        None
        if dj_id is None
        else types.SimpleNamespace(id=dj_id, mention=f"<@{dj_id}>")
    )
    return types.SimpleNamespace(dj=dj)


def _actor(actor_id):
    return types.SimpleNamespace(id=actor_id)


async def test_can_control_allows_the_dj():
    assert await music.Music._can_control(_cog(manager=False), _player(5), _actor(5))


async def test_can_control_allows_a_manager_over_a_different_dj():
    assert await music.Music._can_control(_cog(manager=True), _player(5), _actor(9))


async def test_can_control_refuses_a_plain_listener():
    assert not await music.Music._can_control(
        _cog(manager=False), _player(5), _actor(9)
    )


async def test_can_control_opens_when_no_dj_is_set():
    assert await music.Music._can_control(_cog(manager=False), _player(None), _actor(9))


async def test_can_control_handles_a_missing_actor_id():
    # A None-DJ session opens regardless of the actor's id shape.
    assert await music.Music._can_control(_cog(manager=False), _player(None), object())


# ---------------------------------------------------------------------------
# views._ensure_can_control - the button/select gate
# ---------------------------------------------------------------------------


class _GateCog:
    """Wraps the real ``Music._can_control`` with a stubbed manage-guild check.

    ``_settings_pool`` returns None (no configured DJ role), so this drives the
    unconfigured-guild decision table - the one that must not have moved.
    """

    def __init__(self, manager):
        self._has_manage_guild = lambda actor: manager

    def _settings_pool(self):
        return None

    async def _is_music_manager(self, player, actor):
        return await music.Music._is_music_manager(self, player, actor)

    async def _privileged(self, predicate, player, actor):
        return await music.Music._privileged(self, predicate, player, actor)

    async def _can_control(self, player, actor):
        return await music.Music._can_control(self, player, actor)


async def test_ensure_can_control_permits_the_dj(make_interaction):
    interaction = make_interaction(user_id=5)
    ok = await views._ensure_can_control(_GateCog(False), _player(5), interaction)
    assert ok is True
    assert interaction.sent == []  # permitted -> no refusal, callback proceeds


async def test_ensure_can_control_permits_a_manager(make_interaction):
    interaction = make_interaction(user_id=9)
    ok = await views._ensure_can_control(_GateCog(True), _player(5), interaction)
    assert ok is True
    assert interaction.sent == []


async def test_ensure_can_control_refuses_a_listener_with_the_generic_message(
    make_interaction,
):
    interaction = make_interaction(user_id=9)
    ok = await views._ensure_can_control(_GateCog(False), _player(5), interaction)
    assert ok is False
    assert len(interaction.sent) == 1
    (args, kwargs) = interaction.sent[0]
    assert kwargs.get("ephemeral") is True
    assert args[0] == "Only the DJ (<@5>) or a moderator can control playback."


async def test_ensure_can_control_opens_when_no_dj_and_never_sends(make_interaction):
    interaction = make_interaction(user_id=9)
    ok = await views._ensure_can_control(_GateCog(False), _player(None), interaction)
    assert ok is True
    assert interaction.sent == []  # no DJ -> open, and .mention is never touched


# ---------------------------------------------------------------------------
# No duplicated rule (mirrors the voteskip reuse guard)
# ---------------------------------------------------------------------------


def test_views_gate_defines_no_own_control_rule():
    # The view layer must not grow its own copy of the DJ/manager decision; it
    # reuses the cog predicate, which reuses effects.can_control_playback.
    assert not hasattr(views, "can_control_playback")
    assert not hasattr(views, "is_effect_exempt")


# ---------------------------------------------------------------------------
# _start_genre - the vibe-card pick takes the station-zap gate on a live replace
# ---------------------------------------------------------------------------
#
# The vibe card is the /play entry for everyone, so picking a genre from SILENCE
# stays open. But when a session is already LIVE the pick REPLACES playback - the
# exact destructive station zap _change_station DJ-gates - so it must take the
# same gate, or a plain listener could wipe the DJ's session through the card.


class _GenreCog:
    """A minimal Music stand-in exposing just what ``_start_genre`` touches.

    ``_can_control`` is the REAL predicate (threading a stubbed manage-guild
    check) so the test pins the actual gate, not a re-statement of it.
    ``_apply_genre`` records whether the destructive replace ran.
    """

    def __init__(self, *, manager=False):
        self._has_manage_guild = lambda actor: manager
        self.apply_calls = []

    def _nodes_available(self):
        return True

    def _settings_pool(self):
        return None

    async def _is_music_manager(self, player, actor):
        return await music.Music._is_music_manager(self, player, actor)

    async def _privileged(self, predicate, player, actor):
        return await music.Music._privileged(self, predicate, player, actor)

    async def _can_control(self, player, actor):
        return await music.Music._can_control(self, player, actor)

    async def _apply_genre(self, player, genre, requester_id, *, replace):
        self.apply_calls.append((genre, requester_id, replace))
        return (None, [object()])


def _live_player(fake_player_cls, *, dj_id, current, queued=0):
    player = fake_player_cls()
    player.current = current
    # An empty two-lane queue by default: _start_genre reads it for the per-guild
    # queue cap before it seeds, and a start-from-silence pick must never be
    # refused by it. ``queued`` fills the user lane to drive the cap refusal.
    player.queue = types.SimpleNamespace(
        tracks=[object()] * queued, autoplay_tracks=[]
    )
    player.dj = (
        None if dj_id is None else types.SimpleNamespace(id=dj_id, mention=f"<@{dj_id}>")
    )
    player.home = object()  # non-None so _start_genre never rebinds home
    player.channel = object()
    return player


def _member_interaction(make_interaction, *, user_id, player, monkeypatch):
    """A make_interaction wired for ``_start_genre``: a Member author in voice, a
    guild whose voice_client is ``player``, both isinstance gates monkeypatched."""

    class _FakeMember:
        def __init__(self, uid):
            self.id = uid
            self.mention = f"<@{uid}>"
            self.voice = types.SimpleNamespace(channel=object())

    monkeypatch.setattr(music.discord, "Member", _FakeMember)
    monkeypatch.setattr(music.sonolink, "Player", type(player))

    interaction = make_interaction(user_id=user_id)
    interaction.user = _FakeMember(user_id)
    interaction.guild = types.SimpleNamespace(voice_client=player)
    interaction.channel = object()
    return interaction


async def test_start_genre_refuses_a_listener_replacing_a_live_session(
    make_interaction, monkeypatch
):
    class _FakePlayer:
        pass

    cog = _GenreCog(manager=False)
    player = _live_player(_FakePlayer, dj_id=5, current=object())  # DJ 5, playing
    interaction = _member_interaction(
        make_interaction, user_id=9, player=player, monkeypatch=monkeypatch
    )
    genre = types.SimpleNamespace(label="Lo-fi")

    await music.Music._start_genre(cog, interaction, genre)

    # The destructive replace never ran, and the listener got the station refusal.
    assert cog.apply_calls == []
    assert len(interaction.followups) == 1
    (args, kwargs) = interaction.followups[0]
    assert kwargs.get("ephemeral") is True
    assert args[0] == "Only the DJ (<@5>) can change the station."


async def test_start_genre_allows_the_dj_to_zap_a_live_session(
    make_interaction, monkeypatch
):
    class _FakePlayer:
        pass

    cog = _GenreCog(manager=False)
    player = _live_player(_FakePlayer, dj_id=9, current=object())  # DJ is 9
    interaction = _member_interaction(
        make_interaction, user_id=9, player=player, monkeypatch=monkeypatch
    )
    genre = types.SimpleNamespace(label="Lo-fi")

    await music.Music._start_genre(cog, interaction, genre)

    # The DJ's pick ran the replace.
    assert cog.apply_calls == [(genre, 9, True)]


async def test_start_genre_opens_from_silence_for_a_plain_listener(
    make_interaction, monkeypatch
):
    class _FakePlayer:
        pass

    cog = _GenreCog(manager=False)
    # Nothing playing (current is None): starting from silence is open to anyone,
    # even a non-DJ, even with a DJ still set from a prior track.
    player = _live_player(_FakePlayer, dj_id=5, current=None)
    interaction = _member_interaction(
        make_interaction, user_id=9, player=player, monkeypatch=monkeypatch
    )
    genre = types.SimpleNamespace(label="Lo-fi")

    await music.Music._start_genre(cog, interaction, genre)

    # No gate from silence: the fresh session started (replace=False).
    assert cog.apply_calls == [(genre, 9, False)]


async def test_start_genre_refuses_a_start_from_silence_on_a_full_queue(
    make_interaction, monkeypatch
):
    """The queue cap (lot P2) is the OTHER thing standing between the card and
    the seed, and it is the only refusal a start-from-silence pick can meet.

    A zap purges both lanes before it seeds, so only this path can find the queue
    already at the cap. It has to say so plainly: the seed seam refuses by
    returning nothing, so without this branch the member would be told
    "I couldn't find any Lo-fi tracks right now", which is a lie about a full
    queue. Pins the refusal AND that the destructive seed never ran.
    """

    class _FakePlayer:
        pass

    cog = _GenreCog(manager=False)
    # Nothing playing, but the user lane already sits at the cap.
    player = _live_player(
        _FakePlayer, dj_id=None, current=None, queued=music.MAX_QUEUE_TRACKS
    )
    interaction = _member_interaction(
        make_interaction, user_id=9, player=player, monkeypatch=monkeypatch
    )
    genre = types.SimpleNamespace(label="Lo-fi")

    await music.Music._start_genre(cog, interaction, genre)

    assert cog.apply_calls == []
    assert len(interaction.followups) == 1
    (args, kwargs) = interaction.followups[0]
    assert kwargs.get("ephemeral") is True
    # The cap refusal, not the "found nothing" line the empty seed would produce.
    assert args[0] == music.queue_full_message()
    assert str(music.MAX_QUEUE_TRACKS) in args[0]


async def test_start_genre_still_seeds_when_the_queue_has_one_slot_left(
    make_interaction, monkeypatch
):
    """Counter-test: the refusal is ``<= 0`` room, not "nearly full". One free
    slot still starts a station."""

    class _FakePlayer:
        pass

    cog = _GenreCog(manager=False)
    player = _live_player(
        _FakePlayer, dj_id=None, current=None, queued=music.MAX_QUEUE_TRACKS - 1
    )
    interaction = _member_interaction(
        make_interaction, user_id=9, player=player, monkeypatch=monkeypatch
    )
    genre = types.SimpleNamespace(label="Lo-fi")

    await music.Music._start_genre(cog, interaction, genre)

    assert cog.apply_calls == [(genre, 9, False)]
