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

TWO PROPERTIES THE EVICTION HAS TO HAVE, both of which the first version missed:

* IT MUST NOT EVICT A KEY THAT IS STILL COOLING. One map can serve per-caller
  windows (leveling debounces under a per-GUILD cooldown, the profile connector
  scheduler under a per-pair backoff), and a sweep that judged every entry by
  the INSTANCE window silently dropped keys whose real window was longer -
  which reads as "cooldown over" and lets the debounced event through early.
  Each entry therefore carries its OWN window, and the sweep uses that. Such a
  caller must name that window on :meth:`Cooldowns.touch` as well as on
  :meth:`Cooldowns.is_active`, or the entry is SEATED under the instance
  default and a sweep landing before the next check evicts it anyway; the
  widening on the check is only a safety net for one that forgets.
* IT MUST BE AMORTISED. Rebuilding the whole dict on every touch past the cap is
  O(n) per event on a hot path, so a map sitting just above its cap did a full
  rebuild per message forever. The sweep now runs only when the map has GROWN
  past a threshold that is re-armed to twice the surviving size, so the rebuild
  cost is O(1) amortised per touch and the map stays bounded by roughly twice
  the number of genuinely-active keys.
"""

from __future__ import annotations

import time


class Cooldowns:
    """Track when each key was last used and answer whether it is still cooling.

    ``seconds`` is the default window length, used for any entry whose toucher
    did not name one. ``sweep_at`` is the size the map must exceed before the
    first sweep runs; after each sweep the next one is armed at twice the
    surviving size (never below ``sweep_at``), which is what makes the pruning
    amortised rather than per-touch.
    """

    def __init__(self, seconds: float, *, sweep_at: int = 2000) -> None:
        self.seconds = seconds
        self._sweep_at = sweep_at
        # key -> (last touch, the window that entry is cooling for)
        self._seen: dict = {}
        # The size the map must EXCEED for the next sweep to run.
        self._sweep_when = sweep_at

    def is_active(
        self, key, *, now: float | None = None, seconds: float | None = None
    ) -> bool:
        """True while ``key`` was last touched within the cooldown window.

        ``seconds`` overrides the stored window for this one check, so a single
        map can debounce keys under per-caller windows (leveling reads a
        per-guild cooldown); it defaults to the window the entry was touched
        with, which is the instance ``seconds`` unless the toucher named one.
        A caller passing it here should pass the SAME value to :meth:`touch`,
        so the entry is seated under the window that governs it rather than
        relying on the widening below to repair it after the fact.

        An override LONGER than the entry's stored window widens that entry, so
        the sweep cannot evict a key this caller still considers cooling. It is
        one dict store, and only on the call that first widens the entry (after
        it, the stored window already matches), so the hot path pays nothing per
        event. A SHORTER override never narrows the entry: an over-kept key is
        harmless (it is re-touched or ages out), an under-kept one is a missed
        cooldown.
        """
        entry = self._seen.get(key)
        if entry is None:
            return False
        now = time.monotonic() if now is None else now
        last, window = entry
        if seconds is not None:
            if seconds > window:
                self._seen[key] = (last, seconds)
            window = seconds
        return (now - last) < window

    def touch(
        self, key, *, now: float | None = None, seconds: float | None = None
    ) -> None:
        """Record ``key`` as used now, sweeping stale entries past the size cap.

        ``seconds`` stores a per-entry window (defaulting to the instance one).
        A caller whose window is per-guild or per-user should pass the same
        value it passes to :meth:`is_active`, so the sweep judges the entry by
        the window that actually governs it.
        """
        now = time.monotonic() if now is None else now
        self._seen[key] = (now, self.seconds if seconds is None else seconds)
        if len(self._seen) > self._sweep_when:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        """Drop every entry past its OWN window, then re-arm the next sweep.

        The survivors are all still cooling, so the next sweep is armed at twice
        their number: the map has to double before another rebuild is worth
        doing, which spreads one O(n) pass over n touches. ``sweep_at`` remains
        the floor, so a small map is never swept more eagerly than it was.
        """
        self._seen = {
            key: entry
            for key, entry in self._seen.items()
            if (now - entry[0]) < entry[1]
        }
        self._sweep_when = max(self._sweep_at, 2 * len(self._seen))

    def __len__(self) -> int:
        return len(self._seen)
