"""Top.gg vote rewards: the ledger a vote writes, and everything built on it.

V1 was SILENT INFRASTRUCTURE: no command, no DM, no embed. V2 (this file, same
module) adds every surface a member actually sees, on top of the exact same
table and the exact same upsert - nothing about how a vote is BANKED changed:

* ``/vote`` - a card showing the link, current boost, streak and lifetime
  votes (see :class:`VoteStatusView`).
* A lazy catch-up poll at ``/vote`` open, for the rare vote our webhook missed
  (see :meth:`Votes._maybe_catch_up`).
* A best-effort thank-you DM on a real vote (see :meth:`Votes._send_thank_you`).
* An opt-in "you can vote again" reminder, 12h after a real vote, through the
  existing one-shot timers table (see :meth:`Votes._schedule_reminder`).
* A "Supporter" badge on the profile card for a recent voter (see
  :func:`is_recent_supporter`, read from ``cogs/community/profile/cog.py``).

Where a vote comes from: top.gg POSTs it to the hardened webhook in
cogs/system/webstats.py, which dispatches ``dbl_vote`` with a ``BotVoteData``.
That cog's own listener still logs every vote and is untouched - discord.py
delivers the event to EVERY registered listener, so the two coexist and neither
is the other's fallback.

THE ADDITIVE RULE this lot is built on: a member who does not vote must never
hit a closed door. Nothing here takes anything away or gates anything - the only
effect of a vote is that the voter's XP grants are multiplied for a while. A
non-voter's experience is byte-for-byte what it was before this file existed.

Two facts about the payload, both verified against the installed topgg lib
(topgg/types.py) rather than its annotations:

* ``data["user"]`` is a STRING even though ``VoteDataDict`` annotates it as an
  int - ``parse_vote_dict`` only int-converts ``bot``/``guild``. It is int()ed
  here, once, at the boundary.
* ``data["type"]`` is ``"upvote"`` for a real vote and ``"test"`` for the button
  on the bot's own top.gg edit page. A test vote is logged and dropped: it is an
  operator poking the webhook, and it must never write a streak or hand out XP.

The type is checked as an ALLOWLIST (``!= "upvote"``), not by dropping the one
known-bad value: this listener WRITES, so anything whose meaning we do not know
must fall through to a log line rather than be banked as a real vote. That is
the one place it deliberately diverges from the webstats logger, which only ever
prints. It also makes a protocol change loud instead of silent: top.gg's newer
v1 webhooks send ``type: "vote.create"`` with the voter under ``data.user.id``,
so a migration would show up here as a warning per vote (and zero writes)
instead of as a ledger quietly filling with nothing.
"""

import datetime
import logging

import discord
from discord.ext import commands

from tools import i18n, settings
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _
from tools.quotas import SlidingWindowQuota

log = logging.getLogger(__name__)

# The widest value topgg_votes.user_id (BIGINT) can hold. A payload id outside
# it is not a Discord snowflake, and refusing it here keeps the write path free
# of an exception that only a malformed body could ever cause.
MAX_SNOWFLAKE = (1 << 63) - 1

# How long one vote's XP boost lasts. top.gg lets a user vote every 12h, so the
# weekday value is exactly "until you could vote again": a voter who keeps
# voting is continuously boosted, and one who stops is back to normal the moment
# their next vote was due. top.gg counts WEEKEND votes double, so a weekend vote
# gets double the window rather than a second, invisible kind of reward.
BOOST_HOURS = 12
WEEKEND_BOOST_HOURS = 24

# How long a streak survives without a vote. Not the vote interval (top.gg
# enforces that itself); this is only the point at which a streak BREAKS, so a
# voter who comes back inside a day never loses one to an hour's drift.
STREAK_WINDOW_HOURS = 24

# The replay floor. top.gg lets one user vote every 12h, so two GENUINE votes
# are never closer together than that - anything landing within an hour of this
# user's previous vote is the SAME vote arriving twice (a redelivery, a replayed
# capture, an operator curling the endpoint with a saved body) and must change
# nothing at all. The v0 payload this bot consumes carries no vote id, so there
# is nothing to deduplicate ON; the row's own timestamp is the only evidence
# available, and this window is twelve times under the vote floor and orders of
# magnitude above any plausible redelivery delay.
#
# Why it matters more than it looks: the webhook is a PUBLIC endpoint, and while
# the password gate and webstats' per-IP throttle bound who can reach it, a
# double delivery used to mean a real streak silently gaining a step it was
# never voted for. The ledger is meant to be the truth about how many times
# somebody voted, so the fix belongs in the statement, not in the caller.
REPLAY_WINDOW_HOURS = 1

