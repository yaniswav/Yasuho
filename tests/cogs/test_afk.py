"""Mention-policy tests for the AFK cog (cogs/community/afk.py).

An AFK status is free text the member types once and the bot then re-broadcasts
live, in their channel, EVERY time somebody pings them. With default mentions
that is a ping amplifier: "@everyone" or a victim's mention parked in a status
fires again and again, on a trigger anyone else can pull. The same is true of
the display name the notice quotes.

So every assertion below is made against the wire form of the AllowedMentions
actually handed to ``channel.send`` (``to_dict()``), not against the message
text - the text is *meant* to keep the raw characters; what must be impossible
is Discord resolving them into a ping.
"""

import datetime
import types

import discord

from cogs.community.afk import AFK

AFK_ID = 111
VICTIM_ID = 999

# Any real timestamp: the notice formats it through human_timedelta.
_SINCE = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class _Pool:
    """Only the two statements the cog runs, keyed by the user id argument."""

    def __init__(self, rows=None):
        self.rows = rows or {}

    async def fetch(self, _query, *_args):
        return []

    async def fetchrow(self, _query, *args):
        return self.rows.get(args[0])

    async def execute(self, _query, *_args):
        return None


def _user(user_id, display_name="Ami"):
    return types.SimpleNamespace(
        id=user_id, display_name=display_name, mention=f"<@{user_id}>", bot=False
    )


def _make_cog(pool, afk_users):
    cog = AFK(types.SimpleNamespace(db_pool=pool))
    cog.afk_users = set(afk_users)
    return cog


def _message(author, mentions, channel):
    return types.SimpleNamespace(
        author=author,
        guild=types.SimpleNamespace(id=1),
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
# The AFK notice (the re-broadcast surface)
# ---------------------------------------------------------------------------


async def test_an_afk_status_cannot_ping_the_third_parties_it_names():
    channel = _Channel()
    afk_member = _user(AFK_ID)
    status = f"@everyone <@{VICTIM_ID}> wake up"
    pool = _Pool({AFK_ID: {"message": status, "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID})

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
    pool = _Pool({AFK_ID: {"message": f"back soon, poke <@{AFK_ID}>", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    assert _mentions_of(channel.sent[0][1]).get("users") == [AFK_ID]


async def test_a_display_name_cannot_smuggle_an_everyone_ping():
    """The status is not the only free text in the notice - the display name is
    quoted verbatim too, and "@everyone" as a nickname is a one-word raid."""
    channel = _Channel()
    afk_member = _user(AFK_ID, display_name="@everyone")
    pool = _Pool({AFK_ID: {"message": "brb", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID})

    await cog.on_message(_message(_user(222), [afk_member], channel))

    _assert_cannot_ping_third_parties(_mentions_of(channel.sent[0][1]), {AFK_ID})


async def test_every_repeat_of_the_notice_carries_the_policy():
    """The amplification is in the repetition: three pings, three sends, and the
    guard must hold on each one (not just the first)."""
    channel = _Channel()
    afk_member = _user(AFK_ID)
    pool = _Pool({AFK_ID: {"message": f"<@{VICTIM_ID}>", "since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID})

    for _i in range(3):
        await cog.on_message(_message(_user(222), [afk_member], channel))

    assert len(channel.sent) == 3
    for _content, kwargs in channel.sent:
        _assert_cannot_ping_third_parties(_mentions_of(kwargs), {AFK_ID})


async def test_a_member_who_is_not_afk_produces_no_notice_at_all():
    channel = _Channel()
    cog = _make_cog(_Pool(), set())

    await cog.on_message(_message(_user(222), [_user(333)], channel))

    assert channel.sent == []


# ---------------------------------------------------------------------------
# The welcome-back line
# ---------------------------------------------------------------------------


async def test_welcome_back_only_ever_pings_the_returning_member():
    channel = _Channel()
    author = _user(AFK_ID, display_name="@everyone")
    pool = _Pool({AFK_ID: {"since": _SINCE}})
    cog = _make_cog(pool, {AFK_ID})

    await cog.on_message(_message(author, [], channel))

    assert len(channel.sent) == 1
    _assert_cannot_ping_third_parties(_mentions_of(channel.sent[0][1]), {AFK_ID})
