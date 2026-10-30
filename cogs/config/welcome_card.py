"""Pure Pillow rendering for the welcome card.

This module owns the image-drawing half of the welcome system: given plain
values (avatar bytes, a display name, a member count, a background colour) it
returns a PNG ``BytesIO``. It deliberately knows nothing about discord or the
cog so the rendering stays testable and side-effect free; the cog fetches the
avatar bytes and delegates here, then ships the buffer as a discord.File.
"""

import io
import re

from PIL import Image, ImageDraw, ImageFont, ImageOps

from cogs.community.leveling import rank_card
from tools import card_themes

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


# Yasuho+ custom lines: bounded on input (modal, dashboard) and again on render.
MAX_LINE_LENGTH = 60
# Cap of the filled line, so a long display name cannot blow the text up.
_MAX_FILLED_LENGTH = 120
# Darkening laid over a custom background so the text stays readable.
_BACKGROUND_SCRIM = (0, 0, 0, 140)


_PLACEHOLDER = re.compile(r"\{(user|server|count)\}")


def fill_template(template, user, server, count):
    """Fill a custom line: literal one-pass substitution of {user}/{server}/{count}.

    Never ``str.format``: the template is user text, so "{0}" or
    "{user.__class__}" must come out literally. Returns ``None`` for an empty
    template (the caller draws the stock line).
    """

    text = " ".join(str(template or "").split())[:MAX_LINE_LENGTH]
    if not text:
        return None
    values = {"user": str(user), "server": str(server), "count": str(count)}
    # One pass: a substituted value is never scanned for placeholders again.
    text = _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    # Collapse after substitution: a newline in a name would make Pillow's
    # textlength raise on multiline text.
    return " ".join(text.split())[:_MAX_FILLED_LENGTH] or None


def _fit(draw, text, font, available):
    """Trim ``text`` with an ellipsis until it fits ``available`` pixels."""

    base = text
    while base and draw.textlength(text, font=font) > available:
        base = base[:-1]
        text = base.rstrip() + "..."
    return text


def _cover_background(background_bytes, size):
    """The stored image cover-fitted to ``size`` under a dark scrim, or None.

    ``ImageOps.fit`` crops before it resizes, so an odd-aspect source never
    builds a huge intermediate. Any decode failure means "no background".
    """

    try:
        with Image.open(io.BytesIO(background_bytes)) as source:
            if source.width * source.height > rank_card._pixel_ceiling():
                return None
            fitted = ImageOps.fit(
                source.convert("RGBA"), size, Image.LANCZOS, centering=(0.5, 0.5)
            )
    except Exception:
        return None
    return Image.alpha_composite(fitted, Image.new("RGBA", size, _BACKGROUND_SCRIM))


def render_card(
    avatar_bytes,
    display_name,
    member_count,
    bg_rgb,
    style=card_themes.DEFAULT.welcome,
    background_bytes=None,
    greeting=None,
    subtitle=None,
):
    """Render a welcome card and return it as a PNG ``BytesIO``.

    Parameters are plain values so this function is pure and never touches the
    event loop. ``avatar_bytes`` is the raw avatar image data, ``display_name``
    the greeting target, ``member_count`` the member ordinal, and ``bg_rgb`` an
    (r, g, b) tuple for the card background. The last three are the Yasuho+
    extras, all optional: stored background bytes (drawn instead of the flat
    colour), and already-filled ``greeting`` / ``subtitle`` lines (see
    :func:`fill_template`). The text colour rides on ``style``.
    """

    width, height = 640, 200
    size = 128
    ring = 6
    card = None
    if background_bytes:
        card = _cover_background(background_bytes, (width, height))
    if card is None:
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
        fill=style.ring + (255,),
    )
    card.paste(avatar, (avatar_x, avatar_y), mask)

    title_font, sub_font = _load_fonts()

    text_x = avatar_x + size + ring + 28
    available = width - text_x - 24

    # Shrink the greeting until it fits the remaining width so long
    # display names never overflow the card.
    name = display_name
    welcome_text = f"Welcome {name}!"
    if greeting:
        welcome_text = _fit(draw, greeting, title_font, available)
    else:
        while name and draw.textlength(welcome_text, font=title_font) > available:
            name = name[:-1]
            welcome_text = f"Welcome {name.rstrip()}...!"
    sub_text = f"Member #{member_count}"
    if subtitle:
        sub_text = _fit(draw, subtitle, sub_font, available)

    draw.text(
        (text_x, 60),
        welcome_text,
        font=title_font,
        fill=style.text + (255,),
        stroke_width=2,
        stroke_fill=style.stroke,
    )
    draw.text(
        (text_x, 112),
        sub_text,
        font=sub_font,
        fill=style.text + (255,),
        stroke_width=1,
        stroke_fill=style.stroke,
    )

    buf = io.BytesIO()
    card.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf
