"""Purpose: the DAILY SUMMARY (Yasuho+) - yesterday's headline numbers against
the day before, posted once a day into a channel the server chose.

Same shape as the weekly digest (digest.py) and the same honesty contract: it
collects nothing of its own, every number comes out of the statements rollups.py
already owns (ACTIVITY_SERIES and GROWTH), and it reads the configuration fresh
from guild_settings at delivery time (no tools.settings LRU, so the dashboard
needs no invalidation kind for this key).

Four rules.

1. THE HONEST WINDOW. The summary reports YESTERDAY (a complete UTC day) and
   never the day in progress, compared against the day before it.

2. A MISSING DAY IS NOT A ZERO. ``server_stats_days`` has one row per observed
   guild-day, so a day with no row is a day nobody was counting. Yesterday not
   observed = nothing is posted; the day before not observed = the comparison is
   omitted (never a 0%); a NULL member_count snapshot = the member total is
   omitted (never 0 members).

3. EXACTLY ONCE PER GUILD PER UTC DAY, LATE WITHIN THE SAME UTC DAY. The
   claim (:data:`CLAIM`) writes the UTC day into ``serverstats_daily_state``
   BEFORE anything is sent; only the writer that changed the row proceeds. A
   guild is delivered on any hourly tick between its hour and 23:59 UTC of that
   day, and the reported day is "yesterday" relative to the delivery day, so a
   late tick reports the very same day an early one would have and cannot
   double-post. A day the bot was down for the whole remaining stretch is
   SKIPPED (the digest's catch-up window is a week; this one is a day).

4. NO THUNDERING HERD, AND NEVER BEFORE 01:00 UTC. Each guild has a fixed
   delivery hour in 1..23, ``guild_id % 23 + 1`` (:data:`CANDIDATES`), so the
   fleet is spread over 23 hourly ticks instead of all landing at 00:00 UTC,
   and the last 5-minute buffer flush of the reported day (which runs near
   midnight) has landed before anything is read.

5. THE PREMIUM CHECK COMES BEFORE THE CLAIM. bot.premium degrades to "nobody
   is Yasuho+" when its load fails, so claiming first would burn a paying
   guild's day on a cache problem. A lapsed guild is therefore NOT claimed: it
   stays a candidate and costs one in-memory dict lookup per tick (see
   cog.run_daily_once for how the LIMITed query is kept from being starved).

Time zone: UTC, like the weekly digest and the collector's own days. The server
has no timezone setting in config, and the aggregates are bucketed per UTC day,
so a local-day report could not be built from them anyway.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import discord

from . import rollups
from tools import settings
from tools.formats import random_colour
from tools.i18n import _, ngettext

# The guild_settings key. ABSENT means OFF (see digest.KEY_DIGEST_CHANNEL).
KEY_DAILY_CHANNEL = "serverstats_daily_channel"

# Per hourly tick. 50 x 24 ticks = 1200 opted-in guilds per day, the honest
# ceiling; Yasuho+ servers are a small subset of the fleet and the hour spread
# means a tick sees about 1/24 of them.
FAN_OUT_LIMIT = 50

# Opted-in guilds still owed today's summary whose delivery hour has come.
# Anti-join on the claim table (the LIMITed fan-out must not re-read the head
# of the list), JSON null reads as OFF, and the key is a literal so the planner
# matches the partial index guild_settings_daily_channel_idx (test-pinned
# against schema.sql). $1 = today (UTC date), $2 = current UTC hour, $3 = limit,
# $4 = guild ids this tick already looked at (lapsed ones are not claimed).
CANDIDATES = """
    SELECT gs.guild_id,
           gs.settings ->> 'serverstats_daily_channel' AS channel_id
      FROM guild_settings gs
      LEFT JOIN serverstats_daily_state st
             ON st.guild_id = gs.guild_id AND st.last_day = $1
     WHERE gs.settings ? 'serverstats_daily_channel'
       AND gs.settings ->> 'serverstats_daily_channel' IS NOT NULL
       AND st.guild_id IS NULL
       AND (gs.guild_id % 23) + 1 <= $2
       AND gs.guild_id <> ALL($4::bigint[])
     ORDER BY gs.guild_id
     LIMIT $3;
    """

# The claim: one atomic statement whose RETURNING is the permission to send.
# ``<`` rather than ``<>`` so a clock stepping backwards can never re-claim an
# older day.
CLAIM = """
    INSERT INTO serverstats_daily_state (guild_id, last_day)
    VALUES ($1, $2)
    ON CONFLICT (guild_id) DO UPDATE
       SET last_day = EXCLUDED.last_day
     WHERE serverstats_daily_state.last_day < EXCLUDED.last_day
    RETURNING guild_id;
    """

CLEAR_KEY = (
    "UPDATE guild_settings SET settings = settings - $2::text WHERE guild_id = $1"
)


def delivery_hour(guild_id):
    """The UTC hour (1-23, never 0) from which this guild's summary may be delivered."""
    return int(guild_id) % 23 + 1


