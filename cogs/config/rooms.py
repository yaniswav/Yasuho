"""Join-to-create voice hubs ("autorooms").

A guild can run up to five join-to-create voice hubs, one per game mode
(Ranked, Quickplay, Arcade, ...). Each hub owns a category, a trigger voice
channel and a room-name template. When a member joins a hub's trigger channel
we spin up a fresh temp room from the template, move them in, and delete the
room once it empties.

Configuration lives in the ``guild_settings`` JSONB (via ``tools/settings.py``)
under the ``autorooms`` key as a list of hub dicts. All shaping/validation is
delegated to the pure ``tools/autoroom.py`` helpers; this cog only performs the
Discord and DB side effects. On first load, any rows from the legacy
``auto_room`` table are migrated into default hubs and then ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

import discord
from discord.ext import commands

from tools import i18n, interactions, settings
from tools.autoroom import (
    CREATE_COOLDOWN_SECONDS,
    DEFAULT_LABEL,
    DEFAULT_TEMPLATE,
    GUILD_CHANNEL_BUDGET,
    HUB_OVERHEAD_CHANNELS,
    MAX_CATEGORIES,
    MAX_HUBS,
    SLOT_VALUES,
    blacklisted_targets,
    can_add_hub,
    channels_needed,
    claimable,
    default_hub,
    normalize_hubs,
    owner_from_overwrites,
    render_room_name,
    slot_value_label,
    summarise_hub,
)
from tools.cooldowns import Cooldowns
from tools.i18n import _
from tools.views import LocaleModal

log = logging.getLogger(__name__)

# Default channel names the Add modal prefills. These are channel names, not
# prose, so they stay as plain literals.
DEFAULT_CATEGORY_NAME = "Temp-Rooms"
DEFAULT_HUB_NAME = "Join to create"

# How often the empty-room reaper wakes to check a tracked room.
CLEANUP_INTERVAL_SECONDS = 15


def _parse_int(text, default):
    """Best-effort int parse: blank/garbage falls back to ``default``."""
    text = (text or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def _make_limit_select(current):
    """Build the user-limit Select (0 = unlimited) pre-selecting ``current``.

    The options are the curated ``SLOT_VALUES`` (0 reads as "unlimited"). When
    ``current`` matches one of them it is marked ``default`` so submitting the
    modal unchanged keeps the hub's limit; an off-menu value (e.g. a legacy
    typed 7) simply preselects nothing and the picker forces a choice.
    """
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = 0
    options = [
        discord.SelectOption(
            label=slot_value_label(value),
            value=str(value),
            default=(value == current),
        )
        for value in SLOT_VALUES
    ]
    return discord.ui.Select(
        placeholder=_("User limit..."),
        min_values=1,
        max_values=1,
        options=options,
    )


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


class _RemoveSelect(discord.ui.Select):
    """Select listing the guild's hubs; picking one removes it."""

    def __init__(self, handler, hubs):
        options = [
            discord.SelectOption(
                label=(hub.get("label") or DEFAULT_LABEL)[:100],
                value=hub["id"],
                description=summarise_hub(hub)[:100],
            )
            for hub in hubs
        ]
        super().__init__(
            placeholder=_("Remove a hub..."),
            min_values=1,
            max_values=1,
            options=options,
        )
        self._handler = handler

    async def callback(self, interaction):
        await self._handler(interaction, self.values[0])


class AddHubModal(LocaleModal):
    """Create a hub: its category, its trigger channel, and the config row.

    Five rows (Discord's modal maximum): label, category name, hub channel
    name, room template and user limit. ``max_rooms``/``private`` take sensible
    defaults and are tweaked later through the Edit modal.
    """

    def __init__(self, cog, panel):
        super().__init__(title=_("Add an autoroom hub"))
        self.cog = cog
        self.panel = panel
        self.label_input = discord.ui.TextInput(
            label=_("Hub label"),
            default=DEFAULT_LABEL,
            max_length=100,
            required=True,
        )
        self.category_input = discord.ui.TextInput(
            label=_("Category name"),
            default=DEFAULT_CATEGORY_NAME,
            max_length=100,
            required=True,
        )
        self.hub_input = discord.ui.TextInput(
            label=_("Join-to-create channel name"),
            default=DEFAULT_HUB_NAME,
            max_length=100,
            required=True,
        )
        self.template_input = discord.ui.TextInput(
            label=_("Room name template ({user}, {count})"),
            default=DEFAULT_TEMPLATE,
            max_length=100,
            required=True,
        )
        self.limit_select = _make_limit_select(0)
        for item in (
            self.label_input,
            self.category_input,
            self.hub_input,
            self.template_input,
        ):
            self.add_item(item)
        self.add_item(
            discord.ui.Label(
                text=_("User limit (0 = unlimited)"), component=self.limit_select
            )
        )

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        limit_values = self.limit_select.values
        message = await self.cog._add_hub(
            interaction.guild,
            label=(self.label_input.value or "").strip() or DEFAULT_LABEL,
            category_name=(self.category_input.value or "").strip()
            or DEFAULT_CATEGORY_NAME,
            hub_name=(self.hub_input.value or "").strip() or DEFAULT_HUB_NAME,
            template=(self.template_input.value or "").strip() or DEFAULT_TEMPLATE,
            user_limit=_parse_int(limit_values[0], 0) if limit_values else 0,
        )
        await interaction.followup.send(message, ephemeral=True)
        await self.panel._rerender()


