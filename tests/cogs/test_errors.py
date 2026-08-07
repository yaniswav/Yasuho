"""Tests for the global error handler's embed builder (cogs/system/errors.py).

Regression guard: a long error/usage string must not exceed Discord's field
limits (256 for the name, 1024 for the value), which would 400 the whole error
report and hide the underlying error.
"""

import types

import discord
import pytest
from discord.ext import commands

from cogs.system import errors


def _ctx():
    """Minimal fake Context: _error_embed only reads message.created_at and author."""
    return types.SimpleNamespace(
        message=types.SimpleNamespace(created_at=discord.utils.utcnow()),
        author=types.SimpleNamespace(
            display_avatar=types.SimpleNamespace(url="https://example.com/a.png")
        ),
    )


def test_error_embed_caps_long_field_value():
    embed = errors._error_embed(_ctx(), "Name", "x" * 2000)
    assert len(embed.fields[0].value) <= 1024


def test_error_embed_caps_long_field_name():
    embed = errors._error_embed(_ctx(), "N" * 500, "value")
    assert len(embed.fields[0].name) <= 256


def test_error_embed_keeps_short_content_intact():
    embed = errors._error_embed(_ctx(), "Oops", "short value")
    assert embed.fields[0].name == "Oops"
    assert embed.fields[0].value == "short value"


async def test_command_invoke_error_hides_internal_detail(monkeypatch, caplog):
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
    )
    cog = errors.Errors(bot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    command = types.SimpleNamespace(
        qualified_name="explode",
        cog_name="Test",
        signature="",
    )
    command.__str__ = lambda self: "explode"
    ctx = types.SimpleNamespace(
        command=command,
        cog=None,
        author=types.SimpleNamespace(
            id=42,
            display_avatar=types.SimpleNamespace(
                url="https://example.com/a.png"
            ),
        ),
        guild=types.SimpleNamespace(id=7),
        message=types.SimpleNamespace(created_at=discord.utils.utcnow()),
        prefix="!",
        me=types.SimpleNamespace(mention="<@1>"),
        bot=bot,
        send=send,
    )
    monkeypatch.setattr(errors.secrets, "token_hex", lambda _size: "cafebabe")

    await cog._on_command_error(
        ctx,
        commands.CommandInvokeError(RuntimeError("database-password-leak")),
    )

    value = sent[0][1]["embed"].fields[0].value
    assert "cafebabe" in value
    assert "database-password-leak" not in value
    assert "database-password-leak" in caplog.text


def _handler_ctx(sent, *, command=None, bot=None):
    """Build (cog, ctx) around a capturing send.

    ``sent`` collects (args, kwargs) of every ctx.send call so a test can assert
    what reached the user. ``bot`` lets a caller supply get_cog/commands for the
    CommandNotFound path; otherwise a minimal bot is used.
    """
    if bot is None:
        bot = types.SimpleNamespace(
            user=types.SimpleNamespace(name="Yasuho"),
            on_command_error=None,
        )
    cog = errors.Errors(bot)

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    if command is None:
        command = types.SimpleNamespace(
            qualified_name="explode", cog_name="Test", signature=""
        )
        command.__str__ = lambda self: "explode"

    ctx = types.SimpleNamespace(
        command=command,
        cog=None,
        author=types.SimpleNamespace(
            id=42,
            display_avatar=types.SimpleNamespace(url="https://example.com/a.png"),
        ),
        guild=types.SimpleNamespace(id=7),
        message=types.SimpleNamespace(created_at=discord.utils.utcnow()),
        prefix="!",
        me=types.SimpleNamespace(mention="<@1>"),
        bot=bot,
        send=send,
        invoked_with="xyz",
    )
    return cog, ctx


async def test_hybrid_slash_crash_takes_the_invoke_branch(monkeypatch, caplog):
    """A runtime crash inside a hybrid command invoked as a slash reaches this
    handler wrapped HybridCommandError -> app CommandInvokeError -> the real
    error. It must be unwrapped so slash and prefix share the observability:
    a logged traceback plus a user-facing error_id, and no internal detail.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)
    monkeypatch.setattr(errors.secrets, "token_hex", lambda _size: "cafebabe")

    app_error = discord.app_commands.CommandInvokeError(
        types.SimpleNamespace(name="explode"),
        ValueError("database-password-leak"),
    )
    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

    value = sent[0][1]["embed"].fields[0].value
    assert "cafebabe" in value
    assert "database-password-leak" not in value
    assert "database-password-leak" in caplog.text


async def test_hybrid_app_check_failure_stays_discreet(caplog):
    """An @app_commands.check refusal reaches this handler as
    HybridCommandError -> app CheckFailure. It is a deliberate refusal, so it
    must take the discreet CheckFailure branch: no ERROR log, and never the
    alarming "report this to the bot owner" text.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)

    with caplog.at_level("ERROR"):
        await cog._on_command_error(
            ctx,
            commands.HybridCommandError(
                discord.app_commands.CheckFailure("nope")
            ),
        )

    assert caplog.text == ""
    assert len(sent) == 1
    assert "do not have permission" in sent[0][0][0]
    assert "embed" not in sent[0][1]


