"""Unit tests for following a server-initiated voice move (issue G2).

Symptom: a mod drags the bot to another voice channel and it snaps back to the
original room (and refuses controls). Root cause: sonolink's ``DpyPlayer`` moves
the *audio* on a drag but never updates the inherited ``discord.VoiceProtocol``
``channel`` attribute, so ``player.channel`` stays pinned to the OLD room - which
the same-voice gates, the DJ handoff, the empty-channel auto-leave, the idle
check, the snapshot AND the websocket-close reconnect all read (the reconnect
then ``connect()``s back to the stale channel, literally dragging the bot back).

The fix has two pure, testable seams plus the wiring they drive:

* :func:`music.is_bot_channel_move` - decides when a voice-state change is OUR bot
  moving between two real channels (excludes a fresh connect and a disconnect);
* :func:`music.resolve_voice_channel` - resolves a payload ``channel_id`` to a
  channel via the guild (the ``Player.on_voice_state_update`` override's core);
* the cog listener's bot-move branch - points ``player.channel`` at the new room,
  re-snapshots (new ``voice_channel_id``) and refreshes the controller.

All sonolink-free except the one listener-wiring test, which stubs
``sonolink.Player`` so the ``isinstance`` gate accepts a fake player.
"""

import types

from cogs.music import music

# ---------------------------------------------------------------------------
# is_bot_channel_move (pure decision table)
# ---------------------------------------------------------------------------


def test_move_between_two_channels_is_a_move():
    assert music.is_bot_channel_move(True, 10, 20)


def test_same_channel_is_not_a_move():
    # A mute / deafen / self-video toggle keeps the channel; not a move.
    assert not music.is_bot_channel_move(True, 10, 10)


def test_fresh_connect_is_not_a_move():
    # None -> B is our own connect (sonolink's connect owns it), not a drag.
    assert not music.is_bot_channel_move(True, None, 20)


def test_disconnect_is_not_a_move():
    # B -> None is a leave / kick (disconnect + cleanup own it), not a drag.
    assert not music.is_bot_channel_move(True, 10, None)


def test_full_disconnect_none_to_none_is_not_a_move():
    assert not music.is_bot_channel_move(True, None, None)


def test_not_our_bot_is_never_a_move():
    # A human (or another bot) moving between channels must not trip the branch.
    assert not music.is_bot_channel_move(False, 10, 20)


# ---------------------------------------------------------------------------
# resolve_voice_channel (the override's channel-sync core)
# ---------------------------------------------------------------------------


class _Guild:
    def __init__(self, channels):
        self._channels = channels
        self.lookups = []

    def get_channel(self, channel_id):
        self.lookups.append(channel_id)
        return self._channels.get(channel_id)


def test_resolve_returns_the_channel_for_a_known_id():
    channel = object()
    guild = _Guild({20: channel})
    assert music.resolve_voice_channel(guild, "20") is channel
    # The payload id is a string; it is coerced to int for get_channel.
    assert guild.lookups == [20]


def test_resolve_none_channel_id_is_a_disconnect():
    guild = _Guild({20: object()})
    assert music.resolve_voice_channel(guild, None) is None
    assert guild.lookups == []  # never queried on a disconnect


def test_resolve_falsy_channel_id_is_none():
    guild = _Guild({})
    assert music.resolve_voice_channel(guild, "") is None
    assert music.resolve_voice_channel(guild, 0) is None


def test_resolve_missing_guild_is_none():
    assert music.resolve_voice_channel(None, "20") is None


def test_resolve_unknown_channel_is_none():
    guild = _Guild({})  # get_channel returns None for an unknown id
    assert music.resolve_voice_channel(guild, "999") is None


# ---------------------------------------------------------------------------
# Cog listener wiring: the bot-move branch follows the move
# ---------------------------------------------------------------------------


_BOT_ID = 4242


def _channel(channel_id):
    return types.SimpleNamespace(id=channel_id, name=f"room-{channel_id}")


def _listener_self(snapshot_calls):
    async def _fire_voice_watch(member):
        pass

    async def _snapshot(player):
        snapshot_calls.append(player)

    return types.SimpleNamespace(
        bot=types.SimpleNamespace(user=types.SimpleNamespace(id=_BOT_ID)),
        _fire_voice_watch=_fire_voice_watch,
        _snapshot=_snapshot,
    )


async def test_listener_follows_the_bot_move(monkeypatch):
    class _FakePlayer:
        def __init__(self):
            self.channel = _channel(10)
            self.controller = None

    # The listener gates on isinstance(player, sonolink.Player); stub the proxy so
    # a lightweight fake counts as a player for the duration of this test.
    monkeypatch.setattr(music.sonolink, "Player", _FakePlayer)

    player = _FakePlayer()
    new_channel = _channel(20)
    member = types.SimpleNamespace(
        id=_BOT_ID,
        bot=True,
        guild=types.SimpleNamespace(voice_client=player),
    )
    before = types.SimpleNamespace(channel=_channel(10))
    after = types.SimpleNamespace(channel=new_channel)

    snapshot_calls = []
    await music.Music.on_voice_state_update(
        _listener_self(snapshot_calls), member, before, after
    )

    # player.channel now reflects the NEW room, and the move was persisted.
    assert player.channel is new_channel
    assert snapshot_calls == [player]


async def test_listener_ignores_a_bot_mute_toggle(monkeypatch):
    # Same channel on both sides (a mute/deafen change) is not a move: no snapshot,
    # channel untouched.
    class _FakePlayer:
        def __init__(self):
            self.channel = _channel(10)
            self.controller = None

    monkeypatch.setattr(music.sonolink, "Player", _FakePlayer)
    player = _FakePlayer()
    original = player.channel
    member = types.SimpleNamespace(
        id=_BOT_ID,
        bot=True,
        guild=types.SimpleNamespace(voice_client=player),
    )
    before = types.SimpleNamespace(channel=_channel(10))
    after = types.SimpleNamespace(channel=_channel(10))

    snapshot_calls = []
    await music.Music.on_voice_state_update(
        _listener_self(snapshot_calls), member, before, after
    )

    assert player.channel is original
    assert snapshot_calls == []


async def test_listener_ignores_a_human_move(monkeypatch):
    # A human moving between channels must not trip the bot-move snapshot branch.
    # (The human path continues into the DJ-handoff / empty-channel logic, which
    # this test does not drive; it only asserts the bot branch stayed silent.)
    class _FakePlayer:
        def __init__(self):
            self.channel = _channel(10)
            self.controller = None
            self.dj = None  # a None DJ short-circuits the handoff cleanly

    monkeypatch.setattr(music.sonolink, "Player", _FakePlayer)
    player = _FakePlayer()
    # A human in the same channel as the bot so the empty-channel branch returns
    # early (there is still a human present) without an asyncio.sleep.
    human = types.SimpleNamespace(id=1, bot=False)
    channel = _channel(10)
    channel.members = [human]
    player.channel = channel
    member = types.SimpleNamespace(
        id=1,
        bot=False,
        guild=types.SimpleNamespace(voice_client=player),
    )
    before = types.SimpleNamespace(channel=_channel(30))
    after = types.SimpleNamespace(channel=_channel(40))

    snapshot_calls = []
    await music.Music.on_voice_state_update(
        _listener_self(snapshot_calls), member, before, after
    )

    # The bot-move branch never fired: no snapshot from it, and player.channel is
    # the room the (still-present) human keeps the bot in.
    assert snapshot_calls == []
    assert player.channel is channel
