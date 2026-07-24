"""Unit tests for the pure helpers in ``cogs/anilist/chapters.py``.

Everything exercised here is pure and side-effect-free (no network, DB, Discord
or Lavalink): the seen-key (de)serialisation that bridges the pure
:func:`tools.mangadex.chapter_key` tuples and the ``mangadex_seen_chapters`` TEXT
column, the chapter-number / timestamp formatters the card leans on, the per-tick
alert cap, the fan-out target planner, and - the highest-value guard - that the
persistent Read button's custom_id template is fullmatch-disjoint from every other
``alf:`` DynamicItem template, so discord.py's dispatch can never cross-route a
click. Importing the cog module is enough; nothing in it runs at import time.
"""

import re
from datetime import datetime, timedelta, timezone

from cogs.anilist import chapters as ch
from cogs.anilist.airing import SEEN_TEMPLATE
from cogs.anilist.chapters import (
    MAX_ALERTS_PER_MANGA,
    POLL_SECONDS,
    READ_TEMPLATE,
    _cap_alerts,
    _chapter_number_str,
    _chapter_timestamp,
    _deserialize_key,
    _search_title,
    _serialize_key,
    _sub_media,
    plan_chapter_targets,
)
from cogs.anilist.feed import ADD_TEMPLATE, LIKE_TEMPLATE, REPLY_TEMPLATE
from tools import mangadex as md
from tools import round_robin as rr

# ---------------------------------------------------------------------------
# documented constants (guard the tuned values the design leans on)
# ---------------------------------------------------------------------------


def test_documented_constants():
    assert POLL_SECONDS == 1800
    assert MAX_ALERTS_PER_MANGA == 3
    assert ch.MAX_MAPPING_SEARCHES_PER_TICK == 3
    assert ch.MISSING_RETRY_DAYS == 7
    assert ch.LIST_FETCH_BUDGET == 10
    assert ch.FEED_BUDGET == 25
    assert ch.MAX_FEED_PAGES == 6
    assert ch.POLL_PHASE_OFFSET == 60
    assert ch.SEEN_PRUNE_DAYS == 90
    assert ch.SEEN_PRUNE_KEEP == 200


# ---------------------------------------------------------------------------
# _serialize_key / _deserialize_key - the seen-memory <-> TEXT bridge
# ---------------------------------------------------------------------------


def test_serialize_key_shapes():
    assert _serialize_key(("ch", "386")) == "ch:386"
    assert _serialize_key(("ch", "110.5")) == "ch:110.5"
    assert _serialize_key(("id", "abc-uuid")) == "id:abc-uuid"


def test_deserialize_key_roundtrips_numeric_and_id():
    for key in [("ch", "386"), ("ch", "110.5"), ("id", "abc-uuid-123")]:
        assert _deserialize_key(_serialize_key(key)) == key


def test_deserialize_key_preserves_colon_in_value():
    # A non-numeric label keeps its stripped text (see chapter_key), which can
    # itself carry a colon; the split is on the FIRST colon so it round-trips.
    key = ("ch", "Extra: Part 1")
    assert _serialize_key(key) == "ch:Extra: Part 1"
    assert _deserialize_key("ch:Extra: Part 1") == key


def test_serialize_matches_mangadex_chapter_key():
    # The end-to-end contract: what plan_chapter_alerts hands back as a key must
    # serialise and reload identically, so the loaded seen set compares to fresh
    # keys exactly.
    key = md.chapter_key({"volume": "38", "chapter": "386"})
    assert key == ("ch", "386")
    assert _deserialize_key(_serialize_key(key)) == key

    oneshot = md.chapter_key({"id": "one", "chapter": None})
    assert oneshot == ("id", "one")
    assert _deserialize_key(_serialize_key(oneshot)) == oneshot


# ---------------------------------------------------------------------------
# _chapter_number_str - custom_id-safe chapter number, or None
# ---------------------------------------------------------------------------


def test_chapter_number_str_integer_forms():
    assert _chapter_number_str("386") == "386"
    assert _chapter_number_str(386) == "386"
    assert _chapter_number_str(" 42 ") == "42"


def test_chapter_number_str_decimal_kept():
    assert _chapter_number_str("110.5") == "110.5"
    assert _chapter_number_str("386.0") == "386.0"


def test_chapter_number_str_non_numeric_is_none():
    assert _chapter_number_str(None) is None
    assert _chapter_number_str("") is None
    assert _chapter_number_str("   ") is None
    assert _chapter_number_str("Extra") is None
    assert _chapter_number_str("12a") is None


def test_chapter_number_str_output_fits_read_template():
    # Whatever it returns must sit inside the Read template's chapter group, or the
    # button it builds could not dispatch.
    pattern = re.compile(READ_TEMPLATE)
    for raw in ("386", 386, "110.5", "386.0", " 7 "):
        num = _chapter_number_str(raw)
        assert num is not None
        assert pattern.fullmatch("alf:read:42:{n}".format(n=num))


# ---------------------------------------------------------------------------
# _chapter_timestamp - MangaDex readableAt ISO string -> epoch int
# ---------------------------------------------------------------------------


def test_chapter_timestamp_z_offset_and_naive_agree():
    expected = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
    assert _chapter_timestamp("2023-01-01T00:00:00Z") == expected
    assert _chapter_timestamp("2023-01-01T00:00:00+00:00") == expected
    # A naive value is read as UTC.
    assert _chapter_timestamp("2023-01-01T00:00:00") == expected


def test_chapter_timestamp_junk_and_empty_are_none():
    assert _chapter_timestamp(None) is None
    assert _chapter_timestamp("") is None
    assert _chapter_timestamp("   ") is None
    assert _chapter_timestamp("not-a-date") is None


# ---------------------------------------------------------------------------
# _search_title - romaji, then english, else None
# ---------------------------------------------------------------------------


def test_search_title_prefers_romaji_then_english():
    assert _search_title({"title": {"romaji": "Kingdom", "english": "Kingdom EN"}}) == "Kingdom"
    assert _search_title({"title": {"english": "Only English"}}) == "Only English"


