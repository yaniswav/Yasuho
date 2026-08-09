"""Purpose: the PRESENTATION half of ST3 - the ``/serverstats`` Components V2
card. cogs/community/serverstats/rollups.py is the READ layer and its honesty
contract is the spec this module renders against; nothing here issues SQL or
mutates anything, it only shapes :mod:`rollups` value objects into text.

Three honesty rules from rollups.py, restated here because every section below
has to obey them:

1. ``Overview.delta_pct`` (and ``RetentionWeek.net`` on a week with
   ``has_data`` False) is ``None`` when a delta cannot be HONESTLY computed -
   render NOTHING that looks like a percentage or a signed number for it, say
   so in words instead. Never a silent ``0%``.
2. ``has_data`` False on a :class:`~.rollups.GrowthPoint`,
   :class:`~.rollups.ActivityPoint` or :class:`~.rollups.RetentionWeek` means
   UNKNOWN, not zero - rendered as a dash, and (in a mini-series) as the
   dedicated placeholder glyph from :func:`render_bar`, never folded into a
   real bar.
3. ``RetentionReport.has_activity_data`` gates the WHOLE "active members"
   half of the retention block - it is omitted outright on a guild without
   leveling (or one whose leveling window has fully aged out of
   ``xp_period``), never shown as a wall of dashes.

One rule of its own, which no rollups value object can enforce: /serverstats is
PUBLIC, so anything naming a specific channel is cut to the INVOKING member's
visibility (:func:`_render_top_channels`), never the bot's. The read layer keeps
returning complete rows; the filtering is a rendering decision, so it re-applies
on every rebuild (the 7/30-day toggle included).

Precedents followed: :class:`~cogs.community.leveling.seasons_views.HallOfFameCard`
(``AuthorLayoutView``, the ``_handler``-bound button shape, the house footer)
and the now-playing progress bar (``cogs/music/views.py``'s
``PROGRESS_FILLED``/``PROGRESS_EMPTY`` two-tone bar with no partial-block
gradation) for :func:`render_bar` below.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import discord

from . import charts, rollups
from tools import interactions
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# The overview + top-channels toggle: exactly two windows, exactly two reads
# replayed per click (rollups.overview + rollups.top_channels) - the ST2
# contract this card promised. Growth/activity/retention never change window
# from the toggle; only these two sections do.
ALT_OVERVIEW_DAYS = 30

# How many of the busiest channels the card shows. Small on purpose: this is a
# glanceable card, not a per-channel report.
TOP_CHANNELS_LIMIT = 5

# Horizontal bar width (characters) for the top-channels ranking - wide enough
# to show real proportion between the busiest channel and the rest.
TOP_CHANNEL_BAR_WIDTH = 10

# Mini-series (growth / activity) render one bar PER DAY, 1 character wide,
# joined into a single line - a coarse two-state (high/low) sparkline in the
# same discrete, no-partial-block style as the music progress bar, rather than
# a richer glyph ramp this codebase has no other precedent for.
DAY_BAR_WIDTH = 1

BAR_FILLED = "\N{DARK SHADE}"  # "▓" - matches cogs/music/views.py PROGRESS_FILLED
BAR_EMPTY = "\N{LIGHT SHADE}"  # "░" - matches cogs/music/views.py PROGRESS_EMPTY
BAR_UNKNOWN = "-"  # an unknown value/day, never a bar level (see rule 2 above)

# U3: the PNG chart the card attaches alongside its text sparklines. Filename
# is a constant (not derived per-render) because it only ever needs to match
# the "attachment://<name>" reference the MediaGallery item below points at -
# there is exactly one chart per card, never several competing for a name.
CHART_FILENAME = "serverstats_chart.png"

# The rendering budget: charts.render_activity_chart runs through the
# bot-wide image semaphore (tools.rendering.run_image_job, 2 slots - the same
# seam cogs/community/leveling/rank_card.py's render uses), and this timeout
# is what keeps a saturated semaphore from ever making the card WAIT for its
# chart. On a timeout, an exception from the Pillow work, or anything else,
# cmd_serverstats falls back to sending the card without an attachment - the
# text sparklines in _render_growth / _render_activity already carry the same
# information, just less prettily, and are drawn unconditionally either way.
CHART_RENDER_TIMEOUT = 2.0

_NO_PINGS = discord.AllowedMentions.none()


def build_chart_points(activity, growth):
    """Zip a :class:`~.rollups.ActivitySeries` and a :class:`~.rollups.Growth`
    - the SAME window, see cmd_serverstats, which reads both over identical
    days - into a tuple of :class:`~.charts.ChartPoint`.

    Pure: reads only the two rollups value objects the card already holds,
    no new query, no I/O. A day becomes a hole (``messages=None``,
    ``net=None``) exactly when its point's ``has_data`` is ``False`` - the
    SAME honesty test the text sparklines above already apply (rule 2 of the
    module docstring), so the PNG chart and its text fallback can never
    disagree about which days are unknown.
    """
    growth_by_day = {point.day: point for point in growth.points}
    result = []
    for point in activity.points:
        matching = growth_by_day.get(point.day)
        net = matching.net if (matching is not None and matching.has_data) else None
        messages = point.messages if point.has_data else None
        result.append(charts.ChartPoint(day=point.day, messages=messages, net=net))
    return tuple(result)


# ---------------------------------------------------------------------------
# Pure rendering: mini-bars
# ---------------------------------------------------------------------------
def render_bar(values, width):
    """Render one proportional horizontal bar per value, all sharing ONE scale.

    ``values`` is a sequence where each entry is either a non-negative number
    or ``None`` for a value that is UNKNOWN rather than zero (a hole in a
    series - an absent watched day, a week that predates collection; see the
    module docstring's rule 2). Every KNOWN value becomes a ``width``-character
    two-tone bar (``BAR_FILLED``/``BAR_EMPTY``, discrete segments, no partial
    block - the same style as the music now-playing progress bar), filled
    proportionally to its share of the LARGEST known value across the WHOLE
    call. That single shared scale is what makes a set of bars comparable to
    each other - a row per top channel, or a run of 1-wide bars joined into one
    line for a day-by-day mini-series - rather than each bar being scaled only
    against itself (which would make every non-zero value look identically
    "full").

    A ``None`` entry renders as ``width`` copies of ``BAR_UNKNOWN`` - an
    unknown value must never look like a real, even zero, measurement. An
    all-zero series of KNOWN values renders every bar fully ``BAR_EMPTY``,
    which is a real zero and must not be confused with ``BAR_UNKNOWN``.

    Returns a tuple of ``len(values)`` strings, each exactly ``width``
    characters long. An empty ``values`` returns an empty tuple. Pure and
    total: no exception for any combination of empty / all-``None`` / a single
    peak / a flat zero series.
    """
    values = tuple(values)
    if not values:
        return ()

    known = [value for value in values if value is not None]
    peak = max(known) if known else 0

    bars = []
    for value in values:
        if value is None:
            bars.append(BAR_UNKNOWN * width)
            continue
        if peak <= 0:
            filled = 0
        else:
            filled = round(width * (value / peak))
            filled = max(0, min(width, filled))
        bars.append(BAR_FILLED * filled + BAR_EMPTY * (width - filled))
    return tuple(bars)


def _sparkline(values):
    """A day-by-day (or week-by-week) mini-series as ONE joined bar line."""
    return "".join(render_bar(values, DAY_BAR_WIDTH))


def _level_sparkline(values):
    """A mini-series for a LEVEL series, rebased to its own low point.

    :func:`render_bar` scales against the largest known value, i.e. from a ZERO
    baseline. That is the right baseline for a VOLUME series (messages: zero
    really is zero, and a quiet day should draw short) and the wrong one for a
    LEVEL series like a member count, which never goes anywhere near zero. Fed
    raw member counts, render_bar fills every single bar solid: at
    ``DAY_BAR_WIDTH`` a bar is filled as soon as a day is at least half the
    peak, so 1500 -> 1000 members over a month and 1000 -> 1400 draw the exact
    same flat, saturated line. The "Growth" curve would be incapable of showing
    growth.

    Rebasing every known value to the LOWEST known value of the series fixes
    that without bending a single honesty rule: ``None`` stays ``None`` (a hole
    is still BAR_UNKNOWN, never a level), and a perfectly flat window rebases to
    all zeroes, which render_bar draws as all-empty - "no change", visibly
    different from the dashes of "we do not know". The caller labels the bar so
    the baseline is never mistaken for zero members.
    """
    known = [value for value in values if value is not None]
    floor = min(known) if known else 0
    return _sparkline(
        [None if value is None else value - floor for value in values]
    )


def _last_bar_note():
    """The footnote every mini-series carries under its bar line.

    The SERIES reads (growth, activity) deliberately keep the day in progress -
    rollups.window_bounds ends the window on TODAY, unlike the overview's
    complete-days-only bounds - so the rightmost bar is always a few hours of
    today drawn against full days. Unlabeled, that bar reads as a real drop on
    every card opened before the evening. Said in words rather than by dropping
    the point: the curve keeps its most recent data, the reader is just told
    what the last bar is.
    """
    return _("Last bar: today, still in progress.")


def _signed(value):
    """``+3`` / ``-2`` / ``+0`` - a plain signed integer, never wrapped (no
    natural-language word in it, same convention as seasons_views.py's raw
    ``<@&{role_id}>`` mention token)."""
    return f"+{value}" if value >= 0 else str(value)


# ---------------------------------------------------------------------------
# Pure rendering: section bodies (rollups value object -> list[str] lines)
# ---------------------------------------------------------------------------
def _render_overview(overview):
    # The window label is UNCONDITIONAL and comes first, because it qualifies
    # every number below it - not just the delta. rollups.overview_bounds stops
    # this section at YESTERDAY (the last fully elapsed UTC day), so both the
    # total and the daily average exclude the day in progress. The rest of the
    # card does NOT (top channels, growth and activity all include today), so
    # leaving this unsaid whenever a delta happens to be unavailable would let a
    # reader take "Overview (7 days)" for the same 7 days the section above it
    # counts. Said once, here, in every branch.
    lines = [
        "-# "
        + _("Complete days only, through {end_day}.").format(
            end_day=overview.end_day.isoformat()
        )
    ]
    if overview.days_available == 0:
        # No complete day of this window carries data, so total_messages is a
        # structural 0 (shape_overview only sums the days that exist) - printing
        # "0 messages" would publish a silence nobody measured, the same lie
        # rule 2 forbids per day. Same sentence as the growth/activity blocks.
        lines.append(_("No day of this window was observed yet."))
    else:
        lines.append(
            _("{total} messages - {average:.1f}/day average").format(
                total=overview.total_messages, average=overview.average_per_day
            )
        )
    if overview.partial:
        lines.append(
            _("Partial window: only {available} of {days} days have data.").format(
                available=overview.days_available, days=overview.days
            )
        )
    if overview.delta_pct is None:
        lines.append(_("Not enough history yet to compare against a previous window."))
    else:
        sign = "+" if overview.delta_pct >= 0 else ""
        lines.append(
            _("{sign}{pct:.1f}% vs the {days} days before").format(
                sign=sign,
                pct=overview.delta_pct,
                days=overview.days,
            )
        )
    return lines


def _render_top_channels(guild, top_channels, member):
    resolved = [
        (channel_count, guild.get_channel(channel_count.channel_id))
        for channel_count in top_channels
    ]
    # A channel the bot can no longer see (deleted, or visibility lost since
    # the row was written) is silently dropped - see queries.py's TOP_CHANNELS
    # comment: names are never resolved at read time, so this is the ONE place
    # that decides what is actually shown.
    #
    # CONFIDENTIALITY: /serverstats is a PUBLIC command, so the ranking must be
    # cut to the INVOKER's visibility, never the bot's - the bot sees the staff
    # channels too, and a row here leaks both a private channel's existence and
    # how busy it is. The filter lives at RENDER time (the DB rows stay
    # complete), so the 7/30-day toggle re-filters for free on every rebuild,
    # and the mention token <#id> is only ever emitted for a channel this member
    # can already read.
    visible = [
        (cc, ch)
        for cc, ch in resolved
        if ch is not None and ch.permissions_for(member).view_channel
    ]
    if not visible:
        return [_("No channel activity in this window.")]

    bars = render_bar([cc.messages for cc, _ch in visible], TOP_CHANNEL_BAR_WIDTH)
    return [
        f"<#{cc.channel_id}> `{bar}` **{cc.messages}**"
        for (cc, _ch), bar in zip(visible, bars)
    ]


def _render_growth(growth):
    # TWO DIFFERENT POPULATIONS, so both lines below say which one they count:
    # the member count is guild.member_count as the gateway reports it (BOTS
    # INCLUDED, cog.py's snapshot sweep), while joins/leaves come from the
    # member events that skip bots outright (cog.py's on_member_join /
    # on_member_remove). Their two "net" figures therefore diverge by
    # construction on any guild that adds or removes a bot, and an unlabeled
    # pair reads as an arithmetic bug in the card.
    lines = []
    if growth.member_count_last is None:
        lines.append(_("No member count recorded yet."))
    elif growth.member_delta is None:
        lines.append(
            _("{count} members now").format(count=growth.member_count_last)
        )
    else:
        lines.append(
            _(
                "{count} members now (net {delta} over the window, bots included)"
            ).format(
                count=growth.member_count_last, delta=_signed(growth.member_delta)
            )
        )
    if growth.days_with_data == 0:
        # rollups.shape_growth only ever SUMS the days that exist, so with none
        # of them observed total_joins/total_leaves/net are all a structural 0.
        # Printing "0 joins / 0 leaves (+0 net)" would be exactly the invented
        # zero GrowthPoint refuses to publish per day (module rule 2) - a guild
        # whose first snapshot has not landed yet would read as a guild where
        # provably nobody joined or left. Say we were not looking instead.
        lines.append(_("No day of this window was observed yet."))
    else:
        lines.append(
            _("{joins} joins / {leaves} leaves ({net} net, humans only)").format(
                joins=growth.total_joins,
                leaves=growth.total_leaves,
                net=_signed(growth.net),
            )
        )
        if growth.days_with_data < growth.days:
            lines.append(
                _("Only {watched} of {days} days were observed.").format(
                    watched=growth.days_with_data, days=growth.days
                )
            )
    values = [point.member_count for point in growth.points]
    if values:  # an empty window has no curve to draw - never an empty `` pair
        lines.append(f"`{_level_sparkline(values)}`")
        notes = []
        if any(value is not None for value in values):
            # The baseline is the window's own low point, not zero (see
            # _level_sparkline). Say so, or an empty cell reads as "no members".
            notes.append(_("Member count, relative to this window's low point."))
        notes.append(_last_bar_note())
        lines.append("-# " + " ".join(notes))
    return lines


def _render_activity(activity):
    lines = []
    if activity.days_with_data == 0:
        # days_with_data counts the days the collector was WATCHING (see
        # rollups.shape_activity: a day outside watched_days is has_data False,
        # kept out of the total AND out of this counter). Zero of them therefore
        # means "we were not looking", never "nobody posted" - the exact
        # distinction rule 2 exists to protect, so the wording must not claim
        # silence it cannot prove.
        lines.append(_("No day of this window was observed yet."))
    else:
        lines.append(
            _("{total} messages over {watched} observed day(s)").format(
                total=activity.total_messages, watched=activity.days_with_data
            )
        )
        if activity.peak_day is not None:
            lines.append(
                _("Peak: {peak} messages on {day}").format(
                    peak=activity.peak_messages, day=activity.peak_day.isoformat()
                )
            )
    values = [point.messages for point in activity.points]
    if values:  # an empty window has no curve to draw - never an empty `` pair
        lines.append(f"`{_sparkline(values)}`")
        lines.append("-# " + _last_bar_note())
    return lines


def _render_retention(retention):
    lines = []
    for week in retention.weeks:
        if not week.has_data:
            lines.append(f"{week.week}: -")
            continue
        lines.append(
            _("{week}: net {net} ({joins} joins / {leaves} leaves)").format(
                week=week.week,
                net=_signed(week.net),
                joins=week.joins,
                leaves=week.leaves,
            )
        )
    if retention.has_activity_data:
        lines.append("")
        lines.append("**" + _("Active members (via leveling)") + "**")
        for week in retention.weeks:
            active = "-" if week.active_members is None else str(week.active_members)
            lines.append(f"{week.week}: {active}")
    return lines


# ---------------------------------------------------------------------------
# The toggle button
# ---------------------------------------------------------------------------
def _other_window(days):
    """The window a click on the toggle switches TO.

    Single source of truth for the flip: the button's LABEL and the reload's
    TARGET have to agree, and computing it twice is how a card ends up promising
    "Show 30 days" and then reloading 7.
    """
    return (
        ALT_OVERVIEW_DAYS
        if days == rollups.DEFAULT_OVERVIEW_DAYS
        else rollups.DEFAULT_OVERVIEW_DAYS
    )


class _ToggleDaysButton(discord.ui.Button):
    """Flips the overview + top-channels window between 7 and 30 days.

    Label describes the OTHER state, same convention as seasons_views.py's
    ``_AnnounceToggleButton`` - a button names the action a click performs,
    not the state it is currently in.
    """

    def __init__(self, card):
        super().__init__(
            label=_("Show {days} days").format(days=_other_window(card.days)),
            style=discord.ButtonStyle.secondary,
        )
        self.card = card

    async def callback(self, interaction):
        await self.card._toggle_days(interaction)


class ServerStatsCard(AuthorLayoutView):
    """The ``/serverstats`` Components V2 card.

    Holds ONLY the dataclasses of the CURRENT render - :class:`~.rollups.Overview`,
    the resolved top-channels list, :class:`~.rollups.Growth`,
    :class:`~.rollups.ActivitySeries` and :class:`~.rollups.RetentionReport` -
    never a history of past toggles. The 7/30-day toggle replaces ``overview``
    and ``top_channels`` in place with exactly two fresh reads (``pool`` is
    kept for that reason and touched nowhere else); growth, activity and
    retention are fixed for the card's lifetime.
    """

    def __init__(
        self,
        pool,
        guild,
        author,
        since,
        overview,
        top_channels,
        growth,
        activity,
        retention,
        *,
        timeout=180,
        chart_filename=None,
    ):
        super().__init__(author.id, timeout=timeout)
        self.pool = pool
        self.guild = guild
        # The PNG chart's attachment filename, or None when the card has no
        # chart to show (rendering failed, timed out, or was never attempted
        # - cmd_serverstats decides). Stored on self, not recomputed, so the
        # 7/30-day TOGGLE's rebuild (_build below) keeps referencing the
        # SAME attachment rather than needing a second render: the chart
        # covers the fixed 30-day series window, which the toggle never
        # touches (see the module docstring - only overview/top_channels
        # change on a click), and discord.py's edit_message call in
        # _toggle_days does not pass `attachments=`, so the file already on
        # the message survives the edit untouched.
        self.chart_filename = chart_filename
        # The invoking MEMBER, not just an id: the top-channels ranking is cut
        # to this member's own view_channel permissions (see
        # _render_top_channels). It is the Member the command was invoked with
        # (a guild_only command always has one) - never re-resolved through
        # guild.get_member, which returns None on a partial member cache
        # (chunk_guilds_at_startup=False) and would silently blank the section.
        self.author = author
        self.since = since
        self.days = overview.days
        self.overview = overview
        self.top_channels = top_channels
        self.growth = growth
        self.activity = activity
        self.retention = retention
        self._build()

    def drop_chart(self):
        """Rebuild this card WITHOUT its chart reference.

        Called by cmd_serverstats when the send that carried the attachment
        was refused (no ``attach_files`` in this channel, an upload Discord
        would not take): a Components V2 message whose MediaGallery points
        at an ``attachment://`` file that is not being uploaded is a broken
        card, so the retry has to drop the gallery, not just the file. The
        text sparklines below it already carry the same information.
        """
        self.chart_filename = None
        self._build()

    async def _toggle_days(self, interaction):
        """Swap the overview + top-channels window - exactly TWO reads, the
        ST2 contract this card promised (rollups.overview, rollups.top_channels;
        nothing else is re-queried, growth/activity/retention are untouched)."""
        try:
            new_days = _other_window(self.days)
            overview = await rollups.overview(
                self.pool, self.guild.id, days=new_days, since=self.since
            )
            top_channels = await rollups.top_channels(
                self.pool, self.guild.id, days=new_days, limit=TOP_CHANNELS_LIMIT
            )
            self.days = new_days
            self.overview = overview
            self.top_channels = top_channels
            self._build()
            await interaction.response.edit_message(view=self, allowed_mentions=_NO_PINGS)
        except Exception:
            log.exception("serverstats window toggle failed")
            # Never leave the click on Discord's own opaque "This interaction
            # failed" - see seasons_views.py's pager for the same discipline.
            await interactions.notify_failure(interaction, _("Something went wrong."))

    def _build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())

        container.add_item(
            discord.ui.TextDisplay(
                "### \N{BAR CHART} "
                + _("{guild} - Server statistics").format(guild=self.guild.name)
            )
        )
        if self.since is not None:
            container.add_item(
                discord.ui.TextDisplay(
                    "-# " + _("Data collected since {date}").format(date=self.since.isoformat())
                )
            )
        else:
            container.add_item(
                discord.ui.TextDisplay("-# " + _("No statistics collected yet."))
            )
        container.add_item(discord.ui.Separator())

        if self.chart_filename:
            # A single-item gallery pointing at the file cmd_serverstats
            # attached alongside this view - see tools.rendering.run_image_job
            # for the render seam and CHART_RENDER_TIMEOUT above for the
            # fallback discipline. Never added at all when there is no
            # chart: an empty MediaGallery is invalid, and the text
            # sparklines further down already stand on their own.
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(f"attachment://{self.chart_filename}")
                )
            )
            container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Overview ({days} days)").format(days=self.overview.days) + "**\n"
                + "\n".join(_render_overview(self.overview))
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Top channels ({days} days)").format(days=self.overview.days) + "**\n"
                + "\n".join(
                    _render_top_channels(
                        self.guild, self.top_channels, self.author
                    )
                )
            )
        )
        container.add_item(discord.ui.ActionRow(_ToggleDaysButton(self)))
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Growth ({days} days)").format(days=self.growth.days) + "**\n"
                + "\n".join(_render_growth(self.growth))
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Activity ({days} days)").format(days=self.activity.days) + "**\n"
                + "\n".join(_render_activity(self.activity))
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Retention ({weeks} weeks)").format(weeks=len(self.retention.weeks))
                + "**\n"
                + "\n".join(_render_retention(self.retention))
            )
        )

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " - "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)
