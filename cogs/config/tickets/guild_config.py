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

Corollary on the WRITE side: resetting a key DELETES it (:func:`set_key`), it
never stores a "neutral" value. See that function for why, and
``.claude/plans/dashboard-tickets-contract.md`` for the identical rule the
dashboard is held to.

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

# How long a ticket may sit idle before it closes (int hours, 1..168, then
# rounded UP to a duration Discord accepts - see :func:`snap_inactivity_hours`).
# It IS the thread's auto-archive duration: the room goes quiet, Discord archives
# it after exactly this long, and the archive is what closes the ticket
# (cogs/config/tickets/lifecycle.py). The hourly sweep reads the same number as
# its backstop age cut.
KEY_INACTIVITY_HOURS = "tickets_inactivity_hours"

# The blurb shown on the panel above the button (optional free text).
KEY_PANEL_MESSAGE = "tickets_panel_message"

# Every key this module owns, in the order the surfaces display them; the
# dashboard writes these and nothing else under the tickets kind. ORDERED
# because :func:`read_raw` builds a dict from it and a frozenset's iteration
# order is not stable across processes - a panel whose lines shuffle between
# restarts would be a bug nobody could reproduce.
KEY_ORDER = (
    KEY_PANEL_CHANNEL,
    KEY_SUPPORT_ROLE,
    KEY_LOG_CHANNEL,
    KEY_MAX_OPEN_PER_USER,
    KEY_INACTIVITY_HOURS,
    KEY_PANEL_MESSAGE,
)

KEYS = frozenset(KEY_ORDER)


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
# auto-archive, and a ticket's inactivity window IS that duration: the thread
# archives after that much silence, and the archive is what closes the ticket.
# So those four durations are the window's whole vocabulary, in hours - which is
# also why they are the presets the picker offers.
INACTIVITY_PRESET_HOURS = (1, 24, 72, 168)

_AUTO_ARCHIVE_MINUTES_BY_HOURS = {1: 60, 24: 1440, 72: 4320, 168: 10080}

# The house default, in minutes: three days, i.e. DEFAULT_INACTIVITY_HOURS. Long
# enough that a weekend cannot archive a live ticket under its participants,
# short enough that an abandoned one leaves the channel list on its own even if
# the bot is down.
AUTO_ARCHIVE_MINUTES = _AUTO_ARCHIVE_MINUTES_BY_HOURS[DEFAULT_INACTIVITY_HOURS]

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


def snap_inactivity_hours(hours: int) -> int:
    """Round a window UP to the next window Discord can actually enforce. Pure.

    The picker only ever offers :data:`INACTIVITY_PRESET_HOURS`, but the
    dashboard is NOT bound by that list - the contract lets it write any integer
    in ``MIN..MAX_INACTIVITY_HOURS`` - and Discord accepts exactly four
    auto-archive durations. So a stored 100 has to become one of the four.

    UP, never down, and that direction is the whole point: a window resolved
    DOWN would archive - and therefore CLOSE - a ticket earlier than the server
    asked, in the middle of somebody's conversation, while one resolved up only
    keeps a dead room a few more hours before the same archive ends it. The
    surfaces show what this returns, so the number an admin reads is the number
    the ticket is held to.
    """
    for preset in INACTIVITY_PRESET_HOURS:
        if hours <= preset:
            return preset
    return INACTIVITY_PRESET_HOURS[-1]


def auto_archive_minutes(hours: int) -> int:
    """The ``auto_archive_duration`` a ticket thread is created with. Pure.

    This is what makes the inactivity window a real control rather than a label:
    the window IS the thread's auto-archive duration, so Discord enforces it for
    free and no ticket needs a timer.
    """
    return _AUTO_ARCHIVE_MINUTES_BY_HOURS[snap_inactivity_hours(hours)]


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
    """Idle hours before a ticket closes: always one of the four presets.

    Clamped into ``MIN..MAX_INACTIVITY_HOURS`` and then SNAPPED, so every
    surface, the thread's auto-archive duration and the sweep's age cut all act
    on the same number - a guild cannot be shown 100h and archived at 168h.
    """
    return snap_inactivity_hours(
        coerce_count(
            await _read(pool, guild_id, KEY_INACTIVITY_HOURS),
            minimum=MIN_INACTIVITY_HOURS,
            maximum=MAX_INACTIVITY_HOURS,
            default=DEFAULT_INACTIVITY_HOURS,
        )
    )


async def panel_message(pool: typing.Any, guild_id: typing.Any):
    """The custom panel blurb, or ``None`` when the guild kept the default."""
    return coerce_text(
        await _read(pool, guild_id, KEY_PANEL_MESSAGE),
        limit=MAX_PANEL_MESSAGE_LENGTH,
    )


