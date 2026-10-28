"""Dashboard -> bot action queue: the scheduled-announcement executors.

Companion module to ``cogs/system/dashboard_actions.py`` (which owns the queue,
the claim and the result write-back) and merged into its single kind table,
exactly like ``dashboard_music_actions``. The dashboard never writes the
``scheduled_announcements`` table itself: every change goes through
``AnnouncementStore`` here, so the exactly-once timer bookkeeping, the
per-guild limit and the validation are the bot's own.

Handler contract: ``async handler(bot, guild_id, payload[, actor]) -> dict``.
``guild_id`` is authoritative (the claimed row); every payload value is
re-validated. Snowflakes travel as strings, both ways.

Payloads (all ids are snowflake strings):

* ``announcement_create``: ``{"channel_id", "local_time": "YYYY-MM-DDTHH:MM",
  "timezone": "Europe/Paris", "recurrence": "none|daily|weekly|monthly",
  "embed"?: <embed_creator config>, "content"?: str, "role_id"?: str}``
* ``announcement_update``: ``{"announcement_id", ...any create field}``
* ``announcement_set_enabled``: ``{"announcement_id", "enabled": bool}``
* ``announcement_delete``: ``{"announcement_id"}``

Success: ``{"ok": true, "announcement_id", "next_run_at": ISO-8601 | null}``.
Error codes: ``bad_payload``, ``guild_unavailable``, ``engine_unavailable``,
``channel_not_found``, ``not_text_channel``, ``missing_permissions``,
``role_not_found``, ``role_not_mentionable``, ``not_found``,
``limit_reached`` (free 3 / Yasuho+ 25 rows, paused ones count),
``announcement_archived`` (resume/edit-while-enabled of a row over the limit
after a downgrade; pause and delete still work), ``invalid_announcement``.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import logging

import discord

from cogs.community.announcements_store import AnnouncementArchived, AnnouncementLimitReached

log = logging.getLogger(__name__)

_FIELDS = ("channel_id", "local_time", "timezone", "recurrence", "embed", "content", "role_id")


def _id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _store(bot):
    engine = bot.get_cog("ScheduledAnnouncements")
    return engine.store if engine is not None else None


def _result(row):
    next_run = row["next_run_at"]
    return {
        "ok": True,
        "announcement_id": str(row["id"]),
        "next_run_at": next_run.isoformat() if next_run is not None else None,
    }


def _error(exc):
    text = str(exc)
    if isinstance(exc, AnnouncementLimitReached):
        return {"ok": False, "error": "limit_reached"}
    if isinstance(exc, AnnouncementArchived):
        return {"ok": False, "error": "announcement_archived"}
    return {"ok": False, "error": "invalid_announcement", "detail": text[:200]}


def _changes(guild, payload, actor):
    """Validate the create/update fields present in ``payload``.

    Returns ``(changes, error)``; ``changes`` holds store-ready values.
    """
    changes = {}
    if "channel_id" in payload:
        channel = guild.get_channel(_id(payload["channel_id"]) or 0)
        if channel is None:
            return None, {"ok": False, "error": "channel_not_found"}
        if not isinstance(channel, discord.TextChannel):
            return None, {"ok": False, "error": "not_text_channel"}
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return None, {"ok": False, "error": "missing_permissions"}
        changes["channel_id"] = channel.id
    if "local_time" in payload:
        try:
            local = datetime.datetime.fromisoformat(str(payload["local_time"]))
        except ValueError:
            return None, {"ok": False, "error": "bad_payload"}
        if local.tzinfo is not None:
            return None, {"ok": False, "error": "bad_payload"}
        changes["local_time"] = local
    for key in ("timezone", "recurrence"):
        if key in payload:
            if not isinstance(payload[key], str):
                return None, {"ok": False, "error": "bad_payload"}
            changes[key] = payload[key]
    if "embed" in payload:
        if payload["embed"] is not None and not isinstance(payload["embed"], dict):
            return None, {"ok": False, "error": "bad_payload"}
        changes["embed"] = payload["embed"]
    if "content" in payload:
        if payload["content"] is not None and not isinstance(payload["content"], str):
            return None, {"ok": False, "error": "bad_payload"}
        changes["content"] = payload["content"] or None
    if "role_id" in payload:
        if payload["role_id"] is None:
            changes["role_id"] = None
        else:
            role = guild.get_role(_id(payload["role_id"]) or 0)
            if role is None or role.is_default():
                return None, {"ok": False, "error": "role_not_found"}
            # The bot would ping it on the actor's behalf: only a role the actor
            # could ping themselves (mentionable, or they hold Mention Everyone).
            if not role.mentionable and not actor.guild_permissions.mention_everyone:
                return None, {"ok": False, "error": "role_not_mentionable"}
            changes["role_id"] = role.id
    return changes, None


async def _exec_create(bot, guild_id, payload, actor):
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}
    store = _store(bot)
    if store is None:
        return {"ok": False, "error": "engine_unavailable"}
    if not {"channel_id", "local_time", "timezone"} <= set(payload):
        return {"ok": False, "error": "bad_payload"}
    changes, error = _changes(guild, payload, actor)
    if error is not None:
        return error
    try:
        row = await store.create(
            guild.id,
            changes.pop("channel_id"),
            actor.id,
            local_time=changes.pop("local_time"),
            timezone=changes.pop("timezone"),
            recurrence=changes.pop("recurrence", "none"),
            **changes,
        )
    except ValueError as exc:
        return _error(exc)
    return _result(row)


async def _exec_update(bot, guild_id, payload, actor):
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}
    store = _store(bot)
    if store is None:
        return {"ok": False, "error": "engine_unavailable"}
    announcement_id = _id(payload.get("announcement_id"))
    if announcement_id is None:
        return {"ok": False, "error": "bad_payload"}
    changes, error = _changes(guild, {k: payload[k] for k in _FIELDS if k in payload}, actor)
    if error is not None:
        return error
    try:
        row = await store.update(guild.id, announcement_id, **changes)
    except ValueError as exc:
        return _error(exc)
    if row is None:
        return {"ok": False, "error": "not_found"}
    return _result(row)


async def _exec_set_enabled(bot, guild_id, payload):
    store = _store(bot)
    if store is None:
        return {"ok": False, "error": "engine_unavailable"}
    announcement_id = _id(payload.get("announcement_id"))
    enabled = payload.get("enabled")
    if announcement_id is None or not isinstance(enabled, bool):
        return {"ok": False, "error": "bad_payload"}
    try:
        row = await store.set_enabled(guild_id, announcement_id, enabled)
    except ValueError as exc:
        return _error(exc)
    if row is None:
        return {"ok": False, "error": "not_found"}
    return _result(row)


async def _exec_delete(bot, guild_id, payload):
    store = _store(bot)
    if store is None:
        return {"ok": False, "error": "engine_unavailable"}
    announcement_id = _id(payload.get("announcement_id"))
    if announcement_id is None:
        return {"ok": False, "error": "bad_payload"}
    if not await store.delete(guild_id, announcement_id):
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "announcement_id": str(announcement_id)}


EXECUTORS = {
    "announcement_create": _exec_create,
    "announcement_update": _exec_update,
    "announcement_set_enabled": _exec_set_enabled,
    "announcement_delete": _exec_delete,
}

# The two kinds that can make the bot ping a role take the requesting Member.
ACTOR_KINDS = frozenset({"announcement_create", "announcement_update"})
