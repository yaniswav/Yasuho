"""Tests for tools.paginator.

Covers the pure helper ``paginate_lines`` (chunking, placeholder, title/colour)
and the ``Paginator`` view's ``_sync`` button/footer bookkeeping. Nothing here
touches the network, Discord, a database, or Lavalink: ``paginate_lines`` is a
pure function and ``Paginator._sync`` only mutates in-memory button/embed state,
so we never dispatch an interaction or send a message.
"""

import math

import discord

from tools.paginator import Paginator, paginate_lines


# ---------------------------------------------------------------------------
# paginate_lines
# ---------------------------------------------------------------------------


def test_empty_returns_single_placeholder_embed():
    embeds = paginate_lines([])
    assert len(embeds) == 1
    assert embeds[0].description == "Nothing to show."


def test_empty_applies_title_and_colour():
    embeds = paginate_lines([], title="Leaderboard", colour=0x00FF00)
    assert len(embeds) == 1
    assert embeds[0].title == "Leaderboard"
    assert embeds[0].colour.value == 0x00FF00


def test_single_full_page_is_one_embed():
    lines = [f"line {i}" for i in range(10)]
    embeds = paginate_lines(lines, per_page=10)
    assert len(embeds) == 1
    assert embeds[0].description == "\n".join(lines)


def test_lines_chunk_into_ceil_n_over_per_page_embeds():
    # Exercise a few (n, per_page) shapes, including a non-even final chunk.
    for n, per_page in [(1, 10), (10, 10), (11, 10), (25, 10), (7, 3), (100, 7)]:
        lines = [f"line {i}" for i in range(n)]
        embeds = paginate_lines(lines, per_page=per_page)
        expected = math.ceil(n / per_page)
        assert len(embeds) == expected, (n, per_page)
        # Each embed carries exactly its slice of the input, in order.
        for page, embed in enumerate(embeds):
            chunk = lines[page * per_page : (page + 1) * per_page]
            assert embed.description == "\n".join(chunk)


def test_default_per_page_is_ten():
    lines = [f"line {i}" for i in range(21)]
    embeds = paginate_lines(lines)  # per_page default = 10
    assert len(embeds) == 3


def test_title_and_colour_applied_to_every_page():
    lines = [f"line {i}" for i in range(25)]
    embeds = paginate_lines(lines, title="Scores", colour=0x123456, per_page=10)
    assert len(embeds) == 3
    for embed in embeds:
        assert embed.title == "Scores"
        assert embed.colour.value == 0x123456


def test_colour_defaults_to_a_random_colour_when_none():
    # colour=None -> random_colour() is used, so a colour is always present.
    embeds = paginate_lines(["a", "b"], per_page=1)
    assert all(isinstance(embed.colour, discord.Colour) for embed in embeds)


# ---------------------------------------------------------------------------
# Paginator._sync
# ---------------------------------------------------------------------------


def test_sync_at_first_index_disables_first_and_prev_only():
    embeds = paginate_lines([f"line {i}" for i in range(30)], per_page=10)
    view = Paginator(embeds, author_id=1)
    view.index = 0
    view._sync()
    assert view.first_page.disabled is True
    assert view.prev_page.disabled is True
    assert view.next_page.disabled is False
    assert view.last_page.disabled is False


def test_sync_at_last_index_disables_next_and_last_only():
    embeds = paginate_lines([f"line {i}" for i in range(30)], per_page=10)
    view = Paginator(embeds, author_id=1)
    view.index = len(view.embeds) - 1
    view._sync()
    assert view.next_page.disabled is True
    assert view.last_page.disabled is True
    assert view.first_page.disabled is False
    assert view.prev_page.disabled is False


def test_sync_footer_reflects_current_page():
    embeds = paginate_lines([f"line {i}" for i in range(30)], per_page=10)
    view = Paginator(embeds, author_id=1)

    view.index = 0
    view._sync()
    assert view.embeds[0].footer.text == "Page 1/3"

    view.index = 2
    view._sync()
    assert view.embeds[2].footer.text == "Page 3/3"


def test_single_embed_disables_all_navigation():
    # When there is one page it is both the first and the last, so every
    # navigation button is disabled and the footer reads Page 1/1.
    view = Paginator(paginate_lines([]), author_id=1)
    assert len(view.embeds) == 1
    view._sync()
    assert view.first_page.disabled is True
    assert view.prev_page.disabled is True
    assert view.next_page.disabled is True
    assert view.last_page.disabled is True
    assert view.embeds[0].footer.text == "Page 1/1"


def test_empty_embeds_falls_back_to_placeholder():
    # Constructing with an empty iterable must still yield a usable page.
    view = Paginator([], author_id=1)
    assert len(view.embeds) == 1
    assert view.embeds[0].description == "Nothing to show."


def test_paginator_does_not_shadow_view_internal_refresh():
    # Guards the prod regression where a View subclass method named ``_refresh``
    # shadowed discord.py's internal ``View._refresh(self, components)`` and
    # crashed on MESSAGE_UPDATE. Neither Paginator nor its AuthorView base may
    # define ``_refresh`` themselves.
    from tools.views import AuthorView

    assert "_refresh" not in vars(Paginator)
    assert "_refresh" not in vars(AuthorView)
