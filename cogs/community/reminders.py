import asyncio
import datetime
import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import reminders_store as reminders_tool
from tools.formats import random_colour
from tools.i18n import _, ngettext
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

# Cap RECURRING reminders per user, bot-wide. A recurring reminder is the only
# kind that re-inserts itself, so it is the only kind whose row count does not
# drain on its own: five of them at the 1h floor is 120 rows/day of churn per
# user, which is the ceiling we are willing to underwrite at 1000+ guilds.
MAX_RECURRING_REMINDERS = 5

# The recurring cap's count, shared by the pre-flight read and the guarded
# creation below so the two can never drift into counting different things.
RECURRING_COUNT_QUERY = (
    "SELECT COUNT(*) FROM timers "
    "WHERE event = 'reminder' AND extra->>'author_id' = $1 "
    "AND extra->>'repeat_seconds' IS NOT NULL"
)

# Advisory-lock class id for the recurring cap (see create_reminder_timer).
# The TWO-ARGUMENT form of pg_advisory_xact_lock has a key space entirely
# separate from the one-argument bigint form tools/privacy.py locks avatar
# history on, so this lock provably cannot collide with any other in the bot.
# The second half is the user id folded into int4; a fold collision only ever
# costs two unrelated members microseconds on a command neither runs often.
_RECURRING_LOCK_CLASS = 0x52454D49  # 'REMI'
_INT4_FOLD = 2147483647

TIMER_CLAIM_TIMEOUT_MINUTES = 5
TIMER_RETRY_MAX_SECONDS = 3600

# Delivery semantics split by event (see dispatch_timers):
#
# * DURABLE_TIMER_EVENTS use claim -> deliver -> delete with bounded retries.
#   The action MUST be idempotent (a re-fire is harmless) and losing a firing
#   is worse than doubling it. Only the tempban unban qualifies: a missed unban
#   is a permanent ban, and the unban is idempotent through call_timer's
#   NotFound guard, so at-least-once is safe and durability is valuable.
# * Every other event (reminders, and the generic ``*_timer_complete`` dispatch
#   used by announcements/temprole) uses DELETE-as-atomic-claim BEFORE delivery
#   (at-most-once). Those side effects are NOT idempotent - a doubled
#   announcement or temprole re-fire is user-visible - so a crash mid-delivery
#   must lose at most one firing, never double it.
DURABLE_TIMER_EVENTS = frozenset({"tempban"})

# A durable timer that keeps failing is retried with exponential backoff up to
# this many attempts; on exhaustion the row is dead-lettered (deleted + logged)
# so a permanently Forbidden action cannot retry for eternity.
MAX_TIMER_ATTEMPTS = 12


def _author_mention_only(author_id):
    """The single-entry ``users=`` list a reminder delivery may ping.

    ``[]`` (no user mention resolves) rather than a raise when the id is
    missing or corrupt: the delivery it guards happens after the row has
    already been deleted, so nothing here may cost a firing. An unresolvable
    id also cannot produce a working ``<@...>`` tag in the body anyway.
    """
    try:
        return [discord.Object(id=int(author_id))]
    except (TypeError, ValueError):
        return []


class ReminderChannelGone(Exception):
    """A reminder's target channel no longer exists (a 404 on fetch_channel).

    Raised rather than swallowed because the two delivery shapes have to answer
    it differently: a one-shot is simply dropped (its row is already gone), while
    a RECURRING one must also unwind the next occurrence its claim committed, or
    the series would re-fire into the same dead channel for ever. Carrying that
    distinction in the return value of ``call_timer`` would let a future caller
    ignore it by accident; an exception cannot be ignored silently.
    """


def timer_retry_delay(attempts):
    """Return the bounded exponential retry delay after a delivery failure."""
    return min(60 * (2 ** max(0, attempts)), TIMER_RETRY_MAX_SECONDS)


