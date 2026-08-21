"""AFK cog rules (cogs/community/afk.py), in order of what it would cost to
get wrong:

1. SCOPE. An AFK status is free text its author typed for ONE server. Stored
   user-globally it was re-broadcast verbatim in every other server they share
   with the bot, which is somebody's own words read out to an audience they
   never wrote them for. It is announced in its own guild and nowhere else, and
   a row with no guild recorded (written before the column existed) announces
   nowhere at all.
2. VOLUME. One notice per MESSAGE whatever it mentions, plus a per (channel,
   member) window - so mentioning an AFK member twenty times in one message
   costs one DB read and one send, and a mention loop cannot wall a channel.
3. MENTIONS. The status and the display name are free text the bot re-broadcasts
   on a trigger anyone else can pull; with default mentions that is a ping
   amplifier. Every assertion about this is made against the wire form of the
   AllowedMentions actually handed to ``channel.send`` (``to_dict()``), not
   against the message text - the text is *meant* to keep the raw characters;
   what must be impossible is Discord resolving them into a ping.
"""

import datetime
import types

import discord
import pytest

from cogs.community import afk as afk_module
from cogs.community.afk import AFK
from tools.cooldowns import Cooldowns

AFK_ID = 111
VICTIM_ID = 999
GUILD_ID = 1
OTHER_GUILD_ID = 2

# Any real timestamp: the notice formats it through human_timedelta.
_SINCE = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _fresh_debounce(monkeypatch):
    """The notice window is module state (bounded, process-wide). Each test gets
    its own, or the first notice would silence every test after it."""
    monkeypatch.setattr(
        afk_module,
        "_NOTICE_DEBOUNCE",
        Cooldowns(afk_module.NOTICE_COOLDOWN_SECONDS),
    )


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class _Channel:
    def __init__(self, channel_id=10):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class _Pool:
    """The statements the cog runs, answered the way Postgres would.

    Rows are keyed by the PAIR the table is now read and deleted on,
    ``(user_id, guild_id)`` - so this fake can never answer a question the real
    statement would not, which is the whole point of the scoping rule. A tuple
    lookup also reproduces ``guild_id IS NOT DISTINCT FROM $2`` exactly: NULL
    matches NULL, and matches nothing else.
    """

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return [
            {"user_id": user_id, "guild_id": guild_id}
            for (user_id, guild_id) in self.rows
        ]

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        row = self.rows.get(args)
        if query.lstrip().startswith("DELETE") and row is not None:
            del self.rows[args]
        return row

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return None

    @property
    def reads(self):
        return [call for call in self.calls if "SELECT" in call[0]]


def _user(user_id, display_name="Ami"):
    return types.SimpleNamespace(
        id=user_id, display_name=display_name, mention=f"<@{user_id}>", bot=False
    )


def _make_cog(pool, afk_users):
    """``afk_users`` maps user id -> the guild the status was set in (None for a
    legacy row), which is the in-memory prefilter the hot path reads."""
    cog = AFK(types.SimpleNamespace(db_pool=pool))
    cog.afk_users = dict(afk_users)
    return cog


def _message(author, mentions, channel, guild_id=GUILD_ID):
    return types.SimpleNamespace(
        author=author,
        guild=types.SimpleNamespace(id=guild_id),
        mentions=list(mentions),
        channel=channel,
    )


def _mentions_of(kwargs):
    """The wire dict of the AllowedMentions passed to a send (never implicit)."""
    allowed = kwargs.get("allowed_mentions")
    assert isinstance(allowed, discord.AllowedMentions), (
        "the send must carry an EXPLICIT allowed_mentions, not the bot default"
    )
    return allowed.to_dict()


def _assert_cannot_ping_third_parties(wire, permitted_ids):
    assert "everyone" not in wire.get("parse", [])
    assert "roles" not in wire.get("parse", [])
    assert wire.get("roles", []) == []
    # "users" in parse would mean "resolve every mention in the text", which is
    # exactly the hole; the id allow-list must be used instead.
    assert "users" not in wire.get("parse", [])
    assert set(wire.get("users", [])) == set(permitted_ids)


# ---------------------------------------------------------------------------
# Scope: the status belongs to the guild it was set in
# ---------------------------------------------------------------------------


