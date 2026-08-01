"""Per-guild music configuration: the keys the dashboard writes and the bot reads.

The music feature had no server-level configuration at all: every guild got the
same starting volume, the same autoplay default, the same vote-skip policy, the
same "DJ or Manage Server" privilege rule and the same SponsorBlock behaviour.
This module is the ONE place that turns a stored per-guild value into a decision
the cog can act on.

Storage. The five keys live INSIDE the ``guild_settings`` JSONB blob (the same
blob welcome / automod / modlog_events / warn_escalation already share), read
through ``tools.settings.get_guild``. That store is a size-bounded in-process LRU
that the dashboard already invalidates through the ``yasuho_dashboard`` NOTIFY
channel (kind ``music_config``, see ``cogs/system/dashboard_sync.py``), so the
readers below add NO hot-path database traffic: at most one row fetch per guild
per eviction, shared by all five keys because they ride one blob.

Absent means default. A key that was never written is ABSENT from the blob, and
absence resolves to the bot's historical behaviour - never to a materialised
default row. That is the acceptance bar for this lot: a guild the dashboard has
never touched behaves exactly as it did before this module existed. It is also
why the bool coercion takes the default as an argument and treats ``None``
(absent) differently from ``False`` (explicitly turned off).

Untrusted input. The values come from another process writing the same database.
Even though that process is our own dashboard, nothing here trusts the stored
shape: every reader runs a pure coercer that accepts the JSON shapes a Node
writer plausibly produces (JS serialises large ids as strings), CLAMPS a
numeric volume into the bounds the ``/volume`` command itself enforces rather
than refusing it, and falls back to the default for anything it cannot make
sense of. A settings read that raises is logged and degrades to the default too:
a broken configuration read must never take music down.

Layering: a LEAF of the music package (it imports only ``tools.settings``), so
``music.py`` importing it can never form a cycle and it is unit-testable with no
database, no Discord and no sonolink.
"""

from __future__ import annotations

import logging
import math
import typing

from tools import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The key set (v1). Adding a key here is a contract change: mirror it in
# .claude/plans/dashboard-executors-contract-d.md before shipping.
# ---------------------------------------------------------------------------

# Volume a BRAND-NEW player starts at, before its first track (int, 0..200).
KEY_DEFAULT_VOLUME = "music_default_volume"

# Default autoplay mode for a NEW session (bool). Deliberately the SAME string as
# the per-user preference key in ``cogs/music/music.py`` (AUTOPLAY_PREF_KEY) and
# ``cogs/community/usersettings.py``: they live in DIFFERENT tables
# (``user_settings`` vs ``guild_settings``), so there is no collision, and using
# one name keeps "the autoplay setting" recognisable on both sides. Precedence is
# resolved in ``music.resolve_session_autoplay``: an explicit personal preference
# wins, this guild default fills in when the member has none, ON if neither.
KEY_AUTOPLAY = "music_autoplay"

# Whether a non-privileged /skip opens a public vote (bool, default True).
KEY_VOTESKIP = "music_voteskip"

# Role whose members get the same music privileges as Manage Server (role id).
KEY_DJ_ROLE = "music_dj_role"

# Whether SponsorBlock segment skipping is armed on new players (bool, True).
KEY_SPONSORBLOCK = "music_sponsorblock"

# Every key this module owns; the dashboard writes these and nothing else under
# the ``music_config`` kind.
KEYS = frozenset(
    {
        KEY_DEFAULT_VOLUME,
        KEY_AUTOPLAY,
        KEY_VOTESKIP,
        KEY_DJ_ROLE,
        KEY_SPONSORBLOCK,
    }
)


# The volume bounds. These are the bounds the ``/volume`` command itself declares
# (``value: commands.Range[int, 0, 200]`` in ``cogs/music/music.py``), so a
# dashboard-configured default can never reach a level a member could not ask for
# in Discord. Kept numerically identical to
# ``cogs.system.dashboard_music_actions.MIN_VOLUME`` / ``MAX_VOLUME`` (the live
# remote-control executor), pinned by a test so the two can never drift.
MIN_VOLUME = 0
MAX_VOLUME = 200

# Bot defaults for the two booleans, i.e. what an ABSENT key resolves to. Both
# restate today's behaviour: every non-privileged skip opens a vote in a room of
# more than two humans (``cogs/music/voteskip.py``), and every player born gets
# the SponsorBlock categories PUT to the node (``cogs/music/sponsorblock.py``).
DEFAULT_VOTESKIP = True
DEFAULT_SPONSORBLOCK = True


# String spellings a JSON writer might use for a boolean. Anything outside these
# (and outside the real bool / number shapes) resolves to the caller's default.
_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "f", "no", "n", "off"})


# ---------------------------------------------------------------------------
# Pure coercion (no database, no Discord) - the untrusted-payload boundary.
# ---------------------------------------------------------------------------


