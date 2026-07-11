"""Unit tests for the pure page-math behind the interactive queue view.

``cogs.music.music.queue_page`` resolves the paginated slice of the upcoming
tracks for a requested page: it clamps the page into range (so a queue that
shrank under the viewer never lands on a blank page), reports a page count that
is at least one even for an empty queue, and returns the ``[start:end]`` bounds
that slice the track list. It touches no discord, sonolink node, database or
voice connection - only integer arithmetic - so it is tested in isolation here.

``sonolink`` is stubbed by the repo-root conftest on the 3.10 dev box and
imported for real on 3.12+ CI; importing ``music`` is safe either way.
"""

from cogs.music import music

PER = music.QUEUE_PAGE_SIZE  # 10


# ---------------------------------------------------------------------------
# empty queue
# ---------------------------------------------------------------------------


def test_empty_queue_is_one_page():
    # Zero tracks still resolves to a single (empty) page 0, slice [0:0].
    page, pages, start, end = music.queue_page(0, 0)
    assert (page, pages, start, end) == (0, 1, 0, 0)


def test_empty_queue_clamps_a_stale_page():
    # A page index left over from a queue that has since drained clamps to 0.
    page, pages, start, end = music.queue_page(0, 5)
    assert (page, pages, start, end) == (0, 1, 0, 0)


# ---------------------------------------------------------------------------
# page counts on the per-page boundary
# ---------------------------------------------------------------------------


def test_exactly_one_full_page():
    # Ten tracks fill exactly one page; no second page appears.
    page, pages, start, end = music.queue_page(PER, 0)
    assert (page, pages, start, end) == (0, 1, 0, PER)


def test_one_over_a_full_page_spills_to_two():
    _, pages, _, _ = music.queue_page(PER + 1, 0)
    assert pages == 2


def test_two_full_pages_exactly():
    _, pages, _, _ = music.queue_page(2 * PER, 0)
    assert pages == 2


def test_twenty_four_tracks_is_three_pages():
    _, pages, _, _ = music.queue_page(24, 0)
    assert pages == 3


# ---------------------------------------------------------------------------
# slicing
# ---------------------------------------------------------------------------


def test_first_page_slice():
    page, pages, start, end = music.queue_page(24, 0)
    assert (page, pages, start, end) == (0, 3, 0, 10)


def test_middle_page_slice():
    page, pages, start, end = music.queue_page(24, 1)
    assert (page, pages, start, end) == (1, 3, 10, 20)


def test_last_page_slice_is_short():
    # The tail page holds the remainder only (24 -> 4 tracks), end clamps to 24.
    page, pages, start, end = music.queue_page(24, 2)
    assert (page, pages, start, end) == (2, 3, 20, 24)


# ---------------------------------------------------------------------------
# clamping out-of-range pages
# ---------------------------------------------------------------------------


def test_page_past_the_end_clamps_to_last():
    page, pages, start, end = music.queue_page(24, 99)
    assert (page, pages, start, end) == (2, 3, 20, 24)


def test_negative_page_clamps_to_zero():
    page, pages, start, end = music.queue_page(24, -3)
    assert (page, pages, start, end) == (0, 3, 0, 10)


def test_negative_total_is_treated_as_empty():
    # Defensive: a bogus negative count degrades to the empty single page.
    page, pages, start, end = music.queue_page(-5, 2)
    assert (page, pages, start, end) == (0, 1, 0, 0)


# ---------------------------------------------------------------------------
# custom per_page
# ---------------------------------------------------------------------------


def test_custom_per_page_paging():
    # Five items at three-per-page -> two pages, second page holds the last two.
    page, pages, start, end = music.queue_page(5, 1, per_page=3)
    assert (page, pages, start, end) == (1, 2, 3, 5)
