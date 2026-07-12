"""A tiny per-key cooldown map that prunes itself.

Some hot paths (leveling XP gain, autoroom room creation) keyed a plain dict on
(guild_id, user_id) to debounce repeated events, but never evicted it, so the
dict grew for the whole lifetime of the process. This centralises that identical
"is this key still cooling down?" logic in one place, with lazy eviction so the
map can never grow without bound.

It is NOT a durable rate-limit contract, only an in-memory debounce: entries are
dropped once they age past the window, and a stale key simply reads as inactive.
Times default to time.monotonic() so a wall-clock change can never skew a window;
callers may inject ``now`` (used by the tests).
"""

from __future__ import annotations

import time


class Cooldowns:
    """Track when each key was last used and answer whether it is still cooling.

    ``seconds`` is the window length. ``sweep_at`` caps the map: once it holds
    more than that many keys, the next ``touch`` drops every entry already past
    the window, so the size stays bounded by the number of genuinely-active keys.
    """

    def __init__(self, seconds: float, *, sweep_at: int = 2000) -> None:
        self.seconds = seconds
        self._sweep_at = sweep_at
        self._seen: dict = {}

    def is_active(
        self, key, *, now: float | None = None, seconds: float | None = None
    ) -> bool:
        """True while ``key`` was last touched within the cooldown window.

        ``seconds`` overrides the instance window for this one check, so a single
        map can debounce keys under per-caller windows (leveling reads a per-guild
        cooldown); it defaults to the instance ``seconds`` when omitted.
        """
        now = time.monotonic() if now is None else now
        window = self.seconds if seconds is None else seconds
        last = self._seen.get(key)
        return last is not None and (now - last) < window

    def touch(self, key, *, now: float | None = None) -> None:
        """Record ``key`` as used now, sweeping stale entries past the size cap."""
        now = time.monotonic() if now is None else now
        self._seen[key] = now
        if len(self._seen) > self._sweep_at:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        cutoff = now - self.seconds
        self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}

    def __len__(self) -> int:
        return len(self._seen)
