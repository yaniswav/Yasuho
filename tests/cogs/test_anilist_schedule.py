"""Unit tests for ``/anilist schedule`` (cogs/anilist/schedule.py).

Two layers, no network / DB / Discord gateway anywhere:

* the pure helpers the design rests on - the forward-only window arithmetic (the
  browse must be structurally unable to look into the past or past the horizon),
  the media-id bounding, the UTC-day grouping and its timezone-proof day label,
  and the local pagination;
* the REQUEST BUDGET itself, exercised against a fake cog that counts GraphQL
  calls: a channel browse is one call, a personal browse is two once the AniList
  id is known, three on the cold path, paging is zero and a window step is one.
  That budget is the whole promise the module docstring makes to the pollers,
  so it is asserted rather than described.
"""

import datetime
import time

import pytest

from cogs.anilist import schedule as sc
from cogs.anilist.schedule import (
    API_PER_PAGE,
    DAY,
    MAX_HORIZON_DAYS,
    MAX_MEDIA_IDS,
    MAX_WINDOW_OFFSET,
    PAGE_SIZE,
    WINDOW_DAYS,
    AiringScheduleView,
    ScheduleMixin,
    bound_media_ids,
    clamp_offset,
    clamp_page,
    day_bucket,
    day_label_ts,
    group_by_day,
    page_count,
    page_slice,
    sort_rows,
    window_bounds,
)

NOW = 1_700_000_000


# ---------------------------------------------------------------------------
# documented constants (the tuned bounds the scale story leans on)
# ---------------------------------------------------------------------------


def test_documented_constants():
    assert WINDOW_DAYS == 7
    assert MAX_HORIZON_DAYS == 14
    assert MAX_WINDOW_OFFSET == 7
    assert PAGE_SIZE == 10
    assert API_PER_PAGE == 50
    assert MAX_MEDIA_IDS == 500
    assert DAY == 86400
    # The whole navigable space is exactly two windows.
    assert MAX_WINDOW_OFFSET + WINDOW_DAYS == MAX_HORIZON_DAYS


# ---------------------------------------------------------------------------
# window arithmetic - forward only, bounded horizon
# ---------------------------------------------------------------------------


def test_clamp_offset_bounds_and_garbage():
    assert clamp_offset(0) == 0
    assert clamp_offset(WINDOW_DAYS) == WINDOW_DAYS
    assert clamp_offset(-1) == 0
    assert clamp_offset(-999) == 0
    assert clamp_offset(999) == MAX_WINDOW_OFFSET
    assert clamp_offset(None) == 0
    assert clamp_offset("nope") == 0


def test_window_is_never_in_the_past():
    # The CRITICAL invariant: this surface is forward-looking, the poller owns
    # the past. No offset - not even a negative one - can produce a lower bound
    # before now.
    for offset in (-999, -7, -1, 0, 1, 7, 999):
        greater, lesser = window_bounds(NOW, offset)
        assert greater >= NOW
        assert lesser > greater


def test_window_span_and_horizon():
    greater, lesser = window_bounds(NOW, 0)
    assert greater == NOW
    assert lesser - greater == WINDOW_DAYS * DAY

    # The second (and last) window starts a week out and ends at the horizon.
    greater, lesser = window_bounds(NOW, MAX_WINDOW_OFFSET)
    assert greater == NOW + MAX_WINDOW_OFFSET * DAY
    assert lesser == NOW + MAX_HORIZON_DAYS * DAY

    # Nothing can reach beyond the horizon, however far a caller asks.
    assert window_bounds(NOW, 10_000)[1] == NOW + MAX_HORIZON_DAYS * DAY


def test_window_bounds_accept_a_float_clock():
    greater, lesser = window_bounds(NOW + 0.75, 0)
    assert (greater, lesser) == (NOW, NOW + WINDOW_DAYS * DAY)


# ---------------------------------------------------------------------------
# media-id bounding
# ---------------------------------------------------------------------------


def test_bound_media_ids_dedups_sorts_and_reports_no_truncation():
    ids, truncated = bound_media_ids([30, 10, 20, 10, None, 30])
    assert ids == [10, 20, 30]
    assert truncated is False


