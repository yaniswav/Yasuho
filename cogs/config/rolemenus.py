"""Enriched self-assignable role menus (Components V2 select, with rules).

Complements the button-role and reaction-role cogs with a select-menu picker:
one dropdown where members set their roles in a single interaction, with an
"exclusive" rule (pick exactly one, e.g. colours) or "any" (pick several). Menus
are backed by the ``role_menus`` table and re-registered as persistent views on
startup, so they keep working across restarts. Role changes are hierarchy-safe:
Yasuho never touches a role above her own or a managed role.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import json
import logging

import discord
from discord.ext import commands

from tools import i18n, role_menus
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

MAX_MENUS_PER_GUILD = 25


# ----------------------------------------------------------------------
# Persistent public menu
# ----------------------------------------------------------------------
class RoleMenuSelect(discord.ui.Select):
    """The public self-role dropdown; custom_id is unique per menu message."""

    def __init__(self, message_id, config):
        self.config = config
        options = []
        for opt in config.get("options", [])[:role_menus.MAX_OPTIONS]:
            options.append(
                discord.SelectOption(
                    label=str(opt.get("label") or opt["role_id"])[:100],
                    value=str(opt["role_id"]),
                    emoji=opt.get("emoji") or None,
                    description=(opt.get("description") or None),
                )
            )
        exclusive = bool(config.get("exclusive"))
        super().__init__(
            placeholder=config.get("placeholder") or "Pick your roles...",
            custom_id=f"rolemenu:{message_id}",
            min_values=0,
            max_values=1 if exclusive else max(1, len(options)),
            options=options or [discord.SelectOption(label="(no roles)", value="0")],
            row=0,
        )

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                _("Roles can only be set inside a server."), ephemeral=True
            )

        menu_ids = [o["role_id"] for o in self.config.get("options", [])]
        selected = [int(v) for v in self.values if v.isdigit()]
        held = [r.id for r in member.roles]
        to_add, to_remove = role_menus.resolve_selection(
            selected, held, menu_ids, exclusive=bool(self.config.get("exclusive"))
        )

        bot_top = guild.me.top_role
        added, removed, skipped = [], [], []
        for rid in to_add:
            role = guild.get_role(rid)
            if role is None or role >= bot_top or role.managed:
                skipped.append(rid)
                continue
            try:
                await member.add_roles(role, reason="Self-role menu")
                added.append(role)
            except discord.HTTPException:
                skipped.append(rid)
        for rid in to_remove:
            role = guild.get_role(rid)
            if role is None or role >= bot_top:
                continue
            try:
                await member.remove_roles(role, reason="Self-role menu")
                removed.append(role)
            except discord.HTTPException:
                pass

        none = discord.AllowedMentions.none()
        parts = []
        if added:
            parts.append(
                _("Added: {roles}").format(roles=", ".join(r.mention for r in added))
            )
        if removed:
            parts.append(
                _("Removed: {roles}").format(
                    roles=", ".join(r.mention for r in removed)
                )
            )
        if skipped and not added and not removed:
            parts.append(
                _("I couldn't manage those roles - they may be above my highest role.")
            )
        if not parts:
            parts.append(_("No changes."))
        await interaction.response.send_message(
            "\n".join(parts), ephemeral=True, allowed_mentions=none
        )


class RoleMenuView(discord.ui.View):
    """Persistent (timeout=None) view wrapping a single self-role dropdown."""

    def __init__(self, message_id, config):
        super().__init__(timeout=None)
        self.add_item(RoleMenuSelect(message_id, config))


# ----------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------
class HeaderModal(LocaleModal):
    """Edit the menu's header title + description."""

    def __init__(self, builder):
        super().__init__(title=_("Menu header"))
        self.builder = builder
        self.title_field = discord.ui.TextInput(
            label=_("Title"),
            default=builder.draft.get("title") or None,
            max_length=256,
            required=True,
        )
        self.desc_field = discord.ui.TextInput(
            label=_("Description"),
            style=discord.TextStyle.paragraph,
            default=builder.draft.get("description") or None,
            max_length=2000,
            required=False,
        )
        self.add_item(self.title_field)
        self.add_item(self.desc_field)

    async def on_submit(self, interaction):
        try:
            self.builder.draft["title"] = self.title_field.value.strip()
            self.builder.draft["description"] = self.desc_field.value.strip()
            await self.builder._rerender(interaction)
        except Exception:
            log.exception("Role menu header modal failed")
            await self.builder._error(interaction)


