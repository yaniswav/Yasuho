"""An UNCACHED target must be refused, never waved through.

Yasuho runs with ``chunk_guilds_at_startup=False`` (core.py), so
``guild.get_member`` is a sparse cache: a miss means "I have not seen them",
NOT "they are not in this server". The ban/kick/tempban/massban commands are
annotated ``discord.User`` / ``discord.Object``, whose converters happily hand
back a bare User for someone who IS a member - and the old cache-only guard read
that miss as "no hierarchy to compare" and returned None. The guard therefore
DEGRADED TO ALLOWED for exactly the quiet senior staffer a moderator would want
to attack: someone who has not spoken since the last restart.

The fix resolves the target before deciding, and refuses when the rank cannot be
established. These tests pin all three outcomes of that resolution:

* uncached BUT actually a higher-ranked member -> refused (the escalation),
* uncached and genuinely absent (a real 404)     -> allowed (hackbans still work),
* uncached and UNRESOLVABLE (5xx / timeout)      -> refused (fail closed).

Pure fakes: no Discord, no network. ``fetch_member`` / ``query_members`` are
counted so the cost claim (one REST call per single target only on a cache miss;
gateway batches, not N REST calls, for massban) is asserted, not assumed.
"""

import types

import discord

from cogs.moderation import moderation
from tools import modchecks


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Role:
    def __init__(self, position, name="role"):
        self.position = position
        self.name = name

    def __ge__(self, other):
        return self.position >= other.position


class _Member:
    """A guild member as far as the hierarchy comparison is concerned."""

    def __init__(self, uid, top_pos):
        self.id = uid
        self.top_role = _Role(top_pos, f"member-{uid}")
        self.mention = f"<@{uid}>"
        self.display_avatar = types.SimpleNamespace(url="https://example.test/a.png")


class _User:
    """What a ``discord.User`` converter hands back: no roles, just an identity."""

    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.display_avatar = types.SimpleNamespace(url="https://example.test/a.png")


def _http_response(status):
    return types.SimpleNamespace(status=status, reason="probe")


class _Guild:
    """Sparse member cache plus a scripted ``fetch_member`` / ``query_members``.

    ``cached`` is what ``get_member`` knows; ``server_side`` is who is REALLY in
    the guild. The gap between the two is the bug's whole habitat.
    """

    def __init__(self, owner_id=100, bot_top=50, cached=(), server_side=(), fails=False):
        self.id = 10
        self.owner_id = owner_id
        self.me = types.SimpleNamespace(top_role=_Role(bot_top, "bot-top"))
        self.icon = None
        self._cached = {m.id: m for m in cached}
        self._server_side = {m.id: m for m in server_side}
        self.fails = fails
        self.fetch_calls = []
        self.query_calls = []
        self.bans = []
        self.kicks = []
        self.bulk_ban_lots = []

    def get_member(self, uid):
        return self._cached.get(uid)

    async def fetch_member(self, uid):
        self.fetch_calls.append(uid)
        if self.fails:
            raise discord.HTTPException(_http_response(503), "upstream is sulking")
        member = self._server_side.get(uid)
        if member is None:
            raise discord.NotFound(_http_response(404), "Unknown Member")
        return member

    async def query_members(self, *, user_ids, limit=100, cache=True):
        self.query_calls.append(list(user_ids))
        if self.fails:
            raise TimeoutError("gateway member query timed out")
        return [self._server_side[u] for u in user_ids if u in self._server_side]

    async def ban(self, target, *, reason=None, **kwargs):
        self.bans.append(target.id)

    async def kick(self, target, *, reason=None):
        self.kicks.append(target.id)

    async def bulk_ban(self, users, *, reason=None, delete_message_seconds=0):
        ids = [u.id for u in users]
        self.bulk_ban_lots.append(ids)
        return types.SimpleNamespace(
            banned=[types.SimpleNamespace(id=i) for i in ids], failed=[]
        )


class _EditMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _Ctx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.sent = []
        self.confirm_message = _EditMsg()
        self.message = types.SimpleNamespace(created_at=None)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _author(uid=1, top_pos=10):
    return types.SimpleNamespace(
        id=uid,
        top_role=_Role(top_pos, "author-top"),
        mention=f"<@{uid}>",
        display_avatar=types.SimpleNamespace(url="https://example.test/a.png"),
        __str__=lambda self: f"mod-{uid}",
    )


