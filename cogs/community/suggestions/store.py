"""Suggestion box storage: config normalisation, statuses and every query.

Pure of discord rendering. The config is the ``suggestions`` key of the
guild_settings JSONB blob (same path as milestones / event reminders); the rows
live in ``suggestions`` and ``suggestion_votes`` (schema.sql).

Typography rule: ASCII '-' and '...' only.
"""

import datetime

from tools.snowflake import coerce_id

SETTINGS_KEY = "suggestions"
TIMER_EVENT = "suggestion_digest"
LOCK_NS = 0x53554754

TITLE_MAX = 100
DETAILS_MAX = 1000
REASON_MAX = 200
LABEL_MAX = 30
MAX_CUSTOM_STATUSES = 5
DIGEST_TOP = 5
DIGEST_DAYS = 7
DIGEST_HOUR = 9  # UTC
DEFAULT_COLOUR = 0x95A5A6

STATUS_OPEN = "open"
BUILTIN_STATUSES = ("open", "approved", "rejected", "implemented")
# Voting is closed (and the thread archived and locked) for these two.
CLOSING_STATUSES = ("rejected", "implemented")
CUSTOM_PREFIX = "c:"


def normalise(blob):
    """A fresh, fully-typed config from the stored (or dashboard-written) blob."""
    blob = blob if isinstance(blob, dict) else {}
    customs = []
    raw = blob.get("custom_statuses")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        colour = item.get("colour")
        if not isinstance(label, str) or not label.strip():
            continue
        label = label.strip()[:LABEL_MAX]
        if label in {c["label"] for c in customs}:
            continue
        if type(colour) is not int or not 0 <= colour <= 0xFFFFFF:
            colour = DEFAULT_COLOUR
        customs.append({"label": label, "colour": colour})
    weekday = blob.get("digest_weekday")
    return {
        "enabled": blob.get("enabled") is True,
        "channel_id": coerce_id(blob.get("channel_id")),
        "staff_role_id": coerce_id(blob.get("staff_role_id")),
        "custom_statuses": customs[:MAX_CUSTOM_STATUSES],
        "digest": blob.get("digest") is True,
        "digest_weekday": weekday if type(weekday) is int and 0 <= weekday <= 6 else 0,
    }