def test_bound_media_ids_caps_and_flags_truncation():
    ids, truncated = bound_media_ids(range(MAX_MEDIA_IDS + 25))
    assert len(ids) == MAX_MEDIA_IDS
    assert truncated is True
    # Deterministic: the cap keeps the LOWEST ids, so the same list always
    # produces the same outgoing request.
    assert ids == list(range(MAX_MEDIA_IDS))


def test_bound_media_ids_empty_cases():
    assert bound_media_ids([]) == ([], False)
    assert bound_media_ids(None) == ([], False)
    assert bound_media_ids([None, None]) == ([], False)


# ---------------------------------------------------------------------------
# day grouping + the timezone-proof day label
# ---------------------------------------------------------------------------


def test_day_bucket_groups_by_utc_day():
    midnight = 1_700_000_000 // DAY * DAY
    assert day_bucket(midnight) == day_bucket(midnight + DAY - 1)
    assert day_bucket(midnight + DAY) == day_bucket(midnight) + 1


def test_day_label_renders_the_right_date_in_every_populated_timezone():
    # A <t:...:D> is rendered in the VIEWER's timezone, so the label instant is
    # chosen to keep the rendered civil date equal to the UTC bucket's date.
    # Inhabited offsets span 26 hours, wider than a day, so the guarantee is
    # stated for the densely populated band UTC-10 (Hawaii) .. UTC+13 (NZ DST).
    for seed in (1_700_000_000, 1_600_000_000, 0, 2_000_000_000):
        bucket = day_bucket(seed)
        utc_date = datetime.datetime.fromtimestamp(
            bucket * DAY, datetime.timezone.utc
        ).date()
        label = day_label_ts(bucket)
        for hours in range(-sc.DAY_LABEL_HOUR, 24 - sc.DAY_LABEL_HOUR):
            tz = datetime.timezone(datetime.timedelta(hours=hours))
            assert datetime.datetime.fromtimestamp(label, tz).date() == utc_date
        assert -sc.DAY_LABEL_HOUR <= -10 and 24 - sc.DAY_LABEL_HOUR > 13


def test_sort_rows_drops_undated_rows_and_totalises_the_order():
    rows = [
        {"airingAt": 200, "mediaId": 2, "episode": 1},
        {"airingAt": None, "mediaId": 9, "episode": 9},
        {"airingAt": 100, "mediaId": 5, "episode": 3},
        {"airingAt": 100, "mediaId": 1, "episode": 4},
        {"mediaId": 8},
    ]
    out = sort_rows(rows)
    assert [(r["airingAt"], r["mediaId"]) for r in out] == [
        (100, 1),
        (100, 5),
        (200, 2),
    ]


def test_sort_rows_empty_cases():
    assert sort_rows([]) == []
    assert sort_rows(None) == []


def test_group_by_day_makes_consecutive_runs_in_airing_order():
    midnight = 1_700_000_000 // DAY * DAY
    rows = sort_rows(
        [
            {"airingAt": midnight + 3600, "mediaId": 1},
            {"airingAt": midnight + 7200, "mediaId": 2},
            {"airingAt": midnight + DAY + 60, "mediaId": 3},
            {"airingAt": midnight + 2 * DAY, "mediaId": 4},
        ]
    )
    groups = group_by_day(rows)
    assert [len(g[1]) for g in groups] == [2, 1, 1]
    assert [g[0] for g in groups] == sorted(g[0] for g in groups)


def test_group_by_day_empty():
    assert group_by_day([]) == []
    assert group_by_day(None) == []


# ---------------------------------------------------------------------------
# local pagination
# ---------------------------------------------------------------------------


def test_page_count_and_clamp():
    assert page_count(0) == 1
    assert page_count(1) == 1
    assert page_count(PAGE_SIZE) == 1
    assert page_count(PAGE_SIZE + 1) == 2
    assert clamp_page(-5, 25) == 0
    assert clamp_page(99, 25) == page_count(25) - 1
    assert clamp_page("x", 25) == 0


def test_page_slice_never_raises_on_a_stale_index():
    rows = list(range(25))
    assert page_slice(rows, 0) == rows[:PAGE_SIZE]
    assert page_slice(rows, 2) == rows[2 * PAGE_SIZE :]
    # A page index left over from a bigger window is clamped, not an IndexError.
    assert page_slice(rows, 99) == rows[2 * PAGE_SIZE :]
    assert page_slice([], 3) == []


