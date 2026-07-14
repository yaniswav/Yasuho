import asyncio
import datetime
import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from tools import reminders as reminders_tool
from tools.formats import random_colour
from tools.i18n import _
from tools.time import (
    FutureTime,
    ShortTime,
    UserFriendlyTime,
    human_timedelta,
    parse_timestamp_token,
)
from tools.views import AuthorLayoutView, AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Fallback reminder body when the user leaves the message blank; mirrors the
# free-text command's UserFriendlyTime(default="something").
DEFAULT_REMINDER_MESSAGE = "something"

# Cap pending reminders per user so nobody can flood the timers table.
MAX_PENDING_REMINDERS = 25


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
            placeholder=_("e.g. 10m, tomorrow at 6pm, or a <t:...> tag"),
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

        # A pasted Discord timestamp token wins outright (UTC); otherwise fall
        # back to the existing ShortTime -> FutureTime natural-language parsing.
        dt = parse_timestamp_token(when_raw)
        if dt is None:
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
        else:
            dt = dt.astimezone(tzinfo)

        if dt <= now:
            return await interaction.response.send_message(
                _("That time is in the past. Give me a moment in the future."),
                ephemeral=True,
            )

        if (
            await self.cog._pending_reminder_count(self.author_id)
            >= MAX_PENDING_REMINDERS
        ):
            return await interaction.response.send_message(
                _(
                    "You already have {count} reminders pending - wait for some "
                    "to fire before adding more."
                ).format(count=MAX_PENDING_REMINDERS),
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


class _RemPagerButton(discord.ui.Button):
    """A reminders-card pager button whose click delegates to a bound handler.

    Components V2 layouts cannot use the ``@discord.ui.button`` decorator
    (buttons live inside :class:`discord.ui.ActionRow` children), so Prev/Next
    are plain instances that forward their click to a coroutine on the owning
    card - the same shape as the leveling cog's ``_PagerButton``.
    """

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction):
        await self._handler(interaction)


class _CancelSelect(discord.ui.Select):
    """Dropdown of the visible page's reminders; picking one cancels it.

    Confirm-less by design (a cancel is trivially reversible - just re-run
    ``/remind``), so the pick deletes the timer and the card re-renders in
    place. Labels are the truncated reminder text; the description carries the
    relative fire time so a member can tell two same-worded reminders apart.
    """

    def __init__(self, card, page_reminders):
        self._owner = card
        options = []
        for r in page_reminders:
            label = reminders_tool.truncate(
                r["message"], reminders_tool.SELECT_LABEL_MAX
            ) or _("(no text)")
            when = human_timedelta(r["expires"])
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(r["id"]),
                    description=_("in {when}").format(when=when)[
                        : reminders_tool.SELECT_LABEL_MAX
                    ],
                )
            )
        super().__init__(
            placeholder=_("Cancel a reminder..."),
            options=options
            or [discord.SelectOption(label=_("(nothing to cancel)"), value="_none")],
            disabled=not options,
        )

    async def callback(self, interaction):
        try:
            value = self.values[0]
            if value == "_none":
                return await interaction.response.defer()
            await self._owner._cancel(interaction, int(value))
        except Exception:
            log.exception("Reminder cancel select failed")


