"""ST3: the ``/serverstats`` Components V2 card
(cogs/community/serverstats/views.py) and its command body
(cogs/community/serverstats/cog.py's ``cmd_serverstats``).

Covers, in order of how much it would hurt to get wrong:

1. :func:`render_bar` is pure and total: empty input, ``None`` (unknown, never
   a bar level), a peak (fully filled), an all-zero series (fully empty, NOT
   confused with unknown), and a shared scale across a whole call.
2. Every honesty rule rollups.py hands the card is actually rendered that
   way: ``delta_pct`` None prints no percentage, a partial window is labeled,
   an unknown day is a dash in the mini-series, and the retention block's
   "active members" half is omitted outright without ``has_activity_data``.
3. The unresolvable-channel drop (queries.py's contract: names are never
   resolved at read time, so the card is the one place that decides
   visibility), the INVOKER-visibility filter that keeps a public command from
   leaking a staff channel's existence and volume, and the sober empty-state
   messages.
4. The 7/30-day toggle replays EXACTLY two reads (rollups.overview,
   rollups.top_channels) - the ST2 contract - and answers the clicker even
   when a reload fails.
5. ``cmd_serverstats`` defers before its reads, reuses ``growth.watched_days``
   (zero extra query), and is author-gated / locale-resolving like every
   other :class:`~tools.views.AuthorLayoutView`.
"""

import datetime
import types

import discord

from cogs.community.serverstats import cog as serverstats_cog
from cogs.community.serverstats import rollups, views
from tools import i18n

TODAY = datetime.date(2026, 7, 28)
ONE_DAY = datetime.timedelta(days=1)


# ---------------------------------------------------------------------------
# render_bar: pure, total
# ---------------------------------------------------------------------------
def test_render_bar_empty_values_returns_empty_tuple():
    assert views.render_bar([], 10) == ()


def test_render_bar_none_is_a_placeholder_never_a_level():
    bars = views.render_bar([None, 10], 4)
    assert bars[0] == views.BAR_UNKNOWN * 4
    assert views.BAR_FILLED not in bars[0]
    assert views.BAR_EMPTY not in bars[0]


def test_render_bar_peak_value_fills_completely():
    bars = views.render_bar([10, 5, 2], 8)
    assert bars[0] == views.BAR_FILLED * 8


def test_render_bar_all_zero_known_values_render_empty_not_unknown():
    bars = views.render_bar([0, 0, 0], 6)
    assert bars == (views.BAR_EMPTY * 6,) * 3
    assert views.BAR_UNKNOWN not in "".join(bars)


def test_render_bar_shared_scale_across_the_whole_call():
    """A value that is half the peak fills roughly half the bar - and the
    peak used is the LARGEST across the whole call, not per-bar."""
    bars = views.render_bar([100, 50], 10)
    assert bars[0] == views.BAR_FILLED * 10
    assert bars[1].count(views.BAR_FILLED) == 5


def test_render_bar_width_one_degrades_to_binary():
    bars = views.render_bar([10, 4, None], 1)
    assert bars[0] == views.BAR_FILLED
    assert bars[2] == views.BAR_UNKNOWN
    assert len(bars[1]) == 1


# ---------------------------------------------------------------------------
# fixtures: build rollups value objects directly (no DB, no shape_* calls -
# this file tests the CARD's rendering, not the READ layer's shaping, which
# is already pinned by test_serverstats_rollups.py)
# ---------------------------------------------------------------------------
def _overview(**overrides):
    base = dict(
        days=7,
        end_day=TODAY - ONE_DAY,
        total_messages=1000,
        days_available=7,
        average_per_day=142.9,
        previous_messages=900,
        previous_days_available=7,
        delta_pct=11.1,
        first_day=TODAY - datetime.timedelta(days=30),
    )
    base.update(overrides)
    return rollups.Overview(**base)


def _growth(points, **overrides):
    base = dict(
        days=len(points),
        points=tuple(points),
        total_joins=sum((p.joins or 0) for p in points),
        total_leaves=sum((p.leaves or 0) for p in points),
        net=sum((p.joins or 0) for p in points) - sum((p.leaves or 0) for p in points),
        days_with_data=sum(1 for p in points if p.has_data),
        member_count_first=next(
            (p.member_count for p in points if p.member_count is not None), None
        ),
        member_count_last=next(
            (p.member_count for p in reversed(points) if p.member_count is not None), None
        ),
    )
    base.update(overrides)
    return rollups.Growth(**base)


