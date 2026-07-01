"""Unit tests for ``tools/time.py``.

Every case pins an explicit ``now=`` so results are deterministic and never
depend on the wall clock. No network, database, Discord, or Lavalink is touched;
the time helpers are pure and operate on the values passed in.

Covered:
- ``ShortTime`` for ``2h`` / ``10m`` (dt == now + delta), invalid input
  (``commands.BadArgument``) and the ``<t:...>`` discord timestamp path.
- ``human_timedelta`` for a known future delta, a known past delta, the
  ``brief=True`` form, the ``suffix=False`` form and the empty ("now") case.
- ``FutureTime`` rejecting a time in the past.
- ``Time`` falling back from ``ShortTime`` to ``HumanTime``.
"""

import datetime

import pytest
from discord.ext import commands

from tools.time import FutureTime, HumanTime, ShortTime, Time, human_timedelta

UTC = datetime.timezone.utc
# A fixed reference instant, mid-month/mid-day so relativedelta decomposition
# never straddles a month boundary for the small deltas used below.
NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ShortTime
# ---------------------------------------------------------------------------


def test_shorttime_hours():
    st = ShortTime("2h", now=NOW)
    assert st.dt == NOW + datetime.timedelta(hours=2)


def test_shorttime_minutes():
    st = ShortTime("10m", now=NOW)
    assert st.dt == NOW + datetime.timedelta(minutes=10)


def test_shorttime_invalid_raises_bad_argument():
    with pytest.raises(commands.BadArgument):
        ShortTime("notatime", now=NOW)


def test_shorttime_discord_timestamp():
    # <t:...> path: the short-time regex fails to match, so the discord
    # timestamp fallback parses the unix seconds directly (now= is ignored).
    st = ShortTime("<t:1700000000>", now=NOW)
    assert st.dt == datetime.datetime.fromtimestamp(1700000000, tz=UTC)


def test_shorttime_discord_timestamp_with_style():
    # A trailing style flag (e.g. ``:R``) is accepted and stripped.
    st = ShortTime("<t:1700000000:R>", now=NOW)
    assert st.dt == datetime.datetime.fromtimestamp(1700000000, tz=UTC)


def test_shorttime_non_utc_tzinfo_keeps_instant():
    # A non-UTC tzinfo shifts the wall-clock representation but not the instant.
    tz = datetime.timezone(datetime.timedelta(hours=2))
    st = ShortTime("2h", now=NOW, tzinfo=tz)
    assert st.dt == NOW + datetime.timedelta(hours=2)
    assert st.dt.utcoffset() == datetime.timedelta(hours=2)


# ---------------------------------------------------------------------------
# human_timedelta
# ---------------------------------------------------------------------------


def test_human_timedelta_future():
    dt = NOW + datetime.timedelta(hours=2, minutes=30)
    assert human_timedelta(dt, source=NOW) == "2 hours and 30 minutes"


def test_human_timedelta_future_single_unit():
    dt = NOW + datetime.timedelta(hours=2)
    assert human_timedelta(dt, source=NOW) == "2 hours"


def test_human_timedelta_past_has_ago_suffix():
    dt = NOW - datetime.timedelta(days=3)
    assert human_timedelta(dt, source=NOW) == "3 days ago"


def test_human_timedelta_brief_future():
    dt = NOW + datetime.timedelta(hours=2, minutes=30)
    assert human_timedelta(dt, source=NOW, brief=True) == "2h 30m"


def test_human_timedelta_brief_past():
    dt = NOW - datetime.timedelta(days=3)
    assert human_timedelta(dt, source=NOW, brief=True) == "3d ago"


def test_human_timedelta_past_without_suffix():
    dt = NOW - datetime.timedelta(days=3)
    assert human_timedelta(dt, source=NOW, suffix=False) == "3 days"


def test_human_timedelta_zero_delta_is_now():
    assert human_timedelta(NOW, source=NOW) == "now"


def test_human_timedelta_naive_dt_treated_as_utc():
    # A naive dt is assumed UTC; the naive source below is also assumed UTC.
    dt = NOW.replace(tzinfo=None) + datetime.timedelta(hours=2)
    source = NOW.replace(tzinfo=None)
    assert human_timedelta(dt, source=source) == "2 hours"


# ---------------------------------------------------------------------------
# Time / HumanTime / FutureTime
# ---------------------------------------------------------------------------


def test_time_uses_shorttime_when_possible():
    # A short-time expression is parsed by ShortTime and never marked past.
    t = Time("2h", now=NOW)
    assert t.dt == NOW + datetime.timedelta(hours=2)
    assert t._past is False


def test_time_falls_back_to_humantime():
    # "tomorrow" is not a ShortTime, so Time must fall back to HumanTime.
    # Proven by: ShortTime rejects it, yet Time produces the HumanTime result.
    with pytest.raises(commands.BadArgument):
        ShortTime("tomorrow", now=NOW)

    t = Time("tomorrow", now=NOW)
    human = HumanTime("tomorrow", now=NOW)
    assert t.dt == human.dt
    assert t.dt == NOW + datetime.timedelta(days=1)


def test_future_time_rejects_past():
    with pytest.raises(commands.BadArgument):
        FutureTime("yesterday", now=NOW)


def test_future_time_accepts_future():
    ft = FutureTime("tomorrow", now=NOW)
    assert ft.dt == NOW + datetime.timedelta(days=1)
    assert ft._past is False