class _RolePicker(discord.ui.RoleSelect):
    def __init__(self, builder):
        self._owner = builder
        defaults = []
        for opt in builder.draft.get("options", []):
            role = builder.guild.get_role(opt["role_id"])
            if role is not None:
                defaults.append(role)
        super().__init__(
            placeholder=_("Pick the roles to offer..."),
            min_values=0,
            max_values=role_menus.MAX_OPTIONS,
            default_values=defaults[: role_menus.MAX_OPTIONS],
            row=0,
        )

    async def callback(self, interaction):
        try:
            self._owner.draft["options"] = [
                {
                    "role_id": r.id,
                    "label": r.name[: role_menus.MAX_LABEL],
                    "emoji": None,
                    "description": None,
                }
                for r in self.values
            ]
            await self._owner._rerender(interaction)
        except Exception:
            log.exception("Role menu role picker failed")
            await self._owner._error(interaction)


class _RuleSelect(discord.ui.Select):
    def __init__(self, builder):
        self._owner = builder
        exclusive = bool(builder.draft.get("exclusive"))
        options = [
            discord.SelectOption(
                label=_("Members can pick any"),
                value="any",
                default=not exclusive,
                emoji="\U00002714",
            ),
            discord.SelectOption(
                label=_("Members pick exactly one"),
                value="one",
                default=exclusive,
                emoji="\U0001F518",
            ),
        ]
        super().__init__(placeholder=_("Selection rule..."), options=options, row=1)

    async def callback(self, interaction):
        try:
            self._owner.draft["exclusive"] = self.values[0] == "one"
            await self._owner._rerender(interaction)
        except Exception:
            log.exception("Role menu rule select failed")
            await self._owner._error(interaction)


class _ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, builder):
        self._owner = builder
        defaults = []
        cid = builder.draft.get("channel_id")
        if cid:
            channel = builder.guild.get_channel(cid)
            if channel is not None:
                defaults = [channel]
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Channel to post the menu in..."),
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=2,
        )

    async def callback(self, interaction):
        try:
            self._owner.draft["channel_id"] = self.values[0].id
            await self._owner._rerender(interaction)
        except Exception:
            log.exception("Role menu channel picker failed")
            await self._owner._error(interaction)


class _HeaderButton(discord.ui.Button):
    def __init__(self, builder):
        self._owner = builder
        super().__init__(label=_("Edit header"), style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction):
        await interaction.response.send_modal(HeaderModal(self._owner))


class _PostButton(discord.ui.Button):
    def __init__(self, builder):
        self._owner = builder
        super().__init__(label=_("Post menu"), style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction):
        try:
            await self._owner.post(interaction)
        except Exception:
            log.exception("Role menu post failed")
            await self._owner._error(interaction)