def parse_colour(text):
    """``#RRGGBB`` / ``RRGGBB`` to an int, or None."""
    text = (text or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def custom_key(label):
    return CUSTOM_PREFIX + label


def is_custom(key):
    return key.startswith(CUSTOM_PREFIX)


def is_closing(key):
    return key in CLOSING_STATUSES


def next_digest(now, weekday=0):
    """The first ``weekday`` at 09:00 UTC strictly after ``now``."""
    now = now.astimezone(datetime.timezone.utc)
    target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    target += datetime.timedelta(days=(weekday - now.weekday()) % 7)
    if target <= now:
        target += datetime.timedelta(days=7)
    return target


# -- queries -----------------------------------------------------------------

_CREATE_SQL = (
    "INSERT INTO suggestions (guild_id, number, channel_id, message_id, author_id, "
    "title, details) "
    "SELECT $1, COALESCE(max(number), 0) + 1, $2, 0, $3, $4, $5 "
    "FROM suggestions WHERE guild_id = $1 RETURNING *"
)


async def create(pool, guild_id, channel_id, author_id, title, details):
    """Insert a suggestion with the next per-guild number (race-free).

    ``message_id`` is 0 until :func:`attach_message`; the digest and every
    reader skip such a row.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1, $2)", LOCK_NS, guild_id % 2147483647
            )
            return await conn.fetchrow(
                _CREATE_SQL, guild_id, channel_id, author_id, title, details
            )


async def attach_message(pool, suggestion_id, message_id, thread_id=None):
    """False when the row is gone (erased while it was being posted)."""
    row = await pool.fetchval(
        "UPDATE suggestions SET message_id = $2, thread_id = $3 WHERE id = $1 RETURNING id",
        suggestion_id, message_id, thread_id,
    )
    return row is not None


async def attach_thread(pool, suggestion_id, thread_id):
    await pool.execute(
        "UPDATE suggestions SET thread_id = $2 WHERE id = $1", suggestion_id, thread_id
    )


async def discard(pool, suggestion_id):
    await pool.execute("DELETE FROM suggestions WHERE id = $1", suggestion_id)


async def get(pool, suggestion_id, guild_id):
    return await pool.fetchrow(
        "SELECT * FROM suggestions WHERE id = $1 AND guild_id = $2", suggestion_id, guild_id
    )


async def counts(pool, suggestion_id):
    """``(up, down)`` from the votes table."""
    row = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE value = 1) AS up, "
        "count(*) FILTER (WHERE value = -1) AS down "
        "FROM suggestion_votes WHERE suggestion_id = $1",
        suggestion_id,
    )
    return int(row["up"]), int(row["down"])


# One statement: toggle off on the same value, switch or insert otherwise, and
# nothing at all while the suggestion is closed or not in this guild.
_VOTE_SQL = """
WITH s AS (
    SELECT id, status FROM suggestions WHERE id = $1 AND guild_id = $4
),
del AS (
    DELETE FROM suggestion_votes v USING s
    WHERE v.suggestion_id = s.id AND v.user_id = $2 AND v.value = $3
      AND s.status <> ALL($5::text[])
    RETURNING 1
),
ins AS (
    INSERT INTO suggestion_votes (suggestion_id, user_id, value)
    SELECT s.id, $2, $3 FROM s
    WHERE s.status <> ALL($5::text[]) AND NOT EXISTS (SELECT 1 FROM del)
    ON CONFLICT (suggestion_id, user_id) DO UPDATE SET value = EXCLUDED.value
    RETURNING 1
)
SELECT (SELECT status FROM s) AS status,
       EXISTS (SELECT 1 FROM del) AS removed,
       EXISTS (SELECT 1 FROM ins) AS cast
"""


async def toggle_vote(pool, suggestion_id, guild_id, user_id, value):
    """``cast`` / ``removed`` / ``closed`` / ``missing``."""
    row = await pool.fetchrow(
        _VOTE_SQL, suggestion_id, user_id, value, guild_id, list(CLOSING_STATUSES)
    )
    if row["status"] is None:
        return "missing"
    if row["removed"]:
        return "removed"
    return "cast" if row["cast"] else "closed"


_STATUS_SQL = (
    "UPDATE suggestions s SET status = $3, status_reason = $4, updated_at = now() "
    "FROM (SELECT id, status FROM suggestions WHERE id = $1 AND guild_id = $2 "
    "FOR UPDATE) old WHERE s.id = old.id RETURNING s.*, old.status AS previous"
)


async def set_status(pool, suggestion_id, guild_id, status, reason):
    """The updated row (with ``previous``), or None when it is gone."""
    return await pool.fetchrow(
        _STATUS_SQL, suggestion_id, guild_id, status, reason or None
    )


async def top_recent(pool, guild_id):
    """The digest: top open suggestions of the last week by score."""
    return await pool.fetch(
        "SELECT s.id, s.number, s.title, s.channel_id, s.message_id, "
        "COALESCE(sum(v.value), 0)::int AS score "
        "FROM suggestions s LEFT JOIN suggestion_votes v ON v.suggestion_id = s.id "
        "WHERE s.guild_id = $1 AND s.status = 'open' AND s.message_id > 0 "
        "AND s.created_at > now() - make_interval(days => $2) "
        "GROUP BY s.id ORDER BY score DESC, s.created_at LIMIT $3",
        guild_id, DIGEST_DAYS, DIGEST_TOP,
    )


async def message_refs(pool, user_id):
    """Where this user's suggestions were posted, for best-effort erasure."""
    return await pool.fetch(
        "SELECT number, channel_id, message_id, thread_id FROM suggestions "
        "WHERE author_id = $1 AND message_id > 0",
        user_id,
    )


async def delete_posted(bot, refs):
    """Best effort: remove the posted messages of erased suggestions.

    Called after the rows are gone; any failure is skipped (the data that
    matters is the row). The discussion thread holds other members' replies,
    so it is kept: renamed to its bare number (the name held the title),
    then archived and locked.
    """
    for ref in refs:
        try:
            channel = bot.get_channel(ref["channel_id"])
            if channel is not None:
                await channel.get_partial_message(ref["message_id"]).delete()
        except Exception:
            pass
        try:
            if ref["thread_id"]:
                thread = bot.get_channel(ref["thread_id"]) or await bot.fetch_channel(
                    ref["thread_id"]
                )
                # An archived thread must be unarchived before any other edit.
                await thread.edit(name="#{n}".format(n=ref["number"]), archived=False, locked=True)
                await thread.edit(archived=True)
        except Exception:
            pass
