"""Unit tests for :mod:`tools.premium_archive` (M4a-1: the lazy archival
helper - .claude/plans/monetisation/4-plan-retenu.md).

No I/O, no cogs: :func:`tools.premium_archive.classify` is pure, exercised
against plain dicts (mapping access) and ``types.SimpleNamespace`` (attribute
access) - the two shapes :func:`tools.premium_archive._field` is written to
read, mirroring tools.premium's own ``_get``.
"""

from __future__ import annotations

import types

from tools import premium_archive as pa


def _res(id, created_at, kept=False):
    return {"id": id, "created_at": created_at, "kept": kept}


# ---------------------------------------------------------------------------
# classify: ordering (kept first, then oldest, ties by id)
# ---------------------------------------------------------------------------


def test_oldest_first_under_the_limit():
    resources = [_res("c", 3), _res("a", 1), _res("b", 2)]
    result = pa.classify(resources, limit=2)
    assert result.active_ids == frozenset({"a", "b"})
    assert result.archived_ids == frozenset({"c"})


def test_kept_wins_a_free_slot_over_merely_older():
    # "b" is kept, so it beats the strictly-older "a" for the one free slot.
    resources = [_res("a", 1), _res("b", 2, kept=True), _res("c", 3)]
    result = pa.classify(resources, limit=1)
    assert result.active_ids == frozenset({"b"})
    assert result.archived_ids == frozenset({"a", "c"})


def test_exact_limit_keeps_everything_active():
    resources = [_res("a", 1), _res("b", 2), _res("c", 3)]
    result = pa.classify(resources, limit=3)
    assert result.active_ids == frozenset({"a", "b", "c"})
    assert result.archived_ids == frozenset()


def test_limit_above_count_keeps_everything_active():
    resources = [_res("a", 1), _res("b", 2)]
    result = pa.classify(resources, limit=100)
    assert result.active_ids == frozenset({"a", "b"})
    assert result.archived_ids == frozenset()


def test_empty_resources():
    result = pa.classify([], limit=5)
    assert result.active_ids == frozenset()
    assert result.archived_ids == frozenset()


def test_limit_zero_archives_everything():
    resources = [_res("a", 1), _res("b", 2)]
    result = pa.classify(resources, limit=0)
    assert result.active_ids == frozenset()
    assert result.archived_ids == frozenset({"a", "b"})


def test_negative_limit_archives_everything():
    resources = [_res("a", 1)]
    result = pa.classify(resources, limit=-5)
    assert result.active_ids == frozenset()
    assert result.archived_ids == frozenset({"a"})


def test_ties_ordered_by_id():
    # Same created_at: the lower id is treated as "earlier" for a deterministic
    # tiebreak, regardless of input order.
    resources = [_res(5, 1), _res(1, 1), _res(3, 1)]
    result = pa.classify(resources, limit=2)
    assert result.active_ids == frozenset({1, 3})
    assert result.archived_ids == frozenset({5})


def test_kept_over_the_limit_is_still_archived():
    # "kept" only wins a free slot over something merely older - it does not
    # override the limit itself when MORE resources are kept than fit.
    resources = [
        _res("a", 1, kept=True),
        _res("b", 2, kept=True),
        _res("c", 3, kept=True),
    ]
    result = pa.classify(resources, limit=2)
    assert result.active_ids == frozenset({"a", "b"})
    assert result.archived_ids == frozenset({"c"})


def test_input_order_does_not_matter():
    forward = [_res("a", 1), _res("b", 2), _res("c", 3)]
    backward = list(reversed(forward))
    assert pa.classify(forward, limit=2).active_ids == pa.classify(
        backward, limit=2
    ).active_ids


# ---------------------------------------------------------------------------
# attribute-access resources (SimpleNamespace, no "kept" field at all)
# ---------------------------------------------------------------------------


def test_attribute_access_resources_with_no_kept_field():
    resources = [
        types.SimpleNamespace(id="a", created_at=1),
        types.SimpleNamespace(id="b", created_at=2),
    ]
    result = pa.classify(resources, limit=1)
    assert result.active_ids == frozenset({"a"})
    assert result.archived_ids == frozenset({"b"})


# ---------------------------------------------------------------------------
# ArchivalResult helpers
# ---------------------------------------------------------------------------


def test_is_active_and_is_archived():
    result = pa.classify([_res("a", 1), _res("b", 2)], limit=1)
    assert result.is_active("a") is True
    assert result.is_archived("a") is False
    assert result.is_active("b") is False
    assert result.is_archived("b") is True


def test_unseen_id_is_neither_active_nor_archived():
    # Fail-closed default: an id this call never classified reads as not-active
    # (never mistaken for "active") AND not-archived (never mistaken for an
    # excess this verdict actually found).
    result = pa.classify([_res("a", 1)], limit=1)
    assert result.is_active("ghost") is False
    assert result.is_archived("ghost") is False


# ---------------------------------------------------------------------------
# Negative control: the ordering rule must actually be load-bearing.
# ---------------------------------------------------------------------------


def test_negative_control_newest_first_would_fail():
    # A classify that (wrongly) kept the NEWEST resources active instead of
    # the oldest would archive "a" and keep "c" - the opposite of what this
    # module promises. This pins the real orientation so a future refactor
    # that silently flips "oldest first" to "newest first" is caught here.
    resources = [_res("a", 1), _res("b", 2), _res("c", 3)]
    result = pa.classify(resources, limit=2)
    assert "c" not in result.active_ids
    assert "a" in result.active_ids