# ONE statement does the entire bookkeeping for a vote: the row appears on the
# first vote and is updated on every later one, with the streak decided by the
# row's OWN previous timestamp inside the same statement. That matters for more
# than elegance - a read-then-write would let two deliveries interleave between
# the read and the write and both compute the same "next" streak. Here the two
# serialise on the row lock and the loser recomputes against what the winner
# actually wrote.
#
# $1 user id, $2 boost hours (12, or 24 on a weekend), $3 the streak window,
# $4 the replay floor, $5 whether THIS write is a catch-up (see below).
# ``now()`` is the transaction timestamp, so every column of
# a given vote is stamped from ONE clock reading - last_vote_at and
# boost_expires_at can never disagree about when the vote happened.
#
# THE REPLAY BRANCH, repeated in each SET because SQL has no way to say "leave
# this row alone" conditionally: when the previous vote is less than $4 old, all
# five columns keep the value they already had, which makes a redelivery a true
# no-op rather than a free streak step. The predicate is bounded BELOW by zero
# on purpose - a last_vote_at in the FUTURE (a clock stepping backwards) is not
# a replay, and must fall through to the streak rules so the next real vote
# re-stamps the row from the current clock instead of freezing the ledger until
# the future timestamp is reached.
#
# THE SECOND HALF OF THAT PREDICATE, ``$5 OR NOT topgg_votes.caught_up``, is what
# keeps the replay floor from eating a REAL vote (see CATCHUP_STALE_HOURS for the
# other side of it). The catch-up poll banks a vote on the evidence "top.gg says
# this member voted some time in the last 12h" and can only stamp last_vote_at =
# now() for it, so the row may claim a vote is minutes old when it is really
# nearly twelve hours old. A member who then votes again for real - which top.gg
# allows the moment their true 12h is up - would have that genuine webhook
# delivery land inside the one-hour floor and be discarded: no streak step, no
# lifetime count, no thank-you. So a row STAMPED BY A CATCH-UP is soft evidence,
# and a webhook delivery ($5 false) landing on one is treated as the real thing:
# it re-stamps the row, steps the streak, and clears the flag.
#
# The cost of that choice, stated plainly: when a webhook is merely SLOW and a
# catch-up beats it to the same vote, that delivery is now counted twice. It is
# bounded to exactly one extra count - the flag is cleared by the first webhook,
# so any further delivery inside the hour is a replay again - and it can only
# happen at all when the ledger already looked 12h stale, which is the rare case
# the catch-up exists for. One over-counted vote in that corner is the right
# trade against silently dropping genuine ones.
#
# A catch-up write itself ($5 true) is unaffected by the flag: it stays purely
# time-based against its own wider floor, so two catch-ups in a row can never
# bank two votes.
#
# Clock skew cannot explode a streak either way: a negative interval is <= the
# streak window, so it continues by exactly +1, and the same statement stamps
# last_vote_at = now(), so the skew is spent rather than compounded.
#
# boost_expires_at never moves BACKWARDS (GREATEST): a deadline a voter was
# already promised stays promised, whatever a later vote - or a later change to
# the durations above - would compute for it. A vote can only ever help.
#
# ``replayed`` is a LOG signal, not a decision: the branches above decide what
# the row becomes without consulting it. It reads "the statement left an older
# timestamp in place", which is exactly what a replay does - the only way it can
# read false for a replay is two deliveries landing on the same transaction
# timestamp to the microsecond, and even then the row is still correct, only the
# log line is quieter than it deserved.
RECORD_VOTE = """
INSERT INTO topgg_votes (
    user_id, last_vote_at, streak, total_votes, boost_expires_at, caught_up
)
VALUES ($1, now(), 1, 1, now() + $2 * INTERVAL '1 hour', $5)
ON CONFLICT (user_id) DO UPDATE SET
    last_vote_at = CASE
        WHEN (now() - topgg_votes.last_vote_at
              BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour')
             AND ($5 OR NOT topgg_votes.caught_up)
        THEN topgg_votes.last_vote_at
        ELSE now()
    END,
    streak = CASE
        WHEN (now() - topgg_votes.last_vote_at
              BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour')
             AND ($5 OR NOT topgg_votes.caught_up)
        THEN topgg_votes.streak
        WHEN now() - topgg_votes.last_vote_at <= $3 * INTERVAL '1 hour'
        THEN topgg_votes.streak + 1
        ELSE 1
    END,
    total_votes = CASE
        WHEN (now() - topgg_votes.last_vote_at
              BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour')
             AND ($5 OR NOT topgg_votes.caught_up)
        THEN topgg_votes.total_votes
        ELSE topgg_votes.total_votes + 1
    END,
    boost_expires_at = CASE
        WHEN (now() - topgg_votes.last_vote_at
              BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour')
             AND ($5 OR NOT topgg_votes.caught_up)
        THEN topgg_votes.boost_expires_at
        ELSE GREATEST(
            topgg_votes.boost_expires_at, now() + $2 * INTERVAL '1 hour'
        )
    END,
    caught_up = CASE
        WHEN (now() - topgg_votes.last_vote_at
              BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour')
             AND ($5 OR NOT topgg_votes.caught_up)
        THEN topgg_votes.caught_up
        ELSE $5
    END
RETURNING last_vote_at, streak, total_votes, boost_expires_at,
          last_vote_at < now() AS replayed
"""

