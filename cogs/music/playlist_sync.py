"""Server playlists imported from a link, optionally kept in sync (Yasuho+).

``/serverplaylist import`` loads a public playlist link (YouTube, Spotify,
Deezer, Apple Music, ... whatever Lavalink resolves into a playlist) and stores
its tracks as an ordinary server playlist. With ``keep_synced`` the link is kept
on the row (``guild_playlists.source_url``) and a weekly background pass
re-imports it. This module owns the pure decisions, the SQL, the source loader
and the sweep; the commands live in :mod:`cogs.music.playlists_shared`.

TIMER SHAPE: ONE sweep timer, not one timer per playlist. A row per linked
playlist would be up to guilds x 10 timers rows all falling due in the same
dispatcher; the reminders dispatcher fires them one by one with no notion of a
shared budget, so the global Spotify cap would have to be rebuilt around it
anyway. Instead a single ``playlist_sync`` row in the shared ``timers`` table
(DELETE-as-claim, at-most-once, dispatched as ``on_playlist_sync_timer_complete``)
wakes every :data:`SWEEP_INTERVAL` seconds, is RE-ARMED BEFORE any work (a crash
mid-sweep costs that sweep, never the series) and then resyncs at most
:data:`MAX_RESYNCS_PER_SWEEP` playlists. The per-playlist schedule is the row's
own ``next_sync_at``, which the sweep advances as the claim (UPDATE ... RETURNING)
before it loads anything, so even two overlapping sweeps could not take one row
twice.

SCALE: the global cap is 6 sweeps/h x 10 = at most 60 source loads per hour
bot-wide, however many guilds link playlists (1000 guilds x 10 = 10 000 linked
rows would need ~60/h on a 7 day cycle, so the cap is also the steady-state
rate; beyond it the oldest-due rows simply wait). Each load is one Lavalink
request (LavaSrc paginates a Spotify playlist at ~100 tracks, so <= ~10 Spotify
calls for a 1000-track playlist, in practice 2-5): well under 600 Spotify
requests/h, ~0.2 req/s, spread by a 1-4 s pause between loads and by a 0-6 h
jitter on each playlist's next date. The sweep SELECT is one query on a partial
index; the claim and the write are single-row updates.

FAILURE: a source that 404s, went private or is empty records ``sync_error``,
keeps the old tracks and is retried a week later; after :data:`MAX_STRIKES`
consecutive failures ``next_sync_at`` becomes NULL ("sync paused": the link is
kept, nothing is retried, nobody is messaged). A Lavalink outage is not the
source's fault: it costs no strike and is retried in an hour.

LAPSE (nothing taken hostage): when a guild is no longer entitled (or the bot
left it) the sweep does NOT resync it but only pushes that row's
``next_sync_at`` one day ahead - one cheap UPDATE per row per day, no budget
spent. The link and tracks stay untouched, and syncing resumes by itself within
a day of the subscription returning. (Not re-arming and re-arming on demand
would need a hook in the entitlement code for a case that costs one UPDATE a day.)
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import random
import typing
from urllib.parse import urlsplit

import discord
from discord.ext import commands
from sonolink.models import SearchResult

from cogs.music import urlguard

log = logging.getLogger(__name__)

SYNC_EVENT = "playlist_sync"

SYNC_INTERVAL = datetime.timedelta(days=7)
SYNC_JITTER_MAX = datetime.timedelta(hours=6)
SWEEP_INTERVAL = datetime.timedelta(minutes=10)
# 6 sweeps/h x 10 = the global ceiling of 60 source loads per hour.
MAX_RESYNCS_PER_SWEEP = 10
# Rows read per sweep. Lapsed rows spend no resync budget but do spend a slot
# here (once a day each), so this bounds the sweep's work as well.
CANDIDATE_BATCH = 50
MAX_STRIKES = 3
LAPSED_RECHECK = datetime.timedelta(days=1)
NODE_RETRY = datetime.timedelta(hours=1)
PAUSE_BETWEEN_LOADS = (1.0, 4.0)
MAX_URL_LEN = 500

# Status codes of a source load (stored in sync_error for the failures).
OK = "ok"
BLOCKED = "blocked"
BAD = "bad_link"
NOT_PLAYLIST = "not_playlist"
UNAVAILABLE = "unavailable"
EMPTY = "empty"
LOAD_FAILED = "load_failed"
# Not the source's fault (DNS hiccup, transport error, a Lavalink fault): never a
# strike. At import it reads as "couldn't load"; at sync it is retried in an hour.
TRANSIENT = "transient"
# Re-arm retries after the dispatcher consumed the sweep row (seconds).
REARM_BACKOFF = (1, 5, 20)
# Safety net: the idle loop re-runs the idempotent ensure at most this often.
ENSURE_EVERY = 3600.0
# Advisory-lock key (two-int form, a key space separate from the one-int form).
_LOCK_CLASS = 0x504C5359  # 'PLSY'


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def source_domain(url: typing.Optional[str]) -> str:
    """The host to show for a source link ("open.spotify.com"), never the URL."""
    host = urlsplit(url or "").hostname or ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def clip_tracks(
    blobs: typing.Sequence[str], cap: int
) -> typing.Tuple[typing.List[str], int]:
    """``(kept, over)``: the head that fits ``cap`` and how many were left out."""
    kept = list(blobs[: max(int(cap), 0)])
    return kept, len(blobs) - len(kept)


def next_due(now: datetime.datetime, *, jitter: float) -> datetime.datetime:
    """The next weekly slot: a week out plus ``jitter`` (0..1) of the jitter span."""
    return now + SYNC_INTERVAL + SYNC_JITTER_MAX * max(0.0, min(float(jitter), 1.0))


def is_paused(source_url: typing.Optional[str], next_sync_at: typing.Any) -> bool:
    """A linked playlist whose schedule was dropped after repeated failures."""
    return bool(source_url) and next_sync_at is None


@dataclasses.dataclass(frozen=True)
class SourceResult:
    status: str
    blobs: typing.Tuple[str, ...] = ()
    total_ms: int = 0
    # Per-track length, aligned with blobs, so a clip can total what it kept.
    lengths: typing.Tuple[int, ...] = ()


def clip_result(result: SourceResult, cap: int) -> typing.Tuple[SourceResult, int]:
    """``(result limited to cap tracks, over)`` with ``total_ms`` of the kept head."""
    kept, over = clip_tracks(result.blobs, cap)
    lengths = result.lengths[: len(kept)]
    return (
        dataclasses.replace(
            result, blobs=tuple(kept), lengths=tuple(lengths), total_ms=sum(lengths)
        ),
        over,
    )


# ---------------------------------------------------------------------------
# SQL (module level, pool first - the always_on.py shape)
# ---------------------------------------------------------------------------


async def due_rows(pool, now: datetime.datetime, limit: int = CANDIDATE_BATCH):
    return await pool.fetch(
        """
        SELECT guild_id, name_norm
        FROM guild_playlists
        WHERE source_url IS NOT NULL AND next_sync_at IS NOT NULL
          AND next_sync_at <= $1
        ORDER BY next_sync_at
        LIMIT $2
        """,
        now,
        int(limit),
    )


async def claim_row(
    pool,
    guild_id: int,
    norm: str,
    now: datetime.datetime,
    next_at: datetime.datetime,
) -> typing.Optional[str]:
    """Take one due row by moving its schedule; returns its source_url (or None).

    The conditional ``next_sync_at <= now`` makes this the at-most-once claim:
    a row already taken (or unlinked, or paused) matches nothing.
    """
    return await pool.fetchval(
        """
        UPDATE guild_playlists SET next_sync_at = $4
        WHERE guild_id = $1 AND name_norm = $2
          AND source_url IS NOT NULL AND next_sync_at IS NOT NULL
          AND next_sync_at <= $3
        RETURNING source_url
        """,
        guild_id,
        norm,
        now,
        next_at,
    )


async def record_success(
    pool,
    guild_id: int,
    norm: str,
    url: str,
    blobs: typing.Sequence[str],
    total_ms: int,
    now: datetime.datetime,
) -> bool:
    """Replace the tracks from a good load. ``source_url = $3`` so a playlist
    unlinked or re-linked elsewhere while the load ran is left alone."""
    status = await pool.execute(
        """
        UPDATE guild_playlists
        SET tracks = $4, track_count = $5, total_ms = $6,
            synced_at = $7, sync_failures = 0, sync_error = NULL
        WHERE guild_id = $1 AND name_norm = $2 AND source_url = $3
        """,
        guild_id,
        norm,
        url,
        list(blobs),
        len(blobs),
        int(total_ms),
        now,
    )
    return status.rsplit(" ", 1)[-1] == "1"


async def record_failure(
    pool, guild_id: int, norm: str, url: str, error: str, max_strikes: int = MAX_STRIKES
) -> None:
    """Count one failure; the third in a row drops the schedule (paused)."""
    await pool.execute(
        """
        UPDATE guild_playlists
        SET sync_failures = sync_failures + 1, sync_error = $4,
            next_sync_at = CASE WHEN sync_failures + 1 >= $5
                                THEN NULL ELSE next_sync_at END
        WHERE guild_id = $1 AND name_norm = $2 AND source_url = $3
        """,
        guild_id,
        norm,
        url,
        error,
        int(max_strikes),
    )


async def reschedule(
    pool, guild_id: int, norm: str, url: str, next_at: datetime.datetime
) -> None:
    """Move a row's schedule without a verdict (Lavalink down: no strike)."""
    await pool.execute(
        "UPDATE guild_playlists SET next_sync_at = $4 "
        "WHERE guild_id = $1 AND name_norm = $2 AND source_url = $3 "
        "AND next_sync_at IS NOT NULL",
        guild_id,
        norm,
        url,
        next_at,
    )


