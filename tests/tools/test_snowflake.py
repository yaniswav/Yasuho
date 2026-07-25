"""Unit tests for tools.snowflake - the tolerant id coercion at the JSONB seams.

The dashboard (a Node process) serialises snowflakes as STRINGS, so every id
read out of a guild_settings blob must survive both spellings; and a bool must
NEVER pass as an id (``True`` is an ``int`` in Python, the classic trap).
"""

import pytest

from tools.snowflake import coerce_id, coerce_ids


# ---------------------------------------------------------------------------
# coerce_id: accepted spellings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, expected",
    [
        (123, 123),
        ("123", 123),
        ("  123  ", 123),  # whitespace from a hand-edited blob
        (877293049194057728, 877293049194057728),
        ("877293049194057728", 877293049194057728),  # beyond 2^53: exact
    ],
)
def test_accepted_ids(value, expected):
    assert coerce_id(value) == expected


def test_string_and_int_agree():
    """The whole point: both spellings resolve to the SAME id."""
    assert coerce_id("877293049194057728") == coerce_id(877293049194057728)


# ---------------------------------------------------------------------------
# coerce_id: refused values
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "abc",
        "12a",
        "12.0",
        "-5",
        -5,
        0,
        "0",
        12.0,
        [],
        {},
        object(),
        "١٢٣",  # Arabic-Indic digits: isdigit() but not ours
        "²",  # superscript two: isdigit() True, int() would raise
    ],
)
def test_refused_values(value):
    assert coerce_id(value) is None


@pytest.mark.parametrize("value", [True, False])
def test_bool_is_refused(value):
    """bool is an int in Python: a stray toggle must not become id 1."""
    assert coerce_id(value) is None


def test_no_raise_on_weird_string():
    """A digit-looking non-ASCII string must return None, never raise."""
    assert coerce_id("²") is None


# ---------------------------------------------------------------------------
# coerce_ids
# ---------------------------------------------------------------------------
def test_coerce_ids_mixed_list_keeps_order_and_drops_junk():
    assert coerce_ids(["1", 2, None, True, "x", "3"]) == [1, 2, 3]


def test_coerce_ids_all_strings_matches_all_ints():
    assert coerce_ids(["10", "20"]) == coerce_ids([10, 20]) == [10, 20]


@pytest.mark.parametrize("value", [None, "123", 123, {}, object()])
def test_coerce_ids_rejects_non_sequences(value):
    """A bare string must not iterate character by character."""
    assert coerce_ids(value) == []


def test_coerce_ids_empty():
    assert coerce_ids([]) == []