def daily_period(today):
    """``(day, previous_day)``: yesterday and the day before, for ``today`` (UTC)."""
    one = datetime.timedelta(days=1)
    return today - one, today - 2 * one


@dataclass(frozen=True)
class DailyReport:
    """One UTC day of a guild, as the summary states it.

    ``previous_messages`` and ``delta_pct`` are ``None`` unless the day before
    was observed (and, for the delta, carries messages); ``member_count`` is
    ``None`` when the daily snapshot was not taken. None is never shown as 0.
    """

    day: datetime.date
    observed: bool
    messages: int
    previous_messages: int | None
    delta_pct: float | None
    joins: int
    leaves: int
    member_count: int | None

    @property
    def net(self):
        return self.joins - self.leaves

    @property
    def has_data(self):
        return self.observed


def shape_daily(activity_rows, growth_rows, today):
    """Turn the two 2-day reads into a :class:`DailyReport`. Pure."""
    day, previous = daily_period(today)
    growth_by_day = {row["day"]: row for row in growth_rows or ()}
    messages_by_day = {
        row["day"]: int(row["messages"] or 0) for row in activity_rows or ()
    }
    row = growth_by_day.get(day)
    if row is None:
        return DailyReport(day, False, 0, None, None, 0, 0, None)

    messages = messages_by_day.get(day, 0)
    previous_messages = (
        messages_by_day.get(previous, 0) if previous in growth_by_day else None
    )
    delta = None
    if previous_messages:
        delta = round((messages - previous_messages) * 100.0 / previous_messages, 1)
    member_count = row["member_count"]
    return DailyReport(
        day=day,
        observed=True,
        messages=messages,
        previous_messages=previous_messages,
        delta_pct=delta,
        joins=int(row["joins"] or 0),
        leaves=int(row["leaves"] or 0),
        member_count=int(member_count) if member_count is not None else None,
    )


def _signed(value):
    return f"+{value}" if value >= 0 else str(value)


def render(report, guild_name):
    """The summary as ONE compact embed. Pure."""
    embed = discord.Embed(
        title="\N{BAR CHART} " + _("Daily server summary"),
        description=_("Here is what happened on {guild} yesterday.").format(
            guild=guild_name
        ),
        colour=random_colour(),
    )
    lines = [_("{total} messages").format(total=report.messages)]
    if report.delta_pct is None:
        lines.append(_("No comparable day before this one."))
    else:
        sign = "+" if report.delta_pct >= 0 else ""
        lines.append(
            _("{sign}{pct}% vs the day before").format(
                sign=sign, pct=f"{report.delta_pct:.1f}"
            )
        )
    embed.add_field(name=_("Messages"), value="\n".join(lines), inline=False)

    members = [
        _("{joins} joins / {leaves} leaves ({net} net, humans only)").format(
            joins=report.joins, leaves=report.leaves, net=_signed(report.net)
        )
    ]
    if report.member_count is not None:
        # The snapshot is taken on the first flush of the UTC day, i.e. it is
        # the count at the START of the reported day.
        members.append(
            ngettext(
                "{count} member at the start of the day",
                "{count} members at the start of the day",
                report.member_count,
            ).format(count=report.member_count)
        )
    embed.add_field(name=_("Members"), value="\n".join(members), inline=False)

    embed.set_footer(text=_("{day} (complete UTC day).").format(day=report.day.isoformat()))
    return embed


async def collect(pool, guild_id, today):
    """Yesterday and the day before: TWO guild-prefixed PK range reads."""
    day, previous = daily_period(today)
    activity_rows = await pool.fetch(rollups.ACTIVITY_SERIES, guild_id, previous, day)
    growth_rows = await pool.fetch(rollups.GROWTH, guild_id, previous, day)
    return shape_daily(activity_rows, growth_rows, today)


async def candidates(pool, today, hour, limit=FAN_OUT_LIMIT, seen=()):
    return await pool.fetch(CANDIDATES, today, hour, limit, list(seen))


async def claim(pool, guild_id, day):
    """Claim this guild's summary for the UTC ``day``; True when WE won it."""
    return await pool.fetchval(CLAIM, guild_id, day) is not None


async def set_channel(pool, guild_id, channel_id):
    await settings.set_guild(pool, guild_id, KEY_DAILY_CHANNEL, channel_id)


async def clear_channel(pool, guild_id):
    """Turn the summary OFF by DELETING the key (see digest.clear_channel)."""
    await pool.execute(CLEAR_KEY, guild_id, KEY_DAILY_CHANNEL)
    settings.invalidate_guild(guild_id)