def test_search_title_none_when_absent():
    assert _search_title({}) is None
    assert _search_title({"title": {}}) is None
    assert _search_title(None) is None


# ---------------------------------------------------------------------------
# _cap_alerts - newest kept, oldest dropped (alerts arrive oldest-first)
# ---------------------------------------------------------------------------


def test_cap_alerts_below_cap_keeps_all():
    alerts = [{"id": 1}, {"id": 2}]
    kept, dropped = _cap_alerts(alerts, cap=3)
    assert kept == alerts
    assert dropped == []


def test_cap_alerts_at_cap_keeps_all():
    alerts = [{"id": 1}, {"id": 2}, {"id": 3}]
    kept, dropped = _cap_alerts(alerts, cap=3)
    assert kept == alerts
    assert dropped == []


def test_cap_alerts_above_cap_keeps_newest_drops_oldest():
    # Oldest-first input: [old ... new]; keeping the newest `cap` keeps the tail.
    alerts = [{"id": i} for i in range(1, 6)]  # 1..5
    kept, dropped = _cap_alerts(alerts, cap=3)
    assert [a["id"] for a in kept] == [3, 4, 5]
    assert [a["id"] for a in dropped] == [1, 2]


def test_cap_alerts_default_cap_is_module_constant():
    alerts = [{"id": i} for i in range(1, 8)]  # 7 alerts
    kept, dropped = _cap_alerts(alerts)
    assert len(kept) == MAX_ALERTS_PER_MANGA
    assert [a["id"] for a in kept] == [5, 6, 7]
    assert len(dropped) == 7 - MAX_ALERTS_PER_MANGA


# ---------------------------------------------------------------------------
# plan_chapter_targets - who receives an alert for a manga
#
# dm_lists_by_user maps a Discord user to the manga on their Reading list; the
# channel_media map is now built from a feed's EXPLICIT title subscriptions
# (anilist_channel_subs), not from its followed users' lists. The planner's shape
# is unchanged: a media-id-set membership test per side.
# ---------------------------------------------------------------------------


def test_plan_targets_dm_membership():
    dm = {100: {10, 20}, 200: {20, 30}, 300: {40}}
    dm_users, channels = plan_chapter_targets(20, dm, {})
    assert dm_users == [100, 200]  # sorted, only those reading media 20
    assert channels == []


def test_plan_targets_channel_membership():
    # channel_media is the per-feed SUBSCRIBED media-id set.
    channel_media = {(1, 111): {10, 20}, (2, 222): {30}}
    dm_users, channels = plan_chapter_targets(10, {}, channel_media)
    assert dm_users == []
    assert channels == [(1, 111)]


def test_plan_targets_both_and_sorted():
    dm = {900: {5}, 100: {5, 6}}
    channel_media = {(2, 20): {5}, (1, 10): {5}}
    dm_users, channels = plan_chapter_targets(5, dm, channel_media)
    assert dm_users == [100, 900]
    assert channels == [(1, 10), (2, 20)]


def test_plan_targets_no_match_is_empty():
    dm = {1: {10}}
    channel_media = {(1, 1): {10}}
    dm_users, channels = plan_chapter_targets(99, dm, channel_media)
    assert dm_users == []
    assert channels == []


def test_plan_targets_dm_and_channel_are_independent():
    # A media subscribed by a feed but on no DM user's list still fans out to the
    # channel and to nobody's DMs (the two circuits are independent).
    dm = {1: {10}}
    channel_media = {(7, 70): {99}}
    dm_users, channels = plan_chapter_targets(99, dm, channel_media)
    assert dm_users == []
    assert channels == [(7, 70)]


# ---------------------------------------------------------------------------
# _sub_media - the synthesised media for a channel-subscribed manga
# ---------------------------------------------------------------------------


def test_sub_media_carries_id_and_searchable_title():
    media = _sub_media(4321, "Berserk")
    assert media["id"] == 4321
    # The cached title rides under romaji so the mapping search finds it exactly
    # like a list-derived manga.
    assert _search_title(media) == "Berserk"


def test_sub_media_none_title_is_unsearchable_not_a_crash():
    # A subscription that never captured a title yields a media the search simply
    # skips (left unmapped, retried later), never a raise.
    media = _sub_media(7, None)
    assert media["id"] == 7
    assert _search_title(media) is None


# ---------------------------------------------------------------------------
# READ_TEMPLATE disjointness - the click-dispatch safety guard
# ---------------------------------------------------------------------------

# discord.py dispatches a DynamicItem by fullmatch-ing the clicked custom_id
# against each registered template. If a chapter Read id fullmatched the like /
# reply / add / seen template (or vice versa), a click would run the wrong
# handler. These ids are exactly what each button builds.
_SAMPLES = {
    "like": ["alf:like:1", "alf:like:123456"],
    "reply": ["alf:reply:1", "alf:reply:987654"],
    "add": ["alf:add:1", "alf:add:42"],
    "seen": ["alf:seen:1:1", "alf:seen:123:45"],
    "read": ["alf:read:1:1", "alf:read:42:386", "alf:read:7:110.5"],
}
_TEMPLATES = {
    "like": re.compile(LIKE_TEMPLATE),
    "reply": re.compile(REPLY_TEMPLATE),
    "add": re.compile(ADD_TEMPLATE),
    "seen": re.compile(SEEN_TEMPLATE),
    "read": re.compile(READ_TEMPLATE),
}


def test_each_custom_id_matches_only_its_own_template():
    for kind, ids in _SAMPLES.items():
        for cid in ids:
            assert _TEMPLATES[kind].fullmatch(cid), (kind, cid)
            for other, pattern in _TEMPLATES.items():
                if other == kind:
                    continue
                assert pattern.fullmatch(cid) is None, (other, cid)


def test_read_template_captures_mid_and_chapter():
    m = _TEMPLATES["read"].fullmatch("alf:read:42:110.5")
    assert m.group("mid") == "42"
    assert m.group("chapter") == "110.5"
    m = _TEMPLATES["read"].fullmatch("alf:read:7:386")
    assert m.group("mid") == "7"
    assert m.group("chapter") == "386"