class _Bot:
    def __init__(self, pool):
        self.db_pool = pool

    def get_cog(self, name):
        return None


def _cog(fake_pool, confirm=True):
    cog = moderation.Moderation(_Bot(fake_pool))

    async def _confirm(_ctx, _embed, **_kw):
        return confirm

    async def _noop(*_a, **_kw):
        return None

    cog._confirm = _confirm
    cog._post_modlog = _noop
    return cog


def _last_text(ctx):
    return ctx.sent[-1][0][0]


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------
async def test_resolver_refuses_an_uncached_higher_ranked_member():
    """The core escalation: uncached, but really outranks the moderator."""
    boss = _Member(2, top_pos=40)
    guild = _Guild(cached=(), server_side=(boss,))  # in the guild, not in cache
    ctx = _Ctx(_author(top_pos=10), guild)

    err = await modchecks.hierarchy_error_resolved(ctx, _User(2))

    assert err is not None and "equal to or above yours" in err
    assert guild.fetch_calls == [2], "one REST resolve, only on the cache miss"


async def test_old_cache_only_check_would_have_allowed_it():
    """Pins the bug this fix closes: the sync guard still reads a miss as None."""
    boss = _Member(2, top_pos=40)
    guild = _Guild(cached=(), server_side=(boss,))
    ctx = _Ctx(_author(top_pos=10), guild)

    assert modchecks.hierarchy_error(ctx, _User(2)) is None
    assert guild.fetch_calls == []  # it never even asked


async def test_resolver_allows_a_target_genuinely_absent_from_the_guild():
    """A real hackban by id must keep working: 404 is a trusted negative."""
    guild = _Guild(cached=(), server_side=())
    ctx = _Ctx(_author(top_pos=10), guild)

    assert await modchecks.hierarchy_error_resolved(ctx, _User(42)) is None
    assert guild.fetch_calls == [42]


async def test_resolver_fails_closed_when_the_lookup_breaks():
    """5xx / timeout leaves the rank UNKNOWN, and unknown must not mean go."""
    guild = _Guild(cached=(), server_side=(), fails=True)
    ctx = _Ctx(_author(top_pos=10), guild)

    err = await modchecks.hierarchy_error_resolved(ctx, _User(42))

    assert err is not None and "couldn't check" in err


async def test_resolver_skips_the_fetch_on_a_cache_hit():
    """Cost guard: a cached target costs nothing extra."""
    junior = _Member(2, top_pos=5)
    guild = _Guild(cached=(junior,), server_side=(junior,))
    ctx = _Ctx(_author(top_pos=10), guild)

    assert await modchecks.hierarchy_error_resolved(ctx, _User(2)) is None
    assert guild.fetch_calls == []


# ---------------------------------------------------------------------------
# ban / kick / tempban
# ---------------------------------------------------------------------------
async def test_ban_refuses_an_uncached_higher_ranked_member(fake_pool):
    boss = _Member(2, top_pos=40)
    guild = _Guild(cached=(), server_side=(boss,))
    ctx = _Ctx(_author(top_pos=10), guild)

    await moderation.Moderation._ban.callback(
        _cog(fake_pool), ctx, _User(2), reason="nope"
    )

    assert guild.bans == [], "the ban must never reach Discord"
    assert fake_pool.calls == [], "and no case may be recorded"
    assert "equal to or above yours" in _last_text(ctx)


async def test_ban_still_works_on_a_real_non_member(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}
    guild = _Guild(cached=(), server_side=())
    ctx = _Ctx(_author(top_pos=10), guild)

    await moderation.Moderation._ban.callback(
        _cog(fake_pool), ctx, _User(42), reason="raider"
    )

    assert guild.bans == [42]


async def test_ban_fails_closed_when_the_target_cannot_be_resolved(fake_pool):
    guild = _Guild(cached=(), server_side=(), fails=True)
    ctx = _Ctx(_author(top_pos=10), guild)

    await moderation.Moderation._ban.callback(
        _cog(fake_pool), ctx, _User(2), reason="nope"
    )

    assert guild.bans == []
    assert "couldn't check" in _last_text(ctx)


async def test_kick_refuses_an_uncached_higher_ranked_member(fake_pool):
    boss = _Member(2, top_pos=40)
    guild = _Guild(cached=(), server_side=(boss,))
    ctx = _Ctx(_author(top_pos=10), guild)

    await moderation.Moderation._kick.callback(
        _cog(fake_pool), ctx, _User(2), reason="nope"
    )

    assert guild.kicks == []
    assert fake_pool.calls == []
    assert "equal to or above yours" in _last_text(ctx)


