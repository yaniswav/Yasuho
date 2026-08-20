import datetime
import logging
import time
import urllib.parse

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.http import TIMEOUT, get_session
from tools.i18n import _
from tools.interactions import notify_failure
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Native Discord poll limits (shared by the text path and the modal path).
QUICKPOLL_MAX_QUESTION = 300
QUICKPOLL_MAX_OPTION = 55
QUICKPOLL_MIN_OPTIONS = 2
QUICKPOLL_MAX_OPTIONS = 10


def build_quickpoll(question, options):
    """Validate inputs and build a single-choice 24h poll.

    Returns ``(poll, None)`` on success or ``(None, error_message)`` when the
    inputs are rejected, so both the pipe-delimited text path and the modal path
    can share the exact same validation and poll-building logic.
    """

    question = (question or "").strip()
    options = [o for o in (options or []) if o]

    if not question:
        return None, _(
            "Give a question and options: `quickpoll question | option 1 | option 2`"
        )
    if len(options) < QUICKPOLL_MIN_OPTIONS:
        return None, _("A poll needs at least two options.")
    if len(options) > QUICKPOLL_MAX_OPTIONS:
        return None, _("A poll can have at most 10 options.")
    if len(question) > QUICKPOLL_MAX_QUESTION:
        return None, _("The question must be 300 characters or fewer.")
    if any(len(option) > QUICKPOLL_MAX_OPTION for option in options):
        return None, _("Each option must be 55 characters or fewer.")

    poll = discord.Poll(question=question, duration=datetime.timedelta(hours=24))
    for option in options:
        poll.add_answer(text=option)
    return poll, None


class QuickPollModal(LocaleModal):
    """Form that collects a question plus one option per line, then sends a poll."""

    def __init__(self):
        super().__init__(title=_("Create a poll"))
        self.question_input = discord.ui.TextInput(
            label=_("Question"),
            style=discord.TextStyle.short,
            max_length=QUICKPOLL_MAX_QUESTION,
            required=True,
        )
        self.add_item(self.question_input)
        self.options_input = discord.ui.TextInput(
            label=_("Options (one per line, 2 to 10)"),
            style=discord.TextStyle.paragraph,
            required=True,
        )
        self.add_item(self.options_input)

    async def on_submit(self, interaction):
        options = [
            line.strip() for line in (self.options_input.value or "").splitlines()
        ]
        poll, error = build_quickpoll(self.question_input.value, options)
        if error:
            return await notify_failure(interaction, error)

        try:
            await interaction.response.send_message(poll=poll)
        except (discord.HTTPException, ValueError):
            log.exception("Failed to send native quickpoll (modal)")
            await notify_failure(interaction, _("I could not create that poll here."))


class QuickPollLauncher(AuthorView):
    """Author-gated view whose button opens the poll-creation modal."""

    def __init__(self, author_id):
        super().__init__(
            author_id, timeout=180, deny_message="This prompt isn't for you."
        )
        button = discord.ui.Button(
            label=_("Create poll"), style=discord.ButtonStyle.primary
        )
        button.callback = self._launch
        self.add_item(button)

    async def _launch(self, interaction):
        await interaction.response.send_modal(QuickPollModal())


# Keep a deleted message snipeable for this long, then forget it: bounds both
# the memory the snipe cache holds and how long deleted-message content lingers.
_SNIPE_TTL = 15 * 60
_SNIPE_SWEEP_AT = 500

# --------------------------------------------------------------------------
# THE MARKER: message ids THIS BOT deleted, as {message_id: monotonic expiry}.
#
# Snipe used to re-publish ANY deletion, which handed every member an undo
# button for the bot's own moderation: post an invite, let AutoMod delete it,
# type ?snipe and the invite is back in the channel - now inside an embed the
# bot itself posted. The same held for `?purge 1` and, worse, for the AniList
# flow that deletes a member's message BECAUSE it contains an API token.
#
# MESSAGE_DELETE does not say who deleted a message, so the answer has to be
# recorded on the way out. Every bot-side deletion in this process - AutoMod's
# `message.delete()`, `purge`, `delete_after=`, the token cleanup, the music
# controller's own tidying - funnels through exactly ONE call,
# `HTTPClient.delete_message`, so wrapping that one call marks all of them for
# one dict store apiece and couples this cog to no other cog. Re-running
# AutoMod's own verdict here was the alternative and it is strictly worse: it
# reads per-guild settings on every deletion, it cannot see a spam deletion at
# all (that verdict is rate-based, not content-based), and it knows nothing
# about the non-AutoMod deletions above.
#
# The window only has to outlive the round trip from our DELETE to the gateway
# event it produces; two minutes is generous for that and keeps the map small.
# --------------------------------------------------------------------------
_BOT_DELETED_TTL = 120
_BOT_DELETED_SWEEP_AT = 512
_bot_deleted = {}


