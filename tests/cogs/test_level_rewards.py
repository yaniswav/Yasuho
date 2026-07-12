"""Unit tests for cogs.community.level_rewards.LevelRewards.grant_for_levelup.

The pure add/remove decision math is covered in tests/tools/test_level_rewards.py;
these tests drive the cog-level APPLICATION of that decision against fakes:
role hierarchy skips (debug, never breaks the grant), HTTPException on a single
role (swallowed, the rest still apply), and lazy pruning of a rule whose role
was deleted (row removed, INFO logged). All of this is live-Discord-shaped
behaviour that cannot be pure, so it is exercised here against fakes rather
than in tools/test_level_rewards.py.
"""

import types

import discord

from cogs.community.level_rewards import LevelRewards
from tools import level_rewards as lr

# ---------------------------------------------------------------------------
# Fakes: guild / role / member shaped just enough for the hierarchy checks
# (role.is_default(), role.managed, role < guild.me.top_role) and for
# member.add_roles / member.remove_roles to be observable.
# ---------------------------------------------------------------------------


class _FakeRole:
    def __init__(self, role_id, position=1, managed=False, default=False):
        self.id = role_id
        self.position = position
        self.managed = managed
        self._default = default
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __repr__(self):
        return f"_FakeRole({self.id})"


class _FakeGuild:
    def __init__(self, guild_id, roles=(), bot_top_position=100):
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self._roles = {r.id: r for r in roles}
        self.me = types.SimpleNamespace(top_role=_FakeRole(0, position=bot_top_position))

    def get_role(self, role_id):
        return self._roles.get(role_id)


class _FakeMember:
    def __init__(self, member_id, roles=()):
        self.id = member_id
        self.roles = list(roles)
        self.added = []
        self.removed = []
        self._add_raises = {}
        self._remove_raises = {}

    def fail_add(self, role_id, exc):
        self._add_raises[role_id] = exc

    async def add_roles(self, role, reason=None):
        if role.id in self._add_raises:
            raise self._add_raises[role.id]
        self.added.append(role)
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        if role.id in self._remove_raises:
            raise self._remove_raises[role.id]
        self.removed.append(role)
        self.roles = [r for r in self.roles if r.id != role.id]


class _FakeHTTPResponse:
    status = 429
    reason = "Too Many Requests"


def _http_exc(message="boom"):
    return discord.HTTPException(_FakeHTTPResponse(), message)


def _make_bot(fake_pool):
    return types.SimpleNamespace(db_pool=fake_pool)


def _rows(*pairs):
    """(level, role_id) pairs -> asyncpg-Record-shaped dicts."""
    return [{"level": lvl, "role_id": rid} for lvl, rid in pairs]


# ---------------------------------------------------------------------------
# No rules configured: zero-cost, no mode lookup
# ---------------------------------------------------------------------------


async def test_no_rules_returns_empty_and_skips_the_mode_query(fake_pool):
    cog = LevelRewards(_make_bot(fake_pool))
    guild = _FakeGuild(1)
    member = _FakeMember(2)

    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == []
    assert member.added == []
    # Only the rules fetch ran - the mode lookup is skipped when there is
    # nothing to grant (SCALE STORY: the common "no rewards configured" guild
    # costs exactly one tiny query per level-up, not two).
    assert [c[0] for c in fake_pool.calls] == ["fetch"]


# ---------------------------------------------------------------------------
# Stack mode: grants, never removes
# ---------------------------------------------------------------------------


async def test_stack_mode_grants_the_new_role(fake_pool):
    role20 = _FakeRole(20, position=5)
    guild = _FakeGuild(1, roles=[role20], bot_top_position=100)
    member = _FakeMember(2)

    fake_pool.fetch_return = _rows((5, 20))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == [role20]
    assert member.added == [role20]


async def test_stack_mode_catch_up_grants_a_stale_lower_rule(fake_pool):
    """A rule added for a level the member already passed is granted on their
    next level-up (no retro sweep needed - see tools/level_rewards.py)."""
    role10 = _FakeRole(10, position=5)
    guild = _FakeGuild(1, roles=[role10])
    member = _FakeMember(2)  # never held role10

    fake_pool.fetch_return = _rows((2, 10))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 8, 9)

    assert granted == [role10]


# ---------------------------------------------------------------------------
# Replace mode: swaps tiers
# ---------------------------------------------------------------------------


async def test_replace_mode_adds_new_tier_and_removes_the_old_one(fake_pool):
    old_role = _FakeRole(10, position=3)
    new_role = _FakeRole(20, position=5)
    guild = _FakeGuild(1, roles=[old_role, new_role])
    member = _FakeMember(2, roles=[old_role])

    fake_pool.fetch_return = _rows((1, 10), (5, 20))
    fake_pool.fetchval_return = "replace"

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == [new_role]
    assert member.added == [new_role]
    assert member.removed == [old_role]


