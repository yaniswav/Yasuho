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
import time
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
    can_add_hub,
    channels_needed,
    default_hub,
    normalize_hubs,
    render_room_name,
    summarise_hub,
)
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


def _parse_bool(text):
    """Read a loose yes/no answer from a modal text field.

    Also accepts the current locale's translation of "yes"/"y": the Edit modal
    prefills this field with the localized _("yes")/_("no"), so a value left
    unchanged in a non-English locale must still round-trip to True instead of
    silently reading as False and un-setting the hub's private flag.
    """
    value = (text or "").strip().lower()
    affirmatives = {"1", "yes", "y", "true", "on", "private", "locked"}
    affirmatives.add(_("yes").strip().lower())
    affirmatives.add(_("y").strip().lower())
    return value in affirmatives


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
        self.limit_input = discord.ui.TextInput(
            label=_("User limit (0 = unlimited)"),
            default="0",
            max_length=2,
            required=False,
        )
        for item in (
            self.label_input,
            self.category_input,
            self.hub_input,
            self.template_input,
            self.limit_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.cog._add_hub(
            interaction.guild,
            label=(self.label_input.value or "").strip() or DEFAULT_LABEL,
            category_name=(self.category_input.value or "").strip()
            or DEFAULT_CATEGORY_NAME,
            hub_name=(self.hub_input.value or "").strip() or DEFAULT_HUB_NAME,
            template=(self.template_input.value or "").strip() or DEFAULT_TEMPLATE,
            user_limit=_parse_int(self.limit_input.value, 0),
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
        self.limit_input = discord.ui.TextInput(
            label=_("User limit (0 = unlimited)"),
            default=str(hub.get("user_limit", 0)),
            max_length=2,
            required=False,
        )
        self.rooms_input = discord.ui.TextInput(
            label=_("Max rooms (1-50)"),
            default=str(hub.get("max_rooms", 20)),
            max_length=2,
            required=False,
        )
        self.private_input = discord.ui.TextInput(
            label=_("Private rooms? (yes/no)"),
            default=_("yes") if hub.get("private") else _("no"),
            max_length=5,
            required=False,
        )
        for item in (
            self.label_input,
            self.template_input,
            self.limit_input,
            self.rooms_input,
            self.private_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.cog._edit_hub(
            interaction.guild,
            self.hub_id,
            label=(self.label_input.value or "").strip() or DEFAULT_LABEL,
            template=(self.template_input.value or "").strip() or DEFAULT_TEMPLATE,
            user_limit=_parse_int(self.limit_input.value, 0),
            max_rooms=_parse_int(self.rooms_input.value, 20),
            private=_parse_bool(self.private_input.value),
        )
        await interaction.followup.send(message, ephemeral=True)
        await self.panel._rerender()


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
            await interaction.response.send_modal(EditHubModal(self.cog, self, hub))
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
        # {(guild_id, user_id): monotonic} last-created timestamp (anti-spam).
        self._cooldowns = {}
        self._locks = defaultdict(asyncio.Lock)  # per-guild creation lock
        self._cleanup_tasks = set()  # outstanding empty-room reapers

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
        """Cancel any outstanding empty-room reapers on unload."""
        for task in list(self._cleanup_tasks):
            task.cancel()

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
        now = time.monotonic()
        key = (member.guild.id, member.id)
        if now - self._cooldowns.get(key, 0.0) < CREATE_COOLDOWN_SECONDS:
            return
        self._cooldowns[key] = now

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
            try:
                await member.move_to(new_channel)
            except discord.HTTPException:
                log.debug("Could not move member into new room", exc_info=True)

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
                    return
                if len(channel.members) == 0:
                    try:
                        await channel.delete()
                    except discord.HTTPException:
                        pass
                    self._active[(guild_id, hub_id)].discard(channel_id)
                    return
            except Exception:
                log.exception("Failed to clean up temp room %s", channel_id)
                return

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
