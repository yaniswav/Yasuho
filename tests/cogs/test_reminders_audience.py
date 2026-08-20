"""A reminder must still have somebody to remind when it fires.

THE BUG THIS PINS. A reminder is delivered into a CHANNEL and it MENTIONS its
author every time. Nothing re-checked, at fire time, that the author was still
there to be mentioned - so a member who was kicked or banned kept being
announced and pinged, on their own schedule, in a room they could no longer
open. A recurring one does that for ever: the series re-inserts itself, and the
only person who could cancel it is the one person who can no longer reach
``/reminders``.

Everything checked here can change between scheduling and delivery, which is why
it is checked at DELIVERY. The two ways to lose the right to be pinged in a room
- leaving the guild, and losing read access to the channel - are both tested,
and so is the conservatism that keeps a network blip from cancelling anybody's
series: only a REST 404 counts as proof of absence.
"""

import datetime
import json
import logging
import types

import discord
import pytest

from cogs.community.reminders import (
    Reminder,
    ReminderAudienceGone,
    ReminderChannelGone,
    ReminderChannelHidden,
    ReminderUndeliverable,
)

UTC = datetime.timezone.utc
DAY = 86400
AUTHOR_ID = 5
CHANNEL_ID = 9
GUILD_ID = 3


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _http_error(cls, status):
    return cls(types.SimpleNamespace(status=status, reason="test"), "boom")


class _Perms:
    def __init__(self, view_channel=True):
        self.view_channel = view_channel


class _Guild:
    def __init__(self, *, member=None, fetch=None):
        self.id = GUILD_ID
        self._member = member
        self._fetch = fetch
        self.fetches = 0

    def get_member(self, user_id):
        if self._member is not None and self._member.id == user_id:
            return self._member
        return None

    async def fetch_member(self, user_id):
        self.fetches += 1
        if self._fetch is None:
            raise _http_error(discord.NotFound, 404)
        if isinstance(self._fetch, Exception):
            raise self._fetch
        return self._fetch


class _Channel:
    def __init__(self, guild=None, perms=None):
        self.guild = guild
        self.sent = []
        self._perms = perms if perms is not None else _Perms()

    def permissions_for(self, member):
        return self._perms

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def _member(user_id=AUTHOR_ID):
    return types.SimpleNamespace(id=user_id)


def _cog(channel, pool=None):
    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    bot = types.SimpleNamespace(
        db_pool=pool,
        loop=types.SimpleNamespace(create_task=_create_task),
        get_channel=lambda _cid: channel,
    )
    return Reminder(bot)


def _row(timer_id=1, **extra):
    payload = {
        "author_id": AUTHOR_ID,
        "channel_id": CHANNEL_ID,
        "guild_id": GUILD_ID,
        "message": "stretch",
    }
    payload.update(extra)
    return {
        "id": timer_id,
        "event": "reminder",
        "created": datetime.datetime.now(UTC) - datetime.timedelta(days=1),
        "extra": payload,
    }


# ---------------------------------------------------------------------------
# Who is still there to be reminded
# ---------------------------------------------------------------------------


async def test_a_reminder_for_a_member_who_left_is_not_delivered():
    """THE regression: a kicked or banned member keeps getting pinged."""
    channel = _Channel(_Guild(member=None, fetch=None))
    cog = _cog(channel)

    with pytest.raises(ReminderAudienceGone):
        await cog.call_timer(_row())

    assert channel.sent == []


async def test_a_member_who_can_no_longer_see_the_channel_is_not_delivered():
    """Still in the guild, but the room was closed to them: delivering would
    announce and ping them somewhere they cannot read.

    A DIFFERENT verdict from having left, though - see the series tests below.
    """
    member = _member()
    channel = _Channel(_Guild(member=member), perms=_Perms(view_channel=False))
    cog = _cog(channel)

    with pytest.raises(ReminderChannelHidden):
        await cog.call_timer(_row())

    assert channel.sent == []


async def test_a_member_who_is_still_there_is_still_reminded():
    member = _member()
    guild = _Guild(member=member)
    channel = _Channel(guild)
    cog = _cog(channel)

    await cog.call_timer(_row())

    assert len(channel.sent) == 1
    assert "stretch" in channel.sent[0][0]
    assert guild.fetches == 0  # a cache hit costs no REST call


async def test_an_empty_member_cache_is_confirmed_by_rest_before_cancelling():
    """``get_member`` answering None is NOT proof of absence - a cache can miss,
    and treating a miss as a departure would cancel live members' series."""
    guild = _Guild(member=None, fetch=_member())
    channel = _Channel(guild)
    cog = _cog(channel)

    await cog.call_timer(_row())

    assert guild.fetches == 1
    assert len(channel.sent) == 1


async def test_a_transient_lookup_failure_never_cancels_anything():
    """Only a 404 - the API stating outright that this user is not here - ends a
    reminder. A 500 or a rate limit must cost nothing."""
    guild = _Guild(member=None, fetch=_http_error(discord.HTTPException, 500))
    channel = _Channel(guild)
    cog = _cog(channel)

    await cog.call_timer(_row())

    assert len(channel.sent) == 1


async def test_a_dm_reminder_skips_the_check_entirely():
    """No guild and no overwrites: the recipient IS the channel."""
    channel = _Channel(guild=None)
    cog = _cog(channel)

    await cog.call_timer(_row(guild_id=None))

    assert len(channel.sent) == 1


async def test_a_corrupt_author_id_costs_the_check_never_the_delivery():
    channel = _Channel(_Guild(member=None, fetch=None))
    cog = _cog(channel)

    await cog.call_timer(_row(author_id="not-a-number"))

    assert len(channel.sent) == 1


