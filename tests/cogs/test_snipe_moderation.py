"""?snipe must not be an undo button for the bot's own deletions.

THE BUG THIS PINS. The snipe cache took EVERY deletion it saw and handed the
last one back to anybody who asked. So a member could post a Discord invite, let
AutoMod delete it, type ?snipe - and the invite was back in the channel, this
time inside an embed the bot itself posted. The same trick replayed a message
`?purge 1` had just removed, and (worse) the AniList flow's cleanup, which
deletes a member's message precisely BECAUSE it contains an API token.

THE MARKER. MESSAGE_DELETE does not say who deleted a message, so the answer is
recorded on the way out instead: every bot-side deletion in this process funnels
through ``HTTPClient.delete_message``, which the cog wraps to note the id. Two
properties are what make it work and both are tested below - the mark is written
BEFORE the HTTP call (the gateway event can beat the await), and the check runs
at CACHE time, so a message the bot removed never sits in memory at all.
"""

import datetime
import inspect
import time
import types

import discord
import pytest

from cogs.utility import utility

UTC = datetime.timezone.utc
CHANNEL_ID = 5000
OTHER_CHANNEL_ID = 6000
MESSAGE_ID = 777001


@pytest.fixture(autouse=True)
def _isolate_module_state():
    utility._bot_deleted.clear()
    yield
    utility._bot_deleted.clear()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Author:
    def __init__(self, *, bot=False, name="Kira"):
        self.bot = bot
        self._name = name
        self.display_avatar = types.SimpleNamespace(url="https://cdn.test/a.png")

    def __str__(self):
        return self._name


class _Message:
    def __init__(self, content="hi", *, message_id=MESSAGE_ID, channel_id=CHANNEL_ID, author=None):
        self.id = message_id
        self.content = content
        self.channel = types.SimpleNamespace(id=channel_id)
        self.author = author if author is not None else _Author()
        self.created_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)


class _Http:
    """Stands in for discord.py's HTTPClient, with the one method that matters."""

    def __init__(self):
        self.deleted = []

    async def delete_message(self, channel_id, message_id, *, reason=None):
        self.deleted.append((channel_id, message_id))


class _RacingHttp(_Http):
    """A delete whose gateway event lands BEFORE the HTTP call returns.

    Not a contrived case: the deletion and the event dispatch share one event
    loop and nothing orders them, so this is the ordering a marker written after
    the await would silently miss.
    """

    def __init__(self):
        super().__init__()
        self.cog = None
        self.message = None

    async def delete_message(self, channel_id, message_id, *, reason=None):
        await super().delete_message(channel_id, message_id)
        await utility.Utility.on_message_delete(self.cog, self.message)


class _Bot:
    def __init__(self, http=None):
        self.http = http if http is not None else _Http()


class _Ctx:
    def __init__(self, channel_id=CHANNEL_ID):
        self.channel = types.SimpleNamespace(id=channel_id)
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))

    @property
    def texts(self):
        return [args[0] for args, _kw in self.sends if args]

    @property
    def embeds(self):
        return [kw["embed"] for _a, kw in self.sends if kw.get("embed") is not None]


def _cog(http=None):
    return utility.Utility(_Bot(http))


async def _delete(cog, message):
    """The gateway event for a deletion, whoever performed it."""
    await utility.Utility.on_message_delete(cog, message)


async def _snipe(cog, ctx):
    await utility.Utility.snipe.callback(cog, ctx)
    return ctx


# ---------------------------------------------------------------------------
# The hole
# ---------------------------------------------------------------------------


async def test_a_message_this_bot_deleted_is_never_snipeable():
    """THE regression: post an invite, let the bot delete it, snipe it back."""
    cog = _cog()
    invite = _Message("join https://discord.gg/raid")

    await cog.bot.http.delete_message(CHANNEL_ID, invite.id)
    await _delete(cog, invite)

    ctx = await _snipe(cog, _Ctx())
    assert ctx.embeds == []
    assert "Nothing to snipe" in ctx.texts[0]


async def test_a_bot_deletion_is_dropped_at_cache_time_not_at_snipe_time():
    """A message the bot removed must not sit in memory waiting to be asked
    for - the deletion is the point, so it is never stored at all."""
    cog = _cog()
    message = _Message("secret token abcdef")

    await cog.bot.http.delete_message(CHANNEL_ID, message.id)
    await _delete(cog, message)

    assert cog._snipes == {}