async def test_hybrid_app_no_private_message_keeps_the_dm_branch(caplog):
    """app NoPrivateMessage subclasses app CheckFailure, but the handler has a
    dedicated DM branch. Flattening it to a generic CheckFailure would swap
    "use this in a server" for a bare permission refusal, which is wrong: the
    user has every permission, they are just in a DM.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)

    app_error = discord.app_commands.NoPrivateMessage()
    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

    assert caplog.text == ""
    field = sent[0][1]["embed"].fields[0]
    assert "private messages" in field.name
    assert "invite.yasuho.xyz" in field.value


async def test_hybrid_app_bot_missing_permissions_names_the_permissions(caplog):
    """app BotMissingPermissions must reach the ext branch that says "I am
    missing permissions" and names them, not the generic refusal that blames
    the user. The missing_permissions list carries over verbatim.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)

    app_error = discord.app_commands.BotMissingPermissions(["manage_roles"])
    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

    assert caplog.text == ""
    field = sent[0][1]["embed"].fields[0]
    assert "I am missing permissions" in field.name
    assert "Manage Roles" in field.value


async def test_hybrid_app_mapped_shapes_preserve_the_cause(monkeypatch):
    """Both mappings must keep the original app error as __cause__ so a later
    reader (or a traceback) can still see what actually refused.

    Observed by swapping the ext exception class the handler instantiates for a
    recording subclass: isinstance still matches, so the branch is unchanged.
    """
    for attr, app_error in (
        ("NoPrivateMessage", discord.app_commands.NoPrivateMessage()),
        (
            "BotMissingPermissions",
            discord.app_commands.BotMissingPermissions(["ban_members"]),
        ),
    ):
        built = []
        base = getattr(commands, attr)

        class _Recording(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built.append(self)

        with monkeypatch.context() as patch:
            patch.setattr(commands, attr, _Recording)
            sent = []
            cog, ctx = _handler_ctx(sent)
            await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

        assert len(built) == 1, f"{attr} was not mapped"
        assert built[0].__cause__ is app_error
        assert len(sent) == 1


async def test_hybrid_app_check_subclass_without_branch_stays_generic(caplog):
    """Ordering guard: only the two shapes WITH a dedicated branch are mapped.
    Any other app CheckFailure subclass (here MissingPermissions) still
    flattens to the generic discreet refusal.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)

    app_error = discord.app_commands.MissingPermissions(["manage_guild"])
    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

    assert caplog.text == ""
    assert len(sent) == 1
    assert "do not have permission" in sent[0][0][0]
    assert "embed" not in sent[0][1]


async def test_hybrid_app_transformer_error_takes_the_input_branch(caplog):
    """A value a Transformer could not convert is a user input error, not a
    crash: the bad-argument branch, no ERROR log, no error_id.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)

    app_error = discord.app_commands.TransformerError(
        "abc",
        discord.AppCommandOptionType.string,
        discord.app_commands.Transformer(),
    )
    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

    assert caplog.text == ""
    field = sent[0][1]["embed"].fields[0]
    assert "bad argument" in field.name
    assert "Failed to convert abc" in field.value


