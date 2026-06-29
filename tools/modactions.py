"""Shared moderation backbone: case numbering, action colours, and case embeds.

The moderation and automod cogs create a case here and funnel the resulting
embed to the guild's mod-log via ModLog.post_action(guild, embed).
"""

from __future__ import annotations

import discord

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

    The per-guild number is computed in the INSERT; the UNIQUE(guild_id,
    case_number) constraint guarantees integrity if two actions ever race.
    """

    row = await pool.fetchrow(
        "INSERT INTO cases "
        "(guild_id, case_number, user_id, moderator_id, action, reason, expires) "
        "VALUES ($1, (SELECT COALESCE(MAX(case_number), 0) + 1 FROM cases "
        "WHERE guild_id = $1), $2, $3, $4, $5, $6) "
        "RETURNING case_number",
        guild_id,
        user_id,
        moderator_id,
        action,
        reason,
        expires,
    )
    return row["case_number"]


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
