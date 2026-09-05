"""Run the whole suite as if it were N days from now.

WHY. A test that writes a date down and hands it to code reading the real clock
passes for weeks, then fails on a day nobody touched it. Two did exactly that on
2026-08-29; a third was found by this harness, sixteen days before it would have
fired. Push and pull_request runs cannot see that class - the break arrives with
the CALENDAR, not with a commit.

HOW TO USE IT::

    YASUHO_TIME_TRAVEL_DAYS=400 python -m pytest -q -p tests.timetravel

FREEZING STARTS BEFORE COLLECTION, and that is the whole design. A module-level
``TODAY = date.today()`` is evaluated at IMPORT, so a per-test fixture leaves the
module anchored in the present while the code under test sees the future - a
mismatch that CANNOT happen in real life. The first version of this harness did
exactly that and reported two false bombs; freezing in ``pytest_configure``
reported one, and that one was real.

``tick=True`` keeps the clock MOVING from the frozen point. A hard freeze stops
``time.monotonic`` too, which asyncio schedules on.
"""

import datetime
import os

OFFSET_ENV = "YASUHO_TIME_TRAVEL_DAYS"

_freezer = None


def pytest_configure(config):
    offset = os.environ.get(OFFSET_ENV)
    if not offset:
        return
    from freezegun import freeze_time

    global _freezer
    target = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=int(offset)
    )
    _freezer = freeze_time(target, tick=True)
    _freezer.start()
    config.addinivalue_line(
        "markers", "timetravel: suite running %s days ahead" % offset
    )


def pytest_report_header(config):
    offset = os.environ.get(OFFSET_ENV)
    if offset:
        return "time travel: running as if it were %s days from now" % offset
    return None


def pytest_unconfigure(config):
    global _freezer
    if _freezer is not None:
        _freezer.stop()
        _freezer = None
