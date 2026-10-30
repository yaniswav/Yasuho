"""Yasuho+ welcome card: premium gating, lapse fallback, config round-trip."""

import dataclasses
import io
import types

from PIL import Image

import cogs.config.welcome as welcome_module
from cogs.config.welcome import Welcome, _default_config, _merge_defaults
from tools import card_themes, premium


def _png(size=(64, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


class _Avatar:
    async def read(self):
        return b"avatar-bytes"

    def replace(self, **kwargs):
        return self


class _Member:
    def __init__(self):
        self.display_avatar = _Avatar()
        self.display_name = "Newbie"
        self.guild = types.SimpleNamespace(id=1, name="Hub", member_count=42)


def _cog(fake_pool, *, premium_ok, monkeypatch):
    limits = dataclasses.replace(premium.GUILD_FREE, premium_welcome_card=premium_ok)
    bot = types.SimpleNamespace(
        db_pool=fake_pool,
        premium=types.SimpleNamespace(for_guild=lambda guild_id: limits),
    )
    calls = []

    async def _run(bot_, function, *args, **kwargs):
        calls.append(args)
        return "buf"

    async def _theme(bot_, guild_id):
        return card_themes.DEFAULT

    monkeypatch.setattr(welcome_module.rendering, "run_image_job", _run)
    monkeypatch.setattr(card_themes, "for_guild", _theme)
    return Welcome(bot), calls


_CONFIG = {
    **_default_config(),
    "card_greeting": "Hey {user}",
    "card_subtitle": "{count} in {server}",
    "card_color": "#FF00FF",
}


async def test_premium_render_uses_background_lines_and_colour(fake_pool, monkeypatch):
    fake_pool.fetchval_return = _png()
    cog, calls = _cog(fake_pool, premium_ok=True, monkeypatch=monkeypatch)
    await cog.render_welcome_card(_Member(), _CONFIG)
    args = calls[0]
    assert args[4].text == (255, 0, 255) and args[4].ring == (255, 0, 255)
    assert args[5] == fake_pool.fetchval_return
    assert args[6] == "Hey Newbie"
    assert args[7] == "42 in Hub"


async def test_free_render_is_the_stock_card_and_reads_nothing(fake_pool, monkeypatch):
    cog, calls = _cog(fake_pool, premium_ok=False, monkeypatch=monkeypatch)
    await cog.render_welcome_card(_Member(), _CONFIG)
    args = calls[0]
    assert args[4] is card_themes.DEFAULT.welcome
    assert args[5:] == (None, None, None)
    assert fake_pool.calls == []


async def test_lapse_keeps_stored_values_and_premium_return_reapplies(
    fake_pool, monkeypatch
):
    config = _merge_defaults(_CONFIG)
    cog, _ = _cog(fake_pool, premium_ok=False, monkeypatch=monkeypatch)
    await cog.render_welcome_card(_Member(), config)
    assert config["card_greeting"] == "Hey {user}"
    assert config["card_color"] == "#FF00FF"
    fake_pool.fetchval_return = _png()
    cog, calls = _cog(fake_pool, premium_ok=True, monkeypatch=monkeypatch)
    await cog.render_welcome_card(_Member(), config)
    assert calls[0][6] == "Hey Newbie"


async def test_premium_without_stored_background_draws_the_flat_card(
    fake_pool, monkeypatch
):
    fake_pool.fetchval_return = None
    cog, calls = _cog(fake_pool, premium_ok=True, monkeypatch=monkeypatch)
    await cog.render_welcome_card(_Member(), _default_config())
    assert calls[0][5:] == (None, None, None)


def test_merge_defaults_sanitises_card_keys():
    config = _merge_defaults(
        {"card_greeting": "x" * 200, "card_subtitle": 5, "card_color": "zzz"}
    )
    assert len(config["card_greeting"]) == 60
    assert config["card_subtitle"] is None
    assert config["card_color"] is None
    assert _merge_defaults({"card_color": "f0f"})["card_color"] == "#FF00FF"
    assert _merge_defaults({"card_color": 0x5865F2})["card_color"] == "#5865F2"
    assert _merge_defaults(None)["card_greeting"] is None


async def _submit_modal(greeting, subtitle, colour, config):
    sent, saved = [], []

    class _Cog:
        bot = None

        async def save(self, guild_id, cfg):
            saved.append(dict(cfg))

    class _Panel:
        guild = types.SimpleNamespace(id=1)
        cog = _Cog()

        def __init__(self):
            self.config = config

        async def _rerender(self, interaction):
            sent.append("rerender")

    modal = welcome_module.CardModal(_Panel())
    modal.greeting_field._value = greeting
    modal.subtitle_field._value = subtitle
    modal.colour_field._value = colour

    async def _send(text, **kwargs):
        sent.append(text)

    await modal.on_submit(
        types.SimpleNamespace(response=types.SimpleNamespace(send_message=_send))
    )
    return sent, saved


async def test_invalid_colour_keeps_the_lines_and_the_old_colour():
    config = {**_default_config(), "card_color": "#112233"}
    sent, saved = await _submit_modal("Hey", "Sub", "nope", config)
    assert config["card_greeting"] == "Hey" and config["card_subtitle"] == "Sub"
    assert config["card_color"] == "#112233"
    assert len(saved) == 1 and "rerender" not in sent
    assert "saved" in sent[0]


async def test_valid_submit_saves_everything_and_rerenders():
    config = _default_config()
    sent, saved = await _submit_modal("Hey", "", "#f0f", config)
    assert config["card_color"] == "#FF00FF" and config["card_subtitle"] is None
    assert sent == ["rerender"] and len(saved) == 1
