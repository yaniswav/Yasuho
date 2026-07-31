"""Shared moderation backbone: case numbering, action colours, and case embeds.

The moderation and automod cogs create a case here and funnel the resulting
embed to the guild's mod-log via ModLog.post_action(guild, embed).
"""

from __future__ import annotations

import datetime
import logging

import asyncpg
import discord

from tools import settings, warn_escalation

log = logging.getLogger(__name__)

# How many times to retry the case INSERT when two actions in the same guild
# race for the same MAX(case_number) + 1 and one loses the UNIQUE constraint.
_CASE_INSERT_RETRIES = 5

# Colour per action type, used for case + mod-log embeds.
ACTION_COLOURS = {
    "ban": 0xE74C3C,
    "tempban": 0xE74C3C,
    "softban": 0xE67E22,
    "kick": 0xE67E22,
    "mute": 0xE67E22,
    "tempmute": 0xE67E22,
    "warn": 0xF1C40F,
    "unban": 0x2ECC71,
    "unmute": 0x2ECC71,
    "note": 0x95A5A6,
}
ACTION_VERBS = {
    "ban": "Banned",
    "tempban": "Temp-banned",
    "softban": "Soft-banned",
    "kick": "Kicked",
    "mute": "Muted",
    "tempmute": "Temp-muted",
    "warn": "Warned",
    "unban": "Unbanned",
    "unmute": "Unmuted",
    "note": "Note",
}


def action_colour(action):
    return ACTION_COLOURS.get(action, 0x95A5A6)


async def create_case(
    pool, guild_id, user_id, moderator_id, action, reason=None, expires=None
):
    """Insert a case with the next per-guild case number; return that number.

    The per-guild number is MAX(case_number) + 1, computed inside the INSERT.
    The UNIQUE(guild_id, case_number) constraint stops two racing actions from
    sharing a number: the loser raises UniqueViolationError, so we retry (the
    MAX is higher by then) rather than let the mod action fail after the
    ban/kick has already happened.
    """

    query = (
        "INSERT INTO cases "
        "(guild_id, case_number, user_id, moderator_id, action, reason, expires) "
        "VALUES ($1, (SELECT COALESCE(MAX(case_number), 0) + 1 FROM cases "
        "WHERE guild_id = $1), $2, $3, $4, $5, $6) "
        "RETURNING case_number"
    )
    last_exc = None
    for _attempt in range(_CASE_INSERT_RETRIES):
        try:
            row = await pool.fetchrow(
                query,
                guild_id,
                user_id,
                moderator_id,
                action,
                reason,
                expires,
            )
            return row["case_number"]
        except asyncpg.UniqueViolationError as exc:
            last_exc = exc
    raise last_exc


async def record_warn(pool, guild_id, user_id, moderator_id, reason=None):
    """Atomically create a warn case and increment its running counter.

    A warn is one logical persistence operation. Keeping both writes in a single
    data-modifying CTE prevents a case without a counter increment (or the
    reverse) when the database fails between statements.
    """
    query = (
        "WITH inserted_case AS ("
        "INSERT INTO cases "
        "(guild_id, case_number, user_id, moderator_id, action, reason) "
        "VALUES ($1, (SELECT COALESCE(MAX(case_number), 0) + 1 FROM cases "
        "WHERE guild_id = $1), $2, $3, 'warn', $4) "
        "RETURNING case_number"
        "), bumped_warn AS ("
        "INSERT INTO warns (guild_id, user_id, warns_count) VALUES ($1, $2, 1) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE "
        "SET warns_count = warns.warns_count + 1 "
        "RETURNING warns_count"
        ") "
        "SELECT inserted_case.case_number, bumped_warn.warns_count "
        "FROM inserted_case CROSS JOIN bumped_warn"
    )
    last_exc = None
    for _attempt in range(_CASE_INSERT_RETRIES):
        try:
            row = await pool.fetchrow(
                query, guild_id, user_id, moderator_id, reason
            )
            return row["case_number"], row["warns_count"]
        except asyncpg.UniqueViolationError as exc:
            last_exc = exc
    raise last_exc


async def remove_warn_case(pool, guild_id, user_id, case_number):
    """Atomically remove one warn case and decrement the matching counter."""
    row = await pool.fetchrow(
        "WITH removed AS ("
        "DELETE FROM cases WHERE guild_id = $1 AND user_id = $2 "
        "AND action = 'warn' AND case_number = $3 RETURNING id"
        "), updated AS ("
        "UPDATE warns SET warns_count = GREATEST("
        "warns_count - (SELECT COUNT(*) FROM removed), 0"
        ") WHERE guild_id = $1 AND user_id = $2 RETURNING warns_count"
        ") "
        "SELECT (SELECT COUNT(*) FROM removed)::integer AS removed_count, "
        "COALESCE((SELECT warns_count FROM updated), 0)::integer AS warns_count",
        guild_id,
        user_id,
        case_number,
    )
    return row["removed_count"], row["warns_count"]


