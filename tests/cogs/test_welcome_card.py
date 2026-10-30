"""Tests for cogs.config.welcome_card.render_card.

render_card is pure Pillow: given avatar bytes, a display name, a member count
and a background colour, it must return a PNG BytesIO that opens as a 640x200
RGB image. These tests build a tiny in-memory PNG for the avatar so nothing
touches the network, disk fixtures, Discord, or a database.
"""

import hashlib
import io

from PIL import Image

from cogs.config.welcome_card import render_card


def _tiny_png_bytes(color=(10, 120, 200, 255), size=(8, 8)):
    """Return the bytes of a tiny in-memory RGBA PNG for use as an avatar."""
    img = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_render_card_returns_640x200_rgb_bytesio():
    avatar = _tiny_png_bytes()

    result = render_card(avatar, "Yasuho", 42, (30, 40, 50))

    assert isinstance(result, io.BytesIO)
    # A returned buffer should be rewound and ready to read.
    assert result.tell() == 0

    with Image.open(result) as img:
        assert img.size == (640, 200)
        assert img.mode == "RGB"


def test_render_card_long_name_does_not_raise():
    avatar = _tiny_png_bytes()
    long_name = "Supercalifragilisticexpialidocious" * 20

    result = render_card(avatar, long_name, 1, (0, 0, 0))

    with Image.open(result) as img:
        assert img.size == (640, 200)
        assert img.mode == "RGB"


def test_render_card_empty_name_does_not_raise():
    # The shrink loop is guarded by ``while name``; an empty name must not spin.
    avatar = _tiny_png_bytes()

    result = render_card(avatar, "", 7, (12, 34, 56))

    with Image.open(result) as img:
        assert img.size == (640, 200)
        assert img.mode == "RGB"


def test_render_card_produces_valid_png_signature():
    avatar = _tiny_png_bytes()

    result = render_card(avatar, "Test", 3, (200, 100, 50))

    head = result.getvalue()[:8]
    result.seek(0)
    assert head == b"\x89PNG\r\n\x1a\n"


def test_render_card_background_colour_applied():
    # A corner pixel away from the avatar/text should carry the requested bg.
    avatar = _tiny_png_bytes()
    bg = (12, 34, 56)

    result = render_card(avatar, "Yasuho", 9, bg)

    with Image.open(result) as img:
        # Top-right corner is background only (avatar sits on the left).
        assert img.getpixel((img.width - 1, 0)) == bg


