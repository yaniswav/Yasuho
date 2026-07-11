"""Pure building blocks for the guided music UX.

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

import datetime
import itertools
import re
import time
import typing
import unicodedata
from dataclasses import dataclass

from tools.i18n import N_

# How many playable tracks the vibe picker enqueues per genre pick. Kept small so
# a pick seeds a session without flooding the queue; the curated queries return a
# spread of individual tracks, so the top few already span the genre.
TRACKS_PER_GENRE = 7

# How long (seconds) a pending "join a voice channel" watch stays live. Doubles
# as the join card's own view timeout so the card and its watch expire together.
WATCH_TTL = 300.0


# ---------------------------------------------------------------------------
# Mix / compilation detector
# ---------------------------------------------------------------------------
#
# A plain genre search ("phonk mix") is dominated by hour-long DJ compilations and
# "full album" uploads rather than individual songs, which makes for a poor seed:
# one pick then fills the queue with two or three 60-minute blobs. :func:`looks_like_mix`
# scores a candidate against several concordant signals and flags it once the total
# crosses :data:`MIX_SCORE_THRESHOLD`, so the vibe picker can prefer real tracks.
#
# It is deliberately WEIGHTED, not a keyword blacklist: a single weak signal (a
# normal-length song that merely has the word "mix", "radio" or "nonstop" in its
# title, or an artist channel whose name ends in "Mix") never flags on its own; a
# keyword paired with an abnormal duration, or several weak signals together, does.
# Weights and the threshold were tuned against the live local Lavalink node across
# all eight genres (see the lot R1 eval), so every genre yields a full page of
# individual tracks with no obvious compilation slipping through.

# Duration is the single strongest tell. A track past ~8 minutes is a strong
# suspect; past ~20 minutes it is a near-certain compilation on its own.
MIX_LONG_MS = 8 * 60 * 1000  # 8 min  -> strong suspicion (partial weight)
MIX_VERY_LONG_MS = 20 * 60 * 1000  # 20 min -> near-certain (crosses threshold alone)

# A candidate is treated as a mix once its accumulated score reaches this. Tuned so
# that: a lone weak keyword (1) or a lone author tell (1) stays well under it; an
# 8-20 min duration (2) plus any keyword (1+) crosses it; a >=20 min duration (4)
# or any single unambiguous "full album"/"compilation"/"playlist" phrase (3) crosses
# it by itself.
MIX_SCORE_THRESHOLD = 3

# Points awarded per duration bracket (graduated, not additive across brackets).
_DURATION_VERY_LONG_POINTS = 4
_DURATION_LONG_POINTS = 2

# Unambiguous compilation phrases (multilingual, accent-folded). Any one of these
# is enough on its own: they essentially never appear in a single-song title.
#
# The English superlative fragment "best of" is deliberately NOT here (it moved to
# the medium list - see below - because it collides with real English singles the
# genre queries surface). The Spanish/Portuguese/French superlatives are kept strong:
# none of the eight configured genres searches in those languages, so they never
# reach a genre seed, and on the music platforms they overwhelmingly title
# compilations. Revisit if a Romance-language genre is ever added.
_STRONG_TITLE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\bfull album\b",
        r"\balbum complet[oa]?\b",  # es/pt "album completo", fr "album complet"
        r"\bcompilation\b",
        r"\bcompilacao\b",  # pt
        r"\bcompilacion\b",  # es
        r"\brecopilacion\b",  # es "recopilacion" (compilation)
        r"\bgreatest hits\b",
        r"\bgrandes exitos\b",  # es
        r"\blo mejor de\b",  # es
        r"\blos mejores\b",  # es
        r"\bas melhores\b",  # pt
        r"\bles meilleur",  # fr "les meilleurs/meilleures ..."
        r"\bdj set\b",
        r"\bmega ?mix\b",
        r"\bmegamix\b",
        r"\bmedley\b",
        r"\bplaylist\b",
    )
)
_STRONG_TITLE_POINTS = 3

# Medium signals: tracklist counts, hour-length markers, year ranges, "mixtape", and
# the "best of" superlative fragment. Strong hints, but each can (rarely) show up in
# a legitimate single-song title, so on their own they sit one weak signal short of
# the threshold.
#
# "best of" is medium, NOT strong: unlike a complete compilation label, it is a
# grammatical fragment that continues into a noun which routinely forms a real song
# title - "Best of You" (Foo Fighters), "The Best of Me" (Bryan Adams), "Best of My
# Love" (The Emotions), "Best of Both Worlds" (Van Halen). A genuine "Best Of ..."
# compilation is always album-length, so the duration bracket (+2/+4) corroborates it
# past the threshold anyway; a 4-minute single titled "Best of ..." must not be
# dropped from a genre seed on the phrase alone.
_MEDIUM_TITLE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\bbest of\b",
        r"\bmixtape\b",
        r"\btop\s?\d{1,3}\b",  # top 20 / top 50 / top 100 tracklist count
        r"\b(?:19|20)\d{2}\s*[-/]\s*(?:19|20)\d{2}\b",  # 2010-2020 span
        r"\b\d{1,2}\s*(?:hours?|hrs?|heures?|horas|stunden)\b",  # N hours (multi-lang)
        r"\bone hour\b",
    )
)
_MEDIUM_TITLE_POINTS = 2

# Weak signals: words that appear in plenty of legitimate single tracks (remixes,
# radio edits, songs that happen to contain a year). Worth only a nudge; they flag
# only when they pile up or land on top of an abnormal duration.
_WEAK_TITLE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\bmix\b",  # "(Extended Mix)", "PHONK MIX", ...
        r"\bmixes\b",
        r"\bmezcla\b",  # es "mix"
        r"\bnon[- ]?stop\b",
        r"\bradio\b",
        r"\b(?:19|20)\d{2}\b",  # a bare 4-digit year token
    )
)
_WEAK_TITLE_POINTS = 1

# Author channel tells. A channel whose name ends in "Mix"/"Radio"/"Compilation"
# is a compilation factory; "and N more" is Lavalink's multi-artist credit, which a
# stitched-together compilation carries. Weak on their own (a real artist channel
# can end in "Mix").
_AUTHOR_SUFFIXES = ("mix", "mixes", "radio", "compilation")
_AUTHOR_MULTI = re.compile(r"\band \d+ more\b")
_AUTHOR_POINTS = 1


def _fold(text: str) -> str:
    """Lowercase ``text`` and strip accents to ASCII for keyword matching.

    Folds so the ASCII patterns above match accented real-world titles
    ("recopilacion" matches "recopilacion", "heures" any case). Pure.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def mix_score(title: str, author: str, duration_ms: typing.Optional[float]) -> int:
    """Return the accumulated mix-suspicion score for a candidate track.

    Higher means more likely to be an hour-long compilation rather than a single
    song. Exposed alongside :func:`looks_like_mix` so the threshold can be reasoned
    about and tested directly. Pure and None-safe.
    """
    score = 0

    try:
        duration = int(duration_ms) if duration_ms else 0
    except (TypeError, ValueError):
        duration = 0
    if duration >= MIX_VERY_LONG_MS:
        score += _DURATION_VERY_LONG_POINTS
    elif duration >= MIX_LONG_MS:
        score += _DURATION_LONG_POINTS

    folded_title = _fold(title or "")
    for pattern in _STRONG_TITLE_PATTERNS:
        if pattern.search(folded_title):
            score += _STRONG_TITLE_POINTS
    for pattern in _MEDIUM_TITLE_PATTERNS:
        if pattern.search(folded_title):
            score += _MEDIUM_TITLE_POINTS
    for pattern in _WEAK_TITLE_PATTERNS:
        if pattern.search(folded_title):
            score += _WEAK_TITLE_POINTS

    folded_author = _fold(author or "")
    if any(folded_author.endswith(suffix) for suffix in _AUTHOR_SUFFIXES):
        score += _AUTHOR_POINTS
    if _AUTHOR_MULTI.search(folded_author):
        score += _AUTHOR_POINTS

    return score


