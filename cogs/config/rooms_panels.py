"""Per-room voicemaster control panel for temporary voice rooms.

Move-only split from ``rooms.py``: the Components V2 ``LayoutView`` posted in
each temp room's own text chat (owner-gated buttons for slots/rename/lock/hide/
kick/blacklist/transfer/bump/claim) plus its short-lived ephemeral sub-pickers
and rename modal. All Discord/DB side effects still live on the cog and are
reached through the ``self.cog`` reference each component is constructed with.
"""

from __future__ import annotations

import logging

import discord

from tools import i18n, interactions
from tools.autoroom import (
    SLOT_VALUES,
    blacklisted_targets,
    claimable,
    slot_value_label,
)
from tools.i18n import _
from tools.views import LocaleModal

log = logging.getLogger(__name__)


class _PanelButton(discord.ui.Button):
    """A layout button whose click forwards to a bound coroutine.

    Components V2 layouts cannot use the ``@discord.ui.button`` decorator, so
    each button is a plain instance that delegates to a handler on the panel.
    """

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction):
        await self._handler(interaction)


# ---------------------------------------------------------------------------
# Per-room control panel (voicemaster)
# ---------------------------------------------------------------------------
#
# One control message is posted in each temp room's own text chat. Its live
# state (limit, name, lock, hide, blacklist) is pushed onto the Discord channel
# and dies with it; ownership is marked by the owner's manage_channels overwrite
# on the channel (``_room_owners`` is only a fast cache of that). The view has
# ``timeout=None`` so it stays alive for the life of the channel and is gated to
# the current owner - except Claim, which anyone in the channel may use once the
# owner has left. After a restart the on_ready re-adoption re-seeds ownership
# from the channel overwrites and re-posts a fresh panel.

_ACTION_LIMIT = "limit"
_ACTION_RENAME = "rename"
_ACTION_LOCK = "lock"
_ACTION_HIDE = "hide"
_ACTION_KICK = "kick"
_ACTION_UNBLACKLIST = "unblacklist"
_ACTION_TRANSFER = "transfer"
_ACTION_BUMP = "bump"
_ACTION_CLAIM = "claim"

# Stable custom_id for the Claim button so ``interaction_check`` can recognise
# it and let a non-owner through (Claim is validated in its own handler). Only
# one Claim button exists per control message, so a constant id is unambiguous.
_CLAIM_CUSTOM_ID = "autoroom:room:claim"

# Discord caps a select at 25 options; member pickers are sliced to this.
_MEMBER_OPTION_CAP = 25