# ---------------------------------------------------------------------------
# title link
# ---------------------------------------------------------------------------


def test_schedule_link_strips_brackets_and_masks_the_url():
    media = {"title": {"romaji": "Re:Zero [Director's Cut]"}, "siteUrl": "https://a/1"}
    assert sc._schedule_link(media) == "[Re:Zero Director's Cut](https://a/1)"


def test_schedule_link_without_a_url_is_bare_text():
    assert sc._schedule_link({"title": {"romaji": "Bocchi"}}) == "Bocchi"


def test_schedule_link_truncates_a_very_long_title():
    media = {"title": {"romaji": "A" * 200}, "siteUrl": "https://a/1"}
    link = sc._schedule_link(media)
    label = link[1 : link.index("]")]
    assert len(label) <= sc.TITLE_LIMIT
    assert label.endswith("...")


# ---------------------------------------------------------------------------
# fakes for the budget tests
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []
        self.fetchrow_calls = 0
        self.fetch_calls = 0

    async def fetchrow(self, _query, *_args):
        self.fetchrow_calls += 1
        return self.row

    async def fetch(self, _query, *_args):
        self.fetch_calls += 1
        return self.rows


class _FakeBot:
    def __init__(self, pool):
        self.db_pool = pool


class _FakeCog(ScheduleMixin):
    """A ScheduleMixin with counted GraphQL + a stubbed token status."""

    def __init__(self, *, pool=None, responses=None, token_status=("ok", "tok")):
        self.bot = _FakeBot(pool or _FakePool())
        self.calls = []
        self._responses = list(responses or [])
        self._status = token_status

    async def _graphql(self, query, variables, token=None):
        self.calls.append((query, variables, token))
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def _token_status(self, _user_id):
        return self._status


def _schedule_response(rows, *, has_next=False):
    return {
        "data": {
            "Page": {
                "pageInfo": {"hasNextPage": has_next},
                "airingSchedules": rows,
            }
        }
    }


def _list_response(media_ids):
    return {
        "data": {
            "MediaListCollection": {
                "lists": [
                    {"entries": [{"mediaId": m, "progress": 0} for m in media_ids]}
                ]
            }
        }
    }


def _row(media_id, airing_at, episode=1):
    return {
        "id": media_id * 1000 + episode,
        "airingAt": airing_at,
        "episode": episode,
        "mediaId": media_id,
        "media": {
            "id": media_id,
            "title": {"romaji": "Title {0}".format(media_id)},
            "siteUrl": "https://anilist.co/anime/{0}".format(media_id),
        },
    }


@pytest.fixture(autouse=True)
def _clear_viewer_cache():
    sc._viewer_id_cache.clear()
    yield
    sc._viewer_id_cache.clear()


# ---------------------------------------------------------------------------
# _fetch_airing_window - one page, forward bounds, honest failure
# ---------------------------------------------------------------------------


async def test_window_fetch_sends_forward_bounds_and_exactly_one_page():
    cog = _FakeCog(responses=[_schedule_response([_row(1, NOW + 3600)])])
    rows, has_more, window = await cog._fetch_airing_window(
        [3, 1, 2], offset_days=0, now=NOW
    )

    assert len(cog.calls) == 1  # ONE call, never a paginate-until-empty loop
    query, variables, token = cog.calls[0]
    assert query is sc.SCHEDULE_WINDOW_QUERY
    assert token is None  # public data: a browse never spends a token
    assert variables["page"] == 1
    assert variables["perPage"] == API_PER_PAGE
    assert variables["mediaIds"] == [3, 1, 2]
    assert variables["greater"] == NOW
    assert variables["lesser"] == NOW + WINDOW_DAYS * DAY
    assert window == (NOW, NOW + WINDOW_DAYS * DAY)
    assert has_more is False
    assert len(rows) == 1


async def test_window_fetch_uses_its_own_query_not_the_pollers():
    # The poller's trailing-window query is load-bearing for its cursor; the two
    # must never converge.
    from cogs.anilist import airing as ai

    assert sc.SCHEDULE_WINDOW_QUERY != ai.AIRING_SCHEDULE_QUERY
    assert "hasNextPage" in sc.SCHEDULE_WINDOW_QUERY
    assert "hasNextPage" not in ai.AIRING_SCHEDULE_QUERY