def _golden_avatar():
    img = Image.new("RGBA", (16, 16))
    img.putdata([(x * 15, y * 15, (x + y) * 7, 255) for y in range(16) for x in range(16)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_default_welcome_render_matches_the_golden_hash():
    out = render_card(_golden_avatar(), "Yasuho", 42, (30, 40, 50)).getvalue()
    assert hashlib.sha256(out).hexdigest() == (
        "166e66051060b6cb8faa3f95403783df3ba812398314f20d104b70c0c58e0725"
    )


# ---------------------------------------------------------------------------
# Yasuho+ extras: background, custom lines, text colour
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

from cogs.config import welcome_card  # noqa: E402
from tools.card_themes import WelcomeStyle  # noqa: E402


def _image_bytes(size, mode="RGB", fmt="PNG", colour=None):
    img = Image.new(mode, size, colour if colour is not None else 0)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


@pytest.mark.parametrize(
    "size,mode",
    [
        ((1, 1), "RGB"),
        ((3, 2), "RGBA"),
        ((640, 200), "L"),
        ((4000, 3000), "RGB"),
        ((1, 6000), "RGB"),
        ((6000, 1), "RGB"),
        ((50, 50), "P"),
        ((90, 400), "LA"),
    ],
)
def test_background_any_source_yields_640x200(size, mode):
    bg = _image_bytes(size, mode)
    out = render_card(_tiny_png_bytes(), "Yasuho", 3, (1, 2, 3), background_bytes=bg)
    with Image.open(out) as img:
        assert img.size == (640, 200)
        assert img.mode == "RGB"


def test_background_replaces_the_flat_colour_and_is_darkened():
    bg = _image_bytes((640, 200), "RGB", colour=(200, 200, 200))
    out = render_card(
        _tiny_png_bytes(), "Yasuho", 3, (255, 0, 0), background_bytes=bg
    )
    with Image.open(out) as img:
        r, g, b = img.getpixel((img.width - 1, 0))
    assert (r, g, b) != (255, 0, 0)
    assert 0 < r < 200  # the scrim darkened the photo, the card is not black


def test_corrupt_background_falls_back_to_the_flat_colour():
    out = render_card(
        _tiny_png_bytes(), "Yasuho", 3, (30, 40, 50), background_bytes=b"not an image"
    )
    with Image.open(out) as img:
        assert img.getpixel((img.width - 1, 0)) == (30, 40, 50)


def test_fill_template_is_literal_replacement_only():
    fill = welcome_card.fill_template
    assert fill("Hi {user} in {server} #{count}", "Ann", "Hub", 7) == "Hi Ann in Hub #7"
    assert fill("{0} {user.__class__} {}", "Ann", "Hub", 7) == "{0} {user.__class__} {}"
    assert fill("{server}", "x", "{user}", 1) == "{user}"  # no second pass
    assert fill("", "a", "b", 1) is None
    assert fill(None, "a", "b", 1) is None
    assert fill("  \n ", "a", "b", 1) is None


def test_fill_template_clamps_template_and_result():
    assert len(welcome_card.fill_template("x" * 500, "a", "b", 1)) == 60
    long = welcome_card.fill_template("{user}" * 10, "n" * 100, "s", 1)
    assert len(long) <= 120


def test_custom_lines_change_the_card_and_still_fit():
    base = render_card(_tiny_png_bytes(), "Yasuho", 3, (1, 2, 3)).getvalue()
    custom = render_card(
        _tiny_png_bytes(), "Yasuho", 3, (1, 2, 3), greeting="Hello there", subtitle="Enjoy"
    ).getvalue()
    assert base != custom
    huge = render_card(
        _tiny_png_bytes(), "Yasuho", 3, (1, 2, 3),
        greeting="W" * 60, subtitle="W" * 60,
    )
    with Image.open(huge) as img:
        assert img.size == (640, 200)


def test_text_colour_rides_on_the_style():
    pink = WelcomeStyle(ring=(255, 0, 255), text=(255, 0, 255))
    default = render_card(_tiny_png_bytes(), "Yasuho", 3, (1, 2, 3)).getvalue()
    tinted = render_card(_tiny_png_bytes(), "Yasuho", 3, (1, 2, 3), pink).getvalue()
    assert default != tinted


def test_fill_template_substitutes_in_a_single_pass():
    fill = welcome_card.fill_template
    assert fill("Hi {user}", "{server}", "Hub", 1) == "Hi {server}"
    assert fill("{count}", "{count}", "Hub", 9) == "9"


def test_fill_template_collapses_whitespace_after_substitution():
    out = welcome_card.fill_template("Hi {user}", "a\nb\tc", "Hub", 1)
    assert out == "Hi a b c"
    render_card(_tiny_png_bytes(), "x", 1, (1, 2, 3), greeting=out)  # must not raise


def test_oversized_background_falls_back_to_the_flat_colour(monkeypatch):
    monkeypatch.setattr(welcome_card.rank_card, "_pixel_ceiling", lambda: 100)
    bg = _image_bytes((50, 50), "RGB", colour=(200, 200, 200))
    assert welcome_card._cover_background(bg, (640, 200)) is None
    out = render_card(_tiny_png_bytes(), "Y", 1, (30, 40, 50), background_bytes=bg)
    with Image.open(out) as img:
        assert img.getpixel((img.width - 1, 0)) == (30, 40, 50)
