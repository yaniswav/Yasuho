import logging
import secrets
from datetime import timedelta

import discord
import Levenshtein as lv
from discord.ext import commands

from . import arg_completion
from tools import interactions
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


# How long a CHANNEL fallback message survives when the caller asked for no
# deletion. The interaction reply the user expected was ephemeral or scoped to
# their invocation; the fallback is a plain public message in a possibly busy
# channel, so it is not free. Two minutes is long enough to notice it and copy
# an 8-hex-character error_id, short enough that the channel is not littered
# with stale crash embeds. Nothing diagnosable is lost when it disappears: the
# error_id is written to the log BEFORE any send is attempted (see the
# CommandInvokeError branch and the final else).
#
# It is a FLOOR, not just a default: a branch's own delete_after was chosen for
# the reply the user was watching for (the Forbidden branch says "I need more
# permissions!" and clears it after 3 seconds, because on the interaction it
# appears exactly where the user is looking). The channel fallback is the
# opposite situation - the user is not looking, which is why it had to be sent
# at all - so a 3-second life would delete the rescue before it is read. The
# caller may ask for LONGER; it may not ask for shorter than the time it takes
# to notice a message and copy an error_id out of it.
_FALLBACK_DELETE_AFTER = 120.0


def _fallback_delete_after(delete_after):
    """How long the CHANNEL fallback lives: the caller's wish, floored."""

    if delete_after is None:
        return _FALLBACK_DELETE_AFTER
    return max(delete_after, _FALLBACK_DELETE_AFTER)


def _triage_fields(ctx):
    """``(command, user, guild)`` for a log line, fetched defensively.

    These lines are the last thing standing between a silent user and a
    diagnosable incident, so a half-built Context (``ctx.command`` is None on
    CommandNotFound, and a Context can reach the handler before every attribute
    is populated) must not turn one into an AttributeError and hand the caller
    the raise ``_safe_send`` exists to prevent.
    """

    return (
        getattr(getattr(ctx, "command", None), "qualified_name", None),
        getattr(getattr(ctx, "author", None), "id", None),
        getattr(getattr(ctx, "guild", None), "id", None),
    )


def _interaction_transport(ctx):
    """Return the interaction ``ctx.send`` would use, or None if it would not.

    discord.py's ``Context.send`` (2.7, ext/commands/context.py) begins with
    ``if self.interaction is None or self.interaction.is_expired(): return await
    super().send(...)`` - i.e. it posts a plain CHANNEL message. Knowing which
    transport it picked is what makes the channel rung meaningful: retrying the
    channel after a channel send just failed would be the identical request (and
    could double-post), while retrying the channel after the INTERACTION webhook
    failed is a genuinely different route to the same user.

    Defensive on purpose: a broken/mocked interaction whose ``is_expired`` raises
    is treated as live, because the cost of guessing wrong that way is one extra
    channel attempt, whereas guessing the other way is silence.
    """
    interaction = getattr(ctx, "interaction", None)
    if interaction is None:
        return None
    try:
        expired = bool(interaction.is_expired())
    except Exception:
        expired = False
    return None if expired else interaction


def _is_acknowledged(interaction):
    """True when discord.py believes this interaction was already responded to.

    ``InteractionResponse.is_done()`` is ``self._response_type is not None``, and
    ``send_message`` assigns ``_response_type`` only AFTER the HTTP call returns
    (discord.py 2.7, interactions.py). That makes a False -> True transition a
    reliable LOCAL proof that a response actually landed, which is how the helper
    below tells "the reply never reached the user" from "the reply reached the
    user and only the post-send bookkeeping failed".
    """
    if interaction is None:
        return False
    try:
        return bool(interaction.response.is_done())
    except Exception:
        return False


def _is_acknowledged_race(exc):
    """True when a send failed because the interaction was ALREADY answered.

    Two shapes, both meaning "use a followup instead":

    * ``discord.InteractionResponded`` - raised locally by discord.py when
      ``_response_type`` is already set. It subclasses ``ClientException``, NOT
      ``HTTPException``, so a ``except discord.HTTPException`` would miss it.
    * an HTTP error with code 40060 ("Interaction has already been
      acknowledged") - the API saw an ack that discord.py's purely local
      ``is_done()`` never recorded (a defer or response sent from another task,
      a view, or a hybrid pre-hook).
    """
    if isinstance(exc, discord.InteractionResponded):
        return True
    return isinstance(exc, discord.HTTPException) and getattr(exc, "code", None) == 40060