def mark_bot_deleted(message_id, *, now=None):
    """Record that this process is deleting ``message_id`` right now."""
    now = time.monotonic() if now is None else now
    if len(_bot_deleted) > _BOT_DELETED_SWEEP_AT:
        for mid in [mid for mid, until in _bot_deleted.items() if until < now]:
            del _bot_deleted[mid]
    _bot_deleted[int(message_id)] = now + _BOT_DELETED_TTL


def take_bot_deleted(message_id, *, now=None):
    """True when this bot deleted that message. CONSUMES the marker.

    Consuming it is what keeps the map bounded in the common case: every mark
    is followed by the gateway event it predicts, so the entry is claimed rather
    than left to age out. The TTL is only for the deletions that never land (a
    403 on our side, a message somebody else removed first).
    """
    until = _bot_deleted.pop(int(message_id), None)
    if until is None:
        return False
    return until >= (time.monotonic() if now is None else now)


def install_bot_delete_marker(bot):
    """Wrap the one HTTP call every bot-side message deletion goes through.

    Returns the original callable so :meth:`Utility.cog_unload` can put it back,
    or ``None`` when there was nothing to wrap (already installed, or the
    library seam moved - which test_snipe_moderation pins so an upgrade breaks
    loudly rather than quietly re-opening the hole).
    """
    http = getattr(bot, "http", None)
    if http is None:
        return None
    if getattr(http, "_yasuho_snipe_marker", False):
        return None
    original = getattr(http, "delete_message", None)
    if original is None:
        log.warning(
            "snipe: no delete_message seam on the HTTP client, so this bot's "
            "own moderation deletions cannot be filtered out of ?snipe"
        )
        return None

    async def delete_message(*args, **kwargs):
        # MARK FIRST, delete second. The gateway can deliver MESSAGE_DELETE to
        # on_message_delete before this await returns - the two run on the same
        # loop and nothing orders them - so marking afterwards would race the
        # very event the marker exists to filter.
        try:
            message_id = args[1] if len(args) > 1 else kwargs["message_id"]
            mark_bot_deleted(message_id)
        except Exception:
            # A marker is a nicety; the deletion is not. Never let this stop it.
            log.warning("snipe: could not mark a bot deletion", exc_info=True)
        return await original(*args, **kwargs)

    http.delete_message = delete_message
    http._yasuho_snipe_marker = True
    return original


def remove_bot_delete_marker(bot, original):
    """Put the library's own method back (cog reload / unload)."""
    http = getattr(bot, "http", None)
    if http is None or original is None:
        return
    try:
        # ``original`` is the bound library method, so re-binding it as an
        # instance attribute behaves identically to the class attribute it
        # shadows; the flag is what stops a reload from wrapping twice.
        http.delete_message = original
        http._yasuho_snipe_marker = False
    except Exception:
        log.warning("snipe: could not restore the delete hook", exc_info=True)


