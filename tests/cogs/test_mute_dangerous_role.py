"""``?mute`` must refuse to apply a configured mute role that carries a
dangerous permission - defence in depth for the one surface
(``muterole.role_id``) a dashboard write can point at an arbitrary role with
no Discord-side guard at all (see cogs/system/dashboard_actions.py).

The moderator is the one human waiting on this apply site, so this is also
the one site that answers them in the command reply rather than just logging.
"""

import types

import discord

from cogs.moderation import moderation


class _Role:
    def __init__(self, role_id, position=1, permissions=None):
        self.id = role_id
        self.position = position
        self.permissions = permissions or discord.Permissions.none()

    def __ge__(self, other):
        return self.position >= other.position


def _author(uid=1, top_pos=50):
    return types.SimpleNamespace(
        id=uid,
        top_role=_Role(0, top_pos),
        guild_permissions=types.SimpleNamespace(administrator=False),
    )


def _target_member(uid=2, top_pos=5):
    return types.SimpleNamespace(id=uid, top_role=_Role(0, top_pos), roles=[])


class _Pool:
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def fetchval(self, *args, **kwargs):  # pragma: no cover - cache primed
        raise AssertionError("muteroles cache should already be primed")


class _Guild:
    def __init__(self, guild_id, owner_id, bot_top_pos, roles, member):
        self.id = guild_id
        self.owner_id = owner_id
        self.me = types.SimpleNamespace(top_role=_Role(0, bot_top_pos))
        self.roles = roles
        self._member = member

    def get_member(self, uid):
        return self._member if uid == self._member.id else None


class _Ctx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _User:
    """Stands in for the ``discord.Member`` the converter would hand mute()."""

    def __init__(self, uid, top_pos):
        self.id = uid
        self.top_role = _Role(0, top_pos)
        self.roles = []
        self.added = []

    async def add_roles(self, role, reason=None):
        self.added.append((role, reason))


def _cog(pool):
    bot = types.SimpleNamespace(db_pool=pool, muteroles={})
    return moderation.Moderation(bot)


async def test_mute_refuses_a_dangerous_configured_role():
    role = _Role(99, position=10, permissions=discord.Permissions(manage_guild=True))
    guild = _Guild(1, owner_id=999, bot_top_pos=50, roles=[role], member=None)
    guild._member = None
    author = _author()
    user = _User(2, top_pos=5)
    guild.get_member = lambda uid: user if uid == user.id else None

    pool = _Pool()
    cog = _cog(pool)
    cog.bot.muteroles[1] = 99
    ctx = _Ctx(author, guild)

    await cog.mute.callback(cog, ctx, user, reason="test")

    assert user.added == []
    assert pool.executed == []  # no mutedmembers row for a refused apply
    assert ctx.sent, "the moderator must be told why nothing happened"
    text = str(ctx.sent[-1])
    assert "dangerous" in text.lower()


async def test_mute_still_applies_a_harmless_configured_role():
    role = _Role(99, position=10, permissions=discord.Permissions.none())
    author = _author()
    user = _User(2, top_pos=5)
    guild = _Guild(1, owner_id=999, bot_top_pos=50, roles=[role], member=None)
    guild.get_member = lambda uid: user if uid == user.id else None

    pool = _Pool()
    cog = _cog(pool)
    cog.bot.muteroles[1] = 99
    ctx = _Ctx(author, guild)

    await cog.mute.callback(cog, ctx, user, reason="test")

    assert [applied for applied, _reason in user.added] == [role]
    assert len(pool.executed) == 1


async def test_mute_still_applies_the_bot_created_silent_mute_role():
    """Non-regression: the role Yasuho creates herself (0 permissions) is unaffected."""
    role = _Role(99, position=10, permissions=discord.Permissions(0))
    author = _author()
    user = _User(2, top_pos=5)
    guild = _Guild(1, owner_id=999, bot_top_pos=50, roles=[role], member=None)
    guild.get_member = lambda uid: user if uid == user.id else None

    pool = _Pool()
    cog = _cog(pool)
    cog.bot.muteroles[1] = 99
    ctx = _Ctx(author, guild)

    await cog.mute.callback(cog, ctx, user, reason="test")

    assert [applied for applied, _reason in user.added] == [role]
