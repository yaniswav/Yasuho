"""V5c-C: /temprole - limits, hierarchy refusals, expiry (no real clock reads).

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import datetime
import types

import discord
import pytest

from cogs.moderation import temprole
from cogs.moderation.temprole import TempRole
from tools import premium

NOW = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
HOUR = datetime.timedelta(hours=1)


class _Perms:
    def __init__(self, value=0):
        self.value = value


class _Role:
    """Comparable by position like discord.Role."""

    def __init__(self, rid, position, *, managed=False, default=False, perms=0):
        self.id = rid
        self.position = position
        self.managed = managed
        self._default = default
        self.permissions = _Perms(perms)
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


def _ctx(author_top=10, bot_top=10, owner=False):
    me = types.SimpleNamespace(top_role=_Role(1, bot_top))
    guild = types.SimpleNamespace(id=5, me=me, owner_id=1 if owner else 999)
    author = types.SimpleNamespace(
        id=1, top_role=_Role(2, author_top),
        guild_permissions=types.SimpleNamespace(administrator=False),
    )
    return types.SimpleNamespace(guild=guild, author=author)


# -- refusals --------------------------------------------------------------
def test_role_error_refuses_everyone_and_managed_roles():
    assert temprole.role_error(_Role(5, 0, default=True))
    assert temprole.role_error(_Role(6, 3, managed=True))
    assert temprole.role_error(_Role(8, 3)) is None


def test_hierarchy_invoker_and_bot_must_both_outrank_the_role():
    from tools import modchecks

    role = _Role(9, 5)
    assert modchecks.role_hierarchy_error(_ctx(author_top=5, bot_top=10), role)  # invoker
    assert modchecks.role_hierarchy_error(_ctx(author_top=10, bot_top=5), role)  # bot
    assert modchecks.role_hierarchy_error(_ctx(author_top=10, bot_top=10), role) is None


def test_duration_bounds():
    assert temprole.duration_error(NOW + datetime.timedelta(seconds=30), NOW)
    assert temprole.duration_error(NOW + datetime.timedelta(days=91), NOW)
    assert temprole.duration_error(NOW + HOUR, NOW) is None


def test_the_limits_are_five_free_and_one_hundred_plus():
    assert premium.GUILD_FREE.max_temp_roles == 5
    assert premium.GUILD_PREMIUM.max_temp_roles == 100


def test_the_event_is_not_the_role_menus_event_nor_durable():
    from cogs.community.reminders import DURABLE_TIMER_EVENTS

    assert temprole.TIMER_EVENT != "temprole"
    assert temprole.TIMER_EVENT not in DURABLE_TIMER_EVENTS  # DELETE-as-claim, at-most-once


# -- expiry ----------------------------------------------------------------
class _Pool:
    def __init__(self, claimed=True, exists=False):
        self.claimed, self.exists = claimed, exists
        self.grants = []

    async def fetchrow(self, query, *args):
        assert query.startswith("DELETE FROM temp_roles")
        return {"?column?": 1} if self.claimed else None

    async def fetchval(self, query, *args):
        return 1 if self.exists else None

    def acquire(self):  # only the retry path reaches it
        raise AssertionError("unexpected transaction")


class _Member:
    def __init__(self, roles, error=None):
        self.roles = roles
        self.removed = []
        self.error = error

    async def remove_roles(self, role, reason=None):
        if self.error:
            raise self.error
        self.removed.append(role)


class _Guild:
    def __init__(self, role, member=None, fetch_error=None):
        self.role, self.member, self.fetch_error = role, member, fetch_error
        self.fetched = 0

    def get_role(self, rid):
        return self.role if self.role and self.role.id == rid else None

    def get_member(self, uid):
        return None  # chunk_guilds_at_startup=False: the cache is cold

    async def fetch_member(self, uid):
        self.fetched += 1
        if self.fetch_error:
            raise self.fetch_error
        return self.member


def _resp(status):
    return types.SimpleNamespace(status=status, reason="x")


def _cog(pool, guild):
    bot = types.SimpleNamespace(
        db_pool=pool, get_guild=lambda gid: guild, get_cog=lambda n: None
    )
    return TempRole(bot)


EXTRA = {"guild_id": 5, "user_id": 7, "role_id": 9}


async def test_expiry_removes_the_role_from_a_cold_cache_member():
    role = _Role(9, 1)
    member = _Member([role])
    cog = _cog(_Pool(), _Guild(role, member))
    await cog._expire(EXTRA)
    assert member.removed == [role]


async def test_member_who_left_is_dropped_silently():
    role = _Role(9, 1)
    guild = _Guild(role, fetch_error=discord.NotFound(_resp(404), "gone"))
    cog = _cog(_Pool(), guild)
    await cog._expire(EXTRA)  # no raise, no retry (the pool would assert)
    assert guild.fetched == 1


async def test_role_deleted_is_dropped_silently():
    guild = _Guild(None)
    await _cog(_Pool(), guild)._expire(EXTRA)
    assert guild.fetched == 0


async def test_an_extended_grant_ignores_the_stale_wakeup():
    role = _Role(9, 1)
    member = _Member([role])
    guild = _Guild(role, member)
    await _cog(_Pool(claimed=False, exists=True), guild)._expire(EXTRA)
    assert member.removed == [] and guild.fetched == 0


async def test_erased_record_still_lets_the_role_come_off():
    """/mydata erasure deletes the row, never the timer: the removal still runs."""
    role = _Role(9, 1)
    member = _Member([role])
    await _cog(_Pool(claimed=False, exists=False), _Guild(role, member))._expire(EXTRA)
    assert member.removed == [role]


async def test_forbidden_is_logged_not_retried():
    role = _Role(9, 1)
    member = _Member([role], error=discord.Forbidden(_resp(403), "no"))
    await _cog(_Pool(), _Guild(role, member))._expire(EXTRA)  # pool.acquire would assert


async def test_bad_extra_is_ignored():
    await _cog(_Pool(), _Guild(None))._expire({"guild_id": "x"})


class _ReqPool(_Pool):
    def __init__(self, claimed=True):
        super().__init__(claimed=claimed)
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))


@pytest.mark.parametrize(
    "error", [discord.HTTPException(_resp(500), "boom"), OSError("net"), asyncio.TimeoutError()]
)
async def test_any_transient_error_requeues_with_a_bounded_attempt(monkeypatch, error):
    role = _Role(9, 1)
    member = _Member([role], error=error)
    calls = []

    async def fake_grant(pool, g, u, r, when, limit, attempt=0):
        calls.append(attempt)

    monkeypatch.setattr(temprole, "grant", fake_grant)
    cog = _cog(_ReqPool(), _Guild(role, member))
    await cog._expire(EXTRA)
    await cog._expire({**EXTRA, "attempt": temprole._MAX_ATTEMPTS - 1})
    assert calls == [1]  # the second one gave up


async def test_retry_after_erasure_writes_only_the_timer(monkeypatch):
    """The record was erased by /mydata: the retry must NOT recreate it."""
    role = _Role(9, 1)
    member = _Member([role], error=discord.HTTPException(_resp(503), "x"))

    async def no_grant(*a, **k):
        raise AssertionError("the row must not be recreated")

    monkeypatch.setattr(temprole, "grant", no_grant)
    pool = _ReqPool(claimed=False)
    await _cog(pool, _Guild(role, member))._expire(EXTRA)
    assert len(pool.executed) == 1 and pool.executed[0][0].startswith("INSERT INTO timers")


async def test_a_db_error_on_the_claim_requeues_the_timer():
    class Broken(_ReqPool):
        async def fetchrow(self, query, *args):
            raise OSError("db down")

    pool = Broken()
    await _cog(pool, _Guild(_Role(9, 1)))._expire(EXTRA)
    assert len(pool.executed) == 1


# -- the command itself ------------------------------------------------------
class _Ctx:
    def __init__(self, base):
        self.guild, self.author = base.guild, base.author
        self.sent = []

    async def defer(self):
        pass

    async def send(self, *args, **kwargs):
        self.sent.append(args[0] if args else kwargs)


class _GrantPool:
    def __init__(self):
        self.grants = 0


def _add_cog(monkeypatch, result=("new", None), pool=None):
    calls = []

    async def fake_grant(*args, **kwargs):
        calls.append(args)
        return result


    monkeypatch.setattr(temprole, "grant", fake_grant)
    bot = types.SimpleNamespace(
        db_pool=pool, get_cog=lambda n: None,
        premium=types.SimpleNamespace(for_guild=lambda gid: premium.GUILD_FREE),
    )
    return TempRole(bot), calls


class _Duration:
    def __init__(self):
        self.dt = discord.utils.utcnow() + HOUR  # relative: no real-date dependence


async def _run_add(cog, ctx, role):
    member = types.SimpleNamespace(id=7, mention="<@7>", roles=[])

    async def add_roles(r, reason=None):
        member.roles.append(r)

    member.add_roles = add_roles
    await cog.temprole_add.callback(cog, ctx, member, role, _Duration(), reason=None)
    return member


async def test_add_refuses_a_dangerous_role_and_grants_nothing(monkeypatch):
    cog, calls = _add_cog(monkeypatch)
    ctx = _Ctx(_ctx())
    admin = _Role(7, 3, perms=discord.Permissions(administrator=True).value)
    member = await _run_add(cog, ctx, admin)
    assert calls == [] and member.roles == [] and ctx.sent


async def test_add_refuses_when_the_invoker_does_not_outrank_the_role(monkeypatch):
    cog, calls = _add_cog(monkeypatch)
    ctx = _Ctx(_ctx(author_top=3))
    member = await _run_add(cog, ctx, _Role(8, 5))
    assert calls == [] and member.roles == []


async def test_add_refuses_when_the_bot_does_not_outrank_the_role(monkeypatch):
    cog, calls = _add_cog(monkeypatch)
    ctx = _Ctx(_ctx(bot_top=3))
    await _run_add(cog, ctx, _Role(8, 5))
    assert calls == []


async def test_add_grants_a_safe_role_the_invoker_outranks(monkeypatch):
    cog, calls = _add_cog(monkeypatch)
    role = _Role(8, 2)
    member = await _run_add(cog, _Ctx(_ctx()), role)
    assert len(calls) == 1 and member.roles == [role]
    assert calls[0][5] == 5  # the free limit travelled to the grant


class _HeldPool:
    def __init__(self, row):
        self.row = row

    async def fetchval(self, query, *args):
        return 1 if self.row else None


async def _run_add_with_roles(cog, ctx, role, roles, add_error=None):
    member = types.SimpleNamespace(id=7, mention="<@7>", roles=list(roles))

    async def add_roles(r, reason=None):
        if add_error:
            raise add_error

    member.add_roles = add_roles
    await cog.temprole_add.callback(cog, ctx, member, role, _Duration(), reason=None)


async def test_a_role_held_without_a_grant_is_refused(monkeypatch):
    role = _Role(8, 2)
    cog, calls = _add_cog(monkeypatch, pool=_HeldPool(False))
    ctx = _Ctx(_ctx())
    await _run_add_with_roles(cog, ctx, role, [role])
    assert calls == [] and ctx.sent


async def test_a_role_held_through_a_grant_may_be_extended(monkeypatch):
    role = _Role(8, 2)
    cog, calls = _add_cog(monkeypatch, result=("extended", NOW), pool=_HeldPool(True))
    await _run_add_with_roles(cog, _Ctx(_ctx()), role, [role])
    assert len(calls) == 1


async def test_a_failed_new_grant_is_revoked(monkeypatch):
    revoked = []

    async def fake_revoke(*a):
        revoked.append(a)

    monkeypatch.setattr(temprole, "revoke", fake_revoke)
    cog, calls = _add_cog(monkeypatch)
    err = discord.HTTPException(_resp(500), "x")
    await _run_add_with_roles(cog, _Ctx(_ctx()), _Role(8, 2), [], add_error=err)
    assert len(revoked) == 1


async def test_a_failed_extension_keeps_the_original_grant(monkeypatch):
    """The role from the first grant must not become permanent."""
    revoked = []

    async def fake_revoke(*a):
        revoked.append(a)

    monkeypatch.setattr(temprole, "revoke", fake_revoke)
    role = _Role(8, 2)
    cog, calls = _add_cog(monkeypatch, result=("extended", NOW), pool=_HeldPool(True))
    err = discord.HTTPException(_resp(500), "x")
    await _run_add_with_roles(cog, _Ctx(_ctx()), role, [role], add_error=err)
    assert revoked == []
    assert len(calls) == 2 and calls[1][4] == NOW  # the old end was put back