def _activity(points, **overrides):
    known = [(p.day, p.messages) for p in points if p.has_data]
    peak_day, peak_messages = (None, 0)
    if known:
        peak_day, peak_messages = max(known, key=lambda pair: pair[1])
    base = dict(
        days=len(points),
        points=tuple(points),
        total_messages=sum(m for _d, m in known),
        days_with_data=len(known),
        peak_day=peak_day,
        peak_messages=peak_messages,
    )
    base.update(overrides)
    return rollups.ActivitySeries(**base)


def _retention(weeks, **overrides):
    base = dict(
        weeks=tuple(weeks),
        leveling=False,
        has_activity_data=any(w.active_members is not None for w in weeks),
    )
    base.update(overrides)
    return rollups.RetentionReport(**base)


class _FakeMember:
    def __init__(self, member_id=1):
        self.id = member_id


class _FakeChannel:
    """A channel whose ``permissions_for`` answers PER MEMBER.

    ``visible_to`` is a set of member ids allowed to read the channel, or None
    for "everyone can" - the /serverstats card cuts its top-channels ranking to
    the invoker's own view_channel, so this is the fake that makes a staff-only
    channel actually staff-only in the tests.
    """

    def __init__(self, visible_to=None):
        self.visible_to = visible_to

    def permissions_for(self, member):
        allowed = self.visible_to is None or member.id in self.visible_to
        return types.SimpleNamespace(view_channel=allowed)


class _FakeGuild:
    def __init__(self, guild_id=1, name="Guild", channels=None):
        self.id = guild_id
        self.name = name
        self._channels = channels or {}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


