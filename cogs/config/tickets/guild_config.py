"""Per-guild support-ticket configuration: the keys the dashboard writes and
the bot reads.

Storage. The six keys live INSIDE the ``guild_settings`` JSONB blob (the same
blob welcome / automod / music already share), read through
``tools.settings.get_guild``. That store is a size-bounded in-process LRU the
dashboard invalidates through the ``yasuho_dashboard`` NOTIFY channel, so the
readers below add NO hot-path database traffic: at most one row fetch per guild
per eviction, shared by all six keys because they ride one blob.

Absent means DISABLED (or the bot default). A key that was never written is
ABSENT from the blob, and absence resolves to "this guild has no tickets" - never
to a materialised default row. A guild nobody configured must behave exactly as
it did before this module existed, which is why ``panel_channel_id`` returning
``None`` is the single authority on "the feature is off here": the button reads
it at CLICK time and answers "not set up" rather than trusting whatever message
it happens to be attached to.

Untrusted input. The values come from another process writing the same database.
Even though that process is our own dashboard, nothing here trusts the stored
shape: ids go through ``tools.snowflake.coerce_id`` (JS serialises snowflakes as
strings, and ``guild.get_channel("123")`` silently returns ``None``), counts are
CLAMPED into the bounds the commands themselves enforce rather than refused, and
anything unusable falls back to the default. A settings read that raises is
logged and degrades to the default too: a broken configuration read must never
turn into a broken button.

Layering: a LEAF of the tickets package (it imports only ``tools.settings`` and
``tools.snowflake``), so every other module here can import it without forming a
cycle and it is unit-testable with no database and no Discord.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import math
import typing

from tools import settings
from tools.snowflake import coerce_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The key set (v1). Adding a key here is a contract change: mirror it in the
# dashboard contract before shipping.
# ---------------------------------------------------------------------------

# The channel the panel lives on. ALSO the on/off switch: absent = no tickets in
# this guild, whatever buttons may still be sitting in old messages.
KEY_PANEL_CHANNEL = "tickets_panel_channel"

# Role pinged inside a freshly opened ticket (optional).
KEY_SUPPORT_ROLE = "tickets_support_role"

# Where ticket lifecycle lines are posted (optional; consumed by lot T2).
KEY_LOG_CHANNEL = "tickets_log_channel"

# How many tickets ONE member may have open at once (int, clamped 1..5).
KEY_MAX_OPEN_PER_USER = "tickets_max_open_per_user"

# How long a ticket may sit idle before the sweep closes it (int hours, 1..168).
# Written and shown here; the sweep that consumes it lands in lot T2.
KEY_INACTIVITY_HOURS = "tickets_inactivity_hours"

# The blurb shown on the panel above the button (optional free text).
KEY_PANEL_MESSAGE = "tickets_panel_message"

# Every key this module owns; the dashboard writes these and nothing else under
# the tickets kind.
KEYS = frozenset(
    {
        KEY_PANEL_CHANNEL,
        KEY_SUPPORT_ROLE,
        KEY_LOG_CHANNEL,
        KEY_MAX_OPEN_PER_USER,
        KEY_INACTIVITY_HOURS,
        KEY_PANEL_MESSAGE,
    }
)


# Bounds and bot defaults. An out-of-range stored value is CLAMPED, not refused:
# a value outside these is a dashboard bug, and the useful behaviour for the
# server is the nearest legal setting, not silently falling back to the default.
MIN_OPEN_PER_USER = 1
MAX_OPEN_PER_USER = 5
DEFAULT_MAX_OPEN_PER_USER = 2

MIN_INACTIVITY_HOURS = 1
MAX_INACTIVITY_HOURS = 168  # one week
DEFAULT_INACTIVITY_HOURS = 72

# Discord accepts exactly 60 / 1440 / 4320 / 10080 minutes for a thread's
# auto-archive. 4320 (three days) is the house default: long enough that a
# weekend cannot archive a live ticket under its participants, short enough that
# an abandoned one leaves the channel list on its own even if the bot is down.
# NOT a guild key - it is a property of how a ticket thread is created, not a
# server preference, and the two knobs a server does get over ticket lifetime
# (the cap and the inactivity window) are above.
AUTO_ARCHIVE_MINUTES = 4320

# The subject the opener types. Bounded here rather than at the modal so the
# storage side and the UI cannot drift: it is written into the thread's opening
# message and NEVER into the database.
MAX_SUBJECT_LENGTH = 200

# The panel blurb. Kept well inside the embed description limit so a stored value
# can never make the panel unpostable.
MAX_PANEL_MESSAGE_LENGTH = 1000


# ---------------------------------------------------------------------------
# Pure coercion (no database, no Discord) - the untrusted-payload boundary.
# ---------------------------------------------------------------------------


def coerce_count(raw: typing.Any, *, minimum: int, maximum: int, default: int) -> int:
    """Read a stored whole number, CLAMPED into ``minimum..maximum``.

    Anything that is not a usable number (absent key, wrong type, non-numeric
    string, NaN/inf) resolves to ``default``. ``bool`` is rejected on purpose: it
    is an ``int`` subclass in Python, and a stray toggle must never read as the
    count 1. Pure.
    """
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            return default
        value = int(raw)
    elif isinstance(raw, str):
        try:
            parsed = float(raw.strip())
        except (TypeError, ValueError):
            return default
        if not math.isfinite(parsed):
            return default
        value = int(parsed)
    else:
        return default
    return max(minimum, min(maximum, value))


def coerce_text(raw: typing.Any, *, limit: int) -> typing.Optional[str]:
    """Read a stored free-text blurb, trimmed to ``limit``, or ``None``.

    Whitespace-only is ``None`` (the same answer as an absent key: there is
    nothing to show), so a blurb cleared to spaces on the dashboard does not
    render as an empty line on the panel. Pure.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return text[:limit]


