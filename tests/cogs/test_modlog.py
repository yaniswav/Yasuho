"""Unit tests for the ``/modlog status`` read-only card (Lot BL3).

Covers ``ModLogStatusView`` in both states (configured / unconfigured, plus
the events-restricted subset) and the ``modlog status`` subcommand wiring.
Drives against the conftest fakes: ``fake_pool`` (records every DB call).
"""

import types

import discord

from cogs.moderation.modlog import EVENT_KEYS, ModLog, ModLogStatusView


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeGuild:
    def __init__(self, guild_id=1, name="guild"):
        self.id = guild_id
        self.name = name

    def get_channel(self, cid):
        return None  # channel resolution isn't exercised by the status card


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
    return ModLog(bot)


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


def _events_block(text):
    """Just the '**Events**\\n...' section (excludes the status line's own dot)."""

    return text.split("**Events**", 1)[1]


# ---------------------------------------------------------------------------
# ModLogStatusView rendering
# ---------------------------------------------------------------------------
def test_status_card_unconfigured_shows_disabled_and_hint():
    guild = _FakeGuild()
    view = ModLogStatusView(guild, None, None)
    assert len(view.children) == 1  # a single Container
    text = _card_text(view)
    assert "Disabled" in text
    assert "Not set" in text
    assert "/modlog set" in text
    # events=None -> every event key shown as enabled (green dot).
    assert _events_block(text).count("🟢") == len(EVENT_KEYS)
    assert _text_chars(view) < 4000


def test_status_card_configured_shows_channel_and_all_events_enabled():
    guild = _FakeGuild()
    view = ModLogStatusView(guild, 555, None)
    text = _card_text(view)
    assert "Enabled" in text
    assert "<#555>" in text
    assert "/modlog set" not in text
    events_text = _events_block(text)
    assert events_text.count("🟢") == len(EVENT_KEYS)
    assert "⚪" not in events_text
    assert _text_chars(view) < 4000


def test_status_card_configured_shows_events_subset():
    guild = _FakeGuild()
    view = ModLogStatusView(guild, 555, ["join", "ban"])
    events_text = _events_block(_card_text(view))
    assert events_text.count("🟢") == 2
    assert events_text.count("⚪") == len(EVENT_KEYS) - 2


# ---------------------------------------------------------------------------
# Subcommand wiring
# ---------------------------------------------------------------------------
async def test_modlog_status_command_sends_the_card(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=7))
    await cog.modlog_status.callback(cog, ctx)
    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert isinstance(kwargs["view"], ModLogStatusView)
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


async def test_modlog_status_command_is_pure_read(fake_pool):
    """A ``status`` call must never write (unlike ``set``/``disable``)."""

    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=8))
    await cog.modlog_status.callback(cog, ctx)
    execs = [c for c in fake_pool.calls if c[0] == "execute"]
    assert execs == []