# ---------------------------------------------------------------------------
# Hierarchy: a role the bot cannot manage is skipped, never breaks the grant
# ---------------------------------------------------------------------------


async def test_role_above_bot_top_role_is_skipped_not_granted(fake_pool):
    unreachable = _FakeRole(20, position=999)  # above bot_top_position=100
    guild = _FakeGuild(1, roles=[unreachable], bot_top_position=100)
    member = _FakeMember(2)

    fake_pool.fetch_return = _rows((5, 20))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == []
    assert member.added == []


async def test_managed_role_is_skipped(fake_pool):
    managed = _FakeRole(20, position=5, managed=True)
    guild = _FakeGuild(1, roles=[managed])
    member = _FakeMember(2)

    fake_pool.fetch_return = _rows((5, 20))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == []


# ---------------------------------------------------------------------------
# A single failed role never breaks the rest of the grant
# ---------------------------------------------------------------------------


async def test_one_failed_add_does_not_block_the_others(fake_pool):
    ok_role = _FakeRole(20, position=5)
    bad_role = _FakeRole(21, position=5)
    guild = _FakeGuild(1, roles=[ok_role, bad_role])
    member = _FakeMember(2)
    member.fail_add(21, _http_exc())

    fake_pool.fetch_return = _rows((5, 20), (5, 21))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == [ok_role]


# ---------------------------------------------------------------------------
# Lazy pruning: a rule pointing at a deleted role is dropped, INFO logged
# ---------------------------------------------------------------------------