def looks_like_mix(
    title: str, author: str, duration_ms: typing.Optional[float]
) -> bool:
    """True when a candidate scores as an hour-long mix/compilation, not a song.

    Weighted, not a blacklist: see :data:`MIX_SCORE_THRESHOLD`. Pure and None-safe,
    so the vibe picker can filter real Lavalink results without a live node in
    tests.
    """
    return mix_score(title, author, duration_ms) >= MIX_SCORE_THRESHOLD


def next_dj(
    members: typing.Sequence[typing.Any], *, leaving_id: typing.Optional[int] = None
) -> typing.Optional[typing.Any]:
    """First non-bot member to inherit the DJ role, or None for an empty room.

    Used by the voice-state listener when the current DJ leaves the player's
    channel: control passes to the first remaining human (channel order), and a
    room with no humans left yields None so the empty-channel disconnect logic
    can take over. ``leaving_id`` drops the departing member even if the voice
    cache has not evicted them yet, so the handoff never re-picks the leaver.
    Pure and bot-safe (skips anything whose ``bot`` attribute is truthy).
    """
    for member in members:
        if getattr(member, "bot", False):
            continue
        if leaving_id is not None and getattr(member, "id", None) == leaving_id:
            continue
        return member
    return None


def current_year(now: typing.Optional[datetime.datetime] = None) -> int:
    """Return the current UTC year, injectable for tests.

    Kept here so the trending queries can splice in the year at runtime rather than
    baking a stale literal into the source.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.year


def resolve_query(template: str, *, now: typing.Optional[datetime.datetime] = None) -> str:
    """Fill a query template's ``{year}`` placeholder with the current year.

    Templates without a placeholder pass through unchanged, so the same call works
    for both the recency-tuned trending query and the evergreen all-time one. Pure.
    """
    return template.format(year=current_year(now))


def interleave_results(
    a: typing.Sequence[typing.Any], b: typing.Sequence[typing.Any]
) -> typing.List[typing.Any]:
    """Alternate two track lists into one, deduped by identifier, order preserved.

    Emits ``a[0], b[0], a[1], b[1], ...`` and drops any track whose ``identifier``
    was already emitted, so blending the trending and all-time queries keeps both
    voices without ever queueing the same track twice. Tracks with no identifier
    are always kept (they cannot be deduped). Pure.
    """
    out: typing.List[typing.Any] = []
    seen: set = set()
    for pair in itertools.zip_longest(a, b):
        for track in pair:
            if track is None:
                continue
            identifier = getattr(track, "identifier", None)
            if identifier is not None:
                if identifier in seen:
                    continue
                seen.add(identifier)
            out.append(track)
    return out


# ---------------------------------------------------------------------------
# /seek target parsing
# ---------------------------------------------------------------------------
#
# The /seek command accepts several human spellings of a position and turns them
# into a millisecond target. Parsing lives here, pure and discord-free, so every
# accepted form and every rejected junk string is exercised without a voice
# connection or a live node. The cog owns clamping the resolved target against
# the current track and the actual player.seek() call.

# Upper bounds on the colon-form fields, kept sane so a fat-fingered "999:99:99"
# is refused rather than seeking hours past any real track. Seconds (and the
# minutes field of an h:mm:ss form) must be a valid clock digit (< 60); a bare
# m:ss minutes field is allowed up to a long-track ceiling; hours are capped at a
# day. The player clamps the resolved target to the track length regardless.
_SEEK_MAX_HOURS = 24
_SEEK_MAX_MS_MINUTES = 600  # minutes field of a bare m:ss form (10h ceiling)

# A relative nudge: a leading sign then whole seconds, tolerant of a space after
# the sign ("+30", "- 15"). Absolute forms are handled by split-on-colon below.
_SEEK_RELATIVE_RE = re.compile(r"^([+-])\s*(\d+)$")
_SEEK_BARE_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class SeekTarget:
    """A parsed /seek request: a millisecond value plus whether it is relative.

    ``relative`` targets carry a signed offset (``+30s`` -> ``+30000``) to add to
    the live playback position; absolute targets carry a non-negative position
    from the start of the track. Both are resolved to a final clamped position by
    :func:`resolve_seek_ms`.
    """

    relative: bool
    milliseconds: int


def parse_seek_target(text: typing.Optional[str]) -> typing.Optional[SeekTarget]:
    """Parse a /seek argument into a :class:`SeekTarget`, or None on junk.

    Accepted forms (outer whitespace tolerated throughout):

    * ``"1:23"`` - m:ss, absolute (seconds < 60)
    * ``"01:02:03"`` - h:mm:ss, absolute (seconds and minutes < 60, hours capped)
    * ``"90"`` - bare whole seconds, absolute
    * ``"+30"`` / ``"-15"`` - a signed second offset, relative

    Returns None for anything else - empty/blank input, non-numeric junk
    ("abc"), a sign with no digits ("-x"), an out-of-range clock field ("1:99")
    or an insanely large colon form. Pure and None-safe.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    match = _SEEK_RELATIVE_RE.match(stripped)
    if match is not None:
        sign, digits = match.groups()
        milliseconds = int(digits) * 1000
        if sign == "-":
            milliseconds = -milliseconds
        return SeekTarget(relative=True, milliseconds=milliseconds)

    if _SEEK_BARE_RE.match(stripped):
        return SeekTarget(relative=False, milliseconds=int(stripped) * 1000)

    parts = [part.strip() for part in stripped.split(":")]
    if not all(part.isdigit() for part in parts):
        return None

    if len(parts) == 2:
        minutes, seconds = int(parts[0]), int(parts[1])
        if seconds >= 60 or minutes >= _SEEK_MAX_MS_MINUTES:
            return None
        total_seconds = minutes * 60 + seconds
        return SeekTarget(relative=False, milliseconds=total_seconds * 1000)

    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
        if seconds >= 60 or minutes >= 60 or hours >= _SEEK_MAX_HOURS:
            return None
        total_seconds = hours * 3600 + minutes * 60 + seconds
        return SeekTarget(relative=False, milliseconds=total_seconds * 1000)

    return None