async def test_window_fetch_skips_the_call_entirely_with_no_media_ids():
    cog = _FakeCog()
    rows, has_more, window = await cog._fetch_airing_window([], now=NOW)
    assert cog.calls == []  # nothing to ask about -> nothing on the wire
    assert (rows, has_more) == ([], False)
    assert window == (NOW, NOW + WINDOW_DAYS * DAY)


async def test_window_fetch_reports_a_fuller_window_instead_of_paginating():
    cog = _FakeCog(
        responses=[_schedule_response([_row(1, NOW + 60)], has_next=True)]
    )
    _rows, has_more, _window = await cog._fetch_airing_window([1], now=NOW)
    assert has_more is True
    assert len(cog.calls) == 1


async def test_window_fetch_distinguishes_failure_from_an_empty_window():
    # A dropped call (throttle ceiling, network) must NOT read as "nothing airs".
    cog = _FakeCog(responses=[None])
    rows, _has_more, _window = await cog._fetch_airing_window([1], now=NOW)
    assert rows is None

    cog = _FakeCog(responses=[_schedule_response([])])
    rows, _has_more, _window = await cog._fetch_airing_window([1], now=NOW)
    assert rows == []


# ---------------------------------------------------------------------------
# id resolution - cheapest rung first
# ---------------------------------------------------------------------------


async def test_id_resolution_from_the_optin_row_costs_no_graphql():
    pool = _FakePool(row={"anilist_user_id": 4242})
    cog = _FakeCog(pool=pool)
    anilist_id, error = await cog._resolve_anilist_user_id(7)
    assert (anilist_id, error) == (4242, None)
    assert cog.calls == []  # the id was already stored by an alert opt-in
    assert pool.fetchrow_calls == 1


async def test_id_resolution_second_call_hits_the_cache_not_the_db():
    pool = _FakePool(row={"anilist_user_id": 4242})
    cog = _FakeCog(pool=pool)
    await cog._resolve_anilist_user_id(7)
    anilist_id, error = await cog._resolve_anilist_user_id(7)
    assert (anilist_id, error) == (4242, None)
    assert pool.fetchrow_calls == 1  # cached, no second read
    assert cog.calls == []


async def test_id_resolution_falls_back_to_viewer_query_once_then_caches():
    pool = _FakePool(row=None)
    cog = _FakeCog(
        pool=pool, responses=[{"data": {"Viewer": {"id": 99, "name": "n"}}}]
    )
    anilist_id, error = await cog._resolve_anilist_user_id(7)
    assert (anilist_id, error) == (99, None)
    assert len(cog.calls) == 1
    assert cog.calls[0][2] == "tok"  # the token authenticates ONLY this call

    # The cold rung is once per user, not once per browse.
    again, error = await cog._resolve_anilist_user_id(7)
    assert (again, error) == (99, None)
    assert len(cog.calls) == 1


async def test_id_resolution_points_an_unlinked_user_at_login():
    cog = _FakeCog(pool=_FakePool(row=None), token_status=("missing", None))
    anilist_id, error = await cog._resolve_anilist_user_id(7)
    assert anilist_id is None
    assert "/anilist login" in error
    assert cog.calls == []  # never asks AniList for an account we cannot name


async def test_id_resolution_asks_an_expired_link_to_be_renewed():
    cog = _FakeCog(pool=_FakePool(row=None), token_status=("relink", None))
    anilist_id, error = await cog._resolve_anilist_user_id(7)
    assert anilist_id is None
    assert "re-link" in error
    assert cog.calls == []


async def test_id_resolution_survives_an_unresolvable_viewer():
    cog = _FakeCog(pool=_FakePool(row=None), responses=[{"data": {"Viewer": None}}])
    anilist_id, error = await cog._resolve_anilist_user_id(7)
    assert anilist_id is None
    assert error
    assert sc._viewer_id_cache == {}  # a failure is never cached