class _SlotSelect(discord.ui.Select):
    """Ephemeral picker of sensible user-limit values for a room."""

    def __init__(self, parent):
        self._owner = parent
        options = [
            discord.SelectOption(label=slot_value_label(value), value=str(value))
            for value in SLOT_VALUES
        ]
        super().__init__(
            placeholder=_("User limit..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        channel = self._owner._channel()
        if channel is None:
            await interaction.response.edit_message(
                content=_("This room no longer exists."), view=None
            )
            return
        try:
            await channel.edit(user_limit=int(self.values[0]))
        except (discord.HTTPException, ValueError):
            await interaction.response.edit_message(
                content=_("Could not change the user limit."), view=None
            )
            return
        await interaction.response.edit_message(
            content=_("Updated the user limit."), view=None
        )


class _MemberActionSelect(discord.ui.Select):
    """Ephemeral picker of channel members for kick/unblacklist/transfer."""

    def __init__(self, parent, members, action):
        self._owner = parent
        self.action = action
        options = [
            discord.SelectOption(
                label=(getattr(member, "display_name", None) or str(member.id))[:100],
                value=str(member.id),
            )
            for member in members[:_MEMBER_OPTION_CAP]
        ]
        placeholders = {
            _ACTION_KICK: _("Select a member to remove..."),
            _ACTION_UNBLACKLIST: _("Select a member to unblacklist..."),
            _ACTION_TRANSFER: _("Select the new owner..."),
        }
        super().__init__(
            placeholder=placeholders.get(action, _("Select a member...")),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        await self._owner._handle_member_action(
            interaction, self.action, int(self.values[0])
        )


class _RoomSubView(discord.ui.View):
    """A short-lived ephemeral view hosting one sub-picker of a room action."""

    def __init__(self, item):
        super().__init__(timeout=60)
        self.add_item(item)


class _RoomRenameModal(LocaleModal):
    """One-field modal that renames the room's voice channel."""

    def __init__(self, parent, current_name):
        super().__init__(title=_("Rename room"))
        self._owner = parent
        self.name_input = discord.ui.TextInput(
            label=_("New room name"),
            default=(current_name or "")[:100],
            max_length=100,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction):
        channel = self._owner._channel()
        if channel is None:
            await interactions.reply(interaction, _("This room no longer exists."))
            return
        name = (self.name_input.value or "").strip()
        if not name:
            await interactions.reply(interaction, _("Give the room a name."))
            return
        try:
            await channel.edit(name=name[:100])
        except discord.HTTPException:
            await interactions.notify_failure(
                interaction, _("Could not rename the room.")
            )
            return
        await interactions.reply(
            interaction, _("Renamed the room to **{name}**.").format(name=name[:100])
        )


class RoomControlView(discord.ui.LayoutView):
    """Per-room voicemaster panel: owner-gated, lives while the channel does.

    A Components V2 ``LayoutView`` (following ``MusicController``): a coloured
    Container holds a header, a Separator and one or two ActionRows of action
    buttons. Not an ``AuthorView`` because the owner can change via
    Transfer/Claim - ``interaction_check`` reads the live owner from the cog's
    in-memory map instead of a fixed author id. ``timeout=None`` keeps it
    responsive for the whole life of the room. Every button either acts on the
    channel directly or opens an ephemeral sub-picker/modal for its input; the
    stateful Lock/Hide buttons re-render in place so their label tracks state.
    """

    def __init__(self, cog, channel_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        self.message = None
        self._build()

    def _channel(self):
        """Return the live voice channel, or ``None`` if it is gone."""
        channel = self.cog.bot.get_channel(self.channel_id)
        if isinstance(channel, discord.VoiceChannel):
            return channel
        return None

    def _is_locked(self):
        """True when @everyone is denied Connect on the live channel."""
        channel = self._channel()
        if channel is None:
            return False
        overwrite = channel.overwrites_for(channel.guild.default_role)
        return overwrite.connect is False

    def _is_hidden(self):
        """True when @everyone is denied View Channel on the live channel."""
        channel = self._channel()
        if channel is None:
            return False
        overwrite = channel.overwrites_for(channel.guild.default_role)
        return overwrite.view_channel is False

    def _make_handler(self, action):
        async def handler(interaction):
            await self._dispatch(interaction, action)

        return handler

    def _build(self):
        """(Re)assemble the layout, reflecting the room's current lock/hide state."""
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(_("## Room controls")))
        owner_id = self.cog._owner_of(self.channel_id)
        if owner_id is not None:
            header = _(
                "Owner: <@{owner_id}> - use the buttons below to manage the room."
            ).format(owner_id=owner_id)
        else:
            header = _(
                "No owner right now - anyone in the room can Claim it below."
            )
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())

        locked = self._is_locked()
        hidden = self._is_hidden()
        row_one = discord.ui.ActionRow(
            _PanelButton(
                self._make_handler(_ACTION_LIMIT),
                label=_("Slots"),
                emoji="🔢",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_RENAME),
                label=_("Rename"),
                emoji="✏️",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_LOCK),
                label=_("Unlock") if locked else _("Lock"),
                emoji="🔓" if locked else "🔒",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_HIDE),
                label=_("Unhide") if hidden else _("Hide"),
                emoji="👁️" if hidden else "🙈",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_KICK),
                label=_("Kick + blacklist"),
                emoji="🔨",
                style=discord.ButtonStyle.danger,
            ),
        )
        row_two = discord.ui.ActionRow(
            _PanelButton(
                self._make_handler(_ACTION_UNBLACKLIST),
                label=_("Remove from blacklist"),
                emoji="♻️",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_TRANSFER),
                label=_("Transfer lead"),
                emoji="👑",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_BUMP),
                label=_("Bump to top"),
                emoji="⬆️",
                style=discord.ButtonStyle.secondary,
            ),
            _PanelButton(
                self._make_handler(_ACTION_CLAIM),
                label=_("Claim"),
                emoji="🙋",
                style=discord.ButtonStyle.success,
                custom_id=_CLAIM_CUSTOM_ID,
            ),
        )
        container.add_item(row_one)
        container.add_item(row_two)
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("-# Only the room owner can use these controls (except Claim).")
            )
        )
        self.add_item(container)

    async def _rerender(self):
        """Redraw the panel in place so stateful button labels stay accurate."""
        if self.message is None:
            return
        self._build()
        try:
            await self.message.edit(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.debug("Could not re-render room control panel", exc_info=True)

    async def _gone(self, interaction):
        await interactions.reply(interaction, _("This room no longer exists."))

    async def interaction_check(self, interaction):
        # Callbacks run in their own task with no locale set; resolve it here so
        # this check AND every action localise for the clicker.
        await i18n.apply_interaction_locale(interaction)
        # Cache is a fast path; the channel's manage_channels overwrite is the
        # source of truth, so this still resolves the owner right after a restart
        # wiped the in-memory map.
        owner_id = self.cog._owner_of(self.channel_id)
        if interaction.user.id == owner_id:
            return True
        # Claim is the one action anyone may reach (validated in its handler);
        # recognise its button by custom_id before rejecting non-owners.
        custom_id = None
        if isinstance(interaction.data, dict):
            custom_id = interaction.data.get("custom_id")
        if custom_id == _CLAIM_CUSTOM_ID:
            return True
        await interaction.response.send_message(
            _("Only the room owner can use these controls."), ephemeral=True
        )
        return False

    async def _dispatch(self, interaction, action):
        handlers = {
            _ACTION_LIMIT: self._do_limit,
            _ACTION_RENAME: self._do_rename,
            _ACTION_LOCK: self._do_lock,
            _ACTION_HIDE: self._do_hide,
            _ACTION_KICK: self._do_kick,
            _ACTION_UNBLACKLIST: self._do_unblacklist,
            _ACTION_TRANSFER: self._do_transfer,
            _ACTION_BUMP: self._do_bump,
            _ACTION_CLAIM: self._do_claim,
        }
        handler = handlers.get(action)
        if handler is None:
            await interactions.reply(interaction, _("Unknown action."))
            return
        try:
            await handler(interaction)
        except Exception:
            log.exception("Room control action %s failed", action)
            await interactions.notify_failure(
                interaction, _("Something went wrong with that action.")
            )

    # -- individual actions --------------------------------------------------

    async def _do_limit(self, interaction):
        if self._channel() is None:
            await self._gone(interaction)
            return
        await interaction.response.send_message(
            _("Choose a user limit for the room:"),
            view=_RoomSubView(_SlotSelect(self)),
            ephemeral=True,
        )

    async def _do_rename(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        await interaction.response.send_modal(_RoomRenameModal(self, channel.name))

    async def _do_lock(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        everyone = channel.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        locked = overwrite.connect is False
        overwrite.connect = None if locked else False
        try:
            await channel.set_permissions(everyone, overwrite=overwrite)
        except discord.HTTPException:
            await interactions.notify_failure(
                interaction, _("Could not change the lock.")
            )
            return
        await interactions.reply(
            interaction,
            _("Unlocked the room - anyone can join.")
            if locked
            else _("Locked the room - no one new can join."),
        )
        await self._rerender()

    async def _do_hide(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        everyone = channel.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        hidden = overwrite.view_channel is False
        overwrite.view_channel = None if hidden else False
        try:
            await channel.set_permissions(everyone, overwrite=overwrite)
        except discord.HTTPException:
            await interactions.notify_failure(
                interaction, _("Could not change visibility.")
            )
            return
        await interactions.reply(
            interaction,
            _("The room is visible again.")
            if hidden
            else _("Hid the room from everyone else."),
        )
        await self._rerender()

    async def _do_kick(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        members = [
            member
            for member in channel.members
            if not member.bot and member.id != interaction.user.id
        ]
        if not members:
            await interactions.reply(
                interaction, _("There's no one else in the room to remove.")
            )
            return
        await interaction.response.send_message(
            _("Pick who to kick and blacklist:"),
            view=_RoomSubView(_MemberActionSelect(self, members, _ACTION_KICK)),
            ephemeral=True,
        )

    async def _do_unblacklist(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        pairs = [
            (target, overwrite.connect)
            for target, overwrite in channel.overwrites.items()
            if isinstance(target, discord.Member)
        ]
        blocked = blacklisted_targets(pairs)
        if not blocked:
            await interactions.reply(interaction, _("No one is blacklisted here."))
            return
        await interaction.response.send_message(
            _("Pick who to remove from the blacklist:"),
            view=_RoomSubView(
                _MemberActionSelect(self, blocked, _ACTION_UNBLACKLIST)
            ),
            ephemeral=True,
        )

    async def _do_transfer(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        members = [
            member
            for member in channel.members
            if not member.bot and member.id != interaction.user.id
        ]
        if not members:
            await interactions.reply(
                interaction, _("There's no one else here to hand the room to.")
            )
            return
        await interaction.response.send_message(
            _("Pick the new room owner:"),
            view=_RoomSubView(_MemberActionSelect(self, members, _ACTION_TRANSFER)),
            ephemeral=True,
        )

    async def _do_bump(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        try:
            # Keep the hub's join-to-create channel first: bump to just BELOW it,
            # not to position 0 (which lands ABOVE the hub trigger channel).
            hub_channel = None
            category = channel.category
            if category is not None:
                for hub_id in self.cog._hub_index.get(channel.guild.id, {}):
                    candidate = channel.guild.get_channel(hub_id)
                    if candidate is not None and candidate.category_id == category.id:
                        hub_channel = candidate
                        break
            if hub_channel is not None:
                await channel.move(after=hub_channel)
            else:
                await channel.move(beginning=True)
        except discord.HTTPException:
            await interactions.notify_failure(
                interaction, _("Could not bump the room.")
            )
            return
        await interactions.reply(interaction, _("Bumped the room to the top."))

    async def _do_claim(self, interaction):
        channel = self._channel()
        if channel is None:
            await self._gone(interaction)
            return
        owner_id = self.cog._owner_of(self.channel_id)
        member_ids = [member.id for member in channel.members]
        if not claimable(owner_id, member_ids):
            await interactions.reply(
                interaction, _("The room already has an owner who's still here.")
            )
            return
        if interaction.user.id not in member_ids:
            await interactions.reply(
                interaction, _("Join the room first to claim it.")
            )
            return
        # Move the channel-backed ownership marker to the claimer.
        await self.cog._transfer_room(channel, interaction.user)
        await interactions.reply(
            interaction, _("You're now the owner of this room.")
        )
        await self._rerender()

    async def _handle_member_action(self, interaction, action, member_id):
        channel = self._channel()
        if channel is None:
            await interaction.response.edit_message(
                content=_("This room no longer exists."), view=None
            )
            return
        member = channel.guild.get_member(member_id)
        if member is None:
            await interaction.response.edit_message(
                content=_("That member is no longer here."), view=None
            )
            return
        try:
            if action == _ACTION_KICK:
                try:
                    await member.move_to(None)
                except discord.HTTPException:
                    log.debug("Could not move member out of room", exc_info=True)
                overwrite = channel.overwrites_for(member)
                overwrite.connect = False
                await channel.set_permissions(member, overwrite=overwrite)
                content = _("Removed and blacklisted {name}.").format(
                    name=member.display_name
                )
            elif action == _ACTION_UNBLACKLIST:
                overwrite = channel.overwrites_for(member)
                overwrite.connect = None
                await channel.set_permissions(member, overwrite=overwrite)
                content = _("Removed {name} from the blacklist.").format(
                    name=member.display_name
                )
            elif action == _ACTION_TRANSFER:
                # Move the channel-backed ownership marker to the new owner.
                await self.cog._transfer_room(channel, member)
                content = _("Transferred room ownership to {name}.").format(
                    name=member.display_name
                )
            else:
                content = _("Unknown action.")
        except discord.HTTPException:
            await interaction.response.edit_message(
                content=_("Could not complete that action."), view=None
            )
            return
        await interaction.response.edit_message(content=content, view=None)
        if action == _ACTION_TRANSFER:
            # Ownership moved: redraw the main panel so its header reflects it.
            await self._rerender()
