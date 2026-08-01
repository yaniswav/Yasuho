"""Unit tests for the dashboard->bot music executors (lot E).

The five ``music_*`` kinds act on the LIVE sonolink player from a background
queue task, so what has to be pinned is (a) that each one drives the music cog's
OWN seam rather than a private reimplementation - a dashboard pause must be
indistinguishable from a ``/pause`` - (b) that a stale dashboard (its panel is
rendered from the ``music_state`` snapshot, which can be minutes old) can never
produce anything worse than a clean ``no_session``, and (c) that the exact
result shapes the Node side contracts on do not drift.

Everything runs against in-memory fakes: a fake player recording the sonolink
calls, a fake Music cog recording the seam calls, and the REAL
``cogs.music.voteskip`` registry (its ``SkipVote`` builds no Discord object in
``__init__``), so no Lavalink node, no gateway and no database are involved. The
``_player_cls`` seam is monkeypatched so the executors' isinstance guard accepts
the fake player - the same lazy-seam trick the verify / buttonroles executor
tests use.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cogs.music import voteskip
from cogs.system import dashboard_actions
from cogs.system import dashboard_music_actions as dma
from tools import i18n, settings

GUILD_ID = 100


# ---------------------------------------------------------------------------
# Fakes: the live player, the Music cog seams, the guild and the bot.
# ---------------------------------------------------------------------------


class FakeTrack:
    def __init__(self, identifier="track-1", title="Song", author="Someone"):
        self.identifier = identifier
        self.title = title
        self.author = author
        self.encoded = "enc-" + identifier


class FakeController:
    """Stand-in for MusicController: records each in-place re-render + its locale."""

    def __init__(self, message=object()):
        self.message = message
        self.renders = []

    async def _rerender(self):
        self.renders.append(i18n.current_locale.get())


_UNSET = object()


class FakePlayer:
    """The sonolink player surface the executors touch, with call recording.

    ``current`` defaults to a loaded track (a live session); pass ``None``
    explicitly for a player with nothing playing.
    """

    def __init__(
        self,
        *,
        paused=False,
        current=_UNSET,
        channel=object(),
        controller=None,
        volume=100,
    ):
        self.paused = paused
        self.current = FakeTrack() if current is _UNSET else current
        self.channel = channel
        self.controller = controller
        self.volume = volume
        self.calls = []

    async def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    async def resume(self):
        self.calls.append(("resume",))
        self.paused = False

    async def set_volume(self, value):
        self.calls.append(("set_volume", value))
        self.volume = value

    async def stop(self, *, clear_queue=False, clear_history=False):
        self.calls.append(("stop", clear_queue, clear_history))
        self.current = None


class FakeMusicCog:
    """The Music cog seams the executors reuse, recording every call.

    ``_execute_skip`` is seeded with the outcome the real shared skip engine
    would return, so the executor is tested against the engine's CONTRACT
    (advanced / nothing-to-skip / queue-emptied) rather than against sonolink.
    """

    def __init__(self, skip_result=None, skip_track=None, votes=None):
        self.snapshots = []
        self.cleared = []
        self.skips = []
        # Both seams re-render translated Discord text (the live-lyrics card, the
        # vote message), so the locale they run under is part of their contract.
        self.clear_locales = []
        self.skip_locales = []
        self._skip_result = skip_result or voteskip.SKIP_RESULT_ADVANCED
        self._skip_track = skip_track if skip_track is not None else FakeTrack("next-1")
        self.skip_votes = votes if votes is not None else voteskip.SkipVotes()

    async def _snapshot(self, player, track=None):
        self.snapshots.append(player)

    async def _clear(self, guild_id):
        self.cleared.append(guild_id)
        self.clear_locales.append(i18n.current_locale.get())

    async def _execute_skip(self, player):
        self.skips.append(player)
        self.skip_locales.append(i18n.current_locale.get())
        if self._skip_result == voteskip.SKIP_RESULT_ADVANCED:
            return self._skip_result, self._skip_track
        if self._skip_result == voteskip.SKIP_RESULT_ENDED:
            await self._clear(GUILD_ID)
            return self._skip_result, None
        return voteskip.SKIP_RESULT_NONE, None


class FakeGuild:
    def __init__(self, guild_id=GUILD_ID, voice_client=None, preferred_locale="en"):
        self.id = guild_id
        self.voice_client = voice_client
        self.preferred_locale = preferred_locale


class MusicPool:
    """Minimal pool: the settings lookup behind resolve_guild_locale, plus the
    claim / finish pair so one end-to-end handle_action flow can be exercised."""

    def __init__(self):
        self.rows = {}

    def add(self, action_id, kind, payload, guild_id=GUILD_ID):
        self.rows[action_id] = {
            "guild_id": guild_id,
            "kind": kind,
            "payload": payload,
            "status": "pending",
            "result": None,
        }

    async def fetchval(self, query, *args):
        return None  # no stored guild settings

    async def fetchrow(self, query, *args):
        if "WHERE id = $1 AND status = 'pending'" in query:
            row = self.rows.get(args[0])
            if row is None or row["status"] != "pending":
                return None
            row["status"] = "running"
            return {
                "guild_id": row["guild_id"],
                "kind": row["kind"],
                "payload": row["payload"],
            }
        raise AssertionError("unexpected fetchrow: %r" % query)  # pragma: no cover

    async def execute(self, query, *args):
        if "WHERE id = $3" in query:
            status, result_json, action_id = args
            row = self.rows.get(action_id)
            if row is not None:
                row["status"] = status
                row["result"] = json.loads(result_json)
            return "UPDATE 1"
        raise AssertionError("unexpected execute: %r" % query)  # pragma: no cover


class FakeBot:
    def __init__(self, guild=None, cog=None, pool=None):
        self.db_pool = pool or MusicPool()
        self._guilds = {} if guild is False else {GUILD_ID: guild}
        self._cogs = {} if cog is None else {"Music": cog}

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)

    def get_cog(self, name):
        return self._cogs.get(name)


@pytest.fixture(autouse=True)
def _accept_fake_player(monkeypatch):
    """The executors isinstance-guard the voice client against sonolink's Player;
    point that lazy seam at the fake so no node/gateway is needed."""
    monkeypatch.setattr(dma, "_player_cls", lambda: FakePlayer)


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Both module-global maps outlive a test: the per-guild lock map is keyed by
    guild (and a lock must never be reused across event loops) and the settings
    LRU is process-wide."""
    dma._MUSIC_LOCKS.clear()
    settings._cache.clear()
    yield
    dma._MUSIC_LOCKS.clear()
    settings._cache.clear()