async def remove_latest_warns(pool, guild_id, user_id, amount):
    """Atomically remove up to ``amount`` newest warn cases and their count."""
    if amount < 1:
        raise ValueError("amount must be at least 1")
    row = await pool.fetchrow(
        "WITH removed AS ("
        "DELETE FROM cases WHERE id IN ("
        "SELECT id FROM cases WHERE guild_id = $1 AND user_id = $2 "
        "AND action = 'warn' ORDER BY case_number DESC LIMIT $3"
        ") RETURNING id"
        "), updated AS ("
        "UPDATE warns SET warns_count = GREATEST("
        "warns_count - (SELECT COUNT(*) FROM removed), 0"
        ") WHERE guild_id = $1 AND user_id = $2 RETURNING warns_count"
        ") "
        "SELECT (SELECT COUNT(*) FROM removed)::integer AS removed_count, "
        "COALESCE((SELECT warns_count FROM updated), 0)::integer AS warns_count",
        guild_id,
        user_id,
        amount,
    )
    return row["removed_count"], row["warns_count"]


async def load_escalation_policy(pool, guild_id):
    """Resolve a guild's warn-escalation policy from settings.

    Returns ``(policy, showing_default)``: ``policy`` is always a usable list of
    rules (:func:`tools.warn_escalation.resolve_policy`); ``showing_default`` is
    True when the guild has no stored policy (unconfigured -> kick at 3) OR its
    stored payload was malformed and we fell back to the default. A malformed
    payload is logged here once, so both the warn command and AutoMod get the
    same behaviour and logging. The hot path (the warn write) ignores the second
    value; only the config panel uses it (to badge the default).
    """
    raw = await settings.get_guild(
        pool, guild_id, warn_escalation.SETTINGS_KEY, None
    )
    policy, malformed = warn_escalation.resolve_policy(raw)
    if malformed:
        log.warning(
            "Guild %s has a malformed warn_escalation payload; falling back to "
            "the default policy (kick at 3).",
            guild_id,
        )
    return policy, (raw is None or malformed)


async def apply_escalation_action(bot, guild, member, rule):
    """Apply a fired warn-escalation rule's action to a member. Never raises.

    Shared by the warn command and AutoMod so both escalate identically. Mirrors
    the historical auto-kick: for a kick/ban it suppresses the matching ModLog
    gateway listener so the action is logged once (by the caller's warn-case
    embed), not twice; a timeout has no such listener. Returns ``True`` on
    success and ``False`` on any failure (role hierarchy, missing permissions,
    member already gone), so the caller can degrade to a clear notice while the
    warn itself stays recorded.
    """
    action = rule["action"]
    reason = f"Warn escalation: reached {rule['threshold']} warns"
    try:
        if action == warn_escalation.TIMEOUT:
            seconds = rule.get("duration") or warn_escalation.DEFAULT_TIMEOUT_SECONDS
            await member.timeout(
                datetime.timedelta(seconds=seconds), reason=reason
            )
        elif action == warn_escalation.KICK:
            funnel_suppress(bot, guild.id, member.id, "remove")
            await guild.kick(member, reason=reason)
        elif action == warn_escalation.BAN:
            funnel_suppress(bot, guild.id, member.id, "ban")
            await guild.ban(member, reason=reason)
        else:  # pragma: no cover - resolve_policy never yields another action
            return False
        return True
    except Exception:
        log.exception(
            "Warn escalation action %s failed for %s in guild %s",
            action,
            member.id,
            guild.id,
        )
        return False


def case_embed(case_number, action, target, moderator, reason, expires=None):
    """A consistent, colour-coded embed for a moderation action."""

    verb = ACTION_VERBS.get(action, action.title())
    embed = discord.Embed(
        title=f"Case #{case_number} - {verb}",
        colour=action_colour(action),
        timestamp=discord.utils.utcnow(),
    )
    avatar = getattr(getattr(target, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=avatar)
    embed.add_field(name="User", value=f"{target.mention} (`{target.id}`)")
    embed.add_field(name="Moderator", value=moderator.mention)
    embed.add_field(
        name="Reason", value=reason or "*No reason provided*", inline=False
    )
    if expires is not None:
        embed.add_field(name="Expires", value=discord.utils.format_dt(expires, "R"))
    embed.set_footer(text=f"User ID: {target.id}")
    return embed


# ----------------------------------------------------------------------
# ModLog funnel
# ----------------------------------------------------------------------
# The single place that resolves the ModLog cog. Routing every post/suppress
# through here means renaming the cog only touches this funnel, not every
# moderation/automod call site (which would otherwise fail get_cog silently).
async def funnel_action(bot, guild, embed):
    """Post a pre-built embed to the guild's mod-log, if ModLog is loaded."""

    cog = bot.get_cog("ModLog")
    if cog is None:
        return
    try:
        await cog.post_action(guild, embed)
    except Exception:
        log.exception("Failed to funnel mod-log action")


def funnel_suppress(bot, guild_id, user_id, kind):
    """Suppress the duplicate listener embed for a bot-initiated action."""

    cog = bot.get_cog("ModLog")
    if cog is None:
        return
    try:
        cog.suppress(guild_id, user_id, kind)
    except Exception:
        log.exception("Failed to funnel mod-log suppress")