def coerce_bool(raw: typing.Any, default: typing.Any) -> typing.Any:
    """Read a stored boolean, returning ``default`` for absent or unusable input.

    ABSENT (``None``) is distinct from ``False``: the first means "never
    configured" and yields ``default``, the second means "explicitly turned off"
    and yields ``False``. Accepts real bools, numbers, and the usual string
    spellings, because the writer is a Node process and JSON shapes vary. Pure.

    ``default`` is returned as given, so a caller wanting a TRI-STATE (absent /
    on / off) passes ``None`` and gets ``None`` back for an unset key.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, float):
        # Non-finite is not a boolean anybody meant to store, and NaN is truthy
        # in Python: falling back to the default keeps this coercer's verdict on
        # a garbage payload identical to coerce_volume's.
        return bool(raw) if math.isfinite(raw) else default
    if isinstance(raw, int):
        return bool(raw)
    if isinstance(raw, str):
        needle = raw.strip().lower()
        if needle in _TRUE_STRINGS:
            return True
        if needle in _FALSE_STRINGS:
            return False
    return default


def coerce_volume(raw: typing.Any) -> typing.Optional[int]:
    """Read a stored volume, CLAMPED into ``MIN_VOLUME..MAX_VOLUME``, or ``None``.

    Clamping rather than rejecting is deliberate: a value out of bounds is a
    dashboard bug, and the useful behaviour for the room is the nearest legal
    level, not silently falling back to 100. ``None`` is reserved for "no usable
    value at all" (absent key, wrong type, non-numeric string), which the caller
    reads as "leave the player's own default alone". Pure.

    ``bool`` is rejected on purpose (it is an ``int`` subclass in Python, and
    ``True`` is not a volume).
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        value = int(raw)
    elif isinstance(raw, str):
        try:
            parsed = float(raw.strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        value = int(parsed)
    else:
        return None
    return max(MIN_VOLUME, min(MAX_VOLUME, value))


def coerce_role_id(raw: typing.Any) -> typing.Optional[int]:
    """Read a stored role id, or ``None`` when there is no usable one.

    Accepts an int or a numeric string (JS serialises snowflakes as strings).
    Zero and negatives are not snowflakes, so they read as "no role" rather than
    as a role nobody holds. Pure.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return value if value > 0 else None


def member_has_role(member: typing.Any, role_id: typing.Optional[int]) -> bool:
    """True when ``member`` currently holds ``role_id``. Pure, duck-typed, safe.

    Prefers ``discord.Member.get_role`` (an O(1) dict lookup) and falls back to
    scanning ``.roles`` for the stand-ins the tests drive. A ``None`` role id (no
    DJ role configured) is False for everyone, which is what keeps an unconfigured
    guild's privilege rule byte-identical to before.
    """
    if role_id is None or member is None:
        return False
    get_role = getattr(member, "get_role", None)
    if callable(get_role):
        try:
            return get_role(role_id) is not None
        except Exception:
            return False
    roles = getattr(member, "roles", None) or ()
    try:
        return any(getattr(role, "id", None) == role_id for role in roles)
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Readers. One LRU-backed settings read each; a failure degrades to the default.
# ---------------------------------------------------------------------------


async def _read(pool: typing.Any, guild_id: typing.Any, key: str) -> typing.Any:
    """Fetch one raw key from the guild's settings blob, or ``None``.

    Never raises: no pool / no guild (the shapes the unit fakes and a DM-context
    caller present) and a settings failure all resolve to ``None``, i.e. to the
    caller's default. Configuration must never be able to break playback.

    Logged at WARNING WITHOUT a traceback on purpose. These readers run at button
    and command frequency, so a Postgres outage would otherwise print one full
    stack per gated interaction per guild - a flood that buries the one line that
    matters. The repr of the exception is kept inline, so the line still says
    WHAT failed (a pool timeout reads differently from a decode error) alongside
    the key and guild - everything a reader can actually act on.
    """
    if pool is None or guild_id is None:
        return None
    try:
        return await settings.get_guild(pool, guild_id, key)
    except Exception as exc:
        log.warning(
            "music guild config: failed to read %s for guild %s: %r",
            key,
            guild_id,
            exc,
        )
        return None


async def default_volume(pool: typing.Any, guild_id: typing.Any):
    """Starting volume for a NEW player (clamped), or ``None`` when unconfigured."""
    return coerce_volume(await _read(pool, guild_id, KEY_DEFAULT_VOLUME))


async def autoplay_default(pool: typing.Any, guild_id: typing.Any):
    """Guild autoplay default as a TRI-STATE: ``True`` / ``False`` / ``None``.

    ``None`` means the guild never configured one, so the bot's own default (ON)
    applies - it is NOT the same as a configured ``False``.
    """
    return coerce_bool(await _read(pool, guild_id, KEY_AUTOPLAY), None)


async def voteskip_enabled(pool: typing.Any, guild_id: typing.Any) -> bool:
    """Whether a non-privileged skip opens a public vote (default: yes)."""
    return bool(
        coerce_bool(await _read(pool, guild_id, KEY_VOTESKIP), DEFAULT_VOTESKIP)
    )


async def sponsorblock_enabled(pool: typing.Any, guild_id: typing.Any) -> bool:
    """Whether new players get the SponsorBlock categories armed (default: yes)."""
    return bool(
        coerce_bool(
            await _read(pool, guild_id, KEY_SPONSORBLOCK), DEFAULT_SPONSORBLOCK
        )
    )


async def dj_role_id(pool: typing.Any, guild_id: typing.Any):
    """The configured DJ role id, or ``None`` when the guild has none."""
    return coerce_role_id(await _read(pool, guild_id, KEY_DJ_ROLE))
