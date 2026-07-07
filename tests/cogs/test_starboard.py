"""Unit tests for the Starboard cog's per-message lock (no bot/DB needed)."""

import asyncio

from cogs.config.starboard import Starboard


async def test_message_lock_serializes_same_message():
    sb = Starboard(bot=None)
    order = []

    async def worker(tag, hold):
        async with sb._message_lock(42):
            order.append(f"{tag}-enter")
            await asyncio.sleep(hold)
            order.append(f"{tag}-exit")

    a = asyncio.create_task(worker("A", 0.02))
    await asyncio.sleep(0.005)  # let A acquire the lock first
    b = asyncio.create_task(worker("B", 0.0))
    await asyncio.gather(a, b)

    # B must not interleave inside A's critical section.
    assert order == ["A-enter", "A-exit", "B-enter", "B-exit"]
    # the lock entry is pruned once nobody holds it.
    assert sb._locks == {}


async def test_message_lock_prunes_after_use():
    sb = Starboard(bot=None)
    async with sb._message_lock(1):
        assert 1 in sb._locks
    assert sb._locks == {}


async def test_message_lock_distinct_messages_do_not_block():
    sb = Starboard(bot=None)
    order = []

    async def worker(mid, tag):
        async with sb._message_lock(mid):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-exit")

    # Different message ids use different locks, so they run concurrently.
    await asyncio.gather(worker(1, "A"), worker(2, "B"))
    assert order[0].endswith("-enter") and order[1].endswith("-enter")
    assert sb._locks == {}
