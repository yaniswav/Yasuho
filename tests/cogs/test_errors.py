"""Tests for the global error handler's embed builder (cogs/system/errors.py).

Regression guard: a long error/usage string must not exceed Discord's field
limits (256 for the name, 1024 for the value), which would 400 the whole error
report and hide the underlying error.
"""

import types

import discord

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
