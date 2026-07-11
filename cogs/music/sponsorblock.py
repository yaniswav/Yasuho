"""Configure and observe the SponsorBlock Lavalink plugin on music players.

The SponsorBlock plugin (loaded on the Lavalink node) can auto-skip non-music
segments of YouTube videos - sponsor reads, self-promo, intros/outros and the
like. It only acts on a player once that player has been told which segment
*categories* to skip, through one per-player REST call:

    PUT /v4/sessions/{sessionId}/players/{guildId}/sponsorblock/categories

This module owns that single concern. It PUTs the default category set at every
player birth (best-effort, never blocking playback) and offers a thin logger for
the plugin's websocket telemetry. Everything here is duck-typed against the
sonolink ``player``/``node`` objects and reuses ``node.send`` (the node's own
credentialed HTTP client), so it never handles credentials and imports cleanly
under the stubbed sonolink used on the dev box (no sonolink internals imported).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


# The segment categories the plugin skips automatically. This is deliberately
# everything the plugin offers EXCEPT ``filler``: filler ("tangents/jokes/other
# non-essential content") marks real content the uploader chose to include, and
# dropping it cuts into the video the user actually asked to hear. The other
# seven categories are all non-content interruptions - paid promotion, self
# promotion, interaction reminders ("like and subscribe"), intro/outro/preview
# bumpers, and off-topic music-video chatter - safe to skip for a listener.
DEFAULT_CATEGORIES = (
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "music_offtopic",
)

# Seconds to wait before the single retry after a 404 (see apply_categories).
# A fresh connect PATCHes the player onto the node asynchronously (from the voice
# server update), so the very first PUT can briefly race ahead of that and 404.
# One short wait lets the player appear before the retry.
_RETRY_DELAY = 0.5

# SponsorBlock plugin websocket event types (Lavalink ``event`` op) that
# log_ws_event recognises. Anything else is ignored: the unknown_event listener
# fires for every event type sonolink does not model, not only these.
_WS_EVENT_TYPES = frozenset(
    {"SegmentsLoaded", "SegmentSkipped", "ChaptersLoaded", "ChapterStarted"}
)

# Strong references to in-flight fire-and-forget tasks. asyncio keeps only a weak
# reference to a bare task, so without this a scheduled PUT could be garbage
# collected mid-flight; the done-callback discards each task as it finishes.
_pending: set[asyncio.Task[Any]] = set()


def categories_path(session_id: str, guild_id: int) -> str:
    """Return the plugin's per-player categories REST path (pure).

    The leading slash matters: sonolink's REST client prefixes ``/v4`` only to
    paths that start with one, yielding the full
    ``/v4/sessions/{sessionId}/players/{guildId}/sponsorblock/categories``.
    """
    return f"/sessions/{session_id}/players/{guild_id}/sponsorblock/categories"


async def apply_categories(
    player: Any,
    *,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    retry_delay: float = _RETRY_DELAY,
) -> bool:
    """PUT ``categories`` for ``player`` via sonolink's authenticated REST seam.

    Best-effort: any failure is logged once at debug and never propagates, so
    SponsorBlock can never break playback. Returns True only when the PUT was
    accepted. Reuses ``node.send`` (the node's credentialed HTTP client), so no
    credentials are handled - or logged - here.

    The PUT needs the player to already exist on the node; a brand-new connect
    PATCHes it there asynchronously, so the first attempt can 404. On a 404 we
    wait ``retry_delay`` seconds and try exactly once more, then give up quietly.
    """
    node = _node_of(player)
    if node is None:
        return False
    try:
        session_id = node.session_id
    except Exception:
        # Node not connected yet / no session id - nothing we can do.
        return False
    guild_id = _guild_id_of(player)
    if guild_id is None:
        return False

    path = categories_path(session_id, guild_id)
    body = list(categories)

    for attempt in (1, 2):
        try:
            await node.send("PUT", path, json=body)
            return True
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404 and attempt == 1:
                await asyncio.sleep(retry_delay)
                continue
            log.debug(
                "SponsorBlock: could not set categories for guild %s (%s)",
                guild_id,
                exc,
            )
            return False
    return False


def schedule_apply(
    player: Any, *, categories: tuple[str, ...] = DEFAULT_CATEGORIES
) -> asyncio.Task[bool] | None:
    """Fire ``apply_categories`` in the background, returning the task (or None).

    Player birth happens on latency-sensitive command paths, and the 404 retry
    can wait up to ``_RETRY_DELAY`` seconds, so this must never block the caller.
    Returns None when there is no running loop (there always is inside the cog).
    The loop is checked *before* the coroutine is built, so the no-loop path never
    leaves an un-awaited coroutine behind.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(apply_categories(player, categories=categories))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def log_ws_event(player: Any, data: dict[str, Any]) -> None:
    """Log a SponsorBlock plugin websocket event at debug (instrumentation only).

    Bound to ``on_sonolink_unknown_event``, which sonolink dispatches for every
    event type it does not model (the plugin's SegmentsLoaded / SegmentSkipped /
    Chapter* events included - see the catch-all in sonolink's player event
    handler). Non-SponsorBlock event types are ignored; nothing here is
    user-facing and playback is never affected.
    """
    event_type = data.get("type")
    if event_type not in _WS_EVENT_TYPES:
        return
    guild_id = _guild_id_of(player)
    if event_type == "SegmentSkipped":
        segment = data.get("segment") or {}
        log.debug(
            "SponsorBlock skipped a %s segment (%s-%s ms) in guild %s",
            segment.get("category"),
            segment.get("start"),
            segment.get("end"),
            guild_id,
        )
    else:
        log.debug("SponsorBlock %s in guild %s", event_type, guild_id)


def _node_of(player: Any) -> Any:
    """Return the player's node, or None if it is not attached yet.

    ``player.node`` raises when the player has no node bound, so this normalises
    that to None for the best-effort callers.
    """
    try:
        return player.node
    except Exception:
        return None


def _guild_id_of(player: Any) -> int | None:
    """Return the player's guild id, or None if it cannot be resolved."""
    guild = getattr(player, "guild", None)
    return getattr(guild, "id", None)
