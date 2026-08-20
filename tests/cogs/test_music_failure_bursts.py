"""One message per BURST of failed tracks, not one per track.

Production, 2026-08-20: YouTube broke, a queued session of tracks all failed on
the same cause within a couple of seconds, and ``on_sonolink_track_exception``
posted one message per track into one channel. Discord answered with 429s - paid
by the bot's shared HTTP client, so every other command in every other guild slowed
down because the music cog was announcing dead tracks.

What is pinned here:

* the accounting - the FIRST failure of a burst opens it (and is announced with
  its title, un-delayed: a lone dead track must keep the message it always had),
  every further failure inside the window is tallied and sends NOTHING;
* the listener end to end: N failures in a burst produce exactly 2 messages, and
  the second one names the count with the right plural;
* the counter-tests - a single failure still produces exactly one message and no
  summary, and two different guilds never share a burst;
* the hygiene: bursts self-delete (the map is empty at rest, not per-guild
  forever), the title in the announcement is defused, mentions are suppressed,
  and every failure is still LOGGED one line per track - the coalescing is
  cosmetic to the diagnostic record, not to it.

Driven with a zero-length window, so nothing here sleeps.
"""

import logging
import types

import pytest

from cogs.music import failures, music

# ---------------------------------------------------------------------------
# The pure accounting
# ---------------------------------------------------------------------------


def test_the_first_failure_opens_the_burst():
    bursts = failures.TrackFailureBursts()
    assert bursts.record(1) is True


def test_every_further_failure_is_silent():
    bursts = failures.TrackFailureBursts()
    bursts.record(1)
    assert [bursts.record(1) for _ in range(5)] == [False] * 5


def test_closing_returns_what_the_burst_swallowed():
    bursts = failures.TrackFailureBursts()
    bursts.record(1)
    for _ in range(39):
        bursts.record(1)
    assert bursts.close(1) == 39


def test_a_lone_failure_swallows_nothing():
    """So the summary is skipped entirely - the title message already said it."""
    bursts = failures.TrackFailureBursts()
    bursts.record(1)
    assert bursts.close(1) == 0


def test_a_closed_burst_opens_again():
    bursts = failures.TrackFailureBursts()
    bursts.record(1)
    bursts.close(1)
    assert bursts.record(1) is True


def test_guilds_never_share_a_burst():
    bursts = failures.TrackFailureBursts()
    assert bursts.record(1) is True
    assert bursts.record(2) is True  # a second guild is not silenced by the first
    bursts.record(1)
    assert bursts.close(1) == 1
    assert bursts.close(2) == 0


def test_the_map_is_empty_at_rest():
    """Bounded by guilds failing RIGHT NOW, not by guilds that ever failed."""
    bursts = failures.TrackFailureBursts()
    for key in range(50):
        bursts.record(key)
        bursts.close(key)
    assert bursts._open == {}
    assert bursts._tasks == {}
    assert bursts._live == set()


async def test_a_failure_landing_while_the_summary_sends_is_not_swallowed():
    """close() disarms BEFORE the send, so the next burst gets its own summary.

    The window is real: the summary takes its count, then awaits a Discord send.
    A failure arriving in there must open a fresh burst that can still be armed,
    not vanish into a key that stays "already armed" forever.
    """
    bursts = failures.TrackFailureBursts(window=0)
    bursts.record(1)
    bursts.record(1)

    async def summary():
        assert bursts.close(1) == 1
        # Standing where the send would be: the key must be re-armable already.
        assert bursts.record(1) is True
        assert bursts.arm(1, _noop()) is not None

    async def _noop():
        return None

    await bursts.arm(1, summary())
    bursts.shutdown()


def test_closing_a_key_that_was_never_opened_is_zero():
    assert failures.TrackFailureBursts().close("nope") == 0


async def test_arming_twice_leaves_only_one_summary():
    bursts = failures.TrackFailureBursts(window=0)
    calls = []

    async def summary(tag):
        calls.append(tag)

    first = bursts.arm(1, summary("a"))
    second = bursts.arm(1, summary("b"))
    assert second is None  # the duplicate was refused, not queued
    await first
    assert calls == ["a"]


async def test_shutdown_cancels_an_armed_summary():
    import asyncio

    bursts = failures.TrackFailureBursts(window=60)
    fired = []

    async def summary():
        await asyncio.sleep(60)
        fired.append(True)

    task = bursts.arm(1, summary())
    bursts.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fired == []
    assert bursts._open == {}


def test_the_window_is_a_few_seconds():
    """Long enough to catch a draining queue, short enough to still read as now."""
    assert 2.0 <= failures.BURST_WINDOW <= 10.0


