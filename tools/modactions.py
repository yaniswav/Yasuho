"""Shared moderation backbone: case numbering, action colours, and case embeds.

The moderation and automod cogs create a case here and funnel the resulting
embed to the guild's mod-log via ModLog.post_action(guild, embed).
"""

from __future__ import annotations

import logging

import asyncpg
import discord

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


async def bump_warn(pool, guild_id, user_id):
    """Add one warn to a member and return the new running count.

    On reaching 3 the counter is reset to 0 and 3 is returned, signalling the
    caller to auto-kick; below the threshold the new count (1 or 2) is stored
    and returned. This is the single home of the 3-strike rule, shared by the
    warn command and AutoMod so both stay in lockstep.
    """
    current = (
        await pool.fetchval(
            "SELECT warns_count FROM warns WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
        or 0
    )
    new_count = current + 1
    if new_count >= 3:
        await pool.execute(
            "INSERT INTO warns (guild_id, user_id, warns_count) VALUES ($1, $2, 0) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET warns_count = 0",
            guild_id,
            user_id,
        )
        return 3
    await pool.execute(
        "INSERT INTO warns (guild_id, user_id, warns_count) VALUES ($1, $2, $3) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE SET warns_count = $3",
        guild_id,
        user_id,
        new_count,
    )
    return new_count


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