class RemindersCard(AuthorLayoutView):
    """Paginated Components V2 card of a member's pending reminders.

    One line per reminder (relative fire time, truncated text, the channel it
    fires in), :data:`~tools.reminders.REMINDER_PAGE_SIZE` per page, with an
    in-card :class:`_CancelSelect` so cancellation lives right where the list
    is - no separate command. Author-gated through
    :class:`~tools.views.AuthorLayoutView` so only the member who opened it can
    flip pages or cancel (the slash surface is also ephemeral, so it never
    leaks a member's reminders into the channel). The pager row only appears
    past a single page.
    """

    def __init__(self, cog, author_id, reminders, capped, *, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.reminders = reminders
        self.capped = capped
        self.page = 0
        self._build()

    def _line(self, r):
        text = reminders_tool.truncate(
            r["message"], reminders_tool.LINE_TEXT_MAX
        ) or _("(no text)")
        if r["channel_id"]:
            return _("{when} - {text}\n-# in <#{channel}>").format(
                when=discord.utils.format_dt(r["expires"], "R"),
                text=text,
                channel=r["channel_id"],
            )
        return _("{when} - {text}").format(
            when=discord.utils.format_dt(r["expires"], "R"), text=text
        )

    def _build(self):
        self.clear_items()
        total = len(self.reminders)
        self.page, total_pages, start, end = reminders_tool.paginate(
            total, self.page
        )
        page_reminders = self.reminders[start:end]

        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay("## " + _("Your reminders")))
        container.add_item(discord.ui.Separator())

        if not self.reminders:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "You have no reminders set. Use `/remind` to add one."
                    )
                )
            )
            self.add_item(container)
            return

        container.add_item(
            discord.ui.TextDisplay(
                "\n".join(self._line(r) for r in page_reminders)
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_CancelSelect(self, page_reminders)))

        footer = _("-# {count} pending").format(
            count=reminders_tool.format_count(total, self.capped)
        )
        if total_pages > 1:
            footer = _("-# Page {page}/{pages} - {count} pending").format(
                page=self.page + 1,
                pages=total_pages,
                count=reminders_tool.format_count(total, self.capped),
            )
        container.add_item(discord.ui.TextDisplay(footer))

        if total_pages > 1:
            container.add_item(
                discord.ui.ActionRow(
                    _RemPagerButton(
                        self._prev,
                        label=_("Prev"),
                        emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                        style=discord.ButtonStyle.secondary,
                        disabled=self.page <= 0,
                    ),
                    _RemPagerButton(
                        self._next,
                        label=_("Next"),
                        emoji="\N{BLACK RIGHT-POINTING TRIANGLE}",
                        style=discord.ButtonStyle.secondary,
                        disabled=self.page >= total_pages - 1,
                    ),
                )
            )

        self.add_item(container)

    async def _cancel(self, interaction, reminder_id):
        # Confirm-less delete: drop it from the DB (author+type scoped) and from
        # the local list, then re-render in place. Whether or not the row still
        # existed, it is gone now, so it must leave the card either way.
        await self.cog.cancel_reminder(reminder_id, self.author_id)
        self.reminders = [r for r in self.reminders if r["id"] != reminder_id]
        # Once anything is removed the remaining count is at or below the cap,
        # so the "25+" overflow marker no longer applies.
        self.capped = False
        self._build()
        await interaction.response.edit_message(view=self)

    async def _prev(self, interaction):
        try:
            self.page -= 1
            self._build()
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Reminders prev failed")

    async def _next(self, interaction):
        try:
            self.page += 1
            self._build()
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Reminders next failed")


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

    async def _pending_reminder_count(self, user_id):
        """How many reminders this user currently has queued."""
        return (
            await self.bot.db_pool.fetchval(
                "SELECT COUNT(*) FROM timers "
                "WHERE event = 'reminder' AND extra->>'author_id' = $1",
                str(user_id),
            )
            or 0
        )

    async def list_pending_reminders(self, user_id):
        """This user's pending reminders, soonest first, bounded and parsed.

        Scoped to ``event = 'reminder'`` AND this author (never a tempban or any
        other timer event). Fetches one row past :data:`REMINDER_LIST_CAP` so the
        caller can tell "exactly the cap" from "more than the cap" and render the
        overflow as ``25+`` without ever loading an unbounded result set. Returns
        ``(reminders, capped)`` where each reminder is a plain dict
        (``id``/``expires``/``channel_id``/``message``/``event``) and ``capped``
        is True when the user has more pending than the cap.
        """
        rows = await self.bot.db_pool.fetch(
            "SELECT id, expires, extra FROM timers "
            "WHERE event = 'reminder' AND extra->>'author_id' = $1 "
            "ORDER BY expires ASC LIMIT $2",
            str(user_id),
            reminders_tool.REMINDER_LIST_CAP + 1,
        )
        parsed = []
        for row in rows:
            extra = row["extra"]
            extra = json.loads(extra) if isinstance(extra, str) else (extra or {})
            parsed.append(
                {
                    "id": row["id"],
                    "expires": row["expires"],
                    "channel_id": extra.get("channel_id"),
                    "message": extra.get("message") or "",
                    "event": "reminder",
                }
            )
        # Defensive type scoping on top of the SQL filter, then apply the cap.
        parsed = reminders_tool.filter_reminders(parsed)
        capped = len(parsed) > reminders_tool.REMINDER_LIST_CAP
        return parsed[: reminders_tool.REMINDER_LIST_CAP], capped

    async def cancel_reminder(self, reminder_id, user_id):
        """Delete one of ``user_id``'s own reminders; return True if it existed.

        The DELETE is scoped to ``event = 'reminder'`` AND this author, so a user
        can only ever cancel their OWN reminders and never another timer type
        (e.g. a moderation tempban). The DELETE is also the atomic claim the
        dispatch loop competes on (see :meth:`dispatch_timers`): if this removes
        the row the loop is currently sleeping on, we wake the loop so it
        re-sleeps against the new earliest timer. Returns False when the row was
        already gone (it fired, or a previous cancel removed it).
        """
        row = await self.bot.db_pool.fetchrow(
            "DELETE FROM timers WHERE id = $1 AND event = 'reminder' "
            "AND extra->>'author_id' = $2 RETURNING id",
            reminder_id,
            str(user_id),
        )
        if row is not None:
            # Wake the dispatch loop: the earliest timer may have just changed,
            # so it should re-read and re-sleep. Harmless (an extra wakeup) even
            # when the cancelled reminder was not the one being awaited.
            self._have_data.set()
            return True
        return False

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

                # DELETE is the atomic claim on this timer: only the deleter
                # fires it. A concurrent cancel_reminder races on the same row,
                # so if our DELETE removed zero rows someone else (a cancel)
                # already owns it - skip firing. This is what makes cancelling
                # the exact timer the loop is sleeping on race-safe: the loop
                # never delivers a reminder the user cancelled in the same tick.
                status = await self.bot.db_pool.execute(
                    "DELETE FROM timers WHERE id=$1", row["id"]
                )
                if status and status.rsplit(" ", 1)[-1] == "0":
                    continue
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
            else:
                # Let other cogs own their timer events (e.g. scheduled
                # announcements) without coupling them into this cog.
                self.bot.dispatch(f"{event}_timer_complete", extra)
        except Exception:
            log.exception("Error while calling timer")

    @commands.hybrid_command(aliases=["remindme", "reminder"])
    @app_commands.describe(
        when=(
            "What to remind you about (and when, e.g. '10m buy milk'). "
            "Blank opens a form."
        ),
        at=(
            "A Discord timestamp to fire at, e.g. a <t:...> tag. "
            "Overrides the time in 'when'."
        ),
    )
    async def remind(
        self,
        ctx,
        at: Optional[commands.Timestamp] = None,
        *,
        when: str = None,
    ):
        """Reminds you of something after a certain amount of time."""

        # Nothing at all supplied -> offer the interactive form. Slash
        # invocations can open the modal straight away; prefix invocations have
        # no interaction, so they get a button that opens it on click.
        if at is None and when is None:
            if ctx.interaction is not None:
                return await ctx.interaction.response.send_modal(
                    RemindModal(self, ctx.channel.id, ctx.author.id)
                )
            view = RemindLauncherView(self, ctx.author.id, ctx.channel.id)
            view.message = await ctx.send(
                _("Tap the button below to set a reminder."), view=view
            )
            return

        if at is not None:
            # A native timestamp wins as the fire time; the free-text remainder
            # (if any) is the message VERBATIM - no time parsing on it. Cleaned
            # for parity with the natural-language path (defangs @mentions).
            dt = at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = ctx.message.created_at.astimezone(datetime.timezone.utc)
            if dt <= now:
                return await ctx.send(
                    _("That time is in the past. Give me a moment in the future.")
                )
            remainder = (when or "").strip()
            if remainder:
                message = await commands.clean_content().convert(ctx, remainder)
            else:
                message = _(DEFAULT_REMINDER_MESSAGE)
        else:
            # No 'at' -> byte-for-byte today's path: the very same converter,
            # invoked manually, so 'when' still yields (dt, message) and raises
            # the same BadArgument on a bad or past time.
            result = await UserFriendlyTime(
                commands.clean_content, default="something"
            ).convert(ctx, when)
            dt = result.dt
            message = result.arg

        if await self._pending_reminder_count(ctx.author.id) >= MAX_PENDING_REMINDERS:
            return await ctx.send(
                _(
                    "You already have {count} reminders pending - wait for some "
                    "to fire before adding more."
                ).format(count=MAX_PENDING_REMINDERS)
            )

        await self.create_timer(
            dt,
            "reminder",
            author_id=ctx.author.id,
            channel_id=ctx.channel.id,
            message=message,
        )
        await ctx.send(
            _("Okay, reminding you {when}: {message}").format(
                when=discord.utils.format_dt(dt, "R"), message=message
            )
        )


    @commands.hybrid_command(name="reminders", aliases=["myreminders"])
    async def reminders(self, ctx):
        """Shows and lets you cancel your pending reminders."""

        reminders_list, capped = await self.list_pending_reminders(ctx.author.id)
        view = RemindersCard(self, ctx.author.id, reminders_list, capped)
        # A LayoutView carries its own content, so no embed/content; suppress
        # mentions since TextDisplay resolves them. Ephemeral on the slash
        # surface so a member's reminders never leak into the channel (prefix
        # invocations have no ephemeral option and post to the channel, but the
        # card is still author-gated so only the invoker can drive it).
        view.message = await ctx.send(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(Reminder(bot))
