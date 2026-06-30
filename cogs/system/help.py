import logging

import discord
from discord.ext import commands

from tools import settings
from tools.formats import plural, random_colour

log = logging.getLogger(__name__)

NO_CATEGORY = "No Category"

# Discord caps an embed description at 4096 characters.
DESCRIPTION_LIMIT = 4096

# Once this many messages have been posted after the help menu, navigating to a
# new category re-posts the menu at the bottom of the channel instead of editing
# it in place (otherwise it would update far up where the user can no longer see
# it). Self-correcting: right after a re-post nothing is below it, so subsequent
# navigation edits in place again until the channel buries it once more.
REPOST_AFTER_MESSAGES = 4

# Curated, top-level help taxonomy: (emoji, display name, member cog classes).
# Each entry groups one or more cog *class names* (what ``bot.get_cog`` expects)
# under a single, human-friendly category so the menu stays tidy instead of
# showing one category per cog. Cogs not listed here but with visible commands
# are swept into the "Other" catch-all (see ``build_categories``).
CATEGORIES = [
    ("🔨", "Moderation", ["Moderation", "AutoMod", "ModLog", "Blacklist"]),
    (
        "⚙️",
        "Server Config",
        ["Settings", "Welcome", "ReactionRoles", "Starboard", "TemporaryRooms", "Twitch"],
    ),
    ("📈", "Community", ["Leveling", "Profiles", "AFK", "Reminder", "AvatarHistory", "UserSettings"]),
    ("🎮", "Fun & Games", ["Fun", "Games"]),
    ("📺", "AniList", ["AniList"]),
    ("🔧", "Tools & Info", ["Info", "Meta", "Utility", "Extras", "SearchWeb"]),
    ("🎵", "Music", ["Music"]),
]
OTHER_EMOJI = "🧩"
OTHER_NAME = "Other"

# Cogs never surfaced as a category (the help cog wires itself in separately).
EXCLUDED_COGS = {"Help"}


def _visible_commands(cmds):
    """Non-hidden commands from ``cmds``, sorted by name."""
    return sorted((c for c in cmds if not c.hidden), key=lambda c: c.name)


def build_categories(bot):
    """Resolve :data:`CATEGORIES` against the bot's currently loaded cogs.

    Returns an ordered list of dicts::

        {"emoji": str, "name": str,
         "groups": [(cog_label, [commands]), ...], "total": int}

    Only the visible (non-hidden) commands of cogs that actually exist are
    included; a member cog that is unloaded or has no visible commands is
    skipped, and a category with zero visible commands is omitted entirely.

    Any cog with visible commands that is not listed in the taxonomy (and is
    not excluded, e.g. the help cog) is collected into a final "Other"
    category so nothing silently disappears.
    """
    resolved = []
    claimed = set(EXCLUDED_COGS)

    for emoji, name, cog_names in CATEGORIES:
        groups = []
        total = 0
        for cog_name in cog_names:
            claimed.add(cog_name)
            cog = bot.get_cog(cog_name)
            if cog is None:
                continue
            cmds = _visible_commands(cog.get_commands())
            if not cmds:
                continue
            groups.append((cog_name, cmds))
            total += len(cmds)
        if total:
            resolved.append(
                {"emoji": emoji, "name": name, "groups": groups, "total": total}
            )

    # Catch-all: cogs (and cog-less commands) not claimed by the taxonomy.
    other_groups = []
    other_total = 0
    for cog_name, cog in bot.cogs.items():
        if cog_name in claimed:
            continue
        cmds = _visible_commands(cog.get_commands())
        if not cmds:
            continue
        other_groups.append((cog_name, cmds))
        other_total += len(cmds)

    cogless = _visible_commands([c for c in bot.commands if c.cog is None])
    if cogless:
        other_groups.append((NO_CATEGORY, cogless))
        other_total += len(cogless)

    if other_total:
        resolved.append(
            {
                "emoji": OTHER_EMOJI,
                "name": OTHER_NAME,
                "groups": other_groups,
                "total": other_total,
            }
        )

    return resolved