def format_interval(seconds):
    """A localized 'every 2 days' label for a recurrence interval.

    The unit choice is the store's pure ``split_interval`` (coarsest exact unit);
    only the pluralisation lives here, through ``ngettext`` so languages with
    other plural rules than English get real forms rather than an '(s)' suffix.
    """
    unit, count = reminders_tool.split_interval(seconds)
    if unit == "week":
        return ngettext("every week", "every {count} weeks", count).format(count=count)
    if unit == "day":
        return ngettext("every day", "every {count} days", count).format(count=count)
    if unit == "hour":
        return ngettext("every hour", "every {count} hours", count).format(count=count)
    if unit == "minute":
        return ngettext(
            "every minute", "every {count} minutes", count
        ).format(count=count)
    return ngettext(
        "every second", "every {count} seconds", count
    ).format(count=count)


def repeat_problem_message(problem):
    """The user-facing explanation for a rejected repeat interval.

    Mirrors the ``problem`` codes ``reminders_store.parse_repeat`` returns, kept
    out of that module so the parsing stays free of any i18n dependency.
    """
    if problem == "too_short":
        return _(
            "I can only repeat a reminder once an hour at most. Try `hourly` "
            "or something longer like `2d`."
        )
    if problem == "too_long":
        return _("I can only repeat a reminder for up to a year - try `365d` or less.")
    return _(
        "I couldn't understand that repeat interval. Try `hourly`, `daily`, "
        "`weekly`, or a duration like `2d` or `12h`."
    )


def recurring_limit_message():
    """The refusal shown when a member is already at the recurring cap."""
    return _(
        "You already have {count} repeating reminders - cancel one with "
        "`/reminders` before adding another."
    ).format(count=MAX_RECURRING_REMINDERS)


def reminder_confirmation(dt, message, repeat_seconds):
    """The 'Okay, reminding you ...' acknowledgement, shared by both surfaces.

    The one-shot wording is untouched (same msgid, same arguments as before this
    feature existed); a recurring reminder gets its own sentence carrying the
    interval, so nobody has to guess whether the repeat was accepted.
    """
    when = discord.utils.format_dt(dt, "R")
    if repeat_seconds is None:
        return _("Okay, reminding you {when}: {message}").format(
            when=when, message=message
        )
    return _("Okay, reminding you {when} and then {interval}: {message}").format(
        when=when, interval=format_interval(repeat_seconds), message=message
    )


def recurrence_extra(repeat_seconds):
    """The ``extra`` fragment that turns a new timer into a recurring series.

    Empty for a one-shot, so ``create_timer`` receives exactly the keyword
    arguments it always did and the stored JSON of a non-repeating reminder is
    byte-identical to what this feature replaced.
    """
    if repeat_seconds is None:
        return {}
    return {"repeat_seconds": repeat_seconds, "occurrence": 1}


