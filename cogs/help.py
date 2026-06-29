import logging

import discord
from discord.ext import commands

from tools import settings
from tools.formats import plural, random_colour

log = logging.getLogger(__name__)

NO_CATEGORY = "No Category"


class CategorySelect(discord.ui.Select):
    """Dropdown of help categories; selecting one shows that cog's commands."""

    def __init__(self, categories):
        options = [
            discord.SelectOption(label=name) for name in categories[:25]
        ]
        super().__init__(
            placeholder="Choose a category…",
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
            try:
                await interaction.response.send_message(
                    "Something went wrong opening that category.", ephemeral=True
                )
            except Exception:
                pass


class HelpView(discord.ui.View):
    """Author-restricted, navigable overview of every command category."""

    def __init__(self, help_command, categories, timeout=180):
        super().__init__(timeout=timeout)
        self.bot = help_command.context.bot
        self.author_id = help_command.context.author.id
        self.prefix = help_command.context.clean_prefix
        self.categories = categories
        self.message = None
        self.add_item(CategorySelect(categories))

    def _category_commands(self, name):
        """Visible (non-hidden) commands for a category, sorted by name."""
        if name == NO_CATEGORY:
            cmds = [c for c in self.bot.commands if c.cog is None]
        else:
            cog = self.bot.get_cog(name)
            cmds = cog.get_commands() if cog is not None else []
        return sorted((c for c in cmds if not c.hidden), key=lambda c: c.name)

    def home_embed(self):
        embed = discord.Embed(title="Help", colour=random_colour())
        lines = ["Select a category from the menu below to browse its commands.\n"]
        for name in self.categories:
            cog = self.bot.get_cog(name)
            count = len(self._category_commands(name))
            desc = cog.description.split("\n")[0] if cog and cog.description else ""
            if desc:
                lines.append(f"**{name}** — {desc} ({count})")
            else:
                lines.append(f"**{name}** ({plural(count):command})")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Use {self.prefix}help <command> for more info")
        return embed

    def category_embed(self, name):
        embed = discord.Embed(title=f"Help | {name}", colour=random_colour())
        cog = self.bot.get_cog(name)
        if cog is not None and cog.description:
            embed.description = cog.description
        for command in self._category_commands(name):
            embed.add_field(
                name=f"{self.prefix}{command.qualified_name}",
                value=command.short_doc or "No description provided.",
                inline=False,
            )
        embed.set_footer(text=f"Use {self.prefix}help <command> for more info")
        return embed

    async def show_category(self, interaction, name):
        await interaction.response.edit_message(
            embed=self.category_embed(name), view=self
        )

    @discord.ui.button(label="Home", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction, button):
        try:
            await interaction.response.edit_message(
                embed=self.home_embed(), view=self
            )
        except Exception:
            log.exception("Failed to return to help home")

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

    async def send_bot_help(self, mapping):
        prefix = self.context.clean_prefix

        categories = []
        for cog, cmds in mapping.items():
            if not any(not c.hidden for c in cmds):
                continue
            categories.append(cog.qualified_name if cog is not None else NO_CATEGORY)
        categories = sorted(categories)[:25]

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
            embed=view.home_embed(), view=view
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
                f"`{c.name}` — {c.short_doc or 'No description provided.'}"
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