async def test_an_unreadable_permission_answer_does_not_cancel_a_reminder():
    class _Hostile(_Channel):
        def permissions_for(self, member):
            raise RuntimeError("permission fold exploded")

    channel = _Hostile(_Guild(member=_member()))
    cog = _cog(channel)

    await cog.call_timer(_row())

    assert len(channel.sent) == 1


# ---------------------------------------------------------------------------
# What it does to a SERIES
# ---------------------------------------------------------------------------


class _TxContext:
    def __init__(self, pool, *, is_transaction):
        self._pool = pool
        self._is_transaction = is_transaction

    async def __aenter__(self):
        return self._pool

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DispatchPool:
    """Claims the due row and hands out id 99 for the next occurrence."""

    NEXT_ID = 99

    def __init__(self, row):
        self.row = row
        self.calls = []

    def _record(self, query, args):
        self.calls.append((query.lstrip(), args))

    async def fetchrow(self, query, *args):
        self._record(query, args)
        return self.row

    async def execute(self, query, *args):
        self._record(query, args)
        return "DELETE 1"

    async def fetchval(self, query, *args):
        self._record(query, args)
        return self.NEXT_ID

    def acquire(self):
        return _TxContext(self, is_transaction=False)

    def transaction(self):
        return _TxContext(self, is_transaction=True)

    @property
    def unwinds(self):
        return [
            call
            for call in self.calls
            if call[0].startswith("DELETE FROM timers WHERE id = $1 AND claimed_at")
            and call[1] == (self.NEXT_ID,)
        ]


def _recurring_row(seconds=DAY):
    row = _row()
    row["expires"] = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1)
    row["extra"] = dict(row["extra"], repeat_seconds=seconds, occurrence=1)
    row["extra"] = json.loads(json.dumps(row["extra"]))
    return row


async def test_a_series_whose_author_left_is_cancelled_not_merely_skipped():
    """The reschedule is already committed when the delivery discovers the
    author is gone, so skipping one firing would leave the series running for
    ever - which is the harm, not the single ping."""
    row = _recurring_row()
    pool = _DispatchPool(row)
    channel = _Channel(_Guild(member=None, fetch=None))
    cog = _cog(channel, pool)

    await cog._deliver_at_most_once(row)

    assert channel.sent == []
    assert len(pool.unwinds) == 1


async def test_a_series_that_delivered_is_left_alone():
    row = _recurring_row()
    pool = _DispatchPool(row)
    channel = _Channel(_Guild(member=_member()))
    cog = _cog(channel, pool)

    await cog._deliver_at_most_once(row)

    assert len(channel.sent) == 1
    assert pool.unwinds == []


async def test_ending_a_series_names_the_reason_and_never_the_message(caplog):
    """An operator has to be able to tell a dead channel from a departed author
    - and PRIVACY.md says the body of a reminder is not something the bot
    stores, which a log file most certainly is."""
    row = _recurring_row()
    pool = _DispatchPool(row)
    channel = _Channel(_Guild(member=None, fetch=None))
    cog = _cog(channel, pool)

    with caplog.at_level(logging.WARNING, logger="cogs.community.reminders"):
        await cog._deliver_at_most_once(row)

    assert "no longer a member" in caplog.text
    assert str(AUTHOR_ID) in caplog.text
    assert "stretch" not in caplog.text


async def test_a_channel_hidden_for_an_afternoon_does_not_delete_the_series():
    """THE second regression. Losing ``view_channel`` is not losing your
    membership: a moderator locking a channel down, a raid lockdown, a role
    handed out at 9am and taken back at 5pm all look identical at fire time.
    Ending the series there silently and permanently deletes every recurring
    reminder anybody had in that room - a moderator's temporary decision
    destroying members' data, discovered only by its absence.
    """
    row = _recurring_row()
    pool = _DispatchPool(row)
    channel = _Channel(_Guild(member=_member()), perms=_Perms(view_channel=False))
    cog = _cog(channel, pool)

    await cog._deliver_at_most_once(row)

    assert channel.sent == []  # this occurrence is still skipped
    assert pool.unwinds == [], "the whole series was deleted over a closed room"


async def test_the_series_delivers_again_once_the_channel_reopens():
    """The proof that skipping is not just 'losing it more slowly'."""
    guild = _Guild(member=_member())
    perms = _Perms(view_channel=False)
    channel = _Channel(guild, perms=perms)
    pool = _DispatchPool(_recurring_row())
    cog = _cog(channel, pool)

    await cog._deliver_at_most_once(_recurring_row())
    perms.view_channel = True
    await cog._deliver_at_most_once(_recurring_row())

    assert len(channel.sent) == 1
    assert pool.unwinds == []


def test_the_family_splits_permanent_causes_from_temporary_ones():
    """One exception family, TWO verdicts. ``terminal`` is what a future third
    reason has to choose, and getting it wrong is either an immortal ping at
    somebody who was banned or somebody else's data deleted for them."""
    assert issubclass(ReminderChannelGone, ReminderUndeliverable)
    assert issubclass(ReminderAudienceGone, ReminderUndeliverable)
    assert issubclass(ReminderChannelHidden, ReminderUndeliverable)
    assert ReminderChannelGone(CHANNEL_ID).reason
    assert ReminderAudienceGone("left").reason == "left"

    assert ReminderChannelGone(CHANNEL_ID).terminal is True
    assert ReminderAudienceGone("left").terminal is True
    assert ReminderChannelHidden("hidden").terminal is False
