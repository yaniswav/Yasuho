import logging
from datetime import timedelta

import discord
import Levenshtein as lv
from discord.ext import commands

from tools import arg_completion
from tools.formats import random_colour

log = logging.getLogger(__name__)


class Errors(commands.Cog):
    """Global command error handler that reports failures as embeds."""

    def __init__(self, bot):
        self.bot = bot
        bot.on_command_error = self._on_command_error

    async def _on_command_error(self, ctx, error, bypass=False):
        if (
            hasattr(ctx.command, "on_error")
            or (ctx.command and hasattr(ctx.cog, f"_{ctx.command.cog_name}__error"))
            and not bypass
        ):
            return

        if isinstance(error, commands.CommandNotFound):
            try:
                await ctx.send(
                    embed=discord.Embed(
                        color=random_colour(),
                        timestamp=ctx.message.created_at,
                    )
                    .add_field(
                        name="**Invalid command entered. Did you mean:**",
                        value=f"`{' | '.join(str(command) for command in self.bot.commands if lv.distance(ctx.invoked_with, command.name) < 4 and not command.hidden) or 'Sorry, no similar commands found'}`",
                    )
                    .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url),
                    delete_after=10,
                )

            except Exception:
                pass

        elif isinstance(error, commands.MissingRequiredArgument):
            # First try to guide the user through the missing arguments with an
            # interactive form (select menus / a modal). Only fall back to the
            # plain usage message when that is not possible for this command.
            try:
                if await arg_completion.start(ctx, error):
                    return
            except Exception:
                log.exception("Interactive arg completion failed; using usage text")

            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you're missing a required argument:**",
                    value=f":warning: Error: `{error}`\n:information_source: Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, commands.BadArgument):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you gave me a bad argument:**",
                    value=f":warning: Error: `{error}`\n:information_source:  Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, commands.CommandOnCooldown):

            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you are on cooldown:**",
                    value=f":hourglass: Remaining time: `{timedelta(seconds=int(error.retry_after))}`\n:information_source: Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url),
                delete_after=60,
            )

        elif isinstance(error, discord.Forbidden):
            await ctx.send("I need more permissions!", delete_after=3)

        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you can't use this command in private messages:**",
                    value="Go in a guild where I am or invite me in your server\ninvite.yasuho.xyz".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, discord.HTTPException):
            pass

        elif isinstance(error, commands.TooManyArguments):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you gave me too many arguments:**",
                    value=f":question: What to do: `look at {ctx.prefix}help and try being more specific`\n:information_source:  Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, commands.UserInputError):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you did something wrong:**",
                    value=f":question: What to do: `look at {ctx.prefix}help and try being more specific`\n:information_source:  Command usage: `{ctx.prefix}{ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like you are missing permissions:**",
                    value=f":warning: Error: `{error}`\n:information_source:  Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, commands.DisabledCommand):
            return

        elif isinstance(error, commands.CommandInvokeError):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like something went wrong while executing command:**",
                    value=f":question: What to do: `Report the bug to bot owner` [<@!228895251576782858>]\n:warning: Error: `{error}`\n:information_source:  Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )

        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                )
                .add_field(
                    name="**Seems like I am missing permissions:**",
                    value=f":warning: Error: `{error}`\n:information_source:  Command usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`".replace(
                        ctx.me.mention, f"@{self.bot.user.name}"
                    ),
                )
                .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
            )


async def setup(bot):
    await bot.add_cog(Errors(bot))