# The two values $5 ever takes, named rather than spelled as bare booleans at
# the call sites: a vote that came in on the webhook, and one the /vote
# catch-up poll found. See RECORD_VOTE's comment for what the flag buys.
FROM_WEBHOOK = False
FROM_CATCHUP = True

# --- V2: user-facing surfaces built on the ledger above ---------------------

# How long a "Supporter" badge stays on the profile card after the last vote
# (cogs/community/profile/cog.py -> views._header_section). Deliberately keyed
# on last_vote_at, NOT boost_expires_at: the boost is gone within 12-24h, and a
# badge that flickered off that fast would read as broken rather than earned.
# A week gives a weekly voter (top.gg's own cadence hint) a badge that is
# always on.
SUPPORTER_WINDOW_DAYS = 7

# The lazy catch-up poll at `/vote` open (Votes._maybe_catch_up). Our own
# ledger is trusted first; top.gg is only asked when it looks stale enough
# that a webhook delivery may genuinely have been lost - never on every open.
#
# It is exactly top.gg's own vote window on purpose, and that is also what makes
# a double count impossible: their /check endpoint answers "did this user vote
# in the last 12 hours", so a ledger row younger than 12h can only be describing
# the very vote /check would confirm, and we never ask. The catch-up write then
# passes THIS value as the upsert's replay floor (see _maybe_catch_up) instead of
# the webhook's one-hour one, so the decision is re-made a second time against
# the DATABASE clock: even if this process' clock drifted far enough to call a
# fresh row stale, the statement itself still refuses to bank a second vote
# within 12h of the one it already has, and the row comes back `replayed`.
CATCHUP_STALE_HOURS = 12

# THE TWO LAYERS THE CATCH-UP POLL IS BOUNDED BY (the house shape - see
# cogs/anilist/throttle.py, which bounds the interactive AniList surface the
# same way).
#
# 1. FAIRNESS, per user: even a stale ledger is only ever checked against top.gg
#    once per window, so repeatedly reopening /vote cannot spend more than one
#    call per user per window.
CATCHUP_COOLDOWN_SECONDS = 60
# 2. CEILING, process-wide: the per-user half alone does not bound the TOTAL,
#    and "no row at all" reads as stale - so every first-ever /vote by a member
#    who has never voted (the common case, at 1000+ guilds) wants a poll. A
#    promo spike would queue hundreds of calls against the ONE top.gg token this
#    process holds, which the guild-count autopost in cogs/system/webstats.py
#    also depends on (top.gg allows on the order of 60 requests/minute for it).
#    Past this ceiling the poll is simply skipped: the card still renders from
#    our own ledger, and the only thing deferred is the rare catch-up of a vote
#    the webhook missed, which that member's next /vote picks up anyway.
CATCHUP_GLOBAL_LIMIT = 20
CATCHUP_GLOBAL_WINDOW = 60.0
# One shared bucket - the quota is keyed, so the process-wide window is just a
# single constant key (same idiom as anilist.throttle._GLOBAL_KEY).
_CATCHUP_GLOBAL_KEY = "votes:catchup"

# The opt-in "you can vote again" reminder (usersettings.PREFS' vote_reminder).
# Fixed at top.gg's own vote cooldown - the reminder fires the moment voting
# again becomes possible, regardless of which boost window (12h or the
# weekend's 24h) the vote that scheduled it happened to grant.
REMINDER_DELAY_HOURS = 12
# The preference key, shared with cogs/community/usersettings.py so the two
# modules read/write the exact same JSONB key without importing each other.
VOTE_REMINDER_PREF_KEY = "vote_reminder"
# The timer event name: a plain scoped DELETE-then-INSERT on `timers`, the same
# machinery reminders/tempbans already use (see cogs/community/reminders.py).
# NOT in reminders.DURABLE_TIMER_EVENTS - a missed "vote again" nudge is not
# worth retrying, so it takes the same at-most-once path as a normal reminder.
VOTE_REMINDER_EVENT = "vote_reminder"


def is_recent_supporter(last_vote_at, now):
    """True when ``last_vote_at`` falls inside the "Supporter" badge window.

    ``last_vote_at`` may be ``None`` (never voted) - reads as "not a
    supporter", never an error. Pure and side-effect free so both the profile
    card and its tests can call it directly against a plain datetime.
    """
    if last_vote_at is None:
        return False
    return now - last_vote_at <= datetime.timedelta(days=SUPPORTER_WINDOW_DAYS)


