"""Card themes: the colours the rank and welcome renderers draw with.

A theme is plain data (frozen dataclasses), so the Pillow renderers stay pure:
they receive a style and never look anything up. :func:`for_guild` is the one
async seam both render paths call. It reads the guild's ``card_theme`` key from
the cached ``guild_settings`` blob (no extra query when warm) and the premium
cache (no I/O), and degrades to :data:`DEFAULT` on any failure - a cosmetic
lookup must never cost a member their card.

Only ``default`` is registered today; its literals are the ones the renderers
used before themes existed, so the default card is byte-identical (golden hash
tests in tests/cogs). A stored key whose premium has lapsed simply resolves to
the default; the stored value is never touched.

The server's accent and background image stay in ``rank_cards`` (see
cogs/community/leveling/rank_card.py) and compose on top of the theme.
"""

import dataclasses
import logging

from tools import premium, settings
from tools.i18n import N_

log = logging.getLogger(__name__)

# Key inside guild_settings (and what the dashboard writes).
SETTINGS_KEY = "card_theme"


@dataclasses.dataclass(frozen=True)
class RankStyle:
    # The scrim is painted over a custom background before any text is drawn:
    # the text colours are picked against the near-black panel, so a bright
    # photo would make them unreadable.
    panel: tuple = (28, 30, 38)
    name: tuple = (240, 242, 248)
    muted: tuple = (176, 182, 200)
    track: tuple = (58, 61, 74)
    scrim: tuple = (18, 19, 26)
    scrim_alpha: int = 150


@dataclasses.dataclass(frozen=True)
class WelcomeStyle:
    ring: tuple = (255, 255, 255)
    text: tuple = (255, 255, 255)
    stroke: tuple = (0, 0, 0, 160)


@dataclasses.dataclass(frozen=True)
class Theme:
    key: str
    label: str
    premium: bool
    rank: RankStyle = RankStyle()
    welcome: WelcomeStyle = WelcomeStyle()


DEFAULT = Theme("default", N_("Classic"), premium=False)
THEMES = {DEFAULT.key: DEFAULT}


def resolve(key, premium_ok):
    """The theme for a stored ``key``: unknown, or premium without
    ``premium_ok``, draws :data:`DEFAULT`."""
    theme = THEMES.get(key, DEFAULT)
    if theme.premium and not premium_ok:
        return DEFAULT
    return theme


async def for_guild(bot, guild_id):
    """The theme ``guild_id`` draws with (never raises)."""
    try:
        key = await settings.get_guild(bot.db_pool, guild_id, SETTINGS_KEY)
        premium_ok = premium.resolve_guild_limits(bot, guild_id).premium_card_themes
        return resolve(key, premium_ok)
    except Exception:
        log.warning("Card theme lookup failed for guild %s", guild_id, exc_info=True)
        return DEFAULT


def catalogue():
    """What the dashboard offers: ``[{"key", "label", "tier"}]``."""
    return [
        {
            "key": theme.key,
            "label": theme.label,
            "tier": "premium" if theme.premium else "free",
        }
        for theme in THEMES.values()
    ]
