import logging
import secrets
from datetime import timedelta

import discord
import Levenshtein as lv
from discord.ext import commands

from . import arg_completion
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


def _generic_report(ctx, error_id):
    """The generic "something broke, report this id" embed.

    Shared by the CommandInvokeError branch and the catch-all else so a crash
    reports identically whether it surfaced from a prefix or a slash (hybrid)
    invocation, and the traceable identifier wording lives in one place.
    """
    return _error_embed(
        ctx,
        _("**Seems like something went wrong while executing command:**"),
        _(
            ":question: What to do: `Report this error identifier to "
            "the bot owner`: `{error_id}`\n{usage}"
        ).format(error_id=error_id, usage=_usage(ctx)),
    )


class Errors(commands.Cog):
    """Global command error handler that reports failures as embeds."""

    def __init__(self, bot):
        self.bot = bot
        bot.on_command_error = self._on_command_error

    async def _on_command_error(self, ctx, error, bypass=False):
        # Parenthesise the whole "command handles its own error" test so that
        # bypass=True always forces the global handler to run; without the
        # parens `and not bypass` bound only to the second operand and a command
        # with its own on_error could never be bypassed.
        if (
            hasattr(ctx.command, "on_error")
            or (ctx.command and hasattr(ctx.cog, f"_{ctx.command.cog_name}__error"))
        ) and not bypass:
            return

        # A hybrid command invoked as a slash re-wraps every app_commands
        # failure as HybridCommandError -> the app error (discord.py
        # ext/commands/hybrid.py). app_commands errors do not derive from
        # commands.CommandError, so an unpeeled one matches no branch below and
        # lands in the final else: logged ERROR "Unhandled" and told to report a
        # bug. Translate each shape to its ext equivalent so a slash invocation
        # takes the exact same branch as the prefix one. Shapes with no ext
        # counterpart (CommandSignatureMismatch, TranslationError...) are left
        # intact on purpose: those really are bugs and belong in the else.
        if isinstance(error, commands.HybridCommandError):
            app_error = error.original

            if isinstance(app_error, discord.app_commands.CommandInvokeError):
                # A runtime crash: same error_id + logged traceback as a prefix
                # invocation.
                inner = app_error.original
                error = (
                    inner
                    if isinstance(inner, commands.CommandError)
                    else commands.CommandInvokeError(inner)
                )

            elif isinstance(app_error, discord.app_commands.CommandOnCooldown):
                # Tested before CheckFailure, which it subclasses: only the
                # cooldown branch states the remaining time, where the generic
                # refusal wording would be plainly wrong.
                error = commands.CommandOnCooldown(
                    commands.Cooldown(
                        app_error.cooldown.rate, app_error.cooldown.per
                    ),
                    app_error.retry_after,
                    commands.BucketType.default,
                )
                error.__cause__ = app_error

            elif isinstance(app_error, discord.app_commands.NoPrivateMessage):
                # Mapped before the generic CheckFailure it subclasses: the
                # handler has a dedicated DM branch below ("use this in a
                # server, here is the invite"), and flattening would replace
                # that helpful wording with a bare permission refusal.
                error = commands.NoPrivateMessage(str(app_error))
                error.__cause__ = app_error

            elif isinstance(app_error, discord.app_commands.BotMissingPermissions):
                # Same reason: the ext branch names the permissions I lack,
                # which reads as "I am missing X" instead of telling the user
                # THEY have no permission. missing_permissions is a list[str]
                # on both the app and the ext exception, so it carries over
                # verbatim into the ext constructor.
                error = commands.BotMissingPermissions(
                    app_error.missing_permissions
                )
                error.__cause__ = app_error

            elif isinstance(app_error, discord.app_commands.CheckFailure):
                # A deliberate refusal from an @app_commands.check (and the
                # MissingPermissions / MissingRole shapes that subclass it and
                # have no dedicated branch): the short discreet reply, never a
                # crash report asking the user to file a bug.
                error = commands.CheckFailure(str(app_error))
                error.__cause__ = app_error

            elif isinstance(app_error, discord.app_commands.TransformerError):
                # The user typed a value a Transformer could not convert. Its
                # message ("Failed to convert X to Y") is user-facing, so reuse
                # the BadArgument branch. discord.py already unwraps the case
                # where the cause is a CommandError, so this is a genuine input
                # error, not a crash.
                error = commands.BadArgument(str(app_error))
                error.__cause__ = app_error

        if isinstance(error, commands.CommandNotFound):
            # "?????" typed as ordinary punctuation parses as prefix +
            # invoked_with "????": nothing alphanumeric means it was never a
            # command attempt, so stay silent. Safe to check before the
            # custom-command dispatch because command_naming rejects any stored
            # name that does not start with an alphanumeric character.
            invoked = ctx.invoked_with
            if not invoked or not any(c.isalnum() for c in invoked):
                log.debug("Ignoring non-command invocation %r", invoked)
                return

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
            await ctx.send(embed=_generic_report(ctx, error_id))

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

        elif isinstance(error, commands.CheckFailure):
            # NotOwner / MissingRole / NSFWChannelRequired / CheckAnyFailure and
            # the like are deliberate refusals, not crashes. The specific check
            # failures above keep their own wording; the rest get one short,
            # discreet reply. Never the alarming "report this to the bot owner"
            # text, which would ask users to file a bug for a permission denial.
            await ctx.send(
                _("You do not have permission to do that"), delete_after=10
            )

        else:
            # Any command error that matched no branch above (including a
            # HybridCommandError shape we could not unwrap). Log the full
            # traceback BEFORE attempting a reply: ctx.send can itself fail (an
            # expired slash interaction, missing permissions) and the traceback
            # of an otherwise-unhandled error must never be lost. Pass exc_info
            # explicitly because on_command_error runs outside the except block,
            # so there is no active exception for log.exception to capture.
            error_id = secrets.token_hex(4)
            log.error(
                "Unhandled command error "
                "[error_id=%s type=%s command=%s user=%s guild=%s]",
                error_id,
                type(error).__name__,
                getattr(ctx.command, "qualified_name", None),
                ctx.author.id,
                ctx.guild.id if ctx.guild else None,
                exc_info=(type(error), error, error.__traceback__),
            )
            await ctx.send(embed=_generic_report(ctx, error_id))


async def setup(bot):
    await bot.add_cog(Errors(bot))
