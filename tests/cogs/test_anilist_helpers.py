"""Unit tests for the pure helpers in ``cogs/anilist/helpers.py``.

Every function under test is a deterministic, side-effect-free transform (no
network, DB, Discord, or Lavalink), so these are plain synchronous asserts.
Assertions were written against the real implementation, not the docstrings:
where the two disagree (e.g. ``_clean_description`` strips HTML but NOT
markdown, and ``_format_fuzzy_date`` does not zero-pad a year-only value), the
tests follow the code.
"""

import datetime

from cogs.anilist import helpers
from cogs.anilist.helpers import (
    DEFAULT_SCORE_FORMAT,
    SCORE_FORMATS,
    SEASONS,
    VALID_STATUSES,
    _clean_description,
    _current_season,
    _format_fuzzy_date,
    _format_ranking,
    _format_score,
    _media_unit,
    _parse_status,
    _progress_max,
    _step_season,
    parse_score,
    render_score,
    score_hint,
)

# ---------------------------------------------------------------------------
# _clean_description
# ---------------------------------------------------------------------------


def test_clean_description_empty_and_none():
    assert _clean_description(None) == ""
    assert _clean_description("") == ""


def test_clean_description_strips_html_tags():
    assert _clean_description("<b>Hello</b> world") == "Hello world"
    assert _clean_description("A<br>B<br/>C") == "ABC"
    assert _clean_description("<i class='x'>tag</i>") == "tag"


def test_clean_description_collapses_whitespace():
    assert _clean_description("a\n\n  b\t c") == "a b c"
    assert _clean_description("   padded   ") == "padded"


def test_clean_description_leaves_markdown_untouched():
    # The implementation only removes HTML tags; markdown asterisks survive.
    assert _clean_description("<b>bold</b> **still bold**") == "bold **still bold**"


def test_clean_description_truncates_over_600_chars():
    result = _clean_description("a" * 700)
    assert len(result) == 603
    assert result == "a" * 600 + "..."
    assert result.endswith("...")


def test_clean_description_truncation_rstrips_before_ellipsis():
    # 599 'a', then a space landing on index 599, then filler past the limit.
    text = "a" * 599 + "   " + "b" * 100
    result = _clean_description(text)
    # Whitespace collapsed, sliced to 600, trailing space rstripped, then "...".
    assert result == "a" * 599 + "..."
    assert len(result) == 602


def test_clean_description_at_boundary_not_truncated():
    # Exactly 600 chars must be returned verbatim (no ellipsis).
    result = _clean_description("a" * 600)
    assert result == "a" * 600
    assert not result.endswith("...")


# ---------------------------------------------------------------------------
# _current_season
# ---------------------------------------------------------------------------


def _dt(month):
    return datetime.datetime(2021, month, 15, tzinfo=datetime.timezone.utc)


def test_current_season_winter_months():
    for month in (12, 1, 2):
        season, year = _current_season(_dt(month))
        assert season == "WINTER"
        assert year == 2021


def test_current_season_spring_months():
    for month in (3, 4, 5):
        assert _current_season(_dt(month)) == ("SPRING", 2021)


def test_current_season_summer_months():
    for month in (6, 7, 8):
        assert _current_season(_dt(month)) == ("SUMMER", 2021)


def test_current_season_fall_months():
    for month in (9, 10, 11):
        assert _current_season(_dt(month)) == ("FALL", 2021)


def test_current_season_default_now_is_valid():
    season, year = _current_season()
    assert season in set(SEASONS)
    assert isinstance(year, int)


# ---------------------------------------------------------------------------
# _step_season
# ---------------------------------------------------------------------------


def test_step_season_forward_within_year():
    assert _step_season("WINTER", 2021, forward=True) == ("SPRING", 2021)
    assert _step_season("SPRING", 2021, forward=True) == ("SUMMER", 2021)
    assert _step_season("SUMMER", 2021, forward=True) == ("FALL", 2021)


def test_step_season_forward_wraps_fall_to_winter_next_year():
    assert _step_season("FALL", 2021, forward=True) == ("WINTER", 2022)


