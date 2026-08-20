"""Configurer-hierarchy guards on the surfaces that hand out a role for you.

Four config commands used to accept ANY role and store it, gated only by a
permission that says nothing about the caller's own position:

* ``/verify setup``      (manage_roles) - one click on a public button grants it.
* ``/autorole set``      (manage_guild) - every joining member receives it.
* ``/levelconfig rewards add`` (manage_guild) - handed out on reaching a level.
* the Twitch panel's Live role select (manage_guild) - handed out on going live.

Each is a self-grant primitive: point it at a role above your own head but below
Yasuho, trigger it (click, rejoin, chat to the level, start a stream), collect.
They now all run ``modchecks.self_assignable_role_error`` - the SAME helper the
reaction-role and button-role publishers use - which asks both halves: the
configurer must outrank the role, and Yasuho must be able to grant it at all.

``/twitch removerole`` is the odd one out: it DELETES a role rather than granting
it, so it runs the role-management guard (``role_hierarchy_error``) instead.

Every test asserts the MUTATION, not just the message: on a refusal nothing is
written and nothing is deleted. Pure fakes - no Discord, no DB.
"""

import types

from cogs.community.leveling.level_rewards import LevelRewards
from cogs.config import settings as settings_cog
from cogs.config import twitch as twitch_cog
from cogs.config import verification


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeRole:
    def __init__(self, role_id=888, position=5, managed=False, default=False):
        self.id = role_id
        self.position = position
        self.mention = f"<@&{role_id}>"
        self.managed = managed
        self._default = default
        self.deleted = []
        self.guild = types.SimpleNamespace(id=100)

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    async def delete(self, reason=None):
        self.deleted.append(reason)


class FakeMember:
    def __init__(self, member_id, top_position, administrator=False):
        self.id = member_id
        self.top_role = FakeRole(1, position=top_position)
        self.guild_permissions = types.SimpleNamespace(administrator=administrator)


class FakeChannel:
    def __init__(self):
        self.id = 555
        self.mention = "#general"
        self.sent = []

    def permissions_for(self, member):
        return types.SimpleNamespace(send_messages=True)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeGuild:
    def __init__(self, guild_id=100, bot_top=10, owner_id=999, roles=()):
        self.id = guild_id
        self.owner_id = owner_id
        self.me = FakeMember(42, top_position=bot_top)
        self.channel = FakeChannel()
        self.roles = list(roles)

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None


class FakeCtx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.channel = guild.channel
        self.interaction = None
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


def _text(ctx):
    return ctx.sends[-1][0][0]


# ---------------------------------------------------------------------------
# 1. /verify setup
# ---------------------------------------------------------------------------
def _verify_cog(fake_pool):
    return verification.Verification(types.SimpleNamespace(db_pool=fake_pool))


async def test_verify_setup_refuses_a_role_at_or_above_the_configurer(fake_pool):
    """The escalating actor: a manage_roles mod aiming above their own head."""
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=10), guild)

    await verification.Verification.verify_setup.callback(
        _verify_cog(fake_pool), ctx, FakeRole(888, position=30)
    )

    assert fake_pool.calls == [], "nothing may be stored"
    assert guild.channel.sent == [], "and no Verify button may be published"
    assert "equal to or above your highest role" in _text(ctx)


async def test_verify_setup_refuses_a_role_above_the_bot(fake_pool):
    guild = FakeGuild(bot_top=10, owner_id=7)
    ctx = FakeCtx(FakeMember(7, top_position=50), guild)

    await verification.Verification.verify_setup.callback(
        _verify_cog(fake_pool), ctx, FakeRole(888, position=20)
    )

    assert fake_pool.calls == []
    assert "isn't above that role" in _text(ctx)


async def test_verify_setup_refuses_a_managed_role(fake_pool):
    """Yasuho can never grant an integration role: refuse before publishing."""
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=40), guild)

    await verification.Verification.verify_setup.callback(
        _verify_cog(fake_pool), ctx, FakeRole(888, position=5, managed=True)
    )

    assert fake_pool.calls == []
    assert "managed by an" in _text(ctx)


async def test_verify_setup_allows_a_role_below_both(fake_pool):
    """Counter-test: the legitimate configurer is served exactly as before."""
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=40), guild)

    await verification.Verification.verify_setup.callback(
        _verify_cog(fake_pool), ctx, FakeRole(888, position=5)
    )

    assert fake_pool.calls, "the verify_role setting was written"
    assert guild.channel.sent, "and the Verify button went out"


