import logging
import secrets
from datetime import timedelta

import discord
import Levenshtein as lv
from discord.ext import commands

from tools import arg_completion
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)


def _error_embed(ctx, name, value):
    """Build the standard error embed used by every branch below.

    Discord caps a field name at 256 and a field value at 1024 characters; a
    long error or usage string would otherwise 400 the whole error report (which
    discord.py then swallows as "Ignoring exception in on_command_error", hiding
    the real error). Clamp both so the report always sends.
    """
    if len(value) > 1024:
        value = value[:1021] + "..."
    return (
        discord.Embed(
            color=random_colour(),
            timestamp=ctx.message.created_at,
        )
        .add_field(name=name[:256], value=value or "​")
        .set_footer(text=ctx.author, icon_url=ctx.author.display_avatar.url)
    )


def _usage(ctx):
    """Return the command usage line with the bot mention replaced by @name."""
    return (
        _(":information_source: Command usage: `{prefix}{command} {signature}`")
        .format(
            prefix=ctx.prefix,
            command=ctx.command,
            signature=ctx.command.signature,
        )
        .replace(ctx.me.mention, f"@{ctx.bot.user.name}")
    )


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
            # A per-guild custom command may claim this name; if it does, it
            # replies and we stop (no "did you mean" for a real custom command).
            cc_cog = self.bot.get_cog("CustomCommands")
            if cc_cog is not None:
                try:
                    if await cc_cog.handle_unknown(ctx):
                        return
                except Exception:
                    log.exception("Custom command dispatch failed")

            try:
                suggestions = (
                    " | ".join(
                        str(command)
                        for command in self.bot.commands
                        if lv.distance(ctx.invoked_with, command.name) < 4
                        and not command.hidden
                    )
                    or _("Sorry, no similar commands found")
                )
                await ctx.send(
                    embed=_error_embed(
                        ctx,
                        _("**Invalid command entered. Did you mean:**"),
                        f"`{suggestions}`",
                    ),
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
                embed=_error_embed(
                    ctx,
                    _("**Seems like you're missing a required argument:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                )
            )

        elif isinstance(error, commands.BadArgument):
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like you gave me a bad argument:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                )
            )

        elif isinstance(error, commands.CommandOnCooldown):

            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like you are on cooldown:**"),
                    _(":hourglass: Remaining time: `{time}`\n{usage}").format(
                        time=timedelta(seconds=int(error.retry_after)),
                        usage=_usage(ctx),
                    ),
                ),
                delete_after=60,
            )

        elif isinstance(error, discord.Forbidden):
            await ctx.send(_("I need more permissions!"), delete_after=3)

        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like you can't use this command in private messages:**"),
                    _(
                        "Go in a guild where I am or invite me in your server\n"
                        "invite.yasuho.xyz"
                    ),
                )
            )

        elif isinstance(error, discord.HTTPException):
            pass

        elif isinstance(error, commands.TooManyArguments):
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like you gave me too many arguments:**"),
                    _(
                        ":question: What to do: `look at {prefix}help and try being "
                        "more specific`\n{usage}"
                    ).format(prefix=ctx.prefix, usage=_usage(ctx)),
                )
            )

        elif isinstance(error, commands.UserInputError):
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like you did something wrong:**"),
                    _(
                        ":question: What to do: `look at {prefix}help and try being "
                        "more specific`\n{usage}"
                    ).format(prefix=ctx.prefix, usage=_usage(ctx)),
                )
            )

        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like you are missing permissions:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                )
            )

        elif isinstance(error, commands.DisabledCommand):
            return

        elif isinstance(error, commands.CommandInvokeError):
            error_id = secrets.token_hex(4)
            original = error.original
            log.error(
                "Command invocation failed [error_id=%s command=%s user=%s guild=%s]",
                error_id,
                getattr(ctx.command, "qualified_name", None),
                ctx.author.id,
                ctx.guild.id if ctx.guild else None,
                exc_info=(
                    type(original),
                    original,
                    original.__traceback__,
                ),
            )
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like something went wrong while executing command:**"),
                    _(
                        ":question: What to do: `Report this error identifier to "
                        "the bot owner`: `{error_id}`\n{usage}"
                    ).format(error_id=error_id, usage=_usage(ctx)),
                )
            )

        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(
                embed=_error_embed(
                    ctx,
                    _("**Seems like I am missing permissions:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                )
            )


async def setup(bot):
    await bot.add_cog(Errors(bot))