def test_step_season_backward_within_year():
    assert _step_season("FALL", 2021, forward=False) == ("SUMMER", 2021)
    assert _step_season("SUMMER", 2021, forward=False) == ("SPRING", 2021)
    assert _step_season("SPRING", 2021, forward=False) == ("WINTER", 2021)


def test_step_season_backward_wraps_winter_to_fall_previous_year():
    assert _step_season("WINTER", 2021, forward=False) == ("FALL", 2020)


def test_step_season_invalid_falls_back_to_current():
    season, year = _step_season("NOTASEASON", 2021, forward=True)
    assert season in set(SEASONS)
    assert isinstance(year, int)


# ---------------------------------------------------------------------------
# _parse_status
# ---------------------------------------------------------------------------


def test_parse_status_none_and_empty():
    assert _parse_status(None) is None
    assert _parse_status("") is None
    assert _parse_status("   ") is None


def test_parse_status_watch_aliases_map_to_current():
    for word in ("watching", "watch", "reading", "read", "current"):
        assert _parse_status(word) == "CURRENT"


def test_parse_status_plan_aliases_map_to_planning():
    for word in ("plan", "planned", "planning", "ptw"):
        assert _parse_status(word) == "PLANNING"


def test_parse_status_completed_aliases():
    for word in ("completed", "complete", "done", "finished"):
        assert _parse_status(word) == "COMPLETED"


def test_parse_status_dropped_paused_repeating_aliases():
    assert _parse_status("drop") == "DROPPED"
    assert _parse_status("dropped") == "DROPPED"
    assert _parse_status("hold") == "PAUSED"
    assert _parse_status("onhold") == "PAUSED"
    assert _parse_status("rewatching") == "REPEATING"
    assert _parse_status("repeat") == "REPEATING"


def test_parse_status_normalises_spacing_and_separators():
    # "plan to watch" -> stripped of spaces/hyphens/underscores -> "plantowatch".
    assert _parse_status("plan to watch") == "PLANNING"
    assert _parse_status("plan-to-read") == "PLANNING"
    assert _parse_status("on_hold") == "PAUSED"
    assert _parse_status("  WATCHING  ") == "CURRENT"


def test_parse_status_raw_enum_passes_through():
    for status in VALID_STATUSES:
        assert _parse_status(status) == status


def test_parse_status_invalid_returns_none():
    assert _parse_status("xyz") is None
    assert _parse_status("not a status") is None


# ---------------------------------------------------------------------------
# _format_score
# ---------------------------------------------------------------------------


def test_format_score_none():
    assert _format_score(None) is None


def test_format_score_whole_numbers_drop_trailing_zero():
    assert _format_score(8.0) == "8"
    assert _format_score(8) == "8"
    assert _format_score("8") == "8"
    assert _format_score(0) == "0"


def test_format_score_fractional_kept():
    assert _format_score(7.5) == "7.5"
    assert _format_score("7.5") == "7.5"


def test_format_score_non_numeric_stringified():
    assert _format_score("abc") == "abc"


# ---------------------------------------------------------------------------
# _media_unit
# ---------------------------------------------------------------------------


def test_media_unit_manga_type_is_chapter():
    assert _media_unit({"type": "MANGA"}) == "chapter"
    assert _media_unit({"type": "MANGA"}, plural=True) == "chapters"


def test_media_unit_anime_type_is_episode():
    assert _media_unit({"type": "ANIME"}) == "episode"
    assert _media_unit({"type": "ANIME"}, plural=True) == "episodes"


def test_media_unit_falls_back_to_counts_when_type_missing():
    # No type: chapters present and no episodes -> treated as manga.
    assert _media_unit({"chapters": 120}) == "chapter"
    # Episodes present -> episode.
    assert _media_unit({"episodes": 12}) == "episode"
    # Nothing at all -> defaults to episode.
    assert _media_unit({}) == "episode"


def test_media_unit_type_overrides_counts():
    # Explicit ANIME even though chapters are present.
    assert _media_unit({"type": "ANIME", "chapters": 5}) == "episode"


# ---------------------------------------------------------------------------
# _progress_max
# ---------------------------------------------------------------------------