class RoleMenuBuilder(AuthorView):
    """Author-restricted builder that composes and posts a self-role menu."""

    def __init__(self, cog, guild, author_id, draft, timeout=600):
        super().__init__(author_id, timeout=timeout, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.draft = draft
        self.add_item(_RolePicker(self))
        self.add_item(_RuleSelect(self))
        self.add_item(_ChannelPicker(self))
        self.add_item(_HeaderButton(self))
        self.add_item(_PostButton(self))

    def header_embed(self):
        """The embed that heads the posted menu (title + description + roles)."""
        colour = self.draft.get("colour")
        embed = discord.Embed(
            title=self.draft.get("title") or _("Pick your roles"),
            description=self.draft.get("description") or None,
            colour=colour if isinstance(colour, int) else random_colour(),
        )
        options = self.draft.get("options") or []
        if options:
            lines = [f"<@&{o['role_id']}>" for o in options]
            embed.add_field(name=_("Roles"), value=" ".join(lines)[:1024], inline=False)
        return embed

    def build_embed(self):
        embed = discord.Embed(
            title=_("Role menu builder"),
            description=_(
                "Pick the roles to offer, choose the rule, set a channel, then "
                "**Post menu**. Members set their roles from a single dropdown."
            ),
            colour=random_colour(),
        )
        options = self.draft.get("options") or []
        embed.add_field(
            name=_("Roles ({count})").format(count=len(options)),
            value=(" ".join(f"<@&{o['role_id']}>" for o in options)[:1024] or _("*None yet.*")),
            inline=False,
        )
        embed.add_field(
            name=_("Rule"),
            value=_("Pick exactly one") if self.draft.get("exclusive") else _("Pick any"),
            inline=True,
        )
        cid = self.draft.get("channel_id")
        embed.add_field(
            name=_("Channel"),
            value=f"<#{cid}>" if cid else _("*Not set.*"),
            inline=True,
        )
        embed.add_field(
            name=_("Header"),
            value=(self.draft.get("title") or _("*default*"))[:256],
            inline=False,
        )
        return embed

    async def _rerender(self, interaction):
        new = RoleMenuBuilder(self.cog, self.guild, self.author_id, self.draft)
        new.message = self.message
        self.stop()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=new.build_embed(), view=new)
                return
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.edit(embed=new.build_embed(), view=new)
            except discord.HTTPException:
                pass

    async def _error(self, interaction):
        try:
            await interaction.response.send_message(
                _("Something went wrong."), ephemeral=True
            )
        except discord.HTTPException:
            pass

    async def post(self, interaction):
        options = role_menus.normalize_options(self.draft.get("options"))
        if not options:
            return await interaction.response.send_message(
                _("Pick at least one role first."), ephemeral=True
            )
        cid = self.draft.get("channel_id")
        channel = self.guild.get_channel(cid) if cid else None
        if channel is None:
            return await interaction.response.send_message(
                _("Pick a channel to post the menu in first."), ephemeral=True
            )
        if not channel.permissions_for(self.guild.me).send_messages:
            return await interaction.response.send_message(
                _("I can't send messages in {channel}.").format(channel=channel.mention),
                ephemeral=True,
            )

        config = {
            "title": self.draft.get("title") or "",
            "description": self.draft.get("description") or "",
            "colour": self.draft.get("colour"),
            "exclusive": bool(self.draft.get("exclusive")),
            "options": options,
        }

        # Post first (no view) to learn the message id, then attach the view so
        # its select carries a message-unique, restart-stable custom_id.
        message = await channel.send(embed=self.header_embed())
        view = RoleMenuView(message.id, config)
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            await message.delete()
            return await interaction.response.send_message(
                _("Posting failed, please try again."), ephemeral=True
            )
        await self.cog.store_menu(message.id, self.guild.id, channel.id, config)
        self.cog.bot.add_view(view, message_id=message.id)

        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            _("Posted the role menu in {channel}.").format(channel=channel.mention),
            ephemeral=True,
        )


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class RoleMenus(commands.Cog):
    """Self-assignable role menus (select dropdowns with rules)."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register every stored menu as a persistent view so it survives a
        # restart, exactly like the button-role cog does for its panels.
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT message_id, config FROM role_menus"
            )
        except Exception:
            log.exception("Failed to load role menus")
            return
        for row in rows:
            config = row["config"]
            if isinstance(config, str):
                config = json.loads(config)
            try:
                self.bot.add_view(
                    RoleMenuView(row["message_id"], config),
                    message_id=row["message_id"],
                )
            except Exception:
                log.exception(
                    "Failed to register role menu for message %s", row["message_id"]
                )

    async def store_menu(self, message_id, guild_id, channel_id, config):
        await self.bot.db_pool.execute(
            "INSERT INTO role_menus (message_id, guild_id, channel_id, config) "
            "VALUES ($1, $2, $3, $4::jsonb) "
            "ON CONFLICT (message_id) DO UPDATE SET config = $4::jsonb",
            message_id,
            guild_id,
            channel_id,
            json.dumps(config),
        )

    async def _menu_count(self, guild_id):
        return (
            await self.bot.db_pool.fetchval(
                "SELECT COUNT(*) FROM role_menus WHERE guild_id = $1", guild_id
            )
            or 0
        )

    @commands.hybrid_command(name="rolemenu", aliases=["selfroles", "rolemenus"])
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def rolemenu(self, ctx):
        """Open the self-role menu builder."""
        if await self._menu_count(ctx.guild.id) >= MAX_MENUS_PER_GUILD:
            return await ctx.send(
                _("This server already has the maximum of {n} role menus.").format(
                    n=MAX_MENUS_PER_GUILD
                )
            )
        draft = {
            "title": _("Pick your roles"),
            "description": "",
            "colour": None,
            "exclusive": False,
            "options": [],
            "channel_id": ctx.channel.id,
        }
        view = RoleMenuBuilder(self, ctx.guild, ctx.author.id, draft)
        view.message = await ctx.send(embed=view.build_embed(), view=view)


async def setup(bot):
    await bot.add_cog(RoleMenus(bot))