def resolve_seek_ms(target: SeekTarget, position_ms: int, length_ms: int) -> int:
    """Resolve a :class:`SeekTarget` to a final position clamped to [0, length].

    A relative target is added to the live ``position_ms``; an absolute one is
    taken as-is. The result is clamped into ``[0, length_ms]`` so a nudge past
    either end lands on the boundary rather than erroring or overshooting. Pure.
    """
    base = position_ms + target.milliseconds if target.relative else target.milliseconds
    if base < 0:
        return 0
    if base > length_ms:
        return length_ms
    return base


@dataclass(frozen=True)
class Genre:
    """One entry in the vibe picker.

    ``key`` is the stable select-option value (never shown, never translated).
    ``label`` is the genre's proper name and stays untranslated by design.
    ``description`` is short descriptive text marked with ``N_`` so pybabel
    collects it; render it through ``_(genre.description)`` at display time.

    Two curated search queries, both tuned (against the live node) to return
    individual tracks rather than hour-long compilations, are blended by the cog:

    * ``query_trending`` leans on recency (it may carry a ``{year}`` placeholder
      filled at runtime by :func:`resolve_query`, so no stale year is baked in).
    * ``query_alltime`` is evergreen, so a fresh session is not all one month's
      virals.

    The cog runs both, interleaves them (:func:`interleave_results`) and filters
    the blend down the mix-detector ladder.
    """

    key: str
    emoji: str
    label: str
    query_trending: str
    query_alltime: str
    description: str


