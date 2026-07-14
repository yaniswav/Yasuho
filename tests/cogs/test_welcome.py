"""Unit tests for the ``/welcome status`` read-only card (Lot BL3).

Covers ``WelcomeStatusView`` in both states (configured / unconfigured) and
the ``welcome status`` subcommand wiring, including the truncated embed
description preview (via ``embed_creator.summarise``). Drives against the
conftest fakes: ``fake_pool`` (records every DB call).
"""

import types

import discord

from cogs.config.welcome import Welcome, WelcomeStatusView, _default_config


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeGuild:
    def __init__(self, guild_id=1, name="guild"):
        self.id = guild_id
        self.name = name


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
    return Welcome(bot)


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


def _card_text(view):
    return "\n".join(
        c.content for c in view.children[0].children if hasattr(c, "content")
    )


# ---------------------------------------------------------------------------
# WelcomeStatusView rendering
# ---------------------------------------------------------------------------
def test_status_card_unconfigured_shows_disabled_and_hint():
    guild = _FakeGuild()
    config = _default_config()  # channel_id=None, enabled=False
    view = WelcomeStatusView(guild, config)
    assert len(view.children) == 1  # a single Container
    text = _card_text(view)
    assert "Disabled" in text
    assert "Not set" in text
    assert "/welcome" in text


def test_status_card_configured_shows_channel_and_toggles():
    guild = _FakeGuild()
    config = _default_config()
    config["channel_id"] = 321
    config["enabled"] = True
    config["card"] = True
    config["random_gif"] = True
    config["ping"] = False
    config["gifs"] = ["https://a", "https://b"]
    view = WelcomeStatusView(guild, config)
    text = _card_text(view)
    assert "Enabled" in text
    assert "<#321>" in text
    assert "2 saved" in text
    assert _text_chars(view) < 4000


def test_status_card_truncates_long_message_template():
    guild = _FakeGuild()
    config = _default_config()
    config["channel_id"] = 1
    config["enabled"] = True
    long_message = "x" * 500
    config["embed"]["description"] = long_message
    view = WelcomeStatusView(guild, config)
    text = _card_text(view)
    assert long_message not in text  # never leaks the untruncated template
    assert "x" * 117 + "..." in text
    assert _text_chars(view) < 4000


# ---------------------------------------------------------------------------
# Subcommand wiring
# ---------------------------------------------------------------------------
async def test_welcome_status_command_sends_the_card(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=701))
    await cog.welcome_status.callback(cog, ctx)
    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert isinstance(kwargs["view"], WelcomeStatusView)
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


async def test_welcome_status_command_is_pure_read(fake_pool):
    """A ``status`` call must never write (unlike ``set``/``disable``/``test``)."""

    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=702))
    await cog.welcome_status.callback(cog, ctx)
    execs = [c for c in fake_pool.calls if c[0] == "execute"]
    assert execs == []
