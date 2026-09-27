"""Taking a reaction role back off must cost ONE REST call, cached or not.

Discord does not send the member object on a reaction REMOVE (only on an add),
and this bot runs with ``chunk_guilds_at_startup=False`` (``core.py``), so the
member cache is sparse BY DESIGN: on a busy guild the member who un-reacted is
usually absent from it. The listener used to answer that with
``guild.get_member(...) or await guild.fetch_member(...)`` and then
``Member.remove_roles`` - a GET whose only product was an id that the very next
request needed anyway.

``Member.remove_roles(role, reason=...)`` with the default ``atomic=True`` is
exactly ``state.http.remove_role(guild.id, member.id, role.id, reason=reason)``
(discord.py 2.7.1), with no client-side hierarchy or membership check, so the
id-only call is the same request minus the fetch.

The claim here is a COUNT, so these tests count: every REST call either branch
makes lands in one recorder, which is aimed first at a case where it must
report calls and then at the cases where it must report fewer, or none.
"""

import logging
import types

import discord

from cogs.config import reactionroles
from cogs.config.reactionroles import REMOVE_REASON, ReactionRoles

GUILD = 3000
MESSAGE = 4000
USER = 5000
ROLE = 6000
EMOJI = "\N{WHITE HEAVY CHECK MARK}"


class _Rest:
    """One recorder for every REST call either branch can make."""

    def __init__(self):
        self.calls = []

    @property
    def count(self):
        return len(self.calls)


class _Http:
    def __init__(self, rest, error=None):
        self._rest = rest
        self.error = error

    async def remove_role(self, guild_id, user_id, role_id, reason=None):
        self._rest.calls.append(
            ("http.remove_role", guild_id, user_id, role_id, reason)
        )
        if self.error is not None:
            raise self.error


class _Role:
    def __init__(self, role_id):
        self.id = role_id
        self.name = "Self-assignable"


class _Member:
    def __init__(self, rest, member_id, guild_id, error=None):
        self.id = member_id
        self._rest = rest
        self._guild_id = guild_id
        self.error = error

    async def remove_roles(self, role, reason=None):
        self._rest.calls.append(
            ("Member.remove_roles", self._guild_id, self.id, role.id, reason)
        )
        if self.error is not None:
            raise self.error


class _Guild:
    def __init__(self, rest, role=None, member=None):
        self.id = GUILD
        self._rest = rest
        self._role = role
        self._member = member

    def get_role(self, role_id):
        if self._role is not None and self._role.id == role_id:
            return self._role
        return None

    def get_member(self, user_id):
        if self._member is not None and self._member.id == user_id:
            return self._member
        return None

    async def fetch_member(self, user_id):
        # The call this lot exists to delete. Recorded, never expected.
        self._rest.calls.append(("Guild.fetch_member", self.id, user_id))
        return _Member(self._rest, user_id, self.id)


def _payload(emoji=EMOJI, guild_id=GUILD, user_id=USER, message_id=MESSAGE):
    return types.SimpleNamespace(
        guild_id=guild_id,
        message_id=message_id,
        user_id=user_id,
        emoji=emoji,
    )


def _cog(guild, rest, http_error=None):
    cog = ReactionRoles.__new__(ReactionRoles)
    cog.bot = types.SimpleNamespace(
        get_guild=lambda gid: guild if guild is not None and gid == GUILD else None,
        http=_Http(rest, error=http_error),
    )
    cog.cache = {(MESSAGE, EMOJI): ROLE}
    return cog


def _not_found(text="Unknown Member"):
    response = types.SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(response, text)


# ---------------------------------------------------------------------------
# The counter, aimed at a case it must report before anything else.
# ---------------------------------------------------------------------------


async def test_the_rest_counter_reports_the_call_a_cached_member_makes():
    """NEGATIVE CONTROL: a run that DOES hit the API must show up as one call."""
    rest = _Rest()
    member = _Member(rest, USER, GUILD)
    guild = _Guild(rest, role=_Role(ROLE), member=member)

    await _cog(guild, rest).on_raw_reaction_remove(_payload())

    assert rest.calls == [("Member.remove_roles", GUILD, USER, ROLE, REMOVE_REASON)]
    assert rest.count == 1


async def test_an_unmapped_reaction_makes_no_call_at_all():
    """...and the counter really can read zero, so zero means zero."""
    rest = _Rest()
    guild = _Guild(rest, role=_Role(ROLE), member=_Member(rest, USER, GUILD))
    cog = _cog(guild, rest)

    await cog.on_raw_reaction_remove(_payload(emoji="\N{CROSS MARK}"))

    assert rest.count == 0


# ---------------------------------------------------------------------------
# The saving: an uncached member costs one call, not two.
# ---------------------------------------------------------------------------


async def test_an_uncached_member_is_stripped_in_a_single_request():
    rest = _Rest()
    guild = _Guild(rest, role=_Role(ROLE), member=None)

    await _cog(guild, rest).on_raw_reaction_remove(_payload())

    assert rest.calls == [
        ("http.remove_role", GUILD, USER, ROLE, REMOVE_REASON)
    ]
    assert rest.count == 1
    assert not any(call[0] == "Guild.fetch_member" for call in rest.calls)