class EditHubModal(LocaleModal):
    """Edit an existing hub's label, template and per-room limits.

    Channel/category renames are left to Discord's own UI; this covers the
    fields the pure config owns, including ``max_rooms`` and ``private`` that
    the Add modal has no room for.
    """

    def __init__(self, cog, panel, hub):
        super().__init__(title=_("Edit autoroom hub"))
        self.cog = cog
        self.panel = panel
        self.hub_id = hub["id"]
        self.label_input = discord.ui.TextInput(
            label=_("Hub label"),
            default=hub.get("label") or DEFAULT_LABEL,
            max_length=100,
            required=True,
        )
        self.template_input = discord.ui.TextInput(
            label=_("Room name template ({user}, {count})"),
            default=hub.get("template") or DEFAULT_TEMPLATE,
            max_length=100,
            required=True,
        )
        self.limit_select = _make_limit_select(hub.get("user_limit", 0))
        self.rooms_input = discord.ui.TextInput(
            label=_("Max rooms (1-50)"),
            default=str(hub.get("max_rooms", 20)),
            max_length=2,
            required=False,
        )
        is_private = bool(hub.get("private"))
        self.private_radio = discord.ui.RadioGroup(required=True)
        self.private_radio.add_option(
            label=_("Private"), value="private", default=is_private
        )
        self.private_radio.add_option(
            label=_("Public"), value="public", default=not is_private
        )
        self.add_item(self.label_input)
        self.add_item(self.template_input)
        self.add_item(
            discord.ui.Label(
                text=_("User limit (0 = unlimited)"), component=self.limit_select
            )
        )
        self.add_item(self.rooms_input)
        self.add_item(
            discord.ui.Label(text=_("Room privacy"), component=self.private_radio)
        )

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        limit_values = self.limit_select.values
        message = await self.cog._edit_hub(
            interaction.guild,
            self.hub_id,
            label=(self.label_input.value or "").strip() or DEFAULT_LABEL,
            template=(self.template_input.value or "").strip() or DEFAULT_TEMPLATE,
            user_limit=_parse_int(limit_values[0], 0) if limit_values else 0,
            max_rooms=_parse_int(self.rooms_input.value, 20),
            private=self.private_radio.value == "private",
        )
        await interaction.followup.send(message, ephemeral=True)
        await self.panel._rerender()


class _RenameChannelsModal(LocaleModal):
    """Rename a hub's category AND its join-to-create channel.

    ``EditHubModal`` is already at Discord's five-row limit, so channel renames
    live in their own two-field modal reached from the hub's manage chooser.
    Only the Discord channels are renamed; the pure hub config is untouched.
    """

    def __init__(self, cog, panel, hub, *, category_name, hub_name):
        super().__init__(title=_("Rename hub channels"))
        self.cog = cog
        self.panel = panel
        self.hub_id = hub["id"]
        self.category_input = discord.ui.TextInput(
            label=_("Category name"),
            default=category_name,
            max_length=100,
            required=True,
        )
        self.hub_input = discord.ui.TextInput(
            label=_("Join-to-create channel name"),
            default=hub_name,
            max_length=100,
            required=True,
        )
        self.add_item(self.category_input)
        self.add_item(self.hub_input)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.cog._rename_hub_channels(
            interaction.guild,
            self.hub_id,
            category_name=(self.category_input.value or "").strip(),
            hub_name=(self.hub_input.value or "").strip(),
        )
        await interaction.followup.send(message, ephemeral=True)
        await self.panel._rerender()