def test_read_template_rejects_non_numeric_chapter():
    # A numberless oneshot never gets a Read button; assert the template would not
    # accept one even if some caller tried to build it.
    assert _TEMPLATES["read"].fullmatch("alf:read:42:Extra") is None
    assert _TEMPLATES["read"].fullmatch("alf:read:42:") is None
    assert _TEMPLATES["read"].fullmatch("alf:read::5") is None


# ---------------------------------------------------------------------------
# Feed round-robin wheel over the mapped-manga set (the O(M) -> O(budget) fix)
#
# There is no batch chapter endpoint, so each mapped manga costs its own feed
# request. The poller polls only a FEED_BUDGET slice per tick via the pure
# tools.round_robin wheel, so the request count is constant. These tests pin the
# properties the poller relies on: every manga is polled within ceil(M/budget)
# ticks (no starvation), the request count is bounded, and the wheel stays correct
# as the mapped set mutates (mappings resolve / subscriptions drop).
# ---------------------------------------------------------------------------


def _drive_wheel(manga_seq, budget, ticks):
    """Run the feed wheel over a (possibly per-tick changing) mapped set.

    ``manga_seq`` is a callable ``tick -> iterable of mangadex ids`` giving the
    mapped set on each tick. Returns ``(polls_per_tick, poll_counts)`` - the batch
    served each tick and how many times each id was polled in total.
    """

    after = None
    polls_per_tick = []
    poll_counts = {}
    for t in range(ticks):
        mapped = sorted(manga_seq(t))
        batch, after = rr.next_batch(mapped, after, budget)
        polls_per_tick.append(batch)
        for mid in batch:
            poll_counts[mid] = poll_counts.get(mid, 0) + 1
    return polls_per_tick, poll_counts


def test_feed_wheel_polls_every_manga_within_one_cycle_no_starvation():
    manga = ["m{:03d}".format(i) for i in range(125)]  # 125 tracked manga
    budget = ch.FEED_BUDGET  # 25
    cycle = rr.poll_interval_ticks(len(manga), budget)
    assert cycle == 5  # ceil(125/25); at POLL_SECONDS=1800 -> ~2.5h per manga

    polls_per_tick, poll_counts = _drive_wheel(lambda _t: manga, budget, cycle)

    # Constant per-tick request budget, never exceeded.
    assert all(len(batch) == budget for batch in polls_per_tick)
    # Every manga polled exactly once within the cycle - full coverage, no
    # starvation, no duplicate work.
    assert set(poll_counts) == set(manga)
    assert all(count == 1 for count in poll_counts.values())


def test_feed_wheel_effective_interval_matches_formula():
    # The interval the poller logs is ceil(M / budget) ticks.
    assert rr.poll_interval_ticks(2000, ch.FEED_BUDGET) == 80  # 2000 manga / 25
    assert rr.poll_interval_ticks(25, ch.FEED_BUDGET) == 1  # fits in one tick
    assert rr.poll_interval_ticks(26, ch.FEED_BUDGET) == 2  # one over -> two ticks
    assert rr.poll_interval_ticks(0, ch.FEED_BUDGET) == 0  # nothing tracked


def test_feed_wheel_budget_ge_set_polls_all_every_tick():
    # When the mapped set fits inside the budget, every manga is polled every tick
    # (interval 1) - no degradation at small scale.
    manga = ["a", "b", "c"]
    _polls, counts = _drive_wheel(lambda _t: manga, ch.FEED_BUDGET, 4)
    assert counts == {"a": 4, "b": 4, "c": 4}


def test_feed_wheel_stable_when_manga_added_mid_cycle():
    # A newly-mapped manga entering the wheel mid-cycle is picked up within one
    # further cycle and never starves the incumbents.
    base = ["m{:02d}".format(i) for i in range(40)]  # 40 manga, budget 25

    def seq(t):
        # From tick 2 onward a brand-new manga id joins the mapped set.
        return base + (["NEW"] if t >= 2 else [])

    _polls, counts = _drive_wheel(seq, ch.FEED_BUDGET, 6)
    # The new manga was polled at least once (entered the wheel, not starved).
    assert counts.get("NEW", 0) >= 1
    # Every incumbent still got polled too.
    assert all(counts.get(mid, 0) >= 1 for mid in base)


def test_feed_wheel_stable_when_marker_manga_removed():
    # If the manga that was the resume marker is unsubscribed before the next tick,
    # the wheel resumes cleanly at the next id (bisect-by-value), no crash, no skip.
    manga = ["a", "b", "c", "d", "e"]
    batch1, after = rr.next_batch(manga, None, 2)
    assert batch1 == ["a", "b"]
    assert after == "b"
    # 'b' (the marker) is removed from the set before the next tick.
    batch2, after = rr.next_batch(["a", "c", "d", "e"], after, 2)
    assert batch2 == ["c", "d"]  # resumes strictly after the removed marker value


# ---------------------------------------------------------------------------
# Deferred-user / round-robin cursor safety (why chapters need no warmup hold)
#
# The chapter cursor + seen memory are PER MANGA (mangadex_chapter_state /
# mangadex_seen_chapters), driven by the pure tools.mangadex.plan_chapter_alerts.
# A manga NOT polled on a tick is simply never handed to the planner, so its cursor
# and seen set are untouched and it loses nothing - it catches up whenever the wheel
# reaches it. This is the exact property that makes deferring a never-cached user
# safe (their manga only enters the wheel later), UNLIKE airing's single global
# cursor. We encode it against the real planner.
# ---------------------------------------------------------------------------


def _chapter(num, readable_at, cid=None):
    return {
        "id": cid or "id-{}".format(num),
        "chapter": str(num),
        "volume": None,
        "readableAt": readable_at,
        "title": None,
        "externalUrl": None,
    }


