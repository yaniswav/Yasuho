"""Role/member hierarchy guards on the role-management and warn commands.

Covers the privilege-escalation fixes:

* ``addrole``/``removerole``/``moverole`` (gated only by ``manage_roles``) now
  refuse to touch a role the invoker does not outrank, unless the invoker owns
  the guild or is an Administrator, and refuse when the bot itself cannot manage
  the role (or, for ``moverole``, the target position).
* ``warn`` (gated by ``kick_members``) now runs ``modchecks.hierarchy_error``
  before recording anything, like its ban/kick/mute siblings.

Pure fakes only - no Discord, DB or network.
"""

import types

from cogs.moderation import moderation
from cogs.moderation.warns import Warns
from tools import modchecks


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Role:
    def __init__(self, position, name="role"):
        self.position = position
        self.name = name
        self.edits = []

    def __ge__(self, other):
        return self.position >= other.position

    async def edit(self, *, position):
        self.edits.append(position)


class _Ctx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _author(uid, top_pos, admin=False):
    return types.SimpleNamespace(
        id=uid,
        top_role=_Role(top_pos, "author-top"),
        guild_permissions=types.SimpleNamespace(administrator=admin),
    )


def _guild(owner_id, bot_top_pos, members=None):
    lookup = {m.id: m for m in (members or [])}
    return types.SimpleNamespace(
        owner_id=owner_id,
        me=types.SimpleNamespace(top_role=_Role(bot_top_pos, "bot-top")),
        get_member=lookup.get,
    )


def _mod_cog():
    return moderation.Moderation(types.SimpleNamespace())


def _last_text(ctx):
    return ctx.sent[-1][0][0]


# ---------------------------------------------------------------------------
# M1 - role_hierarchy_error unit behaviour (owner / admin bypass, bot guard)
# ---------------------------------------------------------------------------
def test_role_guard_rejects_non_admin_below_target():
    author = _author(1, top_pos=5)  # below the role
    guild = _guild(owner_id=100, bot_top_pos=50)
    role = _Role(10, "staff")
    assert modchecks.role_hierarchy_error(_Ctx(author, guild), role) is not None


def test_role_guard_owner_bypasses():
    author = _author(100, top_pos=1)  # low role but owns the guild
    guild = _guild(owner_id=100, bot_top_pos=50)
    role = _Role(10, "staff")
    assert modchecks.role_hierarchy_error(_Ctx(author, guild), role) is None


def test_role_guard_admin_bypasses():
    author = _author(1, top_pos=1, admin=True)  # low role but Administrator
    guild = _guild(owner_id=100, bot_top_pos=50)
    role = _Role(10, "staff")
    assert modchecks.role_hierarchy_error(_Ctx(author, guild), role) is None


def test_role_guard_rejects_when_role_at_or_above_bot():
    # Invoker outranks the role, but the bot does not -> still refused.
    author = _author(1, top_pos=100)
    guild = _guild(owner_id=100, bot_top_pos=40)
    role = _Role(50, "staff")
    assert modchecks.role_hierarchy_error(_Ctx(author, guild), role) is not None


# ---------------------------------------------------------------------------
# M1 - command-level rejection before any mutation
# ---------------------------------------------------------------------------
async def test_addrole_rejects_non_admin_below_target():
    author = _author(1, top_pos=5)
    guild = _guild(owner_id=100, bot_top_pos=50)
    ctx = _Ctx(author, guild)
    role = _Role(10, "staff")

    await moderation.Moderation.addrole.callback(_mod_cog(), ctx, "-all", role)

    # Rejected before the mass-add ever runs.
    assert ctx.sent and "highest role" in _last_text(ctx)


async def test_removerole_rejects_non_admin_below_target():
    author = _author(1, top_pos=5)
    guild = _guild(owner_id=100, bot_top_pos=50)
    ctx = _Ctx(author, guild)
    role = _Role(10, "staff")

    await moderation.Moderation.removerole.callback(_mod_cog(), ctx, "-all", role)

    assert ctx.sent and "highest role" in _last_text(ctx)


async def test_moverole_rejects_non_admin_below_target():
    author = _author(1, top_pos=5)
    guild = _guild(owner_id=100, bot_top_pos=50)
    ctx = _Ctx(author, guild)
    role = _Role(10, "staff")

    await moderation.Moderation.moverole.callback(_mod_cog(), ctx, role, 3)

    assert role.edits == []  # never touched the role
    assert "highest role" in _last_text(ctx)


async def test_moverole_rejects_position_at_or_above_bot():
    # Invoker outranks the role, but the destination position is above the bot.
    author = _author(1, top_pos=100)
    guild = _guild(owner_id=100, bot_top_pos=20)
    ctx = _Ctx(author, guild)
    role = _Role(10, "staff")

    await moderation.Moderation.moverole.callback(_mod_cog(), ctx, role, 25)

    assert role.edits == []
    assert "my highest role" in _last_text(ctx).lower()


async def test_moverole_owner_below_role_still_moves():
    # The owner may move a role above their own, as long as the bot can host the
    # target position - the guard must not block a legitimate move.
    author = _author(100, top_pos=1)  # owns the guild
    guild = _guild(owner_id=100, bot_top_pos=50)
    ctx = _Ctx(author, guild)
    role = _Role(10, "staff")

    await moderation.Moderation.moverole.callback(_mod_cog(), ctx, role, 5)

    assert role.edits == [5]


# ---------------------------------------------------------------------------
# M2 - warn refuses a higher-ranked target before record_warn
# ---------------------------------------------------------------------------
class _WarnBot:
    def __init__(self, pool):
        self.db_pool = pool