# The eight international genres offered on a bare /play. Labels are proper names
# (untranslated); queries are ASCII, curated (and measured against the live node)
# to return individual tracks rather than hour-long compilations; descriptions are
# N_-marked for translation at render time. ``{year}`` in a trending query is filled
# at runtime, never hardcoded.
GENRE_CATALOG: tuple[Genre, ...] = (
    Genre(
        key="phonk",
        emoji="\N{RACING CAR}",
        label="Phonk",
        query_trending="phonk sped up {year}",
        query_alltime="phonk edit audio",
        description=N_("Dark, drift-ready beats"),
    ),
    Genre(
        key="lofi",
        emoji="\N{HOT BEVERAGE}",
        label="Lofi",
        query_trending="lofi type beat {year}",
        query_alltime="lofi hip hop track",
        description=N_("Chill beats to relax and study"),
    ),
    Genre(
        key="pop",
        emoji="\N{MICROPHONE}",
        label="Pop",
        query_trending="new pop single {year}",
        query_alltime="pop hit single",
        description=N_("Chart-topping hits"),
    ),
    Genre(
        key="hiphop",
        emoji="\N{FIRE}",
        label="Hip-Hop",
        query_trending="hip hop rap song {year}",
        query_alltime="hip hop rap single",
        description=N_("Rap and hip-hop heat"),
    ),
    Genre(
        key="edm",
        emoji="\N{HIGH VOLTAGE SIGN}",
        label="Electro/EDM",
        query_trending="edm single {year}",
        query_alltime="electro house single",
        description=N_("Festival-ready electronic energy"),
    ),
    Genre(
        key="rock",
        emoji="\N{GUITAR}",
        label="Rock",
        query_trending="new rock single {year}",
        query_alltime="classic rock single",
        description=N_("Guitar-driven classics"),
    ),
    Genre(
        key="jazz",
        emoji="\N{SAXOPHONE}",
        label="Jazz",
        query_trending="jazz track {year}",
        query_alltime="jazz standard track",
        description=N_("Smooth, laid-back jazz"),
    ),
    Genre(
        key="jpop",
        emoji="\N{CHERRY BLOSSOM}",
        label="J-Pop/Anime",
        query_trending="jpop song {year}",
        query_alltime="jpop hit single",
        description=N_("Anime openings and J-pop"),
    ),
)