async def test_a_status_set_in_one_guild_is_never_announced_in_another():
    """THE CROSS-TENANT LEAK. "back in a bit, ping <@boss> if it burns" is text
    written for one server's audience; another server it is replayed in gets a
    stranger's message, the names in it and the fact that they are away."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": "at the dentist", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(
        _message(_user(222), [afk_member], channel, guild_id=OTHER_GUILD_ID)
    )

    assert channel.sent == []
    assert pool.calls == [], "and it costs no query either - the prefilter answers"


async def test_a_status_set_here_is_announced_here():
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": "at the dentist", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    assert len(channel.sent) == 1
    assert "at the dentist" in channel.sent[0][0]


async def test_the_read_itself_is_scoped_to_this_guild():
    """Belt and braces: the in-memory prefilter decides fast, but the SQL is
    what is authoritative, so it carries the guild too."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": "brb", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    query, args = pool.reads[0]
    assert "guild_id" in query
    assert args == (AFK_ID, GUILD_ID)


async def test_a_legacy_row_with_no_guild_announces_nowhere():
    """A row written before the column existed has an unknown origin, and an
    unknown origin is not a licence to republish somebody's text."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, None): {"message": "old status", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: None})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    assert channel.sent == []


async def test_setting_a_status_records_the_guild_it_was_set_in():
    pool = _Pool()
    cog = _make_cog(pool, {})
    sent = []
    ctx = types.SimpleNamespace(
        author=_user(AFK_ID),
        guild=types.SimpleNamespace(id=GUILD_ID),
        send=lambda **kwargs: sent.append(kwargs) or _noop(),
    )

    await AFK.afk.callback(cog, ctx, message="brb")

    query, args = pool.calls[0]
    assert "guild_id" in query
    assert args == (AFK_ID, GUILD_ID, "brb")
    assert cog.afk_users == {AFK_ID: GUILD_ID}


async def _noop():
    return None


# ---------------------------------------------------------------------------
# Volume: one notice per message, one per window
# ---------------------------------------------------------------------------


async def test_twenty_mentions_in_one_message_produce_one_notice():
    """The amplifier: the notice used to be sent once PER MENTION, so a single
    message could turn one member's status into twenty sends of their text."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": "brb", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(_user(222), [afk_member] * 20, channel))

    assert len(channel.sent) == 1
    assert len(pool.reads) == 1, "one DB read too, not twenty"


async def test_two_afk_members_named_in_one_message_still_cost_one_notice():
    channel = _Channel()
    first, second = _user(AFK_ID), _user(AFK_ID + 1)
    pool = _Pool(
        {
            (AFK_ID, GUILD_ID): {"message": "brb", "since": _SINCE},
            (AFK_ID + 1, GUILD_ID): {"message": "afk", "since": _SINCE},
        }
    )
    cog = _make_cog(pool, {AFK_ID: GUILD_ID, AFK_ID + 1: GUILD_ID})

    await cog.on_message(_message(_user(222), [first, second], channel))

    assert len(channel.sent) == 1


async def test_a_mention_loop_in_one_channel_is_throttled():
    """Ten messages, each a legitimate single mention: without a window this is
    ten sends of the same line, which is how a channel gets walled."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": "brb", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    for _i in range(10):
        await cog.on_message(_message(_user(222), [afk_member], channel))

    assert len(channel.sent) == 1
    assert len(pool.reads) == 1


async def test_the_window_is_per_channel_and_per_member():
    """Not a global mute: someone pinging them in a DIFFERENT channel still
    learns they are away, and so does someone pinging a DIFFERENT member."""
    here, there = _Channel(10), _Channel(11)
    first, second = _user(AFK_ID), _user(AFK_ID + 1)
    pool = _Pool(
        {
            (AFK_ID, GUILD_ID): {"message": "brb", "since": _SINCE},
            (AFK_ID + 1, GUILD_ID): {"message": "afk", "since": _SINCE},
        }
    )
    cog = _make_cog(pool, {AFK_ID: GUILD_ID, AFK_ID + 1: GUILD_ID})

    await cog.on_message(_message(_user(222), [first], here))
    await cog.on_message(_message(_user(222), [first], there))
    await cog.on_message(_message(_user(222), [second], here))

    assert len(here.sent) == 2
    assert len(there.sent) == 1


# ---------------------------------------------------------------------------
# The AFK notice: the re-broadcast surface (mention policy)
# ---------------------------------------------------------------------------


async def test_an_afk_status_cannot_ping_the_third_parties_it_names():
    channel = _Channel()
    afk_member = _user(AFK_ID)
    status = f"@everyone <@{VICTIM_ID}> wake up"
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": status, "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    assert len(channel.sent) == 1, "the AFK notice must still be posted"
    content, kwargs = channel.sent[0]
    assert f"<@{VICTIM_ID}>" in content  # the text is untouched...
    wire = _mentions_of(kwargs)
    _assert_cannot_ping_third_parties(wire, {AFK_ID})  # ...but inert
    assert VICTIM_ID not in wire.get("users", [])


async def test_the_afk_members_own_mention_still_resolves():
    """The subject of the sentence stays pingable: only they are named by the
    surface, so restricting the allow-list must not silence their own tag."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool(
        {(AFK_ID, GUILD_ID): {"message": f"back soon, poke <@{AFK_ID}>", "since": _SINCE}}
    )
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    assert _mentions_of(channel.sent[0][1]).get("users") == [AFK_ID]