async def test_hybrid_app_cooldown_takes_the_cooldown_branch(caplog):
    """app CommandOnCooldown subclasses app CheckFailure, so it must be peeled
    before it: the user needs the remaining time, not a permission refusal.
    """
    sent = []
    cog, ctx = _handler_ctx(sent)

    app_error = discord.app_commands.CommandOnCooldown(
        discord.app_commands.Cooldown(1, 120.0), 65.0
    )
    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.HybridCommandError(app_error))

    assert caplog.text == ""
    field = sent[0][1]["embed"].fields[0]
    assert "cooldown" in field.name
    assert "0:01:05" in field.value


async def test_unknown_error_type_is_logged_and_reported(monkeypatch, caplog):
    """A command error matching no branch must not vanish: the else logs the
    full traceback and still replies with a traceable error_id.
    """

    class _Unhandled(commands.CommandError):
        pass

    sent = []
    cog, ctx = _handler_ctx(sent)
    monkeypatch.setattr(errors.secrets, "token_hex", lambda _size: "deadbeef")

    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, _Unhandled("weird failure"))

    assert "Unhandled command error" in caplog.text
    assert "_Unhandled" in caplog.text
    value = sent[0][1]["embed"].fields[0].value
    assert "deadbeef" in value


async def test_bypass_forces_handler_past_command_on_error():
    """With bypass=True the handler must run even when the command defines its
    own on_error; with bypass=False that same command short-circuits (no send).
    """
    command = types.SimpleNamespace(
        qualified_name="owned",
        cog_name="Test",
        signature="",
        on_error=lambda *a: None,
    )
    command.__str__ = lambda self: "owned"

    sent = []
    cog, ctx = _handler_ctx(sent, command=command)
    await cog._on_command_error(ctx, commands.NotOwner(), bypass=False)
    assert sent == []

    await cog._on_command_error(ctx, commands.NotOwner(), bypass=True)
    assert len(sent) == 1
    assert "do not have permission" in sent[0][0][0]


class _FakeCommand:
    """Visible bot command for the suggestion scan (name, hidden, str())."""

    def __init__(self, name):
        self.name = name
        self.hidden = False

    def __str__(self):
        return self.name


async def test_command_not_found_does_not_reach_the_else(caplog):
    """Non-regression: an unknown command keeps its "did you mean" branch and is
    never logged as a crash nor shown the generic report embed.
    """
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        commands=[_FakeCommand("xyz2")],
        get_cog=lambda _name: None,
    )
    sent = []
    cog, ctx = _handler_ctx(sent, bot=bot)

    with caplog.at_level("ERROR"):
        await cog._on_command_error(ctx, commands.CommandNotFound())

    assert "Unhandled command error" not in caplog.text
    assert "Command invocation failed" not in caplog.text
    name = sent[0][1]["embed"].fields[0].name
    assert "Invalid command entered" in name


def _untouchable_bot():
    """Bot whose CommandNotFound surfaces fail loudly if touched.

    Iterating ``commands`` (the suggestion scan) or calling ``get_cog`` (the
    custom-command dispatch) raises, so a test proves the silent path does
    neither, not merely that nothing was sent.
    """

    class _NoIter:
        def __iter__(self):
            raise AssertionError("suggestions must not be computed")

    def get_cog(_name):
        raise AssertionError("custom-command dispatch must not run")

    return types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        commands=_NoIter(),
        get_cog=get_cog,
    )


@pytest.mark.parametrize("invoked", ["????", "!!!!", "?_?", "\U0001F600"])
async def test_punctuation_only_invocation_is_silent(invoked):
    """"?????" typed as chat punctuation parses as prefix + invoked_with
    "????". Nothing alphanumeric means it was never a command attempt: no
    reply, no suggestion scan, no custom-command lookup.
    """
    sent = []
    cog, ctx = _handler_ctx(sent, bot=_untouchable_bot())
    ctx.invoked_with = invoked

    await cog._on_command_error(ctx, commands.CommandNotFound())

    assert sent == []


@pytest.mark.parametrize("invoked", ["", None])
async def test_empty_invoked_with_is_silent(invoked):
    sent = []
    cog, ctx = _handler_ctx(sent, bot=_untouchable_bot())
    ctx.invoked_with = invoked

    await cog._on_command_error(ctx, commands.CommandNotFound())

    assert sent == []


