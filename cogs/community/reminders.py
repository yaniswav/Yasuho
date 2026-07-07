import asyncio
import datetime
import json
import logging

import discord
from discord.ext import commands

from tools import modchecks
from tools.i18n import _
from tools.time import (
    FutureTime,
    ShortTime,
    UserFriendlyTime,
    human_timedelta,
)
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Fallback reminder body when the user leaves the message blank; mirrors the
# free-text command's UserFriendlyTime(default="something").
DEFAULT_REMINDER_MESSAGE = "something"


class RemindModal(LocaleModal):
    """Interactive reminder form: a short "When" and a paragraph "Message".

    The "When" field is parsed with the cog's own time parsing (ShortTime for
    relative/absolute inputs, falling back to FutureTime for natural language),
    then the same "reminder" timer row the text command creates is inserted.
    """

    def __init__(self, cog, channel_id, author_id):
        super().__init__(title=_("Set a reminder"))
        self.cog = cog
        self.channel_id = channel_id
        self.author_id = author_id

        self.when_input = discord.ui.TextInput(
            label=_("When"),
            placeholder=_("e.g. 10m, tomorrow at 6pm, in 3 days"),
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
        )
        self.add_item(self.when_input)

        self.message_input = discord.ui.TextInput(
            label=_("Message"),
            placeholder=_("What should I remind you about?"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction):
        when_raw = (self.when_input.value or "").strip()
        message = (self.message_input.value or "").strip() or _(
            DEFAULT_REMINDER_MESSAGE
        )

        tzinfo = await self.cog.get_tzinfo(interaction.user.id)
        now = interaction.created_at.astimezone(tzinfo)

        try:
            dt = ShortTime(when_raw, now=now, tzinfo=tzinfo).dt
        except commands.BadArgument:
            try:
                dt = FutureTime(when_raw, now=now, tzinfo=tzinfo).dt
            except commands.BadArgument:
                return await interaction.response.send_message(
                    _(
                        "I couldn't understand that time. Try something like "
                        "`10m`, `tomorrow at 6pm`, or `in 3 days`."
                    ),
                    ephemeral=True,
                )

        if dt <= now:
            return await interaction.response.send_message(
                _("That time is in the past. Give me a moment in the future."),
                ephemeral=True,
            )

        await self.cog.create_timer(
            dt,
            "reminder",
            author_id=self.author_id,
            channel_id=self.channel_id,
            message=message,
        )
        await interaction.response.send_message(
            _("Okay, reminding you {when}: {message}").format(
                when=discord.utils.format_dt(dt, "R"), message=message
            ),
            ephemeral=True,
        )


class RemindLauncherView(AuthorView):
    """A single button that opens the reminder modal (prefix-command path).

    Prefix invocations have no interaction to open a modal with, so the command
    posts this view and the author clicks the button to summon the modal.
    """

    def __init__(self, cog, author_id, channel_id, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.cog = cog
        self.channel_id = channel_id

        button = discord.ui.Button(
            label=_("Set a reminder"),
            style=discord.ButtonStyle.primary,
            emoji="\N{ALARM CLOCK}",
        )
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction):
        await interaction.response.send_modal(
            RemindModal(self.cog, self.channel_id, self.author_id)
        )


class Reminder(commands.Cog):
    """Reminders and time-based timers (reminders, temp-bans)."""

    def __init__(self, bot):
        self.bot = bot
        self.bot.reminder = self
        self._have_data = asyncio.Event()
        self._task = self.bot.loop.create_task(self.dispatch_timers())

    def cog_unload(self):
        self._task.cancel()
        self.bot.reminder = None

    async def get_tzinfo(self, user_id):
        return datetime.timezone.utc

    async def create_timer(self, when, event, **extra):
        row = await self.bot.db_pool.fetchrow(
            "INSERT INTO timers(event, expires, created, extra) "
            "VALUES($1, $2, $3, $4::jsonb) RETURNING id",
            event,
            when,
            datetime.datetime.now(datetime.timezone.utc),
            json.dumps(extra),
        )
        self._have_data.set()
        return row

    async def get_active_timer(self):
        return await self.bot.db_pool.fetchrow(
            "SELECT * FROM timers ORDER BY expires LIMIT 1"
        )

    async def dispatch_timers(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                self._have_data.clear()
                row = await self.get_active_timer()
                if row is None:
                    await self._have_data.wait()
                    continue

                now = datetime.datetime.now(datetime.timezone.utc)
                delta = (row["expires"] - now).total_seconds()
                if delta > 0:
                    try:
                        await asyncio.wait_for(
                            self._have_data.wait(), timeout=min(delta, 86400)
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                await self.bot.db_pool.execute(
                    "DELETE FROM timers WHERE id=$1", row["id"]
                )
                await self.call_timer(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error while dispatching timers")
                await asyncio.sleep(5)

    async def call_timer(self, row):
        extra = row["extra"]
        extra = json.loads(extra) if isinstance(extra, str) else (extra or {})
        event = row["event"]
        try:
            if event == "reminder":
                ch = self.bot.get_channel(extra["channel_id"])
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(extra["channel_id"])
                    except discord.HTTPException:
                        ch = None
                if ch:
                    await ch.send(
                        _("<@{author_id}>, {when}: {message}").format(
                            author_id=extra["author_id"],
                            when=human_timedelta(row["created"]),
                            message=extra["message"],
                        )
                    )
            elif event == "tempban":
                g = self.bot.get_guild(extra["guild_id"])
                if g:
                    await g.unban(
                        discord.Object(id=extra["user_id"]),
                        reason="Temp-ban expired",
                    )
        except Exception:
            log.exception("Error while calling timer")

    @commands.hybrid_command()
    async def remind(
        self,
        ctx,
        *,
        when: UserFriendlyTime(commands.clean_content, default="something") = None,
    ):
        """Reminds you of something after a certain amount of time."""

        # No time supplied -> offer the interactive form. Slash invocations can
        # open the modal straight away; prefix invocations have no interaction,
        # so they get a button that opens it on click.
        if when is None:
            if ctx.interaction is not None:
                return await ctx.interaction.response.send_modal(
                    RemindModal(self, ctx.channel.id, ctx.author.id)
                )
            view = RemindLauncherView(self, ctx.author.id, ctx.channel.id)
            view.message = await ctx.send(
                _("Tap the button below to set a reminder."), view=view
            )
            return

        await self.create_timer(
            when.dt,
            "reminder",
            author_id=ctx.author.id,
            channel_id=ctx.channel.id,
            message=when.arg,
        )
        await ctx.send(
            _("Okay, reminding you {when}: {message}").format(
                when=discord.utils.format_dt(when.dt, "R"), message=when.arg
            )
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx,
        member: discord.User,
        duration: ShortTime,
        *,
        reason: str = None,
    ):
        """Temporarily bans a member for the given duration."""

        err = modchecks.hierarchy_error(ctx, member)
        if err:
            return await ctx.send(err)

        try:
            await ctx.guild.ban(member, reason=reason)
        except discord.Forbidden:
            return await ctx.send(
                _("I don't have permission to ban that member.")
            )
        except discord.HTTPException:
            return await ctx.send(_("Sorry, I couldn't ban that member."))
        await self.create_timer(
            duration.dt,
            "tempban",
            guild_id=ctx.guild.id,
            user_id=member.id,
        )
        await ctx.send(
            _("Banned {member} until {time}.").format(
                member=member, time=discord.utils.format_dt(duration.dt, "F")
            )
        )


async def setup(bot):
    await bot.add_cog(Reminder(bot))
