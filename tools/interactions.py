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


async def defer(
    interaction, *, ephemeral: bool = False, thinking: bool = False, surface: str = "interaction"
) -> bool:
    """Best-effort ``response.defer`` that LOGS a failure instead of hiding it.

    Callers defer before a slow round-trip, then follow up. A defer that fails is
    not benign: the interaction has expired or was already answered, and the
    follow-up almost always fails too - exactly the invisible failure that leaves a
    user on Discord's "Something went wrong" with an empty log. So the failure is
    logged at warning (with ``surface`` for triage), never silently swallowed.
    Returns ``True`` when the defer landed, ``False`` otherwise (callers may ignore
    it; it is there for those that want to bail early).
    """

    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.HTTPException:
        log.warning("interactions.defer failed on %s", surface, exc_info=True)
        return False


async def refresh_layout(
    interaction, message, view, *, surface: str = "panel", allowed_mentions=None
) -> None:
    """View-only in-place refresh of a Components V2 (LayoutView) panel.

    A Components V2 message carries its content inside the view, so Discord rejects
    an ``embed=`` on such an edit; this never passes one (the embed-carrying variant
    is :func:`refresh_in_place`). Tries the live interaction edit first; when the
    interaction was already answered (e.g. a deferred modal submit) it falls back to
    editing the stored message. A first-attempt failure is an expected fallthrough
    to that fallback and stays at DEBUG; a failure of the FINAL fallback means the
    refresh never landed, so it is logged at warning with ``surface``.

    ``allowed_mentions`` is OMITTED from the edit unless a caller passes one, so the
    default behaviour is unchanged: discord.py folds the client default into an edit
    that says nothing (core.Yasuho: users=True). A panel whose text holds tokens it
    must not (re)notify - attacker-chosen guild names, raw ``<@id>`` - passes
    ``discord.AllowedMentions.none()`` here, the same reasoning as
    ``AuthorLayoutView.on_timeout``.
    """

    extra = {} if allowed_mentions is None else {"allowed_mentions": allowed_mentions}
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=view, **extra)
            return
    except discord.HTTPException:
        log.debug(
            "interactions.refresh_layout: live edit failed on %s, falling back",
            surface,
            exc_info=True,
        )
    if message is not None:
        try:
            await message.edit(view=view, **extra)
        except discord.HTTPException:
            log.warning(
                "interactions.refresh_layout: could not refresh %s", surface, exc_info=True
            )


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


# ---------------------------------------------------------------------------
# Ephemeral flow marker
# ---------------------------------------------------------------------------
# Key under which a command records "every reply in this flow is private".
#
# discord.py keeps NO local trace of an ephemeral defer: InteractionResponse.defer
# puts the flag in the outgoing payload and stores only `_response_type`
# (discord.py 2.7, interactions.py), so nothing downstream can read the choice
# back. That matters because a later sender may not be the command at all - the
# global error reporter in cogs/system/errors.py answers a crashed command
# without ever seeing its body, and `Context.send` defaults `ephemeral=False`.
# Without a marker, a crash inside an ephemeral flow answers PUBLICLY on a
# command whose every other reply is private.
#
# `Interaction.extras` is the dictionary discord.py documents for exactly this
# ("can be used to store extraneous data for use by things like checks or before
# invoke hooks"), and it dies with the interaction, so there is nothing to clean
# up and nothing to leak into another invocation.
EPHEMERAL_FLOW = "yasuho_ephemeral_flow"


def _extras(ctx):
    """The interaction's extras dict for this Context, or None on the prefix path."""

    extras = getattr(getattr(ctx, "interaction", None), "extras", None)
    return extras if isinstance(extras, dict) else None


def mark_ephemeral(ctx) -> None:
    """Record that this invocation's replies are ephemeral. Never raises.

    A no-op on the prefix path (no interaction, nothing to remember) and on any
    stand-in whose interaction has no ``extras`` mapping: a bookkeeping helper
    must never be the thing that breaks a command.
    """

    extras = _extras(ctx)
    if extras is not None:
        extras[EPHEMERAL_FLOW] = True


def prefers_ephemeral(ctx) -> bool:
    """True when this invocation was marked as an ephemeral flow."""

    extras = _extras(ctx)
    return bool(extras is not None and extras.get(EPHEMERAL_FLOW))


async def defer_ephemeral(ctx) -> None:
    """``ctx.defer(ephemeral=True)`` that also RECORDS the privacy choice.

    Use this instead of a bare ``await ctx.defer(ephemeral=True)`` whenever the
    command's own replies are ephemeral, so that anything replying LATER on the
    same interaction - the global error reporter above all - keeps the flow
    private instead of dropping a public message into the channel.

    Inert on the prefix path: ``Context.defer`` returns immediately when
    ``ctx.interaction`` is None, and there is no interaction to mark.
    """

    await ctx.defer(ephemeral=True)
    mark_ephemeral(ctx)