@pytest.mark.parametrize("invoked", ["?play", "!ban", "??play", "?!ban"])
async def test_other_bots_longer_prefix_is_silent(invoked):
    """"??play" or "?!ban" aimed at another bot with a longer prefix, on a
    server where our prefix is "?", parses as our prefix + an invoked_with
    starting with the leftover punctuation. No command name can start with a
    non-alphanumeric character, so this is fully silent: no custom-command
    lookup, no suggestion scan, no reply at users talking to the other bot.
    """
    sent = []
    cog, ctx = _handler_ctx(sent, bot=_untouchable_bot())
    ctx.invoked_with = invoked

    await cog._on_command_error(ctx, commands.CommandNotFound())

    assert sent == []


async def test_leading_non_alnum_char_is_silent():
    """"_rank" contains alphanumerics, but its FIRST character is not one, and
    command_naming forbids that shape for any stored name: silent, with no
    dispatch or suggestion scan.
    """
    sent = []
    cog, ctx = _handler_ctx(sent, bot=_untouchable_bot())
    ctx.invoked_with = "_rank"

    await cog._on_command_error(ctx, commands.CommandNotFound())

    assert sent == []


@pytest.mark.parametrize(
    ("invoked", "close"), [("foo", "food"), ("4", "42")]
)
async def test_alnum_invocation_keeps_the_did_you_mean_reply(invoked, close):
    """A typo starting alphanumeric ("foo", "?4" -> "4") with a real nearby
    command is a plausible attempt: the custom-command lookup still runs
    first, then the reply carries the actual suggestion.
    """
    looked_up = []
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        commands=[_FakeCommand(close)],
        get_cog=lambda name: looked_up.append(name),
    )
    sent = []
    cog, ctx = _handler_ctx(sent, bot=bot)
    ctx.invoked_with = invoked

    await cog._on_command_error(ctx, commands.CommandNotFound())

    assert looked_up == ["CustomCommands"]
    assert "Invalid command entered" in sent[0][1]["embed"].fields[0].name
    assert close in sent[0][1]["embed"].fields[0].value


async def test_no_suggestion_found_stays_silent():
    """A garbage name far from every command draws NO reply at all: the old
    "Sorry, no similar commands found" fallback embed must not come back.
    """
    looked_up = []
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        commands=[_FakeCommand("play"), _FakeCommand("rank")],
        get_cog=lambda name: looked_up.append(name),
    )
    sent = []
    cog, ctx = _handler_ctx(sent, bot=bot)
    ctx.invoked_with = "qwjxzvqwjxzv"

    await cog._on_command_error(ctx, commands.CommandNotFound())

    # The custom-command lookup still ran (a real cc may claim any name)...
    assert looked_up == ["CustomCommands"]
    # ...but with nothing close enough to suggest, the user hears nothing.
    assert sent == []


async def test_custom_command_still_claims_before_suggestions():
    """First-claim ordering: when a custom command handles the name, the
    handler stops without computing or sending suggestions.
    """

    class _CC:
        async def handle_unknown(self, _ctx):
            return True

    class _NoIter:
        def __iter__(self):
            raise AssertionError("suggestions must not be computed")

    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        commands=_NoIter(),
        get_cog=lambda _name: _CC(),
    )
    sent = []
    cog, ctx = _handler_ctx(sent, bot=bot)
    ctx.invoked_with = "greet"

    await cog._on_command_error(ctx, commands.CommandNotFound())

    assert sent == []


def test_command_naming_rejects_punctuation_only_names():
    """Placement evidence: the silence guard runs BEFORE the custom-command
    dispatch, which is safe only while no stored name can start with a
    non-alphanumeric character. validate_name (_NAME_RE) requires an
    alphanumeric FIRST character, so any name the guard silences - pure
    punctuation or merely a non-alnum first char like "??x" or "_x" - could
    never have been created.
    """
    from cogs.config import command_naming as cn

    for raw in ("????", "!!!!", "?_?", "\U0001F600", "??x", "_x", "?play"):
        name = cn.normalize_name(raw)
        assert (
            cn.validate_name(name, reserved=set(), existing=set()) == "bad_chars"
        )
