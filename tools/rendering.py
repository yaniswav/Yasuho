"""Shared concurrency ceiling for Pillow and other blocking image renders.

Every blocking image job in the bot goes through :func:`run_image_job`, which
holds a bot-wide semaphore so Pillow can never take over the default executor
(shared with every other ``run_in_executor`` caller). The ceiling is what makes
the fan-out safe; the acquisition TIMEOUT is what keeps it from becoming a queue
nobody can leave - see the function's own note.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import functools

DEFAULT_IMAGE_CONCURRENCY = 2

# How long (seconds) a job waits for one of the two slots before giving up.
#
# The ceiling is only two slots wide and it is shared by everything: sub-second
# rank/welcome/level cards and serverstats charts on interactive paths, but also
# avatar recompression and the multi-megabyte ZIP archives the personal-data
# export builds. Without a bound, a card render queued behind a couple of those
# archives waits for as long as they take - on a path whose caller is an
# interaction with its own, much shorter, deadline - and the user sees nothing at
# all rather than the fallback the caller already knows how to send.
#
# A few seconds is the right order of magnitude: with two slots and sub-second
# renders, a wait past this is not contention, it is a jammed pool, and every
# caller in the tree either catches the failure itself or hands it to a fallback.
#
# The default is a DEFAULT, and the personal-data export opts out of it with
# `timeout=None` (both of its call sites: cogs/community/usersettings.py and
# cogs/system/dashboard_user_actions.py). It is the exception the rule needs: it
# claims a once-an-hour cooldown slot BEFORE the render and does not release it
# on failure, so timing out there does not degrade to a fallback - it charges
# the user an hour of a data-rights path for a queue they did not cause. Every
# other caller loses at most one image.
DEFAULT_ACQUIRE_TIMEOUT = 5.0


async def run_image_job(bot, function, *args, timeout=DEFAULT_ACQUIRE_TIMEOUT, **kwargs):
    """Run one blocking image job without saturating the default executor.

    ``timeout`` bounds the WAIT FOR A SLOT, not the render. Pass ``None`` to wait
    forever (a caller that must never fail), or a longer value for a job worth
    queueing for. On expiry it raises :exc:`TimeoutError` (which is what
    :exc:`asyncio.TimeoutError` is on 3.11+, so an ``except Exception`` catches
    it, and it is emphatically NOT a ``CancelledError`` the caller would have to
    re-raise) and the caller falls back.

    Why the timeout stops at the acquire and does not cover the render: a
    ``run_in_executor`` future cannot cancel the thread already running it, so
    "timing out" mid-render would free the caller while the work continues -
    except that unwinding the semaphore would ALSO release the slot the thread is
    still using, letting a third and fourth Pillow job in and quietly breaking
    the very ceiling this module exists to enforce. Bounding the queue wait is
    what actually addresses saturation: if a slot is free the job starts at once,
    and if none is, that is precisely the case worth abandoning. A caller that
    additionally wants its own render bounded wraps this call in
    ``asyncio.wait_for`` (cogs/community/serverstats/cog.py does).

    ``timeout`` is consumed here and never forwarded, so a target function with a
    ``timeout`` parameter of its own must be pre-bound (``functools.partial``).
    No caller does today.

    Who passes what: every render caller takes the default; only the two
    personal-data export sites pass ``None`` (see DEFAULT_ACQUIRE_TIMEOUT's note
    for why the export is the one job worth queueing for indefinitely).
    """
    semaphore = getattr(bot, "image_render_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(DEFAULT_IMAGE_CONCURRENCY)
        bot.image_render_semaphore = semaphore
    callback = functools.partial(function, *args, **kwargs)
    if timeout is None:
        await semaphore.acquire()
    else:
        await asyncio.wait_for(semaphore.acquire(), timeout)
    try:
        return await bot.loop.run_in_executor(None, callback)
    finally:
        semaphore.release()
