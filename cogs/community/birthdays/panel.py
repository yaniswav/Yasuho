"""The /birthdays manager panel: channel, role, hour, timezone, Yasuho+ message.

Authority (Manage Server) is re-checked on every click. Writes are deferred
first, saved to the guild_settings blob, then the daily timer is reconciled.

Typography rule: ASCII '-' and '...' only.
"""

import discord

from . import store
from tools import embed_creator, premium, premium_upsell, role_audit, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

PLACEHOLDER_HINT = "{users} {server}"


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Birthday channel..."),
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
            placeholder=_("Birthday role (optional)..."),
            min_values=0,
            max_values=1,
            default_values=[current] if current is not None else [],
            row=1,
        )

    async def callback(self, interaction):
        await self._owner.pick_role(interaction, self.values[0] if self.values else None)


class _HourSelect(discord.ui.Select):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            placeholder=_("Hour of the celebration..."),
            options=[
                discord.SelectOption(label=f"{h:02d}:00", value=str(h), default=h == current)
                for h in range(24)
            ],
            row=2,
        )

    async def callback(self, interaction):
        self._owner.config["hour"] = int(self.values[0])
        await self._owner.save_and_rerender(interaction)


class TimezoneModal(LocaleModal):
    def __init__(self, panel):
        super().__init__(title=_("Timezone"))
        self.panel = panel
        self.zone = discord.ui.TextInput(
            label=_("IANA timezone"),
            placeholder="Europe/Paris",
            default=panel.config["timezone"],
            max_length=64,
        )
        self.add_item(self.zone)

    async def on_submit(self, interaction):
        name = self.zone.value.strip()
        if not store.valid_timezone(name):
            return await interaction.response.send_message(
                _("That timezone is not valid. Use a name like Europe/Paris or America/New_York."),
                ephemeral=True,
            )
        self.panel.config["timezone"] = name
        await self.panel.save_and_rerender(interaction)


class MessageModal(LocaleModal):
    """Yasuho+: the custom celebration message."""

    def __init__(self, panel):
        super().__init__(title=_("Custom message"))
        self.panel = panel
        self.message_field = discord.ui.TextInput(
            label=_("Message (empty = default)"),
            placeholder=PLACEHOLDER_HINT,
            default=panel.config["message"],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=store.MAX_MESSAGE_LENGTH,
        )
        self.add_item(self.message_field)

    async def on_submit(self, interaction):
        if not self.panel.custom_allowed:
            return await self.panel.refuse_custom(interaction)
        self.panel.config["message"] = (self.message_field.value or "").strip() or None
        await self.panel.save_and_rerender(interaction)


