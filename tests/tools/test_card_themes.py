"""Tests for tools/card_themes.py: resolution, catalogue and the render seam."""

import dataclasses
import io
import types

import pytest
from PIL import Image

from cogs.community.leveling.leveling import Leveling
from cogs.config.welcome_card import render_card
from tools import card_themes, premium, settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    settings._cache.clear()
    yield
    settings._cache.clear()


_PREMIUM = card_themes.Theme(
    "neon",
    "Neon",
    premium=True,
    rank=card_themes.RankStyle(panel=(1, 2, 3)),
    welcome=card_themes.WelcomeStyle(ring=(255, 0, 255)),
)


@pytest.fixture
def with_premium_theme(monkeypatch):
    monkeypatch.setattr(card_themes, "THEMES", {**card_themes.THEMES, "neon": _PREMIUM})


def test_resolve_unknown_key_is_default():
    assert card_themes.resolve("nope", True) is card_themes.DEFAULT
    assert card_themes.resolve(None, True) is card_themes.DEFAULT


def test_resolve_premium_key_lapsed_is_default(with_premium_theme):
    assert card_themes.resolve("neon", False) is card_themes.DEFAULT


def test_resolve_premium_key_active(with_premium_theme):
    assert card_themes.resolve("neon", True) is _PREMIUM


def test_catalogue_snapshot(with_premium_theme):
    assert card_themes.catalogue() == [
        {"key": "default", "label": "Classic", "tier": "free"},
        {"key": "neon", "label": "Neon", "tier": "premium"},
    ]


def test_catalogue_today_is_only_the_default():
    assert card_themes.catalogue() == [
        {"key": "default", "label": "Classic", "tier": "free"}
    ]


def _avatar():
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (10, 120, 200, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_non_default_theme_changes_both_images(with_premium_theme):
    welcome_default = render_card(_avatar(), "Yasuho", 1, (30, 40, 50)).getvalue()
    welcome_neon = render_card(
        _avatar(), "Yasuho", 1, (30, 40, 50), _PREMIUM.welcome
    ).getvalue()
    assert welcome_default != welcome_neon

    args = (_avatar(), "Yasuho", 3, 1, 10, 0, 100, (88, 101, 242))
    rank_default = Leveling._render_rank_card(*args).getvalue()
    rank_neon = Leveling._render_rank_card(*args, theme=_PREMIUM).getvalue()
    assert rank_default != rank_neon


def _bot(fake_pool, *, premium_ok):
    limits = dataclasses.replace(
        premium.GUILD_FREE, premium_card_themes=premium_ok
    )
    resolver = types.SimpleNamespace(for_guild=lambda guild_id: limits)
    return types.SimpleNamespace(db_pool=fake_pool, premium=resolver)


async def test_for_guild_reads_the_cached_blob_once(fake_pool, with_premium_theme):
    fake_pool.fetchval_return = '{"card_theme": "neon"}'
    bot = _bot(fake_pool, premium_ok=True)
    assert await card_themes.for_guild(bot, 5) is _PREMIUM
    assert await card_themes.for_guild(bot, 5) is _PREMIUM
    assert len(fake_pool.calls) == 1  # warm cache: no extra query


async def test_for_guild_lapsed_premium_draws_default(fake_pool, with_premium_theme):
    fake_pool.fetchval_return = '{"card_theme": "neon"}'
    bot = _bot(fake_pool, premium_ok=False)
    assert await card_themes.for_guild(bot, 5) is card_themes.DEFAULT


async def test_for_guild_never_raises():
    assert await card_themes.for_guild(types.SimpleNamespace(), 5) is card_themes.DEFAULT