async def test_warn_refuses_higher_ranked_member_before_record(fake_pool):
    author = _author(5, top_pos=10)
    target = types.SimpleNamespace(id=2, mention="<@2>", top_role=_Role(20))
    guild = _guild(owner_id=100, bot_top_pos=50, members=[target])
    ctx = _Ctx(author, guild)
    cog = Warns(_WarnBot(fake_pool))

    await Warns.warn.callback(cog, ctx, target, reason="nope")

    # The guard fired before any persistence: record_warn never touched the DB.
    assert fake_pool.calls == []
    assert ctx.sent and "role is equal to or above yours" in _last_text(ctx)


# ---------------------------------------------------------------------------
# M3 - massban filters higher-ranked members out of the bulk_ban lot
#
# bulk_ban is evaluated against the BOT's top role, so without a per-target
# hierarchy guard a Ban-Members moderator could wipe staff ranked above them.
# ---------------------------------------------------------------------------
class _EditMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _MassCtx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.sent = []
        self.confirm_message = _EditMsg()

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _MassGuild:
    def __init__(self, owner_id, bot_top_pos, members=None):
        self.id = 10
        self.owner_id = owner_id
        self.me = types.SimpleNamespace(top_role=_Role(bot_top_pos, "bot-top"))
        self._members = {m.id: m for m in (members or [])}
        self.bulk_ban_lots = []

    def get_member(self, uid):
        return self._members.get(uid)

    async def bulk_ban(self, users, *, reason=None, delete_message_seconds=0):
        ids = [u.id for u in users]
        self.bulk_ban_lots.append(ids)
        return types.SimpleNamespace(
            banned=[types.SimpleNamespace(id=i) for i in ids],
            failed=[],
        )


class _MassBot:
    def __init__(self, pool):
        self.db_pool = pool

    def get_cog(self, name):
        return None


def _mass_author(uid, top_pos):
    return types.SimpleNamespace(
        id=uid,
        top_role=_Role(top_pos, "author-top"),
        display_avatar=types.SimpleNamespace(url="https://example.test/avatar"),
    )


def _target(uid, top_pos=None):
    """A massban target passed by id; a member also carries a top_role."""
    if top_pos is None:
        return types.SimpleNamespace(id=uid)
    return types.SimpleNamespace(id=uid, top_role=_Role(top_pos, f"member-{uid}"))


def _mass_cog(fake_pool):
    cog = moderation.Moderation(_MassBot(fake_pool))

    async def _yes(_ctx, _embed, **_kw):
        return True

    async def _noop(*_a, **_kw):
        return None

    cog._confirm = _yes
    cog._post_modlog = _noop
    return cog


async def _run_massban(cog, ctx, users, reason="raid"):
    await moderation.Moderation.massban.callback(cog, ctx, users, reason=reason)


async def test_massban_skips_member_ranked_above_invoker(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}
    author = _mass_author(1, top_pos=10)
    higher = _target(2, top_pos=20)  # a resolvable member ranked above the mod
    guild = _MassGuild(owner_id=100, bot_top_pos=50, members=[higher])
    ctx = _MassCtx(author, guild)

    await _run_massban(_mass_cog(fake_pool), ctx, [_target(2, top_pos=20)])

    # The higher-ranked staffer was removed from the lot and never banned.
    assert guild.bulk_ban_lots == []
    summary = ctx.confirm_message.edits[-1]["embed"]
    fields = {f.name: f.value for f in summary.fields}
    assert fields["Banned"] == "0"
    assert fields["Skipped"] == "1"


async def test_massban_bans_non_member_hackban(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}
    author = _mass_author(1, top_pos=10)
    guild = _MassGuild(owner_id=100, bot_top_pos=50)  # id 42 not in the guild
    ctx = _MassCtx(author, guild)

    await _run_massban(_mass_cog(fake_pool), ctx, [_target(42)])

    assert guild.bulk_ban_lots == [[42]]
    summary = ctx.confirm_message.edits[-1]["embed"]
    fields = {f.name: f.value for f in summary.fields}
    assert fields["Banned"] == "1"
    assert fields["Skipped"] == "0"


async def test_massban_owner_invoker_bypasses_hierarchy(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}
    author = _mass_author(100, top_pos=1)  # low role but owns the guild
    higher = _target(2, top_pos=20)
    guild = _MassGuild(owner_id=100, bot_top_pos=50, members=[higher])
    ctx = _MassCtx(author, guild)

    await _run_massban(_mass_cog(fake_pool), ctx, [_target(2, top_pos=20)])

    # Owner outranks everyone (bot still above the member), so the ban proceeds.
    assert guild.bulk_ban_lots == [[2]]
    summary = ctx.confirm_message.edits[-1]["embed"]
    fields = {f.name: f.value for f in summary.fields}
    assert fields["Skipped"] == "0"


async def test_massban_mixed_lot_bans_only_eligible(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}
    author = _mass_author(1, top_pos=10)
    higher = _target(2, top_pos=20)  # skipped: outranks the mod
    lower = _target(3, top_pos=5)  # banned: member below the mod, below the bot
    guild = _MassGuild(owner_id=100, bot_top_pos=50, members=[higher, lower])
    ctx = _MassCtx(author, guild)

    await _run_massban(
        cog=_mass_cog(fake_pool),
        ctx=ctx,
        users=[_target(2, top_pos=20), _target(3, top_pos=5), _target(42)],
    )

    # Only the low-ranked member and the non-member hackban make the lot.
    assert guild.bulk_ban_lots == [[3, 42]]
    summary = ctx.confirm_message.edits[-1]["embed"]
    fields = {f.name: f.value for f in summary.fields}
    assert fields["Banned"] == "2"
    assert fields["Skipped"] == "1"
