"""The one "which guild is this player in?" answer, and the raise it exists for.

sonolink makes ``Player.guild`` a PROPERTY that raises ``RuntimeError`` - not
``AttributeError`` - while the player has no guild attached, so
``getattr(player, "guild", None)`` does not protect anybody from it. The package
had grown five hand-written helpers for that one question and they disagreed
about the raise; ``cogs.music.playerinfo.guild_id_of`` is now the only one.

WHY THIS FILE EXISTS AT ALL. The failure this helper prevents is SILENT: the
RuntimeError comes out inside a websocket event handler, a voice listener or a
per-guild lock, so the room simply never gets its panel / its vote / its skip and
nothing says why. A helper whose success is "nothing happened" needs a
calibrated witness, so:

* :func:`test_the_raising_player_stand_in_really_raises_like_sonolink` asserts
  the stand-in raises AND that ``getattr`` does not rescue it. Every "did not
  raise" assertion below is only evidence because of that one.
* the derivation tests each have their negative twin: a shape that must answer
  ``None``, so "returned an id" can never be the answer to "returned anything".
* two REAL production entry points (a SponsorBlock websocket event and the skip
  vote registry) are driven with that player, so the consolidation is proven
  where the exposure actually was, not only on the helper.

No loop-free fakery beyond ``types.SimpleNamespace``: no node, no Discord, no
database.
"""

import logging
import types

import pytest

from cogs.music import playerinfo, sponsorblock, voteskip


class _RaisingGuildPlayer:
    """A player sonolink has not attached to a guild yet.

    Mirrors the installed ``sonolink/gateway/player/_base.py``: ``guild`` is a
    property, and it raises ``RuntimeError`` while the player's ``_guild`` is
    still None. Everything else about it is ordinary.
    """

    def __init__(self, channel=None, home=None):
        self.channel = channel
        self.home = home

    @property
    def guild(self):
        raise RuntimeError("Player is not yet attached to a guild.")


def _guild(guild_id):
    return types.SimpleNamespace(id=guild_id)


def _channel(guild_id):
    return types.SimpleNamespace(id=99, guild=_guild(guild_id))


# ---------------------------------------------------------------------------
# The witness: the stand-in is calibrated, so "it did not raise" means something
# ---------------------------------------------------------------------------


def test_the_raising_player_stand_in_really_raises_like_sonolink():
    """If this ever stops raising, every other test in this file stops proving."""
    player = _RaisingGuildPlayer()

    with pytest.raises(RuntimeError):
        player.guild
    # The whole point: getattr's default does NOT catch a RuntimeError, so the
    # "defensive" shape four of the five old helpers used protected nobody.
    with pytest.raises(RuntimeError):
        getattr(player, "guild", None)


# ---------------------------------------------------------------------------
# The derivation, each step with its negative twin
# ---------------------------------------------------------------------------


def test_the_voice_channel_answers_even_when_the_property_raises():
    player = _RaisingGuildPlayer(channel=_channel(1234))

    assert playerinfo.guild_id_of(player) == 1234


def test_the_home_channel_answers_for_a_player_with_no_voice_channel():
    """A player mid-move or already disconnected: ``channel`` is gone, home is not."""
    player = _RaisingGuildPlayer(channel=None, home=_channel(5678))

    assert playerinfo.guild_id_of(player) == 5678


def test_a_player_that_only_has_the_property_is_still_answered():
    """The legacy shape the four replaced helpers read, and nothing else.

    The consolidation must not have narrowed the answer: a player whose guild is
    reachable ONLY through that attribute (an attached player with no channels in
    hand) still resolves, so no call site lost bookkeeping it used to get.
    """
    player = types.SimpleNamespace(channel=None, home=None, guild=_guild(4321))

    assert playerinfo.guild_id_of(player) == 4321


def test_a_player_with_nothing_answers_none_instead_of_raising():
    """The negative control, and the contract every call site is written against."""
    assert playerinfo.guild_id_of(_RaisingGuildPlayer()) is None
    assert playerinfo.guild_id_of(types.SimpleNamespace()) is None
    assert playerinfo.guild_id_of(types.SimpleNamespace(guild=None)) is None


def test_a_guild_object_with_no_id_is_not_mistaken_for_an_id():
    """``None`` means "no guild-keyed work", so a half-built guild must say None."""
    player = types.SimpleNamespace(channel=types.SimpleNamespace(guild=object()))

    assert playerinfo.guild_id_of(player) is None


# ---------------------------------------------------------------------------
# ...and the two production paths that used to read the property unprotected
# ---------------------------------------------------------------------------


def test_a_sponsorblock_websocket_event_logs_instead_of_blowing_up(caplog):
    """``log_ws_event`` is bound to a gateway event: a raise there is unowned.

    This is instrumentation only, which is exactly why it read the property
    unprotected and why nobody would have noticed it taking out the event
    handler. It must now log the guild it derived from the channel.
    """
    player = _RaisingGuildPlayer(channel=_channel(8787))

    with caplog.at_level(logging.DEBUG, logger=sponsorblock.log.name):
        sponsorblock.log_ws_event(player, {"type": "SegmentsLoaded"})

    assert any("8787" in record.getMessage() for record in caplog.records), caplog.text


async def test_opening_a_skip_vote_degrades_instead_of_blowing_up():
    """The skip path: ``SkipVotes.open`` used to raise on this player.

    A room with no postable channel degrades to an instant skip - a decision the
    registry is written to make - and it can only make it if reaching the guild
    id did not take the coroutine down first.
    """
    player = _RaisingGuildPlayer(channel=_channel(9090))
    registry = voteskip.SkipVotes()

    outcome = await registry.open(
        types.SimpleNamespace(), player, types.SimpleNamespace(id=1), None
    )

    assert outcome == voteskip.SKIP_INSTANT
