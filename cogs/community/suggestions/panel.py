"""The /suggestions manager panel: channel, staff role, on/off, Yasuho+ extras.

Authority (Manage Server) is re-checked on every click. Writes are deferred
first, saved to the guild_settings blob, then the digest timer is reconciled.

Typography rule: ASCII '-' and '...' only.
"""

import discord

from . import store
from tools import embed_creator, premium, premium_upsell, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView, LocaleModal


def weekday_name(index):
    return (
        _("Monday"), _("Tuesday"), _("Wednesday"), _("Thursday"),
        _("Friday"), _("Saturday"), _("Sunday"),
    )[index]


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Suggestions channel..."),
            min_values=1,
            max_values=1,
            default_values=[current] if current is not None else [],
            row=0,
        )

    async def callback(self, interaction):
        await self._owner.pick_channel(interaction, self.values[0].id)


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            placeholder=_("Staff role (optional)..."),
            min_values=0,
            max_values=1,
            default_values=[current] if current is not None else [],
            row=1,
        )

    async def callback(self, interaction):
        await self._owner.pick_role(interaction, self.values[0] if self.values else None)


class _RemoveSelect(discord.ui.Select):
    def __init__(self, panel, customs):
        self._owner = panel
        super().__init__(
            placeholder=_("Remove a custom status..."),
            options=[discord.SelectOption(label=c["label"], value=c["label"]) for c in customs],
            row=3,
        )

    async def callback(self, interaction):
        config = self._owner.config
        config["custom_statuses"] = [
            c for c in config["custom_statuses"] if c["label"] != self.values[0]
        ]
        await self._owner.save_and_rerender(interaction)


class _WeekdaySelect(discord.ui.Select):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            placeholder=_("Digest day (09:00 UTC)..."),
            options=[
                discord.SelectOption(label=weekday_name(i), value=str(i), default=i == current)
                for i in range(7)
            ],
            row=4,
        )

    async def callback(self, interaction):
        self._owner.config["digest_weekday"] = int(self.values[0])
        await self._owner.save_and_rerender(interaction)


class AddStatusModal(LocaleModal):
    """Yasuho+: one custom status (label and colour)."""

    def __init__(self, panel):
        super().__init__(title=_("Add a custom status"))
        self.panel = panel
        self.label = discord.ui.TextInput(
            label=_("Label"), min_length=1, max_length=store.LABEL_MAX
        )
        self.colour = discord.ui.TextInput(
            label=_("Colour (#RRGGBB)"), placeholder="#F1C40F", required=False, max_length=7
        )
        self.add_item(self.label)
        self.add_item(self.colour)

    async def on_submit(self, interaction):
        panel = self.panel
        if not panel.custom_allowed:
            return await panel.refuse_custom(interaction)
        label = self.label.value.strip()
        colour = store.parse_colour(self.colour.value) if self.colour.value.strip() else store.DEFAULT_COLOUR
        customs = panel.config["custom_statuses"]
        if colour is None:
            return await interaction.response.send_message(
                _("That colour is not valid. Use the form #RRGGBB."), ephemeral=True
            )
        if label in {c["label"] for c in customs} or len(customs) >= store.MAX_CUSTOM_STATUSES:
            return await interaction.response.send_message(
                _("Use a new label, and no more than {count} custom statuses.").format(
                    count=store.MAX_CUSTOM_STATUSES
                ),
                ephemeral=True,
            )
        customs.append({"label": label, "colour": colour})
        await panel.save_and_rerender(interaction)