async def test_deleted_role_prunes_its_rule_row(fake_pool, caplog):
    # role_id 99 has a rule but no longer exists in the guild.
    guild = _FakeGuild(1, roles=[])
    member = _FakeMember(2)

    fake_pool.fetch_return = _rows((5, 99))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    with caplog.at_level("INFO"):
        granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == []
    deletes = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(deletes) == 1
    _method, query, args = deletes[0]
    assert "DELETE FROM level_rewards" in query
    assert args[0] == 1
    assert list(args[1]) == [99]
    assert any("Pruned level_rewards" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The grant call never raises, even if the DB blows up
# ---------------------------------------------------------------------------


async def test_db_failure_is_swallowed_and_returns_empty(fake_pool):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    fake_pool.fetch = boom
    guild = _FakeGuild(1)
    member = _FakeMember(2)

    cog = LevelRewards(_make_bot(fake_pool))
    granted = await cog.grant_for_levelup(guild, member, 4, 5)

    assert granted == []


# ---------------------------------------------------------------------------
# The `/levelrewards mode` command must not silently disable leveling for a
# guild that enabled it only through the LEGACY guild_settings JSONB (no
# level_config row yet): a fresh level_config row would default enabled=FALSE
# and mask that flag on the next restart. The upsert seeds `enabled` from the
# legacy flag on INSERT and never touches it on UPDATE.
# ---------------------------------------------------------------------------


class _ModeCtx:
    def __init__(self, guild_id=1):
        self.sends = []
        self.guild = types.SimpleNamespace(id=guild_id)

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


async def test_mode_upsert_seeds_enabled_from_legacy_jsonb(fake_pool):
    cog = LevelRewards(_make_bot(fake_pool))
    ctx = _ModeCtx(guild_id=7)

    await cog.levelrewards_mode.callback(cog, ctx, "replace")

    upserts = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(upserts) == 1
    _method, query, args = upserts[0]
    # The INSERT seeds enabled from the legacy flag (never a bare default FALSE
    # that would clobber a legacy-on guild), and only rewards_mode on conflict.
    assert "guild_settings" in query
    assert "leveling_enabled" in query
    assert "COALESCE" in query
    assert "DO UPDATE SET rewards_mode" in query
    assert "enabled = " not in query  # UPDATE must never write enabled
    assert args == (7, "replace")


# ---------------------------------------------------------------------------
# The `/levelrewards add` cap is enforced RACE-SAFELY inside the INSERT (a WHERE
# COUNT guard), not just by the advisory pre-check, and a null insert is
# disambiguated into "already a reward" vs "at the maximum".
# ---------------------------------------------------------------------------


def _route_add(fake_pool, count=0, inserted=5, exists=None):
    async def fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "INSERT INTO level_rewards" in query:
            return inserted
        if query.lstrip().startswith("SELECT COUNT"):
            return count
        if query.lstrip().startswith("SELECT 1"):
            return exists
        return None

    fake_pool.fetchval = fetchval


async def test_add_insert_carries_the_atomic_cap_guard(fake_pool):
    role = _FakeRole(20, position=5)
    role.guild = types.SimpleNamespace(id=1)
    guild = _FakeGuild(1, roles=[role], bot_top_position=100)
    ctx = _ModeCtx(guild_id=1)
    ctx.guild = guild
    _route_add(fake_pool, count=0, inserted=5)

    cog = LevelRewards(_make_bot(fake_pool))
    await cog.levelrewards_add.callback(cog, ctx, 5, role)

    inserts = [c for c in fake_pool.calls if "INSERT INTO level_rewards" in c[1]]
    assert len(inserts) == 1
    _method, query, args = inserts[0]
    # The cap lives in the statement, so a concurrent add cannot exceed it.
    assert "WHERE (SELECT COUNT(*) FROM level_rewards WHERE guild_id = $1) < $4" in (
        " ".join(query.split())
    )
    assert args[3] == lr.MAX_REWARDS_PER_GUILD


async def test_add_null_insert_with_existing_rule_reports_duplicate(fake_pool):
    role = _FakeRole(20, position=5)
    role.guild = types.SimpleNamespace(id=1)
    guild = _FakeGuild(1, roles=[role], bot_top_position=100)
    ctx = _ModeCtx(guild_id=1)
    ctx.guild = guild
    _route_add(fake_pool, count=1, inserted=None, exists=1)  # row already present

    cog = LevelRewards(_make_bot(fake_pool))
    await cog.levelrewards_add.callback(cog, ctx, 5, role)

    assert any("already a level" in c[0][0] for c in ctx.sends)


async def test_add_null_insert_from_a_lost_cap_race_reports_maximum(fake_pool):
    role = _FakeRole(20, position=5)
    role.guild = types.SimpleNamespace(id=1)
    guild = _FakeGuild(1, roles=[role], bot_top_position=100)
    ctx = _ModeCtx(guild_id=1)
    ctx.guild = guild
    # Pre-check saw room (count under cap) but the atomic INSERT still added
    # nothing and the row does not exist -> a concurrent add filled the last slot.
    _route_add(fake_pool, count=0, inserted=None, exists=None)

    cog = LevelRewards(_make_bot(fake_pool))
    await cog.levelrewards_add.callback(cog, ctx, 5, role)

    assert any("maximum" in c[0][0] for c in ctx.sends)


# ---------------------------------------------------------------------------
# reconcile_for_level (leveling L5): the cog-level application of the level-DOWN
# decision, used by an admin XP edit that dropped a member below a tier.
# ---------------------------------------------------------------------------


async def test_reconcile_for_level_stack_keeps_every_role(fake_pool):
    """Stack mode is a no-op: earned roles are kept even when XP is removed."""
    low = _FakeRole(20, position=5)
    high = _FakeRole(30, position=6)
    guild = _FakeGuild(1, roles=[low, high])
    member = _FakeMember(2, roles=[low, high])
    fake_pool.fetch_return = _rows((5, 20), (10, 30))
    fake_pool.fetchval_return = "stack"

    cog = LevelRewards(_make_bot(fake_pool))
    added, removed = await cog.reconcile_for_level(guild, member, 5)

    assert added == []
    assert removed == []
    assert member.removed == []  # nothing stripped in stack mode


async def test_reconcile_for_level_replace_strips_the_higher_tier(fake_pool):
    low = _FakeRole(20, position=5)
    high = _FakeRole(30, position=6)
    guild = _FakeGuild(1, roles=[low, high])
    member = _FakeMember(2, roles=[low, high])
    fake_pool.fetch_return = _rows((5, 20), (10, 30))
    fake_pool.fetchval_return = "replace"

    cog = LevelRewards(_make_bot(fake_pool))
    # dropped from level 10 down to level 5: the level-10 role is recomputed away.
    added, removed = await cog.reconcile_for_level(guild, member, 5)

    assert added == []
    assert removed == [high]
    assert member.removed == [high]


async def test_reconcile_for_level_no_rules_is_empty_and_skips_mode(fake_pool):
    guild = _FakeGuild(1)
    member = _FakeMember(2)
    fake_pool.fetch_return = []

    cog = LevelRewards(_make_bot(fake_pool))
    added, removed = await cog.reconcile_for_level(guild, member, 5)

    assert added == []
    assert removed == []
    # Only the rules fetch ran - no mode lookup when there is nothing to do.
    assert [c[0] for c in fake_pool.calls] == ["fetch"]


async def test_reconcile_for_level_db_failure_is_swallowed(fake_pool):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    fake_pool.fetch = boom
    cog = LevelRewards(_make_bot(fake_pool))

    added, removed = await cog.reconcile_for_level(_FakeGuild(1), _FakeMember(2), 5)

    assert added == [] and removed == []