def _env(player=None, cog=None, **guild_kwargs):
    """A live session: a guild whose voice_client is the player, + the Music cog."""
    player = FakePlayer() if player is None else player
    cog = FakeMusicCog() if cog is None else cog
    guild = FakeGuild(voice_client=player, **guild_kwargs)
    return FakeBot(guild=guild, cog=cog), player, cog


# ---------------------------------------------------------------------------
# music_pause / music_resume: the cog's own act -> snapshot -> re-render order,
# and idempotent success against a stale dashboard.
# ---------------------------------------------------------------------------


async def test_music_pause_pauses_snapshots_and_refreshes_the_panel():
    controller = FakeController()
    bot, player, cog = _env(FakePlayer(paused=False, controller=controller))

    result = await dma._exec_music_pause(bot, GUILD_ID, {})

    assert result == {"ok": True, "paused": True}
    # The sonolink seam, not a reimplementation.
    assert player.calls == [("pause",)]
    # Snapshot right away: the persisted paused flag drives the restore maths.
    assert cog.snapshots == [player]
    # And the panel is refreshed in place, like the controller button does.
    assert len(controller.renders) == 1


async def test_music_pause_on_an_already_paused_player_is_idempotent_success():
    """The dashboard renders from a snapshot that can be stale, so a pause for a
    player someone already paused in Discord is a race, not an error."""
    controller = FakeController()
    bot, player, cog = _env(FakePlayer(paused=True, controller=controller))

    result = await dma._exec_music_pause(bot, GUILD_ID, {})

    assert result == {"ok": True, "paused": True}  # current state, reported back
    assert player.calls == []  # no Lavalink call
    assert cog.snapshots == []  # no snapshot write
    assert controller.renders == []  # nothing changed on screen


