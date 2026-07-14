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

import datetime
import json
import logging
import re

import discord
from discord.ext import commands

from tools import i18n, interactions, role_menus
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorLayoutView, LocaleModal

log = logging.getLogger(__name__)

MAX_MENUS_PER_GUILD = 25

_CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d+>$")

# Quick-pick durations for a temporary self-role: (extractable label, value fed
# to role_menus.parse_duration). The custom box overrides any pick. "Permanent"
# maps to 0 seconds. Labels are marked with N_ (module-level) and translated at
# modal-build time with _(...).
_DURATION_PRESETS = (
    (N_("1 hour"), "1h"),
    (N_("12 hours"), "12h"),
    (N_("1 day"), "1d"),
    (N_("7 days"), "7d"),
    (N_("Permanent"), "0"),
)


def valid_emoji(text):
    """Cheap check that a string is a usable select-option emoji.

    Accepts a custom-emoji token (<:name:id> / <a:name:id>) or a short string
    holding a real (high-codepoint) unicode emoji. Rejects plain text so a bad
    value can never make the posted menu fail to send. Not exhaustive, but it
    keeps the common "typed a word" mistake out.
    """
    text = (text or "").strip()
    if not text:
        return False
    if _CUSTOM_EMOJI.match(text):
        return True
    # A unicode emoji: short, carries a high codepoint, and holds NO ASCII
    # letter/digit - that last check kills the common "typed a word (+ emoji)"
    # mistake (e.g. "blue", "x") that Discord would 400 on send.
    if any(c.isascii() and c.isalnum() for c in text):
        return False
    return len(text) <= 8 and any(ord(c) > 0x2000 for c in text)


def _format_duration(seconds):
    """Render seconds back into a compact '1d'/'2h'/'30m' for a modal default."""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


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

        # Auto-remove any temporary role just added, via the shared timer system.
        # Guarded so a DB hiccup here never leaves the interaction unanswered.
        reminder = interaction.client.get_cog("Reminder")
        if reminder is not None and added:
            by_id = {o["role_id"]: o for o in self.config.get("options", [])}
            try:
                for role in added:
                    secs = int((by_id.get(role.id) or {}).get("temp_seconds") or 0)
                    if secs > 0:
                        when = discord.utils.utcnow() + datetime.timedelta(seconds=secs)
                        await reminder.create_timer(
                            when,
                            "temprole",
                            guild_id=guild.id,
                            user_id=member.id,
                            role_id=role.id,
                        )
            except Exception:
                log.exception("Failed to schedule temp-role removal")

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


class RoleOptionModal(LocaleModal):
    """Set the emoji + description shown for one role option."""

    def __init__(self, builder, role_id):
        super().__init__(title=_("Role option"))
        self.builder = builder
        self.role_id = role_id
        opt = next(
            (o for o in builder.draft.get("options", []) if o["role_id"] == role_id),
            None,
        )
        self.emoji_field = discord.ui.TextInput(
            label=_("Emoji (optional)"),
            required=False,
            max_length=64,
            default=(opt.get("emoji") if opt else None),
            placeholder=_("A single emoji, or leave blank"),
        )
        self.desc_field = discord.ui.TextInput(
            label=_("Short description (optional)"),
            required=False,
            max_length=role_menus.MAX_DESCRIPTION,
            default=(opt.get("description") if opt else None),
        )
        temp = (opt or {}).get("temp_seconds") or 0
        # Quick-pick radio for the common durations; preselect the option that
        # matches the current value when one does, else leave it empty and
        # prefill the custom box with the exact current duration.
        self.temp_radio = discord.ui.RadioGroup(required=False)
        matched = False
        for label, value in _DURATION_PRESETS:
            is_current = role_menus.parse_duration(value) == temp
            matched = matched or is_current
            self.temp_radio.add_option(label=_(label), value=value, default=is_current)
        self.temp_custom = discord.ui.TextInput(
            label=_("Custom duration"),
            placeholder=_("e.g. 2h, 30m, 1d"),
            required=False,
            max_length=10,
            default=(_format_duration(temp) if (temp and not matched) else None),
        )
        self.add_item(self.emoji_field)
        self.add_item(self.desc_field)
        self.add_item(
            discord.ui.Label(
                text=_("Temporary? (optional)"),
                component=self.temp_radio,
                description=_("Pick one, or type a custom value below to override."),
            )
        )
        self.add_item(self.temp_custom)

    async def on_submit(self, interaction):
        try:
            emoji = self.emoji_field.value.strip() or None
            if emoji is not None and not valid_emoji(emoji):
                return await interaction.response.send_message(
                    _("That emoji isn't valid. Use a single emoji, or leave it blank."),
                    ephemeral=True,
                )
            desc = self.desc_field.value.strip() or None
            # Custom box overrides the radio when filled (ColourModal-style
            # precedence); otherwise the picked preset; otherwise permanent.
            raw = (self.temp_custom.value or "").strip()
            if raw:
                temp_seconds = role_menus.parse_duration(raw)
            elif self.temp_radio.value is not None:
                temp_seconds = role_menus.parse_duration(self.temp_radio.value)
            else:
                temp_seconds = 0
            for opt in self.builder.draft.get("options", []):
                if opt["role_id"] == self.role_id:
                    opt["emoji"] = emoji
                    opt["description"] = desc
                    opt["temp_seconds"] = temp_seconds
                    break
            await self.builder._rerender(interaction)
        except Exception:
            log.exception("Role option modal failed")
            await self.builder._error(interaction)


