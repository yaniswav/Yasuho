"""The reaction-role upsert may only ever touch a row owned by the CALLER guild.

``reaction_roles`` is keyed on (message_id, emoji) with ``guild_id`` as a plain
column, so an unqualified ``DO UPDATE`` repoints whatever row that key finds -
including one belonging to a completely different server. Message ids are not
secret (``reactionrole list`` prints them, links carry them, snowflakes are
ordered), so a moderator with manage_roles on THEIR OWN guild could aim at a
known id and silently re-point a live mapping in someone else's, or, via the
cache, break it until the next restart.

Two properties are asserted here, and they are different:

1. STATEMENT SHAPE - the SQL actually carries guild scoping (the conflict clause
   compares ``reaction_roles.guild_id`` to ``EXCLUDED.guild_id``) and reports
   back whether it wrote, so a text-level regression is caught.
2. MUTATION - a write issued from guild B against guild A's row changes NOTHING:
   not the stored role, not the owning guild, and not the in-memory cache that
   ``on_raw_reaction_add`` actually reads.

Property 2 runs against a tiny in-memory table that implements the ON CONFLICT
semantics itself, so the test fails if the guard is removed from the statement.
The same statement was probed for real against Postgres inside a ROLLBACK.
"""

import types

from cogs.config.reactionroles import ReactionRoles
from cogs.system import dashboard_actions

MSG = 777
EMOJI = "\U0001F3AE"
GUILD_A = 100
GUILD_B = 200
ROLE_A = 888
ROLE_B = 999


# ---------------------------------------------------------------------------
# A pool whose reaction_roles table honours a guild-scoped ON CONFLICT
# ---------------------------------------------------------------------------
class ScopedPool:
    """Enough of asyncpg to execute the reaction-role upsert for real.

    ``rows`` maps (message_id, emoji) -> {"role_id", "guild_id"}. The conflict
    branch is applied ONLY when the statement text carries the guild guard, so
    dropping that guard makes the cross-tenant assertions fail rather than pass.
    """

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.calls = []

    def _guild_scoped(self, query):
        flat = " ".join(query.split())
        return "reaction_roles.guild_id = EXCLUDED.guild_id" in flat

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "INSERT INTO reaction_roles" not in query:
            return None
        message_id, emoji, role_id, guild_id = args
        key = (message_id, emoji)
        existing = self.rows.get(key)
        if existing is None:
            self.rows[key] = {"role_id": role_id, "guild_id": guild_id}
            return role_id
        # ON CONFLICT DO UPDATE ... WHERE reaction_roles.guild_id = EXCLUDED.guild_id
        if self._guild_scoped(query) and existing["guild_id"] != guild_id:
            return None  # the DO UPDATE was skipped: no row, nothing written
        existing["role_id"] = role_id
        return role_id

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "INSERT 0 1"


def _owned_by_a():
    return ScopedPool({(MSG, EMOJI): {"role_id": ROLE_A, "guild_id": GUILD_A}})


# ---------------------------------------------------------------------------
# Fakes: a role the guard lets through, and the guild/ctx around it
# ---------------------------------------------------------------------------
class FakeRole:
    def __init__(self, role_id, position=5, managed=False, default=False):
        self.id = role_id
        self.position = position
        self.mention = f"<@&{role_id}>"
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class FakeMember:
    def __init__(self, member_id, top_position):
        self.id = member_id
        self.top_role = FakeRole(1, position=top_position)
        self.guild_permissions = types.SimpleNamespace(administrator=False)


class FakeMessage:
    def __init__(self):
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class FakeChannel:
    def __init__(self):
        self.id = 555
        self.message = FakeMessage()

    async def fetch_message(self, mid):
        return self.message


class FakeGuild:
    def __init__(self, guild_id, bot_top=10):
        self.id = guild_id
        self.owner_id = 999
        self.me = FakeMember(42, top_position=bot_top)
        self.channel = FakeChannel()

    def get_channel_or_thread(self, channel_id):
        return self.channel


class _Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeContext:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.channel = guild.channel
        self.interaction = None
        self.sends = []

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


def _cog(pool):
    return ReactionRoles(types.SimpleNamespace(db_pool=pool))


# ---------------------------------------------------------------------------
# 1. Statement shape
# ---------------------------------------------------------------------------
async def test_cog_upsert_carries_guild_scoping_and_reports_back():
    pool = ScopedPool()
    cog = _cog(pool)
    guild = FakeGuild(GUILD_A)

    await cog._persist_reaction_role(guild, guild.channel, MSG, EMOJI, FakeRole(ROLE_A))

    method, query, args = pool.calls[0]
    flat = " ".join(query.split())
    assert method == "fetchval", "the write must report whether it landed"
    assert "ON CONFLICT (message_id, emoji) DO UPDATE" in flat
    assert "WHERE reaction_roles.guild_id = EXCLUDED.guild_id" in flat
    assert "RETURNING role_id" in flat
    assert args == (MSG, EMOJI, ROLE_A, GUILD_A)


