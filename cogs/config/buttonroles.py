"""Self-assignable role buttons - a modern take on reaction roles.

The admin builds a panel through an interactive, author-restricted builder
(open with the ``buttonrole`` command). The panel embed is composed with the
shared ``tools.embed_creator`` toolkit (title/description/colour/author/footer/
thumbnail/image/fields), and every role button is customisable (label, emoji and
ButtonStyle). The finished panel can be posted to a channel or attached to an
existing message the bot itself authored.

Persistence: one row per (message, role) button lives in the ``button_roles``
table with a stable custom_id ('br:<role_id>'). On startup the cog re-registers
every stored panel as a persistent view (timeout=None) so the buttons keep
working across restarts.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the fancy
ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import logging
import re

import discord
from discord.ext import commands

from tools import embed_creator, i18n
from tools.formats import random_colour
from tools.i18n import _
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Self-assignable role buttons are public by design: anyone may click them to
# toggle a role. The author-restriction / on_timeout conventions therefore apply
# only to the admin-facing builder below (BuilderView), not to the persistent
# ButtonRoleView.

# Discord allows at most 25 components in a message view; cap the buttons there.
MAX_BUTTONS = 25

# ButtonStyle ints we accept for a callable role button. Link (5) and premium (6)
# styles cannot carry a custom_id / callback, so they are excluded.
_STYLE_BY_NAME = {
    "primary": 1,
    "blurple": 1,
    "blue": 1,
    "secondary": 2,
    "grey": 2,
    "gray": 2,
    "success": 3,
    "green": 3,
    "danger": 4,
    "red": 4,
}
_STYLE_LABEL = {1: "Primary", 2: "Secondary", 3: "Success", 4: "Danger"}
# A coloured dot per style for the builder's button summary.
_STYLE_DOT = {
    1: "\U0001F535",  # blue circle
    2: "\U000026AB",  # black circle
    3: "\U0001F7E2",  # green circle
    4: "\U0001F534",  # red circle
}

# https://discord.com/channels/<guild>/<channel>/<message>
_LINK_RE = re.compile(
    r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def _parse_style(text):
    """Parse a style name to a ButtonStyle int (defaults to secondary = 2)."""

    if not text:
        return 2
    return _STYLE_BY_NAME.get(text.strip().lower(), 2)


def _coerce_style(value):
    """Map a stored int to a safe, callable ButtonStyle (secondary fallback)."""

    try:
        style = discord.ButtonStyle(int(value))
    except (ValueError, TypeError):
        return discord.ButtonStyle.secondary
    if style.value not in (1, 2, 3, 4):
        return discord.ButtonStyle.secondary
    return style


def _parse_message_ref(text, default_channel_id):
    """Resolve a message ID or jump link to (guild_id, channel_id, message_id).

    A jump link yields all three. A bare numeric ID yields (None, the supplied
    default channel id, the message id). Returns None when nothing parses.
    """

    if not text:
        return None
    text = text.strip()
    match = _LINK_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    if text.isdigit():
        return None, default_channel_id, int(text)
    return None


# ----------------------------------------------------------------------
# Persistent (public) view: one button per self-assignable role
# ----------------------------------------------------------------------
class ButtonRoleButton(discord.ui.Button):
    """A single self-assignable role button with a stable, persistent custom_id."""

    def __init__(self, role_id, label, emoji=None, style=discord.ButtonStyle.secondary):
        self.role_id = role_id
        super().__init__(
            label=(label or "Role")[:80],
            emoji=(emoji or None),
            style=style,
            custom_id=f"br:{role_id}",
        )

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                _("Roles can only be toggled inside a server."), ephemeral=True
            )
            return

        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                _("That role no longer exists."), ephemeral=True
            )
            return

        none = discord.AllowedMentions.none()
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Button role")
                await interaction.response.send_message(
                    _("Removed {role} from you.").format(role=role.mention),
                    ephemeral=True,
                    allowed_mentions=none,
                )
            else:
                await member.add_roles(role, reason="Button role")
                await interaction.response.send_message(
                    _("Gave you {role}.").format(role=role.mention),
                    ephemeral=True,
                    allowed_mentions=none,
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                _(
                    "I don't have permission to manage that role. It may be above "
                    "my highest role."
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            log.exception("Failed to toggle button role %s", self.role_id)
            await interaction.response.send_message(
                _("Something went wrong toggling that role."), ephemeral=True
            )


class ButtonRoleView(discord.ui.View):
    """Persistent (timeout=None) view holding one button per self-assignable role."""

    def __init__(self, rows):
        super().__init__(timeout=None)
        # rows: iterable of (role_id, label, emoji, style_int)
        for role_id, label, emoji, style in rows:
            try:
                self.add_item(
                    ButtonRoleButton(role_id, label, emoji, _coerce_style(style))
                )
            except Exception:
                # A single bad stored emoji must not break the whole panel.
                log.exception("Failed to build role button for %s", role_id)
                try:
                    self.add_item(
                        ButtonRoleButton(role_id, label, None, _coerce_style(style))
                    )
                except Exception:
                    log.exception("Dropping unbuildable role button for %s", role_id)


# ----------------------------------------------------------------------
# Builder components (admin-facing, author-restricted)
# ----------------------------------------------------------------------
class _AddRoleSelect(discord.ui.RoleSelect):
    """Pick a role to turn into a new button (opens the customise modal)."""

    def __init__(self):
        super().__init__(
            placeholder=_("Add a role as a button..."),
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction):
        await self.view.on_add_role(interaction, self.values[0])


class _TargetChannelSelect(discord.ui.ChannelSelect):
    """Pick the channel a freshly posted panel should land in."""

    def __init__(self, builder):
        channel = builder.guild.get_channel(builder.target_channel_id)
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Channel to post the panel in..."),
            min_values=1,
            max_values=1,
            default_values=[channel] if channel is not None else [],
            row=2,
        )

    async def callback(self, interaction):
        await self.view.on_target_selected(interaction, self.values[0])


class _RemoveButtonSelect(discord.ui.Select):
    """List the configured buttons so the admin can remove one."""

    def __init__(self, builder):
        options = []
        for index, button in enumerate(builder.config["buttons"][:25]):
            role = builder.guild.get_role(button["role_id"])
            name = button.get("label") or (role.name if role else str(button["role_id"]))
            options.append(
                discord.SelectOption(
                    label=name[:100] or _("Role"),
                    value=str(index),
                    description=(role.name[:100] if role else None),
                )
            )
        super().__init__(
            placeholder=_("Remove a role button..."),
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction):
        try:
            index = int(self.values[0])
            buttons = self.view.config["buttons"]
            if 0 <= index < len(buttons):
                buttons.pop(index)
            await self.view._rerender(interaction)
        except Exception:
            log.exception("Button-role remove select failed")
            await self.view._error(interaction)


class AddButtonModal(LocaleModal):
    """Customise a new role button: label, emoji and ButtonStyle."""

    def __init__(self, builder, role):
        super().__init__(title=_("Add role button"))
        self.builder = builder
        self.role = role
        self.label_field = discord.ui.TextInput(
            label=_("Button label"),
            required=True,
            max_length=80,
            default=role.name[:80],
        )
        self.emoji_field = discord.ui.TextInput(
            label=_("Emoji (optional)"),
            required=False,
            max_length=64,
            placeholder=_("Paste an emoji, or leave blank"),
        )
        self.style_field = discord.ui.TextInput(
            label=_("Style"),
            required=False,
            max_length=16,
            default="secondary",
            placeholder="primary / secondary / success / danger",
        )
        self.add_item(self.label_field)
        self.add_item(self.emoji_field)
        self.add_item(self.style_field)

    async def on_submit(self, interaction):
        try:
            buttons = self.builder.config["buttons"]
            if len(buttons) >= MAX_BUTTONS:
                await interaction.response.send_message(
                    _("A panel can have at most {max} buttons.").format(
                        max=MAX_BUTTONS
                    ),
                    ephemeral=True,
                )
                return
            if any(b["role_id"] == self.role.id for b in buttons):
                await interaction.response.send_message(
                    _("{role} already has a button on this panel.").format(
                        role=self.role.mention
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            buttons.append(
                {
                    "role_id": self.role.id,
                    "label": (self.label_field.value.strip() or self.role.name)[:80],
                    "emoji": self.emoji_field.value.strip(),
                    "style": _parse_style(self.style_field.value),
                }
            )
            await self.builder._rerender(interaction)
        except Exception:
            log.exception("Button-role add modal failed")
            await self.builder._error(interaction)


class AttachModal(LocaleModal):
    """Collect a message ID or jump link to attach the role buttons to."""

    def __init__(self, builder):
        super().__init__(title=_("Attach to a message"))
        self.builder = builder
        self.ref_field = discord.ui.TextInput(
            label=_("Message ID or link"),
            required=True,
            max_length=200,
            placeholder="123456789012345678 or https://discord.com/channels/...",
        )
        self.add_item(self.ref_field)

    async def on_submit(self, interaction):
        try:
            await self.builder._do_attach(interaction, self.ref_field.value)
        except Exception:
            log.exception("Button-role attach modal failed")
            await self.builder._error(interaction)


# ----------------------------------------------------------------------
# Builder view (the single entry point for admins)
# ----------------------------------------------------------------------
class BuilderView(AuthorView):
    """Author-restricted builder that designs, posts or attaches a panel.

    Satisfies the embed_creator.EmbedEditorHost protocol (embed_config +
    on_embed_changed) so the shared edit dropdown drops straight in. The config
    blob is {"embed": <embed_creator sub-blob>, "buttons": [...]} and is reused
    by reference across rebuilds so the embed editor always mutates a stable ref.
    """

    # No interpolation tokens for button-role panels.
    placeholder_hint = ""

    def __init__(self, cog, guild, author_id, target_channel_id, config, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.target_channel_id = target_channel_id
        self.config = config

        self.add_item(embed_creator.make_edit_select(self, row=0))
        self.add_item(_AddRoleSelect())
        self.add_item(_TargetChannelSelect(self))
        if self.config["buttons"]:
            self.add_item(_RemoveButtonSelect(self))

    # ---- EmbedEditorHost contract ----
    @property
    def embed_config(self):
        return self.config["embed"]

    async def on_embed_changed(self, interaction):
        await self._rerender(interaction)

    # ---- role helpers ----
    @staticmethod
    def _can_assign(role):
        guild = role.guild
        me = guild.me
        return (
            not role.is_default()
            and not role.managed
            and me is not None
            and role < me.top_role
        )

    def _role_assignable(self, role_id):
        role = self.guild.get_role(role_id)
        return role is not None and self._can_assign(role)

    def _assignable_buttons(self):
        return [b for b in self.config["buttons"] if self._role_assignable(b["role_id"])]

    @staticmethod
    def _rows(buttons):
        return [
            (b["role_id"], b["label"], b.get("emoji") or None, b["style"])
            for b in buttons
        ]

    # ---- status embed ----
    def build_embed(self):
        embed_cfg = self.config.get("embed") or {}
        colour = embed_cfg.get("color")
        embed = discord.Embed(
            title=_("Button role builder"),
            description=_(
                "Design the panel below. Edit the embed, add role buttons, then "
                "**Post panel** to a channel or **Attach to message** to drop the "
                "buttons onto a message I already sent. Every change is kept until "
                "you post."
            ),
            colour=colour if isinstance(colour, int) else random_colour(),
        )

        buttons = self.config.get("buttons") or []
        if buttons:
            lines = []
            for button in buttons[:25]:
                dot = _STYLE_DOT.get(button.get("style"), _STYLE_DOT[2])
                emoji = (button.get("emoji") or "").strip()
                label = button.get("label") or "Role"
                prefix = f"{dot} {emoji}".strip()
                lines.append(f"{prefix} {label} -> <@&{button['role_id']}>")
            value = "\n".join(lines)
        else:
            value = _("*No buttons yet. Pick a role below to add one.*")
        embed.add_field(
            name=_("Buttons ({count})").format(count=len(buttons)),
            value=value[:1024],
            inline=False,
        )

        embed.add_field(
            name=_("Embed"),
            value=embed_creator.summarise(embed_cfg),
            inline=False,
        )
        embed.add_field(
            name=_("Post target"),
            value=(
                f"<#{self.target_channel_id}>"
                if self.target_channel_id
                else _("*Not set.*")
            ),
            inline=False,
        )
        embed.set_footer(text=_("Only you can use these controls."))
        return embed

    def _render_panel_embed(self, buttons):
        """The single public-facing embed (embed_creator render + a fallback)."""

        embed = embed_creator.render(self.config["embed"])
        if not embed_creator.embed_has_content(embed):
            embed.title = _("Self-assignable roles")
            embed.description = (
                _(
                    "Click a button below to give yourself a role, or click it "
                    "again to remove it:"
                )
                + "\n"
                + "\n".join(f"- <@&{b['role_id']}>" for b in buttons)
            )
            if embed.colour is None:
                embed.colour = discord.Colour(random_colour())
        return embed

    # ---- component callbacks ----
    async def on_add_role(self, interaction, role):
        try:
            if not self._can_assign(role):
                await interaction.response.send_message(
                    _(
                        "I can't assign that role - it's either managed by an "
                        "integration or above my highest role."
                    ),
                    ephemeral=True,
                )
                return
            if any(b["role_id"] == role.id for b in self.config["buttons"]):
                await interaction.response.send_message(
                    _("{role} already has a button on this panel.").format(
                        role=role.mention
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.send_modal(AddButtonModal(self, role))
        except Exception:
            log.exception("Button-role add-role select failed")
            await self._error(interaction)

    async def on_target_selected(self, interaction, channel):
        try:
            self.target_channel_id = channel.id
            await self._rerender(interaction)
        except Exception:
            log.exception("Button-role target select failed")
            await self._error(interaction)

    # ---- action buttons ----
    @discord.ui.button(label="Post panel", style=discord.ButtonStyle.success, row=4)
    async def post_button(self, interaction, button):
        try:
            await self._do_post(interaction)
        except Exception:
            log.exception("Button-role post failed")
            await self._error(interaction)

    @discord.ui.button(label="Attach to message", style=discord.ButtonStyle.primary, row=4)
    async def attach_button(self, interaction, button):
        try:
            if not self._assignable_buttons():
                await interaction.response.send_message(
                    _("Add at least one assignable role button before attaching."),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(AttachModal(self))
        except Exception:
            log.exception("Button-role attach launch failed")
            await self._error(interaction)

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary, row=4)
    async def preview_button(self, interaction, button):
        try:
            buttons = self._assignable_buttons() or (self.config["buttons"] or [])
            embed = self._render_panel_embed(buttons)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Button-role preview failed")
            await self._error(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
    async def cancel_button(self, interaction, button):
        try:
            embed = discord.Embed(
                title=_("Cancelled"),
                description=_("Panel building was cancelled."),
                colour=random_colour(),
            )
            await self._finish(interaction, embed)
        except Exception:
            log.exception("Button-role cancel failed")
            await self._error(interaction)

    # ---- post / attach ----
    async def _do_post(self, interaction):
        buttons = self.config["buttons"]
        assignable = self._assignable_buttons()
        if not assignable:
            if not buttons:
                await interaction.response.send_message(
                    _("Add at least one role button before posting."),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    _(
                        "None of those roles can be assigned by me - they're "
                        "either managed by an integration or above my highest "
                        "role."
                    ),
                    ephemeral=True,
                )
            return

        channel = self.guild.get_channel(self.target_channel_id)
        if channel is None:
            await interaction.response.send_message(
                _("Pick a valid text channel to post the panel in."),
                ephemeral=True,
            )
            return

        rows = self._rows(assignable)
        embed = self._render_panel_embed(assignable)
        try:
            panel = await channel.send(embed=embed, view=ButtonRoleView(rows))
        except discord.Forbidden:
            await interaction.response.send_message(
                _("I can't send messages in {channel}.").format(
                    channel=channel.mention
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Failed to post button-role panel")
            await interaction.response.send_message(
                _("Something went wrong posting the panel."), ephemeral=True
            )
            return

        await self._persist(panel.id, channel.id, assignable, rows)

        skipped = len(buttons) - len(assignable)
        done = discord.Embed(
            title=_("Panel posted"),
            description=_("Your button-role panel is live in {channel}.").format(
                channel=channel.mention
            ),
            colour=random_colour(),
        )
        if skipped:
            done.add_field(
                name=_("Skipped"),
                value=_("{count} role(s) I can't assign were left off.").format(
                    count=skipped
                ),
                inline=False,
            )
        await self._finish(interaction, done)

    async def _do_attach(self, interaction, raw):
        assignable = self._assignable_buttons()
        if not assignable:
            await interaction.response.send_message(
                _("Add at least one assignable role button before attaching."),
                ephemeral=True,
            )
            return

        parsed = _parse_message_ref(raw, self.target_channel_id)
        if parsed is None:
            await interaction.response.send_message(
                _("That doesn't look like a message ID or a Discord message link."),
                ephemeral=True,
            )
            return
        guild_id, channel_id, message_id = parsed
        if guild_id is not None and guild_id != self.guild.id:
            await interaction.response.send_message(
                _("That message link points to a different server."),
                ephemeral=True,
            )
            return

        channel = self.guild.get_channel_or_thread(channel_id)
        if channel is None:
            await interaction.response.send_message(
                _("I can't find that channel in this server."), ephemeral=True
            )
            return

        try:
            target = await channel.fetch_message(message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                _("I couldn't find a message with that ID in that channel."),
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                _("I can't read messages in {channel}.").format(
                    channel=channel.mention
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Failed to fetch attach target message")
            await interaction.response.send_message(
                _("Something went wrong fetching that message."), ephemeral=True
            )
            return

        # Discord only lets a bot edit components onto a message IT authored.
        if target.author.id != self.cog.bot.user.id:
            await interaction.response.send_message(
                _(
                    "I can only add role buttons to a message I posted myself - "
                    "Discord won't let a bot edit buttons onto someone else's "
                    "message. Use **Post panel** to publish a fresh panel instead "
                    "(I'll happily recreate the same embed and buttons there)."
                ),
                ephemeral=True,
            )
            return

        rows = self._rows(assignable)
        # Apply the builder's embed only when the admin actually customised one;
        # otherwise keep the existing message content and just add the buttons.
        built = embed_creator.render(self.config["embed"])
        edit_kwargs = {"view": ButtonRoleView(rows)}
        if embed_creator.embed_has_content(built):
            edit_kwargs["embed"] = built
        try:
            await target.edit(**edit_kwargs)
        except discord.Forbidden:
            await interaction.response.send_message(
                _("I'm not allowed to edit that message."), ephemeral=True
            )
            return
        except discord.HTTPException:
            log.exception("Failed to attach button-role view to message %s", message_id)
            await interaction.response.send_message(
                _("Something went wrong attaching the buttons."), ephemeral=True
            )
            return

        await self._persist(target.id, channel.id, assignable, rows)

        done = discord.Embed(
            title=_("Buttons attached"),
            description=_(
                "Added the role button(s) to [that message]({link}) in {channel}."
            ).format(link=target.jump_url, channel=channel.mention),
            colour=random_colour(),
        )
        await self._finish(interaction, done)

    # ---- persistence ----
    async def _persist(self, message_id, channel_id, buttons, rows):
        # Authoritative for this message: replace its whole stored set so a role
        # dropped from the panel does not leave a stale row behind.
        records = [
            (
                message_id,
                self.guild.id,
                channel_id,
                b["role_id"],
                b["label"][:80],
                (b.get("emoji") or None),
                int(b["style"]),
            )
            for b in buttons
        ]
        try:
            async with self.cog.bot.db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM button_roles WHERE message_id = $1;",
                        message_id,
                    )
                    await conn.executemany(
                        """
                        INSERT INTO button_roles
                        (message_id, guild_id, channel_id, role_id, label, emoji, style)
                        VALUES ($1, $2, $3, $4, $5, $6, $7);
                        """,
                        records,
                    )
        except Exception:
            log.exception("Failed to persist button roles for message %s", message_id)

        # Register the persistent view so the buttons survive a restart.
        try:
            self.cog.bot.add_view(ButtonRoleView(rows), message_id=message_id)
        except Exception:
            log.exception(
                "Failed to register button-role view for message %s", message_id
            )

    # ---- view plumbing ----
    async def _rerender(self, interaction):
        new = BuilderView(
            self.cog,
            self.guild,
            self.author_id,
            self.target_channel_id,
            self.config,
        )
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def _finish(self, interaction, embed):
        for child in self.children:
            child.disabled = True
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=embed, view=self
        )

    async def _error(self, interaction):
        await embed_creator.notify_failure(interaction)


def _default_panel_config():
    """A fresh builder config: a seeded embed sub-blob plus an empty button list."""

    embed = embed_creator.default_embed()
    embed["title"] = "Self-assignable roles"
    embed["description"] = (
        "Click a button below to give yourself a role, or click it again to "
        "remove it."
    )
    embed["color"] = random_colour()
    return {"embed": embed, "buttons": []}


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class ButtonRoles(commands.Cog):
    """Self-assignable roles via buttons - a modern take on reaction roles."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register every stored panel as a persistent view so the buttons
        # survive bot restarts.
        query = """
            SELECT message_id, role_id, label, emoji, style
            FROM button_roles
            ORDER BY message_id;
            """
        rows = await self.bot.db_pool.fetch(query)

        grouped = {}
        for row in rows:
            grouped.setdefault(row["message_id"], []).append(
                (row["role_id"], row["label"], row["emoji"], row["style"])
            )

        for mid, items in grouped.items():
            try:
                self.bot.add_view(ButtonRoleView(items), message_id=mid)
            except Exception:
                log.exception(
                    "Failed to register button-role view for message %s", mid
                )

    async def _open_builder(self, ctx):
        config = _default_panel_config()
        view = BuilderView(self, ctx.guild, ctx.author.id, ctx.channel.id, config)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_group(aliases=["br"])
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def buttonrole(self, ctx):
        """Open the interactive button-role builder."""

        if ctx.invoked_subcommand is None:
            await self._open_builder(ctx)

    @buttonrole.command(name="create")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def buttonrole_create(self, ctx):
        """Open the interactive button-role builder."""

        await self._open_builder(ctx)

    @buttonrole.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def buttonrole_list(self, ctx):
        """List every button-role panel set up in this guild."""

        query = """
            SELECT message_id, channel_id, role_id
            FROM button_roles
            WHERE guild_id = $1
            ORDER BY message_id;
            """
        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title=_("Button roles"),
                description=_(
                    "No button-role panels have been set up for this guild."
                ),
                colour=random_colour(),
            )
            await ctx.send(embed=embed)
            return

        grouped = {}
        for row in rows:
            grouped.setdefault(
                (row["message_id"], row["channel_id"]), []
            ).append(row["role_id"])

        lines = []
        for (mid, cid), role_ids in grouped.items():
            roles = " ".join(f"<@&{rid}>" for rid in role_ids)
            link = f"https://discord.com/channels/{ctx.guild.id}/{cid}/{mid}"
            lines.append(f"[`{mid}`]({link}) - {roles}")

        await Paginator(
            paginate_lines(lines, title=_("Button roles")), author_id=ctx.author.id
        ).start(ctx)

    @buttonrole.command(name="delete")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def buttonrole_delete(self, ctx, message_id: str):
        """Delete a button-role panel by its message ID (strips the buttons)."""

        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send(_("That doesn't look like a valid message ID."))
            return

        query = """
            DELETE FROM button_roles
            WHERE message_id = $1 AND guild_id = $2
            RETURNING channel_id;
            """
        rows = await self.bot.db_pool.fetch(query, mid, ctx.guild.id)

        if not rows:
            await ctx.send(_("No button-role panel found with that message ID."))
            return

        # Best-effort: strip the buttons off the message rather than delete it,
        # so a panel attached to an existing announcement keeps its content.
        channel = ctx.guild.get_channel_or_thread(rows[0]["channel_id"])
        if channel is not None:
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

        embed = discord.Embed(
            title=_("Button-role panel deleted"),
            description=_(
                "Removed `{count}` role button(s) for message `{mid}`."
            ).format(count=len(rows), mid=mid),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ButtonRoles(bot))