def _mention_prefixed(ctx, content):
    """The channel-fallback content: the invoker's mention, then the message.

    Built by runtime concatenation, not a new translatable string: gluing a
    mention onto an already-translated sentence carries no words of its own.
    """
    mention = getattr(getattr(ctx, "author", None), "mention", None)
    if not mention:
        return content
    return f"{mention} {content}" if content else mention


def _invoker_only_mentions(ctx):
    """Allow exactly one ping - the invoker - and nothing else.

    The embed can carry attacker-chosen text (a bad argument, a guild name), and
    embeds never ping; the content only ever holds our own mention. Pinning the
    allow-list to the single author object keeps that true even if a future
    branch interpolates something into the content.
    """
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[ctx.author],
        replied_user=False,
    )


async def _safe_send(
    ctx,
    content=None,
    *,
    embed=None,
    delete_after=None,
    surface="error-report",
    error_id=None,
):
    """Deliver one error report. NEVER raises. The only send in this module.

    Prod incident 2026-08-25 03:23:03: ``/anilist login`` failed with
    ``NotFound: 404 (error code 10062): Unknown interaction`` (the interaction
    token had died on the 3-second initial-response deadline). The handler logged
    it correctly with an error_id, then called ``ctx.send`` on that SAME dead
    interaction to tell the user - which raised 10062 again. discord.py logged
    "Ignoring exception in on_command_error" and the user got NOTHING, not even
    an error message. Every branch of this handler had that shape.

    Why ``ctx.send`` alone cannot be trusted: its liveness test is
    ``interaction.is_expired()``, which is ``utcnow() >= created_at + 15 minutes``
    - a purely local clock check. The 10062 class of death is the 3-SECOND
    initial-response deadline (and any server-side token invalidation), which
    that check cannot see. So ``ctx.send`` confidently posts to a token Discord
    has already thrown away.

    The ladder, in order:

    1. **Interaction live and unacknowledged -> respond normally.** Delegated to
       ``ctx.send``, which owns that fork (``response.send_message``) plus the
       ephemeral/``delete_after`` mechanics. Trying it first also keeps the reply
       attached to the user's invocation.
    2. **Interaction already acknowledged -> followup.** ``ctx.send`` covers the
       case discord.py knows about locally (``is_done()`` -> ``followup.send``).
       Rung 2 covers the case it does NOT: the API answers 40060 (or discord.py
       raises ``InteractionResponded``) because something else acked the
       interaction. Then the followup webhook is the right transport, and the
       retry is issued explicitly here.
    3. **Interaction dead/expired -> the CHANNEL.** A plain message via
       ``ctx.channel.send``. Grounded: for a slash-invoked hybrid,
       ``Context.from_interaction`` synthesises the message around
       ``interaction.channel or PartialMessageable(id=interaction.channel_id)``,
       so ``ctx.channel`` is a real, sendable channel that is not bound to the
       interaction token at all. The user is still standing in that channel even
       when the token is gone. Observed dead-token shapes: 10062 (Unknown
       interaction), 10015 (Unknown Webhook), 50027 (Invalid Webhook Token).
       Skipped when ``ctx.send`` already went to the channel (rung 1 was a
       channel send), because repeating it would be the same failing request.
    4. **Channel refuses -> WARNING, then give up silently.** Forbidden (403: no
       send_messages / no embed_links), NotFound (404: channel deleted), a rate
       limit, anything. Giving up is the correct last rung: an error reporter
       must never become the error. The WARNING carries the surface, the
       error_id, the command, the user and the guild so a silent user report is
       still traceable.

    Rungs 1 and 2 honour an EPHEMERAL flow: a command that acknowledged with
    ``interactions.defer_ephemeral`` (``/anilist login``, ``/anilist code``) gets
    an ephemeral crash report too, because ``Context.send`` would otherwise
    default it to a public message inside a flow the command kept private. Rung
    3 cannot be ephemeral - a channel message has no such thing - and stays
    public on purpose: at that point the token is dead, the channel is the only
    route left, and silence is the failure this whole ladder exists to prevent.
    What it carries is safe to be seen: an error_id and the command's usage
    signature, never the arguments the user typed.

    The fallback channel message DOES mention the invoker: a slash invocation is
    invisible to bystanders, so an unaddressed embed would read as noise to the
    channel and might not be noticed by the one person it is for. Mentions are
    restricted to that single user (never @everyone, never roles), so no
    attacker-chosen text in an error string can turn a crash report into a ping.

    Catches ``Exception`` rather than ``discord.HTTPException`` at every rung
    because the failures that matter are not all HTTPExceptions:
    ``discord.RateLimited`` subclasses ``DiscordException`` only,
    ``InteractionResponded`` subclasses ``ClientException``, and an aiohttp
    connection error can surface raw. ``asyncio.CancelledError`` is a
    ``BaseException`` on 3.8+, so shutdown still propagates untouched.
    """
    interaction = _interaction_transport(ctx)
    acknowledged = _is_acknowledged(interaction)

    # Only pass the kwargs the caller actually set: a branch that sends plain
    # text must not start carrying `embed=None`, which some send implementations
    # read as "clear the embeds" rather than "no embed given".
    args = () if content is None else (content,)
    embed_kwargs = {} if embed is None else {"embed": embed}
    kwargs = dict(embed_kwargs)
    if delete_after is not None:
        kwargs["delete_after"] = delete_after
    # Match the command's own privacy on the interaction rungs. `Context.send`
    # forwards `ephemeral` (default False) on every interaction path, so without
    # this a crash inside an ephemerally-deferred command - `/anilist login`,
    # `/anilist code` - would answer PUBLICLY on a flow the command deliberately
    # kept private. Only added when a command actually marked the flow (see
    # tools.interactions.defer_ephemeral), so every other call shape is
    # byte-identical to before.
    private = interactions.prefers_ephemeral(ctx)
    if private:
        kwargs["ephemeral"] = True

    # Rung 1 + the half of rung 2 discord.py already knows about.
    try:
        await ctx.send(*args, **kwargs)
        return
    except Exception as exc:
        failure = exc
        log.debug("Error report: primary reply failed on %s", surface, exc_info=True)

    if interaction is not None:
        if not acknowledged and _is_acknowledged_race(failure):
            # Rung 2: something else acked this interaction behind discord.py's
            # back. The initial-response slot is spent; the followup webhook is
            # not. (Webhook.send has no delete_after, so that hint is dropped
            # here rather than faked.)
            try:
                await interaction.followup.send(
                    *args, **embed_kwargs, **({"ephemeral": True} if private else {})
                )
                return
            except Exception as exc:
                failure = exc
                log.debug(
                    "Error report: followup retry failed on %s", surface, exc_info=True
                )

        elif not acknowledged and _is_acknowledged(interaction):
            # is_done() flipped False -> True across our own send, which only
            # happens after a successful create_interaction_response. The
            # overwhelmingly likely reading is: the response WAS accepted and
            # only the post-send bookkeeping - original_response(), the
            # delete_after scheduling - blew up. The user already has the
            # report, so a channel fallback here would post a public duplicate.
            #
            # It is not PROOF, though: another task could have acked this
            # interaction while our own send failed for an unrelated reason (a
            # raw transport error rather than the 40060 that rung 2 catches),
            # and then nobody told the user anything. The two are
            # indistinguishable from here, so the message is not retried (a
            # duplicate crash embed is a real cost, on the far likelier branch)
            # but it is NOT swallowed silently either: this is the one exit that
            # neither delivers nor warns, and the error_id bridge - "the user
            # reports seeing nothing, find the traceback" - is the whole point
            # of the ladder. So it warns, with the id.
            log.warning(
                "Error report ambiguous: interaction acked mid-send, not retried "
                "[surface=%s error_id=%s command=%s user=%s guild=%s]",
                surface,
                error_id,
                *_triage_fields(ctx),
                exc_info=failure,
            )
            return

        # Rung 3: the interaction token is unusable, the channel is not.
        try:
            await ctx.channel.send(
                _mention_prefixed(ctx, content),
                **embed_kwargs,
                delete_after=_fallback_delete_after(delete_after),
                allowed_mentions=_invoker_only_mentions(ctx),
            )
            return
        except Exception as exc:
            failure = exc
            log.debug(
                "Error report: channel fallback failed on %s", surface, exc_info=True
            )

    # Rung 4: out of transports. Say so once, loudly enough to be found.
    # Every field is fetched defensively (see _triage_fields).
    log.warning(
        "Error report undeliverable [surface=%s error_id=%s command=%s user=%s guild=%s]",
        surface,
        error_id,
        *_triage_fields(ctx),
        exc_info=failure,
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
            # No built-in or custom command can START with a non-alphanumeric
            # character (command_naming's _NAME_RE requires an alnum first
            # char, pinned by test), so a non-alnum first character was never a
            # command attempt: "?????" typed as punctuation parses as prefix +
            # invoked_with "????", and "??play" aimed at ANOTHER bot with a
            # longer prefix ("??", "?!") on a server where ours is "?" parses
            # as prefix + "?play". Stay silent, before the custom-command
            # dispatch, instead of replying "Invalid command" at users talking
            # to the other bot.
            invoked = ctx.invoked_with
            if not invoked or not invoked[0].isalnum():
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
                suggestions = " | ".join(
                    str(command)
                    for command in self.bot.commands
                    if lv.distance(ctx.invoked_with, command.name) < 4
                    and not command.hidden
                )
                # Nothing close enough to suggest: a reply would carry no
                # information ("no similar commands found"), so stay silent.
                if not suggestions:
                    log.debug("No suggestion for unknown command %r", invoked)
                    return
                await _safe_send(
                    ctx,
                    embed=_error_embed(
                        ctx,
                        _("**Invalid command entered. Did you mean:**"),
                        f"`{suggestions}`",
                    ),
                    delete_after=10,
                    surface="command-not-found",
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

            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you're missing a required argument:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                ),
                surface="missing-required-argument",
            )

        elif isinstance(error, commands.BadArgument):
            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you gave me a bad argument:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                ),
                surface="bad-argument",
            )

        elif isinstance(error, commands.CommandOnCooldown):

            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you are on cooldown:**"),
                    _(":hourglass: Remaining time: `{time}`\n{usage}").format(
                        time=timedelta(seconds=int(error.retry_after)),
                        usage=_usage(ctx),
                    ),
                ),
                delete_after=60,
                surface="cooldown",
            )

        elif isinstance(error, discord.Forbidden):
            await _safe_send(
                ctx,
                _("I need more permissions!"),
                delete_after=3,
                surface="forbidden",
            )

        elif isinstance(error, commands.NoPrivateMessage):
            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you can't use this command in private messages:**"),
                    _(
                        "Go in a guild where I am or invite me in your server\n"
                        "invite.yasuho.xyz"
                    ),
                ),
                surface="no-private-message",
            )

        elif isinstance(error, discord.HTTPException):
            pass

        elif isinstance(error, commands.TooManyArguments):
            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you gave me too many arguments:**"),
                    _(
                        ":question: What to do: `look at {prefix}help and try being "
                        "more specific`\n{usage}"
                    ).format(prefix=ctx.prefix, usage=_usage(ctx)),
                ),
                surface="too-many-arguments",
            )

        elif isinstance(error, commands.UserInputError):
            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you did something wrong:**"),
                    _(
                        ":question: What to do: `look at {prefix}help and try being "
                        "more specific`\n{usage}"
                    ).format(prefix=ctx.prefix, usage=_usage(ctx)),
                ),
                surface="user-input-error",
            )

        elif isinstance(error, commands.MissingPermissions):
            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like you are missing permissions:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                ),
                surface="missing-permissions",
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
            # The error_id is in the log ABOVE this line, so it survives even
            # when every rung of _safe_send fails and the user sees nothing.
            await _safe_send(
                ctx,
                embed=_generic_report(ctx, error_id),
                surface="command-invoke-error",
                error_id=error_id,
            )

        elif isinstance(error, commands.BotMissingPermissions):
            await _safe_send(
                ctx,
                embed=_error_embed(
                    ctx,
                    _("**Seems like I am missing permissions:**"),
                    _(":warning: Error: `{error}`\n{usage}").format(
                        error=error, usage=_usage(ctx)
                    ),
                ),
                surface="bot-missing-permissions",
            )

        elif isinstance(error, commands.CheckFailure):
            # NotOwner / MissingRole / NSFWChannelRequired / CheckAnyFailure and
            # the like are deliberate refusals, not crashes. The specific check
            # failures above keep their own wording; the rest get one short,
            # discreet reply. Never the alarming "report this to the bot owner"
            # text, which would ask users to file a bug for a permission denial.
            await _safe_send(
                ctx,
                _("You do not have permission to do that"),
                delete_after=10,
                surface="check-failure",
            )

        else:
            # Any command error that matched no branch above (including a
            # HybridCommandError shape we could not unwrap). Log the full
            # traceback BEFORE attempting a reply: every rung of _safe_send can
            # fail (an expired slash interaction, missing permissions) and the
            # traceback of an otherwise-unhandled error - along with the
            # error_id it is keyed by - must never be lost. Pass exc_info
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
            await _safe_send(
                ctx,
                embed=_generic_report(ctx, error_id),
                surface="unhandled",
                error_id=error_id,
            )


async def setup(bot):
    await bot.add_cog(Errors(bot))
