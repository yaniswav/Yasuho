"""What happens to a ticket after it is opened: the two in-thread controls, and
the three ways a ticket ends.

The controls are the ``tk:`` DynamicItem buttons open.py reserved. They carry
the THREAD id in their ``custom_id`` - the thread is the ticket's identity
(``thread_id`` is the table's UNIQUE column, and ``storage.fetch_by_thread`` is
the seam T1 left for exactly this), so a button posted months ago still resolves
its ticket with nothing stored per message and nothing captured at post time.

THE CLOSE ORDER, and why it is that order
-----------------------------------------
1. **Transcript**, from the live thread. First because it is the only artifact
   that cannot be rebuilt: after this point the thread can be archived, locked,
   or deleted by a moderator, and every one of those makes a later read worse or
   impossible. It is also read BEFORE the row is closed so that a crash mid-flow
   loses nothing that the sweep could not redo.
2. **The row** (``storage.close_ticket``). This is the mutual exclusion for
   everything below it: the statement only matches an OPEN row, so exactly one
   caller in the world gets to write one transcript, one archive and one log
   line. It also frees the opener's cap slot, which is the one consequence a
   member can feel, so it happens before any best-effort Discord call that might
   fail. And it is what makes step 4 self-suppressing: our own archive fires
   THREAD_UPDATE straight back into :meth:`TicketLifecycle.on_thread_update`,
   which reads the row, sees ``closed`` and stops. (Even if that raced, the
   ``AND status = 'open'`` guard would refuse it a second time.)
3. **The in-thread notice**, before the archive because an archived thread
   cannot be posted in.
4. **Archive and lock.** Best effort. The ticket is already closed if this
   fails; a live thread on a closed ticket is untidy, a locked thread on an open
   row would strand a cap slot behind a button nobody can click.
5. **The log summary plus the transcript file**, last, because it needs
   ``closed_at`` and because a log channel nobody configured must not be able to
   stop a close.

WHERE THE TRANSCRIPT GOES: the configured log channel, and nowhere else. It is
built only when such a channel exists and the bot may attach a file there (both
tested before a single page of history is read), it never touches the database
(transcripts.py has no pool), and it is never sent to the opener, the thread, or
a DM. A guild with no log channel gets no transcript at all - the close says so
and carries on.

The room is told EITHER WAY, and that symmetry is deliberate: the closing notice
states that no transcript was saved when none was, and states that one was saved
when one was. The person being helped is the only party who cannot see the log
channel, so they are the one who has to be told a copy of the conversation just
went there.

THE THREE ENDINGS
-----------------
* **Somebody clicks Close.** The opener or support, behind an ephemeral confirm.
* **The thread auto-archives.** Discord's own auto-archive IS the inactivity
  signal, which is why the feature needs no per-ticket timer and no
  last-activity column: the room goes quiet, Discord archives it after THE
  GUILD'S OWN WINDOW (``tickets_inactivity_hours``, written onto the thread as
  its ``auto_archive_duration`` when open.py creates it), and the listener turns
  that into a close. That is where the setting takes effect, which is why this
  path does not read it again: the archive it reacts to already carries it.
  The transcript is read from the ARCHIVED thread and the thread is NOT
  unarchived - reads work on an archived thread, and the only edit Discord
  accepts on one is unarchiving it, so "lock it too" would cost an unarchive,
  a re-archive and a flicker in the channel list for no protection worth having.
  (A member with the permission may unarchive a closed thread afterwards. The
  row stays closed and every button says so: a live room on a closed ticket is
  untidy, but re-opening a ticket is a decision this lot does not make, and
  fighting the unarchive would be a loop.)
* **The thread is deleted.** ``on_raw_thread_delete`` closes the row so the
  opener's cap slot is not held by a room that no longer exists. Raw, because
  discord.py only dispatches the non-raw ``on_thread_delete`` for a thread that
  was still in cache.

THE SWEEP is the backstop for the endings the gateway did not deliver, and T1's
hand-off named both: a thread that archived while the bot was down (discord.py
turns a THREAD_UPDATE for an uncached thread into ``thread_join``, not
``thread_update``, so no listener sees it), and the committed-open row whose
thread never existed. Once an hour, ONE query, at most fifty rows, no fetches.

Its "is this still alive?" test is the thread CACHE, and that works because of a
documented asymmetry: ``Guild.get_thread`` keeps ACTIVE threads and drops
archived ones ("This does not always retrieve archived threads, as they are not
retained in the internal cache" - discord.py 2.7.1). So a cache HIT proves the
ticket is live and the row is skipped; a cache MISS is archived, deleted, or
unknown. A miss alone is never enough - the row must ALSO be older than the
guild's own inactivity window - so a transient gateway state cannot close a busy
ticket. A swept close writes no transcript: there is no thread object to read
one from, and inventing a fetch here is exactly the storm this design refuses.

The one cache state that is NOT a miss but looks like one is a re-IDENTIFY:
``parse_ready`` clears ``_guilds`` and re-adds every guild as an UNAVAILABLE
stub with no threads, while ``Client.is_ready()`` is still true from the
previous connection (it is only cleared by ``close()`` and ``clear()``). For the
seconds the GUILD_CREATE stream takes, every live ticket would read as
unreachable. So the sweep also refuses an ``unavailable`` guild, which is
exactly the flag those stubs carry.

Scale story (1000+ guilds). Steady state is still zero: no per-ticket timer, no
per-message write, and no work at all in the common listener paths - a thread
archiving anywhere in the world is filtered by ONE cached settings read (is this
that guild's ticket channel?) before any query is considered. The sweep is one
indexed statement per hour, bounded at 50 rows, with an in-memory cursor that
rotates through the open set so a guild whose tickets are all healthy cannot
starve a guild whose ticket is stale.

ONE close costs at most ten history pages, one UPDATE and two sends. The number
that matters at this scale is how many closes can run AT ONCE, and only one path
can fan out: tickets that went quiet together archive together, so the archive
listener could start an unbounded number of transcript reads in the same second.
That is what :data:`_ARCHIVE_CLOSE_LIMIT` bounds. The other paths are bounded by
construction - the sweep closes its batch one row at a time, a click is one
person - and :data:`_CLOSING` keeps two closes of the SAME ticket from both
paying for a transcript before the database refuses one of them.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

import discord
from discord.ext import commands, tasks

from . import guild_config, storage, transcripts
from tools import i18n, interactions
from tools.formats import format_dt, random_colour
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)


# The ``tk:`` namespace open.py reserved. Disjoint literal prefixes, so
# discord.py's fullmatch dispatch can never route a close click to claim; the
# id part is a thread snowflake, so the whole custom_id stays far inside the
# 100-character limit.
CLOSE_TEMPLATE = r"tk:close:(?P<tid>\d+)"
CLAIM_TEMPLATE = r"tk:claim:(?P<tid>\d+)"

# Discord's cap on a thread name; the claim rename is trimmed to it.
MAX_THREAD_NAME = 100

# The sweep. One pass an hour, at most this many rows per pass - see the module
# docstring for why both numbers are small and why the cursor rotates.
SWEEP_INTERVAL_HOURS = 1
SWEEP_BATCH = 50

# Why a ticket ended. Only ``manual`` names a person; the other two are what the
# log summary says instead of a name, and all three write ``closed_by = NULL``
# except the first.
REASON_MANUAL = "manual"
REASON_INACTIVITY = "inactivity"
REASON_DELETED = "deleted"

# Ticket threads whose close is running RIGHT NOW, as thread ids.
#
# NOT the exactly-once gate - that is storage.close_ticket's ``AND status =
# 'open'``, and it stays the authority because it is the only one that spans
# processes. This is the cheap in-process half: without it, a double-confirmed
# close (or a click landing exactly on the archive listener) has BOTH callers
# read up to ten pages of history and render a transcript before the database
# tells the loser to throw its copy away. Every add has a matching discard in a
# finally, so the set holds one entry per close in flight - a handful at 1000+
# guilds - and losing it on a restart costs nothing.
_CLOSING = set()

# How many archive-triggered closes may run at once, process-wide.
#
# The one place this feature can fan out: a cohort of tickets that went quiet
# together archives together, and each close reads history. Four at a time keeps
# a quiet Sunday morning from spending the global REST budget on transcripts,
# and closes are not urgent - the row is already the truth the moment its
# UPDATE lands, and everything the semaphore delays is the artefact around it.
_ARCHIVE_CLOSE_LIMIT = 4
_ARCHIVE_CLOSES = asyncio.Semaphore(_ARCHIVE_CLOSE_LIMIT)

# What :func:`perform_close` answers when the close FAILED, as opposed to the
# ``None`` that means somebody else closed this ticket first.
#
# The two used to be the same answer, and that was a real bug with a real
# victim: a close whose UPDATE raised told the clicker "this ticket is already
# closed", so nobody retried and the room stayed live on an open row, holding
# its opener's cap slot behind a button that had apparently already worked.
# "Somebody else did it" and "it did not happen" are opposite facts and the
# person who clicked has to be told which one they got.
CLOSE_FAILED = object()


# ---------------------------------------------------------------------------
# The in-thread controls
# ---------------------------------------------------------------------------


class TicketCloseButton(
    discord.ui.DynamicItem[discord.ui.Button], template=CLOSE_TEMPLATE
):
    """Persistent Close button: opener or support, behind a confirm."""

    def __init__(self, thread_id):
        self.thread_id = int(thread_id)
        super().__init__(
            discord.ui.Button(
                label=_("Close ticket"),
                style=discord.ButtonStyle.danger,
                emoji="\N{LOCK}",
                custom_id="tk:close:{tid}".format(tid=int(thread_id)),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        await _run_close_click(interaction, self.thread_id)


class TicketClaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=CLAIM_TEMPLATE
):
    """Persistent Claim button: support only, once, never by the opener."""

    def __init__(self, thread_id):
        self.thread_id = int(thread_id)
        super().__init__(
            discord.ui.Button(
                label=_("Claim"),
                style=discord.ButtonStyle.secondary,
                emoji="\N{RAISED HAND}",
                custom_id="tk:claim:{tid}".format(tid=int(thread_id)),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        await _run_claim(interaction, self.thread_id)


class TicketControlsView(discord.ui.View):
    """The two buttons that ride the ticket's opening message.

    ``timeout=None`` because these outlive the process: the buttons dispatch
    through the DynamicItem registry (:meth:`TicketLifecycle.cog_load`), not
    through a stored view, so a ticket opened before a restart still works.
    """

    def __init__(self, thread_id):
        super().__init__(timeout=None)
        self.add_item(TicketClaimButton(thread_id))
        self.add_item(TicketCloseButton(thread_id))


class _CloseConfirmView(AuthorView):
    """Ephemeral "really close it?" prompt for one clicker.

    Both buttons are built in ``__init__`` - which runs inside the click's task,
    where the clicker's locale is set - so the labels are translated, unlike a
    class-level decorator whose label is frozen at import.
    """

    def __init__(self, author_id, thread_id, number, *, timeout=60):
        super().__init__(
            author_id,
            timeout=timeout,
            deny_message="This prompt isn't for you.",
        )
        self.thread_id = int(thread_id)
        self.number = number
        confirm = discord.ui.Button(
            label=_("Close ticket"), style=discord.ButtonStyle.danger
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label=_("Cancel"), style=discord.ButtonStyle.secondary
        )
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction):
        # Answer the click FIRST (the prompt becomes a plain "closing..." line
        # with no buttons), then do the work. The close is several round trips
        # long, and an interaction left unanswered for that long expires.
        self.stop()
        try:
            await interaction.response.edit_message(
                content=_("Closing ticket #{number}...").format(number=self.number),
                view=None,
            )
        except discord.HTTPException:
            log.debug("tickets: could not update the close prompt", exc_info=True)
        await _confirmed_close(interaction, self.thread_id)

    async def _cancel(self, interaction):
        self.stop()
        try:
            await interaction.response.edit_message(
                content=_("The ticket stays open."), view=None
            )
        except discord.HTTPException:
            log.debug("tickets: could not clear the close prompt", exc_info=True)


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


async def is_support(member, pool):
    """True for the configured support role, or anyone with Manage Server.

    The support role is the one T1 defined (``guild_config.support_role_id``) -
    the same role the opening message pings - so "staff" means one thing across
    the feature. Manage Server is the standing fallback: a server that never set
    a support role still has people who can act, and a server that did keeps its
    admins able to.
    """
    if member is None:
        return False
    if getattr(member.guild_permissions, "manage_guild", False):
        return True
    role_id = await guild_config.support_role_id(pool, member.guild.id)
    if not role_id:
        return False
    return member.get_role(role_id) is not None


def claimed_thread_name(number, display_name):
    """``ticket-<n>-<claimer>``, trimmed to Discord's 100-character cap. Pure.

    Whitespace in the name is collapsed (a display name can contain newlines,
    which a thread name cannot), and a name that collapses to nothing degrades
    to the plain ``ticket-<n>`` rather than to a trailing dash.
    """
    base = "ticket-{number}".format(number=number)
    slug = " ".join(str(display_name or "").split())
    if not slug:
        return base
    return "{base}-{slug}".format(base=base, slug=slug)[:MAX_THREAD_NAME]


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


async def _run_claim(interaction, thread_id):
    """Take the ticket for the clicker, or say precisely why not."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return await interactions.reply(
            interaction, _("Tickets only work inside a server.")
        )

    pool = getattr(interaction.client, "db_pool", None)
    if not await is_support(member, pool):
        return await interactions.reply(
            interaction, _("Only the support team can claim a ticket.")
        )

    try:
        row = await storage.claim_ticket(pool, thread_id, member.id)
    except Exception:
        log.exception("tickets: claim failed in guild %s", guild.id)
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )

    if row is None:
        return await interactions.reply(
            interaction, _("This thread is not a ticket.")
        )

    number = row["ticket_number"]
    if not row["taken"]:
        # The statement already told us which rule refused, in the order the
        # rules are checked: closed beats claimed beats "that is your own".
        if row["status"] != "open":
            return await interactions.reply(
                interaction, _("Ticket #{number} is already closed.").format(
                    number=number
                )
            )
        if row["claimed_by"]:
            return await interactions.reply(
                interaction,
                _("Ticket #{number} is already claimed by {user}.").format(
                    number=number, user="<@{0}>".format(row["claimed_by"])
                ),
            )
        return await interactions.reply(
            interaction, _("You cannot claim your own ticket.")
        )

    # ANSWER THE CLICK FIRST. The rename below is the one call in this feature
    # that can sit on a rate limit for minutes - Discord allows two thread
    # renames per ten minutes, and the open flow already spent one naming this
    # thread ``ticket-<n>`` - and discord.py handles that by sleeping on the
    # bucket. Renaming before replying would let a claim expire the interaction
    # and show the clicker a failure for something that did work.
    await interactions.reply(
        interaction,
        _("Ticket #{number} is yours.").format(number=number),
    )

    thread = _resolve_thread(interaction, guild, thread_id)
    if thread is None:
        return
    try:
        await thread.send(
            _("{user} is taking care of this ticket.").format(user=member.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.debug("tickets: could not announce the claim", exc_info=True)

    try:
        await thread.edit(
            name=claimed_thread_name(number, member.display_name),
            reason=f"Ticket #{number} claimed by {member} ({member.id})",
        )
    except discord.HTTPException:
        # Cosmetic. The claim is recorded and the announcement above names the
        # claimer, so a thread that keeps its old name hides nothing.
        log.warning("tickets: could not rename claimed thread %s", thread_id)


def _resolve_thread(interaction, guild, thread_id):
    """The thread this click happened in, from the click or from the cache."""
    channel = getattr(interaction, "channel", None)
    if isinstance(channel, discord.Thread) and channel.id == int(thread_id):
        return channel
    return guild.get_thread(int(thread_id))


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


async def _run_close_click(interaction, thread_id):
    """Check who is asking, then put a confirm in front of them."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return await interactions.reply(
            interaction, _("Tickets only work inside a server.")
        )

    pool = getattr(interaction.client, "db_pool", None)
    try:
        row = await storage.fetch_by_thread(pool, thread_id)
    except Exception:
        log.exception("tickets: close lookup failed in guild %s", guild.id)
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )

    if row is None:
        return await interactions.reply(
            interaction, _("This thread is not a ticket.")
        )
    if row["status"] != "open":
        return await interactions.reply(
            interaction,
            _("Ticket #{number} is already closed.").format(
                number=row["ticket_number"]
            ),
        )

    allowed = member.id == int(row["opener_id"]) or await is_support(member, pool)
    if not allowed:
        return await interactions.reply(
            interaction,
            _("Only the member who opened this ticket, or the support team, can close it."),
        )

    await _close_prompt(interaction, thread_id, row["ticket_number"])


async def _close_prompt(interaction, thread_id, number):
    """The ephemeral confirm. One extra click, and it is worth it.

    Close is destructive from the member's point of view - the room they were
    being helped in locks - and the button sits in the same message as the
    opening summary, where a stray tap is easy.
    """
    view = _CloseConfirmView(interaction.user.id, thread_id, number)
    try:
        await interaction.response.send_message(
            _("Close ticket #{number}? The thread will be archived and locked.").format(
                number=number
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
    except discord.HTTPException:
        log.warning("tickets: could not send the close prompt", exc_info=True)


async def _confirmed_close(interaction, thread_id):
    """Run the close flow for a confirmed click, and report it to the clicker."""
    guild = interaction.guild
    bot = interaction.client
    pool = getattr(bot, "db_pool", None)

    try:
        row = await storage.fetch_by_thread(pool, thread_id)
    except Exception:
        log.exception("tickets: close re-read failed")
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )
    if row is None or row["status"] != "open":
        return await interactions.reply(
            interaction, _("This ticket is already closed.")
        )

    thread = _resolve_thread(interaction, guild, thread_id)
    closed = await perform_close(
        bot, guild, thread, row, closed_by=interaction.user, reason=REASON_MANUAL
    )
    if closed is CLOSE_FAILED:
        # The close did NOT happen: the ticket is still open, the room is still
        # live and the cap slot is still held. Say so, because the only thing
        # that fixes it is somebody clicking again - and a clicker told "already
        # closed" never does.
        return await interactions.reply(
            interaction,
            _(
                "I could not close ticket #{number}. Nothing has changed, so "
                "please try again."
            ).format(number=row["ticket_number"]),
        )
    if closed is None:
        return await interactions.reply(
            interaction, _("This ticket is already closed.")
        )
    await interactions.reply(
        interaction,
        _("Ticket #{number} is closed.").format(number=row["ticket_number"]),
    )


async def perform_close(bot, guild, thread, row, *, closed_by, reason):
    """Close a ticket end to end. Returns the closed row, ``None``, or
    :data:`CLOSE_FAILED`.

    ``None`` means somebody else closed it first and this caller must do nothing
    - see storage.close_ticket, which is the mutual exclusion for every step
    below it. :data:`CLOSE_FAILED` means the close did not happen at all: the
    row is still open and a caller that reports to a human must say so rather
    than claim the ticket ended. ``thread`` may be ``None`` (the sweep and the
    deleted-thread path have no object to act on), and an ARCHIVED thread is
    left archived.

    The step order and the reasoning for it are in the module docstring; this
    function is that list, in that order.

    The :data:`_CLOSING` guard around it answers the same ``None`` for a close
    that is already running in this process, BEFORE the transcript is paid for.
    It is a cost guard, not a correctness one: the database gate below is what
    actually makes a close exactly-once.
    """
    thread_id = int(row["thread_id"])
    if thread_id in _CLOSING:
        return None
    _CLOSING.add(thread_id)
    try:
        return await _close_now(
            bot, guild, thread, row, closed_by=closed_by, reason=reason
        )
    finally:
        _CLOSING.discard(thread_id)


async def _close_now(bot, guild, thread, row, *, closed_by, reason):
    """The close itself, the five steps in order. See :func:`perform_close`."""
    pool = getattr(bot, "db_pool", None)
    thread_id = int(row["thread_id"])
    number = row["ticket_number"]
    locale_code = await i18n.resolve_guild_locale(bot, guild)
    channel = await _log_channel(guild, pool)

    # 1. The transcript, from the live thread, and ONLY when there is a log
    # channel to receive it AND we may attach a file there: no destination, no
    # transcript, ever. The attachment permission is tested HERE rather than
    # only at the send for two reasons - a channel that refuses files costs no
    # history pages at all, and the closing notice below can then state whether
    # a transcript was saved without ever claiming one that could not land.
    can_attach = channel is not None and _can_attach(channel)
    transcript = None
    if thread is not None and can_attach:
        with i18n.locale(locale_code):
            header = _transcript_header(guild, row)
        transcript = await transcripts.build(
            thread,
            header=header,
            filename="ticket-{number}-transcript.txt".format(number=number),
        )

    # 2. The row. Exactly-once gate for everything after it.
    try:
        closed = await storage.close_ticket(
            pool, thread_id, getattr(closed_by, "id", None)
        )
    except Exception:
        # NOT the same answer as the gate below: this ticket is still OPEN. See
        # CLOSE_FAILED - a caller reporting to a human has to be able to tell
        # "somebody beat you to it" from "it did not happen".
        log.exception("tickets: closing ticket %s failed", thread_id)
        return CLOSE_FAILED
    if closed is None:
        return None

    # 3 and 4. Talk in the room, then shut it. Both skipped for a thread that is
    # gone, and for one Discord already archived (which cannot be posted in, and
    # whose only accepted edit is the unarchive this flow deliberately refuses).
    if thread is not None and not thread.archived:
        # The GUILD's locale, not the closer's: this line is read by everybody
        # in the room, the same reasoning that renders the panel per guild.
        with i18n.locale(locale_code):
            await _post_closing_notice(
                thread,
                number,
                no_log_channel=channel is None,
                transcript_saved=transcript is not None,
            )
        try:
            await thread.edit(
                archived=True,
                locked=True,
                reason=f"Ticket #{number} closed",
            )
        except discord.HTTPException:
            log.warning("tickets: could not archive thread %s", thread_id)

    # 5. The log line, with the transcript attached.
    if channel is not None:
        with i18n.locale(locale_code):
            await _post_log_summary(
                channel,
                closed,
                closed_by=closed_by,
                reason=reason,
                transcript=transcript,
                had_thread=thread is not None,
                can_attach=can_attach,
            )
    return closed


async def _post_closing_notice(thread, number, *, no_log_channel, transcript_saved):
    """The last message in the room, so the lock is not a surprise.

    BOTH answers are stated, and that symmetry is the point. The room used to be
    told only when NO transcript was kept, which left the loud case silent: the
    person being helped learned that their conversation was NOT filed anywhere,
    and never learned when a copy of it had just been sent to a staff channel
    they cannot see. A privacy notice that only fires when there is nothing to
    disclose is not a notice.
    """
    text = _("Ticket #{number} is closed. Thanks for reaching out.").format(
        number=number
    )
    if no_log_channel:
        text += "\n-# " + _(
            "This server has no ticket log channel, so no transcript was saved."
        )
    elif transcript_saved:
        # Only when one was really built, for a channel this bot may post in
        # AND attach to - all three tested before this line runs (see
        # _close_now). What is left is a transient failure on the send itself,
        # which is logged as a warning; erring towards "we told you" is the
        # right side to err on for a disclosure.
        text += "\n-# " + _(
            "A transcript of this conversation was saved to this server's "
            "ticket log channel, where its staff team keeps it."
        )
    try:
        await thread.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.debug("tickets: could not post the closing notice", exc_info=True)


def _transcript_header(guild, row):
    """The few context lines at the top of the transcript file.

    Translated (the guild's locale, set by the caller) because a human reads
    them; the message lines below are not, because they are a record.
    """
    opened = row["opened_at"]
    opener_id = int(row["opener_id"])
    member = guild.get_member(opener_id)
    opener = "{0} ({1})".format(member, opener_id) if member else str(opener_id)
    return [
        _("Transcript of ticket #{number}").format(number=row["ticket_number"]),
        _("Server: {guild}").format(guild="{0} ({1})".format(guild.name, guild.id)),
        _("Opened by {user} on {date}").format(
            user=opener,
            date=opened.strftime(transcripts.TIMESTAMP_FORMAT) if opened else "?",
        ),
        _("Taken on {date}").format(
            date=discord.utils.utcnow().strftime(transcripts.TIMESTAMP_FORMAT)
        ),
        _("Attachment links are Discord CDN links and stop working after a while."),
        "-" * 60,
    ]


async def _log_channel(guild, pool):
    """The configured log channel, if it exists and the bot can post there."""
    channel_id = await guild_config.log_channel_id(pool, guild.id)
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return None
    me = guild.me
    if me is None:
        return None
    perms = channel.permissions_for(me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        log.warning(
            "tickets: cannot post the ticket log in guild %s channel %s",
            guild.id,
            channel.id,
        )
        return None
    return channel


def _can_attach(channel):
    """Whether the bot may attach the transcript file in this log channel.

    Its own function because two callers need the SAME answer: _close_now
    decides whether to read the thread at all (and what the room is told), and
    the log summary decides whether the file rides the embed.
    """
    me = channel.guild.me
    return me is not None and channel.permissions_for(me).attach_files


async def _post_log_summary(
    channel, closed, *, closed_by, reason, transcript, had_thread, can_attach
):
    """One embed per closed ticket, with the transcript attached to it.

    Mentions are rendered as ``<@id>`` but the send pins
    ``AllowedMentions.none()``: a log line is a record, and pinging the opener
    of every closed ticket in a staff channel would be a notification storm for
    nothing.
    """
    embed = discord.Embed(
        title=_("Ticket #{number} closed").format(number=closed["ticket_number"]),
        colour=random_colour(),
    )
    embed.add_field(
        name=_("Opened by"), value="<@{0}>".format(closed["opener_id"]), inline=True
    )
    embed.add_field(
        name=_("Claimed by"),
        value=(
            "<@{0}>".format(closed["claimed_by"])
            if closed["claimed_by"]
            else _("Nobody")
        ),
        inline=True,
    )
    embed.add_field(name=_("Ended by"), value=_ending(closed_by, reason), inline=True)
    embed.add_field(
        name=_("Opened"), value=format_dt(closed["opened_at"], "f"), inline=True
    )
    embed.add_field(
        name=_("Closed"), value=format_dt(closed["closed_at"], "f"), inline=True
    )

    file = transcript if (transcript is not None and can_attach) else None
    if file is None:
        embed.set_footer(text=_no_transcript_note(had_thread, can_attach))
    else:
        # Say what the attachment IS, on every close. A server that configured a
        # log channel is not asked again, so this line is the notice: the file
        # is the private thread's conversation, and from here it lives in this
        # channel rather than dying with the thread.
        embed.set_footer(
            text=_(
                "Transcript attached: what was said in the ticket thread. It "
                "stays in this channel."
            )
        )

    try:
        await channel.send(
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.warning(
            "tickets: could not post the ticket log in %s", channel.id, exc_info=True
        )


def _ending(closed_by, reason):
    """Who or what ended the ticket, for the log embed.

    A name only ever appears for a close somebody clicked; every automatic path
    writes ``closed_by = NULL`` and says what happened instead.
    """
    if reason == REASON_DELETED:
        return _("The thread was deleted.")
    if reason == REASON_MANUAL and closed_by is not None:
        return "<@{0}>".format(closed_by.id)
    return _("Auto-closed after inactivity.")


def _no_transcript_note(had_thread, can_attach):
    """Why the embed has no file attached - never silence.

    Two facts decide it and the transcript itself is not one of them, which is
    the point of the ordering: ``can_attach`` is tested BEFORE the read now (see
    _close_now), so a channel that refuses files leaves the transcript None as
    well, and asking about it would blame a thread this path never even opened.
    The caller has already established there is no file to attach; these two
    answer WHY.
    """
    if not had_thread:
        return _("No transcript: the thread was already gone.")
    if not can_attach:
        return _("No transcript: I cannot attach files in this channel.")
    # The only case left: a thread we could open a file for and failed to read.
    return _("No transcript: I could not read the thread.")


# ---------------------------------------------------------------------------
# The cog: registration, the two listeners, and the sweep
# ---------------------------------------------------------------------------


class TicketLifecycle(commands.Cog):
    """Ticket endings: the in-thread controls, the listeners and the sweep."""

    def __init__(self, bot):
        self.bot = bot
        # Rotating sweep cursor (a ticket row id). In memory on purpose: it is a
        # fairness hint, not state - losing it on a restart only means the scan
        # begins at the oldest open row again, which is where it would have gone
        # anyway once it wrapped.
        self._sweep_cursor = 0
        self.sweep_stale_tickets.start()

    async def cog_load(self):
        # Process-wide registration, so a click on a ticket opened before this
        # start still dispatches - the whole point of DynamicItem.
        try:
            self.bot.add_dynamic_items(TicketClaimButton, TicketCloseButton)
        except Exception:
            log.exception("tickets: failed to register the in-thread controls")

    def cog_unload(self):
        self.sweep_stale_tickets.cancel()
        # Drop the registration so a reload does not leave a stale template
        # behind; the next cog_load adds it again.
        try:
            self.bot.remove_dynamic_items(TicketClaimButton, TicketCloseButton)
        except Exception:
            log.debug("tickets: could not unregister the controls", exc_info=True)

    # -- listeners ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_update(self, before, after):
        """Discord archived a thread: if it was an open ticket, that is a close.

        The FLIP is the signal, not the state: an edit to an already-archived
        thread must not re-run anything. Everything else returns before any I/O,
        and the ticket-channel test below is a cached settings read, so a busy
        server's threads archiving all day cost this listener no queries.
        """
        if not after.archived or before.archived:
            return
        try:
            await self._close_archived(after)
        except Exception:
            log.exception("tickets: auto-close on archive failed")

    async def _close_archived(self, thread):
        guild = thread.guild
        pool = getattr(self.bot, "db_pool", None)
        panel_channel = await guild_config.panel_channel_id(pool, guild.id)
        if not panel_channel or thread.parent_id != int(panel_channel):
            # Not this guild's ticket channel (or tickets are off). A ticket
            # whose panel MOVED since it was opened lands here too and is left
            # to the sweep, which does not care where the thread lives.
            return
        row = await storage.fetch_by_thread(pool, thread.id)
        if row is None or row["status"] != "open":
            return
        # The only fan-out in the feature: tickets that went quiet together
        # archive together, and each close reads history. Held around the close
        # ONLY - the guards above stay free for the thousands of threads that
        # archive somewhere and are not tickets.
        async with _ARCHIVE_CLOSES:
            await perform_close(
                self.bot, guild, thread, row, closed_by=None,
                reason=REASON_INACTIVITY,
            )

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload):
        """A deleted ticket thread must not keep holding its opener's cap slot.

        RAW because discord.py only dispatches the rich ``on_thread_delete`` for
        a thread still in cache, and an archived ticket thread is exactly the
        one that is not.
        """
        try:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return
            pool = getattr(self.bot, "db_pool", None)
            panel_channel = await guild_config.panel_channel_id(pool, guild.id)
            if not panel_channel or payload.parent_id != int(panel_channel):
                return
            row = await storage.fetch_by_thread(pool, payload.thread_id)
            if row is None or row["status"] != "open":
                return
            await perform_close(
                self.bot, guild, None, row, closed_by=None, reason=REASON_DELETED
            )
        except Exception:
            log.exception("tickets: closing a deleted ticket thread failed")

    # -- the sweep ---------------------------------------------------------

    @tasks.loop(hours=SWEEP_INTERVAL_HOURS)
    async def sweep_stale_tickets(self):
        try:
            await self.run_sweep_once()
        except Exception:
            # A pass that raises must not kill the loop: the next hour's pass
            # re-reads from the same cursor and sees the same rows.
            log.exception("tickets: inactivity sweep failed")

    @sweep_stale_tickets.before_loop
    async def _before_sweep(self):
        # READY is what makes the thread cache trustworthy: GUILD_CREATE carries
        # the active threads the bot can see, and the sweep's whole liveness test
        # is "is this thread in that cache".
        await self.bot.wait_until_ready()

    async def run_sweep_once(self):
        """One bounded pass. Returns how many tickets it closed (for tests)."""
        pool = getattr(self.bot, "db_pool", None)
        if pool is None or not self.bot.is_ready():
            return 0

        rows = await storage.fetch_sweep_candidates(
            pool,
            after_id=self._sweep_cursor,
            min_age_hours=guild_config.MIN_INACTIVITY_HOURS,
            limit=SWEEP_BATCH,
        )
        # Advance the cursor over what we just looked at; a short batch means we
        # reached the end of the open set, so the next pass starts over.
        if len(rows) < SWEEP_BATCH:
            self._sweep_cursor = 0
        else:
            self._sweep_cursor = int(rows[-1]["id"])

        now = discord.utils.utcnow()
        closed = 0
        for row in rows:
            try:
                if await self._sweep_row(row, now, pool):
                    closed += 1
            except Exception:
                log.exception(
                    "tickets: sweeping ticket row %s failed", row["id"]
                )
        if closed:
            log.info("tickets: inactivity sweep closed %s tickets", closed)
        return closed

    async def _sweep_row(self, row, now, pool):
        """Close one row if it is both unreachable and past its guild's window."""
        guild = self.bot.get_guild(int(row["guild_id"]))
        if guild is None:
            # Not a guild this process can see. Never act blind - a shard we do
            # not own, or a guild we left (whose rows retention deletes).
            return False
        if guild.unavailable:
            # A guild we hold as a STUB, not one we know about. Two ways to get
            # here and both are the same trap: an outage (GUILD_DELETE with
            # unavailable) and, far more dangerous, a re-IDENTIFY - parse_ready
            # re-adds every guild as an unavailable, thread-less stub while
            # is_ready() is still true from the previous connection. Its
            # ``_threads`` is empty, so EVERY live ticket in it would read as
            # unreachable and a pass landing in that window would close the lot.
            # See the module docstring; there is no reopen to undo it with.
            return False
        if guild.get_thread(int(row["thread_id"])) is not None:
            # In the ACTIVE-thread cache: the room is alive, so the ticket is.
            return False

        hours = await guild_config.inactivity_hours(pool, guild.id)
        if now - row["opened_at"] < datetime.timedelta(hours=hours):
            # Unreachable but young. Could be a thread archived early, could be
            # a cache we do not trust yet; either way the guild's own window has
            # not passed, so leave it for a later pass.
            return False

        closed = await perform_close(
            self.bot, guild, None, row, closed_by=None, reason=REASON_INACTIVITY
        )
        # A close that failed is not a close: the row stays open, the next pass
        # sees it again, and the pass log must not count it. ``None`` still
        # counts - somebody else closed it, so the ticket did end.
        return closed is not CLOSE_FAILED
