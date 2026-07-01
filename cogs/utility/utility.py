import datetime
import logging
import urllib.parse

import aiohttp
import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.http import TIMEOUT
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


class Utility(commands.Cog):
    """Handy utility commands."""

    def __init__(self, bot):
        self.bot = bot
        self._snipes = {}

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.content:
            return
        self._snipes[message.channel.id] = (
            message.content,
            message.author,
            message.created_at,
        )

    @commands.hybrid_command()
    @commands.guild_only()
    async def snipe(self, ctx):
        """Show the last deleted message in this channel."""

        data = self._snipes.get(ctx.channel.id)
        if not data:
            return await ctx.send(_("Nothing to snipe."))

        content, author, when = data
        embed = discord.Embed(
            description=content,
            colour=random_colour(),
            timestamp=when,
        )
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    async def poll(self, ctx, *, question: str):
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

    @commands.hybrid_command()
    async def quickpoll(self, ctx, *, args: str = None):
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

    @commands.hybrid_command()
    async def translate(self, ctx, *, text: str):
        """Translate text to English (auto-detect source language)."""

        async with ctx.typing():
            try:
                url = (
                    "https://translate.googleapis.com/translate_a/single"
                    "?client=gtx&sl=auto&tl=en&dt=t&q="
                    + urllib.parse.quote(text)
                )
                async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                    async with s.get(url) as r:
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