def test_progress_max_anime_returns_episodes():
    assert _progress_max({"type": "ANIME", "episodes": 24}) == 24


def test_progress_max_manga_returns_chapters():
    assert _progress_max({"type": "MANGA", "chapters": 100}) == 100


def test_progress_max_returns_none_when_unknown():
    assert _progress_max({"type": "ANIME", "episodes": None}) is None
    assert _progress_max({"type": "ANIME"}) is None
    assert _progress_max({"type": "MANGA", "chapters": 0}) is None
    assert _progress_max({}) is None


# ---------------------------------------------------------------------------
# _format_fuzzy_date
# ---------------------------------------------------------------------------


def test_format_fuzzy_date_none_and_no_year():
    assert _format_fuzzy_date(None) is None
    assert _format_fuzzy_date({}) is None
    assert _format_fuzzy_date({"month": 5, "day": 3}) is None


def test_format_fuzzy_date_full():
    assert _format_fuzzy_date({"year": 2021, "month": 5, "day": 3}) == "2021-05-03"


def test_format_fuzzy_date_year_and_month_only():
    assert _format_fuzzy_date({"year": 2021, "month": 5}) == "2021-05"


def test_format_fuzzy_date_year_only():
    # Year-only path returns the bare str(year); it is not zero-padded.
    assert _format_fuzzy_date({"year": 2021}) == "2021"
    assert _format_fuzzy_date({"year": 2021, "day": 3}) == "2021"


# ---------------------------------------------------------------------------
# _format_ranking
# ---------------------------------------------------------------------------


def test_format_ranking_missing_rank_or_context():
    assert _format_ranking({}) is None
    assert _format_ranking({"rank": 3}) is None
    assert _format_ranking({"context": "most popular"}) is None
    assert _format_ranking({"rank": 0, "context": "most popular"}) is None
    assert _format_ranking({"rank": 3, "context": "   "}) is None


def test_format_ranking_all_time():
    ranking = {"rank": 3, "context": "most popular all time", "allTime": True}
    assert _format_ranking(ranking) == "#3 Most Popular (all time)"


def test_format_ranking_non_all_time():
    assert _format_ranking({"rank": 5, "context": "most popular"}) == "#5 Most Popular"
    ranking = {"rank": 1, "context": "highest rated", "allTime": False}
    assert _format_ranking(ranking) == "#1 Highest Rated"


def test_format_ranking_all_time_flag_without_context_phrase():
    # allTime True but context lacks the phrase -> plain title-cased context.
    ranking = {"rank": 2, "context": "most popular", "allTime": True}
    assert _format_ranking(ranking) == "#2 Most Popular"


# ---------------------------------------------------------------------------
# module smoke
# ---------------------------------------------------------------------------


def test_seasons_and_statuses_are_consistent():
    assert SEASONS == ("WINTER", "SPRING", "SUMMER", "FALL")
    # Every alias target must be a real AniList status.
    assert set(helpers._STATUS_ALIASES.values()) <= VALID_STATUSES


# ---------------------------------------------------------------------------
# render_score - the five AniList score formats
# ---------------------------------------------------------------------------


def test_render_score_unset_is_none_in_every_format():
    # None and 0 both mean "unset" (AniList convention) in every format.
    for fmt in SCORE_FORMATS:
        assert render_score(None, fmt) is None
        assert render_score(0, fmt) is None
        assert render_score(0.0, fmt) is None


def test_render_score_non_numeric_is_none():
    assert render_score("abc", "POINT_100") is None


def test_render_score_point_100():
    assert render_score(85, "POINT_100") == "85"
    assert render_score(85.0, "POINT_100") == "85"
    assert render_score(100, "POINT_100") == "100"
    assert render_score(1, "POINT_100") == "1"


def test_render_score_point_10():
    assert render_score(8, "POINT_10") == "8/10"
    assert render_score(8.0, "POINT_10") == "8/10"
    assert render_score(10, "POINT_10") == "10/10"


def test_render_score_point_10_decimal():
    assert render_score(8.5, "POINT_10_DECIMAL") == "8.5/10"
    assert render_score(8, "POINT_10_DECIMAL") == "8.0/10"
    assert render_score(10.0, "POINT_10_DECIMAL") == "10.0/10"


