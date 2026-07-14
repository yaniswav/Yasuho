"""Unit tests for the ``/verify status`` read-only card (Lot BL3).

Covers ``VerifyStatusView`` in both states (configured / unconfigured, plus a
deleted-role edge case) and the ``verify status`` subcommand wiring. Drives
against the conftest fakes: ``fake_pool`` (records every DB call).
"""

import types

import discord

from cogs.config.verification import Verification, VerifyStatusView


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeGuild:
    def __init__(self, guild_id=1, name="guild", roles=()):
        self.id = guild_id
        self.name = name
        self._roles = {r.id: r for r in roles}

    def get_role(self, rid):
        return self._roles.get(rid)


class _FakeRole:
    def __init__(self, role_id, name="Verified"):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"


class _Ctx:
    def __init__(self, guild, author_id=1):
        self.guild = guild
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace()


def _make_cog(fake_pool):
    bot = types.SimpleNamespace(db_pool=fake_pool)
    return Verification(bot)


def _text_chars(view):
    total = 0

    def walk(item):
        nonlocal total
        content = getattr(item, "content", None)
        if isinstance(content, str):
            total += len(content)
        for child in getattr(item, "children", None) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return total


# ---------------------------------------------------------------------------
# VerifyStatusView rendering
# ---------------------------------------------------------------------------
def test_status_card_unconfigured_shows_disabled_and_hint():
    guild = _FakeGuild()
    view = VerifyStatusView(guild, None)
    assert len(view.children) == 1  # a single Container
    text = "\n".join(
        c.content for c in view.children[0].children if hasattr(c, "content")
    )
    assert "Disabled" in text
    assert "Not set" in text
    assert "/verify setup" in text
    assert _text_chars(view) < 4000


def test_status_card_configured_shows_role_mention():
    role = _FakeRole(42)
    guild = _FakeGuild(roles=[role])
    view = VerifyStatusView(guild, 42)
    text = "\n".join(
        c.content for c in view.children[0].children if hasattr(c, "content")
    )
    assert "Enabled" in text
    assert role.mention in text
    assert "/verify setup" not in text
    assert _text_chars(view) < 4000


def test_status_card_deleted_role_shows_placeholder():
    guild = _FakeGuild()  # role 99 was never registered -> resolves to None
    view = VerifyStatusView(guild, 99)
    text = "\n".join(
        c.content for c in view.children[0].children if hasattr(c, "content")
    )
    assert "99" in text
    assert "(deleted)" in text
    assert "Disabled" in text  # a deleted role is not a working configuration


# ---------------------------------------------------------------------------
# Subcommand wiring
# ---------------------------------------------------------------------------
async def test_verify_status_command_sends_the_card(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=7))
    await cog.verify_status.callback(cog, ctx)
    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert isinstance(kwargs["view"], VerifyStatusView)
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


async def test_verify_status_command_is_pure_read(fake_pool):
    """A ``status`` call must never write settings (unlike setup/disable)."""

    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=8))
    await cog.verify_status.callback(cog, ctx)
    execs = [c for c in fake_pool.calls if c[0] == "execute"]
    assert execs == []