# Fast lookup from a select value back to its Genre.
GENRES_BY_KEY: dict[str, Genre] = {genre.key: genre for genre in GENRE_CATALOG}


# How many played-track identifiers a radio session remembers so its refill
# never re-seeds a track it has already played. Bounded so a marathon session
# cannot grow the set without bound; a restart forgets it (an accepted, at-worst
# one-repeat trade-off - the set is not persisted).
PLAYED_IDS_CAP = 500


class PlayedTracks:
    """Bounded, insertion-ordered set of the identifiers a radio session played.

    A radio refill excludes everything already heard this session so a station
    does not loop the same handful of tracks. The set is capped at ``cap``:
    adding past it evicts the oldest identifier first, so a long session stays
    bounded in memory. Re-adding a known identifier refreshes its recency (moves
    it to the newest slot) rather than duplicating it. Pure and None/empty-safe
    (a blank identifier is ignored), so it is unit-tested without a live player.
    """

    def __init__(self, cap: int = PLAYED_IDS_CAP) -> None:
        self._cap = cap
        # dict preserves insertion order (py3.7+); the value is unused.
        self._ids: dict[str, None] = {}

    def add(self, identifier: typing.Optional[str]) -> None:
        """Record ``identifier`` as played, evicting the oldest past the cap."""
        if not identifier:
            return
        # Drop-then-set so a repeat moves to the newest slot instead of aging out.
        self._ids.pop(identifier, None)
        self._ids[identifier] = None
        while len(self._ids) > self._cap:
            oldest = next(iter(self._ids))
            del self._ids[oldest]

    def __contains__(self, identifier: object) -> bool:
        return identifier in self._ids

    def __iter__(self) -> typing.Iterator[str]:
        return iter(self._ids)

    def __len__(self) -> int:
        return len(self._ids)


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
