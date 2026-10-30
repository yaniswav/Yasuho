"""Pure helpers for per-guild custom commands (name validation).

The cog owns the DB, the in-memory cache, the on_message dispatch and the
builder UI; this module only knows the naming rules, so they stay unit-testable
without a bot. It returns error KEYS (not prose) so the cog maps them to
translated messages.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_COMMANDS_PER_GUILD = 50
MAX_NAME_LENGTH = 32
MAX_TEXT_LENGTH = 2000
# Discord's own limits: a link button label, its URL, and an embed's summed text.
MAX_BUTTONS = 3
MAX_BUTTON_LABEL = 80
MAX_BUTTON_URL = 512
MAX_EMBED_TOTAL = 6000

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


def clean_button(label, url):
    """Return ``{"label", "url"}`` when both are usable, else None.

    Link buttons only: the URL must be https with a host and no whitespace, the
    label 1..80 characters. Used at input AND on read, because the dashboard
    may write this table too.
    """
    if not isinstance(label, str) or not isinstance(url, str):
        return None
    label, url = label.strip(), url.strip()
    if not label or len(label) > MAX_BUTTON_LABEL:
        return None
    if not url or len(url) > MAX_BUTTON_URL or re.search(r"\s", url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. "https://[::1": junk must read as invalid, never raise
        return None
    if parts.scheme != "https" or not parts.netloc:
        return None
    return {"label": label, "url": url}


def read_buttons(blob, limit):
    """The stored buttons that are still valid, capped at ``limit`` (read side)."""
    if limit <= 0 or not isinstance(blob, list):
        return []
    out = []
    for item in blob:
        if not isinstance(item, dict):
            continue
        button = clean_button(item.get("label"), item.get("url"))
        if button is not None:
            out.append(button)
    return out[: min(limit, MAX_BUTTONS)]


def parse_buttons(raw):
    """Parse one `label | https://url` per line into ``(buttons, error_key)``.

    Error keys: too_many / format / label / url. Blank lines are skipped and an
    empty input is a valid "no buttons".
    """
    buttons = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        label, sep, url = line.rpartition("|")
        if not sep:
            return None, "format"
        if not label.strip() or len(label.strip()) > MAX_BUTTON_LABEL:
            return None, "label"
        button = clean_button(label, url)
        if button is None:
            return None, "url"
        buttons.append(button)
    if len(buttons) > MAX_BUTTONS:
        return None, "too_many"
    return buttons, None