# ---------------------------------------------------------------------------
# 2. /autorole set
# ---------------------------------------------------------------------------
class _AutoLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _AutoBot:
    def __init__(self, pool):
        self.db_pool = pool
        self.eager_cache_lock = _AutoLock()
        self.autoroles = {}


async def test_autorole_set_refuses_a_role_at_or_above_the_configurer(fake_pool):
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=10), guild)
    bot = _AutoBot(fake_pool)
    cog = settings_cog.Settings(bot)

    await settings_cog.Settings.autorole_set.callback(
        cog, ctx, FakeRole(888, position=30)
    )

    assert fake_pool.calls == []
    assert bot.autoroles == {}, "the hot-path cache must stay empty too"
    assert "equal to or above your highest role" in _text(ctx)


async def test_autorole_set_refuses_a_managed_role(fake_pool):
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=40), guild)
    bot = _AutoBot(fake_pool)

    await settings_cog.Settings.autorole_set.callback(
        settings_cog.Settings(bot), ctx, FakeRole(888, position=5, managed=True)
    )

    assert fake_pool.calls == []
    assert bot.autoroles == {}
    assert "managed by an" in _text(ctx)


async def test_autorole_set_allows_a_role_below_both(fake_pool):
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=40), guild)
    bot = _AutoBot(fake_pool)

    await settings_cog.Settings.autorole_set.callback(
        settings_cog.Settings(bot), ctx, FakeRole(888, position=5)
    )

    assert fake_pool.calls, "the row was upserted"
    assert bot.autoroles == {100: 888}


async def test_autorole_set_allows_the_owner_any_role_under_the_bot(fake_pool):
    guild = FakeGuild(bot_top=50, owner_id=7)
    ctx = FakeCtx(FakeMember(7, top_position=1), guild)
    bot = _AutoBot(fake_pool)

    await settings_cog.Settings.autorole_set.callback(
        settings_cog.Settings(bot), ctx, FakeRole(888, position=40)
    )

    assert bot.autoroles == {100: 888}


# ---------------------------------------------------------------------------
# 3. /levelconfig rewards add
# ---------------------------------------------------------------------------
def _rewards_cog(fake_pool):
    return LevelRewards(types.SimpleNamespace(db_pool=fake_pool))


async def test_level_reward_add_refuses_a_role_at_or_above_the_configurer(fake_pool):
    """manage_guild must not become "hand me any role below the bot at level 1"."""
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=10), guild)
    role = FakeRole(888, position=30)
    role.guild = types.SimpleNamespace(id=guild.id)

    await _rewards_cog(fake_pool).cmd_add(ctx, 1, role)

    assert fake_pool.calls == [], "no rule may be counted, let alone inserted"
    assert "equal to or above your highest role" in _text(ctx)


async def test_level_reward_add_refuses_a_role_above_the_bot(fake_pool):
    """A rule Yasuho could never apply is refused, not stored broken."""
    guild = FakeGuild(bot_top=10, owner_id=7)
    ctx = FakeCtx(FakeMember(7, top_position=50), guild)
    role = FakeRole(888, position=20)
    role.guild = types.SimpleNamespace(id=guild.id)

    await _rewards_cog(fake_pool).cmd_add(ctx, 1, role)

    assert fake_pool.calls == []
    assert "isn't above that role" in _text(ctx)


async def test_level_reward_add_allows_a_role_below_both(fake_pool):
    guild = FakeGuild(bot_top=50)
    ctx = FakeCtx(FakeMember(7, top_position=40), guild)
    role = FakeRole(888, position=5)
    role.guild = types.SimpleNamespace(id=guild.id)

    async def fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        return 0 if query.lstrip().startswith("SELECT COUNT") else 3

    fake_pool.fetchval = fetchval

    await _rewards_cog(fake_pool).cmd_add(ctx, 3, role)

    assert any("INSERT INTO level_rewards" in c[1] for c in fake_pool.calls)
    assert "embed" in ctx.sends[-1][1]


# ---------------------------------------------------------------------------
# 4. Twitch Live role select (P2)
# ---------------------------------------------------------------------------
class _FakePanel:
    def __init__(self, guild, config, saves):
        self.guild = guild
        self.config = config
        self._saves = saves
        self.rerendered = 0
        self.errors = 0

        async def save(guild_id, cfg):
            self._saves.append((guild_id, dict(cfg)))

        self.cog = types.SimpleNamespace(save=save)

    async def _rerender(self, interaction):
        self.rerendered += 1

    async def _error(self, interaction):
        self.errors += 1