def test_viewer_cache_is_bounded_and_swept():
    now = 1000.0
    for user_id in range(sc._VIEWER_ID_SWEEP_AT + 5):
        sc._viewer_id_put(user_id, user_id, now)
    assert len(sc._viewer_id_cache) == sc._VIEWER_ID_SWEEP_AT + 5

    # One put past the cap on a later clock sweeps every stale row.
    sc._viewer_id_put(999_999, 1, now + sc._VIEWER_ID_TTL + 1)
    assert len(sc._viewer_id_cache) == 1
    assert sc._viewer_id_get(999_999, now + sc._VIEWER_ID_TTL + 1) == 1


def test_viewer_cache_expires_on_the_ttl():
    sc._viewer_id_put(1, 55, 100.0)
    assert sc._viewer_id_get(1, 100.0 + sc._VIEWER_ID_TTL - 1) == 55
    assert sc._viewer_id_get(1, 100.0 + sc._VIEWER_ID_TTL) is None


# ---------------------------------------------------------------------------
# public watching list
# ---------------------------------------------------------------------------


async def test_watching_ids_are_read_token_free():
    cog = _FakeCog(responses=[_list_response([5, 6])])
    ids = await cog._fetch_watching_media_ids(42)
    assert ids == [5, 6]
    query, variables, token = cog.calls[0]
    assert token is None  # public profile read: never spends the user's token
    assert variables == {"userId": 42}


async def test_watching_ids_treat_a_private_profile_as_empty_not_broken():
    cog = _FakeCog(responses=[{"data": {"MediaListCollection": None}}])
    assert await cog._fetch_watching_media_ids(42) == []


async def test_watching_ids_return_none_when_the_call_did_not_come_back():
    cog = _FakeCog(responses=[None])
    assert await cog._fetch_watching_media_ids(42) is None


# ---------------------------------------------------------------------------
# _schedule_payload - the per-invocation request budget
# ---------------------------------------------------------------------------


async def test_channel_browse_costs_exactly_one_graphql_call():
    pool = _FakePool(rows=[{"media_id": 3}, {"media_id": 1}, {"media_id": 3}])
    cog = _FakeCog(
        pool=pool, responses=[_schedule_response([_row(1, int(time.time()) + 600)])]
    )
    error, view = await cog._schedule_payload(7, "channel", guild_id=1, channel_id=2)

    assert error is None
    assert len(cog.calls) == 1  # subs come from Postgres, not from AniList
    assert cog.calls[0][1]["mediaIds"] == [1, 3]  # deduped + sorted
    assert view.scope == "channel"
    assert pool.fetch_calls == 1


async def test_personal_browse_costs_two_calls_when_the_id_is_known():
    pool = _FakePool(row={"anilist_user_id": 42})
    cog = _FakeCog(
        pool=pool,
        responses=[
            _list_response([10, 11]),
            _schedule_response([_row(10, int(time.time()) + 600)]),
        ],
    )
    error, view = await cog._schedule_payload(7, "me")

    assert error is None
    assert len(cog.calls) == 2  # list + window, nothing else
    assert cog.calls[0][0] is sc.AIRING_LIST_QUERY
    assert cog.calls[1][0] is sc.SCHEDULE_WINDOW_QUERY
    assert view.media_ids == [10, 11]


async def test_personal_browse_cold_path_costs_three_calls_once():
    pool = _FakePool(row=None)
    cog = _FakeCog(
        pool=pool,
        responses=[
            {"data": {"Viewer": {"id": 42}}},
            _list_response([10]),
            _schedule_response([]),
            _list_response([10]),
            _schedule_response([]),
        ],
    )
    error, _view = await cog._schedule_payload(7, "me")
    assert error is None
    assert len(cog.calls) == 3

    # The id is cached now, so the SECOND browse is back to the 2-call budget.
    error, _view = await cog._schedule_payload(7, "me")
    assert error is None
    assert len(cog.calls) == 5


async def test_channel_scope_in_a_dm_is_refused_before_any_work():
    cog = _FakeCog()
    error, view = await cog._schedule_payload(7, "channel", guild_id=None, channel_id=2)
    assert view is None
    assert "server channel" in error
    assert cog.calls == []


async def test_channel_without_subscriptions_gets_an_honest_pointer():
    cog = _FakeCog(pool=_FakePool(rows=[]))
    error, view = await cog._schedule_payload(7, "channel", guild_id=1, channel_id=2)
    assert view is None
    assert "/anilistfeed" in error
    assert cog.calls == []  # no titles -> no request


