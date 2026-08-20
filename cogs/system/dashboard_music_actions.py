"""Dashboard -> bot action queue: the five executors that drive the LIVE player.

Companion module to ``cogs/system/dashboard_actions.py``: that module owns the
queue itself (the dedicated LISTEN connection, the atomic claim, the boot
reconciliation and the result write-back), this one owns the ``music_*``
executors it dispatches to. Same handler contract - ``async
handler(bot, guild_id, payload) -> result dict`` - and the registry stays
SINGLE-SOURCED: ``dashboard_actions`` merges the :data:`EXECUTORS` mapping below
into its own ``_EXECUTORS`` at import time, so there is exactly one kind table.
The dependency only ever points this way (this module never imports
``dashboard_actions``), so no cycle is possible.

Why a separate module: the music executors need the music package's seams, and
``dashboard_actions`` is already 1400 lines of queue mechanics. Splitting by
CONCERN keeps both readable.

Design:

* NEVER reimplement playback logic. Every executor drives the SAME seam the
  bot's own command / controller-button does - ``Player.pause`` / ``resume`` /
  ``set_volume`` / ``stop(clear_queue=True)`` and the cog's ``_execute_skip``,
  ``_snapshot`` and ``_clear`` - so a dashboard pause is indistinguishable from a
  ``/pause``: the same Lavalink call, the same persisted snapshot, the same
  now-playing panel refresh, the same teardown of the lyrics session / skip vote
  / effect-ceiling slot on stop.
* PRIVILEGED by construction. The dashboard writes an action row only under its
  ``requireManageGuild`` gate, so the actor always holds Manage Server - which
  passes ``effects.can_control_playback`` (the DJ lock) and
  ``effects.is_effect_exempt`` (the vote-skip exemption) unconditionally. A
  ``music_skip`` is therefore a privileged DIRECT skip, exactly like a DJ's
  ``/skip``, and never opens a public vote. Same-voice - the other half of the
  in-Discord gate - is inapplicable: the actor is not in the voice channel at
  all, which is the whole point of a remote control.
* The payload is NEVER trusted. Only ``guild_id`` is authoritative (it comes
  from the claimed row); the volume is re-validated against the BOT's own
  command bounds, and liveness is re-read from the live player on every run -
  INSIDE the per-guild lock, see :data:`_MUSIC_LOCKS` - because the dashboard
  renders from the ``music_state`` snapshot, which can be minutes stale.
* Results are machine-facing: short, stable, NEVER localised identifiers, and
  never a stack or a secret.

Failure-key note for the Node side: these five executors report a failure as
``{"ok": false, "reason": "<code>"}`` (the shape the dashboard team specified),
while the QUEUE's own failure paths in ``dashboard_actions`` - ``unknown_kind``,
``internal_error``, ``expired`` - report under ``error``. A consumer should read
``result.reason ?? result.error``. See
``.claude/plans/dashboard-executors-contract-e.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from tools import i18n

log = logging.getLogger(__name__)


# The volume bounds the BOT itself enforces: the /volume command declares
# ``value: commands.Range[int, 0, 200]`` (cogs/music/music.py), so 0..200 is what
# a member can ask for in Discord and what the dashboard is held to as well.
# sonolink's own Player.set_volume accepts 0..1000 and raises ValueError outside
# it - deliberately NOT the bound used here: the dashboard must not be able to
# reach a level the bot's own command refuses.
MIN_VOLUME = 0
MAX_VOLUME = 200


# Per-guild serialisation of the music executors.
#
# Every notification is handled in its own task (``dashboard_actions._on_notify``
# creates one per notify, and the boot reconciliation can re-drive a row while a
# live notify handles another), so two music actions for the SAME guild can
# interleave. That is not harmless here: ``music_stop`` tears the session down
# (``Player.stop`` + ``Music._clear``), and an action that ACTS on a liveness
# snapshot taken before that teardown acts on a session that no longer exists -
# a pause that leaves sonolink's ``_paused`` flag set (the next ``play()``
# inherits it, so the next track of that session starts silent), or a skip that
# falls through to sonolink's autoplay and RESURRECTS playback with a
# recommendation right after an explicit stop, re-persisting ``music_state``
# through the ensuing track_start. Two pause / resume actions can likewise write
# snapshots that disagree with the live paused flag.
#
# The lock therefore does two things, and BOTH are needed: it serialises each
# guild's act -> persist sequence, and every executor re-reads
# :func:`_live_session` INSIDE it so the act is decided on state that cannot
# change under it. Reading liveness outside would keep the sequences atomic while
# still letting each one run against a stale verdict, which is the actual hazard.
#
# Deliberately NOT ``Music._controller_locks``: that one serialises controller
# POSTING and is held across Discord round trips on the track_start hot path.
#
# The mapping is unbounded on purpose, exactly like ``dashboard_actions``'s
# autoroom locks: one ``asyncio.Lock`` (a few hundred bytes, with no event-loop
# binding at all while uncontended) per guild that has ever run a dashboard music
# action - an operator-driven set far smaller than the guild count. A BoundedLRU
# would be worse than useless: evicting an entry while its lock is HELD hands the
# next caller a brand-new lock and silently destroys the mutual exclusion.
_MUSIC_LOCKS = defaultdict(asyncio.Lock)


def _player_cls():
    """Return sonolink's ``Player`` base class, imported lazily.

    Same rationale as ``dashboard_actions``'s lazy ``_verify_view_cls`` /
    ``_embed_creator`` seams: keeping the import inside the function means this
    module (and therefore the whole action registry) imports even where sonolink
    is absent or stubbed, instead of taking the queue down with it. The seam is
    also the monkeypatch point the executor tests use, so they can drive a fake
    player without a Lavalink node.
    """
    import sonolink

    return sonolink.Player


def _voteskip_module():
    """Return the ``cogs.music.voteskip`` module, imported lazily.

    Same rationale as :func:`_player_cls`: voteskip defines ``discord.ui.Button``
    / ``discord.ui.View`` subclasses at import time, so importing it eagerly
    would tie this module (and the whole action registry) to the 2.x UI stack.
    The skip executor reads its ``SKIP_RESULT_*`` constants from here rather than
    re-spelling the strings, so the two can never drift. Tests monkeypatch this
    seam.
    """
    from cogs.music import voteskip

    return voteskip


def _live_session(bot, guild_id):
    """Return ``(player, music_cog)`` for a LIVE session, or ``None``.

    THE single liveness definition behind all five kinds, so ``no_session``
    means exactly one thing on both sides of the queue. A session is live when:

    * the guild is in the gateway cache;
    * ``guild.voice_client`` is a sonolink ``Player`` - the isinstance check
      ``Music._require_player`` makes before every control command (a plain
      ``discord.VoiceClient`` connected by something else is NOT a music
      session);
    * the player still holds a voice channel (``player.channel`` - the guard
      ``Music._snapshot`` uses; a torn-down player has none);
    * something is actually loaded (``player.current`` - the guard ``/music seek``
      uses, and the SAME condition under which ``Music._snapshot`` persists a
      ``music_state`` row at all). This is what keeps the two sides in step:
      the dashboard's music panel is rendered FROM that row, so with no current
      track there is no row, and the panel the operator clicked was showing a
      session the bot no longer has;
    * the ``Music`` cog is loaded - it owns every seam these executors reuse
      (``_snapshot``, ``_execute_skip``, ``_clear``, the skip-vote registry), so
      an unmanaged player is not a session this queue can act on. Reported with
      the SAME code rather than inventing one the dashboard cannot map (the
      ``_exec_autoroom_hub_create`` precedent).

    Returns ``None`` for every one of those states; the caller maps it to the
    single ``no_session`` reason.

    ALWAYS called from inside the guild's :data:`_MUSIC_LOCKS` entry: a verdict
    taken before the lock can be invalidated by the action holding it (a
    ``music_stop`` tears the very session down), and acting on that stale verdict
    is exactly what the lock exists to prevent.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    player = getattr(guild, "voice_client", None)
    if player is None or not isinstance(player, _player_cls()):
        return None
    if getattr(player, "channel", None) is None:
        return None
    if getattr(player, "current", None) is None:
        return None
    cog = bot.get_cog("Music")
    if cog is None:
        return None
    return player, cog