# ---------------------------------------------------------------------------
# Readers. One LRU-backed settings read each; a failure degrades to the default.
# ---------------------------------------------------------------------------


async def _read(pool: typing.Any, guild_id: typing.Any, key: str) -> typing.Any:
    """Fetch one raw key from the guild's settings blob, or ``None``.

    Never raises: no pool / no guild (the shapes the unit fakes and a DM-context
    caller present) and a settings failure all resolve to ``None``, i.e. to the
    caller's default.

    Logged at WARNING WITHOUT a traceback on purpose. These readers run at button
    frequency, so a Postgres outage would otherwise print one full stack per
    click per guild - a flood that buries the one line that matters. The repr of
    the exception stays inline, so the line still says WHAT failed alongside the
    key and the guild.
    """
    if pool is None or guild_id is None:
        return None
    try:
        return await settings.get_guild(pool, guild_id, key)
    except Exception as exc:
        log.warning(
            "tickets guild config: failed to read %s for guild %s: %r",
            key,
            guild_id,
            exc,
        )
        return None


async def panel_channel_id(pool: typing.Any, guild_id: typing.Any):
    """The configured panel channel id, or ``None`` when tickets are off here.

    THE feature switch. ``None`` means every ticket button in this guild answers
    "not set up here", including buttons on panels posted before it was cleared.
    """
    return coerce_id(await _read(pool, guild_id, KEY_PANEL_CHANNEL))


async def support_role_id(pool: typing.Any, guild_id: typing.Any):
    """The role pinged in a new ticket, or ``None`` when the guild set none."""
    return coerce_id(await _read(pool, guild_id, KEY_SUPPORT_ROLE))


async def log_channel_id(pool: typing.Any, guild_id: typing.Any):
    """The ticket log channel id, or ``None`` when the guild set none."""
    return coerce_id(await _read(pool, guild_id, KEY_LOG_CHANNEL))


async def max_open_per_user(pool: typing.Any, guild_id: typing.Any) -> int:
    """How many tickets one member may have open at once (clamped 1..5)."""
    return coerce_count(
        await _read(pool, guild_id, KEY_MAX_OPEN_PER_USER),
        minimum=MIN_OPEN_PER_USER,
        maximum=MAX_OPEN_PER_USER,
        default=DEFAULT_MAX_OPEN_PER_USER,
    )


async def inactivity_hours(pool: typing.Any, guild_id: typing.Any) -> int:
    """Idle hours before a ticket is swept closed (clamped 1..168)."""
    return coerce_count(
        await _read(pool, guild_id, KEY_INACTIVITY_HOURS),
        minimum=MIN_INACTIVITY_HOURS,
        maximum=MAX_INACTIVITY_HOURS,
        default=DEFAULT_INACTIVITY_HOURS,
    )


async def panel_message(pool: typing.Any, guild_id: typing.Any):
    """The custom panel blurb, or ``None`` when the guild kept the default."""
    return coerce_text(
        await _read(pool, guild_id, KEY_PANEL_MESSAGE),
        limit=MAX_PANEL_MESSAGE_LENGTH,
    )
