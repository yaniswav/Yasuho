"""``on_member_join`` must not query ``mutedmembers`` for a guild with no mute role.

Every join used to run a ``SELECT ... FROM mutedmembers`` before looking at
whether the guild even HAS a mute role - a role most guilds never configure. The
answer was then thrown away, because with no role there is nothing to re-apply.
Joins arrive in bursts (a raid, a re-invite, a popular server's evening), so that
was one round trip per join buying nothing.

The eager in-memory cache (``bot.muteroles``, loaded at startup and refreshed by
the dashboard NOTIFY) is now read FIRST, exactly like ``bot.autoroles`` a few
lines above it. These tests pin the skip AND the re-mute it must not break.
"""

import types

from cogs.system import events


class _Pool:
    """Counts every mutedmembers read, and answers with a configurable row."""

    def __init__(self, muted_member_id=None):
        self.muted_member_id = muted_member_id
        self.fetchvals = []

    async def fetchval(self, query, *args):
        self.fetchvals.append((query, args))
        assert "mutedmembers" in query
        return self.muted_member_id


class _Guild:
    def __init__(self, guild_id, roles=None):
        self.id = guild_id
        self._roles = roles or {}

    def get_role(self, role_id):
        return self._roles.get(role_id)


class _Member:
    def __init__(self, guild, member_id=7):
        self.id = member_id
        self.guild = guild
        self.added_roles = []

    async def add_roles(self, role, reason=None):
        self.added_roles.append((role, reason))

    async def send(self, *_args, **_kwargs):  # pragma: no cover - blacklist path
        raise AssertionError("no DM is expected in these tests")


def _cog(pool, *, muteroles=None, autoroles=None):
    cog = object.__new__(events.Events)
    cog.bot = types.SimpleNamespace(
        db_pool=pool,
        blacklist=set(),
        autoroles=autoroles or {},
        muteroles=muteroles or {},
    )
    return cog


# ---------------------------------------------------------------------------
# The regression: no mute role -> no query.
# ---------------------------------------------------------------------------


async def test_join_without_a_mute_role_never_queries_mutedmembers():
    pool = _Pool(muted_member_id=7)
    guild = _Guild(42)

    await _cog(pool).on_member_join(_Member(guild))

    assert pool.fetchvals == []


async def test_join_whose_cached_mute_role_no_longer_exists_skips_too():
    """The id is cached but the role was deleted: nothing to re-apply, so no read."""
    pool = _Pool(muted_member_id=7)
    guild = _Guild(42, roles={})

    await _cog(pool, muteroles={42: 99}).on_member_join(_Member(guild))

    assert pool.fetchvals == []


# ---------------------------------------------------------------------------
# ... and a guild that DOES have one still re-mutes evaders exactly as before.
# ---------------------------------------------------------------------------


async def test_join_with_a_mute_role_still_re_mutes_a_known_evader():
    role = object()
    pool = _Pool(muted_member_id=7)
    guild = _Guild(42, roles={99: role})
    member = _Member(guild)

    await _cog(pool, muteroles={42: 99}).on_member_join(member)

    assert len(pool.fetchvals) == 1
    assert pool.fetchvals[0][1] == (42, 7)
    assert [applied for applied, _reason in member.added_roles] == [role]


async def test_join_with_a_mute_role_leaves_an_unmuted_member_alone():
    role = object()
    pool = _Pool(muted_member_id=None)
    guild = _Guild(42, roles={99: role})
    member = _Member(guild)

    await _cog(pool, muteroles={42: 99}).on_member_join(member)

    assert len(pool.fetchvals) == 1
    assert member.added_roles == []


async def test_the_autorole_still_lands_when_the_mute_path_short_circuits():
    """The early return must not swallow the autorole applied before it."""
    autorole = object()
    pool = _Pool(muted_member_id=7)
    guild = _Guild(42, roles={5: autorole})
    member = _Member(guild)

    await _cog(pool, autoroles={42: 5}).on_member_join(member)

    assert [applied for applied, _reason in member.added_roles] == [autorole]
    assert pool.fetchvals == []
