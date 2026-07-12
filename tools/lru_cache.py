"""A tiny bounded LRU cache (a dict with a ceiling), the size-cap house pattern.

tools/settings.py keeps per-guild and per-user JSONB blobs in-process so hot
paths (leveling's on_message, i18n's locale lookup) do not hit Postgres on every
event. A plain dict there grew one entry per id ever seen and never shrank: guild
blobs are bounded by guild count, but USER blobs are unbounded (one per user who
ever changed a preference, ran a command that resolves a locale, or triggered a
levelup_announce read), so the map could grow for the whole life of the process.

This is the size-cap counterpart to tools.cooldowns (which bounds a debounce map
by TIME): a genuine cache whose useful eviction policy is least-recently-used,
not oldest-touched. Eviction is always safe because tools.settings re-reads an
evicted id from the DB on the next access - the cache is a speed layer, never the
source of truth. Pure and dependency-free, so it is trivially unit-tested.
"""

from __future__ import annotations

from collections import OrderedDict


class BoundedLRU:
    """An OrderedDict-backed cache that evicts the least-recently-used key.

    ``capacity`` is the maximum number of live entries. Every value read
    (``get``/``__getitem__``) and every write (``__setitem__``/``setdefault``)
    marks the key most-recent; once a write pushes the size past ``capacity`` the
    oldest entry is dropped. ``__contains__`` is a peek and does NOT change
    recency, so a bare membership test never keeps a stale entry alive. A dropped
    key simply re-reads from the DB on its next access.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._data: OrderedDict = OrderedDict()

    def __contains__(self, key) -> bool:
        # A peek: membership must not count as a use, or `if key in cache` in a
        # caller's read path would resurrect the entry's recency for free.
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key):
        value = self._data[key]  # raises KeyError like a dict on a miss
        self._data.move_to_end(key)
        return value

    def get(self, key, default=None):
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return default

    def __setitem__(self, key, value) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        self._evict()

    def setdefault(self, key, default):
        """Return ``key``'s value, inserting ``default`` if it is absent.

        Mirrors ``dict.setdefault`` (never overwrites an existing value) so the
        cold-cache concurrency guard in tools.settings keeps a value a concurrent
        writer stored while a slow fetch was in flight. Both the hit and the fresh
        insert count as a use.
        """
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        self._data[key] = default
        self._evict()
        return default

    def clear(self) -> None:
        self._data.clear()

    def _evict(self) -> None:
        # A single write adds at most one entry, so one pop restores the ceiling;
        # the while loop also copes if ``capacity`` were ever lowered at runtime.
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)