async def test_music_resume_resumes_snapshots_and_refreshes_the_panel():
    controller = FakeController()
    bot, player, cog = _env(FakePlayer(paused=True, controller=controller))

    result = await dma._exec_music_resume(bot, GUILD_ID, {})

    assert result == {"ok": True, "paused": False}
    assert player.calls == [("resume",)]
    assert cog.snapshots == [player]
    assert len(controller.renders) == 1


async def test_music_resume_on_a_playing_player_is_idempotent_success():
    bot, player, cog = _env(FakePlayer(paused=False))

    result = await dma._exec_music_resume(bot, GUILD_ID, {})

    assert result == {"ok": True, "paused": False}
    assert player.calls == []
    assert cog.snapshots == []


async def test_pause_panel_refresh_uses_the_guild_locale(monkeypatch):
    """A queue task carries no locale, so the panel would otherwise be re-rendered
    in the default language for every guild."""

    async def _spy(bot, guild):
        return "fr"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _spy)
    controller = FakeController()
    bot, player, cog = _env(FakePlayer(paused=False, controller=controller))

    await dma._exec_music_pause(bot, GUILD_ID, {})

    assert controller.renders == ["fr"]
    # ... and the locale is scoped to the call, never leaked into the task.
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE


async def test_panel_refresh_failure_never_fails_the_action():
    class BrokenController(FakeController):
        async def _rerender(self):
            raise RuntimeError("edit failed")

    bot, player, cog = _env(FakePlayer(paused=False, controller=BrokenController()))

    result = await dma._exec_music_pause(bot, GUILD_ID, {})

    assert result == {"ok": True, "paused": True}
    assert player.calls == [("pause",)]  # the real work still happened


async def test_pause_without_a_controller_is_still_ok():
    bot, player, cog = _env(FakePlayer(paused=False, controller=None))

    assert await dma._exec_music_pause(bot, GUILD_ID, {}) == {"ok": True, "paused": True}


# ---------------------------------------------------------------------------
# music_skip: the shared engine, the privileged direct skip, the live vote.
# ---------------------------------------------------------------------------


async def test_music_skip_routes_through_the_shared_skip_engine():
    cog = FakeMusicCog(skip_result=voteskip.SKIP_RESULT_ADVANCED)
    bot, player, cog = _env(cog=cog)

    result = await dma._exec_music_skip(bot, GUILD_ID, {})

    assert result == {"ok": True, "skipped": True, "ended": False}
    # _execute_skip carries the can_skip pre-check + the QueueEmpty catch, so the
    # executor must never call player.skip() itself.
    assert cog.skips == [player]
    assert player.calls == []


async def test_music_skip_with_nowhere_to_land_is_a_success_that_skipped_nothing():
    """sonolink STOPS the player before raising QueueEmpty, so the engine refuses
    up front: playback is untouched, which is a valid outcome, not a failure."""
    cog = FakeMusicCog(skip_result=voteskip.SKIP_RESULT_NONE)
    bot, player, cog = _env(cog=cog)

    result = await dma._exec_music_skip(bot, GUILD_ID, {})

    assert result == {"ok": True, "skipped": False, "ended": False}
    assert cog.cleared == []  # nothing was torn down


async def test_music_skip_that_empties_the_queue_reports_ended():
    cog = FakeMusicCog(skip_result=voteskip.SKIP_RESULT_ENDED)
    bot, player, cog = _env(cog=cog)

    result = await dma._exec_music_skip(bot, GUILD_ID, {})

    assert result == {"ok": True, "skipped": True, "ended": True}
    # The engine's own teardown ran (music_state, lyrics, vote, effect slot).
    assert cog.cleared == [GUILD_ID]


def _live_vote(cog, player, track_id="track-1"):
    """Seed the REAL registry with a live vote for ``track_id``, no Discord I/O."""
    vote = voteskip.SkipVote(
        cog=cog,
        player=player,
        channel=None,
        track=FakeTrack(track_id),
        initiator=type("M", (), {"id": 7, "mention": "@m"})(),
        registry=cog.skip_votes,
        guild_id=GUILD_ID,
    )
    cog.skip_votes._put(GUILD_ID, vote)
    return vote