def test_render_score_point_5_stars():
    assert render_score(1, "POINT_5") == "★☆☆☆☆"
    assert render_score(4, "POINT_5") == "★★★★☆"
    assert render_score(5, "POINT_5") == "★★★★★"


def test_render_score_point_3_faces():
    assert render_score(1, "POINT_3") == "🙁"
    assert render_score(2, "POINT_3") == "😐"
    assert render_score(3, "POINT_3") == "🙂"


def test_render_score_unknown_format_falls_back_to_point_100():
    assert render_score(85, "GARBAGE") == "85"
    assert render_score(85, None) == "85"


def test_render_score_default_format_is_point_100():
    assert render_score(85) == "85"
    assert DEFAULT_SCORE_FORMAT == "POINT_100"


# ---------------------------------------------------------------------------
# parse_score - validation per format, 0 kept as unset
# ---------------------------------------------------------------------------


def test_parse_score_empty_and_none_and_malformed():
    for fmt in SCORE_FORMATS:
        assert parse_score(None, fmt) is None
        assert parse_score("", fmt) is None
        assert parse_score("   ", fmt) is None
        assert parse_score("abc", fmt) is None


def test_parse_score_zero_is_unset_not_rejected():
    # 0 is a valid input meaning "clear the score" in every format.
    for fmt in SCORE_FORMATS:
        assert parse_score("0", fmt) == 0.0


def test_parse_score_point_100():
    assert parse_score("85", "POINT_100") == 85.0
    assert parse_score("100", "POINT_100") == 100.0
    assert parse_score("101", "POINT_100") is None
    assert parse_score("-1", "POINT_100") is None
    # POINT_100 is integer-only.
    assert parse_score("85.5", "POINT_100") is None


def test_parse_score_point_10():
    assert parse_score("8", "POINT_10") == 8.0
    assert parse_score("10", "POINT_10") == 10.0
    assert parse_score("11", "POINT_10") is None
    assert parse_score("8.5", "POINT_10") is None


def test_parse_score_point_10_decimal():
    assert parse_score("8.5", "POINT_10_DECIMAL") == 8.5
    assert parse_score("8", "POINT_10_DECIMAL") == 8.0
    assert parse_score("10.0", "POINT_10_DECIMAL") == 10.0
    assert parse_score("10.5", "POINT_10_DECIMAL") is None
    assert parse_score("-0.5", "POINT_10_DECIMAL") is None


def test_parse_score_point_5():
    assert parse_score("5", "POINT_5") == 5.0
    assert parse_score("3", "POINT_5") == 3.0
    assert parse_score("6", "POINT_5") is None
    assert parse_score("3.5", "POINT_5") is None


def test_parse_score_point_3():
    assert parse_score("1", "POINT_3") == 1.0
    assert parse_score("3", "POINT_3") == 3.0
    assert parse_score("4", "POINT_3") is None
    assert parse_score("2.5", "POINT_3") is None


def test_parse_score_unknown_format_falls_back_to_point_100():
    assert parse_score("85", "GARBAGE") == 85.0
    assert parse_score("101", "GARBAGE") is None


def test_parse_score_strips_whitespace():
    assert parse_score("  85  ", "POINT_100") == 85.0


# ---------------------------------------------------------------------------
# score_hint - placeholder ranges
# ---------------------------------------------------------------------------


def test_score_hint_per_format():
    assert score_hint("POINT_100") == "0-100"
    assert score_hint("POINT_10") == "0-10"
    assert score_hint("POINT_10_DECIMAL") == "0.0-10.0"
    assert score_hint("POINT_5") == "0-5"
    assert score_hint("POINT_3") == "1-3"


def test_score_hint_unknown_and_default():
    assert score_hint("GARBAGE") == "0-100"
    assert score_hint() == "0-100"


def test_score_formats_registry_is_the_five_anilist_formats():
    assert SCORE_FORMATS == frozenset(
        {"POINT_100", "POINT_10_DECIMAL", "POINT_10", "POINT_5", "POINT_3"}
    )
