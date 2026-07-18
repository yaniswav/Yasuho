"""Shared concurrency ceiling for Pillow and other blocking image renders."""

from __future__ import annotations

import asyncio
import functools

DEFAULT_IMAGE_CONCURRENCY = 2


async def run_image_job(bot, function, *args, **kwargs):
    """Run one blocking image job without saturating the default executor."""
    semaphore = getattr(bot, "image_render_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(DEFAULT_IMAGE_CONCURRENCY)
        bot.image_render_semaphore = semaphore
    callback = functools.partial(function, *args, **kwargs)
    async with semaphore:
        return await bot.loop.run_in_executor(None, callback)
