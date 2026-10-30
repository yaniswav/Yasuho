"""Per-guild custom (canned) commands, run by mention or `/custom`.

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

from . import command_naming as cc
from tools import embed_creator, premium, premium_upsell
from tools.formats import random_colour
from tools.i18n import N_, _, ngettext
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Quick-pick per-command cooldowns: (extractable label, value in seconds fed to
# parse_cooldown). The custom seconds box overrides any pick. Labels are marked
# with N_ (module-level) and translated at modal-build time with _(...).
_COOLDOWN_PRESETS = (
    (N_("None"), "0"),
    (N_("10s"), "10"),
    (N_("30s"), "30"),
    (N_("1m"), "60"),
    (N_("5m"), "300"),
)

PLACEHOLDERS = [
    ("{user}", "A mention of whoever ran the command"),
    ("{server}", "The server name"),
    ("{members}", "The current member count"),
]
PLACEHOLDER_HINT = "{user} {server} {members}"

# error key -> user-facing message (keys come from cogs.config.command_naming).
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


def _with_buttons(response, buttons):
    """Carry the stored link buttons through an edit that rebuilds the blob."""
    if buttons:
        response["buttons"] = buttons
    return response


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


def build_cooldown_fields(current):
    """Build the shared cooldown picker (radio quick-pick + custom seconds box).

    Returns ``(label_item, radio, custom_input)``. The radio preselects the
    preset matching ``current`` when one does, otherwise it stays empty and the
    custom box is prefilled with the exact seconds. Shared by the three
    cooldown-carrying modals so the widget and its copy live in one place.
    """
    current = int(current or 0)
    radio = discord.ui.RadioGroup(required=False)
    matched = False
    for label, value in _COOLDOWN_PRESETS:
        is_current = int(value) == current
        matched = matched or is_current
        radio.add_option(label=_(label), value=value, default=is_current)
    custom = discord.ui.TextInput(
        label=_("Custom seconds"),
        placeholder="0",
        required=False,
        max_length=5,
    )
    if current and not matched:
        custom.default = str(current)
    label_item = discord.ui.Label(
        text=_("Cooldown seconds (optional)"),
        component=radio,
        description=_("Pick one, or type a custom value below to override."),
    )
    return label_item, radio, custom


def resolve_cooldown(radio, custom):
    """Resolve the cooldown picker: custom box wins, else the preset, else 0."""
    raw = (custom.value or "").strip()
    if raw:
        return parse_cooldown(raw)
    if radio.value is not None:
        return parse_cooldown(radio.value)
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
        cooldown_label, self.cooldown_radio, self.cooldown_custom = build_cooldown_fields(0)
        self.add_item(self.name_field)
        self.add_item(self.content_field)
        self.add_item(self.aliases_field)
        self.add_item(cooldown_label)
        self.add_item(self.cooldown_custom)

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
                    "cooldown": resolve_cooldown(self.cooldown_radio, self.cooldown_custom),
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
        self.buttons = response.get("buttons")
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
        cooldown_label, self.cooldown_radio, self.cooldown_custom = build_cooldown_fields(
            response.get("cooldown") or 0
        )
        self.add_item(self.content_field)
        self.add_item(self.aliases_field)
        self.add_item(cooldown_label)
        self.add_item(self.cooldown_custom)

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
                _with_buttons(
                    {
                        "type": "text",
                        "content": self.content_field.value.strip(),
                        "aliases": aliases,
                        "cooldown": resolve_cooldown(self.cooldown_radio, self.cooldown_custom),
                    },
                    self.buttons,
                ),
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
        cooldown_label, self.cooldown_radio, self.cooldown_custom = build_cooldown_fields(0)
        self.add_item(self.name_field)
        self.add_item(self.aliases_field)
        self.add_item(cooldown_label)
        self.add_item(self.cooldown_custom)

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
                "cooldown": resolve_cooldown(self.cooldown_radio, self.cooldown_custom),
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
        if len(embed) > cc.MAX_EMBED_TOTAL:
            return await interaction.response.send_message(
                _("The embed is too long: Discord allows {n} characters in total.").format(
                    n=cc.MAX_EMBED_TOTAL
                ),
                ephemeral=True,
            )
        await self.cog.save_command(
            self.guild.id,
            self.draft["name"],
            _with_buttons(
                {
                    "type": "embed",
                    "embed": self.draft["embed"],
                    "aliases": self.draft.get("aliases") or [],
                    "cooldown": int(self.draft.get("cooldown") or 0),
                },
                self.draft.get("buttons"),
            ),
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
        response = (await self._owner.cog.get_custom_commands(self._owner.guild.id)).get(name)
        if response is None:
            return await self._owner.refresh(interaction, selected=None)
        if response.get("type") == "embed":
            draft = {
                "name": name,
                "embed": embed_creator.merge_embed(response.get("embed")),
                "aliases": response.get("aliases") or [],
                "cooldown": int(response.get("cooldown") or 0),
                "buttons": response.get("buttons"),
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


_BUTTON_ERRORS = {
    "too_many": lambda: _("At most {n} buttons.").format(n=cc.MAX_BUTTONS),
    "format": lambda: _("Write each button as: label | https://link"),
    "label": lambda: _("A button label must be 1 to {n} characters.").format(
        n=cc.MAX_BUTTON_LABEL
    ),
    "url": lambda: _("A button link must be a valid https:// address."),
}


class ButtonsModal(LocaleModal):
    """Set the link buttons shown under a command's reply (Yasuho+)."""

    def __init__(self, panel, name, response, limit):
        super().__init__(title=_("Buttons: {name}").format(name=name)[:45])
        self.panel = panel
        self.name = name
        self.limit = limit
        current = "\n".join(
            f"{b['label']} | {b['url']}" for b in cc.read_buttons(response.get("buttons"), limit)
        )
        self.buttons_field = discord.ui.TextInput(
            label=_("Link buttons (one per line)"),
            style=discord.TextStyle.paragraph,
            default=current or None,
            placeholder="Rules | https://example.com/rules",
            max_length=1000,
            required=False,
        )
        self.add_item(self.buttons_field)

    async def on_submit(self, interaction):
        try:
            buttons, err = cc.parse_buttons(self.buttons_field.value)
            if err is None and len(buttons) > self.limit:
                err = "too_many"
            if err:
                return await interaction.response.send_message(
                    _BUTTON_ERRORS[err](), ephemeral=True
                )
            # Re-read: the command may have been edited while the modal was open.
            response = (await self.panel.cog.get_custom_commands(self.panel.guild.id)).get(
                self.name
            )
            if response is None:
                return await self.panel.refresh(interaction, selected=None)
            response = dict(response)
            if buttons:
                response["buttons"] = buttons
            else:
                response.pop("buttons", None)
            await self.panel.cog.save_command(
                self.panel.guild.id, self.name, response, interaction.user.id
            )
            await self.panel.refresh(interaction, selected=self.name)
        except Exception:
            log.exception("Custom command buttons modal failed")
            await embed_creator.notify_failure(interaction)


