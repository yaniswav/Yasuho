"""Pure, reusable quota primitives for the music chantier (lot P1).

Three upcoming lots - audio effects, lyrics and vote-skip - all need to say
"you have done this too often" or "the whole process is already at capacity"
without each reinventing the bookkeeping. This module owns that half and nothing
else: it is PURE infrastructure with no discord, no sonolink, no database, no
i18n. It never decides what a refusal looks like - a consumer reads a False and
formats (and translates) its own message. It never logs - a consumer reads
:meth:`stats` and decides when and how to record it.

Two shapes cover the need:

* :class:`SlidingWindowQuota` - a keyed rolling-window counter. "At most N hits
  per key per window", where a key is whatever the caller chooses (a user id, a
  guild id, or a ``(guild_id, user_id)`` tuple). Answers the four questions a
  rate-limiter is asked: would a hit fit (:meth:`check`), take a slot if one is
  free (:meth:`hit`), how many are left (:meth:`remaining`) and how long until
  one frees (:meth:`retry_after`).
* :class:`GlobalCeiling` - a process-wide gauge of concurrent holders with no
  time component: acquire a slot, release it, ask how many are held. Models a
  hard cap on simultaneously-active resources (filtered players, live synced-
  lyrics sessions) rather than a rate over time.

Both are bounded so neither can grow without bound on a busy process - the same
discipline as :class:`tools.cooldowns.Cooldowns` and
:class:`cogs.music.vibes.PendingVoiceWatches`: a size cap plus a lazy sweep of
dead entries, paid only when the structure grows past the cap.

Injectable clock: every time-aware method takes ``now`` (seconds, monotonic-
domain). The module never reads a clock behind the caller's back except through
the ``clock`` seam wired at construction (default :func:`time.monotonic`), so
tests drive time explicitly and a wall-clock jump can never skew a window.

:class:`QuotaRegistry` wires the named quotas the chantier actually uses, each
carrying its tuned constant (see the module-level tunables below), so a consumer
writes ``registry.effects_guild.hit(guild_id, now)`` and never repeats a limit.
"""

from __future__ import annotations

import time
import typing
from collections import deque

# ---------------------------------------------------------------------------
# Tunables. Every one of these is a knob, documented here so the values live in
# one place rather than scattered across the consuming cogs. Windows are seconds.
# ---------------------------------------------------------------------------

# Audio effects (lot P4): applying/altering a filter chain is cheap per call but
# spammable, and re-encoding churns the node. Cap per guild, not per user, so one
# member cannot exhaust a small guild's budget while still bounding node churn.
EFFECTS_GUILD_LIMIT = 6
EFFECTS_GUILD_WINDOW = 600.0  # 10 min

# Lyrics (lot P5): a fetch hits an external provider, so it is rate-limited on two
# axes - per user (stop one person hammering) and per guild (stop a whole guild
# hammering on the provider's behalf). The guild ceiling sits well above the per-
# user one so a busy-but-legitimate guild is not throttled by a single quota.
LYRICS_USER_LIMIT = 8
LYRICS_USER_WINDOW = 3600.0  # 1 h
LYRICS_GUILD_LIMIT = 60
LYRICS_GUILD_WINDOW = 3600.0  # 1 h

# Process-wide concurrency ceilings (no time axis). These bound how many of a
# resource can be live across the WHOLE process at once, independent of guild.
FILTERED_PLAYERS_CAP = 40  # simultaneously filtered players (lot P4)
SYNCED_LYRICS_CAP = 25  # simultaneously live synced-lyrics sessions (lot P5)

# Default size cap for a quota's key map before a sweep is attempted. A busy
# 1000-guild process tracks at most a few thousand live keys; the cap only has to
# sit above the genuinely-active working set so the sweep stays rare.
DEFAULT_MAX_KEYS = 4096

_Clock = typing.Callable[[], float]