async def test_music_skip_resolves_a_live_vote_for_the_outgoing_track():
    """A privileged skip must finalise the public vote message at once instead of
    leaving it clickable until its 30 s timeout."""
    cog = FakeMusicCog(
        skip_result=voteskip.SKIP_RESULT_ADVANCED, skip_track=FakeTrack("next-1")
    )
    bot, player, cog = _env(cog=cog)
    vote = _live_vote(cog, player, track_id="track-1")

    result = await dma._exec_music_skip(bot, GUILD_ID, {})

    assert result["skipped"] is True
    assert vote.resolved is True
    assert cog.skip_votes.get(GUILD_ID) is None  # detached from the registry


async def test_music_skip_keeps_a_vote_whose_track_is_still_the_one_playing():
    """The registry API is track-aware, so a skip that somehow lands on the same
    identifier (a re-fire) must not cancel a vote that is still valid."""
    cog = FakeMusicCog(
        skip_result=voteskip.SKIP_RESULT_ADVANCED, skip_track=FakeTrack("track-1")
    )
    bot, player, cog = _env(cog=cog)
    vote = _live_vote(cog, player, track_id="track-1")

    await dma._exec_music_skip(bot, GUILD_ID, {})

    assert vote.resolved is False
    assert cog.skip_votes.get(GUILD_ID) is vote


async def test_music_skip_survives_a_vote_registry_failure():
    class BoomVotes(voteskip.SkipVotes):
        async def notify_track(self, guild_id, track_id):
            raise RuntimeError("registry blew up")

    cog = FakeMusicCog(votes=BoomVotes())
    bot, player, cog = _env(cog=cog)

    # The skip already happened; a vote-message hiccup must never fail it.
    assert await dma._exec_music_skip(bot, GUILD_ID, {}) == {
        "ok": True,
        "skipped": True,
        "ended": False,
    }


# ---------------------------------------------------------------------------
# music_volume: untrusted payload, the BOT's own bounds.
# ---------------------------------------------------------------------------


async def test_music_volume_sets_the_level_and_refreshes_the_panel():
    controller = FakeController()
    bot, player, cog = _env(FakePlayer(controller=controller))

    result = await dma._exec_music_volume(bot, GUILD_ID, {"volume": 150})

    assert result == {"ok": True, "volume": 150}
    assert player.calls == [("set_volume", 150)]
    assert len(controller.renders) == 1
    # Parity with /volume and the volume buttons: no snapshot for a level change.
    assert cog.snapshots == []


@pytest.mark.parametrize("raw", [0, 200, "0", "200", " 150 "])
async def test_music_volume_accepts_the_bounds_and_the_string_spelling(raw):
    bot, player, cog = _env()

    result = await dma._exec_music_volume(bot, GUILD_ID, {"volume": raw})

    assert result["ok"] is True
    assert result["volume"] == int(str(raw).strip())


@pytest.mark.parametrize(
    "raw",
    [
        True,  # a stray bool must never read as 1
        False,
        None,
        1.5,
        150.0,  # a float is refused rather than silently truncated
        "abc",
        "",
        "150%",
        [150],
        {"volume": 150},
    ],
)
async def test_music_volume_rejects_bad_types(raw):
    bot, player, cog = _env()

    result = await dma._exec_music_volume(bot, GUILD_ID, {"volume": raw})

    assert result == {
        "ok": False,
        "reason": "invalid_volume",
        "min": dma.MIN_VOLUME,
        "max": dma.MAX_VOLUME,
    }
    assert player.calls == []  # the session was never touched


@pytest.mark.parametrize("raw", [-1, 201, 1000, "-5", "999"])
async def test_music_volume_rejects_out_of_range_levels(raw):
    """The bound is the BOT's (/volume is Range[int, 0, 200]), not sonolink's
    0..1000 - the dashboard must not reach a level a member cannot ask for."""
    bot, player, cog = _env()

    result = await dma._exec_music_volume(bot, GUILD_ID, {"volume": raw})

    assert result["reason"] == "invalid_volume"
    assert (result["min"], result["max"]) == (0, 200)
    assert player.calls == []


async def test_music_volume_missing_key_is_invalid_volume():
    bot, player, cog = _env()

    assert (await dma._exec_music_volume(bot, GUILD_ID, {}))["reason"] == "invalid_volume"


async def test_music_volume_validates_before_looking_at_the_session():
    """A bad value is a bad value whether or not anything is playing, and the
    dashboard gets the precise reason instead of a misleading no_session."""
    bot = FakeBot(guild=False)

    result = await dma._exec_music_volume(bot, GUILD_ID, {"volume": "loud"})

    assert result["reason"] == "invalid_volume"


