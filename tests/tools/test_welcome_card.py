"""Tests for tools.welcome_card.render_card.

render_card is pure Pillow: given avatar bytes, a display name, a member count
and a background colour, it must return a PNG BytesIO that opens as a 640x200
RGB image. These tests build a tiny in-memory PNG for the avatar so nothing
touches the network, disk fixtures, Discord, or a database.
"""

import io

from PIL import Image

from tools.welcome_card import render_card


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
