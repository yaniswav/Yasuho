"""Runtime gettext i18n for Yasuho.

Compiled .mo catalogs live under locales/<locale>/LC_MESSAGES/yasuho.mo and are
loaded once at import. The active locale lives in a ContextVar set per command
invocation (see Yasuho.get_context in core.py), so concurrent commands in
different guilds never collide. English is the source language and the automatic
fallback for any untranslated string.

Mark user-facing strings with _("...") and interpolate with .format(...), NEVER
f-strings - an f-string is resolved before _() ever sees it, so the extractor
cannot capture a stable message id. Use ngettext(singular, plural, n) for counts.
"""

from __future__ import annotations

import contextvars
import gettext
import os
from glob import glob

DOMAIN = "yasuho"
DEFAULT_LOCALE = "en"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALE_DIR = os.path.join(BASE_DIR, "locales")

# Discover locales that actually ship a compiled catalog on disk.
_discovered = {
    os.path.basename(path)
    for path in glob(os.path.join(LOCALE_DIR, "*"))
    if os.path.isfile(os.path.join(path, "LC_MESSAGES", DOMAIN + ".mo"))
}

# Load every catalog once. The source language maps to NullTranslations, so an
# English message id is returned verbatim and acts as the universal fallback for
# anything not yet translated in another locale.
translations = {
    loc: gettext.translation(DOMAIN, languages=(loc,), localedir=LOCALE_DIR)
    for loc in _discovered
    if loc != DEFAULT_LOCALE
}
translations[DEFAULT_LOCALE] = gettext.NullTranslations()

# Locales offered by the language picker (always includes the default).
LOCALES = frozenset(translations) | {DEFAULT_LOCALE}

current_locale = contextvars.ContextVar("current_locale", default=DEFAULT_LOCALE)


def use_current_gettext(message):
    """Translate ``message`` against the locale of the current invocation."""
    catalog = translations.get(current_locale.get(), translations[DEFAULT_LOCALE])
    return catalog.gettext(message)


def use_current_ngettext(singular, plural, n):
    """Plural-aware translation against the current locale."""
    catalog = translations.get(current_locale.get(), translations[DEFAULT_LOCALE])
    return catalog.ngettext(singular, plural, n)


# Short aliases used throughout the cogs; pybabel is told to scan for these.
_ = use_current_gettext
ngettext = use_current_ngettext


def mark(message):
    """No-op extraction marker (N_).

    Tags a literal so pybabel collects it WITHOUT translating now. Use it for
    strings stored in module-level constants or default arguments (evaluated at
    import, outside any command task), then translate at the in-task use site
    with ``_(stored_value)``. Extraction must pass ``-k N_``.
    """
    return message


N_ = mark


def normalize(code):
    """Map a locale code (Discord 'en-US', 'pt-BR', ...) to a catalog we have.

    Tries the exact code, then the base language, then any catalog sharing the
    base language. Returns None when nothing matches.
    """
    if not code:
        return None
    code = str(code).replace("-", "_")
    if code in LOCALES:
        return code
    base = code.split("_", 1)[0]
    if base in LOCALES:
        return base
    for loc in LOCALES:
        if loc.split("_", 1)[0] == base:
            return loc
    return None


async def resolve_locale(bot, *, user_id, guild_id=None, interaction=None):
    """Resolve the locale for an invocation.

    Chain: per-user setting -> per-guild setting -> the user's Discord client
    locale -> the default. The first that maps to a real catalog wins. The
    settings reads are served from the in-memory settings cache after warmup.
    """
    from tools import settings  # local import avoids an import cycle

    loc = normalize(await settings.get_user(bot.db_pool, user_id, "locale", None))
    if loc:
        return loc
    if guild_id is not None:
        loc = normalize(await settings.get_guild(bot.db_pool, guild_id, "locale", None))
        if loc:
            return loc
    if interaction is not None:
        loc = normalize(getattr(interaction, "locale", None))
        if loc:
            return loc
    return DEFAULT_LOCALE