async def test_both_branches_write_the_same_audit_reason():
    """A moderator reading the audit log must not be able to tell them apart."""
    reasons = set()
    for member in (True, False):
        rest = _Rest()
        guild = _Guild(
            rest,
            role=_Role(ROLE),
            member=_Member(rest, USER, GUILD) if member else None,
        )
        await _cog(guild, rest).on_raw_reaction_remove(_payload())
        reasons.add(rest.calls[0][-1])

    assert reasons == {REMOVE_REASON}


async def test_a_mapping_whose_role_is_gone_costs_nothing():
    """The role lookup moved AHEAD of the member work, so a dead mapping is free.

    It used to fetch the member first and only then discover there was no role
    to remove - one wasted request per un-reaction, forever, on any mapping
    whose role was deleted without the mapping being cleaned up.
    """
    rest = _Rest()
    guild = _Guild(rest, role=None, member=None)

    await _cog(guild, rest).on_raw_reaction_remove(_payload())

    assert rest.count == 0


async def test_an_unknown_guild_or_a_dm_reaction_costs_nothing():
    rest = _Rest()
    cog = _cog(None, rest)

    await cog.on_raw_reaction_remove(_payload())
    await cog.on_raw_reaction_remove(_payload(guild_id=None))

    assert rest.count == 0


async def test_a_variation_selector_still_matches_the_stored_emoji():
    """The FE0F strip is part of the mapping lookup and must survive the reorder."""
    rest = _Rest()
    guild = _Guild(rest, role=_Role(ROLE), member=None)

    await _cog(guild, rest).on_raw_reaction_remove(
        _payload(emoji=EMOJI + "️")
    )

    assert rest.count == 1


# ---------------------------------------------------------------------------
# Failure handling: the member who already left stays silent, like before.
# ---------------------------------------------------------------------------


async def test_a_member_who_already_left_is_not_logged_as_a_failure(caplog):
    """404 is the case the old ``except discord.HTTPException`` swallowed.

    The old body caught a failing ``fetch_member`` and simply did nothing; the
    id-only call learns the same thing from the API instead, and must stay just
    as quiet about it.
    """
    rest = _Rest()
    guild = _Guild(rest, role=_Role(ROLE), member=None)
    cog = _cog(guild, rest, http_error=_not_found())

    with caplog.at_level(logging.ERROR, logger=reactionroles.log.name):
        await cog.on_raw_reaction_remove(_payload())

    assert rest.count == 1
    assert caplog.records == []


async def test_a_real_api_failure_is_still_logged(caplog):
    """...but the silence is scoped to 404. Anything else is still a failure."""
    rest = _Rest()
    guild = _Guild(rest, role=_Role(ROLE), member=None)
    cog = _cog(guild, rest, http_error=RuntimeError("500 from Discord"))

    with caplog.at_level(logging.ERROR, logger=reactionroles.log.name):
        await cog.on_raw_reaction_remove(_payload())

    assert [record.message for record in caplog.records] == [
        "Failed to remove role"
    ]


async def test_a_cached_member_failing_is_still_logged(caplog):
    """The cached branch keeps its own swallow-and-log, unchanged."""
    rest = _Rest()
    member = _Member(rest, USER, GUILD, error=RuntimeError("nope"))
    guild = _Guild(rest, role=_Role(ROLE), member=member)

    with caplog.at_level(logging.ERROR, logger=reactionroles.log.name):
        await _cog(guild, rest).on_raw_reaction_remove(_payload())

    assert [record.message for record in caplog.records] == [
        "Failed to remove role"
    ]


# ---------------------------------------------------------------------------
# The library fact the whole change rests on.
# ---------------------------------------------------------------------------


def test_member_remove_roles_is_the_same_http_call_this_listener_now_makes():
    """Read from the INSTALLED discord.py, not from memory or a docstring.

    ``Member.remove_roles`` is only interchangeable with ``http.remove_role`` if
    the atomic path really is that one request and its default really is atomic.
    Both are checked against the library actually installed, so a version bump
    that changed either turns this red instead of changing behaviour silently.
    """
    import inspect

    signature = inspect.signature(discord.Member.remove_roles)
    assert signature.parameters["atomic"].default is True

    source = inspect.getsource(discord.Member.remove_roles)
    atomic_branch = source.split("else:")[-1]
    assert "self._state.http.remove_role" in atomic_branch

    http_signature = inspect.signature(discord.http.HTTPClient.remove_role)
    assert list(http_signature.parameters) == [
        "self",
        "guild_id",
        "user_id",
        "role_id",
        "reason",
    ]


def test_a_real_bot_exposes_the_http_client_this_listener_reaches_for():
    """``bot.http`` is the seam, resolved on a REAL bot rather than a stand-in.

    The cog's stand-in above hands the listener an ``_Http``, which would keep
    passing if discord.py moved or renamed the client. Building a bare Bot is
    offline (no socket, no token) and is how the rest of this suite pins
    library attributes it depends on.
    """
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    assert callable(getattr(bot.http, "remove_role", None))
