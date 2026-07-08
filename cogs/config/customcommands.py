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
import re
import time

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


def parse_cooldown(raw):
    """Parse a cooldown string into 0..3600 seconds (0 = no cooldown)."""
    try:
        return max(0, min(3600, int((raw or "0").strip() or 0)))
    except ValueError:
        return 0


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
        self.aliases_field = discord.ui.TextInput(
            label=_("Aliases (optional)"),
            placeholder=_("other names, space or comma separated"),
            max_length=200,
            required=False,
        )
        self.cooldown_field = discord.ui.TextInput(
            label=_("Cooldown seconds (optional)"),
            placeholder="0",
            max_length=5,
            required=False,
        )
        self.add_item(self.name_field)
        self.add_item(self.content_field)
        self.add_item(self.aliases_field)
        self.add_item(self.cooldown_field)

    async def on_submit(self, interaction):
        try:
            name = cc.normalize_name(self.name_field.value)
            err = await self.panel.cog.validate_new_name(self.panel.guild.id, name)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            aliases, aerr = await self.panel.cog.validate_aliases(
                self.panel.guild.id, name, self.aliases_field.value
            )
            if aerr:
                return await interaction.response.send_message(aerr, ephemeral=True)
            await self.panel.cog.save_command(
                self.panel.guild.id,
                name,
                {
                    "type": "text",
                    "content": self.content_field.value.strip(),
                    "aliases": aliases,
                    "cooldown": parse_cooldown(self.cooldown_field.value),
                },
                interaction.user.id,
            )
            await self.panel.refresh(interaction, selected=name)
        except Exception:
            log.exception("Custom command text modal failed")
            await embed_creator.notify_failure(interaction)