class _ButtonsButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Buttons"),
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=panel.selected is None,
        )

    async def callback(self, interaction):
        try:
            name = self._owner.selected
            cog, guild = self._owner.cog, self._owner.guild
            response = (await cog.get_custom_commands(guild.id)).get(name) if name else None
            if response is None:
                return await self._owner.refresh(interaction, selected=None)
            limit = premium.resolve_guild_limits(cog.bot, guild.id).max_command_buttons
            if limit <= 0:
                return await cog.refuse_buttons(interaction)
            await interaction.response.send_modal(
                ButtonsModal(self._owner, name, response, limit)
            )
        except Exception:
            log.exception("Custom command buttons button failed")
            await embed_creator.notify_failure(interaction)


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
        self.add_item(_ButtonsButton(self))
        self.add_item(_DeleteButton(self))

    def build_embed(self):
        embed = discord.Embed(
            title=_("Custom commands"),
            description=_(
                "Commands members run with `/custom` or by mentioning me "
                "(`@me name`). Add a text or embed reply, or pick one to "
                "inspect and delete."
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
            n_buttons = len(cc.read_buttons(resp.get("buttons"), cc.MAX_BUTTONS))
            if n_buttons:
                meta.append(
                    ngettext("{count} button", "{count} buttons", n_buttons).format(
                        count=n_buttons
                    )
                )
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
        commands_map = await self.cog.get_custom_commands(self.guild.id)
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
    """Per-guild canned commands run by mention or `/custom`."""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {name: response_dict}; lazily loaded, invalidated on write.
        self._cache = {}
        # guild_id -> {name: uses} (for the panel's usage display).
        self._uses = {}
        # (guild_id, name, user_id) -> monotonic expiry for per-command cooldowns.
        self._cd = {}

    async def get_custom_commands(self, guild_id):
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
        cmds = await self.get_custom_commands(guild_id)
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

    async def refuse_buttons(self, interaction):
        """Refuse the Yasuho+ link buttons: the text, plus the house upsell."""
        upsell = await premium_upsell.for_guild_refusal(
            self.bot,
            limit_key="max_command_buttons",
            guild_id=interaction.guild.id,
            person_id=interaction.user.id,
            is_admin=premium_upsell.invoker_is_admin(interaction.user),
            already_top_tier=premium_upsell.is_guild_already_top_tier(
                self.bot, interaction.guild.id
            ),
            kind="unlock",
        )
        text = _("Link buttons under a command's reply are a Yasuho+ feature.")
        kwargs = {"ephemeral": True}
        if upsell is not None:
            text += "\n" + upsell.line
            view = upsell.view()
            if view is not None:
                kwargs["view"] = view
        await interaction.response.send_message(text, **kwargs)

    def _max_buttons(self, guild):
        return premium.resolve_guild_limits(self.bot, guild.id).max_command_buttons

    async def at_capacity(self, guild_id):
        return len(await self.get_custom_commands(guild_id)) >= cc.MAX_COMMANDS_PER_GUILD

    async def validate_new_name(self, guild_id, name):
        """Return a user-facing error string for a proposed name, or None."""
        reserved = set(self.bot.all_commands.keys())
        existing = set(await self.get_custom_commands(guild_id))
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
        self._cd = {k: v for k, v in self._cd.items() if k[0] != guild_id}

    async def delete_command(self, guild_id, name):
        await self.bot.db_pool.execute(
            "DELETE FROM custom_commands WHERE guild_id = $1 AND name = $2",
            guild_id,
            name,
        )
        self._cache.pop(guild_id, None)
        self._uses.pop(guild_id, None)
        self._cd = {k: v for k, v in self._cd.items() if k[0] != guild_id}

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
            return
        # Keep the cached count live so the panel does not show a stale number.
        guild_uses = self._uses.get(guild_id)
        if guild_uses is not None and name in guild_uses:
            guild_uses[name] += 1

    @commands.hybrid_command(name="customcommands", aliases=["cc", "customcommand"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def customcommands(self, ctx):
        """Open the custom-commands builder."""
        commands_map = await self.get_custom_commands(ctx.guild.id)
        view = CustomCommandsPanel(self, ctx.guild, ctx.author.id, dict(commands_map))
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="custom")
    @commands.guild_only()
    @discord.app_commands.describe(name="The custom command (or alias) to run.")
    async def custom(self, ctx, *, name: str = None):
        """Run one of this server's custom commands."""
        # A bare `@bot custom` keeps reaching a stored command (or alias) that
        # was named "custom" before this command existed.
        name = name or ctx.invoked_with or ""
        resolved, response = await self._resolve(ctx.guild.id, name.strip().lower())
        if response is None:
            await ctx.send(
                _("This server has no custom command called `{name}`.").format(
                    name=discord.utils.escape_markdown(name[:32])
                ),
                ephemeral=True,
            )
            return
        if self._cooling(ctx.guild.id, resolved, ctx.author.id, response):
            await ctx.send(
                _("That command is on cooldown, try again in a moment."),
                ephemeral=True,
            )
            return
        payload = self._payload(response, ctx.author, ctx.guild, self._max_buttons(ctx.guild))
        if payload is None:
            await ctx.send(_("That command has nothing to say."), ephemeral=True)
            return
        try:
            await ctx.send(**payload)
        except discord.HTTPException:
            log.exception("Custom command send failed")
            return
        await self._bump_uses(ctx.guild.id, resolved)

    @custom.autocomplete("name")
    async def _custom_autocomplete(self, interaction, current):
        """Suggest this server's command names; served from the per-guild cache."""
        if interaction.guild is None:
            return []
        current = (current or "").strip().lower()
        names = await self.get_custom_commands(interaction.guild.id)
        return [
            discord.app_commands.Choice(name=n, value=n)
            for n in sorted(names)
            if current in n
        ][:25]

    async def handle_unknown(self, ctx):
        """Dispatch a custom command for an unknown mention command.

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

        # On cooldown we swallow it (return True) so nothing is sent and the
        # "did you mean" stays quiet.
        if self._cooling(ctx.guild.id, name, ctx.author.id, response):
            return True

        await self._run(ctx.message, name, response)
        return True

    def _cooling(self, guild_id, name, user_id, response):
        """True if this user is on the command's cooldown; otherwise start it."""
        # Per-user cooldown, if the command sets one.
        cooldown = int(response.get("cooldown") or 0)
        if cooldown <= 0:
            return False
        key = (guild_id, name, user_id)
        now = time.monotonic()
        if self._cd.get(key, 0.0) > now:
            return True
        self._cd[key] = now + cooldown
        if len(self._cd) > 5000:
            self._cd = {k: v for k, v in self._cd.items() if v > now}
        return False

    @staticmethod
    def _payload(response, member, guild, max_buttons=0):
        """Build the send kwargs for a response, or None when it renders empty.

        ``max_buttons`` is the guild's entitlement: 0 (free or lapsed) leaves the
        stored link buttons unrendered but untouched.
        """
        substitute = build_substitution(member, guild)
        extra = {}
        buttons = cc.read_buttons(response.get("buttons"), max_buttons)
        if buttons:
            view = discord.ui.View(timeout=None)
            for button in buttons:
                view.add_item(discord.ui.Button(label=button["label"], url=button["url"]))
            extra["view"] = view
        if response.get("type") == "embed":
            embed = embed_creator.render(
                response.get("embed") or {}, substitute=substitute
            )
            # Placeholders can grow the text past Discord's summed limit.
            if len(embed) > cc.MAX_EMBED_TOTAL:
                return None
            return {"embed": embed, **extra} if embed_creator.embed_has_content(embed) else None
        content = substitute(response.get("content") or "")
        if not content:
            return None
        # The text is admin-written and stored, but ANY member can trigger it -
        # the client default allowed_mentions (users and roles both True) would
        # let that stored text ping whoever it names. Only the member who ran
        # the command may be pinged by their own invocation.
        return {
            "content": content[: cc.MAX_TEXT_LENGTH],
            "allowed_mentions": discord.AllowedMentions(
                everyone=False, roles=False, users=[member], replied_user=False,
            ),
            **extra,
        }

    async def _run(self, message, name, response):
        payload = self._payload(response, message.author, message.guild, self._max_buttons(message.guild))
        if payload is None:
            return
        try:
            await message.channel.send(**payload)
        except discord.HTTPException:
            log.exception("Custom command send failed")
            return
        await self._bump_uses(message.guild.id, name)


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
