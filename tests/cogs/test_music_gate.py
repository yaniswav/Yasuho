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


def _cog(*, manager: bool):
    """A minimal cog stand-in exposing just the gate's collaborators.

    ``_can_control`` calls ``self._has_manage_guild(actor)``; stubbing it lets the
    manager path be exercised without a real ``discord.Member`` (the real static
    check is ``isinstance(actor, discord.Member) and ...``).
    """
    return types.SimpleNamespace(_has_manage_guild=lambda actor: manager)


def _player(dj_id):
    dj = (
        None
        if dj_id is None
        else types.SimpleNamespace(id=dj_id, mention=f"<@{dj_id}>")
    )
    return types.SimpleNamespace(dj=dj)


def _actor(actor_id):
    return types.SimpleNamespace(id=actor_id)


def test_can_control_allows_the_dj():
    assert music.Music._can_control(_cog(manager=False), _player(5), _actor(5))


def test_can_control_allows_a_manager_over_a_different_dj():
    assert music.Music._can_control(_cog(manager=True), _player(5), _actor(9))


def test_can_control_refuses_a_plain_listener():
    assert not music.Music._can_control(_cog(manager=False), _player(5), _actor(9))


def test_can_control_opens_when_no_dj_is_set():
    assert music.Music._can_control(_cog(manager=False), _player(None), _actor(9))


def test_can_control_handles_a_missing_actor_id():
    # A None-DJ session opens regardless of the actor's id shape.
    assert music.Music._can_control(_cog(manager=False), _player(None), object())


# ---------------------------------------------------------------------------
# views._ensure_can_control - the button/select gate
# ---------------------------------------------------------------------------


class _GateCog:
    """Wraps the real ``Music._can_control`` with a stubbed manage-guild check."""

    def __init__(self, manager):
        self._has_manage_guild = lambda actor: manager

    def _can_control(self, player, actor):
        return music.Music._can_control(self, player, actor)


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

    def _can_control(self, player, actor):
        return music.Music._can_control(self, player, actor)

    async def _apply_genre(self, player, genre, requester_id, *, replace):
        self.apply_calls.append((genre, requester_id, replace))
        return (None, [object()])


def _live_player(fake_player_cls, *, dj_id, current):
    player = fake_player_cls()
    player.current = current
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
