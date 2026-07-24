"""Tests for the global error handler's embed builder (cogs/system/errors.py).

Regression guard: a long error/usage string must not exceed Discord's field
limits (256 for the name, 1024 for the value), which would 400 the whole error
report and hide the underlying error.
"""

import types

import discord
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


async def test_command_not_found_does_not_reach_the_else(caplog):
    """Non-regression: an unknown command keeps its "did you mean" branch and is
    never logged as a crash nor shown the generic report embed.
    """
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        commands=[],
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