async def ensure_sweep_timer(pool, when: datetime.datetime) -> bool:
    """Make sure exactly one pending sweep row exists; True when one was added.

    NOT EXISTS alone is not atomic (concurrent callers each see "none" and each
    insert), so the check-and-insert runs under a transaction advisory lock: the
    second caller's INSERT statement starts after the first committed.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1, 0)", _LOCK_CLASS)
            status = await conn.execute(
                "INSERT INTO timers (event, expires, extra) "
                "SELECT $1::text, $2, '{}'::jsonb "
                "WHERE NOT EXISTS (SELECT 1 FROM timers WHERE event = $1)",
                SYNC_EVENT,
                when,
            )
    return status.rsplit(" ", 1)[-1] == "1"


# ---------------------------------------------------------------------------
# Cog mixin
# ---------------------------------------------------------------------------


class PlaylistSyncMixin:
    """Cog mixin: the source loader and the weekly sweep for linked playlists."""

    _sync_lock: typing.Optional[asyncio.Lock] = None
    _last_sync_ensure: float = float("-inf")

    async def _load_uncached(self, url: str):
        """One Lavalink load that bypasses sonolink's per-node result cache.

        ``Node.search_track`` caches EVERY result forever (errors included), so a
        resync or a re-import through it would return the first answer for ever.
        This goes straight to the REST manager and builds the same
        ``SearchResult`` the cached path returns, caching nothing; ``/play`` keeps
        its cached path untouched. Raises ``RuntimeError`` when no node is up.
        """
        node = self.bot.sl_client.get_best_node()
        data = await node._manager.load_track(url)
        return SearchResult(client=node._ensure_client(), data=data)

    async def _fetch_source_tracks(self, url: str) -> SourceResult:
        """Load ``url`` as a playlist through the URL guard, uncached.

        Never raises: every failure is a status code. A bare track or a text
        query is ``not_playlist`` (only a URL-shaped link that Lavalink resolves
        into a playlist is importable). Only a definite verdict (not found,
        private, not a playlist, blocked) is a strike at sync time; DNS,
        transport and non-"common" Lavalink errors are ``TRANSIENT``.
        """
        text = (url or "").strip()
        if (
            not text
            or len(text) > MAX_URL_LEN
            or not urlguard.is_url_shaped(text)
        ):
            return SourceResult(BAD)
        reason = await urlguard.check_query(text)
        if reason is not None:
            log.warning("Refused unsafe playlist import URL (%s)", reason)
            if reason == urlguard.REASON_UNRESOLVABLE:
                return SourceResult(TRANSIENT)
            return SourceResult(BLOCKED)
        try:
            result = await self._load_uncached(text)
        except RuntimeError:
            return SourceResult(UNAVAILABLE)
        except Exception:
            log.exception("Playlist source load failed")
            return SourceResult(TRANSIENT)
        if result.is_error():
            try:
                severity = str(result.exception.severity)
            except Exception:
                severity = ""
            return SourceResult(LOAD_FAILED if severity == "common" else TRANSIENT)
        if result.is_empty() or result.result is None:
            return SourceResult(EMPTY)
        tracks = getattr(result.result, "tracks", None)
        if tracks is None:
            return SourceResult(NOT_PLAYLIST)
        blobs: typing.List[str] = []
        lengths: typing.List[int] = []
        for track in tracks:
            blob = getattr(track, "encoded", None)
            if blob:
                blobs.append(blob)
                lengths.append(int(getattr(track, "length", 0) or 0))
        if not blobs:
            return SourceResult(EMPTY)
        return SourceResult(OK, tuple(blobs), sum(lengths), tuple(lengths))

    # -- the sweep ---------------------------------------------------------

    async def _ensure_sync_timer(self) -> bool:
        """Arm the sweep row if missing. True when a row exists afterwards."""
        try:
            added = await ensure_sweep_timer(
                self.bot.db_pool, discord.utils.utcnow() + SWEEP_INTERVAL
            )
            if added:
                reminder = self.bot.get_cog("Reminder")
                if reminder is not None:
                    reminder._have_data.set()
        except Exception:
            log.exception("PLAYLIST-SYNC could not arm the sweep timer")
            return False
        return True

    async def _rearm_with_retry(self) -> bool:
        """Re-arm right after the dispatcher consumed the row: a failure here
        would end the series (on_ready does not fire on a gateway resume), so
        retry a few times with a short backoff."""
        if await self._ensure_sync_timer():
            return True
        for delay in REARM_BACKOFF:
            await asyncio.sleep(delay)
            if await self._ensure_sync_timer():
                return True
        log.error("PLAYLIST-SYNC sweep timer NOT re-armed; the idle loop will retry")
        return False

    async def _ensure_sync_timer_hourly(self, now: float) -> None:
        """Safety net called from the idle loop: at most once per hour."""
        if now - self._last_sync_ensure >= ENSURE_EVERY:
            self._last_sync_ensure = now
            await self._ensure_sync_timer()

    @commands.Cog.listener(name="on_ready")
    async def _playlist_sync_on_ready(self) -> None:
        await self._ensure_sync_timer()

    @commands.Cog.listener()
    async def on_playlist_sync_timer_complete(self, extra) -> None:
        # Re-arm FIRST: the dispatcher already deleted this row (the claim), so
        # a crash below must not end the series.
        await self._rearm_with_retry()
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            try:
                stats = await self._run_sync_sweep()
            except Exception:
                log.exception("PLAYLIST-SYNC sweep crashed")
                return
        if stats["synced"] or stats["failed"] or stats["skipped"]:
            log.info(
                "PLAYLIST-SYNC synced=%d failed=%d skipped=%d",
                stats["synced"],
                stats["failed"],
                stats["skipped"],
            )

    async def _sync_pause(self) -> None:
        await asyncio.sleep(random.uniform(*PAUSE_BETWEEN_LOADS))

    async def _run_sync_sweep(
        self, now: typing.Optional[datetime.datetime] = None
    ) -> typing.Dict[str, int]:
        """Resync at most MAX_RESYNCS_PER_SWEEP due playlists (the global cap)."""
        pool = self.bot.db_pool
        now = now or discord.utils.utcnow()
        stats = {"synced": 0, "failed": 0, "skipped": 0}
        loads = 0
        for row in await due_rows(pool, now):
            if loads >= MAX_RESYNCS_PER_SWEEP:
                break
            guild_id, norm = row["guild_id"], row["name_norm"]
            entitled = (
                self.bot.get_guild(guild_id) is not None
                and self.bot.premium.for_guild(guild_id).max_linked_playlists > 0
            )
            if not entitled:
                if await claim_row(pool, guild_id, norm, now, now + LAPSED_RECHECK):
                    stats["skipped"] += 1
                continue
            url = await claim_row(
                pool, guild_id, norm, now, next_due(now, jitter=random.random())
            )
            if url is None:
                continue
            if loads:
                await self._sync_pause()
            loads += 1
            outcome = await self._resync_one(guild_id, norm, url, now)
            stats[outcome] += 1
        return stats

    async def _resync_one(
        self, guild_id: int, norm: str, url: str, now: datetime.datetime
    ) -> str:
        """Reload one claimed playlist; returns ``synced`` or ``failed``."""
        pool = self.bot.db_pool
        result = await self._fetch_source_tracks(url)
        if result.status in (UNAVAILABLE, TRANSIENT):
            await reschedule(pool, guild_id, norm, url, now + NODE_RETRY)
            return "failed"
        if result.status != OK:
            await record_failure(pool, guild_id, norm, url, result.status)
            return "failed"
        cap = self.bot.premium.for_guild(guild_id).max_playlist_tracks
        kept, _over = clip_result(result, cap)
        await record_success(
            pool, guild_id, norm, url, kept.blobs, kept.total_ms, discord.utils.utcnow()
        )
        return "synced"