# ---------------------------------------------------------------------------
# music_stop: the /stop sequence, teardown included.
# ---------------------------------------------------------------------------


async def test_music_stop_stops_with_the_queue_cleared_and_tears_the_session_down():
    bot, player, cog = _env()

    result = await dma._exec_music_stop(bot, GUILD_ID, {})

    assert result == {"ok": True}
    # clear_queue=True also resets the queue MODE, so a looping session cannot
    # restart itself - byte for byte the /stop command.
    assert player.calls == [("stop", True, False)]
    # Music._clear drops music_state, the lyrics session, the vote and the
    # effect-ceiling slot, keyed by the AUTHORITATIVE guild id.
    assert cog.cleared == [GUILD_ID]


# ---------------------------------------------------------------------------
# no_session: the one liveness definition, shared by all five kinds.
# ---------------------------------------------------------------------------


ALL_EXECUTORS = [
    ("music_pause", {}),
    ("music_resume", {}),
    ("music_skip", {}),
    ("music_volume", {"volume": 100}),
    ("music_stop", {}),
]


def _dead_env(case):
    """Every state the music cog itself treats as "there is no session"."""
    cog = FakeMusicCog()
    if case == "no_guild":
        return FakeBot(guild=False, cog=cog)
    if case == "no_voice_client":
        return FakeBot(guild=FakeGuild(voice_client=None), cog=cog)
    if case == "foreign_voice_client":
        # A plain discord.VoiceClient connected by something else is NOT a music
        # session (the isinstance guard /pause makes).
        return FakeBot(guild=FakeGuild(voice_client=object()), cog=cog)
    if case == "no_channel":
        return FakeBot(guild=FakeGuild(voice_client=FakePlayer(channel=None)), cog=cog)
    if case == "nothing_playing":
        # No current track means no music_state row either, so the panel the
        # operator clicked was showing a session the bot no longer has.
        return FakeBot(guild=FakeGuild(voice_client=FakePlayer(current=None)), cog=cog)
    raise AssertionError(case)  # pragma: no cover


@pytest.mark.parametrize("kind, payload", ALL_EXECUTORS)
@pytest.mark.parametrize(
    "case",
    ["no_guild", "no_voice_client", "foreign_voice_client", "no_channel", "nothing_playing"],
)
async def test_every_executor_reports_no_session_when_nothing_is_live(
    kind, payload, case
):
    bot = _dead_env(case)

    result = await dma.EXECUTORS[kind](bot, GUILD_ID, payload)

    assert result == {"ok": False, "reason": "no_session"}


@pytest.mark.parametrize("kind, payload", ALL_EXECUTORS)
async def test_every_executor_reports_no_session_without_the_music_cog(kind, payload):
    """The cog owns every seam these executors reuse, so without it loaded the bot
    cannot act on the session at all."""
    bot = FakeBot(guild=FakeGuild(voice_client=FakePlayer()), cog=None)

    result = await dma.EXECUTORS[kind](bot, GUILD_ID, payload)

    assert result == {"ok": False, "reason": "no_session"}


# ---------------------------------------------------------------------------
# Concurrency: each notification is handled in its own task, so two actions for
# one guild can interleave.
# ---------------------------------------------------------------------------


async def test_pause_and_stop_for_one_guild_are_serialised():
    """Without the per-guild lock a pause's act -> persist sequence can interleave
    with a stop's teardown, so the two disagree about what the session is."""

    class SlowCog(FakeMusicCog):
        def __init__(self):
            super().__init__()
            self.order = []

        async def _snapshot(self, player, track=None):
            self.order.append("snapshot-start")
            await asyncio.sleep(0)  # the DB round trip's suspension point
            self.order.append("snapshot-end")
            await super()._snapshot(player, track)

        async def _clear(self, guild_id):
            self.order.append("clear")
            await super()._clear(guild_id)

    cog = SlowCog()
    bot, player, cog = _env(FakePlayer(paused=False), cog=cog)

    results = await asyncio.gather(
        dma._exec_music_pause(bot, GUILD_ID, {}),
        dma._exec_music_stop(bot, GUILD_ID, {}),
    )

    assert [r["ok"] for r in results] == [True, True]
    # The pause finished its persist BEFORE the stop's teardown began.
    assert cog.order == ["snapshot-start", "snapshot-end", "clear"]