def test_unpolled_manga_keeps_its_cursor_and_loses_no_chapter():
    """A manga skipped on a tick keeps its per-manga cursor; nothing is lost.

    Tick 1 anchors on the first run (alerts nothing, seeds the cursor). Between
    ticks the manga is NOT polled for several ticks while new chapters drop; because
    its cursor is never advanced by anyone else (no shared cursor), when it IS polled
    again the planner still alerts exactly the new chapters, in order, once each.
    """

    # First-ever poll: anti-backfill anchor on the two chapters present. No alerts,
    # cursor moves to the newest readableAt, both keys remembered.
    initial = [
        _chapter(1, "2023-01-01T00:00:00Z"),
        _chapter(2, "2023-01-02T00:00:00Z"),
    ]
    alerts, cursor, seen = md.plan_chapter_alerts(initial, None, set())
    assert alerts == []  # first run anchors silently
    anchored_cursor, anchored_seen = cursor, seen

    # Now imagine the wheel does NOT reach this manga for a while. Its DB cursor and
    # seen set simply stay put (we hold anchored_cursor / anchored_seen unchanged),
    # while chapters 3 and 4 are published upstream.
    later_feed = [
        _chapter(2, "2023-01-02T00:00:00Z"),
        _chapter(3, "2023-01-03T00:00:00Z"),
        _chapter(4, "2023-01-04T00:00:00Z"),
    ]

    # When the wheel finally reaches it again, it catches up from its OWN untouched
    # cursor - alerting exactly the chapters newer than the anchor, none skipped.
    alerts2, cursor2, _seen2 = md.plan_chapter_alerts(
        later_feed, anchored_cursor, anchored_seen
    )
    assert [c["chapter"] for c in alerts2] == ["3", "4"]  # zero loss across the gap
    assert cursor2 == "2023-01-04T00:00:00Z"


def test_deferred_user_manga_only_enters_the_wheel_later_others_unaffected():
    """A not-yet-loaded user's manga is simply absent from the mapped set for a few
    ticks, then joins the wheel; the manga already in the wheel are unaffected and
    keep their own cursors. This is the round-robin analogue of the deferred-user
    safety argument: no shared state couples the deferred manga to the rest.
    """

    incumbents = ["m{:02d}".format(i) for i in range(30)]

    def seq(t):
        # The deferred user's manga 'LATE' only appears from tick 3 (their list
        # finally loaded and it got mapped).
        return incumbents + (["LATE"] if t >= 3 else [])

    _polls, counts = _drive_wheel(seq, ch.FEED_BUDGET, 8)
    # Incumbents were polled throughout; the late manga still got polled once it
    # entered - its lateness cost only its own delay, nothing else's.
    assert counts.get("LATE", 0) >= 1
    assert all(counts.get(mid, 0) >= 1 for mid in incumbents)


# ---------------------------------------------------------------------------
# _fetch_feed backward pagination (the MAJOR fix). Under the widened round-robin
# interval a manga can drop MORE than one FEED_LIMIT page of chapters between polls.
# Fetching only the newest page would let the per-manga cursor jump to the newest
# chapter and SILENTLY skip the older overflow. _fetch_feed pages BACKWARD to the
# stored cursor so nothing outside the newest page is lost. These drive the REAL cog
# method with a stubbed per-page fetch (offset -> raw rows).
# ---------------------------------------------------------------------------

# Chapter readableAt values are monotonic epoch ints (chapter n is newer than n-1),
# which _to_epoch accepts directly and which sidesteps calendar-range limits.
_EPOCH_BASE = 1_700_000_000


def _raw_row(num):
    """A raw MangaDex feed row (pre-parse_chapter_feed) for chapter ``num``."""

    return {
        "id": "id-{}".format(num),
        "attributes": {
            "chapter": str(num),
            "volume": None,
            "readableAt": _EPOCH_BASE + num,
            "title": None,
            "externalUrl": None,
        },
    }


def _desc_rows(highest):
    """Raw rows for chapters ``highest..1``, newest-first (as MangaDex returns)."""

    return [_raw_row(n) for n in range(highest, 0, -1)]