async def _refresh_controller(bot, guild_id, player):
    """Re-render the now-playing panel in place, best-effort.

    The controller buttons re-render right after they act (see
    ``MusicController._pause_resume`` / ``_volume_down`` / ``_volume_up``), and a
    dashboard action is the same thing done remotely: the panel is the ONLY
    feedback the room gets, since the actor is not in the channel to see an
    ephemeral ack. Rendered under the guild's configured language - this runs on
    a background queue task with no interaction context, so without an explicit
    locale the panel would come back in the default one (the
    ``_exec_autoroom_hub_create`` discipline).

    Never raises and never affects the executor's result: a stale panel is
    cosmetic, and the 60 s idle tick refreshes it anyway. A no-op when the player
    has no controller bound, when its message is gone, or when nothing is playing
    (``_rerender`` itself stands down on both).
    """
    controller = getattr(player, "controller", None)
    if controller is None:
        return
    try:
        loc = await i18n.resolve_guild_locale(bot, bot.get_guild(guild_id))
        with i18n.locale(loc):
            await controller._rerender()
    except Exception:
        log.exception(
            "dashboard_music_actions: failed to refresh the controller for guild %s",
            guild_id,
        )


async def _exec_music_pause(bot, guild_id, payload):
    """Pause the live player. Payload: ``{}``.

    Drives ``Player.pause()`` then ``Music._snapshot`` - the exact sequence of
    the ``/pause`` command and the controller's pause button, in that order and
    for the same reason: the persisted ``paused`` flag drives the restore
    position maths, so waiting for the 60 s idle tick would let a restart resume
    playing (at a wrongly advanced position) in a room everyone expected to stay
    silent. The panel is then re-rendered like the button does.

    IDEMPOTENT: pausing an already-paused player is a SUCCESS that reports the
    current state (``{"ok": true, "paused": true}``) and performs no side effect
    at all - no Lavalink call, no snapshot write. The dashboard renders from a
    snapshot that can be stale, so "pause" arriving for a player someone already
    paused in Discord is a normal race, not an error.
    """
    async with _MUSIC_LOCKS[guild_id]:
        session = _live_session(bot, guild_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        player, cog = session
        if player.paused:
            return {"ok": True, "paused": True}
        await player.pause()
        await cog._snapshot(player)
    await _refresh_controller(bot, guild_id, player)
    return {"ok": True, "paused": True}


async def _exec_music_resume(bot, guild_id, payload):
    """Resume the live player. Payload: ``{}``.

    The mirror of :func:`_exec_music_pause`: ``Player.resume()`` then
    ``Music._snapshot`` (the ``/resume`` command's own order, for the same
    restore-maths reason), then the panel refresh the controller button does.

    IDEMPOTENT: resuming a player that is already playing is a SUCCESS reporting
    the current state (``{"ok": true, "paused": false}``) with no side effect.
    """
    async with _MUSIC_LOCKS[guild_id]:
        session = _live_session(bot, guild_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        player, cog = session
        if not player.paused:
            return {"ok": True, "paused": False}
        await player.resume()
        await cog._snapshot(player)
    await _refresh_controller(bot, guild_id, player)
    return {"ok": True, "paused": False}


async def _exec_music_skip(bot, guild_id, payload):
    """Skip the current track. Payload: ``{}``.

    Routes through ``Music._execute_skip`` - the SHARED skip engine behind
    ``/skip``, the controller's skip button and a passed vote - so the
    ``can_skip`` pre-check (sonolink STOPS the player before raising QueueEmpty,
    which would silence the room on the last track), the QueueEmpty catch and the
    ``_clear`` teardown when the skip empties the queue are all the bot's own,
    not a reimplementation.

    This is a PRIVILEGED DIRECT skip and never opens a vote: the action row was
    written under the dashboard's Manage-Guild gate, and a Manage-Server actor is
    exempt from vote-skip anyway (``voteskip.skip_mode(exempt=True)`` via
    ``Music._skip_exempt`` -> ``effects.is_effect_exempt``).

    A LIVE vote for the outgoing track is resolved through the registry's own
    ``SkipVotes.notify_track`` - the identical call the ``track_start`` listener
    makes - so the public vote message finalises with its usual "This track
    already ended." instead of lingering to its 30 s timeout. It is track-aware
    (a vote for the track that is still playing is kept) and idempotent, so the
    real ``track_start`` landing a moment later simply finds nothing to do. The
    queue-emptied path needs nothing: ``_execute_skip`` already routed through
    ``Music._clear``, which clears the vote (and the lyrics session, and the
    effect-ceiling slot). That teardown re-renders translated Discord text (the
    live-lyrics card, the vote message), so the engine call runs under the guild's
    language - a queue task carries none. No controller refresh either - the skip
    fires a ``track_start`` which posts / re-renders the panel through the normal
    event path, exactly as the command and the button rely on.

    The vote resolution runs AFTER the lock is released: it is a Discord message
    edit, and this lock must not be held across a round trip (the reason it is not
    ``Music._controller_locks``). Nothing else in this guild can invalidate it -
    the vote registry is track-keyed and idempotent, so a concurrent stop simply
    finds the vote already gone.

    ``skipped`` is false when there was nowhere to land (empty queue, no loop, no
    autoplay): the request was valid and playback was deliberately left
    UNTOUCHED, so it is a success, not a failure. ``ended`` is true when the skip
    emptied the queue and the session state was cleared.
    """
    voteskip = _voteskip_module()
    loc = await i18n.resolve_guild_locale(bot, bot.get_guild(guild_id))

    async with _MUSIC_LOCKS[guild_id]:
        session = _live_session(bot, guild_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        player, cog = session
        with i18n.locale(loc):
            result, track = await cog._execute_skip(player)

    if result == voteskip.SKIP_RESULT_NONE:
        return {"ok": True, "skipped": False, "ended": False}
    if result == voteskip.SKIP_RESULT_ADVANCED:
        await _resolve_skip_vote(cog, guild_id, track, loc)
        return {"ok": True, "skipped": True, "ended": False}
    # SKIP_RESULT_ENDED: the skip emptied the queue and _execute_skip already
    # tore the session down through Music._clear (vote included).
    return {"ok": True, "skipped": True, "ended": True}


async def _resolve_skip_vote(cog, guild_id, track, loc):
    """Finalise a live skip vote whose track this skip just replaced. Best-effort.

    Delegates to the registry API (``SkipVotes.notify_track``) rather than
    touching a vote's internals, under ``loc`` - the guild's language, resolved
    once by the caller - so the finalised message is not left in the default
    locale. Never raises: a vote message that fails to finalise here still times
    out on its own 30 s view timeout, and must never fail a skip that already
    happened.
    """
    votes = getattr(cog, "skip_votes", None)
    if votes is None:
        return
    try:
        with i18n.locale(loc):
            await votes.notify_track(guild_id, getattr(track, "identifier", None))
    except Exception:
        log.exception(
            "dashboard_music_actions: failed to finalise the skip vote for guild %s",
            guild_id,
        )


def _coerce_volume(raw):
    """Return the payload's volume as an in-range int, or ``None``. Never raises.

    The payload is untrusted, so both the TYPE and the RANGE are re-checked here:
    a bool is rejected outright (a stray ``True`` must never read as 1, the
    ``_exec_autoroom_hub_create`` precedent), an int is taken as-is, a decimal
    STRING is accepted because the dashboard serialises numbers that way, and
    everything else (float, null, list, ...) is refused rather than truncated.
    The bound is the bot's own :data:`MIN_VOLUME`..:data:`MAX_VOLUME`.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not MIN_VOLUME <= value <= MAX_VOLUME:
        return None
    return value


async def _exec_music_volume(bot, guild_id, payload):
    """Set the player volume. Payload: ``{"volume": <int 0..200>}``.

    The value is validated BEFORE the session is touched, against the bounds the
    ``/volume`` command itself declares, and a bad type or an out-of-range level
    both come back as ``invalid_volume`` carrying ``min`` / ``max`` so the
    dashboard can render the real bound instead of hard-coding it.

    Then it drives ``Player.set_volume`` and re-renders the panel - the
    controller's volume buttons' exact sequence. No snapshot: neither the command
    nor the buttons write one for a volume change (the 60 s idle tick refreshes
    the persisted volume), and the executors deliberately do not diverge.
    """
    volume = _coerce_volume(payload.get("volume"))
    if volume is None:
        return {
            "ok": False,
            "reason": "invalid_volume",
            "min": MIN_VOLUME,
            "max": MAX_VOLUME,
        }

    async with _MUSIC_LOCKS[guild_id]:
        session = _live_session(bot, guild_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        player, _cog = session
        await player.set_volume(volume)
    await _refresh_controller(bot, guild_id, player)
    return {"ok": True, "volume": volume}


async def _exec_music_stop(bot, guild_id, payload):
    """Stop playback and clear the queue, staying connected. Payload: ``{}``.

    ``Player.stop(clear_queue=True)`` then ``Music._clear`` - byte for byte the
    ``/stop`` command. Both halves matter: ``clear_queue=True`` also resets the
    queue MODE to NORMAL, so a looping session cannot immediately restart itself,
    and stopping with the queue cleared leaves sonolink's track-end handler
    nothing to start next (its autoplay / ``can_start_next`` path only fires on a
    natural end with something to advance to, never on an explicit stop). The cog
    seam is what makes a dashboard stop a real teardown rather than a silence:
    it drops the persisted ``music_state`` row, ends a live synced-lyrics
    session, cancels a live skip vote and releases the guild's effect-ceiling
    slot - all idempotent.

    That teardown re-renders translated Discord text - the live-lyrics card is
    finalised in place and the skip-vote message is cancelled with its own notice -
    so it runs under the guild's configured language: a queue task carries no
    locale, and without one a fr / es / ja / el guild would watch both flip to the
    default one (the ``_exec_autoroom_hub_create`` discipline, resolved before the
    lock is taken).

    No controller refresh: with nothing playing the panel's own ``_rerender``
    stands down anyway, exactly as after a ``/stop``.
    """
    loc = await i18n.resolve_guild_locale(bot, bot.get_guild(guild_id))

    async with _MUSIC_LOCKS[guild_id]:
        session = _live_session(bot, guild_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        player, cog = session
        await player.stop(clear_queue=True)
        with i18n.locale(loc):
            await cog._clear(guild_id)
    return {"ok": True}


# Merged into ``dashboard_actions._EXECUTORS`` at import time, so the queue keeps
# ONE kind table and this module stays the single owner of the music kinds.
EXECUTORS = {
    "music_pause": _exec_music_pause,
    "music_resume": _exec_music_resume,
    "music_skip": _exec_music_skip,
    "music_volume": _exec_music_volume,
    "music_stop": _exec_music_stop,
}
