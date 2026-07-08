"""Pure helpers for per-guild custom commands (name validation).

The cog owns the DB, the in-memory cache, the on_message dispatch and the
builder UI; this module only knows the naming rules, so they stay unit-testable
without a bot. It returns error KEYS (not prose) so the cog maps them to
translated messages.
"""

from __future__ import annotations

import re

MAX_COMMANDS_PER_GUILD = 50
MAX_NAME_LENGTH = 32
MAX_TEXT_LENGTH = 2000

# A name is one token: starts alphanumeric, then letters/digits/-/_ (no spaces).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_name(name):
    """Lowercase and strip a proposed command name (the stored/lookup form)."""
    return (name or "").strip().lower()


def validate_name(name, *, reserved, existing):
    """Return an error key if ``name`` is unusable, else None. Pure, no i18n.

    ``reserved`` is the set of names already taken by real bot commands (and
    aliases); ``existing`` is this guild's current custom command names. Both
    lowercase. Error keys: empty / too_long / bad_chars / reserved / exists.
    """
    if not name:
        return "empty"
    if len(name) > MAX_NAME_LENGTH:
        return "too_long"
    if not _NAME_RE.match(name):
        return "bad_chars"
    if name in reserved:
        return "reserved"
    if name in existing:
        return "exists"
    return None
