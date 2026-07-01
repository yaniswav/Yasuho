"""Persistence for live music players so playback survives a bot restart.

The music cog keeps everything in memory (the queue, volume, loop mode, the DJ,
the home channel). This module mirrors the essentials into the ``music_state``
table so a restarting process can pick playback back up: one row per guild with
an active player, cleared on disconnect.

Everything here is pure data plumbing - it imports no sonolink types, so it is
unit-testable without a Lavalink backend. Turning a live player into a row (which
touches sonolink objects) and turning a row back into playback both live in the
cog; this module only reads/writes the table and does the position maths.

Every write is best-effort: a persistence hiccup must never break playback, so
the DB helpers swallow and log their errors rather than propagate.
"""

import logging

log = logging.getLogger(__name__)

# Stable node id, shared by core.py (which creates the node) and the cog, so a
# restart can look up and resume the previous Lavalink session by this key.
MUSIC_NODE_ID = "yasuho-main"

# loop_mode column values (kept in sync with the cog's QueueMode mapping).
LOOP_OFF = 0
LOOP_TRACK = 1
LOOP_QUEUE = 2


def extrapolate_position(position_ms, updated_at, now, *, paused, length_ms=None):
    """Best-effort current playback position (ms) at restore time.

    ``position_ms`` was the reported position when the row was written at
    ``updated_at``. A track that was playing has advanced by the elapsed
    wall-clock time; a paused one has not moved. The result is clamped to
    ``[0, length_ms]`` when a length is known, so a slightly stale snapshot can
    never seek past the end of the track.
    """
    position = max(0, int(position_ms or 0))
    if not paused:
        elapsed_ms = max(0.0, (now - updated_at).total_seconds() * 1000)
        position += int(elapsed_ms)
    if length_ms:
        position = min(position, int(length_ms))
    return position


async def save_state(
    pool,
    *,
    guild_id,
    voice_channel_id,
    home_channel_id,
    dj_id,
    volume,
    loop_mode,
    position_ms,
    paused,
    current_track,
    queue,
    controller_message_id=None,
):
    """Upsert one guild's player state (best-effort; never raises)."""
    try:
        await pool.execute(
            """
            INSERT INTO music_state (
                guild_id, voice_channel_id, home_channel_id, dj_id, volume,
                loop_mode, position_ms, paused, current_track, queue,
                controller_message_id, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
            ON CONFLICT (guild_id) DO UPDATE SET
                voice_channel_id      = EXCLUDED.voice_channel_id,
                home_channel_id       = EXCLUDED.home_channel_id,
                dj_id                 = EXCLUDED.dj_id,
                volume                = EXCLUDED.volume,
                loop_mode             = EXCLUDED.loop_mode,
                position_ms           = EXCLUDED.position_ms,
                paused                = EXCLUDED.paused,
                current_track         = EXCLUDED.current_track,
                queue                 = EXCLUDED.queue,
                controller_message_id = EXCLUDED.controller_message_id,
                updated_at            = now()
            """,
            guild_id,
            voice_channel_id,
            home_channel_id,
            dj_id,
            volume,
            loop_mode,
            position_ms,
            paused,
            current_track,
            queue,
            controller_message_id,
        )
    except Exception:
        log.exception("Failed to persist music state for guild %s", guild_id)


async def save_session(pool, node_id, session_id):
    """Persist a node's Lavalink session id for resume on the next start."""
    if not node_id or not session_id:
        return
    try:
        await pool.execute(
            """
            INSERT INTO music_node_session (node_id, session_id, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (node_id) DO UPDATE SET
                session_id = EXCLUDED.session_id, updated_at = now()
            """,
            node_id,
            session_id,
        )
    except Exception:
        log.exception("Failed to persist Lavalink session for node %s", node_id)


async def load_session(pool, node_id):
    """Return the last saved Lavalink session id for a node, or None."""
    try:
        return await pool.fetchval(
            "SELECT session_id FROM music_node_session WHERE node_id = $1", node_id
        )
    except Exception:
        log.exception("Failed to load Lavalink session for node %s", node_id)
        return None


async def load_all_states(pool):
    """Return every persisted player-state row (empty list on error)."""
    try:
        return await pool.fetch("SELECT * FROM music_state")
    except Exception:
        log.exception("Failed to load music state")
        return []


async def clear_state(pool, guild_id):
    """Drop a guild's persisted player state (best-effort)."""
    try:
        await pool.execute("DELETE FROM music_state WHERE guild_id = $1", guild_id)
    except Exception:
        log.exception("Failed to clear music state for guild %s", guild_id)