def _paged(rows, limit):
    """Slice ``rows`` into a ``{offset: page}`` map of ``limit``-sized pages."""

    pages = {}
    for p in range((len(rows) + limit - 1) // limit or 1):
        pages[p * limit] = rows[p * limit:(p + 1) * limit]
    return pages


def _chapters_cog_with_pages(pages, asked=None):
    """A bare AniListChapters whose _fetch_feed_page serves ``pages`` by offset.

    Records the offsets requested so a test can assert where paging stopped (and, in
    ``asked``, the language union each page was asked for). No task loop, bot or DB
    is constructed; _fetch_feed only needs _space + _fetch_feed_page.
    """

    cog = object.__new__(ch.AniListChapters)
    cog._req_count = 0
    requested = []

    async def _no_space():
        cog._req_count += 1

    cog._space = _no_space

    async def _page(_mangadex_id, offset, languages):
        requested.append(offset)
        if asked is not None:
            asked.append(languages)
        return {"data": pages.get(offset, [])}

    cog._fetch_feed_page = _page
    return cog, requested


async def test_fetch_feed_pages_back_past_one_page_and_loses_no_chapter():
    # 12 new chapters, all newer than the cursor, FEED_LIMIT=5 per page. A single
    # newest page would drop chapters 1..7 when the cursor jumps; paging back to the
    # cursor fetches all 12.
    limit = md.FEED_LIMIT
    cog, requested = _chapters_cog_with_pages(_paged(_desc_rows(12), limit))

    cursor = _EPOCH_BASE  # older than every chapter
    got = await cog._fetch_feed("uuid", cursor)
    assert sorted(int(c["chapter"]) for c in got) == list(range(1, 13))
    # Pages at offset 0, 5, 10 requested; offset 10 is a short page (2 rows) -> stop.
    assert requested == [0, 5, 10]


async def test_fetch_feed_stops_once_it_reaches_processed_ground():
    # The cursor sits at chapter 6, so paging stops as soon as a page's oldest row is
    # at/below it - it must not keep fetching older pages needlessly.
    limit = md.FEED_LIMIT
    rows = _desc_rows(12)
    cog, requested = _chapters_cog_with_pages(_paged(rows, limit))

    cursor = _EPOCH_BASE + 6  # chapter 6's readableAt
    got = await cog._fetch_feed("uuid", cursor)
    # Page 0 = ch 12..8 (oldest 8 > cursor -> continue); page 1 = ch 7..3 (oldest 3
    # <= cursor -> stop). The third page is never requested.
    assert requested == [0, 5]
    assert {int(c["chapter"]) for c in got} == set(range(3, 13))
    # Handed to the planner (got is already normalised by _fetch_feed), exactly the
    # chapters strictly newer than the cursor alert - none skipped, none duplicated.
    alerts, _c, _s = md.plan_chapter_alerts(got, cursor, set())
    assert [c["chapter"] for c in alerts] == ["7", "8", "9", "10", "11", "12"]


async def test_fetch_feed_first_run_fetches_a_single_page_only():
    # cursor is None (first ever poll): one page anchors the cursor with no backfill,
    # even though the page is full - never walk the whole back-catalogue.
    limit = md.FEED_LIMIT
    cog, requested = _chapters_cog_with_pages(_paged(_desc_rows(12), limit))

    got = await cog._fetch_feed("uuid", None)
    assert requested == [0]  # single anchor page
    assert len(got) == limit


async def test_fetch_feed_page_cap_is_logged_not_silent(caplog):
    # A burst larger than MAX_FEED_PAGES * FEED_LIMIT above the cursor hits the cap:
    # paging stops at MAX_FEED_PAGES and LOGS a warning (the overflow is not silent),
    # while delivery stays bounded by MAX_ALERTS_PER_MANGA downstream.
    limit = md.FEED_LIMIT
    total = ch.MAX_FEED_PAGES * limit + limit  # one full page beyond the cap
    cog, requested = _chapters_cog_with_pages(_paged(_desc_rows(total), limit))

    cursor = _EPOCH_BASE  # below everything, so every fetched page is full and above
    with caplog.at_level("WARNING"):
        got = await cog._fetch_feed("uuid", cursor)
    assert len(requested) == ch.MAX_FEED_PAGES  # stopped exactly at the cap
    assert len(got) == ch.MAX_FEED_PAGES * limit
    assert "feed page cap" in caplog.text


# ---------------------------------------------------------------------------
# Stale-'missing' mapping retry (C0c)
#
# A 'missing' row is a cached negative, not a life sentence: once its checked_at
# clock is older than MISSING_RETRY_DAYS the media is searched again, so a niche
# title added to MangaDex later becomes visible. The invariant these tests pin is
# the BUDGET one - retries are strictly second-class and can never push the tick
# past MAX_MAPPING_SEARCHES_PER_TICK.
# ---------------------------------------------------------------------------


def _title_media(mid):
    return {"id": mid, "title": {"romaji": "Title {}".format(mid)}}


def _mapping_env(specs):
    """Build ``(union_media, media_by_id, mapping_rows)`` from ``{mid: spec}``.

    A spec of ``None`` means "no mapping row at all" (never searched); otherwise it
    is the row dict as :meth:`_load_mappings` returns it.
    """

    union = set(specs)
    media_by_id = {mid: _title_media(mid) for mid in specs}
    rows = {mid: spec for mid, spec in specs.items() if spec is not None}
    return union, media_by_id, rows


_CHECK_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _checked(offset):
    """A ``checked_at`` stamp ``offset`` seconds after a fixed base (bigger = newer)."""

    return _CHECK_BASE + timedelta(seconds=offset)


def _missing_row(retry_due, checked_at=None):
    return {
        "mangadex_id": None,
        "status": "missing",
        "retry_due": retry_due,
        "checked_at": checked_at,
    }


def test_fresh_missing_row_is_not_retried():
    union, media, rows = _mapping_env({10: _missing_row(False)})
    assert ch._mapping_search_candidates(union, media, rows) == []


def test_stale_missing_row_is_retried_when_budget_is_free():
    union, media, rows = _mapping_env({10: _missing_row(True)})
    assert ch._mapping_search_candidates(union, media, rows) == [10]


def test_found_row_is_never_re_searched_even_if_stale():
    # 'found' rows are terminal here: only 'missing' carries a retry.
    union, media, rows = _mapping_env(
        {10: {"mangadex_id": "uuid-10", "status": "found", "retry_due": True}}
    )
    assert ch._mapping_search_candidates(union, media, rows) == []


def test_never_searched_media_win_the_whole_budget_over_retries():
    # 3 never-searched media + 2 stale retries, budget 3 -> only the new ones run.
    specs = {1: None, 2: None, 3: None, 50: _missing_row(True), 51: _missing_row(True)}
    union, media, rows = _mapping_env(specs)
    picked = ch._mapping_search_candidates(union, media, rows)
    assert picked == [1, 2, 3]
    assert len(picked) == ch.MAX_MAPPING_SEARCHES_PER_TICK


def test_retries_only_take_the_budget_new_media_left():
    # 1 never-searched media + 3 stale retries, budget 3 -> new first, then the 2
    # least-recently-checked retries (50 was checked first, then 51, then 52).
    specs = {
        7: None,
        50: _missing_row(True, _checked(0)),
        51: _missing_row(True, _checked(10)),
        52: _missing_row(True, _checked(20)),
    }
    union, media, rows = _mapping_env(specs)
    picked = ch._mapping_search_candidates(union, media, rows)
    assert picked == [7, 50, 51]
    assert len(picked) == ch.MAX_MAPPING_SEARCHES_PER_TICK


def test_candidates_never_exceed_the_cap_whatever_the_backlog():
    specs = {mid: None for mid in range(1, 6)}
    specs.update({mid: _missing_row(True) for mid in range(100, 140)})
    union, media, rows = _mapping_env(specs)
    picked = ch._mapping_search_candidates(union, media, rows)
    assert len(picked) == ch.MAX_MAPPING_SEARCHES_PER_TICK
    assert all(mid < 100 for mid in picked)  # backlog waits behind the new media


def test_untitled_media_are_skipped_in_both_classes():
    union = {1, 2}
    media = {1: {"id": 1, "title": {}}, 2: {"id": 2, "title": {}}}
    rows = {2: _missing_row(True)}
    assert ch._mapping_search_candidates(union, media, rows) == []


# --- retry fairness: the backlog drains FIFO, not by media id ----------------
#
# The retry budget is what MAX_MAPPING_SEARCHES_PER_TICK leaves over, i.e. only a
# few per tick against a backlog that can be far larger than one MISSING_RETRY_DAYS
# window can serve. Draining by media id would then permanently feed the low-id
# cohort (which goes stale again and re-takes the head) and never reach the high
# ids - exactly the NEWEST media, the ones most likely to have just landed on
# MangaDex. So the order is the checked_at clock: least-recently-checked first.


def test_retries_drain_least_recently_checked_first():
    # Clocks run OPPOSITE to the ids: the oldest check is the highest id, which an
    # id-ordered drain would serve last (and, at scale, never).
    specs = {
        50: _missing_row(True, _checked(300)),
        51: _missing_row(True, _checked(200)),
        52: _missing_row(True, _checked(100)),
    }
    union, media, rows = _mapping_env(specs)
    assert ch._mapping_search_candidates(union, media, rows) == [52, 51, 50]


def test_a_restamped_retry_falls_behind_the_older_backlog():
    # The rotation invariant: a row that was just served (checked_at moved to now)
    # goes to the BACK, so the next tick serves the row that has waited longest.
    specs = {
        50: _missing_row(True, _checked(100)),
        51: _missing_row(True, _checked(200)),
    }
    union, media, rows = _mapping_env(specs)
    assert ch._mapping_search_candidates(union, media, rows, budget=1) == [50]

    rows[50]["checked_at"] = _checked(900)  # re-stamped by the completed search
    assert ch._mapping_search_candidates(union, media, rows, budget=1) == [51]


def test_retry_tiebreak_on_equal_clocks_is_the_media_id():
    # Rows stamped in the same transaction share a clock; the id keeps the drain
    # deterministic (and therefore testable) instead of hash-ordered.
    same = _checked(500)
    specs = {
        52: _missing_row(True, same),
        50: _missing_row(True, same),
        51: _missing_row(True, same),
    }
    union, media, rows = _mapping_env(specs)
    assert ch._mapping_search_candidates(union, media, rows) == [50, 51, 52]


def test_a_row_with_no_readable_clock_is_treated_as_maximally_stale():
    # A missing/unreadable checked_at must never raise (no datetime-vs-None compare)
    # and must not starve either: it ranks ahead of every dated row.
    specs = {
        50: _missing_row(True, _checked(100)),
        51: _missing_row(True, None),
        52: _missing_row(True, "not-a-date"),
    }
    union, media, rows = _mapping_env(specs)
    assert ch._mapping_search_candidates(union, media, rows) == [51, 52, 50]


def test_the_whole_backlog_is_served_before_anything_repeats():
    # End-to-end fairness over two ticks with the real cap: 6 stale rows, clocks
    # inverted against the ids. Each served row is re-stamped (as the resolver's
    # upsert does) and immediately eligible again, so an id-ordered drain would
    # re-serve the low ids on tick 2 and never touch the high ones. FIFO instead
    # serves all 6 exactly once.
    ids = [100, 101, 102, 103, 104, 105]
    specs = {
        mid: _missing_row(True, _checked(1000 - 10 * i)) for i, mid in enumerate(ids)
    }
    union, media, rows = _mapping_env(specs)

    served = []
    stamp = 5000
    for _tick in range(2):
        picked = ch._mapping_search_candidates(union, media, rows)
        assert len(picked) == ch.MAX_MAPPING_SEARCHES_PER_TICK
        for mid in picked:
            served.append(mid)
            stamp += 10
            rows[mid]["checked_at"] = _checked(stamp)  # re-stamped, still stale

    assert sorted(served) == ids  # every row served, none twice
    assert served[:3] == [105, 104, 103]  # oldest checks first, ids notwithstanding


def test_checked_epoch_normalises_what_the_driver_returns():
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ch._checked_epoch(aware) == aware.timestamp()
    # A naive stamp is read as UTC (never as local time).
    assert ch._checked_epoch(datetime(2026, 1, 1)) == aware.timestamp()
    assert ch._checked_epoch(1735689600) == 1735689600.0
    assert ch._checked_epoch(None) is None
    assert ch._checked_epoch("nope") is None


# --- the async resolver: what a retry actually writes ------------------------


def _resolver_cog(payloads):
    """A bare AniListChapters whose _search_manga serves ``payloads`` by media id.

    A payload may be an exception instance, which is raised instead. Records every
    _upsert_mapping call so a test can assert what was persisted.
    """

    cog = object.__new__(ch.AniListChapters)
    cog._embargo_until = 0
    searched = []
    upserts = []

    async def _no_space():
        return None

    cog._space = _no_space

    async def _search(title):
        mid = int(title.split()[-1])
        searched.append(mid)
        payload = payloads[mid]
        if isinstance(payload, Exception):
            raise payload
        return payload

    cog._search_manga = _search

    async def _upsert(mid, mangadex_id, status):
        upserts.append((mid, mangadex_id, status))

    cog._upsert_mapping = _upsert
    return cog, searched, upserts


def _found_payload(mid, uuid):
    return {"data": [{"id": uuid, "attributes": {"links": {"al": str(mid)}}}]}


async def test_retry_that_still_misses_restamps_checked_at():
    # The upsert is the re-stamp (checked_at = now() in _upsert_mapping's SQL), so
    # persisting 'missing' again is what buys another MISSING_RETRY_DAYS of quiet.
    union, media, rows = _mapping_env({50: _missing_row(True)})
    cog, searched, upserts = _resolver_cog({50: {"data": []}})

    assert await cog._resolve_new_mappings(union, media, rows, 0) is True
    assert searched == [50]
    assert upserts == [(50, None, "missing")]
    # The mirrored clock is dropped to None on purpose: only Postgres' now() is
    # authoritative for it, and retry_due=False already keeps the row out of the
    # retry ordering until the real stamp is re-read next tick.
    assert rows[50] == {
        "mangadex_id": None,
        "status": "missing",
        "retry_due": False,
        "checked_at": None,
    }


async def test_successful_retry_persists_the_mapping():
    union, media, rows = _mapping_env({50: _missing_row(True)})
    cog, searched, upserts = _resolver_cog({50: _found_payload(50, "uuid-50")})

    assert await cog._resolve_new_mappings(union, media, rows, 0) is True
    assert searched == [50]
    assert upserts == [(50, "uuid-50", "found")]
    assert rows[50] == {
        "mangadex_id": "uuid-50",
        "status": "found",
        "retry_due": False,
        "checked_at": None,
    }


async def test_fresh_missing_row_costs_no_search_at_all():
    union, media, rows = _mapping_env({50: _missing_row(False)})
    cog, searched, upserts = _resolver_cog({})

    assert await cog._resolve_new_mappings(union, media, rows, 0) is True
    assert searched == []
    assert upserts == []


async def test_transient_retry_failure_writes_nothing():
    # A fetch error must NOT re-stamp: the row keeps its stale clock and is picked
    # up again on a later tick (only a completed search buys the TTL).
    union, media, rows = _mapping_env({50: _missing_row(True)})
    cog, searched, upserts = _resolver_cog({50: ch._FetchError("boom")})

    assert await cog._resolve_new_mappings(union, media, rows, 0) is True
    assert searched == [50]
    assert upserts == []
    assert rows[50]["retry_due"] is True


async def test_retry_search_burst_stays_within_the_tick_cap():
    # 2 new + 3 stale retries: exactly MAX_MAPPING_SEARCHES_PER_TICK searches run,
    # new media first - the retry never widens the budget.
    specs = {
        1: None,
        2: None,
        50: _missing_row(True),
        51: _missing_row(True),
        52: _missing_row(True),
    }
    union, media, rows = _mapping_env(specs)
    cog, searched, upserts = _resolver_cog({mid: {"data": []} for mid in specs})

    assert await cog._resolve_new_mappings(union, media, rows, 0) is True
    assert searched == [1, 2, 50]
    assert len(searched) == ch.MAX_MAPPING_SEARCHES_PER_TICK
    assert len(upserts) == ch.MAX_MAPPING_SEARCHES_PER_TICK


async def test_rate_limited_retry_aborts_the_tick_and_sets_the_embargo():
    union, media, rows = _mapping_env({50: _missing_row(True)})
    cog, searched, _upserts = _resolver_cog({50: ch._RateLimited(90)})

    assert await cog._resolve_new_mappings(union, media, rows, 1000) is False
    assert searched == [50]
    assert cog._embargo_until == 1090


# --- _load_mappings: the staleness verdict is computed by Postgres -----------


class _RecordingPool:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self._rows


async def test_load_mappings_asks_postgres_for_the_staleness_verdict():
    stamp = _checked(0)
    pool = _RecordingPool(
        [
            {
                "anilist_media_id": 10,
                "mangadex_id": None,
                "status": "missing",
                "retry_due": True,
                "checked_at": stamp,
            },
            {
                "anilist_media_id": 11,
                "mangadex_id": "uuid-11",
                "status": "found",
                "retry_due": False,
                "checked_at": stamp,
            },
        ]
    )
    cog = object.__new__(ch.AniListChapters)
    cog.bot = type("_Bot", (), {"db_pool": pool})()

    got = await cog._load_mappings([10, 11])
    query, args = pool.calls[0]
    # The clock comparison rides Postgres' now() - the same clock _upsert_mapping
    # stamps with - and the TTL is passed as the named constant, never inlined.
    assert "checked_at < now() -" in query
    assert args[1] == str(ch.MISSING_RETRY_DAYS)
    assert got[10]["retry_due"] is True
    assert got[11]["retry_due"] is False
    # The raw clock rides along too: the boolean verdict alone cannot order a
    # backlog the per-tick cap can only partly serve (see the FIFO drain).
    assert "checked_at," in query  # selected, not only compared
    assert got[10]["checked_at"] == stamp


# ---------------------------------------------------------------------------
# Chapter-alert languages (C0d)
#
# The v1 semantics: language filters the per-manga feed REQUEST (the union of what
# that manga's trackers read, English always in), never the alert identity. So the
# poll count is unchanged, at-most-once is unchanged, nobody can lose an alert to a
# language race - and only the LINK is picked per recipient.
# ---------------------------------------------------------------------------


def test_language_union_covers_dm_and_channel_trackers():
    dm_targets = [(1, "fr"), (2, "fr"), (3, "en")]
    channel_targets = [(100, "es")]
    assert ch._feed_language_union(dm_targets, channel_targets) == ["en", "fr", "es"]


def test_language_union_is_english_only_when_nobody_asked_otherwise():
    # The overwhelmingly common case: identical to what the tracker requested before
    # this lot, so an English-reading audience sees no behaviour change at all.
    assert ch._feed_language_union([(1, "en"), (2, "en")], []) == ["en"]
    assert ch._feed_language_union([], []) == ["en"]


def test_language_union_drops_a_language_we_do_not_serve():
    assert ch._feed_language_union([(1, "klingon")], []) == ["en"]


def _language_cog():
    """A bare AniListChapters, enough to exercise the language plumbing."""

    return object.__new__(ch.AniListChapters)


def test_feed_languages_clamps_and_logs_the_drop(caplog):
    cog = _language_cog()
    dm_targets = [
        (1, "fr"),
        (2, "fr"),
        (3, "es"),
        (4, "es"),
        (5, "de"),
        (6, "it"),
        (7, "ru"),
    ]
    with caplog.at_level("INFO"):
        got = cog._feed_languages("uuid", dm_targets, [])
    # English plus the three most-requested; the singletons are what gets dropped,
    # and the drop is logged (those readers fall back to another language's release).
    assert got == ["en", "es", "fr", "de"]
    assert len(got) == md.MAX_FEED_LANGUAGES
    assert "languages" in caplog.text
    assert "it" in caplog.text and "ru" in caplog.text


def test_feed_languages_does_not_log_when_nothing_is_dropped(caplog):
    cog = _language_cog()
    with caplog.at_level("INFO"):
        got = cog._feed_languages("uuid", [(1, "fr")], [])
    assert got == ["en", "fr"]
    assert caplog.text == ""


async def test_fetch_feed_asks_every_page_for_the_same_union():
    # ONE request per page, carrying the whole union: widening the union never adds
    # a request, it only adds params and rows.
    asked = []
    limit = md.feed_page_limit(["en", "fr"])
    cog, requested = _chapters_cog_with_pages(
        _paged(_desc_rows(limit + 2), limit), asked
    )

    await cog._fetch_feed("uuid", _EPOCH_BASE, ["en", "fr"])
    assert requested == [0, limit]  # stride follows the widened page, not FEED_LIMIT
    assert asked == [["en", "fr"], ["en", "fr"]]


async def test_fetch_feed_stride_widens_with_the_union():
    # A two-language feed carries ~2 rows per chapter, so the page doubles and the
    # backward pagination keeps covering the same number of distinct chapters.
    limit = md.feed_page_limit(["en", "fr"])
    assert limit == md.FEED_LIMIT * 2
    rows = md.FEED_LIMIT * 2 - 1  # a short page for the widened stride, two for 5
    asked = []
    cog, requested = _chapters_cog_with_pages(_paged(_desc_rows(rows), limit), asked)

    got = await cog._fetch_feed("uuid", _EPOCH_BASE, ["en", "fr"])
    # One request held everything an English-only page would have needed two for.
    assert requested == [0]
    assert len(got) == rows


async def test_fetch_feed_without_languages_is_unchanged():
    # The default path (no tracker preference) is byte-for-byte today's behaviour:
    # FEED_LIMIT-sized pages, English only.
    asked = []
    cog, requested = _chapters_cog_with_pages(
        _paged(_desc_rows(12), md.FEED_LIMIT), asked
    )

    await cog._fetch_feed("uuid", _EPOCH_BASE)
    assert requested == [0, 5, 10]
    assert asked == [None, None, None]


class _FakePool:
    """Minimal asyncpg-pool stand-in: records the query and returns fixed rows."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self._rows


async def test_load_dm_languages_normalizes_and_drops_junk():
    cog = _language_cog()
    cog.bot = type("B", (), {})()
    cog.bot.db_pool = _FakePool(
        [
            {"user_id": 1, "language": "fr"},
            {"user_id": 2, "language": "PT_BR"},
            {"user_id": 3, "language": "klingon"},
            {"user_id": 4, "language": None},
        ]
    )

    got = await cog._load_dm_languages([1, 2, 3, 4])
    # Only the usable picks are kept; everyone else is absent and reads as English.
    assert got == {1: "fr", 2: "pt-br"}
    # ONE query for the whole opt-in set, whatever its size (scale: no per-user read).
    assert len(cog.bot.db_pool.calls) == 1
    assert ch.MANGADEX_LANGUAGE_KEY in cog.bot.db_pool.calls[0][1]


async def test_load_dm_languages_skips_the_query_when_nobody_is_tracked():
    cog = _language_cog()
    cog.bot = type("B", (), {})()
    cog.bot.db_pool = _FakePool([])
    assert await cog._load_dm_languages([]) == {}
    assert cog.bot.db_pool.calls == []


def _lang_chapter(cid, number, language):
    return {
        "id": cid,
        "chapter": str(number),
        "volume": None,
        "readableAt": "2023-01-01T00:00:00Z",
        "translatedLanguage": language,
        "url": "https://mangadex.org/chapter/" + cid,
    }


async def test_fan_out_points_each_recipient_at_their_own_language():
    cog = _language_cog()
    dms = []
    posts = []

    async def _dm(user_id, _media, chapter):
        dms.append((user_id, chapter["id"]))

    async def _post(channel_id, _media, chapter):
        posts.append((channel_id, chapter["id"]))

    cog._deliver_dm = _dm
    cog._deliver_channel = _post

    en = _lang_chapter("en-386", 386, "en")
    fr = _lang_chapter("fr-386", 386, "fr")
    variants = md.index_variants([en, fr])

    # The alert fired on the English row (it landed first); the French reader and a
    # French-speaking server still get the French page, the German reader - whose
    # translation is not out yet - gets the row the alert fired on rather than
    # nothing. NOBODY is skipped: that is the invariant that keeps at-most-once safe.
    await cog._deliver_alert(
        {}, en, variants, [(1, "fr"), (2, "en"), (3, "de")], [(900, "fr"), (901, "en")]
    )
    assert dms == [(1, "fr-386"), (2, "en-386"), (3, "en-386")]
    assert posts == [(900, "fr-386"), (901, "en-386")]


async def test_guild_language_maps_the_guild_locale_and_memoizes(monkeypatch):
    # A channel post has no single reader, so it follows the guild locale - mapped
    # to a MangaDex code, English whenever that locale is not one MangaDex serves.
    cog = _language_cog()
    cog.bot = type("B", (), {"get_guild": staticmethod(lambda gid: gid)})()
    resolved = []

    async def _locale(_bot, guild):
        resolved.append(guild)
        return {1: "fr", 2: "el", 3: "eo"}[guild]

    monkeypatch.setattr(ch.i18n, "resolve_guild_locale", _locale)

    memo = {}
    assert await cog._guild_language(1, memo) == "fr"
    assert await cog._guild_language(2, memo) == "el"
    assert await cog._guild_language(3, memo) == "en"  # unserved locale -> fallback
    # A guild subscribed to several manga is resolved ONCE per tick, not per manga.
    assert await cog._guild_language(1, memo) == "fr"
    assert resolved == [1, 2, 3]


def test_the_language_preference_key_matches_the_panel():
    # The key is coupled by literal across modules (the house convention), so pin it
    # here: a rename on either side must break this, not the feature.
    from cogs.community import usersettings

    match = [
        pref
        for pref in usersettings.CHOICE_PREFS
        if pref.key == ch.MANGADEX_LANGUAGE_KEY
    ]
    assert len(match) == 1
    assert match[0].default == md.DEFAULT_LANGUAGE
    assert match[0].options == md.LANGUAGES