async def get_last_vote_at(pool, user_id):
    """This user's stored ``last_vote_at``, or ``None`` if they never voted.

    ONE indexed primary-key lookup (``topgg_votes.user_id`` is the PK) - the
    same cost class as the profile card's other reads. Used only to decide the
    "Supporter" badge; never raises into the card build (the caller is not
    wrapped here on purpose, so a real DB outage still surfaces the same way
    the card's other reads do, rather than silently hiding the badge).
    """
    return await pool.fetchval(
        "SELECT last_vote_at FROM topgg_votes WHERE user_id = $1", user_id
    )


def vote_url(bot_id):
    """The member-facing top.gg vote link for this bot."""
    return "https://top.gg/bot/{bot_id}/vote".format(bot_id=bot_id)


def forget_vote_boost(bot, user_id):
    """Drop a just-erased user's live XP boost from memory. Best effort.

    Called from the ONE path that erases the ledger row: `?mydata deleteprofile`
    (privacy.delete_user_data), behind its confirmation button. That statement
    deletes the topgg_votes row, but the boost the hot path reads is an
    IN-MEMORY map on the Leveling cog - deleting the row alone would leave a
    boost running until it expired on its own. This is the exact twin of
    presence.forget_collected_presence.

    Deliberately NOT called from `profile clear`: that command has no confirm
    step and no longer touches the vote ledger, so there is nothing there to
    keep in step (see privacy.PROFILE_DELETE_QUERIES vs USER_DELETE_QUERIES).

    It reaches into the LEVELING cog rather than this one because that is where
    the map lives: the vote feature owns what a boost MEANS, the leveling cog
    owns the dict because its hot path is the only thing that reads it (see
    Leveling._vote_boosts). Returns False and never raises when the cog is
    absent or the call fails - a failure here must not turn a completed erasure
    into an error message for the user. Nothing rests on it either: the entry is
    self-expiring, and no path re-arms it without a new vote.
    """
    getter = getattr(bot, "get_cog", None)
    if getter is None:
        return False
    try:
        cog = getter("Leveling")
        if cog is None:
            return False
        cog.forget_vote_boost(user_id)
    except Exception:
        log.exception("votes: could not drop the XP boost of %s", user_id)
        return False
    return True