class Utility(commands.Cog):
    """Handy utility commands."""

    def __init__(self, bot):
        self.bot = bot
        # channel_id -> (content, author, created_at, monotonic_expiry)
        self._snipes = {}
        self._delete_marker = install_bot_delete_marker(bot)

    def cog_unload(self):
        remove_bot_delete_marker(self.bot, self._delete_marker)
        self._delete_marker = None

    def _sweep_snipes(self, now):
        self._snipes = {
            cid: v for cid, v in self._snipes.items() if v[3] >= now
        }

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.content:
            return
        if take_bot_deleted(message.id):
            # WE deleted it. In this codebase that only ever happens for a
            # reason - a moderation action, or a message that had to go because
            # of what it contained - so it must not be handed back to the room.
            # Dropped at CACHE time, not at snipe time: a message the bot
            # removed should not sit in memory waiting to be asked for.
            return
        now = time.monotonic()
        self._snipes[message.channel.id] = (
            message.content,
            message.author,
            message.created_at,
            now + _SNIPE_TTL,
        )
        if len(self._snipes) > _SNIPE_SWEEP_AT:
            self._sweep_snipes(now)

    @commands.hybrid_command()
    @commands.guild_only()
    async def snipe(self, ctx):
        """Show the last deleted message in this channel."""

        # Channel scoping is the cache key itself: a deletion is only ever
        # stored under the channel it happened in and only ever read back under
        # the channel the command was invoked in, so a member can never pull a
        # message out of a room they are not currently standing in. Nothing
        # crosses guilds, and threads are their own channel id.
        data = self._snipes.get(ctx.channel.id)
        if not data or data[3] < time.monotonic():
            self._snipes.pop(ctx.channel.id, None)
            return await ctx.send(_("Nothing to snipe."))

        content, author, when, _expiry = data
        embed = discord.Embed(
            description=content,
            colour=random_colour(),
            timestamp=when,
        )
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        # Belt and braces on top of "embeds do not resolve mentions": the body
        # is somebody else's raw text being replayed by the bot, so nothing in
        # it may ping, whatever Discord decides to do with embed content later.
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------
    # /poll - the polls group.
    #
    # This is the cog that actually broke: Discord caps a bot at 100 GLOBAL
    # top-level application commands, the tree had reached 101, and whichever
    # cog loaded last died with CommandLimitReached. It was this one, so /poll,
    # /quickpoll, /snipe and /translate were all missing from production.
    # Pairing the two poll commands under one group frees a slot; the bulk of
    # the headroom comes from the folds in the sibling cogs.
    #
    # HybridGroup always forces invoke_without_command, and an unrecognised
    # first word rewinds the parser, so `?poll Is this a good idea?` still
    # creates the yes/no poll it always did. The one edge case is a question
    # whose FIRST word is exactly a subcommand name (`?poll multi ...`), which
    # now routes to that subcommand - the same trade every group in this bot
    # already makes (see /playlist).
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="poll")
    async def poll(self, ctx, *, question: str = None):
        """Create a poll: a quick yes/no, or a multiple-choice one."""

        if ctx.invoked_subcommand is not None:
            return
        if question is None:
            return await ctx.send(
                _(
                    "Create a poll with one of these:\n"
                    "- `{p}poll <question>` - a yes/no poll\n"
                    "- `{p}poll multi question | option 1 | option 2` - "
                    "multiple choice (no options opens a form)"
                ).format(p=ctx.clean_prefix)
            )
        await self.poll_yesno(ctx, question=question)

    @poll.command(name="yesno")
    @discord.app_commands.describe(question="The yes/no question to ask.")
    async def poll_yesno(self, ctx, *, question: str):
        """Create a native yes/no poll (runs for 24 hours)."""

        question = question.strip()
        if not question:
            return await ctx.send(_("Please give a question to ask."))
        if len(question) > 300:
            return await ctx.send(
                _("The poll question must be 300 characters or fewer.")
            )

        poll = discord.Poll(question=question, duration=datetime.timedelta(hours=24))
        poll.add_answer(text=_("Yes"), emoji="\U0001F44D")
        poll.add_answer(text=_("No"), emoji="\U0001F44E")

        try:
            await ctx.send(poll=poll)
        except (discord.HTTPException, ValueError):
            log.exception("Failed to send native poll")
            await ctx.send(_("I could not create that poll here."))

    @poll.command(name="multi")
    @discord.app_commands.describe(
        args="question | option 1 | option 2 ... (blank opens a form)."
    )
    async def poll_multi(self, ctx, *, args: str = None):
        """Multiple-choice poll: quickpoll question | option 1 | option 2 ... (no args opens a form)."""

        # Interactive path: no args opens the modal (slash) or offers a button
        # (prefix, where there is no interaction to attach a modal to).
        if not args or not args.strip():
            if ctx.interaction is not None:
                return await ctx.interaction.response.send_modal(QuickPollModal())
            view = QuickPollLauncher(ctx.author.id)
            view.message = await ctx.send(
                _("Click the button below to build a poll."), view=view
            )
            return

        # Fallback text path: pipe-delimited "question | option 1 | option 2".
        parts = [p.strip() for p in args.split("|")]
        poll, error = build_quickpoll(parts[0], parts[1:])
        if error:
            return await ctx.send(error)

        try:
            await ctx.send(poll=poll)
        except (discord.HTTPException, ValueError):
            log.exception("Failed to send native quickpoll")
            await ctx.send(_("I could not create that poll here."))

    # Prefix compatibility shim for the command folded into /poll. Prefix-only
    # (commands.command, never hybrid), so it registers no application command
    # and costs none of the 100 global slash slots; `?quickpoll ...` behaves
    # exactly as it always has, and ?help still lists it.
    @commands.command(name="quickpoll")
    async def quickpoll_prefix(self, ctx, *, args: str = None):
        """Multiple-choice poll: quickpoll question | option 1 | option 2 ... (no args opens a form)."""

        await self.poll_multi(ctx, args=args)

    @commands.hybrid_command(aliases=["tr"])
    @discord.app_commands.describe(text="The text to translate.")
    async def translate(self, ctx, *, text: str):
        """Translate text to English (auto-detect source language)."""

        async with ctx.typing():
            try:
                url = (
                    "https://translate.googleapis.com/translate_a/single"
                    "?client=gtx&sl=auto&tl=en&dt=t&q="
                    + urllib.parse.quote(text)
                )
                async with get_session(self.bot).get(
                    url, timeout=TIMEOUT
                ) as r:
                    data = await r.json()

                translated = "".join(seg[0] for seg in data[0])
                embed = discord.Embed(
                    description=translated,
                    colour=random_colour(),
                )
                embed.set_footer(text=_("auto -> en (unofficial)"))
                await ctx.send(embed=embed)

            except Exception:
                log.exception("translation failed")
                await ctx.send(_("Translation failed."))


async def setup(bot):
    await bot.add_cog(Utility(bot))