# ---------------------------------------------------------------------------
# Whole-configuration read + write, for the surfaces that show or edit ALL six
# keys at once (``/ticket status``, ``/ticket config``).
# ---------------------------------------------------------------------------


async def read_raw(pool: typing.Any, guild_id: typing.Any):
    """Every ticket key of this guild, RAW and uncoerced, or ``None``.

    One settings-blob read shared by all six keys (the first ``get_guild`` loads
    the row, the other five are dict lookups on the same cached blob), which is
    what lets a panel show six values for the price of one.

    RAW on purpose: the coerced readers above cannot tell "this guild set 2" from
    "this guild set nothing and the default is 2", and an editing surface has to,
    or its reset control has nothing to reset and its labels lie about which
    values are the server's. Coerce with :func:`resolve`.

    Returns ``None`` - NOT a map of ``None`` - when the read fails, because those
    two answers mean opposite things here: a map of ``None`` is "this guild is
    unconfigured", and rendering a failed read that way would show a configured
    server an empty panel and invite it to re-enter everything.
    """
    if pool is None or guild_id is None:
        return None
    try:
        return {
            key: await settings.get_guild(pool, guild_id, key) for key in KEY_ORDER
        }
    except Exception as exc:
        # WARNING without a traceback, same reasoning as _read: these run at
        # command frequency and the repr says what failed.
        log.warning(
            "tickets guild config: failed to read the settings of guild %s: %r",
            guild_id,
            exc,
        )
        return None


def resolve(raw: typing.Any) -> dict:
    """Coerce a :func:`read_raw` map into the values the bot acts on. Pure.

    Accepts ``None`` (a failed read) and anything without the keys, and answers
    with the bot defaults - the same degradation the per-key readers apply, so a
    surface built on this behaves exactly like one built on them.
    """
    if not isinstance(raw, dict):
        raw = {}
    return {
        "panel_channel": coerce_id(raw.get(KEY_PANEL_CHANNEL)),
        "support_role": coerce_id(raw.get(KEY_SUPPORT_ROLE)),
        "log_channel": coerce_id(raw.get(KEY_LOG_CHANNEL)),
        "max_open": coerce_count(
            raw.get(KEY_MAX_OPEN_PER_USER),
            minimum=MIN_OPEN_PER_USER,
            maximum=MAX_OPEN_PER_USER,
            default=DEFAULT_MAX_OPEN_PER_USER,
        ),
        "inactivity_hours": snap_inactivity_hours(
            coerce_count(
                raw.get(KEY_INACTIVITY_HOURS),
                minimum=MIN_INACTIVITY_HOURS,
                maximum=MAX_INACTIVITY_HOURS,
                default=DEFAULT_INACTIVITY_HOURS,
            )
        ),
        "panel_message": coerce_text(
            raw.get(KEY_PANEL_MESSAGE), limit=MAX_PANEL_MESSAGE_LENGTH
        ),
    }


# ``settings.set_guild`` patches ONE key with ``jsonb_set``; there is no such
# helper for REMOVING one, so the delete below is the only statement this package
# issues against ``guild_settings`` itself. It is the exact statement the
# dashboard contract prescribes for a reset, so both writers leave the row in the
# same state. Scoped by key: a sibling feature sharing this row is untouched.
_CLEAR_KEY_SQL = (
    "UPDATE guild_settings SET settings = settings - $2::text WHERE guild_id = $1"
)


async def set_key(pool: typing.Any, guild_id: typing.Any, key: str, value: typing.Any):
    """Write ONE ticket key - or DELETE it when ``value`` is ``None``.

    The single write seam of this package, and the reason it exists is the
    "absent means default" rule the whole module is built on: storing a JSON
    ``null`` to mean "unset" would leave the guild configured-with-nothing, which
    reads the same TODAY (every coercion above maps ``null`` to the default) but
    stops being true the day a default changes, and shows up on the dashboard as
    a key somebody set. So a reset REMOVES the key and a guild that resets every
    control is byte-identical to one that never touched the feature.

    The delete goes straight to the row, outside ``tools.settings``, so the LRU
    is invalidated right after - the same out-of-band write-then-invalidate pair
    ``tools.privacy.set_avatar_tracking`` uses. Invalidation is coarse (the whole
    guild blob) and read-through, so the only cost is one re-read.
    """
    if value is None:
        await pool.execute(_CLEAR_KEY_SQL, guild_id, key)
        settings.invalidate_guild(guild_id)
        return
    await settings.set_guild(pool, guild_id, key, value)