async def test_empty_watching_list_is_an_honest_card_not_a_crash():
    cog = _FakeCog(
        pool=_FakePool(row={"anilist_user_id": 42}),
        responses=[_list_response([])],
    )
    error, view = await cog._schedule_payload(7, "me")
    assert view is None
    assert "Watching" in error
    assert len(cog.calls) == 1  # never asks for a schedule of nothing


async def test_unreachable_anilist_is_reported_as_such():
    cog = _FakeCog(
        pool=_FakePool(row={"anilist_user_id": 42}), responses=[None]
    )
    error, view = await cog._schedule_payload(7, "me")
    assert view is None
    assert "try again" in error

    # ... and the same when the LIST came back but the window did not.
    cog = _FakeCog(
        pool=_FakePool(row={"anilist_user_id": 42}),
        responses=[_list_response([10]), None],
    )
    error, view = await cog._schedule_payload(7, "me")
    assert view is None
    assert "try again" in error


async def test_an_enormous_list_is_capped_and_the_panel_says_so():
    cog = _FakeCog(
        pool=_FakePool(row={"anilist_user_id": 42}),
        responses=[
            _list_response(list(range(MAX_MEDIA_IDS + 50))),
            _schedule_response([]),
        ],
    )
    error, view = await cog._schedule_payload(7, "me")
    assert error is None
    assert len(cog.calls[1][1]["mediaIds"]) == MAX_MEDIA_IDS
    assert view.truncated is True


# ---------------------------------------------------------------------------
# the panel - rendering, local paging, window navigation
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.done = False
        self.edits = 0
        self.messages = []

    def is_done(self):
        return self.done

    async def defer(self, **_kwargs):
        self.done = True

    async def edit_message(self, **_kwargs):
        self.edits += 1

    async def send_message(self, content, **_kwargs):
        self.messages.append(content)
        self.done = True


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **_kwargs):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = type("U", (), {"id": 7})()
        self.guild_id = 1


class _DenyingThrottle:
    def allow_interactive(self, _user_id, _guild_id, now=None):
        return False


def _view(cog=None, rows=None, *, offset_days=0, count=25):
    rows = (
        rows
        if rows is not None
        else [_row(i, NOW + 3600 * i) for i in range(1, count + 1)]
    )
    return AiringScheduleView(
        cog,
        7,
        "me",
        [1, 2, 3],
        rows,
        window=window_bounds(NOW, offset_days),
        offset_days=offset_days,
    )


def test_panel_renders_grouped_days_and_relative_timestamps():
    view = _view()
    body = view._body_text()
    assert "<t:{0}:R>".format(NOW + 3600) in body  # every viewer's own timezone
    assert "### <t:" in body  # grouped by day
    assert body.count("Episode") == PAGE_SIZE  # one bounded page


def test_panel_header_names_the_scope_and_the_window():
    view = _view()
    header = view._header_text()
    assert "Watching" in header
    assert "<t:{0}:d>".format(NOW) in header
    assert "<t:{0}:d>".format(NOW + WINDOW_DAYS * DAY) in header


def test_panel_footer_counts_the_whole_window_not_the_page():
    view = _view(count=25)
    footer = view._footer_text()
    assert "25" in footer
    assert "1/3" in footer


def test_panel_footer_flags_a_fuller_window_and_a_capped_list():
    view = AiringScheduleView(
        None,
        7,
        "me",
        [1],
        [_row(1, NOW + 60)],
        window=window_bounds(NOW, 0),
        has_more=True,
        truncated=True,
    )
    footer = view._footer_text()
    assert "more airings" in footer
    assert "very large" in footer


def test_empty_window_renders_an_honest_card():
    view = _view(rows=[])
    assert "Nothing airing" in view._body_text()
    assert view.rows == []


def test_channel_scope_labels_the_channel():
    view = AiringScheduleView(
        None, 7, "channel", [1], [], window=window_bounds(NOW, 0)
    )
    assert "channel" in view._header_text()


