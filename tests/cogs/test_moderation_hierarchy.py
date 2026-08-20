"""Role/member hierarchy guards on the role-management and warn commands.

Covers the privilege-escalation fixes:

* ``addrole``/``removerole``/``moverole`` (gated only by ``manage_roles``) now
  refuse to touch a role the invoker does not outrank, unless the invoker owns
  the guild or is an Administrator, and refuse when the bot itself cannot manage
  the role (or, for ``moverole``, the target position).
* ``warn`` (gated by ``kick_members``) now runs ``modchecks.hierarchy_error``
  before recording anything, like its ban/kick/mute siblings.
* ``move`` (voice) now runs the same check ``voicekick`` runs. It had none, and
  it is a voice kick with extra steps: ``move_to(None)`` disconnects, and an
  unmatched room name resolved to None, so it ejected anyone - the owner
  included - from a command that carried no rank guard at all.

Pure fakes only - no Discord, DB or network.
"""

import re
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
# M1b - move (voice) refuses a higher-ranked target, and refuses to "move"
#       someone to a room that does not exist (which used to disconnect them)
# ---------------------------------------------------------------------------
class _VoiceMember:
    """A move target that records every move_to it is asked to perform."""

    def __init__(self, uid, top_pos):
        self.id = uid
        self.name = f"member-{uid}"
        self.mention = f"<@{uid}>"
        self.top_role = _Role(top_pos, f"member-{uid}")
        self.moves = []

    async def move_to(self, channel, reason=None):
        self.moves.append(channel)


def _voice_guild(owner_id, bot_top_pos, members=(), rooms=()):
    guild = _guild(owner_id, bot_top_pos, members=list(members))
    guild.voice_channels = [
        types.SimpleNamespace(id=i, name=name) for i, name in enumerate(rooms, 1)
    ]
    return guild


async def _run_move(ctx, user, room):
    await moderation.Moderation._move.callback(_mod_cog(), ctx, user, room)


async def test_move_refuses_a_higher_ranked_member():
    author = _author(1, top_pos=10)
    target = _VoiceMember(2, top_pos=20)  # staff, ranked above the moderator
    guild = _voice_guild(100, bot_top_pos=50, members=[target], rooms=["afk"])
    ctx = _Ctx(author, guild)

    await _run_move(ctx, target, "afk")

    assert target.moves == []  # never dragged anywhere
    assert "role is equal to or above yours" in _last_text(ctx)


async def test_move_to_an_unknown_room_never_disconnects_the_target():
    """A typo must not be a voice kick: move_to(None) ejects from voice."""
    author = _author(1, top_pos=10)
    target = _VoiceMember(2, top_pos=5)
    guild = _voice_guild(100, bot_top_pos=50, members=[target], rooms=["General"])
    ctx = _Ctx(author, guild)

    await _run_move(ctx, target, "Genral")  # room that does not exist

    assert target.moves == []
    assert "couldn't find a voice channel" in _last_text(ctx)


async def test_move_refuses_the_guild_owner():
    author = _author(1, top_pos=10)
    owner = _VoiceMember(100, top_pos=5)  # low role, but owns the guild
    guild = _voice_guild(100, bot_top_pos=50, members=[owner], rooms=["afk"])
    ctx = _Ctx(author, guild)

    await _run_move(ctx, owner, "afk")

    assert owner.moves == []
    assert "server owner" in _last_text(ctx)


async def test_move_of_an_eligible_member_still_moves_them():
    """The guard must not break the command it protects."""
    author = _author(1, top_pos=10)
    target = _VoiceMember(2, top_pos=5)
    guild = _voice_guild(100, bot_top_pos=50, members=[target], rooms=["afk"])
    ctx = _Ctx(author, guild)

    await _run_move(ctx, target, "afk")

    assert [c.name for c in target.moves] == ["afk"]
    assert "has been moved to" in _last_text(ctx)


# --- ... and the refusal QUOTES the argument back, publicly ----------------
# `room` is free text the invoker typed and the refusal echoes it into a public
# message. Unescaped that is a kick_members moderator making the bot post
# formatted text, links and pings of their choosing, and a 2000-character room
# pushed the reply past the message limit so the refusal silently failed to send
# - which is a voice kick with an extra step, again.


async def test_the_unknown_room_echo_cannot_ping():
    author = _author(1, top_pos=10)
    target = _VoiceMember(2, top_pos=5)
    guild = _voice_guild(100, bot_top_pos=50, members=[target], rooms=["General"])
    ctx = _Ctx(author, guild)

    await _run_move(ctx, target, "@everyone <@&5>")

    mentions = ctx.sent[-1][1].get("allowed_mentions")
    assert mentions is not None
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False


async def test_the_unknown_room_echo_is_defused_and_bounded():
    author = _author(1, top_pos=10)
    target = _VoiceMember(2, top_pos=5)
    guild = _voice_guild(100, bot_top_pos=50, members=[target], rooms=["General"])

    ctx = _Ctx(author, guild)
    await _run_move(ctx, target, "[click here](https://evil.example)")
    text = _last_text(ctx)
    # An unescaped ``[`` is what opens a masked link; a ``\[`` renders as a
    # plain bracket. Asserting on the raw substring ``](`` would prove nothing.
    assert re.search(r"(?<!\\)\[", text) is None
    assert "evil.example" in text  # quoted as visible text, not published

    ctx = _Ctx(author, guild)
    await _run_move(ctx, target, "x" * 4000)
    assert len(_last_text(ctx)) < 2000


async def test_an_ordinary_room_name_is_still_quoted_readably():
    author = _author(1, top_pos=10)
    target = _VoiceMember(2, top_pos=5)
    guild = _voice_guild(100, bot_top_pos=50, members=[target], rooms=["General"])
    ctx = _Ctx(author, guild)

    await _run_move(ctx, target, "Genral")

    assert "**Genral**" in _last_text(ctx)


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

    async def query_members(self, *, user_ids, limit=100, cache=True):
        """Gateway confirmation that the uncached ids really are non-members.

        massban resolves its bare ids before comparing ranks (a sparse member
        cache cannot tell "absent" from "not seen yet"), so the fake must be able
        to answer. Here every real member is already in ``_members``, so anything
        that reaches this call is genuinely not in the guild: an empty reply is
        the truthful answer, and it keeps the hackban cases below eligible.
        See tests/cogs/test_moderation_uncached_target.py for the case where the
        gateway DOES find someone the cache had never seen.
        """
        return []

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
