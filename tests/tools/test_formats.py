"""Unit tests for tools/formats.py.

Pure formatting helpers: no network, database, Discord, or Lavalink involved.
Each test pins the exact string output of the real implementation so a
behavioural change (e.g. pluralisation rule, join separator, timestamp style)
fails loudly rather than silently drifting.
"""

import datetime

from tools import formats

# ---------------------------------------------------------------------------
# plural.__format__
# ---------------------------------------------------------------------------


def test_plural_singular_when_one():
    # abs(value) == 1 -> singular branch, no trailing 's'.
    assert format(formats.plural(1), "thing") == "1 thing"


def test_plural_default_plural_appends_s():
    # abs(value) != 1 and no '|' -> singular + 's'.
    assert format(formats.plural(2), "thing") == "2 things"


def test_plural_zero_is_plural():
    # 0 is not 1, so it takes the plural form.
    assert format(formats.plural(0), "thing") == "0 things"


def test_plural_negative_one_is_singular():
    # abs(-1) == 1 -> singular; the raw (signed) value is still printed.
    assert format(formats.plural(-1), "thing") == "-1 thing"


def test_plural_negative_many_is_plural():
    assert format(formats.plural(-3), "thing") == "-3 things"


def test_plural_custom_singular_plural_uses_singular():
    # Explicit "singular|plural": value 1 picks the left side.
    assert format(formats.plural(1), "entry|entries") == "1 entry"


def test_plural_custom_singular_plural_uses_plural():
    # Explicit "singular|plural": value != 1 picks the right side.
    assert format(formats.plural(3), "entry|entries") == "3 entries"


def test_plural_via_format_string_interpolation():
    # Exercised the way callers actually use it: inside a format string.
    assert "{:entry|entries}".format(formats.plural(2)) == "2 entries"


# ---------------------------------------------------------------------------
# human_join
# ---------------------------------------------------------------------------


def test_human_join_empty():
    assert formats.human_join([]) == ""


def test_human_join_single():
    assert formats.human_join(["apple"]) == "apple"


def test_human_join_two_uses_final_word():
    # Two items are joined only by the final word (default 'or'), no delimiter.
    assert formats.human_join(["apple", "banana"]) == "apple or banana"


def test_human_join_three_uses_delim_then_final():
    # 3+ items: delimiter between all but the last, final word before the last.
    assert formats.human_join(["a", "b", "c"]) == "a, b or c"


def test_human_join_four():
    assert formats.human_join(["a", "b", "c", "d"]) == "a, b, c or d"


def test_human_join_custom_final_word():
    # The final word replaces 'or'; note there is no Oxford comma before it.
    assert formats.human_join(["a", "b", "c"], final="and") == "a, b and c"


def test_human_join_custom_final_word_two_items():
    assert formats.human_join(["a", "b"], final="and") == "a and b"


def test_human_join_custom_delim():
    assert formats.human_join(["a", "b", "c"], delim=" | ") == "a | b or c"


# ---------------------------------------------------------------------------
# format_dt
# ---------------------------------------------------------------------------


def _utc_ts(*args):
    return int(datetime.datetime(*args, tzinfo=datetime.timezone.utc).timestamp())


def test_format_dt_naive_treated_as_utc_no_style():
    dt = datetime.datetime(2021, 1, 1, 0, 0, 0)  # naive
    expected = _utc_ts(2021, 1, 1)
    assert formats.format_dt(dt) == f"<t:{expected}>"


def test_format_dt_naive_treated_as_utc_with_style():
    dt = datetime.datetime(2021, 1, 1, 0, 0, 0)  # naive
    expected = _utc_ts(2021, 1, 1)
    assert formats.format_dt(dt, "R") == f"<t:{expected}:R>"


def test_format_dt_naive_matches_explicit_utc():
    # A naive datetime must produce the same timestamp as the same wall-clock
    # value tagged UTC (i.e. it is NOT interpreted in the local zone).
    naive = datetime.datetime(2022, 6, 15, 12, 30, 45)
    aware = naive.replace(tzinfo=datetime.timezone.utc)
    assert formats.format_dt(naive) == formats.format_dt(aware)


def test_format_dt_respects_existing_tzinfo():
    # Aware datetime keeps its own offset; +2h shifts the epoch back by 7200s.
    tz = datetime.timezone(datetime.timedelta(hours=2))
    dt = datetime.datetime(2021, 1, 1, 2, 0, 0, tzinfo=tz)
    expected = _utc_ts(2021, 1, 1)  # 02:00+02:00 == 00:00 UTC
    assert formats.format_dt(dt) == f"<t:{expected}>"


def test_format_dt_style_is_appended_verbatim():
    dt = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    ts = int(dt.timestamp())
    assert formats.format_dt(dt, "F") == f"<t:{ts}:F>"


# ---------------------------------------------------------------------------
# random_colour
# ---------------------------------------------------------------------------


def test_random_colour_within_full_rgb_range():
    for _ in range(2000):
        value = formats.random_colour()
        assert isinstance(value, int)
        assert 0x000000 <= value <= 0xFFFFFF


def test_random_colour_uses_full_bounds(monkeypatch):
    # Verify the helper asks randint for the entire RGB range, and passes the
    # result through unchanged (boundary values are reachable).
    captured = {}

    def fake_randint(lo, hi):
        captured["lo"] = lo
        captured["hi"] = hi
        return hi

    monkeypatch.setattr(formats.random, "randint", fake_randint)
    result = formats.random_colour()
    assert captured == {"lo": 0x000000, "hi": 0xFFFFFF}
    assert result == 0xFFFFFF


def test_random_colour_can_return_minimum(monkeypatch):
    monkeypatch.setattr(formats.random, "randint", lambda lo, hi: lo)
    assert formats.random_colour() == 0x000000
