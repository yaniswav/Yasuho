"""Calendar recurrence anchored to a planned local wall-clock datetime."""

from __future__ import annotations

import calendar
import datetime
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc
RECURRENCES = frozenset({"none", "daily", "weekly", "monthly"})


def _instant(local, zone):
    # fold=0 chooses the first repeated time. A nonexistent time moves forward
    # by the DST gap, preserving its minutes instead of losing an occurrence.
    return local.replace(tzinfo=zone, fold=0).astimezone(UTC)


def next_occurrence(local_time, timezone, recurrence, after):
    """Return the first planned occurrence strictly after an aware instant.

    local_time is a naive datetime: its date anchors the first occurrence,
    weekday and original month day. Fast-forward uses calendar arithmetic and
    at most four candidates, independent of the length of an outage.
    """
    if local_time.tzinfo is not None or after.utcoffset() is None:
        raise ValueError("Expected a naive local_time and an aware after")
    if recurrence not in RECURRENCES:
        raise ValueError("Invalid recurrence")
    zone = ZoneInfo(timezone)
    if recurrence == "none":
        instant = _instant(local_time, zone)
        return instant if instant > after else None

    today = after.astimezone(zone).date()
    if recurrence == "monthly":
        steps = max(0, (today.year - local_time.year) * 12 + today.month - local_time.month)
    else:
        interval = 7 if recurrence == "weekly" else 1
        steps = max(0, (today - local_time.date()).days // interval)
    # A gap can shift the previous local date's occurrence across midnight.
    steps = max(0, steps - 1)
    for offset in range(4):
        step = steps + offset
        if recurrence == "monthly":
            year, month = divmod(local_time.year * 12 + local_time.month - 1 + step, 12)
            month += 1
            day = min(local_time.day, calendar.monthrange(year, month)[1])
            candidate = local_time.replace(year=year, month=month, day=day)
        else:
            candidate = local_time + datetime.timedelta(days=step * interval)
        instant = _instant(candidate, zone)
        if instant > after:
            return instant
    raise ValueError("Cannot resolve next occurrence")