# ---------------------------------------------------------------------------
# The listener, end to end
# ---------------------------------------------------------------------------


class _Home:
    def __init__(self):
        self.sends = []

    async def send(self, content=None, **kwargs):
        self.sends.append((content, kwargs))


class _Track:
    def __init__(self, title="Dead Track"):
        self.title = title


def _event(title="Dead Track", message="boom"):
    return types.SimpleNamespace(
        track=_Track(title), exception=types.SimpleNamespace(message=message)
    )


def _player(home, guild_id=99):
    return types.SimpleNamespace(
        home=home, guild=types.SimpleNamespace(id=guild_id)
    )


def _cog(window=0):
    cog = music.Music.__new__(music.Music)
    cog.track_failures = failures.TrackFailureBursts(window=window)
    return cog


async def _fail(cog, player, event):
    """Run the listener and settle whatever summary it armed."""
    await music.Music.on_sonolink_track_exception(cog, player, event)


async def test_a_single_failure_still_says_exactly_what_it_always_said():
    home = _Home()
    cog = _cog()
    await _fail(cog, _player(home), _event("Nice Song"))
    task = cog.track_failures._tasks.get(99)
    if task is not None:
        await task
    assert len(home.sends) == 1
    assert home.sends[0][0] == "There was a problem playing **Nice Song**, skipping it."


async def test_a_burst_of_forty_costs_two_messages_not_forty():
    """THE INCIDENT."""
    home = _Home()
    cog = _cog()
    player = _player(home)
    for index in range(40):
        await _fail(cog, player, _event("Dead {0}".format(index)))
    await cog.track_failures._tasks[99]

    assert len(home.sends) == 2
    assert home.sends[0][0].startswith("There was a problem playing **Dead 0**")
    assert home.sends[1][0] == "39 more tracks could not be played, skipping them."


async def test_a_burst_of_exactly_two_reads_singular():
    home = _Home()
    cog = _cog()
    player = _player(home)
    await _fail(cog, player, _event("A"))
    await _fail(cog, player, _event("B"))
    await cog.track_failures._tasks[99]

    assert home.sends[1][0] == "1 more track could not be played, skipping it."


async def test_two_guilds_are_announced_independently():
    home_a, home_b = _Home(), _Home()
    cog = _cog()
    await _fail(cog, _player(home_a, guild_id=1), _event("A"))
    await _fail(cog, _player(home_b, guild_id=2), _event("B"))
    for task in list(cog.track_failures._tasks.values()):
        await task

    assert len(home_a.sends) == 1
    assert len(home_b.sends) == 1


async def test_a_new_burst_after_the_window_speaks_again():
    home = _Home()
    cog = _cog()
    player = _player(home)
    await _fail(cog, player, _event("First"))
    await cog.track_failures._tasks[99]
    await _fail(cog, player, _event("Second"))
    task = cog.track_failures._tasks.get(99)
    if task is not None:
        await task

    assert [content for content, _kw in home.sends] == [
        "There was a problem playing **First**, skipping it.",
        "There was a problem playing **Second**, skipping it.",
    ]


async def test_the_announced_title_cannot_ping_or_forge_markup():
    home = _Home()
    cog = _cog()
    await _fail(cog, _player(home), _event("@everyone **free nitro**"))
    task = cog.track_failures._tasks.get(99)
    if task is not None:
        await task

    content, kwargs = home.sends[0]
    assert "\\*\\*" in content  # a title is third-party text, defused
    mentions = kwargs.get("allowed_mentions")
    assert mentions is not None and mentions.everyone is False


async def test_a_session_with_no_home_channel_announces_nothing():
    cog = _cog()
    await _fail(cog, types.SimpleNamespace(home=None, guild=None), _event())
    assert cog.track_failures._open == {}


async def test_every_failure_is_still_logged_one_line_per_track(caplog):
    """Coalescing is cosmetic to the CHAT, never to the diagnostic record."""
    home = _Home()
    cog = _cog()
    player = _player(home)
    with caplog.at_level(logging.ERROR, logger="cogs.music.music"):
        for index in range(5):
            await _fail(cog, player, _event("Dead {0}".format(index)))
    await cog.track_failures._tasks[99]

    logged = [r for r in caplog.records if "Track exception" in r.getMessage()]
    assert len(logged) == 5
    assert len(home.sends) == 2


async def test_a_send_failure_never_escapes_the_listener():
    class _Broken:
        async def send(self, *_a, **_kw):
            import discord

            raise discord.HTTPException(
                types.SimpleNamespace(status=403, reason="nope"), "forbidden"
            )

    cog = _cog()
    await _fail(cog, _player(_Broken()), _event())