async def test_dashboard_upsert_carries_guild_scoping():
    """The dashboard executor runs the same statement and must be scoped too."""
    pool = ScopedPool()
    guild = FakeGuild(GUILD_A)
    role = FakeRole(ROLE_A)
    bot = types.SimpleNamespace(
        db_pool=pool,
        get_guild=lambda gid: guild,
        get_cog=lambda name: None,
    )
    guild.get_role = lambda rid: role
    guild.get_channel_or_thread = lambda cid: guild.channel

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        GUILD_A,
        {
            "channel_id": "555",
            "message_id": str(MSG),
            "role_id": str(ROLE_A),
            "emoji": EMOJI,
        },
    )

    assert result["ok"] is True
    flat = " ".join(pool.calls[0][1].split())
    assert "WHERE reaction_roles.guild_id = EXCLUDED.guild_id" in flat
    assert "RETURNING role_id" in flat


# ---------------------------------------------------------------------------
# 2. Mutation: guild B cannot touch guild A's row
# ---------------------------------------------------------------------------
async def test_guild_b_upsert_leaves_guild_a_row_untouched():
    pool = _owned_by_a()
    cog = _cog(pool)
    guild_b = FakeGuild(GUILD_B)

    embed = await cog._persist_reaction_role(
        guild_b, guild_b.channel, MSG, EMOJI, FakeRole(ROLE_B)
    )

    assert embed is None, "the refused write must not be confirmed to the caller"
    assert pool.rows[(MSG, EMOJI)] == {"role_id": ROLE_A, "guild_id": GUILD_A}
    # The cache is what on_raw_reaction_add reads, and its key carries no guild:
    # poisoning it would break guild A live even though its row survived.
    assert cog.cache == {}


async def test_guild_b_command_path_refuses_and_tells_the_mod():
    pool = _owned_by_a()
    cog = _cog(pool)
    guild_b = FakeGuild(GUILD_B)
    mod = FakeMember(7, top_position=9)
    ctx = FakeContext(mod, guild_b)

    await cog.reactionrole_add(cog, ctx, str(MSG), EMOJI, FakeRole(ROLE_B))

    assert pool.rows[(MSG, EMOJI)]["role_id"] == ROLE_A
    assert cog.cache == {}
    assert "another server" in ctx.sends[-1][0][0]
    # No confirmation embed was ever sent.
    assert all("embed" not in kwargs for _args, kwargs in ctx.sends)


async def test_dashboard_executor_refuses_a_message_claimed_by_another_guild():
    pool = _owned_by_a()
    guild_b = FakeGuild(GUILD_B)
    role = FakeRole(ROLE_B)
    cog = _cog(pool)
    bot = types.SimpleNamespace(
        db_pool=pool,
        get_guild=lambda gid: guild_b,
        get_cog=lambda name: cog,
    )
    guild_b.get_role = lambda rid: role

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        GUILD_B,
        {
            "channel_id": "555",
            "message_id": str(MSG),
            "role_id": str(ROLE_B),
            "emoji": EMOJI,
        },
    )

    assert result == {"ok": False, "error": "message_claimed_elsewhere"}
    assert pool.rows[(MSG, EMOJI)] == {"role_id": ROLE_A, "guild_id": GUILD_A}
    assert cog.cache == {}


# ---------------------------------------------------------------------------
# 3. Counter-tests: the legitimate owner is still fully served
# ---------------------------------------------------------------------------
async def test_guild_a_repoints_its_own_mapping():
    pool = _owned_by_a()
    cog = _cog(pool)
    guild_a = FakeGuild(GUILD_A)

    embed = await cog._persist_reaction_role(
        guild_a, guild_a.channel, MSG, EMOJI, FakeRole(ROLE_B)
    )

    assert embed is not None
    assert pool.rows[(MSG, EMOJI)] == {"role_id": ROLE_B, "guild_id": GUILD_A}
    assert cog.cache[(MSG, EMOJI)] == ROLE_B


async def test_guild_b_maps_a_message_of_its_own():
    """B is only blocked on A's row, never on its own new mapping."""
    pool = _owned_by_a()
    cog = _cog(pool)
    guild_b = FakeGuild(GUILD_B)

    embed = await cog._persist_reaction_role(
        guild_b, guild_b.channel, MSG + 1, EMOJI, FakeRole(ROLE_B)
    )

    assert embed is not None
    assert pool.rows[(MSG + 1, EMOJI)] == {"role_id": ROLE_B, "guild_id": GUILD_B}
    assert pool.rows[(MSG, EMOJI)] == {"role_id": ROLE_A, "guild_id": GUILD_A}
    assert cog.cache[(MSG + 1, EMOJI)] == ROLE_B