class SuggestionsPanel(AuthorView):
    """The ephemeral manager."""

    def __init__(self, cog, guild, author_id, config, *, custom_open=False):
        super().__init__(author_id, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.config = config
        self.custom_open = custom_open

        toggle = discord.ui.Button(
            label=_("Disable") if config["enabled"] else _("Enable"),
            style=discord.ButtonStyle.danger if config["enabled"] else discord.ButtonStyle.success,
            row=2,
        )
        toggle.callback = self._toggle
        custom = discord.ui.Button(
            label=_("Customize"), style=discord.ButtonStyle.primary, row=2
        )
        custom.callback = self._customize
        items = [
            _ChannelSelect(self, guild.get_channel(config["channel_id"] or 0)),
            _RoleSelect(self, guild.get_role(config["staff_role_id"] or 0)),
            toggle,
            custom,
        ]
        if custom_open and self.custom_allowed:
            add = discord.ui.Button(
                label=_("Add status"), style=discord.ButtonStyle.secondary, row=2
            )
            add.callback = self._add_status
            digest = discord.ui.Button(
                label=_("Disable digest") if config["digest"] else _("Enable digest"),
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            digest.callback = self._toggle_digest
            items += [add, digest]
            if config["custom_statuses"]:
                items.append(_RemoveSelect(self, config["custom_statuses"]))
            items.append(_WeekdaySelect(self, config["digest_weekday"]))
        for item in items:
            self.add_item(item)

    @property
    def custom_allowed(self):
        return premium.resolve_guild_limits(self.cog.bot, self.guild.id).suggestions_custom

    async def interaction_check(self, interaction):
        # Authority is re-checked on every click, not only when the command opened it.
        if not await super().interaction_check(interaction):
            return False
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            await interaction.response.send_message(
                _("You need the Manage Server permission for this."), ephemeral=True
            )
            return False
        return True

    def build_embed(self):
        config = self.config
        allowed = self.custom_allowed
        embed = discord.Embed(
            title=_("Suggestion box"),
            description=_("Members post suggestions with /suggest and everyone votes on them."),
            colour=random_colour(),
        )
        embed.add_field(name=_("Status"), value=_("Enabled") if config["enabled"] else _("Disabled"))
        embed.add_field(
            name=_("Channel"),
            value=f"<#{config['channel_id']}>" if config["channel_id"] else _("*Not set.*"),
        )
        embed.add_field(
            name=_("Staff role"),
            value=f"<@&{config['staff_role_id']}>" if config["staff_role_id"] else _("*None.*"),
        )
        if allowed:
            embed.add_field(
                name=_("Custom statuses"),
                value=", ".join(c["label"] for c in config["custom_statuses"]) or _("*None.*"),
                inline=False,
            )
            embed.add_field(
                name=_("Weekly digest"),
                value=_("On, {day} 09:00 UTC").format(day=weekday_name(config["digest_weekday"]))
                if config["digest"]
                else _("Off"),
            )
        else:
            embed.set_footer(text=_("Yasuho+ unlocks custom statuses and a weekly digest."))
        return embed

    async def save_and_rerender(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()  # the writes below must not eat the 3 s
        await settings.set_guild(
            self.cog.bot.db_pool, self.guild.id, store.SETTINGS_KEY, dict(self.config)
        )
        await self.cog.reconcile_digest(self.guild.id)
        new = SuggestionsPanel(
            self.cog, self.guild, self.author_id, self.config, custom_open=self.custom_open
        )
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def pick_channel(self, interaction, channel_id):
        channel = self.guild.get_channel(channel_id)
        perms = channel.permissions_for(self.guild.me) if channel and self.guild.me else None
        if perms is None or not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                _(
                    "I can't post suggestions in that channel. Give me View Channel, "
                    "Send Messages and Embed Links there first."
                ),
                ephemeral=True,
            )
        self.config["channel_id"] = channel.id
        await self.save_and_rerender(interaction)

    async def pick_role(self, interaction, role):
        if role is not None and role.is_default():
            return await interaction.response.send_message(
                _("Pick a real role, not @everyone."), ephemeral=True
            )
        self.config["staff_role_id"] = role.id if role is not None else None
        await self.save_and_rerender(interaction)

    async def _toggle(self, interaction):
        if not self.config["enabled"] and not self.config["channel_id"]:
            return await interaction.response.send_message(
                _("Pick a suggestions channel first."), ephemeral=True
            )
        self.config["enabled"] = not self.config["enabled"]
        await self.save_and_rerender(interaction)

    async def _toggle_digest(self, interaction):
        self.config["digest"] = not self.config["digest"]
        await self.save_and_rerender(interaction)

    async def _add_status(self, interaction):
        await interaction.response.send_modal(AddStatusModal(self))

    async def _customize(self, interaction):
        if self.custom_allowed:
            new = SuggestionsPanel(
                self.cog, self.guild, self.author_id, self.config, custom_open=True
            )
            new.message = self.message
            self.stop()
            return await interaction.response.edit_message(embed=new.build_embed(), view=new)
        await self.refuse_custom(interaction)

    async def refuse_custom(self, interaction):
        upsell = await premium_upsell.for_guild_refusal(
            self.cog.bot,
            limit_key="suggestions_custom",
            guild_id=self.guild.id,
            person_id=interaction.user.id,
            is_admin=premium_upsell.invoker_is_admin(interaction.user),
            already_top_tier=premium_upsell.is_guild_already_top_tier(
                self.cog.bot, self.guild.id
            ),
            kind="unlock",
        )
        text = _("Custom statuses and the weekly digest are a Yasuho+ feature.")
        kwargs = {"ephemeral": True}
        if upsell is not None:
            text += "\n" + upsell.line
            view = upsell.view()
            if view is not None:
                kwargs["view"] = view
        await interaction.response.send_message(text, **kwargs)
