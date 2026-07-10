"""Unit tests for the list-first autocomplete cache in ``cogs/anilist/account.py``.

The three targets are pure, side-effect-free helpers (no network, DB, Discord,
or Lavalink): a bounded time-keyed map (``_list_cache_get`` / ``_list_cache_put``
over the module-level ``_list_cache`` dict) and the ``_autocomplete_label``
choice-label formatter. They take ``now`` as an explicit argument (the callers
pass ``time.monotonic()``), so the TTL window is exercised by passing timestamps
directly - no monkeypatching of the clock is needed.

Assertions were written against the real implementation, not the docstrings:
notably ``_autocomplete_label`` does NOT truncate to 100 chars itself - the
callers apply ``[:100]`` at the ``app_commands.Choice`` site - so the tests here
check the raw (untruncated) label and separately document the call-site clamp.
"""

import pytest

from cogs.anilist import account
from cogs.anilist.account import (
    _LIST_CACHE_SWEEP_AT,
    _LIST_CACHE_TTL,
    _autocomplete_label,
    _list_cache_get,
    _list_cache_put,
)


@pytest.fixture(autouse=True)
def _clear_list_cache():
    """Isolate every test from the shared module-level ``_list_cache`` dict."""

    account._list_cache.clear()
    yield
    account._list_cache.clear()


# ---------------------------------------------------------------------------
# _list_cache_put / _list_cache_get
# ---------------------------------------------------------------------------
def test_put_get_roundtrip_returns_stored_entries():
    entries = [({"id": 1}, "cowboy bebop"), ({"id": 2}, "trigun")]
    _list_cache_put(123, entries, now=100.0)

    # Same user id, within the TTL window -> the exact stored list back.
    got = _list_cache_get(123, now=100.0)
    assert got is entries


def test_get_within_ttl_is_a_hit():
    entries = [({"id": 7}, "naruto")]
    _list_cache_put(1, entries, now=0.0)

    # Just under the TTL boundary is still a hit.
    assert _list_cache_get(1, now=_LIST_CACHE_TTL - 0.001) is entries


def test_get_after_ttl_is_a_miss():
    entries = [({"id": 9}, "bleach")]
    _list_cache_put(1, entries, now=0.0)

    # At/after the TTL boundary the entry is stale: ``now - ts >= TTL``.
    assert _list_cache_get(1, now=_LIST_CACHE_TTL) is None
    assert _list_cache_get(1, now=_LIST_CACHE_TTL + 5.0) is None


def test_get_unknown_user_is_clean_miss():
    # No entry ever stored: a plain None, never a KeyError.
    assert _list_cache_get(999, now=0.0) is None


def test_put_overwrites_previous_entry_and_timestamp():
    _list_cache_put(1, [({"id": 1}, "old")], now=0.0)
    fresh = [({"id": 2}, "new")]
    _list_cache_put(1, fresh, now=1000.0)

    # The re-put refreshes the timestamp, so a probe near the new ``now`` hits.
    assert _list_cache_get(1, now=1000.0) is fresh


# ---------------------------------------------------------------------------
# Boundedness / sweep
# ---------------------------------------------------------------------------
def test_sweep_cap_constant_is_documented_value():
    # Guard the documented hard cap the sweep keys off of.
    assert _LIST_CACHE_SWEEP_AT == 500
    assert _LIST_CACHE_TTL == 60.0


def test_no_sweep_at_or_below_cap():
    # Filling exactly to the cap does not trigger the ``> cap`` sweep branch.
    for uid in range(_LIST_CACHE_SWEEP_AT):
        _list_cache_put(uid, [({"id": uid}, "x")], now=0.0)
    assert len(account._list_cache) == _LIST_CACHE_SWEEP_AT


def test_sweep_evicts_stale_rows_once_past_cap():
    # Seed the cap with stale rows at an old timestamp...
    for uid in range(_LIST_CACHE_SWEEP_AT):
        _list_cache_put(uid, [({"id": uid}, "x")], now=0.0)
    assert len(account._list_cache) == _LIST_CACHE_SWEEP_AT

    # ...then one more put far in the future crosses the cap and sweeps every
    # row older than ``now - TTL``, collapsing the map back to the fresh row.
    _list_cache_put(9001, [({"id": 9001}, "fresh")], now=10_000.0)
    assert len(account._list_cache) == 1
    assert _list_cache_get(9001, now=10_000.0) is not None
    assert _list_cache_get(0, now=10_000.0) is None


def test_sweep_keeps_fresh_rows_that_are_still_within_ttl():
    # A put past the cap only drops STALE rows; rows still inside the TTL window
    # survive even though the sweep ran.
    for uid in range(_LIST_CACHE_SWEEP_AT):
        _list_cache_put(uid, [({"id": uid}, "x")], now=0.0)
    # This fresh row (uid 0 re-put) shares the trigger timestamp, so it must
    # survive the sweep that the 501st insert kicks off.
    _list_cache_put(0, [({"id": 0}, "refreshed")], now=1000.0)
    _list_cache_put(9001, [({"id": 9001}, "fresh")], now=1000.0)

    # Only the two rows at ts=1000 remain; the ts=0 rows were swept.
    assert len(account._list_cache) == 2
    assert _list_cache_get(0, now=1000.0) is not None
    assert _list_cache_get(9001, now=1000.0) is not None


# ---------------------------------------------------------------------------
# _autocomplete_label
# ---------------------------------------------------------------------------
def test_autocomplete_label_typical_entry():
    media = {"type": "ANIME", "title": {"romaji": "Cowboy Bebop"}, "seasonYear": 1998}
    assert _autocomplete_label(media) == "[ANIME] Cowboy Bebop (1998)"


def test_autocomplete_label_manga_type_and_year_render():
    media = {"type": "MANGA", "title": {"romaji": "Berserk"}, "seasonYear": 1989}
    assert _autocomplete_label(media) == "[MANGA] Berserk (1989)"


def test_autocomplete_label_missing_optional_fields_no_crash():
    # Empty dict: every field falls back to its sentinel, no KeyError/TypeError.
    assert _autocomplete_label({}) == "[?] Unknown (?)"


def test_autocomplete_label_partial_fields_fall_back():
    # title present but romaji missing -> "Unknown"; type/year missing -> "?".
    media = {"title": {"english": "Attack on Titan"}}
    assert _autocomplete_label(media) == "[?] Unknown (?)"


def test_autocomplete_label_none_valued_fields_fall_back():
    # Explicit None values (not just absent keys) also hit the ``or`` fallbacks.
    media = {"type": None, "title": None, "seasonYear": None}
    assert _autocomplete_label(media) == "[?] Unknown (?)"


def test_autocomplete_label_does_not_truncate_but_callsite_clamp_fits_100():
    # The function itself returns the FULL label (truncation lives at the
    # ``app_commands.Choice`` call sites as ``[:100]``); assert both facts so the
    # 100-char Discord choice limit stays covered without duplicating call logic.
    long_title = "A" * 200
    media = {"type": "ANIME", "title": {"romaji": long_title}, "seasonYear": 2024}
    label = _autocomplete_label(media)
    assert len(label) > 100
    assert label.startswith("[ANIME] " + long_title)
    # The clamp the callers apply keeps the choice name within Discord's cap.
    assert len(label[:100]) == 100
