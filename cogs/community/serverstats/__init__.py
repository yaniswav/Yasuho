"""Purpose: package entry point - exposes the collectors cog to core's extension
discovery (a package whose __init__ defines ``setup`` is loaded whole).

Server statistics, aggregate-only: how many messages a channel saw on a UTC day,
how many members joined or left a guild that day, and what the guild's member
count was. Never any content, never any user id. Kept 90 days, pruned by the
collector itself.

Layout:
* buffer.py  - the pure bounded counters and the UTC-day arithmetic;
* queries.py - the three SQL statements (flush, snapshot, prune);
* cog.py     - the listeners and the single batched flush loop.

Reading the data back is a later lot's job; this package only writes.

Typography rule: ASCII '-' and '...' only.
"""

from .cog import ServerStats

__all__ = ("ServerStats", "setup")


async def setup(bot):
    await bot.add_cog(ServerStats(bot))
