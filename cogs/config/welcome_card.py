"""Pure Pillow rendering for the welcome card.

This module owns the image-drawing half of the welcome system: given plain
values (avatar bytes, a display name, a member count, a background colour) it
returns a PNG ``BytesIO``. It deliberately knows nothing about discord or the
cog so the rendering stays testable and side-effect free; the cog fetches the
avatar bytes and delegates here, then ships the buffer as a discord.File.
"""

import io

from PIL import Image, ImageDraw, ImageFont

# Reuse a TTF already shipped with the bot (see cogs/fun/fun.py); fall back to
# PIL's bitmap default if the file is missing so a render never hard-fails.
_FONT_PATH = "ressources/fonts/impact.ttf"


def _load_fonts():
    """Return (title_font, sub_font), falling back to PIL's default font."""

    try:
        title_font = ImageFont.truetype(_FONT_PATH, size=38)
        sub_font = ImageFont.truetype(_FONT_PATH, size=24)
    except Exception:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()
    return title_font, sub_font


def render_card(avatar_bytes, display_name, member_count, bg_rgb):
    """Render a welcome card and return it as a PNG ``BytesIO``.

    Parameters are plain values so this function is pure and never touches the
    event loop. ``avatar_bytes`` is the raw avatar image data, ``display_name``
    the greeting target, ``member_count`` the member ordinal, and ``bg_rgb`` an
    (r, g, b) tuple for the card background.
    """

    width, height = 640, 200
    size = 128
    ring = 6
    card = Image.new("RGBA", (width, height), bg_rgb + (255,))
    draw = ImageDraw.Draw(card)

    # Avatar drawn in a circle, with a white ring behind it. The mask is
    # built at 4x then downscaled so the circle edge stays smooth.
    avatar = (
        Image.open(io.BytesIO(avatar_bytes))
        .convert("RGBA")
        .resize((size, size), Image.LANCZOS)
    )
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    avatar_x = 36
    avatar_y = (height - size) // 2
    draw.ellipse(
        (
            avatar_x - ring,
            avatar_y - ring,
            avatar_x + size + ring,
            avatar_y + size + ring,
        ),
        fill=(255, 255, 255, 255),
    )
    card.paste(avatar, (avatar_x, avatar_y), mask)

    title_font, sub_font = _load_fonts()

    text_x = avatar_x + size + ring + 28
    available = width - text_x - 24

    # Shrink the greeting until it fits the remaining width so long
    # display names never overflow the card.
    name = display_name
    welcome_text = f"Welcome {name}!"
    while name and draw.textlength(welcome_text, font=title_font) > available:
        name = name[:-1]
        welcome_text = f"Welcome {name.rstrip()}...!"

    draw.text(
        (text_x, 60),
        welcome_text,
        font=title_font,
        fill=(255, 255, 255, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 160),
    )
    draw.text(
        (text_x, 112),
        f"Member #{member_count}",
        font=sub_font,
        fill=(255, 255, 255, 255),
        stroke_width=1,
        stroke_fill=(0, 0, 0, 160),
    )

    buf = io.BytesIO()
    card.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf
