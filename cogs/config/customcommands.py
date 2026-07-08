"""Per-guild custom (canned) commands, invoked by the guild prefix.

Admins build commands through an author-restricted Components V2 panel
(`/customcommands`); each command answers with either plain text or a rich
embed (composed with the shared ``tools.embed_creator`` editor). Responses may
interpolate {user}/{server}/{members}. A lightweight on_message listener does
the dispatch: it never shadows a real command, and a per-guild in-memory cache
keeps the hot path off the database.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import json
import logging

import discord
from discord.ext import commands

from tools import custom_commands as cc
from tools import embed_creator
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

PLACEHOLDERS = [
    ("{user}", "A mention of whoever ran the command"),
    ("{server}", "The server name"),
    ("{members}", "The current member count"),
]
PLACEHOLDER_HINT = "{user} {server} {members}"

# error key -> user-facing message (keys come from tools.custom_commands).
_NAME_ERRORS = {
    "empty": lambda: _("The command name can't be empty."),
    "too_long": lambda: _("The name is too long (max {n} characters).").format(
        n=cc.MAX_NAME_LENGTH
    ),
    "bad_chars": lambda: _(
        "Use only lowercase letters, digits, - and _ (no spaces)."
    ),
    "reserved": lambda: _("That name is already one of my built-in commands."),
    "exists": lambda: _("This server already has a custom command with that name."),
}


def build_substitution(author, guild):
    """Return the token resolver for a dispatched response (pure-ish)."""
    members = f"{guild.member_count:,}" if guild and guild.member_count else "0"
    replacements = {
        "{user}": author.mention if author else "",
        "{server}": guild.name if guild else "",
        "{members}": members,
    }

    def substitute(text):
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    return substitute


# ----------------------------------------------------------------------
# Modals
# ----------------------------------------------------------------------
class AddTextModal(LocaleModal):
    """Create a text custom command (name + content)."""

    def __init__(self, panel):
        super().__init__(title=_("New text command"))
        self.panel = panel
        self.name_field = discord.ui.TextInput(
            label=_("Command name"),
            placeholder=_("e.g. rules (no prefix, no spaces)"),
            max_length=cc.MAX_NAME_LENGTH,
            required=True,
        )
        self.content_field = discord.ui.TextInput(
            label=_("Response text"),
            style=discord.TextStyle.paragraph,
            placeholder=PLACEHOLDER_HINT,
            max_length=cc.MAX_TEXT_LENGTH,
            required=True,
        )
        self.add_item(self.name_field)
        self.add_item(self.content_field)

    async def on_submit(self, interaction):
        try:
            name = cc.normalize_name(self.name_field.value)
            err = await self.panel.cog.validate_new_name(self.panel.guild.id, name)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            await self.panel.cog.save_command(
                self.panel.guild.id,
                name,
                {"type": "text", "content": self.content_field.value.strip()},
                interaction.user.id,
            )
            await self.panel.refresh(interaction, selected=name)
        except Exception:
            log.exception("Custom command text modal failed")
            await embed_creator.notify_failure(interaction)


class EditTextModal(LocaleModal):
    """Edit an existing text command's response (the name is kept)."""

    def __init__(self, panel, name, content):
        super().__init__(title=_("Edit: {name}").format(name=name)[:45])
        self.panel = panel
        self.name = name
        self.content_field = discord.ui.TextInput(
            label=_("Response text"),
            style=discord.TextStyle.paragraph,
            default=content or None,
            placeholder=PLACEHOLDER_HINT,
            max_length=cc.MAX_TEXT_LENGTH,
            required=True,
        )
        self.add_item(self.content_field)

    async def on_submit(self, interaction):
        try:
            await self.panel.cog.save_command(
                self.panel.guild.id,
                self.name,
                {"type": "text", "content": self.content_field.value.strip()},
                interaction.user.id,
            )
            await self.panel.refresh(interaction, selected=self.name)
        except Exception:
            log.exception("Custom command edit modal failed")
            await embed_creator.notify_failure(interaction)


class AddEmbedNameModal(LocaleModal):
    """Ask for the name, then open the embed editor sub-panel."""

    def __init__(self, panel):
        super().__init__(title=_("New embed command"))
        self.panel = panel
        self.name_field = discord.ui.TextInput(
            label=_("Command name"),
            placeholder=_("e.g. welcome (no prefix, no spaces)"),
            max_length=cc.MAX_NAME_LENGTH,
            required=True,
        )
        self.add_item(self.name_field)

    async def on_submit(self, interaction):
        try:
            name = cc.normalize_name(self.name_field.value)
            err = await self.panel.cog.validate_new_name(self.panel.guild.id, name)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            draft = {"name": name, "embed": embed_creator.default_embed()}
            view = CustomEmbedPanel(self.panel.cog, self.panel.guild, self.panel.author_id, draft)
            await interaction.response.send_message(
                embed=view.build_embed(), view=view, ephemeral=True
            )
            view.message = await interaction.original_response()
        except Exception:
            log.exception("Custom command embed name modal failed")
            await embed_creator.notify_failure(interaction)


# ----------------------------------------------------------------------
# Embed sub-panel (an embed_creator.EmbedEditorHost)
# ----------------------------------------------------------------------
class _SaveEmbedButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Save command"), style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction):
        try:
            await self._owner.save(interaction)
        except Exception:
            log.exception("Custom command embed save failed")
            await embed_creator.notify_failure(interaction)


class CustomEmbedPanel(AuthorView):
    """Compose the embed body of an embed custom command, then save it."""

    def __init__(self, cog, guild, author_id, draft, timeout=600):
        super().__init__(author_id, timeout=timeout, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.draft = draft
        self.placeholder_hint = PLACEHOLDER_HINT
        self.asset_hint = "https://..."
        self.add_item(
            embed_creator.make_edit_select(
                self, placeholder=_("Edit the response embed..."), row=0
            )
        )
        self.add_item(_SaveEmbedButton(self))
        self.add_item(
            embed_creator.PlaceholderGuideButton(PLACEHOLDERS, label=_("Placeholders"), row=1)
        )

    @property
    def embed_config(self):
        return self.draft["embed"]

    async def on_embed_changed(self, interaction):
        new = CustomEmbedPanel(self.cog, self.guild, self.author_id, self.draft)
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    def build_embed(self):
        embed = discord.Embed(
            title=_("Embed command: {name}").format(name=self.draft["name"]),
            description=_(
                "Design the embed this command will reply with, then **Save**. "
                "Placeholders: {placeholders}"
            ).format(placeholders=PLACEHOLDER_HINT),
            colour=random_colour(),
        )
        embed.add_field(
            name=_("Preview summary"),
            value=embed_creator.summarise(self.draft["embed"]),
            inline=False,
        )
        return embed

    async def save(self, interaction):
        embed = embed_creator.render(self.draft["embed"])
        if not embed_creator.embed_has_content(embed):
            return await interaction.response.send_message(
                _("Add some content to the embed first."), ephemeral=True
            )
        await self.cog.save_command(
            self.guild.id,
            self.draft["name"],
            {"type": "embed", "embed": self.draft["embed"]},
            interaction.user.id,
        )
        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            _("Saved the `{name}` command.").format(name=self.draft["name"]),
            ephemeral=True,
        )


# ----------------------------------------------------------------------
# Main management panel
# ----------------------------------------------------------------------
class _CommandSelect(discord.ui.Select):
    """Pick a command to view/delete."""

    def __init__(self, panel):
        self._owner = panel
        options = []
        for name in sorted(panel.commands)[:25]:
            response = panel.commands[name]
            kind = _("embed") if response.get("type") == "embed" else _("text")
            options.append(
                discord.SelectOption(
                    label=name,
                    value=name,
                    description=kind,
                    default=name == panel.selected,
                )
            )
        super().__init__(
            placeholder=_("Pick a command..."),
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label=_("(none yet)"), value="_none")],
            disabled=not options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            await self._owner.refresh(interaction, selected=self.values[0])
        except Exception:
            log.exception("Custom command select failed")
            await embed_creator.notify_failure(interaction)


class _AddTextButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Add text"), style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction):
        if await self._owner.cog.at_capacity(self._owner.guild.id):
            return await interaction.response.send_message(
                _("This server already has the maximum of {n} custom commands.").format(
                    n=cc.MAX_COMMANDS_PER_GUILD
                ),
                ephemeral=True,
            )
        await interaction.response.send_modal(AddTextModal(self._owner))


class _AddEmbedButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Add embed"), style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction):
        if await self._owner.cog.at_capacity(self._owner.guild.id):
            return await interaction.response.send_message(
                _("This server already has the maximum of {n} custom commands.").format(
                    n=cc.MAX_COMMANDS_PER_GUILD
                ),
                ephemeral=True,
            )
        await interaction.response.send_modal(AddEmbedNameModal(self._owner))


class _EditButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Edit selected"),
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=panel.selected is None,
        )

    async def callback(self, interaction):
        name = self._owner.selected
        if not name:
            return await interaction.response.send_message(
                _("Pick a command first."), ephemeral=True
            )
        response = (await self._owner.cog.get_commands(self._owner.guild.id)).get(name)
        if response is None:
            return await self._owner.refresh(interaction, selected=None)
        if response.get("type") == "embed":
            draft = {"name": name, "embed": embed_creator.merge_embed(response.get("embed"))}
            view = CustomEmbedPanel(
                self._owner.cog, self._owner.guild, self._owner.author_id, draft
            )
            await interaction.response.send_message(
                embed=view.build_embed(), view=view, ephemeral=True
            )
            view.message = await interaction.original_response()
        else:
            await interaction.response.send_modal(
                EditTextModal(self._owner, name, response.get("content") or "")
            )


class _DeleteButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Delete selected"),
            style=discord.ButtonStyle.danger,
            row=2,
            disabled=panel.selected is None,
        )

    async def callback(self, interaction):
        try:
            name = self._owner.selected
            if name:
                await self._owner.cog.delete_command(self._owner.guild.id, name)
            await self._owner.refresh(interaction, selected=None)
        except Exception:
            log.exception("Custom command delete failed")
            await embed_creator.notify_failure(interaction)


class CustomCommandsPanel(AuthorView):
    """Author-restricted list/add/delete panel for a guild's custom commands."""

    def __init__(self, cog, guild, author_id, commands_map, selected=None, timeout=300):
        super().__init__(author_id, timeout=timeout, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.commands = commands_map
        self.selected = selected if selected in commands_map else None
        self.add_item(_CommandSelect(self))
        self.add_item(_AddTextButton(self))
        self.add_item(_AddEmbedButton(self))
        self.add_item(_EditButton(self))
        self.add_item(_DeleteButton(self))

    def build_embed(self):
        embed = discord.Embed(
            title=_("Custom commands"),
            description=_(
                "Commands members run with the server prefix. Add a text or "
                "embed reply, or pick one to inspect and delete."
            ),
            colour=random_colour(),
        )
        if self.commands:
            lines = [
                _("`{name}` - {kind}").format(
                    name=name,
                    kind=_("embed") if resp.get("type") == "embed" else _("text"),
                )
                for name, resp in sorted(self.commands.items())[:20]
            ]
            embed.add_field(
                name=_("{count} command(s)").format(count=len(self.commands)),
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name=_("None yet"),
                value=_("Use **Add text** or **Add embed** to create one."),
                inline=False,
            )

        if self.selected:
            resp = self.commands.get(self.selected) or {}
            if resp.get("type") == "embed":
                detail = _("An embed reply.")
            else:
                content = resp.get("content") or ""
                detail = content[:300] or _("*empty*")
            embed.add_field(
                name=_("Selected: {name}").format(name=self.selected),
                value=detail,
                inline=False,
            )
        embed.set_footer(text=_("Placeholders: {placeholders}").format(placeholders=PLACEHOLDER_HINT))
        return embed

    async def refresh(self, interaction, *, selected):
        commands_map = await self.cog.get_commands(self.guild.id)
        new = CustomCommandsPanel(
            self.cog, self.guild, self.author_id, dict(commands_map), selected=selected
        )
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class CustomCommands(commands.Cog):
    """Per-guild canned commands invoked by the server prefix."""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {name: response_dict}; lazily loaded, invalidated on write.
        self._cache = {}

    async def get_commands(self, guild_id):
        """Return {name: response} for a guild (cached; loads on a miss)."""
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db_pool.fetch(
            "SELECT name, response FROM custom_commands WHERE guild_id = $1",
            guild_id,
        )
        data = {}
        for row in rows:
            response = row["response"]
            if isinstance(response, str):
                response = json.loads(response)
            data[row["name"]] = response
        self._cache[guild_id] = data
        return data

    async def at_capacity(self, guild_id):
        return len(await self.get_commands(guild_id)) >= cc.MAX_COMMANDS_PER_GUILD

    async def validate_new_name(self, guild_id, name):
        """Return a user-facing error string for a proposed name, or None."""
        reserved = set(self.bot.all_commands.keys())
        existing = set(await self.get_commands(guild_id))
        key = cc.validate_name(name, reserved=reserved, existing=existing)
        if key is None:
            return None
        return _NAME_ERRORS[key]()

    async def save_command(self, guild_id, name, response, created_by):
        await self.bot.db_pool.execute(
            "INSERT INTO custom_commands (guild_id, name, response, created_by) "
            "VALUES ($1, $2, $3::jsonb, $4) "
            "ON CONFLICT (guild_id, name) DO UPDATE SET response = $3::jsonb",
            guild_id,
            name,
            json.dumps(response),
            created_by,
        )
        self._cache.pop(guild_id, None)

    async def delete_command(self, guild_id, name):
        await self.bot.db_pool.execute(
            "DELETE FROM custom_commands WHERE guild_id = $1 AND name = $2",
            guild_id,
            name,
        )
        self._cache.pop(guild_id, None)

    async def _bump_uses(self, guild_id, name):
        try:
            await self.bot.db_pool.execute(
                "UPDATE custom_commands SET uses = uses + 1 "
                "WHERE guild_id = $1 AND name = $2",
                guild_id,
                name,
            )
        except Exception:
            log.exception("Custom command uses bump failed")

    @commands.hybrid_command(name="customcommands", aliases=["cc", "customcommand"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def customcommands(self, ctx):
        """Open the custom-commands builder."""
        commands_map = await self.get_commands(ctx.guild.id)
        view = CustomCommandsPanel(self, ctx.guild, ctx.author.id, dict(commands_map))
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    async def handle_unknown(self, ctx):
        """Dispatch a custom command for an unknown prefix command.

        Called by the Errors cog from its CommandNotFound branch (a single
        dispatch point, so a custom command and the "did you mean" suggestion
        can never both fire). Returns True if a custom command handled it. Real
        commands are matched by discord.py before this ever runs, so there is no
        way to shadow one.
        """
        if ctx.guild is None:
            return False
        name = (ctx.invoked_with or "").lower()
        if not name:
            return False
        response = (await self.get_commands(ctx.guild.id)).get(name)
        if response is None:
            return False
        await self._run(ctx.message, name, response)
        return True

    async def _run(self, message, name, response):
        substitute = build_substitution(message.author, message.guild)
        try:
            if response.get("type") == "embed":
                embed = embed_creator.render(
                    response.get("embed") or {}, substitute=substitute
                )
                if embed_creator.embed_has_content(embed):
                    await message.channel.send(embed=embed)
            else:
                content = substitute(response.get("content") or "")
                if content:
                    await message.channel.send(content[: cc.MAX_TEXT_LENGTH])
        except discord.HTTPException:
            log.exception("Custom command send failed")
            return
        await self._bump_uses(message.guild.id, name)


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