async def test_kick_still_works_on_an_uncached_junior(fake_pool):
    """Counter-test: the legitimate actor is not slowed down by the guard."""
    fake_pool.fetchrow_return = {"case_number": 1}
    junior = _Member(2, top_pos=5)
    guild = _Guild(cached=(), server_side=(junior,))
    ctx = _Ctx(_author(top_pos=10), guild)

    await moderation.Moderation._kick.callback(
        _cog(fake_pool), ctx, _User(2), reason="spam"
    )

    assert guild.kicks == [2]


async def test_tempban_refuses_an_uncached_higher_ranked_member(fake_pool):
    boss = _Member(2, top_pos=40)
    guild = _Guild(cached=(), server_side=(boss,))
    ctx = _Ctx(_author(top_pos=10), guild)

    await moderation.Moderation.tempban.callback(
        _cog(fake_pool), ctx, _User(2), None, reason="nope"
    )

    assert guild.bans == []
    assert "equal to or above yours" in _last_text(ctx)


# ---------------------------------------------------------------------------
# massban: bulk resolution, still fail-closed, still cheap
# ---------------------------------------------------------------------------
async def _run_massban(cog, ctx, ids, reason="raid"):
    users = [discord.Object(id=i) for i in ids]
    await moderation.Moderation.massban.callback(cog, ctx, users, reason=reason)


def _summary(ctx):
    embed = ctx.confirm_message.edits[-1]["embed"]
    return {f.name: f.value for f in embed.fields}


async def test_massban_skips_an_uncached_higher_ranked_member(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}
    boss = _Member(2, top_pos=40)
    junior = _Member(3, top_pos=5)
    guild = _Guild(cached=(), server_side=(boss, junior))
    ctx = _Ctx(_author(top_pos=10), guild)

    await _run_massban(_cog(fake_pool), ctx, [2, 3, 42])

    # 2 is skipped (outranks the mod), 3 is a junior, 42 is a genuine hackban.
    assert guild.bulk_ban_lots == [[3, 42]]
    assert _summary(ctx)["Skipped"] == "1"


async def test_massban_fails_closed_on_an_unresolvable_lot(fake_pool):
    guild = _Guild(cached=(), server_side=(), fails=True)
    ctx = _Ctx(_author(top_pos=10), guild)

    await _run_massban(_cog(fake_pool), ctx, [2, 3, 42])

    assert guild.bulk_ban_lots == [], "an unverified lot is never banned"
    assert _summary(ctx)["Skipped"] == "3"


async def test_massban_resolves_in_gateway_batches_not_per_target(fake_pool):
    """Cost: 200 uncached ids cost two gateway queries, not 200 REST fetches."""
    fake_pool.fetchrow_return = {"case_number": 1}
    ids = list(range(1000, 1200))  # 200 targets, none cached, none in the guild
    guild = _Guild(cached=(), server_side=())
    ctx = _Ctx(_author(top_pos=10), guild)

    await _run_massban(_cog(fake_pool), ctx, ids)

    assert guild.fetch_calls == [], "the bulk path must not use fetch_member"
    assert len(guild.query_calls) == 2
    assert [len(c) for c in guild.query_calls] == [
        modchecks.MEMBER_QUERY_CHUNK,
        200 - modchecks.MEMBER_QUERY_CHUNK,
    ]
    assert guild.bulk_ban_lots == [ids]


async def test_resolve_guild_members_reports_the_three_outcomes():
    boss = _Member(2, top_pos=40)
    cached = _Member(3, top_pos=5)
    guild = _Guild(cached=(cached,), server_side=(boss, cached))

    found, unresolved = await modchecks.resolve_guild_members(guild, [2, 3, 42, 3])

    assert set(found) == {2, 3}  # 2 came from the gateway, 3 from the cache
    assert unresolved == set()  # 42 is PROVEN absent, not unknown
    assert guild.query_calls == [[2, 42]], "cached ids are not re-queried, dupes drop"


async def test_resolve_guild_members_marks_a_failed_chunk_unresolved():
    guild = _Guild(cached=(), server_side=(), fails=True)

    found, unresolved = await modchecks.resolve_guild_members(guild, [1, 2, 3])

    assert found == {}
    assert unresolved == {1, 2, 3}