def _dump_text(view):
    chunks = []

    def walk(item):
        content = getattr(item, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        for child in getattr(item, "children", None) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return "\n".join(chunks)


def _text_displays(view):
    return [
        child
        for child in view.walk_children()
        if isinstance(getattr(child, "content", None), str)
    ]


def _button_labels(view):
    return [
        child.label for child in view.walk_children() if isinstance(child, discord.ui.Button)
    ]


def _card(guild=None, since=TODAY - datetime.timedelta(days=30), overview=None,
          top_channels=None, growth=None, activity=None, retention=None, pool=None,
          author=None):
    guild = guild or _FakeGuild()
    overview = overview or _overview()
    top_channels = top_channels if top_channels is not None else []
    growth = growth or _growth([])
    activity = activity or _activity([])
    retention = retention or _retention([])
    return views.ServerStatsCard(
        pool,
        guild,
        author or _FakeMember(1),
        since,
        overview,
        top_channels,
        growth,
        activity,
        retention,
    )


# ---------------------------------------------------------------------------
# Overview honesty
# ---------------------------------------------------------------------------
def test_overview_no_delta_renders_no_percentage():
    card = _card(overview=_overview(delta_pct=None))
    text = _dump_text(card)
    assert "%" not in text
    assert "Not enough history" in text


def test_overview_delta_is_rendered_with_the_comparison_window():
    card = _card(overview=_overview(delta_pct=11.1, days=7, end_day=TODAY - ONE_DAY))
    text = _dump_text(card)
    assert "+11.1%" in text
    assert (TODAY - ONE_DAY).isoformat() in text


def test_overview_negative_delta_keeps_its_own_sign():
    card = _card(overview=_overview(delta_pct=-8.0))
    text = _dump_text(card)
    assert "-8.0%" in text
    assert "+-8.0%" not in text


def test_overview_partial_window_is_labeled():
    card = _card(overview=_overview(days=7, days_available=3, delta_pct=None))
    text = _dump_text(card)
    assert "Partial window" in text
    assert "3 of 7" in text


def test_overview_labels_its_complete_day_window_even_without_a_delta():
    """The overview stops at YESTERDAY (rollups.overview_bounds) while the rest
    of the card includes today, so the end day qualifies the total and the
    average - not just the delta. It must be stated in every branch, including
    the one where no delta can be published."""
    card = _card(overview=_overview(delta_pct=None, end_day=TODAY - ONE_DAY))
    text = _dump_text(card)
    assert "Complete days only, through {day}.".format(day=(TODAY - ONE_DAY).isoformat()) in text


def test_overview_states_the_end_day_exactly_once_when_a_delta_exists():
    card = _card(overview=_overview(delta_pct=11.1, end_day=TODAY - ONE_DAY))
    text = _dump_text(card)
    assert text.count((TODAY - ONE_DAY).isoformat()) == 1


def test_overview_zero_days_available_publishes_no_number_at_all():
    """With no complete day observed, total_messages is a structural 0 - the
    overview must say it was not looking, not print "0 messages" (the same lie
    the growth and activity blocks refuse to tell, in the same words)."""
    card = _card(
        overview=_overview(
            days_available=0, total_messages=0, average_per_day=0.0, delta_pct=None
        )
    )
    text = _dump_text(card)
    assert "No day of this window was observed yet." in text
    assert "/day" not in text
    assert "0 messages" not in text


# ---------------------------------------------------------------------------
# Top channels: unresolvable drop, INVOKER visibility, proportional bars,
# sober empty state
# ---------------------------------------------------------------------------
def test_top_channels_drops_a_channel_the_bot_cannot_see():
    guild = _FakeGuild(channels={1: _FakeChannel()})  # channel 2 is unresolvable
    top = [rollups.ChannelCount(1, 100), rollups.ChannelCount(2, 999)]
    card = _card(guild=guild, top_channels=top)
    text = _dump_text(card)
    assert "<#1>" in text
    assert "<#2>" not in text
    assert "999" not in text


def test_top_channels_hides_a_channel_the_invoker_cannot_read():
    """/serverstats is PUBLIC: rendering with the BOT's visibility would leak
    both the existence of a staff-only channel and how busy it is to any member
    who runs the command."""
    guild = _FakeGuild(
        channels={1: _FakeChannel(), 2: _FakeChannel(visible_to={99})}  # staff only
    )
    top = [rollups.ChannelCount(1, 100), rollups.ChannelCount(2, 999)]
    card = _card(guild=guild, top_channels=top, author=_FakeMember(1))
    text = _dump_text(card)
    assert "<#1>" in text
    assert "<#2>" not in text
    assert "999" not in text


def test_top_channels_shows_a_private_channel_to_a_member_who_can_read_it():
    """The same card, the same rows: the filter is the CLICKER's own
    view_channel, not a blanket hide."""
    guild = _FakeGuild(
        channels={1: _FakeChannel(), 2: _FakeChannel(visible_to={99})}
    )
    top = [rollups.ChannelCount(1, 100), rollups.ChannelCount(2, 999)]
    card = _card(guild=guild, top_channels=top, author=_FakeMember(99))
    text = _dump_text(card)
    assert "<#2>" in text
    assert "999" in text


def test_top_channels_filtered_empty_falls_back_to_the_sober_message():
    """A member who can read none of the busiest channels gets the same empty
    state as a guild with no data - never a blank section."""
    guild = _FakeGuild(channels={1: _FakeChannel(visible_to={99})})
    card = _card(
        guild=guild,
        top_channels=[rollups.ChannelCount(1, 100)],
        author=_FakeMember(1),
    )
    text = _dump_text(card)
    assert "No channel activity in this window." in text
    assert "<#1>" not in text


def test_top_channels_visibility_is_refiltered_by_the_window_toggle():
    """The filter lives at RENDER time, so it re-applies on every rebuild -
    a 7/30-day toggle can never resurrect a hidden channel."""
    guild = _FakeGuild(channels={1: _FakeChannel(visible_to={99})})
    card = _card(
        guild=guild,
        top_channels=[rollups.ChannelCount(1, 100)],
        author=_FakeMember(1),
    )
    card.top_channels = [rollups.ChannelCount(1, 4242)]
    card._build()
    text = _dump_text(card)
    assert "<#1>" not in text
    assert "4242" not in text


def test_top_channels_empty_after_resolution_is_a_sober_message():
    guild = _FakeGuild(channels={})
    top = [rollups.ChannelCount(1, 100)]
    card = _card(guild=guild, top_channels=top)
    text = _dump_text(card)
    assert "No channel activity in this window." in text


def test_top_channels_bar_is_proportional_to_the_busiest_channel():
    guild = _FakeGuild(channels={1: _FakeChannel(), 2: _FakeChannel()})
    top = [rollups.ChannelCount(1, 100), rollups.ChannelCount(2, 50)]
    card = _card(guild=guild, top_channels=top)
    text = _dump_text(card)
    full_bar = views.BAR_FILLED * views.TOP_CHANNEL_BAR_WIDTH
    assert full_bar in text  # the busiest channel is always fully filled


# ---------------------------------------------------------------------------
# Growth honesty
# ---------------------------------------------------------------------------
def test_growth_unknown_day_is_a_dash_in_the_sparkline_not_a_bar_level():
    points = [
        rollups.GrowthPoint(TODAY - datetime.timedelta(days=1), None, None, None, False),
        rollups.GrowthPoint(TODAY, 100, 2, 1, True),
    ]
    card = _card(growth=_growth(points))
    text = _dump_text(card)
    assert views.BAR_UNKNOWN in text


def test_growth_no_member_delta_omits_net_wording():
    points = [rollups.GrowthPoint(TODAY, 100, 1, 0, True)]
    growth = _growth(points, member_count_first=None, member_count_last=None)
    card = _card(growth=growth)
    text = _dump_text(card)
    assert "No member count recorded yet." in text
    assert "net" not in text.lower().split("joins")[0]  # no net mentioned before the joins line


def test_level_sparkline_distinguishes_a_shrinking_guild_from_a_growing_one():
    """Member counts are a LEVEL series: fed to render_bar from a zero baseline
    they saturate every bar, so a guild that lost a third of its members drew
    the same solid line as one that grew 40%. Rebasing to the window's own low
    point is what makes the growth curve show growth."""
    collapse = views._level_sparkline([1500, 1400, 1300, 1200, 1100, 1000])
    grew = views._level_sparkline([1000, 1080, 1160, 1240, 1320, 1400])
    assert collapse != grew
    assert collapse.startswith(views.BAR_FILLED)
    assert collapse.endswith(views.BAR_EMPTY)
    assert grew.startswith(views.BAR_EMPTY)
    assert grew.endswith(views.BAR_FILLED)


def test_level_sparkline_keeps_holes_as_holes_and_flat_as_empty():
    assert views._level_sparkline([None, 100, 200])[0] == views.BAR_UNKNOWN
    # A perfectly flat window rebases to all zeroes: empty ("no change"), never
    # the dashes that mean "unknown".
    flat = views._level_sparkline([500, 500, 500])
    assert flat == views.BAR_EMPTY * 3
    assert views.BAR_UNKNOWN not in flat
    assert views._level_sparkline([]) == ""
    assert views._level_sparkline([None, None]) == views.BAR_UNKNOWN * 2


def test_growth_labels_which_population_each_net_counts():
    """The two figures come from different populations by construction:
    member_count is guild.member_count (BOTS INCLUDED), joins/leaves skip bots
    outright. Unlabeled, a guild that added a bot reads as a card that cannot
    add up."""
    points = [
        rollups.GrowthPoint(TODAY - ONE_DAY, 100, 1, 0, True),
        rollups.GrowthPoint(TODAY, 104, 1, 0, True),
    ]
    card = _card(growth=_growth(points, days=2))
    text = _dump_text(card)
    assert "bots included" in text  # the member-count line
    assert "humans only" in text  # the joins/leaves line


def test_growth_sparkline_baseline_is_labeled():
    points = [
        rollups.GrowthPoint(TODAY - ONE_DAY, 100, 1, 0, True),
        rollups.GrowthPoint(TODAY, 140, 1, 0, True),
    ]
    card = _card(growth=_growth(points, days=2))
    text = _dump_text(card)
    assert "Member count, relative to this window's low point." in text


def test_growth_zero_observed_days_invents_no_joins_or_leaves():
    """rollups.shape_growth only sums the days that EXIST, so a window with no
    observed day carries a structural total_joins/total_leaves/net of 0.
    Rendering that as "0 joins / 0 leaves (+0 net)" would publish a fact nobody
    measured - the same lie GrowthPoint refuses to tell per day."""
    points = [
        rollups.GrowthPoint(TODAY - ONE_DAY, None, None, None, False),
        rollups.GrowthPoint(TODAY, None, None, None, False),
    ]
    card = _card(growth=_growth(points, days=2))
    text = _dump_text(card)
    assert "No day of this window was observed yet." in text
    assert "joins" not in text
    assert "+0 net" not in text


def test_growth_partial_watched_days_is_noted():
    points = [
        rollups.GrowthPoint(TODAY - datetime.timedelta(days=1), None, None, None, False),
        rollups.GrowthPoint(TODAY, 100, 1, 0, True),
    ]
    growth = _growth(points, days=2, days_with_data=1)
    card = _card(growth=growth)
    text = _dump_text(card)
    assert "Only 1 of 2 days were observed." in text


# ---------------------------------------------------------------------------
# Activity honesty
# ---------------------------------------------------------------------------
def test_activity_zero_observed_days_says_unobserved_not_silent():
    """``days_with_data`` counts WATCHED days, so zero of them means the
    collector was not looking - the card must not word that as "no activity",
    which would claim a silence it cannot prove (honesty rule 2)."""
    points = [rollups.ActivityPoint(TODAY, None, False)]
    card = _card(activity=_activity(points))
    text = _dump_text(card)
    assert "No day of this window was observed yet." in text
    assert "No activity" not in text
    assert "observed day(s)" not in text  # no invented "0 messages over 0 days"


def test_both_sparklines_label_their_last_bar_as_the_day_in_progress():
    """The SERIES windows keep today (rollups.window_bounds), so the rightmost
    bar is a few hours of today drawn against full days - it reads as a crash
    every morning unless the card says what it is."""
    growth = _growth(
        [
            rollups.GrowthPoint(TODAY - ONE_DAY, 100, 1, 0, True),
            rollups.GrowthPoint(TODAY, 120, 1, 0, True),
        ],
        days=2,
    )
    activity = _activity(
        [
            rollups.ActivityPoint(TODAY - ONE_DAY, 500, True),
            rollups.ActivityPoint(TODAY, 20, True),
        ]
    )
    card = _card(growth=growth, activity=activity)
    text = _dump_text(card)
    assert text.count("Last bar: today, still in progress.") == 2


def test_a_card_with_no_series_draws_no_last_bar_note():
    """No bar line, nothing to label - the note must not float alone."""
    card = _card(growth=_growth([]), activity=_activity([]))
    assert "Last bar" not in _dump_text(card)


def test_activity_peak_is_reported():
    points = [
        rollups.ActivityPoint(TODAY - ONE_DAY, 50, True),
        rollups.ActivityPoint(TODAY, 900, True),
    ]
    card = _card(activity=_activity(points))
    text = _dump_text(card)
    assert "Peak: 900 messages on {day}".format(day=TODAY.isoformat()) in text


# ---------------------------------------------------------------------------
# Retention honesty
# ---------------------------------------------------------------------------
def test_retention_unknown_week_is_a_dash():
    weeks = [rollups.RetentionWeek("W2026-20", None, None, None, has_data=False)]
    card = _card(retention=_retention(weeks))
    text = _dump_text(card)
    assert "W2026-20: -" in text


def test_retention_active_members_hidden_without_activity_data():
    weeks = [rollups.RetentionWeek("W2026-31", 5, 2, None, has_data=True)]
    card = _card(retention=_retention(weeks, leveling=False, has_activity_data=False))
    text = _dump_text(card)
    assert "Active members" not in text


def test_retention_active_members_shown_and_labeled_via_leveling():
    weeks = [rollups.RetentionWeek("W2026-31", 5, 2, 42, has_data=True)]
    card = _card(retention=_retention(weeks, leveling=True, has_activity_data=True))
    text = _dump_text(card)
    assert "Active members (via leveling)" in text
    assert "W2026-31: 42" in text


# ---------------------------------------------------------------------------
# Toggle: exactly two reads replayed (the ST2 contract)
# ---------------------------------------------------------------------------
def _wire_overview_and_top(fake_pool, *, current=500, previous=400, first_day=None):
    fake_pool.fetchrow_return = {
        "current_messages": current,
        "previous_messages": previous,
        "first_day": first_day,
    }
    fake_pool.fetch_return = [{"channel_id": 1, "messages": current}]


async def test_toggle_replays_exactly_two_reads(fake_pool, make_interaction):
    _wire_overview_and_top(fake_pool)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    card = _card(guild=guild, pool=fake_pool, overview=_overview(days=7))
    card.message = types.SimpleNamespace()

    await card._toggle_days(make_interaction())

    assert len(fake_pool.calls) == 2
    methods = [c[0] for c in fake_pool.calls]
    assert methods == ["fetchrow", "fetch"]
    assert card.days == views.ALT_OVERVIEW_DAYS


async def test_toggle_flips_back_to_seven_on_a_second_click(fake_pool, make_interaction):
    _wire_overview_and_top(fake_pool)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    card = _card(guild=guild, pool=fake_pool, overview=_overview(days=7))
    card.message = types.SimpleNamespace()

    await card._toggle_days(make_interaction())
    assert card.days == views.ALT_OVERVIEW_DAYS
    fake_pool.calls.clear()
    await card._toggle_days(make_interaction())
    assert card.days == rollups.DEFAULT_OVERVIEW_DAYS
    assert len(fake_pool.calls) == 2


async def test_toggle_never_touches_growth_activity_or_retention(fake_pool, make_interaction):
    """The toggle only affects Overview + Top channels; growth/activity/
    retention state on the card must be untouched (same objects, no re-read)."""
    _wire_overview_and_top(fake_pool)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    growth = _growth([rollups.GrowthPoint(TODAY, 10, 1, 0, True)])
    card = _card(guild=guild, pool=fake_pool, growth=growth)
    card.message = types.SimpleNamespace()
    original_growth = card.growth

    await card._toggle_days(make_interaction())

    assert card.growth is original_growth


def test_toggle_button_label_names_the_window_it_will_load():
    """The label and the reload target come from the same helper; if they ever
    drift the card promises one window and loads the other."""
    card = _card(overview=_overview(days=rollups.DEFAULT_OVERVIEW_DAYS))
    assert "Show {days} days".format(days=views.ALT_OVERVIEW_DAYS) in _button_labels(card)

    wide = _card(overview=_overview(days=views.ALT_OVERVIEW_DAYS))
    assert "Show {days} days".format(
        days=rollups.DEFAULT_OVERVIEW_DAYS
    ) in _button_labels(wide)


async def test_a_failing_toggle_notifies_the_clicker(fake_pool, make_interaction, caplog):
    guild = _FakeGuild(channels={1: _FakeChannel()})
    card = _card(guild=guild, pool=fake_pool)
    card.message = types.SimpleNamespace()

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    fake_pool.fetchrow = boom

    interaction = make_interaction()
    await card._toggle_days(interaction)  # must not raise

    assert interaction.sent
    assert interaction.sent[0][1]["ephemeral"] is True


# ---------------------------------------------------------------------------
# Card plumbing: author-gate + locale (shared AuthorLayoutView contract)
# ---------------------------------------------------------------------------
async def test_card_rejects_a_non_author_interaction(make_interaction):
    card = _card()
    interaction = make_interaction(user_id=999)

    allowed = await card.interaction_check(interaction)

    assert allowed is False
    assert interaction.sent


async def test_card_resolves_the_clicker_locale(make_interaction, monkeypatch):
    card = _card()
    interaction = make_interaction(user_id=1)
    calls = []

    async def _spy(interaction_arg):
        calls.append(interaction_arg)

    monkeypatch.setattr(i18n, "apply_interaction_locale", _spy)

    allowed = await card.interaction_check(interaction)

    assert allowed is True
    assert calls == [interaction]


def test_card_uses_allowed_mentions_none_on_edit():
    assert views._NO_PINGS.users is False
    assert views._NO_PINGS.roles is False


# ---------------------------------------------------------------------------
# Components V2 budget: the card is built from FULL-SIZE inputs (the widest
# windows the command can ask rollups for) and must still fit the API limits -
# 40 components in a message, 4000 characters of text across all of them, and
# no empty TextDisplay (which the API rejects outright).
# ---------------------------------------------------------------------------
MAX_COMPONENTS = 40
MAX_TEXT_CHARS = 4000


def _worst_case_card():
    days = rollups.DEFAULT_SERIES_DAYS
    growth_points = [
        rollups.GrowthPoint(
            TODAY - datetime.timedelta(days=offset), 999999, 999, 999, True
        )
        for offset in range(days - 1, -1, -1)
    ]
    activity_points = [
        rollups.ActivityPoint(TODAY - datetime.timedelta(days=offset), 999999, True)
        for offset in range(days - 1, -1, -1)
    ]
    weeks = [
        rollups.RetentionWeek(key, 99999, 99999, 99999, has_data=True)
        for key in rollups.week_keys(TODAY, rollups.MAX_RETENTION_WEEKS)
    ]
    channel_ids = [10**18 + index for index in range(views.TOP_CHANNELS_LIMIT)]
    guild = _FakeGuild(
        name="G" * 100,  # Discord's guild-name ceiling
        channels={channel_id: _FakeChannel() for channel_id in channel_ids},
    )
    return _card(
        guild=guild,
        overview=_overview(days=30, days_available=3),  # partial -> extra line
        top_channels=[
            rollups.ChannelCount(channel_id, 999999) for channel_id in channel_ids
        ],
        growth=_growth(growth_points, days=days),
        activity=_activity(activity_points, days=days),
        retention=_retention(weeks, leveling=True, has_activity_data=True),
    )


def test_worst_case_card_fits_the_component_budget():
    card = _worst_case_card()
    assert len(list(card.walk_children())) <= MAX_COMPONENTS


def test_worst_case_card_fits_the_text_budget():
    card = _worst_case_card()
    assert sum(len(t.content) for t in _text_displays(card)) <= MAX_TEXT_CHARS


def test_no_text_display_is_ever_empty():
    """An empty TextDisplay is a 400 from Discord. The emptiest possible card -
    no data anywhere - must still give every block something to say."""
    empty = _card(
        since=None,
        overview=_overview(days_available=0, average_per_day=0.0, delta_pct=None),
        top_channels=[],
        growth=_growth([]),
        activity=_activity([]),
        retention=_retention([]),
    )
    for card in (empty, _worst_case_card()):
        for text in _text_displays(card):
            assert text.content.strip(), "empty TextDisplay in the card"
            assert len(text.content) <= MAX_TEXT_CHARS


# ---------------------------------------------------------------------------
# cmd_serverstats: defers first, threads watched_days, gathers every read
# ---------------------------------------------------------------------------
class _RoutingPool:
    """Answers each rollups SQL constant with its own canned result (keyed by
    the SQL string's identity), and records every call into ``trace`` -
    SHARED with the fake ctx's ``defer`` so ordering can be asserted."""

    def __init__(self, answers, trace):
        self.answers = answers
        self.trace = trace
        self.calls = []

    async def fetchval(self, query, *args):
        self.trace.append("read")
        self.calls.append(("fetchval", query, args))
        return self.answers.get(query)

    async def fetchrow(self, query, *args):
        self.trace.append("read")
        self.calls.append(("fetchrow", query, args))
        return self.answers.get(query)

    async def fetch(self, query, *args):
        self.trace.append("read")
        self.calls.append(("fetch", query, args))
        return self.answers.get(query, [])


class _Ctx:
    def __init__(self, guild, author_id, trace):
        self.guild = guild
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.sends = []
        self.trace = trace

    async def defer(self, **kwargs):
        self.trace.append("defer")

    async def send(self, *args, **kwargs):
        self.trace.append("send")
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


def _make_answers(*, leveling=False):
    answers = {
        rollups.DATA_SINCE: TODAY - datetime.timedelta(days=30),
        rollups.OVERVIEW: {
            "current_messages": 500,
            "previous_messages": 400,
            "first_day": TODAY - datetime.timedelta(days=30),
        },
        rollups.TOP_CHANNELS: [{"channel_id": 1, "messages": 500}],
        rollups.GROWTH: [
            {
                "day": TODAY,
                "member_count": 100,
                "joins": 2,
                "leaves": 1,
            }
        ],
        rollups.ACTIVITY_SERIES: [{"day": TODAY, "messages": 500}],
        rollups.RETENTION_NET: [{"week_key": "W2026-31", "joins": 2, "leaves": 1}],
    }
    if leveling:
        answers[rollups.RETENTION_ACTIVITY] = [
            {"week_key": "W2026-31", "active_members": 7}
        ]
    return answers


def _make_cog(pool, leveling_cog=None):
    bot = types.SimpleNamespace(
        db_pool=pool, get_cog=lambda name: leveling_cog if name == "Leveling" else None
    )
    return serverstats_cog.ServerStats(bot)


async def test_cmd_serverstats_defers_before_any_read():
    trace = []
    pool = _RoutingPool(_make_answers(), trace)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    ctx = _Ctx(guild, 1, trace)
    cog = _make_cog(pool)

    await cog.cmd_serverstats(ctx)

    assert trace[0] == "defer"
    assert "read" in trace
    assert trace.index("defer") < trace.index("read")


async def test_cmd_serverstats_sends_a_card_with_allowed_mentions_none():
    trace = []
    pool = _RoutingPool(_make_answers(), trace)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    ctx = _Ctx(guild, 1, trace)
    cog = _make_cog(pool)

    await cog.cmd_serverstats(ctx)

    args, kwargs = ctx.sends[0]
    assert isinstance(kwargs["view"], views.ServerStatsCard)
    assert kwargs["allowed_mentions"].users is False


async def test_cmd_serverstats_reuses_growth_watched_days_zero_extra_query():
    trace = []
    pool = _RoutingPool(_make_answers(), trace)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    ctx = _Ctx(guild, 1, trace)
    cog = _make_cog(pool)

    await cog.cmd_serverstats(ctx)

    assert not any(query == rollups.WATCHED_DAYS for _m, query, _a in pool.calls)


async def test_cmd_serverstats_issues_exactly_six_reads_without_leveling():
    """The query budget, COUNTED, not assumed: data_since, overview,
    top_channels, growth, activity_series and retention_net - six, and no
    seventh. rollups.activity_series would pay a WATCHED_DAYS read of its own if
    the command stopped threading growth.watched_days through, and this is the
    test that notices."""
    trace = []
    pool = _RoutingPool(_make_answers(), trace)
    ctx = _Ctx(_FakeGuild(channels={1: _FakeChannel()}), 1, trace)
    cog = _make_cog(pool)

    await cog.cmd_serverstats(ctx)

    assert [query for _m, query, _a in pool.calls] == [
        rollups.DATA_SINCE,
        rollups.OVERVIEW,
        rollups.TOP_CHANNELS,
        rollups.GROWTH,
        rollups.ACTIVITY_SERIES,
        rollups.RETENTION_NET,
    ]


async def test_cmd_serverstats_issues_exactly_seven_reads_with_leveling():
    """The leveling guild pays exactly ONE extra read (the distinct-actives
    count), never a second pass over anything else."""
    trace = []
    pool = _RoutingPool(_make_answers(leveling=True), trace)
    ctx = _Ctx(_FakeGuild(channels={1: _FakeChannel()}), 1, trace)
    cog = _make_cog(pool, leveling_cog=types.SimpleNamespace(is_enabled=lambda gid: True))

    await cog.cmd_serverstats(ctx)

    assert len(pool.calls) == 7
    assert pool.calls[-1][1] == rollups.RETENTION_ACTIVITY


async def test_cmd_serverstats_without_leveling_hides_active_members():
    trace = []
    pool = _RoutingPool(_make_answers(leveling=False), trace)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    ctx = _Ctx(guild, 1, trace)
    cog = _make_cog(pool, leveling_cog=None)

    await cog.cmd_serverstats(ctx)

    view = ctx.sends[0][1]["view"]
    assert view.retention.has_activity_data is False
    assert not any(query == rollups.RETENTION_ACTIVITY for _m, query, _a in pool.calls)


async def test_cmd_serverstats_with_leveling_enabled_reads_active_members():
    trace = []
    pool = _RoutingPool(_make_answers(leveling=True), trace)
    guild = _FakeGuild(channels={1: _FakeChannel()})
    ctx = _Ctx(guild, 1, trace)
    leveling_cog = types.SimpleNamespace(is_enabled=lambda guild_id: True)
    cog = _make_cog(pool, leveling_cog=leveling_cog)

    await cog.cmd_serverstats(ctx)

    view = ctx.sends[0][1]["view"]
    assert view.retention.has_activity_data is True
    assert any(query == rollups.RETENTION_ACTIVITY for _m, query, _a in pool.calls)


def test_serverstats_command_description_is_a_literal_english_string():
    """House rule: slash command descriptions are English literals, never
    wrapped in _() (they are not user-prose, they are the command metadata
    Discord itself sends to the client)."""
    command = serverstats_cog.ServerStats.serverstats
    assert command.help == "Show this server's activity and growth statistics."
    assert "_(" not in (command.callback.__doc__ or "")
