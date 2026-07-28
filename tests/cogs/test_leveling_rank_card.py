"""The rank-card customisation seam in ``cogs.community.leveling`` (RC1).

Three things are pinned here, in ascending order of how expensive a regression
would be:

* the STYLE CACHE contract - one DB read per cold guild, none on a hit, an
  eviction hook for RC2's panel and the dashboard, and a DB failure that
  degrades to the stock card WITHOUT poisoning the cache;
* the DEFAULT RENDER - a golden hash proving the stock card is byte-for-byte
  what it was before this lot existed (the hash below was captured from the
  pre-RC1 renderer and compared against the post-RC1 one; they matched);
* the CUSTOM RENDER - a real Pillow smoke test (the ``compress_for_storage``
  house pattern in tests/cogs/test_avatarhistory.py) proving a background and an
  accent actually reach the pixels, that the card's rounded corners survive, and
  that a corrupt stored blob silently falls back to the stock panel.

No network, no database, no Discord: the cog is built around a fake bot holding
a recording pool, exactly as tests/cogs/test_leveling.py does.
"""

from __future__ import annotations

import hashlib
import io
import types

import pytest
from PIL import Image

from cogs.community.leveling import Leveling
from tools import rank_card

# ---------------------------------------------------------------------------
# Fixtures: the card's inputs, held constant so the golden hash is meaningful.
# ---------------------------------------------------------------------------

_CARD_ARGS = ("Yasuho Hirose", 12, 3, 15000, 14400, 16900, (88, 101, 242))

# SHA-256 of the stock card rendered from _avatar() + _CARD_ARGS. Captured by
# rendering the PRE-RC1 implementation (git HEAD before this lot) and the
# post-RC1 one side by side: identical, 12865 bytes. It pins the DEFAULT path -
# if a future edit shifts the layout, recolours the panel or leaks the custom
# background's contrast scrim into the no-config path, this is what catches it.
# Pillow is pinned (requirements.lock: pillow==12.3.0) and the font is bundled,
# so the value is stable; a deliberate card redesign regenerates it in the same
# commit that changes the render.
_STOCK_CARD_SHA256 = (
    "283c72f944c86d95ff24fd6de7ad5a5b33b0b9583fc49a29e417adc23608049c"
)


