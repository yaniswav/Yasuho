"""Shared discord.py interaction reply helpers.

These small helpers centralise the "has this interaction already been responded
to?" fork that button/select/modal callbacks repeat everywhere: choose
``response.send_message`` vs ``followup.send`` for a reply, and edit-in-place vs
edit the stored message for a refresh. They live in a neutral module (not
``embed_creator``) so any cog can reuse them without importing the embed toolkit.

``embed_creator`` re-exports ``notify_failure`` and ``refresh_in_place`` from
here, so existing ``embed_creator.notify_failure`` call sites keep working.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def reply(interaction, message, *, ephemeral: bool = True) -> None:
    """Reply on an interaction, using followup.send if it was already answered."""

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
    except discord.HTTPException:
        log.debug("interactions.reply failed", exc_info=True)


async def notify_failure(interaction, message: str = "Something went wrong.") -> None:
    """Best-effort ephemeral error reply that respects the response state."""

    await reply(interaction, message, ephemeral=True)


async def refresh_in_place(interaction, message, *, embed, view) -> None:
    """Edit the panel in place, handling the response.is_done() fork.

    Try the live interaction edit first; fall back to editing the stored message
    when the interaction has already been responded to.
    """

    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=view)
            return
    except discord.HTTPException:
        pass
    if message is not None:
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass
