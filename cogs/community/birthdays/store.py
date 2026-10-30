"""Birthdays storage: config normalisation, date maths and every query.

Pure of discord rendering. The config is the ``birthdays`` key of the
guild_settings JSONB blob (same path as milestones / suggestions); the rows live
in ``birthdays`` (schema.sql): day and month only, never a year, one row per
(guild, member) so a birthday is only ever shared where the member chose to.

Typography rule: ASCII '-' and '...' only.
"""

import calendar
import datetime
from zoneinfo import ZoneInfo

from tools import modchecks
from tools.snowflake import coerce_id

SETTINGS_KEY = "birthdays"
TIMER_EVENT = "birthday_daily"
LOCK_NS = 0x42444159
MAX_MESSAGE_LENGTH = 500
DEFAULT_HOUR = 9
DEFAULT_TIMEZONE = "UTC"
LATE_WINDOW = datetime.timedelta(hours=12)
UPCOMING_DAYS = 30
MEMBER_CHUNK = 100  # REQUEST_GUILD_MEMBERS accepts at most 100 ids


def valid_timezone(name):
    if not isinstance(name, str) or not 0 < len(name) <= 64:
        return False
    try:
        ZoneInfo(name)
    except Exception:  # not found, malformed key, a directory: all just "invalid"
        return False
    return True


def normalise(blob):
    """A fresh, fully-typed config from the stored (or dashboard-written) blob."""
    blob = blob if isinstance(blob, dict) else {}
    hour = blob.get("hour")
    zone = blob.get("timezone")
    message = blob.get("message")
    return {
        "enabled": blob.get("enabled") is True,
        "channel_id": coerce_id(blob.get("channel_id")),
        "role_id": coerce_id(blob.get("role_id")),
        "hour": hour if type(hour) is int and 0 <= hour <= 23 else DEFAULT_HOUR,
        "timezone": zone if valid_timezone(zone) else DEFAULT_TIMEZONE,
        "message": message[:MAX_MESSAGE_LENGTH] if isinstance(message, str) and message.strip() else None,
    }


def valid_date(month, day):
    """True for a real calendar day; Feb 29 is valid (a leap year exists)."""
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2024, month)[1]


def next_fire(now, hour, tzname):
    """The first instant strictly after ``now`` that is ``hour``:00 in ``tzname`` (UTC aware)."""
    zone = ZoneInfo(tzname)
    local = now.astimezone(zone)
    for offset in (0, 1, 2):  # a DST gap can push today's slot back before now
        day = local.date() + datetime.timedelta(days=offset)
        slot = datetime.datetime(day.year, day.month, day.day, hour, tzinfo=zone)
        slot = slot.astimezone(datetime.timezone.utc)
        if slot > now:
            return slot
    return slot  # pragma: no cover - unreachable, kept total


def local_today(instant, tzname):
    return instant.astimezone(ZoneInfo(tzname)).date()


def matches_today(today):
    """(month, day, include_feb29): what to look up for ``today``."""
    feb29 = today.month == 2 and today.day == 28 and not calendar.isleap(today.year)
    return today.month, today.day, feb29


def next_occurrence(today, month, day):
    """The date of this birthday on or after ``today`` (Feb 29 -> Feb 28 off leap years)."""
    for year in (today.year, today.year + 1):
        d = 28 if (month, day) == (2, 29) and not calendar.isleap(year) else day
        occurrence = datetime.date(year, month, d)
        if occurrence >= today:
            return occurrence
    raise ValueError("unreachable")  # pragma: no cover


# -- queries ---------------------------------------------------------------


async def upsert(pool, guild_id, user_id, month, day):
    await pool.execute(
        "INSERT INTO birthdays (guild_id, user_id, month, day) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE SET month = EXCLUDED.month, day = EXCLUDED.day",
        guild_id, user_id, month, day,
    )


async def remove(pool, guild_id, user_id):
    """Delete one member's row; True when there was one."""
    return await pool.fetchval(
        "DELETE FROM birthdays WHERE guild_id = $1 AND user_id = $2 RETURNING 1",
        guild_id, user_id,
    ) is not None


async def remove_member(pool, guild_id, user_id):
    await pool.execute(
        "DELETE FROM birthdays WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
    )


async def on_day(pool, guild_id, month, day, include_feb29):
    rows = await pool.fetch(
        "SELECT user_id FROM birthdays WHERE guild_id = $1 AND "
        "((month = $2 AND day = $3) OR ($4 AND month = 2 AND day = 29)) ORDER BY user_id",
        guild_id, month, day, include_feb29,
    )
    return [r["user_id"] for r in rows]


async def in_months(pool, guild_id, months):
    """The guild's rows in the given months (the upcoming window), filtered in SQL."""
    return await pool.fetch(
        "SELECT user_id, month, day FROM birthdays "
        "WHERE guild_id = $1 AND month = ANY($2::int[])",
        guild_id, list(months),
    )


async def claim_day(pool, guild_id, day):
    """Exactly-once per local day: True only for the first run of ``day`` (or later)."""
    return await pool.fetchval(
        "INSERT INTO birthday_state (guild_id, last_day) VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE SET last_day = EXCLUDED.last_day "
        "WHERE birthday_state.last_day < EXCLUDED.last_day RETURNING guild_id",
        guild_id, day,
    ) is not None


def member_cannot_grant(member, guild, role):
    """True when ``member`` may not choose ``role`` as the birthday role.

    Picking it hands it to whoever sets a birthday for today (themselves
    included), so it takes Manage Roles AND outranking the role (owner and
    Administrator exempt from the rank half, as everywhere else).
    """
    return not member.guild_permissions.manage_roles or bool(
        modchecks.role_hierarchy_error_for(member, guild, role)
    )


def role_problem(guild, role):
    """True when Yasuho cannot hand ``role`` out (default, managed, above her, no permission)."""
    me = guild.me
    return (
        role is None
        or me is None
        or role.is_default()
        or role.managed
        or not me.guild_permissions.manage_roles
        or role.position >= me.top_role.position
    )
