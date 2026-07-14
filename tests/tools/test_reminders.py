"""Unit tests for the pure reminder-card helpers (tools/reminders.py).

These lock down the four side-effect-free pieces the "see and cancel my
reminders" card relies on: the paging math, the label truncation, the
defensive type filter, and the capped-count label. No Discord object, DB, or
event loop is required - the functions are pure.
"""

from tools import reminders as rem

# ---------------------------------------------------------------------------
# paginate
# ---------------------------------------------------------------------------


def test_paginate_empty_list_is_one_blank_page():
    assert rem.paginate(0, 0) == (0, 1, 0, 0)


def test_paginate_single_full_page():
    # Exactly one page of the default size -> no second page.
    assert rem.paginate(rem.REMINDER_PAGE_SIZE, 0) == (
        0,
        1,
        0,
        rem.REMINDER_PAGE_SIZE,
    )


def test_paginate_second_page_slice():
    clamped, pages, start, end = rem.paginate(rem.REMINDER_PAGE_SIZE + 3, 1)
    assert clamped == 1
    assert pages == 2
    assert (start, end) == (rem.REMINDER_PAGE_SIZE, rem.REMINDER_PAGE_SIZE + 3)


def test_paginate_clamps_overshoot_to_last_page():
    # Asking for a page past the end lands on the last real page, never blank.
    clamped, pages, start, end = rem.paginate(rem.REMINDER_PAGE_SIZE + 1, 99)
    assert clamped == pages - 1 == 1
    assert start == rem.REMINDER_PAGE_SIZE
    assert end == rem.REMINDER_PAGE_SIZE + 1


def test_paginate_clamps_negative_page():
    assert rem.paginate(5, -3)[0] == 0


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


def test_truncate_leaves_short_text_untouched():
    assert rem.truncate("hi there", 50) == "hi there"


def test_truncate_strips_whitespace():
    assert rem.truncate("  spaced  ", 50) == "spaced"


def test_truncate_uses_ascii_ellipsis_and_respects_limit():
    out = rem.truncate("x" * 200, 100)
    assert len(out) == 100
    assert out.endswith("...")
    assert "…" not in out  # never the fancy ellipsis


def test_truncate_tiny_limit_degrades_to_hard_cut():
    assert rem.truncate("abcdef", 2) == "ab"


def test_truncate_none_is_empty():
    assert rem.truncate(None, 10) == ""


# ---------------------------------------------------------------------------
# filter_reminders (type scoping)
# ---------------------------------------------------------------------------


def test_filter_reminders_keeps_only_reminder_events():
    rows = [
        {"id": 1, "event": "reminder"},
        {"id": 2, "event": "tempban"},
        {"id": 3, "event": "reminder"},
        {"id": 4, "event": "some_future_event"},
    ]
    kept = rem.filter_reminders(rows)
    assert [r["id"] for r in kept] == [1, 3]


def test_filter_reminders_empty():
    assert rem.filter_reminders([]) == []


def test_filter_reminders_drops_a_tempban_only_list():
    assert rem.filter_reminders([{"id": 9, "event": "tempban"}]) == []


# ---------------------------------------------------------------------------
# format_count
# ---------------------------------------------------------------------------


def test_format_count_plain_when_not_capped():
    assert rem.format_count(7, False) == "7"


def test_format_count_collapses_overflow():
    assert rem.format_count(rem.REMINDER_LIST_CAP, True) == "25+"


def test_format_count_zero():
    assert rem.format_count(0, False) == "0"