class _HubManageView(discord.ui.View):
    """Ephemeral chooser: edit a hub's settings or rename its channels.

    The panel's per-hub accessory opens this so both affordances stay tidy
    despite the Edit modal being full. It is only ever shown ephemerally to the
    panel author, so no author gating beyond that is needed.
    """

    def __init__(self, cog, panel, hub):
        super().__init__(timeout=120)
        self.cog = cog
        self.panel = panel
        self.hub = hub
        self.add_item(
            _PanelButton(
                self._on_settings,
                label=_("Settings"),
                style=discord.ButtonStyle.primary,
                emoji="⚙️",
            )
        )
        self.add_item(
            _PanelButton(
                self._on_rename,
                label=_("Rename channels"),
                style=discord.ButtonStyle.secondary,
                emoji="✏️",
            )
        )

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return True

    async def _on_settings(self, interaction):
        await interaction.response.send_modal(
            EditHubModal(self.cog, self.panel, self.hub)
        )

    async def _on_rename(self, interaction):
        guild = interaction.guild
        category = (
            guild.get_channel(self.hub.get("category_id"))
            if self.hub.get("category_id")
            else None
        )
        hub_channel = (
            guild.get_channel(self.hub.get("hub_channel_id"))
            if self.hub.get("hub_channel_id")
            else None
        )
        category_name = (
            category.name
            if isinstance(category, discord.CategoryChannel)
            else DEFAULT_CATEGORY_NAME
        )
        hub_name = hub_channel.name if hub_channel is not None else DEFAULT_HUB_NAME
        await interaction.response.send_modal(
            _RenameChannelsModal(
                self.cog,
                self.panel,
                self.hub,
                category_name=category_name[:100],
                hub_name=hub_name[:100],
            )
        )


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


class AutoroomPanel(discord.ui.LayoutView):
    """Styled Components V2 panel for managing a guild's autoroom hubs.

    Follows the ``MusicController`` reference: a coloured Container lists each
    hub as a Section (label + summary) with an Edit accessory, plus an Add
    button, a Remove select and a footer showing the hub count and channel
    budget. Restricted to the member who opened it.
    """

    def __init__(self, cog, author_id, guild_id, hubs, *, used_channels=0):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.guild_id = guild_id
        self.hubs = hubs
        self.used_channels = used_channels
        self.message = None
        self._build()

    def _build(self):
        """(Re)assemble the layout from the current hub list."""
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(_("## Autoroom hubs")))
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "Join-to-create voice hubs. Members who join a hub get their "
                    "own temp room, cleaned up when it empties."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        if not self.hubs:
            container.add_item(
                discord.ui.TextDisplay(
                    _("No hubs yet. Use **Add hub** to create your first one.")
                )
            )
        else:
            for hub in self.hubs:
                edit_button = _PanelButton(
                    self._make_edit_handler(hub["id"]),
                    label=_("Edit"),
                    style=discord.ButtonStyle.secondary,
                    emoji="⚙️",
                )
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(
                            _("**{label}**\n{summary}").format(
                                label=hub.get("label") or DEFAULT_LABEL,
                                summary=summarise_hub(hub),
                            )
                        ),
                        accessory=edit_button,
                    )
                )

        container.add_item(discord.ui.Separator())

        add_button = _PanelButton(
            self._on_add,
            label=_("Add hub"),
            style=discord.ButtonStyle.success,
            emoji="➕",
            disabled=not can_add_hub(self.hubs),
        )
        container.add_item(discord.ui.ActionRow(add_button))

        if self.hubs:
            container.add_item(
                discord.ui.ActionRow(_RemoveSelect(self._on_remove, self.hubs))
            )

        reserved = channels_needed(self.hubs)
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "-# {count}/{max_hubs} hubs - ~{reserved} channels reserved - "
                    "{used}/{budget} channels used"
                ).format(
                    count=len(self.hubs),
                    max_hubs=MAX_HUBS,
                    reserved=reserved,
                    used=self.used_channels,
                    budget=GUILD_CHANNEL_BUDGET,
                )
            )
        )

        self.add_item(container)

    def _make_edit_handler(self, hub_id):
        async def handler(interaction):
            await self._on_edit(interaction, hub_id)

        return handler

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where the locale was never
        # set; resolve it here so this check AND the callbacks localise.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This panel isn't for you."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.walk_children():
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _rerender(self):
        """Reload hubs and redraw the panel in place after a change."""
        if self.message is None:
            return
        guild = self.cog.bot.get_guild(self.guild_id)
        self.used_channels = len(guild.channels) if guild is not None else 0
        self.hubs = await self.cog._load_hubs(self.guild_id)
        self._build()
        try:
            await self.message.edit(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.exception("Failed to refresh the autoroom panel")

    async def _on_add(self, interaction):
        try:
            if not can_add_hub(self.hubs):
                await interaction.response.send_message(
                    _("You already have the maximum of {max_hubs} hubs.").format(
                        max_hubs=MAX_HUBS
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(AddHubModal(self.cog, self))
        except Exception:
            log.exception("Autoroom add-hub failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong opening that form.")
            )

    async def _on_edit(self, interaction, hub_id):
        try:
            hub = next((h for h in self.hubs if h["id"] == hub_id), None)
            if hub is None:
                await interaction.response.send_message(
                    _("That hub no longer exists."), ephemeral=True
                )
                return
            await interaction.response.send_message(
                _("Manage the **{label}** hub:").format(
                    label=hub.get("label") or DEFAULT_LABEL
                ),
                view=_HubManageView(self.cog, self, hub),
                ephemeral=True,
            )
        except Exception:
            log.exception("Autoroom edit-hub failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong opening that form.")
            )

    async def _on_remove(self, interaction, hub_id):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            message = await self.cog._remove_hub(interaction.guild, hub_id)
            await interaction.followup.send(message, ephemeral=True)
            await self._rerender()
        except Exception:
            log.exception("Autoroom remove-hub failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong removing that hub.")
            )