async def test_paging_inside_the_window_costs_zero_graphql_calls():
    cog = _FakeCog()
    view = _view(cog, count=25)
    interaction = _FakeInteraction()

    await view._change_page(interaction, +1)
    assert view.page == 1
    assert cog.calls == []  # the window was fetched once; paging is local
    assert interaction.response.edits == 1

    await view._change_page(_FakeInteraction(), +1)
    assert view.page == 2
    # Clamped at the last page rather than running off the end.
    await view._change_page(_FakeInteraction(), +1)
    assert view.page == 2
    await view._change_page(_FakeInteraction(), -5)
    assert view.page == 0
    assert cog.calls == []


async def test_window_step_costs_exactly_one_call_and_resets_the_page():
    cog = _FakeCog(responses=[_schedule_response([_row(1, NOW + 8 * DAY)])])
    view = _view(cog, count=25)
    view.page = 2

    await view._change_window(_FakeInteraction(), +1)

    assert view.offset_days == MAX_WINDOW_OFFSET
    assert view.page == 0
    assert len(cog.calls) == 1
    assert cog.calls[0][1]["greater"] >= NOW


async def test_window_step_past_the_horizon_is_refused_without_a_call():
    cog = _FakeCog()
    view = _view(cog, offset_days=MAX_WINDOW_OFFSET)
    interaction = _FakeInteraction()

    await view._change_window(interaction, +1)

    assert cog.calls == []
    assert view.offset_days == MAX_WINDOW_OFFSET
    assert interaction.response.messages  # told, not silently ignored


async def test_window_step_before_now_is_refused_without_a_call():
    cog = _FakeCog()
    view = _view(cog, offset_days=0)
    interaction = _FakeInteraction()

    await view._change_window(interaction, -1)

    assert cog.calls == []
    assert view.offset_days == 0
    assert interaction.response.messages


async def test_each_edge_refusal_names_its_own_direction():
    """"Earlier" at now must not answer "as far AHEAD as I can look"."""

    back = _FakeInteraction()
    await _view(_FakeCog(), offset_days=0)._change_window(back, -1)
    forward = _FakeInteraction()
    await _view(_FakeCog(), offset_days=MAX_WINDOW_OFFSET)._change_window(forward, +1)

    assert "back" in back.response.messages[0]
    assert "ahead" in forward.response.messages[0]


async def test_a_no_op_edge_step_costs_no_interactive_quota_slot():
    """The edge check runs first: no fetch means nothing to charge for."""

    cog = _FakeCog()
    cog._throttle = _DenyingThrottle()
    view = _view(cog, offset_days=0)
    interaction = _FakeInteraction()

    await view._change_window(interaction, -1)

    # The edge wording, not the throttle's - the slot was never asked for.
    assert cog.calls == []
    assert "back" in interaction.response.messages[0]


async def test_window_step_is_gated_by_the_interactive_throttle():
    cog = _FakeCog(responses=[_schedule_response([])])
    cog._throttle = _DenyingThrottle()
    view = _view(cog)
    interaction = _FakeInteraction()

    await view._change_window(interaction, +1)

    assert cog.calls == []  # refused BEFORE the fetch
    assert view.offset_days == 0
    assert interaction.response.messages


async def test_a_failed_window_step_keeps_the_panel_on_its_current_window():
    cog = _FakeCog(responses=[None])
    view = _view(cog, count=25)
    interaction = _FakeInteraction()

    await view._change_window(interaction, +1)

    assert view.offset_days == 0  # unchanged: a failed step never lies
    assert len(view.rows) == 25
    assert interaction.followup.messages


def _buttons(view):
    import discord

    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def test_panel_only_shows_page_buttons_when_there_is_more_than_one_page():
    assert len(_buttons(_view(count=3))) == 2  # window navigation only
    assert len(_buttons(_view(count=25))) == 4  # paging + window navigation


def test_panel_disables_navigation_at_the_edges():
    view = _view(count=25)
    page_prev, page_next, window_prev, window_next = _buttons(view)
    assert page_prev.disabled is True  # first page
    assert page_next.disabled is False
    assert window_prev.disabled is True  # cannot look before now
    assert window_next.disabled is False

    view.page = page_count(len(view.rows)) - 1
    view.offset_days = MAX_WINDOW_OFFSET
    view._build()
    page_prev, page_next, window_prev, window_next = _buttons(view)
    assert page_next.disabled is True  # last page
    assert window_next.disabled is True  # cannot look past the horizon
    assert window_prev.disabled is False