class CategorySelect(discord.ui.Select):
    """Dropdown of help categories; selecting one jumps to that category."""

    def __init__(self, options):
        super().__init__(
            placeholder="Jump to a category...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            await self.view.show_category(interaction, self.values[0])
        except Exception:
            log.exception("Failed to render help category")
            await self.view.report_error(interaction)


class HelpView(discord.ui.View):
    """Author-restricted, navigable overview of every command category.

    Combines a category dropdown (jump anywhere), previous/next pagination
    (step through categories one at a time) and a Home button (welcome page).
    State is a single index: ``None`` means the Home page, otherwise the
    position within ``self.categories``.
    """

    def __init__(self, help_command, categories, timeout=180):
        super().__init__(timeout=timeout)
        self.help_command = help_command
        self.bot = help_command.context.bot
        self.author_id = help_command.context.author.id
        self.prefix = help_command.context.clean_prefix
        # ``categories`` is a list of resolved category dicts (see
        # ``build_categories``); the index into it is the only navigation state.
        self.categories = categories
        self.index = None
        self.message = None
        self.select = CategorySelect(self._select_options())
        self.add_item(self.select)
        self._update_buttons()

    # -- data helpers -----------------------------------------------------

    def _select_options(self):
        options = []
        for position, category in enumerate(self.categories):
            options.append(
                discord.SelectOption(
                    label=category["name"],
                    value=str(position),
                    description=f"{plural(category['total']):command}",
                    emoji=category["emoji"],
                )
            )
        return options

    # -- embeds -----------------------------------------------------------

    def category_embed(self, index):
        category = self.categories[index]
        embed = discord.Embed(
            title=f"{category['emoji']} {category['name']}",
            colour=random_colour(),
        )

        # Categories aggregate several cogs, so render compactly: a bold
        # sub-header per cog, then one terse line per command. Built line by
        # line so we can stop cleanly before the 4096-char description limit.
        rendered = []
        for position, (label, commands_list) in enumerate(category["groups"]):
            if position:
                rendered.append("")
            rendered.append(f"**{label}**")
            for command in commands_list:
                doc = command.short_doc or "No description provided."
                rendered.append(f"`{self.prefix}{command.qualified_name}` - {doc}")

        notice = (
            f"...more commands available. Use "
            f"`{self.prefix}help <command>` to see them."
        )
        budget = DESCRIPTION_LIMIT - (len(notice) + 1)

        lines = []
        length = 0
        truncated = False
        for line in rendered:
            extra = len(line) + (1 if lines else 0)
            if length + extra > budget:
                truncated = True
                break
            lines.append(line)
            length += extra

        description = "\n".join(lines) if lines else "No commands available."
        if truncated:
            description += "\n" + notice
        embed.description = description

        embed.set_footer(
            text=(
                f"Category {index + 1}/{len(self.categories)} • "
                f"{self.prefix}help <command> for details"
            )
        )
        return embed

    # -- navigation -------------------------------------------------------

    def _update_buttons(self):
        active = self.index is not None
        self.previous.disabled = not active or self.index == 0
        self.forward.disabled = (
            not active or self.index == len(self.categories) - 1
        )

    async def _is_buried(self, interaction):
        """True when enough messages piled up after the menu to re-post it."""
        channel = interaction.channel
        if channel is None or self.message is None:
            return False
        try:
            count = 0
            async for _ in channel.history(
                limit=REPOST_AFTER_MESSAGES, after=self.message
            ):
                count += 1
            return count >= REPOST_AFTER_MESSAGES
        except discord.HTTPException:
            return False

    async def _repost(self, interaction, embed):
        """Re-post the menu at the bottom of the channel, dropping the old one."""
        await interaction.response.defer()
        old = self.message
        self.message = await interaction.channel.send(embed=embed, view=self)
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def _render(self, interaction):
        self._update_buttons()
        if self.index is None:
            embed = await self.help_command.home_embed()
        else:
            embed = self.category_embed(self.index)
        # If later messages have buried the menu, re-post it at the bottom rather
        # than editing in place (which would strand the user scrolled up).
        if await self._is_buried(interaction):
            await self._repost(interaction, embed)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def show_category(self, interaction, value):
        try:
            index = int(value)
        except (TypeError, ValueError):
            index = None
        if index is not None and 0 <= index < len(self.categories):
            self.index = index
        else:
            self.index = None
        await self._render(interaction)

    async def report_error(self, interaction):
        """Best-effort, ephemeral error notice that never raises."""
        try:
            message = "Something went wrong opening the help menu."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.debug("Failed to report help error", exc_info=True)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction, button):
        try:
            self.index = 0 if self.index is None else max(0, self.index - 1)
            await self._render(interaction)
        except Exception:
            log.exception("Failed to page to the previous help category")
            await self.report_error(interaction)

    @discord.ui.button(
        emoji="🏠", label="Home", style=discord.ButtonStyle.primary, row=1
    )
    async def home(self, interaction, button):
        try:
            self.index = None
            await self._render(interaction)
        except Exception:
            log.exception("Failed to return to help home")
            await self.report_error(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, row=1)
    async def forward(self, interaction, button):
        try:
            last = len(self.categories) - 1
            self.index = 0 if self.index is None else min(last, self.index + 1)
            await self._render(interaction)
        except Exception:
            log.exception("Failed to page to the next help category")
            await self.report_error(interaction)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class GroupHelpView(discord.ui.View):
    """Author-restricted group help with a per-user expand/collapse toggle."""

    def __init__(self, help_command, group, expand, timeout=180):
        super().__init__(timeout=timeout)
        self.help_command = help_command
        self.bot = help_command.context.bot
        self.author_id = help_command.context.author.id
        self.group = group
        self.expand = expand
        self.message = None
        self._sync_button()

    def _sync_button(self):
        self.toggle.label = (
            "Collapse subcommands" if self.expand else "Expand subcommands"
        )

    def embed(self):
        return self.help_command.group_embed(self.group, self.expand)

    @discord.ui.button(label="Expand subcommands", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction, button):
        try:
            self.expand = not self.expand
            await settings.set_user(
                self.bot.db_pool, self.author_id, "help_expand", self.expand
            )
            self._sync_button()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        except Exception:
            log.exception("Failed to toggle help_expand")
            try:
                await interaction.response.send_message(
                    "Something went wrong updating that preference.", ephemeral=True
                )
            except Exception:
                pass

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class YasuhoHelp(commands.HelpCommand):
    """Custom help command for Yasuho (replaces the default help_command)."""

    async def home_embed(self):
        """The friendly welcome page shown by the bot-wide help menu."""
        bot = self.context.bot
        prefix = self.context.clean_prefix

        embed = discord.Embed(
            title=f"{bot.user.name} • Help",
            description=(
                f"Hey there! I'm **{bot.user.name}**, glad to help. 💫\n\n"
                f"Use the menu to jump to a category, the arrows to browse, "
                f"or `{prefix}help <command>` for a specific command."
            ),
            colour=random_colour(),
        )
        embed.set_thumbnail(url=bot.user.display_avatar.url)

        # Server basics (or DM fallback).
        guild = self.context.guild
        if guild is not None:
            try:
                enabled = await settings.get_guild(
                    bot.db_pool, guild.id, "leveling_enabled", False
                )
                leveling = "Enabled ✅" if enabled else "Disabled ❌"
            except Exception:
                log.exception(
                    "Failed to read leveling_enabled for guild %s", guild.id
                )
                leveling = "Unknown"
            embed.add_field(
                name="🏠 Server",
                value=(
                    f"Prefix: `{prefix}`\n"
                    f"Members: **{guild.member_count:,}**\n"
                    f"Leveling: {leveling}"
                ),
                inline=True,
            )
        else:
            embed.add_field(
                name="🏠 Direct Messages",
                value=f"Default prefix: `{prefix}`",
                inline=True,
            )

        # Curated categories, split across fields to respect the 1024-char
        # limit. Each line shows the emoji, name and total visible-command
        # count aggregated across the category's member cogs.
        categories = build_categories(bot)
        lines = []
        total = 0
        for category in categories:
            total += category["total"]
            lines.append(
                f"{category['emoji']} **{category['name']}** - "
                f"{plural(category['total']):command}"
            )

        for position, chunk in enumerate(self._chunk(lines)):
            embed.add_field(
                name="📚 Categories" if position == 0 else "​",
                value=chunk,
                inline=position == 0 and guild is not None,
            )

        embed.set_footer(
            text=(
                f"{plural(total):command} across "
                f"{plural(len(categories)):category|categories} • "
                "Use the menu or arrows to explore"
            )
        )
        return embed

    @staticmethod
    def _chunk(lines, limit=1024):
        """Group lines into newline-joined blocks no longer than ``limit``."""
        chunks = []
        current = []
        length = 0
        for line in lines:
            extra = len(line) + 1
            if current and length + extra > limit:
                chunks.append("\n".join(current))
                current = []
                length = 0
            current.append(line)
            length += extra
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def send_bot_help(self, mapping):
        prefix = self.context.clean_prefix
        categories = build_categories(self.context.bot)

        if not categories:
            embed = discord.Embed(
                title="Help",
                description=f"Use `{prefix}help <command>` for more info on a command.",
                colour=random_colour(),
            )
            await self.get_destination().send(embed=embed)
            return

        view = HelpView(self, categories)
        view.message = await self.get_destination().send(
            embed=await self.home_embed(), view=view
        )

    def group_embed(self, group, expand):
        """Build the group-help embed, honouring the expand preference."""
        prefix = self.context.clean_prefix
        embed = discord.Embed(
            title=self.get_command_signature(group),
            description=group.help or "No description provided.",
            colour=random_colour(),
        )

        if group.aliases:
            aliases = ", ".join(f"`{alias}`" for alias in group.aliases)
            embed.add_field(name="Aliases", value=aliases, inline=False)

        subcommands = sorted(
            (c for c in group.commands if not c.hidden), key=lambda c: c.name
        )
        if not subcommands:
            return embed

        if expand:
            value = "\n".join(
                f"`{c.name}` - {c.short_doc or 'No description provided.'}"
                for c in subcommands
            )
            embed.add_field(name="Subcommands", value=value, inline=False)
        else:
            embed.add_field(
                name="Subcommands",
                value=(
                    f"This command has {plural(len(subcommands)):subcommand}. "
                    f"Toggle expansion in /settings, or use "
                    f"`{prefix}help {group.qualified_name} <subcommand>`."
                ),
                inline=False,
            )
        return embed

    async def send_command_help(self, command):
        embed = discord.Embed(
            title=self.get_command_signature(command),
            description=command.help or "No description provided.",
            colour=random_colour(),
        )

        if command.aliases:
            aliases = ", ".join(f"`{alias}`" for alias in command.aliases)
            embed.add_field(name="Aliases", value=aliases, inline=False)

        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group):
        expand = await settings.get_user(
            self.context.bot.db_pool, self.context.author.id, "help_expand", False
        )
        embed = self.group_embed(group, expand)

        if not any(not c.hidden for c in group.commands):
            await self.get_destination().send(embed=embed)
            return

        view = GroupHelpView(self, group, expand)
        view.message = await self.get_destination().send(embed=embed, view=view)

    async def send_error_message(self, error):
        embed = discord.Embed(
            title="Help",
            description=error,
            colour=random_colour(),
        )
        await self.get_destination().send(embed=embed)


class Help(commands.Cog):
    """Installs the custom help command and restores the original on unload."""

    def __init__(self, bot):
        self.bot = bot
        self._original = bot.help_command
        bot.help_command = YasuhoHelp()
        bot.help_command.cog = self

    async def cog_unload(self):
        self.bot.help_command = self._original


async def setup(bot):
    await bot.add_cog(Help(bot))
