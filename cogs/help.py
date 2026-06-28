import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.paginator import Paginator

log = logging.getLogger(__name__)


class YasuhoHelp(commands.HelpCommand):
    """Custom help command for Yasuho (replaces the default help_command)."""

    def _colour(self):
        return random_colour()

    async def send_bot_help(self, mapping):
        prefix = self.context.clean_prefix
        embeds = []

        for cog, cmds in mapping.items():
            visible = await self.filter_commands(cmds, sort=True)
            if not visible:
                continue

            name = cog.qualified_name if cog is not None else "No Category"
            command_names = ", ".join(f"`{command.name}`" for command in visible)
            embed = discord.Embed(
                title=f"Help | {name}",
                description=command_names,
                colour=self._colour(),
            )
            embed.set_footer(text=f"Use {prefix}help <command> for more info")
            embeds.append(embed)

        if not embeds:
            embeds.append(
                discord.Embed(
                    title="Help",
                    description=f"Use `{prefix}help <command>` for more info on a command.",
                    colour=self._colour(),
                )
            )

        await Paginator(embeds, author_id=self.context.author.id).start(
            self.get_destination()
        )

    async def send_cog_help(self, cog):
        prefix = self.context.clean_prefix
        embed = discord.Embed(
            title=f"{cog.qualified_name} commands",
            description=cog.description or None,
            colour=self._colour(),
        )

        visible = await self.filter_commands(cog.get_commands(), sort=True)
        for command in visible:
            embed.add_field(
                name=command.name,
                value=command.short_doc or "No description provided.",
                inline=False,
            )

        embed.set_footer(text=f"Use {prefix}help <command> for more info")
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command):
        embed = discord.Embed(
            title=self.get_command_signature(command),
            description=command.help or "No description provided.",
            colour=self._colour(),
        )

        if command.aliases:
            aliases = ", ".join(f"`{alias}`" for alias in command.aliases)
            embed.add_field(name="Aliases", value=aliases, inline=False)

        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group):
        embed = discord.Embed(
            title=self.get_command_signature(group),
            description=group.help or "No description provided.",
            colour=self._colour(),
        )

        if group.aliases:
            aliases = ", ".join(f"`{alias}`" for alias in group.aliases)
            embed.add_field(name="Aliases", value=aliases, inline=False)

        visible = await self.filter_commands(group.commands, sort=True)
        if visible:
            subcommands = "\n".join(
                f"`{command.name}` - {command.short_doc or 'No description provided.'}"
                for command in visible
            )
            embed.add_field(name="Subcommands", value=subcommands, inline=False)

        await self.get_destination().send(embed=embed)

    async def send_error_message(self, error):
        embed = discord.Embed(
            title="Help",
            description=error,
            colour=self._colour(),
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