class _CustomizeSelect(discord.ui.Select):
    """Pick one of the chosen roles to give it an emoji + description."""

    def __init__(self, builder):
        self._owner = builder
        options = []
        for opt in builder.draft.get("options", [])[: role_menus.MAX_OPTIONS]:
            sub = opt.get("description") or ""
            temp = opt.get("temp_seconds") or 0
            if temp:
                tag = _("temporary {dur}").format(dur=_format_duration(temp))
                sub = f"{sub} - {tag}" if sub else tag
            options.append(
                discord.SelectOption(
                    label=opt["label"][:100],
                    value=str(opt["role_id"]),
                    emoji=opt.get("emoji") if valid_emoji(opt.get("emoji") or "") else None,
                    description=(sub[:100] if sub else _("no emoji/description yet")),
                )
            )
        super().__init__(
            placeholder=_("Customize a role (emoji + description)..."),
            options=options or [discord.SelectOption(label=_("(pick roles first)"), value="_none")],
            disabled=not options,
        )

    async def callback(self, interaction):
        try:
            value = self.values[0]
            if value == "_none":
                return await interaction.response.defer()
            await interaction.response.send_modal(
                RoleOptionModal(self._owner, int(value))
            )
        except Exception:
            log.exception("Role menu customize select failed")
            await self._owner._error(interaction)


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
        super().__init__(placeholder=_("Selection rule..."), options=options)

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
        super().__init__(label=_("Edit header"), style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        await interaction.response.send_modal(HeaderModal(self._owner))


class _PostButton(discord.ui.Button):
    def __init__(self, builder):
        self._owner = builder
        super().__init__(label=_("Post menu"), style=discord.ButtonStyle.success)

    async def callback(self, interaction):
        try:
            await self._owner.post(interaction)
        except Exception:
            log.exception("Role menu post failed")
            await self._owner._error(interaction)


# ----------------------------------------------------------------------
# Edit a LayoutView panel in place with view=-only (no embed/content)
# ----------------------------------------------------------------------
async def _refresh_layout(interaction, message, view):
    """Edit a LayoutView panel in place with ``view=`` only (no embed/content).

    A Components V2 message carries its content inside the view and Discord
    rejects an ``embed=`` on such an edit. Tries the live interaction edit
    first, then falls back to editing the stored message when the interaction
    was already answered (e.g. a deferred modal submit).
    """

    await interactions.refresh_layout(
        interaction, message, view, surface="role-menus panel"
    )


class RoleMenuBuilder(AuthorLayoutView):
    """Author-restricted builder that composes and posts a self-role menu.

    A single Components V2 :class:`~discord.ui.Container` in the house style
    established by the settings/welcome/Twitch panels. The deny wording
    matches AuthorLayoutView's default ("This panel isn't for you.", the same
    wording the old AuthorView-based builder used explicitly), so it is left
    unset here. Note ``header_embed`` is unrelated to this panel: it builds the
    classic embed for the posted public menu message (see ``post``), which
    stays a plain embed since it is the product self-assigners see, not an
    admin control surface.
    """

    def __init__(self, cog, guild, author_id, draft, timeout=600):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.draft = draft
        self._build()

    def _build(self):
        """(Re)assemble the layout from the current draft."""

        draft = self.draft
        options = draft.get("options") or []
        container = discord.ui.Container(accent_colour=random_colour())

        header_lines = [
            "### " + _("Role menu builder"),
            _(
                "Pick the roles to offer, choose the rule, set a channel, then "
                "**Post menu**. Members set their roles from a single dropdown."
            ),
        ]
        container.add_item(discord.ui.TextDisplay("\n".join(header_lines)))
        container.add_item(discord.ui.Separator())

        roles_value = (
            " ".join(f"<@&{o['role_id']}>" for o in options)[:1024] or _("*None yet.*")
        )
        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Roles ({count})").format(count=len(options))
                + "**\n"
                + roles_value
            )
        )

        rule_value = (
            _("Pick exactly one") if draft.get("exclusive") else _("Pick any")
        )
        cid = draft.get("channel_id")
        channel_value = f"<#{cid}>" if cid else _("*Not set.*")
        container.add_item(
            discord.ui.TextDisplay(
                "**{rule_label}:** {rule_value}   "
                "**{channel_label}:** {channel_value}".format(
                    rule_label=_("Rule"),
                    rule_value=rule_value,
                    channel_label=_("Channel"),
                    channel_value=channel_value,
                )
            )
        )

        header_value = (draft.get("title") or _("*default*"))[:256]
        container.add_item(
            discord.ui.TextDisplay("**" + _("Header") + ":** " + header_value)
        )

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_RolePicker(self)))
        container.add_item(discord.ui.ActionRow(_RuleSelect(self)))
        container.add_item(discord.ui.ActionRow(_ChannelPicker(self)))
        container.add_item(discord.ui.ActionRow(_CustomizeSelect(self)))
        container.add_item(
            discord.ui.ActionRow(_HeaderButton(self), _PostButton(self))
        )
        self.add_item(container)

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

    async def _rerender(self, interaction):
        """Rebuild a fresh panel from current draft and show it in place."""

        new = RoleMenuBuilder(self.cog, self.guild, self.author_id, self.draft)
        new.message = self.message
        self.stop()
        await _refresh_layout(interaction, self.message, new)

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

        self._disable_all()
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
        # message ids of live menus, so on_raw_message_delete can prune the row
        # without a DB hit on every unrelated deletion.
        self._menu_ids = set()

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
                self._menu_ids.add(row["message_id"])
            except Exception:
                log.exception(
                    "Failed to register role menu for message %s", row["message_id"]
                )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        # Deleting the menu message is the natural way to remove a menu: drop its
        # row so it does not linger. Guarded by the in-memory set so this is a
        # no-op for every other deleted message.
        if payload.message_id not in self._menu_ids:
            return
        self._menu_ids.discard(payload.message_id)
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM role_menus WHERE message_id = $1", payload.message_id
            )
        except Exception:
            log.exception("Failed to delete role menu %s", payload.message_id)

    @commands.Cog.listener()
    async def on_temprole_timer_complete(self, extra):
        """Remove a temporary self-role when its timer fires (dispatched by the
        Reminder cog's generic timer handling)."""
        try:
            guild = self.bot.get_guild(extra.get("guild_id"))
            if guild is None:
                return
            member = guild.get_member(extra.get("user_id"))
            if member is None:
                # The member cache is empty after a restart (guilds are not
                # chunked at startup), so fetch the member for a timer that
                # outlived a restart. NotFound = they left, nothing to remove.
                try:
                    member = await guild.fetch_member(extra.get("user_id"))
                except discord.NotFound:
                    return
                except discord.HTTPException:
                    return
            role = guild.get_role(extra.get("role_id"))
            if role is None:
                return
            if role in member.roles:
                await member.remove_roles(role, reason="Temporary self-role expired")
        except discord.HTTPException:
            log.exception("Temp-role removal failed")

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
        self._menu_ids.add(message_id)

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
        view.message = await ctx.send(view=view)


async def setup(bot):
    await bot.add_cog(RoleMenus(bot))