class EditTextModal(LocaleModal):
    """Edit an existing text command's response (the name is kept)."""

    def __init__(self, panel, name, response):
        super().__init__(title=_("Edit: {name}").format(name=name)[:45])
        self.panel = panel
        self.name = name
        self.content_field = discord.ui.TextInput(
            label=_("Response text"),
            style=discord.TextStyle.paragraph,
            default=(response.get("content") or None),
            placeholder=PLACEHOLDER_HINT,
            max_length=cc.MAX_TEXT_LENGTH,
            required=True,
        )
        self.aliases_field = discord.ui.TextInput(
            label=_("Aliases (optional)"),
            default=(" ".join(response.get("aliases") or []) or None),
            placeholder=_("other names, space or comma separated"),
            max_length=200,
            required=False,
        )
        self.cooldown_field = discord.ui.TextInput(
            label=_("Cooldown seconds (optional)"),
            default=str(response.get("cooldown") or 0),
            max_length=5,
            required=False,
        )
        self.add_item(self.content_field)
        self.add_item(self.aliases_field)
        self.add_item(self.cooldown_field)

    async def on_submit(self, interaction):
        try:
            aliases, aerr = await self.panel.cog.validate_aliases(
                self.panel.guild.id, self.name, self.aliases_field.value
            )
            if aerr:
                return await interaction.response.send_message(aerr, ephemeral=True)
            await self.panel.cog.save_command(
                self.panel.guild.id,
                self.name,
                {
                    "type": "text",
                    "content": self.content_field.value.strip(),
                    "aliases": aliases,
                    "cooldown": parse_cooldown(self.cooldown_field.value),
                },
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
        self.aliases_field = discord.ui.TextInput(
            label=_("Aliases (optional)"),
            placeholder=_("other names, space or comma separated"),
            max_length=200,
            required=False,
        )
        self.cooldown_field = discord.ui.TextInput(
            label=_("Cooldown seconds (optional)"),
            placeholder="0",
            max_length=5,
            required=False,
        )
        self.add_item(self.name_field)
        self.add_item(self.aliases_field)
        self.add_item(self.cooldown_field)

    async def on_submit(self, interaction):
        try:
            name = cc.normalize_name(self.name_field.value)
            err = await self.panel.cog.validate_new_name(self.panel.guild.id, name)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            aliases, aerr = await self.panel.cog.validate_aliases(
                self.panel.guild.id, name, self.aliases_field.value
            )
            if aerr:
                return await interaction.response.send_message(aerr, ephemeral=True)
            draft = {
                "name": name,
                "embed": embed_creator.default_embed(),
                "aliases": aliases,
                "cooldown": parse_cooldown(self.cooldown_field.value),
            }
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
            {
                "type": "embed",
                "embed": self.draft["embed"],
                "aliases": self.draft.get("aliases") or [],
                "cooldown": int(self.draft.get("cooldown") or 0),
            },
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
            draft = {
                "name": name,
                "embed": embed_creator.merge_embed(response.get("embed")),
                "aliases": response.get("aliases") or [],
                "cooldown": int(response.get("cooldown") or 0),
            }
            view = CustomEmbedPanel(
                self._owner.cog, self._owner.guild, self._owner.author_id, draft
            )
            await interaction.response.send_message(
                embed=view.build_embed(), view=view, ephemeral=True
            )
            view.message = await interaction.original_response()
        else:
            await interaction.response.send_modal(
                EditTextModal(self._owner, name, response)
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
                detail = content[:200] or _("*empty*")
            meta = []
            uses = self.cog._uses.get(self.guild.id, {}).get(self.selected)
            if uses is not None:
                meta.append(_("used {count}x").format(count=uses))
            aliases = resp.get("aliases") or []
            if aliases:
                meta.append(
                    _("aliases: {list}").format(
                        list=", ".join(f"`{a}`" for a in aliases)
                    )
                )
            cooldown = int(resp.get("cooldown") or 0)
            if cooldown:
                meta.append(_("cooldown: {sec}s").format(sec=cooldown))
            if meta:
                detail = detail + "\n" + " | ".join(meta)
            embed.add_field(
                name=_("Selected: {name}").format(name=self.selected),
                value=detail[:1024],
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
        # guild_id -> {name: uses} (for the panel's usage display).
        self._uses = {}
        # (guild_id, name, user_id) -> monotonic expiry for per-command cooldowns.
        self._cd = {}

    async def get_commands(self, guild_id):
        """Return {name: response} for a guild (cached; loads on a miss)."""
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db_pool.fetch(
            "SELECT name, response, uses FROM custom_commands WHERE guild_id = $1",
            guild_id,
        )
        data, uses = {}, {}
        for row in rows:
            response = row["response"]
            if isinstance(response, str):
                response = json.loads(response)
            data[row["name"]] = response
            uses[row["name"]] = row["uses"]
        self._cache[guild_id] = data
        self._uses[guild_id] = uses
        return data

    async def _resolve(self, guild_id, typed):
        """Resolve a typed name to (name, response) by name then alias, or (None, None)."""
        cmds = await self.get_commands(guild_id)
        if typed in cmds:
            return typed, cmds[typed]
        for name, resp in cmds.items():
            if typed in (resp.get("aliases") or []):
                return name, resp
        return None, None

    async def validate_aliases(self, guild_id, name, raw):
        """Parse a raw alias string into a clean list, or return (None, error).

        Aliases are extra names the command also answers to. Each must be a
        valid, non-reserved name; the command's own name is dropped, duplicates
        collapse, and the list is capped. Light on purpose - a clash between two
        custom commands' aliases just resolves first-match.
        """
        parts = [cc.normalize_name(p) for p in re.split(r"[,\s]+", raw or "") if p.strip()]
        if not parts:
            return [], None
        reserved = set(self.bot.all_commands.keys())
        clean, seen = [], set()
        for alias in parts:
            if alias == name or alias in seen:
                continue
            err = cc.validate_name(alias, reserved=reserved, existing=set())
            if err:
                return None, _("Alias `{alias}` is not usable: {why}").format(
                    alias=alias, why=_NAME_ERRORS[err]()
                )
            seen.add(alias)
            clean.append(alias)
        return clean[:5], None

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
        self._uses.pop(guild_id, None)

    async def delete_command(self, guild_id, name):
        await self.bot.db_pool.execute(
            "DELETE FROM custom_commands WHERE guild_id = $1 AND name = $2",
            guild_id,
            name,
        )
        self._cache.pop(guild_id, None)
        self._uses.pop(guild_id, None)

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
        typed = (ctx.invoked_with or "").lower()
        if not typed:
            return False
        name, response = await self._resolve(ctx.guild.id, typed)
        if response is None:
            return False

        # Per-user cooldown, if the command sets one. On cooldown we swallow it
        # (return True) so nothing is sent and the "did you mean" stays quiet.
        cooldown = int(response.get("cooldown") or 0)
        if cooldown > 0:
            key = (ctx.guild.id, name, ctx.author.id)
            now = time.monotonic()
            if self._cd.get(key, 0.0) > now:
                return True
            self._cd[key] = now + cooldown
            if len(self._cd) > 5000:
                self._cd = {k: v for k, v in self._cd.items() if v > now}

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
