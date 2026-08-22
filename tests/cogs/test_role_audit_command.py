"""``?roleaudit``: prefix-only, owner-only, and it always says something.

The engine is tested in tests/tools/test_role_audit.py. This file pins the
properties of the COMMAND that the engine cannot defend on its own:

* PREFIX-ONLY. The global slash tree stands at 78 of Discord's 100 and the 101st
  registration makes a whole cog fail to load with ``CommandLimitReached``
  (tests/test_command_tree_capacity.py tells that story in full). Turning this
  ops tool into a ``hybrid_command`` would spend a slot on something the owner
  runs by hand, so the test asserts it is NOT an application command.
* OWNER-ONLY AND HIDDEN. It names guilds and roles.
* IT ALWAYS REPLIES. Short reports go inline in a code block, long ones go out
  as an attachment - and either way something is sent, because a command whose
  success looks like saying nothing is exactly the failure this whole feature
  was built to avoid.
* THE REPLY NEVER REACHES THE LOG. It names guilds and roles, so the last test
  runs a FLAGGED estate - a report that really carries both names - and only
  then asserts neither reaches ``caplog``. Against an empty estate that
  assertion could never fail, which is no control at all.

Pure fakes: no Discord, no database, no network.
"""

import inspect

import discord
from discord.ext import commands

from cogs.system import admin as admin_cog
from tools import role_audit as ra


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    def __init__(self):
        self.sends = []

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class EmptyPool:
    async def fetch(self, query):
        return []


class FlaggedPool:
    """One autorole row, so the sweep really produces a named finding.

    A pool that yields NOTHING makes the privacy control below vacuous: the
    report would contain no guild name and no role name, and "the name is not in
    the log" would be true because the name exists nowhere at all.
    """

    async def fetch(self, query):
        if "FROM autorole" in query:
            return [{"guild_id": 1, "role_id": 42}]
        return []


class FakeBot:
    def __init__(self, guilds=(), pool=None):
        self.db_pool = pool or EmptyPool()
        self.guilds = list(guilds)

    async def is_owner(self, user):
        """Nobody is the owner here - the gates are meant to say no."""
        return False


class FakeRole:
    def __init__(self, role_id, name, permissions=0, position=3):
        self.id = role_id
        self.name = name
        self.permissions = discord.Permissions(permissions)
        self.position = position
        self.managed = False

    def is_default(self):
        return False


class FakeGuild:
    def __init__(self, guild_id, name, roles=()):
        self.id = guild_id
        self.name = name
        self.unavailable = False
        self._roles = {r.id: r for r in roles}
        top = FakeRole(5000, "Yasuho", position=10)
        self.roles = list(roles) + [top] if roles else []
        self.me = type("Me", (), {"top_role": top})() if roles else None

    def get_role(self, role_id):
        return self._roles.get(role_id)


class NonOwnerCtx:
    """Just enough context for ``is_owner``'s predicate to answer."""

    def __init__(self):
        self.author = object()
        self.bot = self
        self.cog = None

    async def is_owner(self, user):
        return False


def _command():
    return admin_cog.Admin.__cog_commands__ and next(
        c for c in admin_cog.Admin.__cog_commands__ if c.name == "roleaudit"
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_the_command_exists_and_is_hidden():
    cmd = _command()
    assert isinstance(cmd, commands.Command)
    assert cmd.hidden is True


def test_the_command_is_prefix_only_and_spends_no_slash_slot():
    """A hybrid here would cost one of the 22 remaining global command slots."""
    cmd = _command()
    assert not isinstance(cmd, commands.HybridCommand)
    assert not isinstance(cmd, discord.app_commands.Command)
    assert getattr(cmd, "app_command", None) is None


async def test_a_non_owner_is_refused_by_the_command_and_by_the_cog():
    """Behaviour, not a name: the predicate is INVOKED against a non-owner.

    Matching ``__qualname__`` would pass for any check that merely happens to be
    called something owner-ish, so both gates are actually run here - the check
    on the command itself, and the cog-wide ``cog_check`` behind it.
    """
    cmd = _command()
    assert cmd.checks, "the command carries no check at all"

    ctx = NonOwnerCtx()
    refused = False
    for check in cmd.checks:
        try:
            outcome = check(ctx)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            refused = refused or outcome is False
        except commands.NotOwner:
            refused = True
    assert refused, "a non-owner got past the command's own checks"

    cog = admin_cog.Admin(FakeBot())
    assert await cog.cog_check(NonOwnerCtx()) is False


# ---------------------------------------------------------------------------
# It always replies
# ---------------------------------------------------------------------------
async def test_an_empty_estate_still_gets_a_reply_with_its_coverage_numbers():
    cog = admin_cog.Admin(FakeBot([FakeGuild(1, "Somewhere")]))
    ctx = FakeCtx()
    await _command().callback(cog, ctx)

    assert len(ctx.sends) == 1
    (args, kwargs) = ctx.sends[0]
    body = args[0]
    assert body.startswith("```")
    assert "Swept 1 guild(s)" in body
    assert "Nothing flagged" in body
    assert kwargs["allowed_mentions"].everyone is False


async def test_a_long_report_goes_out_as_an_attachment(monkeypatch):
    """Readable when it is long: the whole sweep stays in one greppable piece."""
    long_text = "\n".join("line {0}".format(i) for i in range(400))
    monkeypatch.setattr(ra, "render_report", lambda result: long_text)

    cog = admin_cog.Admin(FakeBot([FakeGuild(1, "Somewhere")]))
    ctx = FakeCtx()
    await _command().callback(cog, ctx)

    assert len(long_text) > admin_cog.AUDIT_INLINE_LIMIT
    (args, kwargs) = ctx.sends[0]
    assert isinstance(kwargs["file"], discord.File)
    assert kwargs["file"].filename == "role-audit.txt"
    assert "400 line(s)" in args[0]


async def test_the_reply_never_reaches_the_log(caplog):
    """Owner-only output: guild and role names must not land in a log file.

    The fixture is deliberately a FLAGGED estate. With an empty one the report
    carries no names at all, so the absence assertions would hold no matter what
    the command logged - the control has to be run against a report that really
    does name the guild and the role it found.
    """
    guild = FakeGuild(
        1,
        "Secret Server",
        roles=[
            FakeRole(
                42,
                "Secret Role",
                permissions=discord.Permissions(administrator=True).value,
            )
        ],
    )
    cog = admin_cog.Admin(FakeBot([guild], pool=FlaggedPool()))
    ctx = FakeCtx()
    with caplog.at_level(0):
        await _command().callback(cog, ctx)

    body = ctx.sends[0][0][0]
    # The control is only meaningful because both names ARE in the reply.
    assert "Secret Server" in body
    assert "Secret Role" in body
    assert "carries administrator" in body

    assert "Secret Server" not in caplog.text
    assert "Secret Role" not in caplog.text
