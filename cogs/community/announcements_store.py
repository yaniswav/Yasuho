"""Scheduled announcement persistence and atomic timer ownership."""

from __future__ import annotations

import datetime
import json
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .announcements_schedule import RECURRENCES, next_occurrence
from tools import embed_creator

UTC = datetime.timezone.utc
log = logging.getLogger(__name__)
_FIELDS = (
    "channel_id", "author_id", "content", "embed", "role_id", "timezone",
    "local_time", "recurrence", "enabled",
)


def max_for_guild(bot, guild_id):
    """Safety ceiling; future guild premium limits have this single seam."""
    return 50


def validate(values):
    """Validate a complete write before issuing SQL; return normalized values."""
    data = dict(values)
    try:
        ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Invalid IANA timezone") from exc
    if data["recurrence"] not in RECURRENCES:
        raise ValueError("Invalid recurrence")
    local = data["local_time"]
    if not isinstance(local, datetime.datetime) or local.tzinfo is not None:
        raise ValueError("local_time must be a naive datetime")
    content = data.get("content")
    if content is not None and (not isinstance(content, str) or len(content) > 2000):
        raise ValueError("Content must be at most 2000 characters")
    embed = data.get("embed")
    if isinstance(embed, str):
        embed = json.loads(embed)
    if embed is not None:
        # An embed_creator config: rendered here once to prove it shows
        # something, and again at fire time with the live placeholders.
        if not isinstance(embed, dict) or len(json.dumps(embed)) > 16384:
            raise ValueError("Invalid embed")
        rendered = render_embed(embed)
        if not embed_creator.embed_has_content(rendered):
            raise ValueError("Embed has no content")
    if not (content and content.strip()) and embed is None:
        raise ValueError("An announcement needs content or an embed")
    data["embed"] = embed
    for key in ("channel_id", "author_id", "role_id"):
        value = data.get(key)
        if key == "role_id" and value is None:
            continue
        if type(value) is not int or value <= 0:
            raise ValueError(f"Invalid {key}")
    # The @everyone role shares the guild's id; a role ping must never be it.
    if data.get("role_id") is not None and data["role_id"] == data.get("guild_id"):
        raise ValueError("Invalid role_id")
    if type(data["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean")
    return data


def render_embed(config, substitute=None):
    """Render a stored embed config, or raise ValueError.

    The config is JSON the database only checks for shape, so a malformed
    nested value (an author that is not an object, a non-string title) is a
    ValueError here - a recorded delivery failure, never a crash - and so is
    an embed over Discord's 6000-character total, checked AFTER the live
    placeholders are substituted.
    """
    try:
        embed = embed_creator.render(config, substitute=substitute)
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("Invalid embed") from exc
    if len(embed) > 6000:
        raise ValueError("Embed exceeds 6000 characters")
    return embed


def render(row, guild=None, channel=None):
    """Return Discord delivery arguments with exactly one optional role ping.

    ``guild`` / ``channel`` feed the {server} / {members} / {channel} tokens;
    without them (write-time validation) the tokens render empty.
    """
    data = row["embed"]
    if isinstance(data, str):
        data = json.loads(data)
    embed = None
    if data is not None:
        embed = render_embed(data, embed_creator.server_substitution(guild, channel))
    role_id = row["role_id"]
    content = row["content"]
    if role_id is not None:
        # The role prefix counts toward the same 2000-character send limit.
        content = f"<@&{role_id}>" + (f" {content}" if content else "")
    mentions = discord.AllowedMentions(
        everyone=False, users=False,
        roles=[discord.Object(id=role_id)] if role_id is not None else False,
        replied_user=False,
    )
    return content, embed, mentions


class AnnouncementStore:
    def __init__(self, bot):
        self.bot = bot
        self.pool = bot.db_pool

    def _wake(self):
        reminder = self.bot.get_cog("Reminder")
        if reminder is not None:
            reminder._have_data.set()

    async def get(self, guild_id, announcement_id):
        return await self.pool.fetchrow(
            "SELECT * FROM scheduled_announcements WHERE guild_id = $1 AND id = $2",
            guild_id, announcement_id,
        )

    async def list_for_guild(self, guild_id):
        return await self.pool.fetch(
            "SELECT * FROM scheduled_announcements WHERE guild_id = $1 ORDER BY id",
            guild_id,
        )

    async def _timer(self, conn, row):
        await conn.execute("DELETE FROM timers WHERE announcement_id = $1", row["id"])
        if row["enabled"]:
            await conn.execute(
                "INSERT INTO timers(event, expires, extra, announcement_id) "
                "VALUES ('announcement', $1, $2::jsonb, $3)",
                row["next_run_at"],
                json.dumps({"guild_id": row["guild_id"], "announcement_id": row["id"]}),
                row["id"],
            )

    async def create(
        self, guild_id, channel_id, author_id, *, local_time, timezone,
        recurrence="none", content=None, embed=None, role_id=None, enabled=True,
    ):
        if type(guild_id) is not int or guild_id <= 0:
            raise ValueError("Invalid guild_id")
        data = validate(dict(
            guild_id=guild_id, channel_id=channel_id, author_id=author_id, local_time=local_time,
            timezone=timezone, recurrence=recurrence, content=content, embed=embed,
            role_id=role_id, enabled=enabled,
        ))
        next_at = next_occurrence(local_time, timezone, recurrence, discord.utils.utcnow())
        if next_at is None:
            raise ValueError("One-shot time must be in the future")
        self._validate_render(data)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize count-and-insert for this guild, including concurrent UIs.
                await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", 0x414E4E43, guild_id % 2147483647)
                count = await conn.fetchval(
                    "SELECT count(*) FROM scheduled_announcements WHERE guild_id = $1", guild_id,
                )
                if count >= max_for_guild(self.bot, guild_id):
                    raise ValueError("Announcement safety ceiling reached")
                row = await conn.fetchrow(
                    "INSERT INTO scheduled_announcements "
                    "(guild_id, channel_id, author_id, content, embed, role_id, timezone, "
                    "local_time, recurrence, enabled, next_run_at) "
                    "VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11) RETURNING *",
                    guild_id, *self._args(data), next_at,
                )
                await self._timer(conn, row)
        self._wake()
        return row

    @staticmethod
    def _args(data):
        return [json.dumps(data[k]) if k == "embed" and data[k] is not None else data[k] for k in _FIELDS]

    @staticmethod
    def _validate_render(data):
        content, _embed, _mentions = render(data)
        if content is not None and len(content) > 2000:
            raise ValueError("Content including the role mention exceeds 2000 characters")

    async def update(self, guild_id, announcement_id, **changes):
        if changes.keys() - set(_FIELDS):
            raise ValueError("Unknown announcement fields")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchrow(
                    "SELECT * FROM scheduled_announcements WHERE guild_id = $1 AND id = $2 FOR UPDATE",
                    guild_id, announcement_id,
                )
                if old is None:
                    return None
                data = validate({**dict(old), **changes})
                self._validate_render(data)
                next_at = next_occurrence(
                    data["local_time"], data["timezone"], data["recurrence"], discord.utils.utcnow(),
                )
                if data["enabled"] and next_at is None:
                    raise ValueError("One-shot time must be in the future")
                if not data["enabled"]:
                    next_at = None  # paused: no next send to show
                row = await conn.fetchrow(
                    "UPDATE scheduled_announcements SET channel_id=$3, author_id=$4, "
                    "content=$5, embed=$6::jsonb, role_id=$7, timezone=$8, local_time=$9, "
                    "recurrence=$10, enabled=$11, next_run_at=$12, failures=0, last_error=NULL, "
                    "revision=revision+1, updated_at=now() WHERE guild_id=$1 AND id=$2 RETURNING *",
                    guild_id, announcement_id, *self._args(data), next_at,
                )
                await self._timer(conn, row)
        self._wake()
        return row

    async def set_enabled(self, guild_id, announcement_id, enabled):
        return await self.update(guild_id, announcement_id, enabled=enabled)

    async def delete(self, guild_id, announcement_id):
        # The cascade competes with claim on the parent row lock. If claim won,
        # that firing is in flight; deletion still cancels the next occurrence.
        row = await self.pool.fetchrow(
            "DELETE FROM scheduled_announcements WHERE guild_id=$1 AND id=$2 RETURNING id",
            guild_id, announcement_id,
        )
        if row is not None:
            self._wake()
        return row is not None

    async def claim_and_reschedule(self, timer, now=None):
        """Lock parent then DELETE-as-claim; commit its next timer before delivery."""
        now = now or discord.utils.utcnow()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM scheduled_announcements WHERE id=$1 FOR UPDATE",
                    timer["announcement_id"],
                )
                if row is None:
                    return None
                claimed = await conn.fetchrow(
                    "DELETE FROM timers WHERE id=$1 AND announcement_id=$2 "
                    "AND claimed_at IS NULL RETURNING *", timer["id"], row["id"],
                )
                if claimed is None or not row["enabled"]:
                    return None
                planned = row["next_run_at"] if row["next_run_at"] is not None else claimed["expires"]
                try:
                    next_at = next_occurrence(
                        row["local_time"], row["timezone"], row["recurrence"],
                        max(now, planned),
                    )
                except (ZoneInfoNotFoundError, ValueError, OverflowError):
                    await conn.execute(
                        "UPDATE scheduled_announcements SET enabled=false, next_run_at=NULL, "
                        "last_error='calendar-error', updated_at=now() WHERE id=$1", row["id"],
                    )
                    log.warning("ANNOUNCE-DISABLED id=%s reason=calendar-error", row["id"])
                    return None
                updated = dict(row)
                updated.update(next_run_at=next_at, enabled=next_at is not None)
                await conn.execute(
                    "UPDATE scheduled_announcements SET next_run_at=$2, enabled=$3, "
                    "updated_at=now() WHERE id=$1", row["id"], next_at, next_at is not None,
                )
                await self._timer(conn, updated)
        self._wake()
        result = dict(claimed)
        result["extra"] = {"announcement": dict(row), "scheduled_at": planned}
        return result

    async def record_result(self, row, error=None):
        """A stale delivery must never change an edited announcement's state."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetchrow(
                    "UPDATE scheduled_announcements SET "
                    "failures=CASE WHEN $3::text IS NULL THEN 0 ELSE failures+1 END, "
                    "last_error=$3, enabled=enabled AND ($3::text IS NULL OR failures+1 < 3), "
                    "next_run_at=CASE WHEN $3::text IS NOT NULL AND failures+1 >= 3 "
                    "THEN NULL ELSE next_run_at END, updated_at=now() "
                    "WHERE id=$1 AND revision=$2 RETURNING *",
                    row["id"], row["revision"], error,
                )
                if result is not None and not result["enabled"]:
                    await conn.execute("DELETE FROM timers WHERE announcement_id=$1", row["id"])
        self._wake()
        return result

    render = staticmethod(render)