class RemindModal(LocaleModal):
    """Interactive reminder form: a short "When" and a paragraph "Message".

    The "When" field is parsed with the cog's own time parsing (ShortTime for
    relative/absolute inputs, falling back to FutureTime for natural language),
    then the same "reminder" timer row the text command creates is inserted.
    """

    def __init__(self, cog, channel_id, author_id, guild_id=None, repeat=None):
        super().__init__(title=_("Set a reminder"))
        self.cog = cog
        self.channel_id = channel_id
        self.author_id = author_id
        self.guild_id = guild_id
        # Prefilled when /remind was invoked with a repeat but no time, so the
        # option the member already picked is not silently lost to the form.
        self.repeat_default = (repeat or "").strip()[:32] or None

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

        # Optional third field: leaving it blank keeps the historical one-shot
        # behaviour exactly. It exists mainly for the prefix surface, which
        # cannot pass the slash command's `repeat` option.
        self.repeat_input = discord.ui.TextInput(
            label=_("Repeat (optional)"),
            placeholder=_("hourly, daily, weekly, or e.g. 2d - blank for once"),
            style=discord.TextStyle.short,
            required=False,
            max_length=32,
            default=self.repeat_default,
        )
        self.add_item(self.repeat_input)

    async def on_submit(self, interaction):
        when_raw = (self.when_input.value or "").strip()
        message = (self.message_input.value or "").strip() or _(
            DEFAULT_REMINDER_MESSAGE
        )

        repeat_seconds, problem = reminders_tool.parse_repeat(
            self.repeat_input.value
        )
        if problem is not None:
            return await interaction.response.send_message(
                repeat_problem_message(problem), ephemeral=True
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

        # The recurring cap is enforced BY the insert (one locked count-and-
        # insert), not by a separate read before it, so two concurrent submits
        # cannot both squeeze past a cap they each read as clear.
        created = await self.cog.create_reminder_timer(
            dt,
            repeat_seconds=repeat_seconds,
            author_id=self.author_id,
            channel_id=self.channel_id,
            guild_id=self.guild_id,
            message=message,
        )
        if created is None:
            return await interaction.response.send_message(
                recurring_limit_message(), ephemeral=True
            )

        await interaction.response.send_message(
            reminder_confirmation(dt, message, repeat_seconds),
            ephemeral=True,
            # The modal body is stored verbatim (the prefix surface defangs
            # its own through clean_content), so this echo carries raw user
            # text. Nothing in an acknowledgement needs to ping anyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RemindLauncherView(AuthorView):
    """A single button that opens the reminder modal (prefix-command path).

    Prefix invocations have no interaction to open a modal with, so the command
    posts this view and the author clicks the button to summon the modal.
    """

    def __init__(
        self, cog, author_id, channel_id, guild_id=None, timeout=180, repeat=None
    ):
        super().__init__(
            author_id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.cog = cog
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.repeat = repeat

        button = discord.ui.Button(
            label=_("Set a reminder"),
            style=discord.ButtonStyle.primary,
            emoji="\N{ALARM CLOCK}",
        )
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction):
        await interaction.response.send_modal(
            RemindModal(
                self.cog,
                self.channel_id,
                self.author_id,
                self.guild_id,
                repeat=self.repeat,
            )
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
    fires in), :data:`~cogs.community.reminders_store.REMINDER_PAGE_SIZE` per page, with an
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
        """One card line: fire time + text, plus a subtext of context notes.

        The notes row carries where it fires and - for a recurring reminder -
        the repeat glyph and its interval. Composing the subtext from parts
        keeps the msgid count flat (one note per fact) instead of needing one
        combined msgid per combination of facts; a one-shot channel reminder
        still renders exactly the string it always did.
        """
        text = reminders_tool.truncate(
            r["message"], reminders_tool.LINE_TEXT_MAX
        ) or _("(no text)")
        line = _("{when} - {text}").format(
            when=discord.utils.format_dt(r["expires"], "R"), text=text
        )
        notes = []
        if r["channel_id"]:
            notes.append(
                _("in <#{channel}>").format(channel=r["channel_id"])
            )
        if r.get("repeat_seconds"):
            notes.append(
                "{glyph} {interval}".format(
                    glyph=reminders_tool.REPEAT_GLYPH,
                    interval=format_interval(r["repeat_seconds"]),
                )
            )
        if notes:
            return line + "\n-# " + " - ".join(notes)
        return line

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
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _prev(self, interaction):
        try:
            self.page -= 1
            self._build()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            log.exception("Reminders prev failed")

    async def _next(self, interaction):
        try:
            self.page += 1
            self._build()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
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

    async def _pending_recurring_count(self, user_id, connection=None):
        """How many RECURRING reminders this user currently has queued.

        ``connection`` runs the count on a caller-held connection, which is how
        :meth:`create_reminder_timer` counts inside its lock; the default reads
        through the pool.

        Cost: the same ``timers_reminder_author_idx (event, (extra->>'author_id'),
        expires)`` the pending-count guard already rides. Both predicates are
        equality on the index's leading columns, so Postgres walks only this
        user's own entries - bounded by :data:`MAX_PENDING_REMINDERS` (25) - and
        rechecks ``repeat_seconds`` on those few heap rows (``extra`` is not in
        the index, so this is not an index-only scan). At 1000+ guilds the scan
        width is a function of one user's cap, never of the table size, so no
        new index is warranted.
        """
        executor = connection if connection is not None else self.bot.db_pool
        return await executor.fetchval(RECURRING_COUNT_QUERY, str(user_id)) or 0

    async def _recurring_limit_reached(self, user_id, connection=None):
        """True when this user cannot add another recurring reminder."""
        return (
            await self._pending_recurring_count(user_id, connection)
            >= MAX_RECURRING_REMINDERS
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
            extra = reminders_tool.parse_extra(row["extra"])
            parsed.append(
                {
                    "id": row["id"],
                    "expires": row["expires"],
                    "channel_id": extra.get("channel_id"),
                    "message": extra.get("message") or "",
                    "event": "reminder",
                    # None for a one-shot; the card only marks a line when set.
                    "repeat_seconds": reminders_tool.recurrence_seconds(extra),
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
        (e.g. a moderation tempban). A claimed reminder cannot be cancelled
        because delivery has already started. If this removes the row the loop
        is currently sleeping on, we wake the loop so it re-sleeps against the
        new earliest timer. Returns False when the row was already gone, already
        claimed, or a previous cancel removed it.

        For a RECURRING reminder this cancels the whole series, with no extra
        code: a series is never more than one pending row (the next occurrence
        is only inserted when the current one fires), so deleting that row is
        deleting the series. Cancelling mid-delivery is the one race the
        ``claimed_at IS NULL`` guard leaves - and the recurring dispatch path
        deletes and re-inserts inside a single transaction, so the cancel either
        wins outright (nothing fires, nothing is rescheduled) or loses to a
        delivery that has already committed the next occurrence, which the user
        can then cancel again.
        """
        row = await self.bot.db_pool.fetchrow(
            "DELETE FROM timers WHERE id = $1 AND event = 'reminder' "
            "AND extra->>'author_id' = $2 AND claimed_at IS NULL RETURNING id",
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

    async def create_reminder_timer(self, when, *, repeat_seconds=None, **extra):
        """Create a reminder timer; return the row, or None if the cap refused it.

        THE creation entry point for both surfaces (the command and the modal).
        A one-shot goes straight to :meth:`create_timer` and is byte-identical to
        what it always was - the recurrence keys are absent and no cap is
        consulted at all.

        A RECURRING one counts and inserts under one per-user advisory lock,
        instead of the check-then-act the pending cap uses, because the two caps
        fail differently. A one-shot that slips past its cap fires once and is
        gone, so the overshoot drains itself. A recurring one re-inserts itself
        for ever and nothing re-checks the cap afterwards, so a single burst of
        concurrent `/remind ... repeat:` calls would park that member above the
        ceiling permanently. The lock closes that window outright rather than
        merely narrowing it: a plain guarded INSERT would still let two
        READ COMMITTED snapshots miss each other's uncommitted row.

        Scale note: the lock is transaction-scoped (released by COMMIT/ROLLBACK,
        never leaked), keyed by THIS member, and taken only on the recurring
        creation path - a rare, human-paced command. Two members never wait on
        each other, so it adds no contention at 1000+ guilds.
        """
        if repeat_seconds is None:
            return await self.create_timer(when, "reminder", **extra)

        user_id = extra["author_id"]
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1, $2)",
                    _RECURRING_LOCK_CLASS,
                    user_id % _INT4_FOLD,
                )
                if await self._recurring_limit_reached(user_id, conn):
                    return None
                row = await conn.fetchrow(
                    "INSERT INTO timers(event, expires, created, extra) "
                    "VALUES($1, $2, $3, $4::jsonb) RETURNING id",
                    "reminder",
                    when,
                    datetime.datetime.now(datetime.timezone.utc),
                    json.dumps({**extra, **recurrence_extra(repeat_seconds)}),
                )
        self._have_data.set()
        return row

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
            "SELECT * FROM timers WHERE claimed_at IS NULL "
            "OR claimed_at < now() - interval '5 minutes' "
            "ORDER BY expires LIMIT 1"
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

                # Two delivery contracts, chosen by event (see
                # DURABLE_TIMER_EVENTS). Tempbans take the durable claim ->
                # deliver -> delete path with bounded retries; everything else
                # takes the at-most-once DELETE-as-claim path.
                if row["event"] in DURABLE_TIMER_EVENTS:
                    await self._deliver_durable(row)
                else:
                    await self._deliver_at_most_once(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error while dispatching timers")
                await asyncio.sleep(5)

    async def _deliver_at_most_once(self, row):
        """Fire reminders / generic dispatched events with at-most-once safety.

        The DELETE is the atomic claim (BL2 contract): the row is removed BEFORE
        delivery, so exactly one worker can win it and a crash mid-delivery loses
        at most one firing - it can never double-fire. ``cancel_reminder``
        competes on the very same conditional DELETE, so a race there simply
        means the loser sees no row and does nothing.

        A recurring reminder takes the same claim, wrapped in a transaction that
        also enqueues its next occurrence (see :meth:`_claim_and_reschedule`).
        Every other row - every non-``reminder`` event, and every reminder
        without a valid ``repeat_seconds`` - takes the untouched single-statement
        path below.
        """
        repeat_seconds = None
        next_id = None
        if row["event"] == "reminder":
            repeat_seconds = reminders_tool.recurrence_seconds(
                reminders_tool.parse_extra(row["extra"])
            )
        if repeat_seconds is not None:
            claimed, next_id = await self._claim_and_reschedule(row, repeat_seconds)
        else:
            claimed = await self.bot.db_pool.fetchrow(
                "DELETE FROM timers WHERE id = $1 AND claimed_at IS NULL RETURNING *",
                row["id"],
            )
        if claimed is None:
            # Someone else (another worker, or a cancel) already took it.
            return
        try:
            await self.call_timer(claimed)
        except asyncio.CancelledError:
            raise
        except ReminderChannelGone:
            # The claim IS the delete, so a one-shot is already dropped and there
            # is nothing more to do - exactly the behaviour this path always had.
            # A recurring one is different: the reschedule is committed by now,
            # so leaving it would re-fire this series into a channel that cannot
            # exist again, for ever, at up to 24 deliveries a day. Nothing else
            # would ever stop it (the series re-inserts itself, and no other
            # timer kind survives this path), so it ends here.
            if next_id is not None:
                await self._end_recurring_series(next_id, claimed)
        except Exception:
            # The row is already gone, so there is nothing to retry: at-most-once
            # deliberately drops this firing rather than risk a double-send.
            log.exception(
                "Timer %s (%s) delivery failed after atomic claim; dropping "
                "(at-most-once)",
                claimed["id"],
                claimed["event"],
            )

    async def _end_recurring_series(self, next_id, claimed):
        """Delete the next occurrence a claim already committed, ending a series.

        The unwind half of :meth:`_claim_and_reschedule`'s ordering: that method
        deliberately commits the next occurrence BEFORE delivering, so a crash
        costs one firing instead of the whole series. When the delivery instead
        proves the series can never be delivered again, the same ordering has to
        be paid back - here.

        Best-effort and never raised out of: the reminder itself is already gone,
        so the worst case of a failure here is the series limping on until the
        next attempt takes the very same path.
        """
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM timers WHERE id = $1 AND claimed_at IS NULL", next_id
            )
        except Exception:
            log.exception(
                "Failed to end recurring reminder series at timer %s", next_id
            )
            return
        log.warning(
            "Ended recurring reminder series (next timer %s, author %s): its "
            "channel no longer exists",
            next_id,
            reminders_tool.parse_extra(claimed["extra"]).get("author_id"),
        )

    async def _claim_and_reschedule(self, row, repeat_seconds):
        """Claim a due recurring reminder AND enqueue its next occurrence.

        Ordering, and why: the reschedule happens BEFORE the delivery, never
        after. The two orderings fail differently and only one of them is
        recoverable by the user.

        * reschedule AFTER the send: a crash between the send and the INSERT
          ends the series silently. The member keeps waiting for a daily
          reminder that will never come again and has no way to notice - the
          failure is invisible.
        * reschedule BEFORE the send (this): a crash between the INSERT and the
          send costs exactly ONE delivery, and the series lives. The member sees
          a gap, not a disappearance, and the next occurrence arrives on time.

        At-most-once is preserved either way, because the claim IS the delete:
        the fired row is gone before anything is sent, so no crash can replay
        it. Losing one delivery is the price of never double-sending, which is
        the same trade the non-recurring path already makes.

        The claim and the INSERT run in ONE transaction, so the remaining window
        - crash after the claim but before the reschedule, which WOULD kill the
        series - does not exist: either both commit or neither does, and if
        neither does the row is still pending and simply fires again later.

        Returns ``(claimed, next_id)``: the claimed row as a plain dict whose
        ``extra`` carries the delivery-only keys ``missed`` and ``next_at``
        (never persisted: the row written for the next occurrence is built from
        the claimed extra, not from this copy), plus the id of that next
        occurrence so the caller can unwind it if the delivery proves the series
        is undeliverable (see :meth:`_end_recurring_series`). ``(None, None)``
        when someone else won the claim.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        next_at, missed = reminders_tool.next_occurrence(
            row["expires"], repeat_seconds, now
        )
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                claimed = await conn.fetchrow(
                    "DELETE FROM timers WHERE id = $1 AND claimed_at IS NULL "
                    "RETURNING *",
                    row["id"],
                )
                if claimed is None:
                    # A cancel or another worker took it; the transaction has
                    # written nothing, so there is no orphan next occurrence.
                    return None, None
                extra = reminders_tool.parse_extra(claimed["extra"])
                next_extra = dict(extra)
                # The counter is the ordinal of the SCHEDULED slot, so slots
                # skipped by an outage still advance it - it stays a faithful
                # "how far along the series is", not a delivery count. A corrupt
                # counter must NOT raise here: the transaction would roll back,
                # leaving the row pending and re-firing every few seconds
                # forever, so it falls back to 1 instead.
                next_extra["occurrence"] = (
                    reminders_tool.occurrence_number(extra) + 1 + missed
                )
                next_id = await conn.fetchval(
                    "INSERT INTO timers(event, expires, created, extra) "
                    "VALUES($1, $2, $3, $4::jsonb) RETURNING id",
                    claimed["event"],
                    next_at,
                    now,
                    json.dumps(next_extra),
                )
        delivery_extra = dict(extra)
        delivery_extra["missed"] = missed
        delivery_extra["next_at"] = next_at.isoformat()
        delivered = dict(claimed)
        delivered["extra"] = delivery_extra
        return delivered, next_id

    async def _deliver_durable(self, row):
        """Fire a durable (idempotent) timer with claim -> deliver -> delete.

        The claim leases the row (``claimed_at``); a crash after delivery but
        before the delete lets the 5-minute stale-claim reclaim re-run it, which
        is safe because the action is idempotent. Delivery failures release the
        claim and reschedule with exponential backoff, incrementing ``attempts``;
        once ``attempts`` would reach :data:`MAX_TIMER_ATTEMPTS` the row is
        dead-lettered (deleted + logged) so it cannot retry forever.
        """
        claimed = await self.bot.db_pool.fetchrow(
            "UPDATE timers SET claimed_at = now() WHERE id = $1 "
            "AND (claimed_at IS NULL OR "
            "claimed_at < now() - interval '5 minutes') RETURNING *",
            row["id"],
        )
        if claimed is None:
            return
        try:
            await self.call_timer(claimed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            prior_attempts = int(claimed["attempts"] or 0)
            attempts = prior_attempts + 1
            if attempts >= MAX_TIMER_ATTEMPTS:
                await self.bot.db_pool.execute(
                    "DELETE FROM timers WHERE id = $1", claimed["id"]
                )
                log.error(
                    "Timer dead-letter: dropping %s timer id=%s after %s "
                    "attempts; extra=%s last_error=%s",
                    claimed["event"],
                    claimed["id"],
                    attempts,
                    claimed["extra"],
                    str(exc)[:500],
                )
                return
            delay = timer_retry_delay(prior_attempts)
            await self.bot.db_pool.execute(
                "UPDATE timers SET claimed_at = NULL, "
                "attempts = attempts + 1, last_error = $2, "
                "expires = now() + "
                "($3::double precision * interval '1 second') "
                "WHERE id = $1",
                claimed["id"],
                str(exc)[:500],
                delay,
            )
            log.exception(
                "Timer %s (%s) delivery failed; retry %s/%s in %ss",
                claimed["id"],
                claimed["event"],
                attempts,
                MAX_TIMER_ATTEMPTS,
                delay,
            )
        else:
            await self.bot.db_pool.execute(
                "DELETE FROM timers WHERE id = $1", claimed["id"]
            )

    @staticmethod
    def _recurrence_footer(extra):
        """The subtext appended to a RECURRING reminder's delivery, else ''.

        Two facts, both only knowable at delivery time: how many occurrences an
        outage swallowed (so a gap never looks like the bot silently dropping
        the series), and when the next one lands. Both keys are injected by
        :meth:`_claim_and_reschedule` and are absent from every one-shot
        reminder, so a non-recurring delivery is byte-identical to before.
        """
        repeat_seconds = reminders_tool.recurrence_seconds(extra)
        if repeat_seconds is None:
            return ""
        parts = []
        # Total, like every other reader of `extra`: the key is injected
        # in-process today and never persisted, but the whole point of parsing
        # `extra` defensively is that nothing downstream has to know that. A
        # ValueError here would cost this delivery outright (at-most-once has
        # already deleted the row), so a corrupt count means "say nothing about
        # missed occurrences", never "lose the reminder".
        try:
            missed = int(extra.get("missed") or 0)
        except (TypeError, ValueError):
            missed = 0
        if missed > 0:
            parts.append(
                ngettext(
                    "I was offline, so {count} occurrence was missed.",
                    "I was offline, so {count} occurrences were missed.",
                    missed,
                ).format(count=missed)
            )
        next_at = extra.get("next_at")
        if next_at:
            try:
                when = datetime.datetime.fromisoformat(next_at)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                parts.append(
                    _("Repeats {interval} - next {when}").format(
                        interval=format_interval(repeat_seconds),
                        when=discord.utils.format_dt(when, "R"),
                    )
                )
        if not parts:
            return ""
        return "\n-# " + " ".join(parts)

    async def call_timer(self, row):
        extra = reminders_tool.parse_extra(row["extra"])
        event = row["event"]
        if event == "reminder":
            ch = self.bot.get_channel(extra["channel_id"])
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(extra["channel_id"])
                except discord.NotFound:
                    log.warning(
                        "Dropping timer %s because channel %s no longer exists",
                        row["id"],
                        extra["channel_id"],
                    )
                    raise ReminderChannelGone(extra["channel_id"]) from None
            await ch.send(
                _("<@{author_id}>, {when}: {message}").format(
                    author_id=extra["author_id"],
                    when=human_timedelta(row["created"]),
                    message=extra["message"],
                )
                + self._recurrence_footer(extra),
                # The body is free text the author typed, and a recurring
                # reminder re-broadcasts it on a schedule - with default
                # mentions that is a ping-harassment tool pointed at whoever
                # they named. Only the author's own mention (the one the
                # template itself writes) may resolve; everything else in the
                # body is inert text. Total, like every other reader of
                # `extra`: the row is already deleted by the time we get here,
                # so a corrupt author_id must cost the mention, never the
                # delivery.
                allowed_mentions=discord.AllowedMentions(
                    users=_author_mention_only(extra.get("author_id")),
                    roles=False,
                    everyone=False,
                ),
            )
        elif event == "tempban":
            g = self.bot.get_guild(extra["guild_id"])
            if g is None:
                try:
                    g = await self.bot.fetch_guild(extra["guild_id"])
                except (discord.NotFound, discord.Forbidden):
                    log.warning(
                        "Dropping temp-ban timer %s because guild %s is unavailable",
                        row["id"],
                        extra["guild_id"],
                    )
                    return
            try:
                await g.unban(
                    discord.Object(id=extra["user_id"]),
                    reason="Temp-ban expired",
                )
            except discord.NotFound:
                # Already manually unbanned: the intended final state is
                # satisfied, so acknowledge the timer.
                return
        else:
            # Let other cogs own their timer events (e.g. scheduled
            # announcements) without coupling them into this cog.
            self.bot.dispatch(f"{event}_timer_complete", extra)

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
        repeat=(
            "Repeat it: hourly, daily, weekly, or a duration like 2d "
            "(min 1h, max 365d). Blank fires once."
        ),
    )
    async def remind(
        self,
        ctx,
        at: Optional[commands.Timestamp] = None,
        *,
        when: str = None,
        repeat: Optional[str] = None,
    ):
        """Reminds you of something after a certain amount of time.

        ``repeat`` is deliberately declared AFTER the consume-rest ``when``:
        discord.py's prefix parser stops at the first keyword-only parameter, so
        on the prefix surface ``repeat`` is never parsed and always keeps its
        default - ``?remind 10m buy milk`` behaves exactly as it always has,
        with no risk of the repeat option swallowing the message. Prefix users
        reach the recurrence through the modal's "Repeat" field instead.
        """

        repeat_seconds, problem = reminders_tool.parse_repeat(repeat)
        if problem is not None:
            return await ctx.send(repeat_problem_message(problem))

        # Nothing at all supplied -> offer the interactive form. Slash
        # invocations can open the modal straight away; prefix invocations have
        # no interaction, so they get a button that opens it on click.
        if at is None and when is None:
            if ctx.interaction is not None:
                return await ctx.interaction.response.send_modal(
                    RemindModal(
                        self,
                        ctx.channel.id,
                        ctx.author.id,
                        ctx.guild.id if ctx.guild else None,
                        repeat=repeat,
                    )
                )
            view = RemindLauncherView(
                self,
                ctx.author.id,
                ctx.channel.id,
                ctx.guild.id if ctx.guild else None,
                repeat=repeat,
            )
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

        # Same as the modal: the recurring cap lives inside the insert, so it
        # cannot be raced by two invocations that both read it as clear.
        created = await self.create_reminder_timer(
            dt,
            repeat_seconds=repeat_seconds,
            author_id=ctx.author.id,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            message=message,
        )
        if created is None:
            return await ctx.send(recurring_limit_message())

        await ctx.send(reminder_confirmation(dt, message, repeat_seconds))

    @remind.autocomplete("repeat")
    async def repeat_autocomplete(self, interaction, current):
        """Suggest the named presets while still allowing any free duration.

        Autocomplete rather than fixed choices on purpose: fixed choices would
        make Discord REFUSE anything but hourly/daily/weekly, and the whole point
        is that "2d" or "12h" work too. So the presets are offered as
        suggestions and whatever the member types is echoed back - already
        parsed - as the first entry when it is a valid interval, which doubles as
        live validation before they ever submit.
        """
        typed = (current or "").strip().lower()
        choices = []
        seconds = reminders_tool.parse_repeat(typed)[0]
        if seconds is not None and typed not in reminders_tool.REPEAT_PRESETS:
            choices.append(
                app_commands.Choice(name=format_interval(seconds)[:100], value=typed)
            )
        for name in reminders_tool.REPEAT_PRESETS:
            if not typed or name.startswith(typed):
                choices.append(app_commands.Choice(name=name, value=name))
        return choices[:25]


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