async def test_a_display_name_cannot_smuggle_an_everyone_ping():
    """The status is not the only free text in the notice - the display name is
    quoted verbatim too, and "@everyone" as a nickname is a one-word raid."""
    channel = _Channel()
    afk_member = _user(AFK_ID, display_name="@everyone")
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": "brb", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    _assert_cannot_ping_third_parties(_mentions_of(channel.sent[0][1]), {AFK_ID})


async def test_every_repeat_of_the_notice_carries_the_policy():
    """The amplification is in the repetition: three channels, three sends (the
    window is per channel), and the guard must hold on each one."""
    channels = [_Channel(20), _Channel(21), _Channel(22)]
    afk_member = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"message": f"<@{VICTIM_ID}>", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    for channel in channels:
        await cog.on_message(_message(_user(222), [afk_member], channel))

    for channel in channels:
        assert len(channel.sent) == 1
        _assert_cannot_ping_third_parties(_mentions_of(channel.sent[0][1]), {AFK_ID})


async def test_a_member_who_is_not_afk_produces_no_notice_at_all():
    channel = _Channel()
    cog = _make_cog(_Pool(), {})

    await cog.on_message(_message(_user(222), [_user(333)], channel))

    assert channel.sent == []


# ---------------------------------------------------------------------------
# The welcome-back line
# ---------------------------------------------------------------------------


async def test_welcome_back_only_ever_pings_the_returning_member():
    channel = _Channel()
    author = _user(AFK_ID, display_name="@everyone")
    pool = _Pool({(AFK_ID, GUILD_ID): {"since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(author, [], channel))

    assert len(channel.sent) == 1
    _assert_cannot_ping_third_parties(_mentions_of(channel.sent[0][1]), {AFK_ID})
    assert cog.afk_users == {}


async def test_talking_in_another_guild_neither_clears_nor_costs_a_write():
    """They are AFK in server A; a message in server B is not them coming back
    THERE - and must not cost one DELETE attempt per message either."""
    channel = _Channel()
    author = _user(AFK_ID)
    pool = _Pool({(AFK_ID, GUILD_ID): {"since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})

    await cog.on_message(_message(author, [], channel, guild_id=OTHER_GUILD_ID))

    assert channel.sent == []
    assert pool.calls == []
    assert cog.afk_users == {AFK_ID: GUILD_ID}


async def test_an_ordinary_message_leaves_the_listener_without_awaiting_anything():
    """The hot path, at 1000+ guilds: the overwhelming majority of messages have
    a non-AFK author and mention nobody, and for those this listener must do
    nothing at all - not even reach the two handlers to be told there is nothing
    to do, which cost a coroutine object each per message.

    Drop the prefilter from ``on_message`` and this fails: both handlers are
    entered (and awaited) for a message that could never concern them.
    """
    entered = []

    async def _clear(message):
        entered.append("clear")

    async def _notify(message):
        entered.append("notify")

    channel = _Channel()
    pool = _Pool({(AFK_ID, GUILD_ID): {"since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: GUILD_ID})
    cog._clear_if_back = _clear
    cog._notify_one_mention = _notify

    # Somebody else, mentioning nobody, while an unrelated member IS afk here.
    await cog.on_message(_message(_user(222), [], channel))

    assert entered == []
    assert pool.calls == []

    # ... and the prefilter is a filter, not a lid: either half opens it.
    await cog.on_message(_message(_user(AFK_ID), [], channel))
    assert entered == ["clear", "notify"]

    entered.clear()
    await cog.on_message(_message(_user(222), [_user(AFK_ID)], channel))
    assert entered == ["clear", "notify"]


async def test_a_legacy_row_clears_the_first_time_its_author_speaks_anywhere():
    """The self-healing half of the NULL rule: it announces nowhere, and the
    next thing its author says anywhere retires it for good."""
    channel = _Channel()
    author = _user(AFK_ID)
    pool = _Pool({(AFK_ID, None): {"since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID: None})

    await cog.on_message(_message(author, [], channel, guild_id=OTHER_GUILD_ID))

    assert len(channel.sent) == 1
    assert cog.afk_users == {}
    assert pool.rows == {}