def _avatar():
    buffer = io.BytesIO()
    Image.new("RGBA", (128, 128), (200, 40, 120, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def _render(background=None, accent=(88, 101, 242)):
    name, level, rank_pos, xp, cur_threshold, next_threshold, _default = _CARD_ARGS
    return Leveling._render_rank_card(
        _avatar(),
        name,
        level,
        rank_pos,
        xp,
        cur_threshold,
        next_threshold,
        accent,
        background,
    ).getvalue()


def _background(colour=(20, 200, 90)):
    """A real, stored-shaped background: card-sized WebP, as RC1 persists it."""
    encoded, stored_format = rank_card.validate_and_downscale(
        _png(Image.new("RGB", (1600, 900), colour))
    )
    assert stored_format == "webp"
    return encoded


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Fake bot / pool.
# ---------------------------------------------------------------------------


class RankCardPool:
    """Fake pool serving the two rank_cards reads, counting every call."""

    def __init__(self, rows=None, backgrounds=None):
        # guild_id -> {"accent": int|None, "background_format": str|None,
        #              "has_background": bool}; absent => no row at all.
        self.rows = rows or {}
        self.backgrounds = backgrounds or {}
        self.config_reads = 0
        self.background_reads = 0
        self.fail_config = False
        self.fail_background = False

    async def fetchrow(self, query, *args):
        assert "FROM rank_cards" in query
        self.config_reads += 1
        if self.fail_config:
            raise RuntimeError("db down")
        return self.rows.get(args[0])

    async def fetchval(self, query, *args):
        assert "FROM rank_cards" in query
        self.background_reads += 1
        if self.fail_background:
            raise RuntimeError("db down")
        return self.backgrounds.get(args[0])


def _cog(pool):
    return Leveling(types.SimpleNamespace(db_pool=pool, get_cog=lambda name: None))


# ---------------------------------------------------------------------------
# Style cache.
# ---------------------------------------------------------------------------


async def test_cold_read_caches_and_a_hit_costs_no_query():
    pool = RankCardPool(
        rows={7: {"accent": 0x5865F2, "background_format": "webp", "has_background": True}}
    )
    cog = _cog(pool)

    assert await cog.ensure_rank_card_style(7) == ((0x58, 0x65, 0xF2), True)
    assert pool.config_reads == 1
    # Second call is a pure BoundedLRU read.
    assert await cog.ensure_rank_card_style(7) == ((0x58, 0x65, 0xF2), True)
    assert pool.config_reads == 1
    # The blob itself is NEVER pulled in by the style read (scale story).
    assert pool.background_reads == 0


async def test_guild_without_a_row_caches_the_stock_style():
    pool = RankCardPool()
    cog = _cog(pool)

    assert await cog.ensure_rank_card_style(7) == (None, False)
    assert await cog.ensure_rank_card_style(7) == (None, False)
    # A stock guild must not re-query on every /rank either.
    assert pool.config_reads == 1


async def test_accent_only_and_background_only_rows():
    pool = RankCardPool(
        rows={
            1: {"accent": 0xFF0000, "background_format": None, "has_background": False},
            2: {"accent": None, "background_format": "webp", "has_background": True},
        }
    )
    cog = _cog(pool)
    assert await cog.ensure_rank_card_style(1) == ((255, 0, 0), False)
    assert await cog.ensure_rank_card_style(2) == (None, True)


async def test_db_failure_degrades_to_stock_without_poisoning_the_cache():
    """A hiccup must not pin the stock style forever, nor break /rank."""
    pool = RankCardPool(
        rows={7: {"accent": 0xFF0000, "background_format": None, "has_background": False}}
    )
    pool.fail_config = True
    cog = _cog(pool)

    assert await cog.ensure_rank_card_style(7) == (None, False)
    assert 7 not in cog._rank_cards

    pool.fail_config = False
    assert await cog.ensure_rank_card_style(7) == ((255, 0, 0), False)


async def test_invalidate_forces_a_reread():
    pool = RankCardPool(
        rows={7: {"accent": None, "background_format": None, "has_background": False}}
    )
    cog = _cog(pool)
    await cog.ensure_rank_card_style(7)
    assert pool.config_reads == 1

    cog.invalidate_rank_card(7)
    pool.rows[7] = {
        "accent": 0x00FF00,
        "background_format": "webp",
        "has_background": True,
    }
    assert await cog.ensure_rank_card_style(7) == ((0, 255, 0), True)
    assert pool.config_reads == 2


async def test_invalidating_an_uncached_guild_is_a_no_op():
    cog = _cog(RankCardPool())
    cog.invalidate_rank_card(12345)  # must not raise


async def test_style_cache_is_bounded():
    """The cap is the whole point: 1000 guilds must not become 1000 entries."""
    pool = RankCardPool()
    cog = _cog(pool)
    for guild_id in range(1500):
        await cog.ensure_rank_card_style(guild_id)
    assert len(cog._rank_cards) == 512


# ---------------------------------------------------------------------------
# Default render: byte-for-byte unchanged.
# ---------------------------------------------------------------------------


def test_default_render_matches_the_pre_rc1_golden_hash():
    assert hashlib.sha256(_render()).hexdigest() == _STOCK_CARD_SHA256


def test_default_render_never_touches_the_background_painter(monkeypatch):
    """No config => not one extra Pillow operation on the stock path."""

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("_paint_background must not run without a background")

    monkeypatch.setattr(Leveling, "_paint_background", staticmethod(_boom))
    assert hashlib.sha256(_render()).hexdigest() == _STOCK_CARD_SHA256


def test_corrupt_stored_background_falls_back_to_the_stock_card():
    """A member's /rank must survive an undecodable row, not error out."""
    rendered = _render(background=b"this is not a webp file")
    assert hashlib.sha256(rendered).hexdigest() == _STOCK_CARD_SHA256


# ---------------------------------------------------------------------------
# Custom render: real Pillow smoke.
# ---------------------------------------------------------------------------


def _open(rendered):
    with Image.open(io.BytesIO(rendered)) as image:
        return image.convert("RGBA")


def test_background_reaches_the_pixels_and_keeps_the_card_geometry():
    rendered = _render(background=_background())
    assert rendered != _render()

    card = _open(rendered)
    assert card.size == rank_card.CARD_SIZE == (880, 240)
    # A point in the empty right-hand strip, above the bar and below the stats:
    # stock card = the flat panel colour, custom card = a green-dominant tint.
    red, green, blue, alpha = card.getpixel((820, 20))
    assert alpha == 255
    assert green > red and green > blue
    # The rounded corner stays transparent: the background is masked to the
    # card's shape rather than pasted as a rectangle.
    assert card.getpixel((0, 0))[3] == 0


def test_background_is_scrimmed_so_the_layout_stays_readable():
    """A pure-white background must not be pasted at full brightness."""
    rendered = _render(background=_background(colour=(255, 255, 255)))
    card = _open(rendered)
    red, green, blue, _alpha = card.getpixel((820, 20))
    assert max(red, green, blue) < 255  # the scrim actually darkened it
    assert min(red, green, blue) > 40  # ...but the image is still visible


def test_accent_reaches_the_progress_bar():
    """The guild accent recolours the bar fill (and the ring / LEVEL label)."""
    accent = (255, 0, 128)
    rendered = _render(accent=accent)
    assert rendered != _render()

    card = _open(rendered)
    # Inside the filled portion of the bar: bar_y = 185, bar_h = 30, and the
    # fixture's XP sits ~24% into the level, so x=250 is filled.
    assert card.getpixel((250, 200))[:3] == accent


def test_background_and_accent_compose():
    both = _render(background=_background(), accent=(255, 0, 128))
    assert both != _render()
    assert both != _render(background=_background())
    assert both != _render(accent=(255, 0, 128))
    assert _open(both).size == rank_card.CARD_SIZE


def test_card_geometry_constants_are_shared_with_the_tool():
    """The renderer must not redeclare the dimensions the storage crops to."""
    card = _open(_render())
    assert card.size == rank_card.CARD_SIZE
    assert rank_card.CARD_SIZE == (rank_card.CARD_WIDTH, rank_card.CARD_HEIGHT)


@pytest.mark.parametrize("size", [(400, 400), (2400, 300)])
def test_stored_background_of_any_source_shape_renders(size):
    """Whatever the admin uploaded, the render receives a card-sized blob."""
    encoded, _ = rank_card.validate_and_downscale(
        _png(Image.new("RGB", size, (10, 80, 200)))
    )
    assert _open(_render(background=encoded)).size == rank_card.CARD_SIZE
