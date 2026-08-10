"""The shared image semaphore must degrade to the caller's fallback, not block.

``tools.rendering.run_image_job`` is the ONE ceiling every blocking image job in
the bot passes through, and it is two slots wide. It now serves both sub-second
interactive renders (rank / welcome / level cards, serverstats charts) and the
multi-megabyte ZIP archives the personal-data export builds. With no bound on the
wait for a slot, a card queued behind a couple of those archives waited for as
long as they took, on a path whose caller has its own much shorter deadline - so
the user saw nothing at all instead of the fallback every one of those callers
already knows how to send.

``run_image_job`` therefore bounds the ACQUIRE. These tests pin the bound, the
default, and - just as important - that the ceiling itself is never weakened: a
slot is released on success, on failure, and never released by a wait that timed
out before it ever held one.
"""

import asyncio
import threading
import time

import pytest

from tools import rendering


class _Bot:
    """The two attributes run_image_job touches: the loop and the semaphore."""

    def __init__(self, semaphore=None):
        self.loop = asyncio.get_running_loop()
        if semaphore is not None:
            self.image_render_semaphore = semaphore


def _double(value):
    return value * 2


# ---------------------------------------------------------------------------
# The happy path is unchanged.
# ---------------------------------------------------------------------------


async def test_a_free_pool_runs_the_job_and_returns_its_value():
    bot = _Bot(asyncio.Semaphore(2))

    assert await rendering.run_image_job(bot, _double, 21) == 42


async def test_arguments_and_keywords_still_reach_the_function():
    bot = _Bot(asyncio.Semaphore(2))

    def _join(first, second, sep="-"):
        return f"{first}{sep}{second}"

    assert await rendering.run_image_job(bot, _join, "a", "b", sep="+") == "a+b"


async def test_a_bot_with_no_semaphore_gets_one_lazily():
    bot = _Bot()

    assert await rendering.run_image_job(bot, _double, 2) == 4
    assert isinstance(bot.image_render_semaphore, asyncio.Semaphore)


# ---------------------------------------------------------------------------
# The regression: a saturated pool must fail fast.
# ---------------------------------------------------------------------------


async def test_a_saturated_pool_times_out_instead_of_blocking():
    semaphore = asyncio.Semaphore(2)
    await semaphore.acquire()
    await semaphore.acquire()
    bot = _Bot(semaphore)

    with pytest.raises(asyncio.TimeoutError):
        await rendering.run_image_job(bot, _double, 1, timeout=0.01)


async def test_the_timeout_is_an_ordinary_exception_a_caller_can_catch():
    """Every call site guards with ``except Exception``; the failure must land there."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    bot = _Bot(semaphore)

    caught = None
    try:
        await rendering.run_image_job(bot, _double, 1, timeout=0.01)
    except Exception as exc:
        caught = exc

    assert isinstance(caught, asyncio.TimeoutError)


async def test_a_wait_that_timed_out_did_not_take_a_slot():
    """A cancelled acquire must not leave the ceiling one slot poorer (or richer)."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    bot = _Bot(semaphore)

    with pytest.raises(asyncio.TimeoutError):
        await rendering.run_image_job(bot, _double, 1, timeout=0.01)

    semaphore.release()
    # The one slot is back and usable: nothing was leaked by the abandoned wait,
    # and nothing was double-released either.
    assert await rendering.run_image_job(bot, _double, 3, timeout=0.5) == 6
    assert semaphore._value == 1


async def test_a_freed_slot_lets_a_queued_job_through_before_its_deadline():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    bot = _Bot(semaphore)

    queued = asyncio.ensure_future(
        rendering.run_image_job(bot, _double, 5, timeout=5.0)
    )
    await asyncio.sleep(0)
    assert not queued.done()

    semaphore.release()
    assert await queued == 10


async def test_timeout_none_waits_for_a_slot_however_long_it_takes():
    """The escape hatch for a caller that must never fail."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    bot = _Bot(semaphore)

    queued = asyncio.ensure_future(
        rendering.run_image_job(bot, _double, 8, timeout=None)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not queued.done()

    semaphore.release()
    assert await queued == 16


# ---------------------------------------------------------------------------
# The ceiling itself.
# ---------------------------------------------------------------------------


async def test_a_failing_job_still_releases_its_slot():
    semaphore = asyncio.Semaphore(1)
    bot = _Bot(semaphore)

    def _boom():
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError):
        await rendering.run_image_job(bot, _boom)

    assert semaphore._value == 1
    assert await rendering.run_image_job(bot, _double, 7) == 14


async def test_no_more_than_the_ceiling_ever_runs_at_once():
    """The whole point of the module: adding a timeout must not widen the pool."""
    semaphore = asyncio.Semaphore(2)
    bot = _Bot(semaphore)
    lock = threading.Lock()
    state = {"live": 0, "peak": 0}

    def _job():
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        time.sleep(0.02)
        with lock:
            state["live"] -= 1

    await asyncio.gather(
        *(rendering.run_image_job(bot, _job, timeout=10.0) for _ in range(6))
    )

    assert state["peak"] <= 2
    assert semaphore._value == 2


# ---------------------------------------------------------------------------
# The default is a bound, not an afterthought.
# ---------------------------------------------------------------------------


def test_the_default_bounds_every_caller_that_asks_for_nothing():
    """The render call sites pass no ``timeout``, so the default protects them all."""
    assert rendering.DEFAULT_ACQUIRE_TIMEOUT is not None
    assert 0 < rendering.DEFAULT_ACQUIRE_TIMEOUT <= 30


# ---------------------------------------------------------------------------
# ... and the one job the default must NOT bound.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    ("cogs.community.usersettings", "cogs.system.dashboard_user_actions"),
)
def test_the_personal_data_export_waits_for_its_slot(module_name):
    """Both export sites must opt out of the acquire bound.

    They CLAIM the once-an-hour export slot before the render and never release
    it (privacy.claim_export_slot: releasing is the abusable direction). So a
    TimeoutError there is not a missing image the caller can paper over - it is
    "Something went wrong building your data export" plus an hour before the
    user may ask again, for a queue somebody else caused. And the export is
    precisely the longest job in the two-slot pool and the one most likely to be
    waiting behind another of its own kind.
    """
    import ast
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_image_job"
    ]

    assert calls, f"{module_name} no longer builds the export through the ceiling"
    for call in calls:
        passed = {kw.arg: kw.value for kw in call.keywords}
        assert "timeout" in passed, (
            f"{module_name} takes the default acquire bound on the export path"
        )
        assert isinstance(passed["timeout"], ast.Constant)
        assert passed["timeout"].value is None