class TemporaryRooms(commands.Cog):
    """Create and clean up temporary voice rooms from join-to-create hubs."""

    def __init__(self, bot):
        self.bot = bot
        # {guild_id: {hub_channel_id: hub}} - the fast negative-cached lookup
        # the voice event consults. A guild absent here has no hubs, so
        # unconfigured guilds cost zero work on every voice update.
        self._hub_index = {}
        # {(guild_id, hub_id): set(channel_id)} temp rooms alive per hub.
        self._active = defaultdict(set)
        # Per-(guild, user) anti-spam debounce that prunes itself (was an
        # unbounded dict of last-created timestamps).
        self._cooldowns = Cooldowns(CREATE_COOLDOWN_SECONDS)
        self._locks = defaultdict(asyncio.Lock)  # per-guild creation lock
        self._cleanup_tasks = set()  # outstanding empty-room reapers
        # {channel_id: owner_user_id} - a fast CACHE of room ownership. The
        # source of truth is the owner's manage_channels overwrite ON the
        # channel, so a restart re-seeds this map from the channels themselves
        # (see the on_ready re-adoption). Everything else (limit, name, lock,
        # hide, blacklist) is likewise channel-backed and survives a restart.
        # Dropped when the reaper deletes the room.
        self._room_owners = {}
        # {channel_id: RoomControlView} so the timeout=None panels can be
        # stopped when their room is reaped.
        self._room_views = {}
        # Guard so the once-per-process startup re-adoption (which survives the
        # restart) runs a single time even though on_ready can fire repeatedly.
        self._adopted = False

    # ------------------------------------------------------------------
    # Load / migration
    # ------------------------------------------------------------------

    async def cog_load(self):
        """Seed the hub index from settings, migrating legacy rows once."""
        self._hub_index = {}
        configured = set()
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT guild_id, settings FROM guild_settings"
            )
        except Exception:
            log.exception("Failed to load autoroom settings")
            rows = []
        for row in rows:
            raw = row["settings"]
            try:
                data = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (TypeError, ValueError):
                continue
            if "autorooms" not in data:
                continue
            configured.add(int(row["guild_id"]))
            hubs = normalize_hubs(data.get("autorooms"))
            if hubs:
                self._index_guild(int(row["guild_id"]), hubs)
        await self._migrate_legacy(configured)

    async def _migrate_legacy(self, configured):
        """Fold old ``auto_room`` rows into default hubs (best-effort, once)."""
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT guild_id, channel_id FROM auto_room"
            )
        except Exception:
            return
        by_guild = defaultdict(list)
        for row in rows:
            by_guild[int(row["guild_id"])].append(int(row["channel_id"]))
        for guild_id, channel_ids in by_guild.items():
            if guild_id in configured:
                continue  # already migrated / configured on the JSONB store
            hubs = []
            for channel_id in channel_ids:
                if not can_add_hub(hubs):
                    break
                channel = self.bot.get_channel(channel_id)
                category = getattr(channel, "category", None)
                hubs.append(
                    default_hub(
                        hub_channel_id=channel_id,
                        category_id=category.id if category else None,
                    )
                )
            if not hubs:
                continue
            try:
                await self._save_hubs(guild_id, hubs)
            except Exception:
                log.exception("Failed to migrate legacy autorooms for %s", guild_id)

    def cog_unload(self):
        """Cancel outstanding reapers and stop any live room control panels."""
        for task in list(self._cleanup_tasks):
            task.cancel()
        for view in list(self._room_views.values()):
            view.stop()
        self._room_views.clear()
        self._room_owners.clear()

    def _forget_room(self, channel_id):
        """Drop a temp room's in-memory state and stop its control panel."""
        self._room_owners.pop(channel_id, None)
        view = self._room_views.pop(channel_id, None)
        if view is not None:
            view.stop()

    async def _grant_room_owner(self, channel, member):
        """Grant ``member`` owner-level perms on a temp room (best-effort).

        The ``manage_channels`` grant is doubly meaningful: it hands the member
        real control of their own room AND marks ownership on the channel itself,
        which is the source of truth that lets ownership survive a restart.
        """
        try:
            overwrite = channel.overwrites_for(member)
            overwrite.manage_channels = True
            overwrite.move_members = True
            overwrite.connect = True
            await channel.set_permissions(member, overwrite=overwrite)
        except discord.HTTPException:
            log.debug("Could not grant room owner perms", exc_info=True)

    async def _revoke_room_owner(self, channel, member):
        """Strip the ownership marker from ``member`` on a temp room (best-effort).

        Clears the ``manage_channels``/``move_members`` grant so the channel no
        longer reports ``member`` as its owner; ``connect`` is left untouched.
        """
        try:
            overwrite = channel.overwrites_for(member)
            overwrite.manage_channels = None
            overwrite.move_members = None
            await channel.set_permissions(member, overwrite=overwrite)
        except discord.HTTPException:
            log.debug("Could not revoke room owner perms", exc_info=True)

    def _owner_from_channel(self, channel):
        """Read the owner id off a room's channel overwrites, or ``None``.

        The channel-backed source of truth: whichever member holds an explicit
        ``manage_channels`` grant owns the room. Roles are ignored - only member
        overwrites can mark ownership.
        """
        pairs = [
            (target.id, overwrite.manage_channels)
            for target, overwrite in channel.overwrites.items()
            if isinstance(target, discord.Member)
        ]
        return owner_from_overwrites(pairs)

    def _owner_of(self, channel_id):
        """Resolve a room's owner: cache first, then the channel overwrite.

        The in-memory map is only a cache; the channel's ``manage_channels``
        overwrite is authoritative. When the cache misses (e.g. right after a
        restart) we read the channel and re-seed the cache so later lookups are
        cheap. Returns ``None`` when the room has no owner.
        """
        owner_id = self._room_owners.get(channel_id)
        if owner_id is not None:
            return owner_id
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.VoiceChannel):
            owner_id = self._owner_from_channel(channel)
            if owner_id is not None:
                self._room_owners[channel_id] = owner_id
        return owner_id

    async def _transfer_room(self, channel, member):
        """Move room ownership to ``member`` on both the channel and the cache.

        Revokes the previous owner's ``manage_channels`` marker (so exactly one
        member owns the channel), grants it to ``member`` and updates the cache.
        """
        prev_id = self._owner_of(channel.id)
        if prev_id is not None and prev_id != member.id:
            prev = channel.guild.get_member(prev_id)
            if prev is not None:
                await self._revoke_room_owner(channel, prev)
        self._room_owners[channel.id] = member.id
        await self._grant_room_owner(channel, member)

    async def _post_room_controls(self, channel, owner):
        """Post the per-room voicemaster control panel in the room's chat.

        A Components V2 ``LayoutView`` is sent with ``view=`` only (no embed, no
        content); the owner is read from ``_room_owners`` when the view builds
        its header, so ``owner`` is only needed to ensure the map is populated
        before the panel renders (the room-creation path already sets it).
        """
        try:
            view = RoomControlView(self, channel.id)
            message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = message
            self._room_views[channel.id] = view
        except discord.HTTPException:
            log.debug("Could not post room control panel", exc_info=True)

    # ------------------------------------------------------------------
    # Config helpers (settings <-> index)
    # ------------------------------------------------------------------

    def _index_guild(self, guild_id, hubs):
        """Point the in-memory index at ``hubs`` for one guild."""
        mapping = {hub["hub_channel_id"]: hub for hub in hubs if hub.get("hub_channel_id")}
        if mapping:
            self._hub_index[guild_id] = mapping
        else:
            self._hub_index.pop(guild_id, None)

    async def _load_hubs(self, guild_id):
        """Return the guild's normalised hub list from settings."""
        blob = await settings.get_guild(self.bot.db_pool, guild_id, "autorooms", [])
        return normalize_hubs(blob)

    async def _save_hubs(self, guild_id, hubs):
        """Persist ``hubs`` to settings and refresh the in-memory index."""
        hubs = normalize_hubs(hubs)
        await settings.set_guild(self.bot.db_pool, guild_id, "autorooms", hubs)
        self._index_guild(guild_id, hubs)
        return hubs

    # ------------------------------------------------------------------
    # Add / edit / remove (called from the panel/modals)
    # ------------------------------------------------------------------

    async def _add_hub(self, guild, *, label, category_name, hub_name, template, user_limit):
        """Create the hub's category + trigger channel, then save the config."""
        hubs = await self._load_hubs(guild.id)
        if not can_add_hub(hubs):
            return _("You already have the maximum of {max_hubs} hubs.").format(
                max_hubs=MAX_HUBS
            )
        if len(guild.categories) >= MAX_CATEGORIES:
            return _(
                "This server is at Discord's limit of {max} categories."
            ).format(max=MAX_CATEGORIES)
        if len(guild.channels) + HUB_OVERHEAD_CHANNELS > GUILD_CHANNEL_BUDGET:
            return _(
                "This server is too close to Discord's {budget}-channel limit "
                "to add another hub."
            ).format(budget=GUILD_CHANNEL_BUDGET)

        try:
            category = await guild.create_category(category_name[:100])
            hub_channel = await guild.create_voice_channel(
                hub_name[:100], category=category
            )
        except discord.HTTPException:
            log.exception("Failed to create autoroom channels")
            return _("Something went wrong while creating the hub's channels.")

        hub = default_hub(
            label=label,
            category_id=category.id,
            hub_channel_id=hub_channel.id,
            template=template,
            user_limit=user_limit,
        )
        hubs.append(hub)
        await self._save_hubs(guild.id, hubs)
        return _("Created the **{label}** hub. Members can join {channel} now.").format(
            label=hub["label"], channel=hub_channel.mention
        )

    async def _edit_hub(
        self, guild, hub_id, *, label, template, user_limit, max_rooms, private
    ):
        """Update the editable fields of an existing hub."""
        hubs = await self._load_hubs(guild.id)
        hub = next((h for h in hubs if h["id"] == hub_id), None)
        if hub is None:
            return _("That hub no longer exists.")
        hub.update(
            label=label,
            template=template,
            user_limit=user_limit,
            max_rooms=max_rooms,
            private=private,
        )
        await self._save_hubs(guild.id, hubs)
        return _("Updated the **{label}** hub.").format(label=hub["label"])

    async def _remove_hub(self, guild, hub_id):
        """Delete a hub's category + trigger channel and drop its config."""
        hubs = await self._load_hubs(guild.id)
        hub = next((h for h in hubs if h["id"] == hub_id), None)
        if hub is None:
            return _("That hub no longer exists.")

        # Delete the trigger channel, then the category and everything left in it
        # (its live temp rooms). Best-effort: config removal must still proceed.
        category = guild.get_channel(hub["category_id"]) if hub.get("category_id") else None
        hub_channel = (
            guild.get_channel(hub["hub_channel_id"]) if hub.get("hub_channel_id") else None
        )
        if hub_channel is not None:
            try:
                await hub_channel.delete()
            except discord.HTTPException:
                log.debug("Could not delete hub channel", exc_info=True)
        if isinstance(category, discord.CategoryChannel):
            for child in list(category.channels):
                self._forget_room(child.id)
                try:
                    await child.delete()
                except discord.HTTPException:
                    log.debug("Could not delete hub category child", exc_info=True)
            try:
                await category.delete()
            except discord.HTTPException:
                log.debug("Could not delete hub category", exc_info=True)

        self._active.pop((guild.id, hub_id), None)
        remaining = [h for h in hubs if h["id"] != hub_id]
        await self._save_hubs(guild.id, remaining)
        return _("Removed the **{label}** hub.").format(
            label=hub.get("label") or DEFAULT_LABEL
        )

    async def _rename_hub_channels(self, guild, hub_id, *, category_name, hub_name):
        """Rename a hub's category and join-to-create channel (best-effort).

        Only the Discord channels are touched; the hub's stored config is
        unchanged. Blank fields are skipped, and a missing channel is simply
        reported rather than raising.
        """
        hubs = await self._load_hubs(guild.id)
        hub = next((h for h in hubs if h["id"] == hub_id), None)
        if hub is None:
            return _("That hub no longer exists.")

        category = (
            guild.get_channel(hub["category_id"]) if hub.get("category_id") else None
        )
        hub_channel = (
            guild.get_channel(hub["hub_channel_id"])
            if hub.get("hub_channel_id")
            else None
        )
        renamed = []
        if category_name and isinstance(category, discord.CategoryChannel):
            try:
                await category.edit(name=category_name[:100])
                renamed.append(_("category"))
            except discord.HTTPException:
                log.debug("Could not rename hub category", exc_info=True)
        if hub_name and isinstance(hub_channel, discord.VoiceChannel):
            try:
                await hub_channel.edit(name=hub_name[:100])
                renamed.append(_("join-to-create channel"))
            except discord.HTTPException:
                log.debug("Could not rename hub channel", exc_info=True)

        if not renamed:
            return _("Nothing was renamed - those channels are missing.")
        return _("Renamed the {targets}.").format(targets=" & ".join(renamed))

    # ------------------------------------------------------------------
    # Room creation + cleanup
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not after.channel:
            return
        hubs = self._hub_index.get(member.guild.id)
        if not hubs:
            return
        hub = hubs.get(after.channel.id)
        if hub is None:
            return

        # Per-user cooldown to kill join/leave spam.
        key = (member.guild.id, member.id)
        if self._cooldowns.is_active(key):
            return
        self._cooldowns.touch(key)

        async with self._locks[member.guild.id]:
            await self._create_room(member, after.channel, hub)

    async def _create_room(self, member, hub_channel, hub):
        """Provision one temp room for ``member`` from ``hub``'s template."""
        try:
            guild = member.guild
            category = None
            if hub.get("category_id"):
                category = guild.get_channel(hub["category_id"])
            if not isinstance(category, discord.CategoryChannel):
                category = hub_channel.category
            if category is None:
                return

            # Enforce the hub's max_rooms cap, pruning any rooms already gone.
            active = {
                cid
                for cid in self._active[(guild.id, hub["id"])]
                if self.bot.get_channel(cid) is not None
            }
            self._active[(guild.id, hub["id"])] = active
            if len(active) >= hub["max_rooms"]:
                return  # at capacity: refuse quietly

            name = render_room_name(
                hub["template"], member.display_name, len(active) + 1
            )
            kwargs = {"category": category}
            if hub["user_limit"] > 0:
                kwargs["user_limit"] = hub["user_limit"]
            if hub["private"]:
                kwargs["overwrites"] = {
                    member: discord.PermissionOverwrite(
                        manage_channels=True, move_members=True
                    )
                }

            new_channel = await guild.create_voice_channel(name, **kwargs)
            active.add(new_channel.id)
            # Cache the owner in memory (fast path) AND stamp a manage_channels
            # overwrite on the channel itself: that overwrite is the source of
            # truth, so ownership survives a restart that wipes this map.
            self._room_owners[new_channel.id] = member.id
            await self._grant_room_owner(new_channel, member)
            try:
                await member.move_to(new_channel)
            except discord.HTTPException:
                log.debug("Could not move member into new room", exc_info=True)

            await self._post_room_controls(new_channel, member)

            task = asyncio.create_task(
                self._cleanup_room(new_channel.id, guild.id, hub["id"])
            )
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)
        except Exception:
            log.exception("Failed to create an autoroom temp room")

    async def _cleanup_room(self, channel_id, guild_id, hub_id):
        """Delete the temp room once it empties, then forget it."""
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    self._active[(guild_id, hub_id)].discard(channel_id)
                    self._forget_room(channel_id)
                    return
                if len(channel.members) == 0:
                    try:
                        await channel.delete()
                    except discord.HTTPException:
                        pass
                    self._active[(guild_id, hub_id)].discard(channel_id)
                    self._forget_room(channel_id)
                    return
            except Exception:
                log.exception("Failed to clean up temp room %s", channel_id)
                return

    # ------------------------------------------------------------------
    # Startup re-adoption (survive a restart)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        """Re-adopt temp rooms left behind by a restart, exactly once.

        This is the guaranteed survive-restart mechanism: it needs no persistent
        views and no per-room DB state. ``on_ready`` can fire repeatedly on
        reconnects, so a flag keeps the sweep to a single run. It must never
        crash startup, hence the broad guard.
        """
        if self._adopted:
            return
        self._adopted = True
        try:
            await self._adopt_existing_rooms()
        except Exception:
            log.exception("Autoroom startup re-adoption failed")

    async def _adopt_existing_rooms(self):
        """Sweep every configured hub's category for orphaned temp rooms."""
        for guild_id, hub_map in list(self._hub_index.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            for hub in list(hub_map.values()):
                try:
                    await self._adopt_hub_rooms(guild, hub)
                except Exception:
                    log.exception(
                        "Failed to re-adopt rooms for hub %s in guild %s",
                        hub.get("id"),
                        guild_id,
                    )

    async def _adopt_hub_rooms(self, guild, hub):
        """Re-adopt (or reap) the temp rooms sitting in one hub's category.

        Every voice channel in the hub's category other than the trigger channel
        is treated as a temp room: empty ones are reaped, live ones are re-armed.
        """
        category_id = hub.get("category_id")
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            return
        hub_channel_id = hub.get("hub_channel_id")
        for channel in list(category.voice_channels):
            if channel.id == hub_channel_id:
                continue  # the join-to-create trigger, never a temp room
            try:
                await self._adopt_room(channel, guild, hub)
            except Exception:
                log.exception("Failed to re-adopt temp room %s", channel.id)

    async def _adopt_room(self, channel, guild, hub):
        """Reap an empty leftover room, or re-arm a live one after a restart.

        Live rooms: re-seed ownership from the channel's ``manage_channels``
        overwrite (may be absent -> left unowned so the panel offers Claim),
        purge the stale inert control panel, post a fresh one, and re-arm the
        empty-room reaper. All Discord calls are best-effort.
        """
        if len(channel.members) == 0:
            try:
                await channel.delete()
            except discord.HTTPException:
                log.debug("Could not reap empty leftover room", exc_info=True)
            self._forget_room(channel.id)
            return

        owner_id = self._owner_from_channel(channel)
        if owner_id is not None:
            self._room_owners[channel.id] = owner_id
        self._active[(guild.id, hub["id"])].add(channel.id)

        await self._purge_stale_panels(channel)
        await self._post_room_controls(channel, None)

        task = asyncio.create_task(
            self._cleanup_room(channel.id, guild.id, hub["id"])
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _purge_stale_panels(self, channel):
        """Delete the bot's own recent messages in a room's voice text chat.

        After a restart the old control panel is inert (its view is dead), so we
        clear the bot's recent messages before posting a fresh panel. Deleted one
        by one (not bulk) so it works regardless of message age and needs no
        manage_messages permission for the bot's own messages. Best-effort.
        """
        me = self.bot.user
        if me is None:
            return
        try:
            await channel.purge(
                limit=10,
                check=lambda message: message.author.id == me.id,
                bulk=False,
            )
        except (discord.HTTPException, AttributeError):
            log.debug("Could not purge stale room panels", exc_info=True)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(
        name="autoroom",
        aliases=["auto_room", "autorooms", "auto_rooms", "room", "rooms"],
        fallback="panel",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def autoroom(self, ctx):
        """Open the autoroom hub setup panel."""
        await self._send_panel(ctx)

    async def _send_panel(self, ctx):
        hubs = await self._load_hubs(ctx.guild.id)
        view = AutoroomPanel(
            self,
            ctx.author.id,
            ctx.guild.id,
            hubs,
            used_channels=len(ctx.guild.channels),
        )
        message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        view.message = message

    @autoroom.command(name="list", aliases=["ls"])
    @commands.guild_only()
    async def list_autorooms(self, ctx):
        """List the configured autoroom hubs in this server."""
        hubs = await self._load_hubs(ctx.guild.id)
        if not hubs:
            await ctx.send(_("There are no autoroom hubs set up in this server."))
            return

        embed = discord.Embed(
            title=_("Autoroom hubs"), colour=discord.Colour.blurple()
        )
        for hub in hubs:
            channel = ctx.guild.get_channel(hub.get("hub_channel_id"))
            location = channel.mention if channel else _("channel missing")
            embed.add_field(
                name=hub.get("label") or DEFAULT_LABEL,
                value=_("{location}\n{summary}").format(
                    location=location, summary=summarise_hub(hub)
                ),
                inline=False,
            )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(TemporaryRooms(bot))