class Votes(commands.Cog):
    """Records top.gg votes, arms the XP boost, and owns every V2 surface."""

    def __init__(self, bot):
        self.bot = bot
        # SCALE STORY: keyed by user_id, bounded the same way tools.rate_limit
        # bounds its per-IP buckets - a plain Cooldowns map that self-sweeps
        # past its size cap, so a flood of distinct users churns entries rather
        # than growing forever. Touched at most once per /vote open that
        # actually reaches the network (see _maybe_catch_up), which is already
        # rare: most opens see a fresh-enough row and never touch this map.
        self._catchup_cooldown = Cooldowns(CATCHUP_COOLDOWN_SECONDS)
        # The second layer (see CATCHUP_GLOBAL_LIMIT): one shared window for the
        # whole process, so no spike of DISTINCT members - each of whom passes
        # the per-user cooldown on their first open - can burn the top.gg token
        # budget the autopost shares. Single-key, so it holds exactly one entry.
        self._catchup_budget = SlidingWindowQuota(
            CATCHUP_GLOBAL_LIMIT, CATCHUP_GLOBAL_WINDOW
        )

    @commands.Cog.listener()
    async def on_dbl_vote(self, data):
        """Record one vote, then arm the voter's XP boost.

        Everything is guarded: a malformed payload, a database that refuses the
        write, or a leveling cog that is not loaded must each cost this listener
        a log line and nothing more. A vote is a gift - failing to bank one is
        never worth an exception in the event loop.
        """
        kind = data.get("type")
        if kind != "upvote":
            # ALLOWLIST, not a denylist. "test" is the button on the bot's own
            # top.gg edit page and is the expected visitor here; anything else
            # is a payload whose meaning this lot does not know. Both are
            # dropped for the same reason - neither is evidence that somebody
            # voted - but only the first one is routine, so only the other is
            # worth waking an operator up for.
            if kind == "test":
                log.info("votes: ignoring a top.gg test vote (%s)", data)
            else:
                log.warning(
                    "votes: ignoring a top.gg payload of unknown type %r "
                    "(has top.gg moved to v1 webhooks?)",
                    kind,
                )
            return

        raw_user = data.get("user")
        try:
            user_id = int(raw_user)
        except (TypeError, ValueError):
            log.warning("votes: vote payload with no usable user id: %r", raw_user)
            return
        if not 0 < user_id <= MAX_SNOWFLAKE:
            # A Discord id is a positive BIGINT. Anything else is not a user,
            # and handing it to asyncpg would raise on the column's range rather
            # than being refused here - one guarded log line instead of an
            # exception traceback per malformed payload.
            log.warning("votes: vote payload with an impossible user id: %r", raw_user)
            return

        # ``is_weekend`` is read with .get, not indexed: the lib only sets it
        # from the payload, and a hand-made or future payload that omits it must
        # degrade to a normal 12h boost rather than raise.
        hours = WEEKEND_BOOST_HOURS if data.get("is_weekend") else BOOST_HOURS

        try:
            row = await self.bot.db_pool.fetchrow(
                RECORD_VOTE,
                user_id,
                hours,
                STREAK_WINDOW_HOURS,
                REPLAY_WINDOW_HOURS,
                FROM_WEBHOOK,
            )
        except Exception:
            log.exception("votes: failed to record the vote of %s", user_id)
            return
        if row is None:  # defensive: the upsert always returns its row
            log.warning("votes: vote of %s recorded no row", user_id)
            return

        # The boost is armed either way. On a replay the deadline coming back is
        # the one the FIRST delivery stored (the statement left it alone), so
        # this costs nothing and covers the case where that first delivery could
        # not reach the leveling cog. Everything else (the thank-you DM, the
        # opt-in reminder) is gated on NOT replayed - see _apply_vote_row.
        armed = await self._apply_vote_row(user_id, row)
        if row["replayed"]:
            log.warning(
                "votes: ignored a redelivered vote for %s (streak stays %s, "
                "total stays %s, armed=%s)",
                user_id,
                row["streak"],
                row["total_votes"],
                armed,
            )
            return
        log.info(
            "votes: %s voted (streak %s, total %s, boost for %sh, armed=%s)",
            user_id,
            row["streak"],
            row["total_votes"],
            hours,
            armed,
        )

    async def _apply_vote_row(self, user_id, row):
        """Every side effect a freshly-recorded vote row triggers.

        Shared by :meth:`on_dbl_vote` (the webhook) and :meth:`_maybe_catch_up`
        (the lazy poll) - both hand this the exact ``RECORD_VOTE`` row and get
        the exact same treatment, so a vote recorded either way looks identical
        from here on. The boost is armed unconditionally (see the callers'
        comment on why a replay still re-arms it); the thank-you DM and the
        reminder are gated on NOT replayed, since a redelivery is the SAME vote
        arriving twice, not a second reason to message the voter.
        """
        armed = self._arm_boost(user_id, row["boost_expires_at"])
        if not row["replayed"]:
            await self._send_thank_you(user_id, row)
            await self._schedule_reminder(user_id, row.get("last_vote_at"))
        return armed

    def _arm_boost(self, user_id, expires_at):
        """Hand the fresh deadline to the Leveling cog's in-memory map.

        The house cross-cog seam (bot.get_cog, as level rewards and role menus
        use it), and defensive for the same reason: the vote is ALREADY banked
        in Postgres when this runs, so a leveling cog that failed to load must
        cost the voter their boost for this process only - never the record of
        their vote. The next restart reads the row back and arms it (see
        Leveling.reload_vote_boosts), so even that recovers by itself.
        """
        cog = self.bot.get_cog("Leveling")
        if cog is None:
            return False
        try:
            cog.note_vote_boost(user_id, expires_at)
        except Exception:
            log.exception("votes: could not arm the XP boost of %s", user_id)
            return False
        return True

    async def _dm(self, user_id, what, render):
        """Deliver ONE best-effort DM to a member, in that member's language.

        The single DM seam this cog has: both the thank-you and the opt-in
        "vote again" nudge go through it, so they share one failure contract
        and one locale seam and can never drift apart.

        ``render`` is called INSIDE the resolved locale context and returns the
        text, which is what lets a caller keep its own ``_()`` string while the
        locale plumbing lives here once. That plumbing is the SAME one the
        dashboard's export DM uses (see
        ``cogs/system/dashboard_user_actions._export_note``):
        ``i18n.resolve_locale`` + ``i18n.locale``, because neither a webhook
        delivery nor a fired timer has a live interaction to inherit a locale
        from, so it is resolved from the member's own saved preference instead.

        Never allowed to cost its caller anything: an unresolvable id, a locale
        lookup that raises, closed DMs, anything else - each costs a log line
        and nothing more, the same contract as :meth:`_arm_boost`. A vote is
        already banked in Postgres by the time any of this runs.
        """
        try:
            user = self.bot.get_user(user_id)
            if user is None:
                user = await self.bot.fetch_user(user_id)
        except Exception:
            log.warning("votes: could not resolve user %s for %s", user_id, what)
            return
        # A payload names an ID, not a person. A bot (or an id that resolves to
        # one) never opted into anything here and cannot read a DM, so it is
        # skipped in silence rather than messaged.
        if user is None or getattr(user, "bot", False):
            return

        loc = i18n.DEFAULT_LOCALE
        try:
            loc = await i18n.resolve_locale(self.bot, user_id=user_id)
        except Exception:
            log.warning("votes: locale lookup failed for %s to %s", what, user_id)
        try:
            # Rendering is inside the guard too, not only the send: a caller's
            # ``render`` reads live state (a row column, self.bot.user), and the
            # one thing this must never do is raise back into a listener that
            # still has work to do after the DM (see _apply_vote_row, which
            # schedules the reminder next).
            with i18n.locale(loc):
                text = render()
            await user.send(text)
        except discord.Forbidden:
            pass  # closed DMs / no shared server: the one failure a member chose
        except Exception:
            log.warning(
                "votes: failed to deliver %s to %s", what, user_id, exc_info=True
            )

    async def _send_thank_you(self, user_id, row):
        """Best-effort thank-you DM for a genuine, non-replayed vote."""
        await self._dm(
            user_id,
            "the thank-you DM",
            lambda: _(
                "Thanks for voting for Yasuho! Your XP is boosted x1.5 until "
                "{when}, and your streak is now {streak}."
            ).format(
                when=discord.utils.format_dt(row["boost_expires_at"], "R"),
                streak=row["streak"],
            ),
        )

    async def _schedule_reminder(self, user_id, last_vote_at):
        """(Re)schedule the opt-in "vote again" reminder, 12h out.

        Reuses the EXISTING one-shot timers table - ``cogs/community/reminders.py``
        owns the dispatch loop, the claim/delete mechanics and the
        ``*_timer_complete`` dispatch; nothing here starts a loop of its own.
        Cancel-then-reschedule on every real vote: any earlier pending
        reminder for this user is deleted (the same scoped-DELETE-by-key shape
        ``reminders.cancel_reminder`` uses for its own event, just keyed on
        ``user_id`` instead of ``author_id``) before the new one is inserted,
        so a repeat voter never accumulates more than one pending row, and the
        reminder always tracks the LATEST vote.

        Skipped entirely, silently, when the Reminder cog is not loaded (the
        additive rule: a missing scheduler must never become an error for the
        voter) or the preference is off (the default - see usersettings.PREFS'
        ``vote_reminder``). ``last_vote_at`` rides along in ``extra`` so the
        fire-time check (:meth:`on_vote_reminder_timer_complete`) can tell
        "still the vote that scheduled me" from "already voted again since".

        RETENTION NOTE: a pending row here can outlive an erasure. Every query
        in ``privacy.USER_DELETE_QUERIES`` is keyed ``WHERE user_id = $1`` (a
        guard its tests enforce repo-wide) and a timer keys the id INSIDE its
        JSONB ``extra``, so `?mydata deleteprofile` does not reach it - exactly
        as it does not reach a user's pending reminders or a tempban. It is at
        most one row naming a user id and a timestamp, for at most
        :data:`REMINDER_DELAY_HOURS`, and the fire-time check treats a missing
        ledger row as "erased, say nothing", so the erasure still wins where it
        counts: nothing is ever sent.
        """
        reminder = self.bot.get_cog("Reminder")
        if reminder is None:
            return
        try:
            wants_reminder = await settings.get_user(
                self.bot.db_pool, user_id, VOTE_REMINDER_PREF_KEY, False
            )
        except Exception:
            log.warning(
                "votes: preference lookup failed for %s; not scheduling a "
                "vote-again reminder",
                user_id,
            )
            return
        if not wants_reminder:
            return

        voted_at = last_vote_at or discord.utils.utcnow()
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM timers WHERE event = $1 "
                "AND extra->>'user_id' = $2 AND claimed_at IS NULL",
                VOTE_REMINDER_EVENT,
                str(user_id),
            )
            await reminder.create_timer(
                discord.utils.utcnow()
                + datetime.timedelta(hours=REMINDER_DELAY_HOURS),
                VOTE_REMINDER_EVENT,
                user_id=user_id,
                voted_at=voted_at.isoformat(),
            )
        except Exception:
            log.exception(
                "votes: failed to (re)schedule the vote-again reminder for %s",
                user_id,
            )

    @commands.Cog.listener()
    async def on_vote_reminder_timer_complete(self, extra):
        """Fire the opt-in "vote again" DM.

        Reached through ``reminders.call_timer``'s generic
        ``f"{event}_timer_complete"`` dispatch for any event it does not own
        itself (see the ``else`` branch there) - never called directly.

        Three live checks, all required, because a lot can change in 12h: the
        preference may have been turned off since it was scheduled, the ledger
        row may have been ERASED since (``?mydata deleteprofile`` deletes
        topgg_votes but not this pending timer, and an erasure must always win
        over a nudge scheduled before it), and the user may already have voted
        again (in which case that newer vote already rescheduled its own
        reminder - see :meth:`_schedule_reminder` - so firing this one too would
        be a duplicate nag). Never raises: a failure here costs one missed
        nudge, never a crash in the dispatch loop that would also cost every
        OTHER pending timer its turn.
        """
        user_id = extra.get("user_id")
        if user_id is None:
            return
        try:
            still_wants_it = await settings.get_user(
                self.bot.db_pool, user_id, VOTE_REMINDER_PREF_KEY, False
            )
            if not still_wants_it:
                return
            row = await self.bot.db_pool.fetchrow(
                "SELECT last_vote_at FROM topgg_votes WHERE user_id = $1", user_id
            )
        except Exception:
            log.warning(
                "votes: vote-again reminder precondition check failed for %s",
                user_id,
                exc_info=True,
            )
            return

        if row is None:
            # The ledger row this reminder was scheduled from is GONE, and the
            # only thing that deletes it is the user erasing their own data
            # (privacy.USER_DELETE_QUERIES, behind `?mydata deleteprofile`).
            # Erasure wins: somebody who just asked us to forget that they vote
            # must not then be DMed about voting again.
            return

        voted_at_raw = extra.get("voted_at")
        if voted_at_raw:
            try:
                scheduled_for = datetime.datetime.fromisoformat(voted_at_raw)
            except (TypeError, ValueError):
                scheduled_for = None
            if scheduled_for is not None and row["last_vote_at"] > scheduled_for:
                # A newer vote landed since this reminder was scheduled - it
                # already (re)scheduled its own, so this firing is stale.
                return

        await self._dm(
            user_id,
            "the vote-again reminder",
            lambda: _(
                "You can vote for Yasuho again! Every vote keeps your XP "
                "boost and streak going: {url}"
            ).format(url=vote_url(self.bot.user.id)),
        )

    async def _maybe_catch_up(self, user_id, row):
        """Lazy top.gg poll for a vote our own webhook may have missed.

        Only even considered when our ledger looks stale (no row at all, or a
        ``last_vote_at`` older than :data:`CATCHUP_STALE_HOURS`) - most
        ``/vote`` opens never reach this at all, since a member who voted
        recently already has a fresh row. Bounded on TWO axes (see
        :data:`CATCHUP_COOLDOWN_SECONDS` and :data:`CATCHUP_GLOBAL_LIMIT`): at
        most one call per user per minute, and at most a fixed number per
        minute across the whole process, so neither one member's spam nor a
        spike of first-time openers can dent the top.gg token budget the
        guild-count autopost shares. Reuses webstats' OWN ``DBLClient`` (never
        a second one) - skips silently when that cog is not loaded or has none
        configured.

        Returns the row to render: the fresh one if a missed vote was found
        and recorded, otherwise the row it was handed, unchanged.
        """
        now = discord.utils.utcnow()
        stale = row is None or (
            now - row["last_vote_at"] >= datetime.timedelta(hours=CATCHUP_STALE_HOURS)
        )
        if not stale:
            return row

        webstats = self.bot.get_cog("Webstats")
        client = getattr(webstats, "dbl_client", None) if webstats is not None else None
        if client is None:
            return row  # not configured on this process - skip silently

        # Fairness first, then the ceiling: a member refused by their own
        # cooldown never touches the shared budget, and a member refused by the
        # ceiling does not burn their cooldown for a call that never happened
        # (the same "a rejection consumes nothing on the axis that rejected it"
        # rule anilist.throttle.allow_interactive follows).
        if self._catchup_cooldown.is_active(user_id):
            return row
        if not self._catchup_budget.hit(_CATCHUP_GLOBAL_KEY):
            log.info(
                "votes: /vote catch-up skipped for %s - the process-wide poll "
                "ceiling (%s/%ss) is already spent for this window",
                user_id,
                CATCHUP_GLOBAL_LIMIT,
                CATCHUP_GLOBAL_WINDOW,
            )
            return row
        self._catchup_cooldown.touch(user_id)

        try:
            voted = await client.get_user_vote(user_id)
        except Exception:
            log.warning(
                "votes: top.gg catch-up poll failed for %s", user_id, exc_info=True
            )
            return row
        if not voted:
            return row

        # top.gg confirms a vote our webhook never delivered. Bank it through
        # the SAME upsert the webhook uses - is_weekend is unknown from this
        # path (top.gg's vote-check API does not say), so it defaults to the
        # normal 12h boost rather than guessing weekend-double. Documented
        # here rather than silently: a member who catches up on a weekend gets
        # the shorter window, which only ever under-rewards, never over.
        #
        # TWO arguments differ from the webhook's call, and they are the two
        # halves of the same fact: this path's evidence is weaker.
        #
        # The first is the replay
        # floor: CATCHUP_STALE_HOURS, not REPLAY_WINDOW_HOURS. The webhook's one
        # hour is sized for a redelivery of the same POST; this path's evidence
        # is only "top.gg says they voted some time in the last 12h", which is
        # exactly the vote our ledger may already hold. Handing the statement
        # the wider floor makes the DB clock re-decide the staleness this method
        # decided on the app clock, so no amount of drift between the two can
        # turn one vote into two rows' worth of streak. When it does bite, the
        # row comes back `replayed` and _apply_vote_row sends nothing.
        #
        # The second is FROM_CATCHUP, which MARKS the row as stamped on that
        # weaker evidence. Without it the wider floor above would then swallow
        # the member's next genuine webhook vote for up to twelve hours - the
        # very votes this poll exists to protect. See RECORD_VOTE.
        try:
            fresh = await self.bot.db_pool.fetchrow(
                RECORD_VOTE,
                user_id,
                BOOST_HOURS,
                STREAK_WINDOW_HOURS,
                CATCHUP_STALE_HOURS,
                FROM_CATCHUP,
            )
        except Exception:
            log.exception("votes: catch-up record failed for %s", user_id)
            return row
        if fresh is None:
            return row

        await self._apply_vote_row(user_id, fresh)
        if fresh["replayed"]:
            log.info(
                "votes: /vote catch-up found a vote for %s the ledger already "
                "had (nothing banked twice)",
                user_id,
            )
        else:
            log.info(
                "votes: /vote catch-up recorded a vote top.gg had for %s that "
                "our webhook had missed",
                user_id,
            )
        return fresh

    @commands.hybrid_command(name="vote")
    async def vote(self, ctx):
        """Shows your top.gg vote status and what voting gives you."""
        # ephemeral=True on the TYPING, not only on the send: on a slash
        # invocation ctx.typing() IS the defer, and a plain one defers publicly
        # - Discord then leaves a visible "thinking" placeholder in the channel
        # that an ephemeral followup never resolves. Same discipline as the
        # profile connector commands (cogs/community/profile/connectors/cog.py).
        # On a prefix invocation there is no interaction, so the flag is ignored
        # and this is a normal typing indicator.
        async with ctx.typing(ephemeral=True):
            row = await self.bot.db_pool.fetchrow(
                "SELECT last_vote_at, streak, total_votes, boost_expires_at "
                "FROM topgg_votes WHERE user_id = $1",
                ctx.author.id,
            )
            row = await self._maybe_catch_up(ctx.author.id, row)
            view = VoteStatusView(self.bot.user.id, row)
            await ctx.send(
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def _status_lines(row):
    """The body of the /vote card: boost / streak / lifetime, for any row.

    Same three lines whether ``row`` is a real row or ``None`` (never voted) -
    only the wording of the boost line changes, never the shape, so a
    first-time voter sees exactly the structure a regular one does. Zero
    shame, zero different treatment.
    """
    if row is None:
        return "\n".join(
            (
                _("**Boost:** none yet - vote below to start one!"),
                _("**Streak:** {streak}").format(streak=0),
                _("**Lifetime votes:** {total}").format(total=0),
            )
        )
    now = discord.utils.utcnow()
    if row["boost_expires_at"] > now:
        boost_line = _("**Boost:** active until {when}").format(
            when=discord.utils.format_dt(row["boost_expires_at"], "R")
        )
    else:
        boost_line = _("**Boost:** none right now - vote again to renew it!")
    return "\n".join(
        (
            boost_line,
            _("**Streak:** {streak}").format(streak=row["streak"]),
            _("**Lifetime votes:** {total}").format(total=row["total_votes"]),
        )
    )


class VoteStatusView(discord.ui.LayoutView):
    """Read-only Components V2 card: a member's own top.gg vote status.

    Mirrors the shape of ``cogs/config/welcome.py``'s ``WelcomeStatusView`` - a
    plain ``LayoutView``, no author gate. Its one control is a link button,
    which Discord opens client-side without ever sending the bot an
    interaction, so there is nothing here that needs guarding or a timeout
    handler.
    """

    def __init__(self, bot_id, row, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(bot_id, row)

    def _build(self, bot_id, row):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay("## " + _("Vote for Yasuho")))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(_status_lines(row)))
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _(
                    "Voting gives you x1.5 XP for 12 hours (24 hours on "
                    "weekends)."
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label=_("Vote on top.gg"),
                    url=vote_url(bot_id),
                )
            )
        )
        self.add_item(container)


async def setup(bot):
    await bot.add_cog(Votes(bot))