async def test_the_marker_is_written_before_the_delete_call_is_awaited():
    """The ordering the fix depends on: the event can arrive mid-await."""
    http = _RacingHttp()
    cog = _cog(http)
    http.cog = cog
    http.message = _Message("join https://discord.gg/raid")

    await cog.bot.http.delete_message(CHANNEL_ID, http.message.id)

    assert http.deleted == [(CHANNEL_ID, http.message.id)]  # it really deleted
    assert cog._snipes == {}  # ...and the event it raced was still filtered


async def test_a_deletion_by_somebody_else_is_still_snipeable():
    """The feature still works: only OUR deletions are withheld."""
    cog = _cog()

    await _delete(cog, _Message("oops wrong channel"))

    ctx = await _snipe(cog, _Ctx())
    assert ctx.embeds[0].description == "oops wrong channel"


async def test_a_marker_is_spent_by_the_event_it_predicted():
    """It marks ONE deletion, not the id for ever: a message id is unique, but
    the map must drain in the common case rather than wait out its TTL."""
    cog = _cog()
    message = _Message("first")

    await cog.bot.http.delete_message(CHANNEL_ID, message.id)
    await _delete(cog, message)

    assert utility._bot_deleted == {}


async def test_a_marker_that_never_lands_expires_instead_of_leaking():
    """A deletion that 403s produces no gateway event, so its mark has to age
    out on its own or the map would grow for the life of the process."""
    utility.mark_bot_deleted(4242, now=0.0)

    assert utility.take_bot_deleted(4242, now=utility._BOT_DELETED_TTL + 1) is False


async def test_the_marker_map_is_swept_and_cannot_grow_without_bound():
    now = time.monotonic()
    for index in range(utility._BOT_DELETED_SWEEP_AT + 2):
        utility.mark_bot_deleted(index, now=now - utility._BOT_DELETED_TTL - 1)
    utility.mark_bot_deleted(999999, now=now)

    assert len(utility._bot_deleted) == 1


# ---------------------------------------------------------------------------
# Scope: the channel, and the mentions
# ---------------------------------------------------------------------------


async def test_snipe_only_ever_reads_the_channel_it_was_invoked_in():
    cog = _cog()

    await _delete(cog, _Message("said in another room", channel_id=OTHER_CHANNEL_ID))

    ctx = await _snipe(cog, _Ctx(CHANNEL_ID))
    assert ctx.embeds == []
    assert "Nothing to snipe" in ctx.texts[0]


async def test_a_sniped_message_cannot_ping_anybody():
    """The body is somebody else's raw text being replayed by the bot."""
    cog = _cog()

    await _delete(cog, _Message("@everyone <@&1234> get in here"))

    ctx = await _snipe(cog, _Ctx())
    _args, kwargs = ctx.sends[0]
    wire = kwargs["allowed_mentions"].to_dict()
    assert wire.get("parse", []) == []
    assert wire.get("users", []) == []
    assert wire.get("roles", []) == []


async def test_bot_messages_and_empty_messages_are_still_ignored():
    cog = _cog()

    await _delete(cog, _Message("beep", author=_Author(bot=True)))
    await _delete(cog, _Message(""))

    assert cog._snipes == {}


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------


def test_the_library_seam_the_marker_wraps_still_exists():
    """If discord.py ever renames or re-signs this, the hook silently stops
    marking anything and the hole re-opens. Break loudly here instead."""
    method = getattr(discord.http.HTTPClient, "delete_message", None)
    assert method is not None
    parameters = list(inspect.signature(method).parameters)
    # The wrapper reads the id positionally (args[1] after self is bound).
    assert parameters[:3] == ["self", "channel_id", "message_id"]


async def test_the_hook_is_installed_once_and_removed_on_unload():
    bot = _Bot()
    original = bot.http.delete_message
    cog = utility.Utility(bot)

    assert bot.http.delete_message is not original  # wrapped
    second = utility.install_bot_delete_marker(bot)
    assert second is None  # a reload must not wrap the wrapper

    cog.cog_unload()
    assert bot.http.delete_message == original
    assert bot.http._yasuho_snipe_marker is False


async def test_a_deletion_still_happens_when_the_marker_cannot_be_written():
    """The marker is a nicety; the deletion is not."""
    bot = _Bot()
    utility.Utility(bot)

    await bot.http.delete_message(CHANNEL_ID, object())  # unmarkable id

    assert bot.http.deleted  # the real call went through anyway
