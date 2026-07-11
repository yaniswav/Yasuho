"""Pure building blocks for the Rythm-style music UX.

This module owns the side-effect-free half of the "choose your vibe" flow so it
can be unit-tested without discord, sonolink, a database or a live Lavalink node:

* :data:`GENRE_CATALOG` - the fixed set of international genres offered by the
  vibe picker, each with a display emoji, an (untranslated) proper-name label, a
  short translatable description and a curated search query.
* :class:`PendingVoiceWatches` - the bounded, self-pruning map that remembers a
  user's open "join a voice channel" card so the cog can auto-swap it into the
  vibe card once they join (fire-once, TTL-scoped, size-capped).

The cog (``cogs/music/music.py``) owns everything that touches discord/sonolink:
the card views, the /play routing and the voice-state listener that fires these
watches. Keeping the data and bookkeeping here means the catalog and the map can
never drift and are exercised in isolation by ``tests/cogs/test_music_vibes.py``.
"""

from __future__ import annotations

import time
import typing
from dataclasses import dataclass

from tools.i18n import N_

# How many playable tracks the vibe picker enqueues per genre pick. Kept small so
# a pick seeds a session without flooding the queue; the curated query returns a
# mix, so the top few already span the genre.
TRACKS_PER_GENRE = 7

# How long (seconds) a pending "join a voice channel" watch stays live. Doubles
# as the join card's own view timeout so the card and its watch expire together.
WATCH_TTL = 300.0


@dataclass(frozen=True)
class Genre:
    """One entry in the vibe picker.

    ``key`` is the stable select-option value (never shown, never translated).
    ``label`` is the genre's proper name and stays untranslated by design.
    ``description`` is short descriptive text marked with ``N_`` so pybabel
    collects it; render it through ``_(genre.description)`` at display time.
    ``query`` is a curated search string tuned to return a good genre mix.
    """

    key: str
    emoji: str
    label: str
    query: str
    description: str


# The eight international genres offered on a bare /play. Labels are proper names
# (untranslated); queries are ASCII, curated to return a good mix from a plain
# YouTube search; descriptions are N_-marked for translation at render time.
GENRE_CATALOG: tuple[Genre, ...] = (
    Genre(
        key="phonk",
        emoji="\N{RACING CAR}",
        label="Phonk",
        query="phonk mix 2025",
        description=N_("Dark, drift-ready beats"),
    ),
    Genre(
        key="lofi",
        emoji="\N{HOT BEVERAGE}",
        label="Lofi",
        query="lofi hip hop radio beats to relax study",
        description=N_("Chill beats to relax and study"),
    ),
    Genre(
        key="pop",
        emoji="\N{MICROPHONE}",
        label="Pop",
        query="top pop hits 2025 playlist",
        description=N_("Chart-topping hits"),
    ),
    Genre(
        key="hiphop",
        emoji="\N{FIRE}",
        label="Hip-Hop",
        query="best hip hop rap mix 2025",
        description=N_("Rap and hip-hop heat"),
    ),
    Genre(
        key="edm",
        emoji="\N{HIGH VOLTAGE SIGN}",
        label="Electro/EDM",
        query="edm festival electro house mix 2025",
        description=N_("Festival-ready electronic energy"),
    ),
    Genre(
        key="rock",
        emoji="\N{GUITAR}",
        label="Rock",
        query="rock classics greatest hits playlist",
        description=N_("Guitar-driven classics"),
    ),
    Genre(
        key="jazz",
        emoji="\N{SAXOPHONE}",
        label="Jazz",
        query="smooth jazz relaxing playlist",
        description=N_("Smooth, laid-back jazz"),
    ),
    Genre(
        key="jpop",
        emoji="\N{CHERRY BLOSSOM}",
        label="J-Pop/Anime",
        query="best anime openings jpop mix",
        description=N_("Anime openings and J-pop"),
    ),
)

# Fast lookup from a select value back to its Genre.
GENRES_BY_KEY: dict[str, Genre] = {genre.key: genre for genre in GENRE_CATALOG}


class PendingVoiceWatches:
    """Bounded, self-pruning map of open "join a voice channel" cards.

    Keyed on ``(guild_id, user_id)``; each entry pairs an opaque ``payload`` (the
    join card's view, so the cog can edit its message and stop it) with an
    absolute monotonic expiry. :meth:`pop` hands a payload back at most once and
    only while it is still live, so a user who joins after the window simply gets
    nothing (fire-once + TTL). :meth:`add` lazily sweeps expired entries once the
    map grows past ``sweep_at``, so it can never grow without bound - the same
    discipline as :class:`tools.cooldowns.Cooldowns`.

    In-memory only: a restart forgets every pending watch. That is acceptable -
    the orphaned card simply times out on its own view timeout, and the user can
    re-run /play. Times default to ``time.monotonic()`` so a wall-clock change
    cannot skew a window; callers may inject ``now`` (used by the tests).
    """

    def __init__(self, ttl: float = WATCH_TTL, *, sweep_at: int = 256) -> None:
        self.ttl = ttl
        self._sweep_at = sweep_at
        self._watches: dict[tuple[int, int], tuple[typing.Any, float]] = {}

    def add(
        self, guild_id: int, user_id: int, payload: typing.Any, *, now: float | None = None
    ) -> None:
        """Register a pending watch, sweeping stale entries past the size cap."""
        now = time.monotonic() if now is None else now
        self._watches[(guild_id, user_id)] = (payload, now + self.ttl)
        if len(self._watches) > self._sweep_at:
            self._sweep(now)

    def pop(
        self, guild_id: int, user_id: int, *, now: float | None = None
    ) -> typing.Any | None:
        """Remove and return the live payload for the key, or None.

        Expired entries are dropped and read as absent, so a watch never fires
        after its window (fire-once is enforced by removing the entry here).
        """
        now = time.monotonic() if now is None else now
        entry = self._watches.pop((guild_id, user_id), None)
        if entry is None:
            return None
        payload, expires_at = entry
        if expires_at <= now:
            return None
        return payload

    def discard(self, guild_id: int, user_id: int) -> None:
        """Forget a pending watch (e.g. when the user re-runs /play)."""
        self._watches.pop((guild_id, user_id), None)

    def _sweep(self, now: float) -> None:
        self._watches = {
            key: value for key, value in self._watches.items() if value[1] > now
        }

    def __len__(self) -> int:
        return len(self._watches)

    def __contains__(self, key: tuple[int, int]) -> bool:
        return key in self._watches