class SlidingWindowQuota:
    """Keyed rolling-window counter: at most ``limit`` hits per key per window.

    Each key owns a deque of the absolute timestamps of its live hits, oldest at
    the left. A window is "rolling", not fixed: a slot frees exactly one window
    after the hit that took it, so there is no bucket boundary to burst across.
    Every read first drops timestamps that have aged past ``window_seconds`` for
    the key it touches, so a stale key costs nothing until it is asked about.

    Bounded. The key map holds at most ``max_keys`` live keys; when :meth:`hit`
    would grow it past that, a lazy sweep first drops every key whose window is
    entirely expired. A key that survives to be swept restarts fresh the next
    time it is used - so immediately after a sweep a key that was mid-window may
    briefly be allowed its full ``limit`` again (a documented, accepted over-
    allowance: the sweep only fires under many-thousand-key pressure, and the
    worst case is a handful of extra hits for a key that was already active).

    Pure and clock-injected: pass ``clock`` at construction to override
    :func:`time.monotonic`, and/or pass ``now`` to any method to pin the instant.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: _Clock = time.monotonic,
    ) -> None:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._hits: dict[typing.Any, deque[float]] = {}
        # Instrumentation counters (monotonic for the life of the process).
        self._hit_count = 0
        self._reject_count = 0

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    def _prune(self, key: typing.Any, now: float) -> deque[float]:
        """Return ``key``'s deque with expired timestamps dropped from the left.

        Removes the key entirely when it empties, so an idle key leaves no trace.
        """
        window = self._hits.get(key)
        if window is None:
            return deque()
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            # Drop the empty deque so idle keys do not linger in the map.
            self._hits.pop(key, None)
        return window

    def check(self, key: typing.Any, now: float | None = None) -> bool:
        """True if a hit would fit right now (does not consume a slot)."""
        now = self._now(now)
        return len(self._prune(key, now)) < self.limit

    def hit(self, key: typing.Any, now: float | None = None) -> bool:
        """Consume a slot for ``key`` and return True; return False when over.

        A rejected call records nothing (it does not extend the window) and only
        bumps the rejection counter. An accepted call may trigger a lazy sweep
        when the key map has grown past ``max_keys``.
        """
        now = self._now(now)
        window = self._prune(key, now)
        if len(window) >= self.limit:
            self._reject_count += 1
            return False
        if window:
            window.append(now)
        else:
            # First live hit for this key: (re)create its deque, then maybe sweep.
            self._hits[key] = deque((now,))
            if len(self._hits) > self._max_keys:
                self._sweep(now)
        self._hit_count += 1
        return True

    def remaining(self, key: typing.Any, now: float | None = None) -> int:
        """How many more hits ``key`` may take in the current window."""
        now = self._now(now)
        return self.limit - len(self._prune(key, now))

    def retry_after(self, key: typing.Any, now: float | None = None) -> float:
        """Seconds until ``key`` frees a slot; ``0.0`` when one is free now.

        When the key is at its limit, the next slot frees exactly one window
        after its oldest live hit. The result is never negative.
        """
        now = self._now(now)
        window = self._prune(key, now)
        if len(window) < self.limit:
            return 0.0
        # At the limit: the oldest hit ages out one window after it landed.
        return max(0.0, window[0] + self.window_seconds - now)

    def _sweep(self, now: float) -> None:
        """Bound the key map: drop dead keys, then hard-evict the oldest if needed.

        First pass reclaims every key whose window is entirely expired - the free,
        correct reclamation (those keys had no live hits, so nothing is lost).
        If that alone leaves the map still over ``max_keys`` (a pathological burst
        of genuinely-live keys), a second pass hard-evicts keys by oldest last-hit
        until the map is back at the cap, guaranteeing a true upper bound. A key
        evicted while still mid-window restarts fresh next time it is used - the
        documented, accepted over-allowance (bounded to at most one extra window's
        worth of hits per evicted key, and only ever under many-thousand-key load).
        """
        cutoff = now - self.window_seconds
        live = {
            key: window
            for key, window in self._hits.items()
            if window and window[-1] > cutoff
        }
        if len(live) > self._max_keys:
            # Still over cap on live keys alone: evict the least-recently-active.
            ordered = sorted(live.items(), key=lambda item: item[1][-1])
            live = dict(ordered[len(ordered) - self._max_keys :])
        self._hits = live

    def tracked_keys(self) -> int:
        """Number of keys currently held in the map (includes not-yet-swept)."""
        return len(self._hits)

    def stats(self) -> dict[str, int]:
        """Cheap snapshot for periodic logging by a consumer.

        ``hits`` and ``rejections`` are lifetime counts; ``tracked_keys`` is the
        live map size (an upper bound on live keys - a few may be expired-but-
        not-yet-swept). All O(1) except ``tracked_keys`` which is O(1) on the map.
        """
        return {
            "hits": self._hit_count,
            "rejections": self._reject_count,
            "tracked_keys": len(self._hits),
        }


class GlobalCeiling:
    """Process-wide gauge of concurrent holders, capped at ``capacity``.

    No time component: a holder occupies a slot from :meth:`acquire` until
    :meth:`release`. Holders are tracked in a set keyed by an opaque id (a player
    guild id, a session id - the caller's choice), so :meth:`acquire` is
    idempotent (re-acquiring an id already held is a no-op that stays True and
    takes no extra slot) and :meth:`release` is idempotent (releasing an unknown
    id is a harmless no-op). The set is inherently bounded by ``capacity`` - it
    can never hold more ids than the cap admits.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        self.capacity = capacity
        self._holders: set[typing.Any] = set()
        self._acquire_count = 0
        self._reject_count = 0

    def acquire(self, holder_id: typing.Any) -> bool:
        """Take a slot for ``holder_id``; True if held after the call.

        Re-acquiring an id already held returns True without taking a second
        slot. Returns False (and records a rejection) only when the id is new and
        the ceiling is full.
        """
        if holder_id in self._holders:
            return True
        if len(self._holders) >= self.capacity:
            self._reject_count += 1
            return False
        self._holders.add(holder_id)
        self._acquire_count += 1
        return True

    def release(self, holder_id: typing.Any) -> None:
        """Free ``holder_id``'s slot; releasing an unheld id is a no-op."""
        self._holders.discard(holder_id)

    def count(self) -> int:
        """How many slots are currently held."""
        return len(self._holders)

    def holders(self) -> frozenset[typing.Any]:
        """Immutable snapshot of the currently-held ids."""
        return frozenset(self._holders)

    def __contains__(self, holder_id: object) -> bool:
        return holder_id in self._holders

    def stats(self) -> dict[str, int]:
        """Cheap snapshot: lifetime acquires/rejections and the live count."""
        return {
            "acquires": self._acquire_count,
            "rejections": self._reject_count,
            "holders": len(self._holders),
        }


class QuotaRegistry:
    """The named quotas and ceilings the music chantier uses, wired once.

    A consumer takes one registry (typically a single shared instance) and reads
    a named quota off it - ``registry.effects_guild``, ``registry.lyrics_user``,
    ``registry.filtered_players`` - so the tuned constants live only here and no
    cog repeats a limit. :meth:`stats` folds every member's own ``stats()`` into
    one dict so a consumer can log the whole picture in a single line.

    A ``clock`` passed here is threaded into every windowed quota, so a test can
    drive the whole registry off one injected clock.
    """

    def __init__(self, *, clock: _Clock = time.monotonic) -> None:
        self.effects_guild = SlidingWindowQuota(
            EFFECTS_GUILD_LIMIT, EFFECTS_GUILD_WINDOW, clock=clock
        )
        self.lyrics_user = SlidingWindowQuota(
            LYRICS_USER_LIMIT, LYRICS_USER_WINDOW, clock=clock
        )
        self.lyrics_guild = SlidingWindowQuota(
            LYRICS_GUILD_LIMIT, LYRICS_GUILD_WINDOW, clock=clock
        )
        self.filtered_players = GlobalCeiling(FILTERED_PLAYERS_CAP)
        self.synced_lyrics = GlobalCeiling(SYNCED_LYRICS_CAP)

    def stats(self) -> dict[str, dict[str, int]]:
        """Per-member stats, keyed by member name, for one-line periodic logging."""
        return {
            "effects_guild": self.effects_guild.stats(),
            "lyrics_user": self.lyrics_user.stats(),
            "lyrics_guild": self.lyrics_guild.stats(),
            "filtered_players": self.filtered_players.stats(),
            "synced_lyrics": self.synced_lyrics.stats(),
        }