def _gated_stop_env():
    """A live session plus a stop that PARKS inside the lock, its teardown already
    begun. Whatever is started afterwards can only run against the session the
    stop left behind - the state a liveness verdict taken before it would miss."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedCog(FakeMusicCog):
        async def _clear(self, guild_id):
            entered.set()
            await release.wait()
            await super()._clear(guild_id)

    bot, player, cog = _env(FakePlayer(paused=False), cog=GatedCog())
    return bot, player, cog, entered, release


async def test_a_pause_queued_behind_a_stop_reports_no_session():
    """Liveness must be read INSIDE the lock. Read outside, this pause acts on a
    verdict taken before the stop tore the session down: it would pause an
    already-stopped player, leaving sonolink's paused flag set for the next
    play() (so the next track of that session starts silent) and telling the
    dashboard the player is paused."""
    bot, player, cog, entered, release = _gated_stop_env()

    stop_task = asyncio.ensure_future(dma._exec_music_stop(bot, GUILD_ID, {}))
    await entered.wait()  # the stop holds the lock, playback already stopped
    pause_task = asyncio.ensure_future(dma._exec_music_pause(bot, GUILD_ID, {}))
    await asyncio.sleep(0)  # ... and the pause is queued behind it
    release.set()

    assert await stop_task == {"ok": True}
    assert await pause_task == {"ok": False, "reason": "no_session"}
    assert player.calls == [("stop", True, False)]  # no pause on a dead session
    assert cog.snapshots == []  # and no snapshot resurrecting music_state


async def test_a_skip_queued_behind_a_stop_reports_no_session():
    """The same hazard with worse teeth: can_skip is true whenever autoplay is
    armed (the default), so driving the skip engine on a stopped session would
    fetch a recommendation and RESURRECT playback right after an explicit stop."""
    bot, player, cog, entered, release = _gated_stop_env()

    stop_task = asyncio.ensure_future(dma._exec_music_stop(bot, GUILD_ID, {}))
    await entered.wait()
    skip_task = asyncio.ensure_future(dma._exec_music_skip(bot, GUILD_ID, {}))
    await asyncio.sleep(0)
    release.set()

    assert await stop_task == {"ok": True}
    assert await skip_task == {"ok": False, "reason": "no_session"}
    assert cog.skips == []  # the skip engine was never driven


async def test_the_skip_vote_is_finalised_outside_the_per_guild_lock():
    """Resolving the vote edits a Discord message; this lock must never be held
    across a round trip (the reason it is not Music._controller_locks)."""
    held = []

    class WatchingVotes(voteskip.SkipVotes):
        async def notify_track(self, guild_id, track_id):
            held.append(dma._MUSIC_LOCKS[guild_id].locked())
            return await super().notify_track(guild_id, track_id)

    cog = FakeMusicCog(votes=WatchingVotes())
    bot, player, cog = _env(cog=cog)

    await dma._exec_music_skip(bot, GUILD_ID, {})

    assert held == [False]


# ---------------------------------------------------------------------------
# Locale: the teardown seams re-render translated Discord text, and a queue task
# carries no locale of its own.
# ---------------------------------------------------------------------------


async def test_stop_teardown_runs_under_the_guild_locale(monkeypatch):
    """Music._clear finalises the live-lyrics card and cancels the skip vote, both
    fully translated - without an explicit locale a fr guild would see them flip
    to the default language."""

    async def _spy(bot, guild):
        return "fr"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _spy)
    bot, player, cog = _env()

    await dma._exec_music_stop(bot, GUILD_ID, {})

    assert cog.clear_locales == ["fr"]
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE  # scoped, never leaked


async def test_skip_teardown_runs_under_the_guild_locale(monkeypatch):
    """A skip that empties the queue routes through the SAME _clear, so the engine
    call carries the guild's language too."""

    async def _spy(bot, guild):
        return "ja"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _spy)
    cog = FakeMusicCog(skip_result=voteskip.SKIP_RESULT_ENDED)
    bot, player, cog = _env(cog=cog)

    result = await dma._exec_music_skip(bot, GUILD_ID, {})

    assert result == {"ok": True, "skipped": True, "ended": True}
    assert cog.skip_locales == ["ja"]
    assert cog.clear_locales == ["ja"]
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE


async def test_skip_vote_resolution_runs_under_the_guild_locale(monkeypatch):
    """The vote message finalises with a translated notice, and the caller resolves
    the locale ONCE for the whole executor."""
    seen = []

    async def _spy(bot, guild):
        seen.append(guild)
        return "es"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _spy)
    locales = []

    class WatchingVotes(voteskip.SkipVotes):
        async def notify_track(self, guild_id, track_id):
            locales.append(i18n.current_locale.get())
            return await super().notify_track(guild_id, track_id)

    cog = FakeMusicCog(votes=WatchingVotes())
    bot, player, cog = _env(cog=cog)

    await dma._exec_music_skip(bot, GUILD_ID, {})

    assert locales == ["es"]
    assert len(seen) == 1  # resolved once, reused for the teardown and the vote


async def test_actions_for_different_guilds_do_not_block_each_other():
    """The lock is per guild, so one guild's slow action never stalls another's."""
    started = []
    release = asyncio.Event()

    class GatedCog(FakeMusicCog):
        async def _snapshot(self, player, track=None):
            started.append(1)
            if len(started) == 1:
                await release.wait()
            await super()._snapshot(player, track)

    cog = GatedCog()
    player_a = FakePlayer(paused=False)
    player_b = FakePlayer(paused=False)
    bot = FakeBot(guild=FakeGuild(voice_client=player_a), cog=cog)
    bot._guilds[200] = FakeGuild(guild_id=200, voice_client=player_b)

    first = asyncio.ensure_future(dma._exec_music_pause(bot, GUILD_ID, {}))
    await asyncio.sleep(0)
    second = await dma._exec_music_pause(bot, 200, {})  # completes while #1 waits

    assert second == {"ok": True, "paused": True}
    release.set()
    assert await first == {"ok": True, "paused": True}


# ---------------------------------------------------------------------------
# Registry: the five kinds are merged into the ONE queue table, and the full
# claim -> dispatch -> result write-back path works end to end.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, _payload", ALL_EXECUTORS)
def test_music_executors_are_registered_in_the_queue(kind, _payload):
    assert kind in dashboard_actions._EXECUTORS
    assert dashboard_actions._EXECUTORS[kind] is dma.EXECUTORS[kind]


def test_music_kinds_do_not_shadow_an_existing_kind():
    """The merge must ADD kinds, never silently replace one of the others - stated
    as disjointness rather than a total, so adding a sixth kind on either side
    never fails this test for the wrong reason."""
    own_execs = {
        getattr(dashboard_actions, name)
        for name in dir(dashboard_actions)
        if name.startswith("_exec_")
    }
    assert own_execs  # sanity: the queue does own executors of its own
    # A music kind colliding with one of them would REPLACE it in the merge, so
    # that handler would drop out of the registry's values.
    assert own_execs <= set(dashboard_actions._EXECUTORS.values())
    assert set(dma.EXECUTORS) <= set(dashboard_actions._EXECUTORS)


async def test_music_pause_full_flow_via_handle_action():
    pool = MusicPool()
    pool.add(1, kind="music_pause", payload={})
    player = FakePlayer(paused=False)
    bot = FakeBot(guild=FakeGuild(voice_client=player), cog=FakeMusicCog(), pool=pool)

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"] == {"ok": True, "paused": True}
    assert player.calls == [("pause",)]


async def test_music_volume_failure_flows_back_as_failed_with_its_reason():
    pool = MusicPool()
    pool.add(1, kind="music_volume", payload={"volume": 9001})
    bot = FakeBot(guild=FakeGuild(voice_client=FakePlayer()), cog=FakeMusicCog(), pool=pool)

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "failed"
    assert pool.rows[1]["result"] == {
        "ok": False,
        "reason": "invalid_volume",
        "min": 0,
        "max": 200,
    }


async def test_music_results_are_json_safe():
    """The queue json.dumps() every result into the row, so no result may carry a
    non-serialisable object (a track, a player, a Discord model)."""
    bot, player, cog = _env()

    for kind, payload in ALL_EXECUTORS:
        result = await dma.EXECUTORS[kind](bot, GUILD_ID, payload)
        json.dumps(result)  # raises if anything unserialisable slipped in
