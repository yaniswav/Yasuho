"""Hub setup/config surfaces for the autoroom feature.

Move-only split from ``rooms.py``: the Components V2 hub-management panel
(``AutoroomPanel``) and its modals/selects (add/edit/rename hub, remove select,
manage chooser) that shape the guild's ``autorooms`` config. Every Discord/DB
side effect still lives on the cog and is reached through the ``self.cog``
reference each component is constructed with.
"""

from __future__ import annotations

import logging

import discord

from .rooms_panels import _PanelButton
from tools import i18n, interactions
from tools.autoroom import (
    DEFAULT_LABEL,
    DEFAULT_TEMPLATE,
    GUILD_CHANNEL_BUDGET,
    MAX_HUBS,
    SLOT_VALUES,
    can_add_hub,
    channels_needed,
    slot_value_label,
    summarise_hub,
)
from tools.i18n import _
from tools.views import LocaleModal

log = logging.getLogger(__name__)

# Default channel names the Add modal prefills. These are channel names, not
# prose, so they stay as plain literals.
DEFAULT_CATEGORY_NAME = "Temp-Rooms"
DEFAULT_HUB_NAME = "Join to create"


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