class _RoleSelectUnderTest(twitch_cog.TwitchRoleSelect):
    """The real select's ``callback``, with ``values`` fed by the test.

    ``values`` is a read-only property on discord.ui.RoleSelect, filled from the
    interaction payload, so it is overridden in a SUBCLASS - patching it onto
    ``TwitchRoleSelect`` itself would leak into every other test in the session.
    ``__init__`` is bypassed for the same reason it is elsewhere: building a real
    select needs a live component tree, and only ``callback`` is under test.
    """

    def __init__(self, panel, chosen):
        self.panel = panel
        self._chosen = chosen

    @property
    def values(self):
        return self._chosen


def _role_select(panel, chosen):
    return _RoleSelectUnderTest(panel, chosen)


async def test_twitch_live_role_refuses_a_role_above_the_configurer(make_interaction):
    guild = FakeGuild(bot_top=50)
    saves = []
    panel = _FakePanel(guild, {"role_id": None}, saves)
    interaction = make_interaction()
    interaction.user = FakeMember(7, top_position=10)

    await _role_select(panel, [FakeRole(888, position=30)]).callback(interaction)

    assert saves == [], "the Live role must not be stored"
    assert panel.config["role_id"] is None
    assert panel.rerendered == 0 and panel.errors == 0
    assert "equal to or above your highest role" in interaction.sent[0][0][0]
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_twitch_live_role_allows_a_role_below_both(make_interaction):
    guild = FakeGuild(bot_top=50)
    saves = []
    panel = _FakePanel(guild, {"role_id": None}, saves)
    interaction = make_interaction()
    interaction.user = FakeMember(7, top_position=40)

    await _role_select(panel, [FakeRole(888, position=5)]).callback(interaction)

    assert panel.config["role_id"] == 888
    assert saves and saves[0][0] == 100
    assert panel.rerendered == 1


async def test_twitch_live_role_can_still_be_cleared(make_interaction):
    """Clearing the select (no values) is not a grant and must stay open."""
    guild = FakeGuild(bot_top=50)
    saves = []
    panel = _FakePanel(guild, {"role_id": 888}, saves)
    interaction = make_interaction()
    interaction.user = FakeMember(7, top_position=10)

    await _role_select(panel, []).callback(interaction)

    assert panel.config["role_id"] is None
    assert saves and panel.rerendered == 1


# ---------------------------------------------------------------------------
# 5. /twitch removerole (P2) - a DELETE, so the role-management guard
# ---------------------------------------------------------------------------
def _twitch_cog_with(config, role):
    cog = twitch_cog.Twitch.__new__(twitch_cog.Twitch)
    saves = []

    async def get_config(guild_id):
        return config

    async def save(guild_id, cfg):
        saves.append((guild_id, dict(cfg)))

    cog.get_config = get_config
    cog.save = save
    cog._resolve_role = lambda guild, cfg: role
    cog.saves = saves
    return cog


async def test_twitch_removerole_refuses_a_role_above_the_invoker():
    """manage_guild is not a licence to delete a role above your own head."""
    role = FakeRole(888, position=30)
    guild = FakeGuild(bot_top=50, roles=[role])
    ctx = FakeCtx(FakeMember(7, top_position=10), guild)
    cog = _twitch_cog_with({"role_id": 888}, role)

    await twitch_cog.Twitch.twitch_removerole.callback(cog, ctx)

    assert role.deleted == [], "the role must survive"
    assert cog.saves == [], "and the config must be left alone"
    assert "equal to or above your highest role" in _text(ctx)
    # The refusal covers the DELETE half only. Leaving it at that would strand
    # someone who merely wanted to unlink, so the message must name the escape
    # hatch (clearing the panel's role select, which writes role_id = None and
    # deletes nothing).
    assert "clear the Live role select" in _text(ctx)


async def test_twitch_removerole_refuses_a_role_above_the_bot():
    role = FakeRole(888, position=40)
    guild = FakeGuild(bot_top=10, owner_id=7, roles=[role])
    ctx = FakeCtx(FakeMember(7, top_position=50), guild)
    cog = _twitch_cog_with({"role_id": 888}, role)

    await twitch_cog.Twitch.twitch_removerole.callback(cog, ctx)

    assert role.deleted == []
    assert "isn't above that role" in _text(ctx)


async def test_twitch_removerole_allows_a_role_below_both():
    role = FakeRole(888, position=5)
    guild = FakeGuild(bot_top=50, roles=[role])
    ctx = FakeCtx(FakeMember(7, top_position=40), guild)
    cog = _twitch_cog_with({"role_id": 888}, role)

    await twitch_cog.Twitch.twitch_removerole.callback(cog, ctx)

    assert role.deleted, "the legitimate admin still removes the role"
    assert cog.saves and cog.saves[0][1]["role_id"] is None
