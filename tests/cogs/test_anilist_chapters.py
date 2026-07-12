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
from datetime import datetime, timezone

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


def _chapters_cog_with_pages(pages):
    """A bare AniListChapters whose _fetch_feed_page serves ``pages`` by offset.

    Records the offsets requested so a test can assert where paging stopped. No task
    loop, bot or DB is constructed; _fetch_feed only needs _space + _fetch_feed_page.
    """

    cog = object.__new__(ch.AniListChapters)
    cog._req_count = 0
    requested = []

    async def _no_space():
        cog._req_count += 1

    cog._space = _no_space

    async def _page(_mangadex_id, offset):
        requested.append(offset)
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