class BirthdayPanel(AuthorView):
    """The ephemeral manager."""

    def __init__(self, cog, guild, author_id, config):
        super().__init__(author_id, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.config = config
        # The role currently being handed out (none while disabled): see save_and_rerender.
        self.live_role_id = config["role_id"] if config["enabled"] else None

        toggle = discord.ui.Button(
            label=_("Disable") if config["enabled"] else _("Enable"),
            style=discord.ButtonStyle.danger if config["enabled"] else discord.ButtonStyle.success,
            row=3,
        )
        toggle.callback = self._toggle
        zone = discord.ui.Button(label=_("Timezone"), style=discord.ButtonStyle.secondary, row=3)
        zone.callback = self._timezone
        custom = discord.ui.Button(label=_("Customize"), style=discord.ButtonStyle.primary, row=3)
        custom.callback = self._customize
        for item in (
            _ChannelSelect(self, guild.get_channel(config["channel_id"] or 0)),
            _RoleSelect(self, guild.get_role(config["role_id"] or 0)),
            _HourSelect(self, config["hour"]),
            toggle,
            zone,
            custom,
        ):
            self.add_item(item)

    @property
    def custom_allowed(self):
        return premium.resolve_guild_limits(self.cog.bot, self.guild.id).birthdays_custom

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
        embed = discord.Embed(
            title=_("Birthdays"),
            description=_(
                "Members share their birthday with /birthday set and I celebrate it here."
            ),
            colour=random_colour(),
        )
        embed.add_field(name=_("Status"), value=_("Enabled") if config["enabled"] else _("Disabled"))
        embed.add_field(
            name=_("Channel"),
            value=f"<#{config['channel_id']}>" if config["channel_id"] else _("*Not set.*"),
        )
        embed.add_field(
            name=_("Role"),
            value=f"<@&{config['role_id']}>" if config["role_id"] else _("*None.*"),
        )
        embed.add_field(
            name=_("Time"), value="{h:02d}:00 {zone}".format(h=config["hour"], zone=config["timezone"])
        )
        template = config["message"] if self.custom_allowed else None
        embed.add_field(name=_("Message"), value=(template or _("Default message"))[:1000], inline=False)
        if not self.custom_allowed:
            embed.set_footer(text=_("Yasuho+ unlocks your own message."))
        return embed

    async def save_and_rerender(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()  # the writes below must not eat the 3 s
        await settings.set_guild(
            self.cog.bot.db_pool, self.guild.id, store.SETTINGS_KEY, dict(self.config)
        )
        await self.cog.reconcile(self.guild.id)
        now_role = self.config["role_id"] if self.config["enabled"] else None
        if self.live_role_id and self.live_role_id != now_role:
            await self.cog.strip_role(self.guild, self.live_role_id, self.config)
        new = BirthdayPanel(self.cog, self.guild, self.author_id, self.config)
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def pick_channel(self, interaction, channel_id):
        channel = self.guild.get_channel(channel_id)
        perms = channel.permissions_for(self.guild.me) if channel and self.guild.me else None
        if perms is None or not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(
                _(
                    "I can't send messages in that channel. Give me View Channel "
                    "and Send Messages there first."
                ),
                ephemeral=True,
            )
        self.config["channel_id"] = channel.id
        await self.save_and_rerender(interaction)

    async def pick_role(self, interaction, role):
        if role is not None:
            if store.role_problem(self.guild, role) or role_audit.refuse_dangerous_role(
                role, surface="birthday", guild_id=self.guild.id
            ):
                return await interaction.response.send_message(
                    _(
                        "I can't give that role. It must be below my highest role, "
                        "not managed by an integration, without powerful permissions, "
                        "and I need Manage Roles."
                    ),
                    ephemeral=True,
                )
        self.config["role_id"] = role.id if role is not None else None
        await self.save_and_rerender(interaction)

    async def _toggle(self, interaction):
        if not self.config["enabled"] and not self.config["channel_id"]:
            return await interaction.response.send_message(
                _("Pick a birthday channel first."), ephemeral=True
            )
        self.config["enabled"] = not self.config["enabled"]
        await self.save_and_rerender(interaction)

    async def _timezone(self, interaction):
        await interaction.response.send_modal(TimezoneModal(self))

    async def _customize(self, interaction):
        if self.custom_allowed:
            return await interaction.response.send_modal(MessageModal(self))
        await self.refuse_custom(interaction)

    async def refuse_custom(self, interaction):
        upsell = await premium_upsell.for_guild_refusal(
            self.cog.bot,
            limit_key="birthdays_custom",
            guild_id=self.guild.id,
            person_id=interaction.user.id,
            is_admin=premium_upsell.invoker_is_admin(interaction.user),
            already_top_tier=premium_upsell.is_guild_already_top_tier(
                self.cog.bot, self.guild.id
            ),
            kind="unlock",
        )
        text = _("A custom birthday message is a Yasuho+ feature.")
        kwargs = {"ephemeral": True}
        if upsell is not None:
            text += "\n" + upsell.line
            view = upsell.view()
            if view is not None:
                kwargs["view"] = view
        await interaction.response.send_message(text, **kwargs)
