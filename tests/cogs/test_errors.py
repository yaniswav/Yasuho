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
