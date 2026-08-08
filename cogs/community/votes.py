"""Top.gg vote rewards: the ledger a vote writes, and the XP boost it arms.

SILENT INFRASTRUCTURE. This lot adds no command, no DM and no embed - nothing a
member can see. It records that a vote happened and arms a temporary XP boost
for the voter; the surfaces that SHOW any of it (a /vote command, a profile
badge, a "vote again" reminder, the thank-you DM) are a later lot and can be
built entirely on top of the table this one fills.

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

import logging

from discord.ext import commands

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
# $4 the replay floor. ``now()`` is the transaction timestamp, so every column of
# a given vote is stamped from ONE clock reading - last_vote_at and
# boost_expires_at can never disagree about when the vote happened.
#
# THE REPLAY BRANCH, repeated in each SET because SQL has no way to say "leave
# this row alone" conditionally: when the previous vote is less than $4 old, all
# four columns keep the value they already had, which makes a redelivery a true
# no-op rather than a free streak step. The predicate is bounded BELOW by zero
# on purpose - a last_vote_at in the FUTURE (a clock stepping backwards) is not
# a replay, and must fall through to the streak rules so the next real vote
# re-stamps the row from the current clock instead of freezing the ledger until
# the future timestamp is reached.
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
    user_id, last_vote_at, streak, total_votes, boost_expires_at
)
VALUES ($1, now(), 1, 1, now() + $2 * INTERVAL '1 hour')
ON CONFLICT (user_id) DO UPDATE SET
    last_vote_at = CASE
        WHEN now() - topgg_votes.last_vote_at
             BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour'
        THEN topgg_votes.last_vote_at
        ELSE now()
    END,
    streak = CASE
        WHEN now() - topgg_votes.last_vote_at
             BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour'
        THEN topgg_votes.streak
        WHEN now() - topgg_votes.last_vote_at <= $3 * INTERVAL '1 hour'
        THEN topgg_votes.streak + 1
        ELSE 1
    END,
    total_votes = CASE
        WHEN now() - topgg_votes.last_vote_at
             BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour'
        THEN topgg_votes.total_votes
        ELSE topgg_votes.total_votes + 1
    END,
    boost_expires_at = CASE
        WHEN now() - topgg_votes.last_vote_at
             BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour'
        THEN topgg_votes.boost_expires_at
        ELSE GREATEST(
            topgg_votes.boost_expires_at, now() + $2 * INTERVAL '1 hour'
        )
    END
RETURNING streak, total_votes, boost_expires_at,
          last_vote_at < now() AS replayed
"""


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
    """Records top.gg votes and arms the voter's temporary XP boost."""

    def __init__(self, bot):
        self.bot = bot

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
        # not reach the leveling cog.
        armed = self._arm_boost(user_id, row["boost_expires_at"])
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


async def setup(bot):
    await bot.add_cog(Votes(bot))
