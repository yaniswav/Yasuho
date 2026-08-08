"""Tests for the opt-in presence sections (cogs/community/profile/presence.py).

This is the one profile section fed by data nobody typed, about people who
never asked, arriving on the loudest event the gateway has. So the guards here
are not "does it render nicely" but the four ways it could be harmful:

* CONSENT - nothing is collected, kept or shown without a marker row; opting
  out deletes the row, un-publishes the section AND erases what was still in
  memory; opting IN twice never wipes what was already collected;
* THE HOT PATH - the listener rejects a non-opted member with one set lookup
  and, provably, without ever suspending (the coroutine is driven by hand and
  must raise StopIteration on the first send), and the same member seen from
  three shared guilds is counted once, not three times;
* THE BOUNDS - the session map, the buffer, the per-tick write ceiling, the
  session-length cap and the payload's own top-N are each exercised at their
  edge, because every one of them is what stops a cosmetic feature from
  becoming a memory or database problem at 1000+ guilds;
* WHAT REACHES A CARD - a game name is third-party text drawn on somebody
  ELSE's profile, Spotify is read live and stored nowhere, and a section with
  nothing to say is OMITTED rather than rendered empty.

Offline throughout: the storage seam is monkeypatched or driven against the
conftest fake pool, so no database, no network and no gateway are involved.
"""

import asyncio
import datetime
import json
import types

import discord
import pytest

from conftest import Record

from cogs.community import usersettings
from cogs.community.profile import cog as profile_cog
from cogs.community.profile import presence, registry, views, visibility
from cogs.community.profile import storage as profile_storage
from cogs.community.profile.connectors import base, storage
from tools import privacy

OWNER = 111
FRIEND = 222
STRANGER = 333

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Guild:
    """Just enough guild for the member-cache seed: a cache and a query."""

    def __init__(self, cached=None):
        self.id = 9
        self._cached = set(cached or ())
        self.queries = []

    def get_member(self, user_id):
        return object() if user_id in self._cached else None

    async def query_members(self, **kwargs):
        self.queries.append(kwargs)
        return []


class _Ctx:
    def __init__(self, author_id=OWNER, guild=None):
        self.author = types.SimpleNamespace(id=author_id)
        self.guild = _Guild() if guild is None else guild
        self.interaction = None
        self.clean_prefix = "?"
        self.sends = []

    def typing(self, **kwargs):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


def _text(ctx):
    args, kwargs = ctx.sends[-1]
    return args[0] if args else kwargs.get("content")


class _Interaction:
    """Just enough interaction for the /mydata deletion button."""

    def __init__(self):
        self.edited = []
        self.followups = []
        self.response = types.SimpleNamespace(defer=self._defer)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kwargs):
        return None

    async def _send(self, *args, **kwargs):
        self.followups.append((args, kwargs))

    async def edit_original_response(self, **kwargs):
        self.edited.append(kwargs)


def _bot_with(collector):
    """A bot whose ``get_cog`` serves the presence collector, and nothing else."""
    return types.SimpleNamespace(
        db_pool=object(),
        get_cog=lambda name: collector if name == "ProfilePresence" else None,
    )


class _Member:
    def __init__(self, user_id=OWNER, name="Owner", activities=()):
        self.id = user_id
        self.display_name = name
        self.activities = list(activities)
        self.display_avatar = types.SimpleNamespace(url="https://cdn/avatar.png")


def _cog(pool=None):
    return presence.ProfilePresence(types.SimpleNamespace(db_pool=pool or object()))


def _spotify(title="The Mother We Share", artist="Chvrches", **overrides):
    data = {
        "details": title,
        "state": artist,
        "assets": {"large_image": "spotify:ab/cd", "large_text": "Every Open Eye"},
        "sync_id": "6Vjkc",
        "session_id": "s",
        "timestamps": {},
        "party": {},
    }
    data.update(overrides)
    return discord.Spotify(**data)


def _connection(name, payload=None, **extra):
    row = {
        "connector": name,
        "external_id": str(OWNER),
        "display_name": None,
        "linked_at": None,
        "last_refresh": None,
        "payload": payload or {},
    }
    row.update(extra)
    return row


def _run_without_awaiting(coro):
    """Drive a coroutine one step; it must finish without EVER suspending.

    This is what "zero awaitable created" means operationally: a coroutine
    that never awaits raises StopIteration on its very first ``send(None)``.
    If it suspends instead, ``send`` returns a value and this fails - which is
    the only honest way to assert that a listener on a hundreds-per-second
    event does no I/O at all.
    """
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise AssertionError("the presence listener suspended on an await")


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (list, dict)):
                yield from _walk(value)


def _texts(view):
    return [
        node["content"]
        for node in _walk(view.to_components())
        if node.get("type") == 10
    ]


class _Container:
    """A stand-in for the Components V2 container a renderer appends to."""

    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)


def _rendered(container):
    out = []
    for item in container.items:
        if isinstance(item, discord.ui.TextDisplay):
            out.append(item.content)
        else:
            for child in item.walk_children():
                if isinstance(child, discord.ui.TextDisplay):
                    out.append(child.content)
    return "\n".join(out)


BIG_BUDGET = views.SectionBudget(text=4000, components=30)


# ---------------------------------------------------------------------------
# The vocabulary is derived, never restated
# ---------------------------------------------------------------------------


def test_the_two_sections_come_from_the_framework_tuple():
    assert (presence.GAMING_SECTION, presence.SPOTIFY_SECTION) == base.PRESENCE_SECTIONS
    assert presence.GAMING_SECTION not in base.LINKABLE
    assert presence.SPOTIFY_SECTION not in base.LINKABLE
    # Both are real registry sections, so visibility and the panel already
    # know them - that is what makes the marker row enough.
    assert registry.is_known(presence.GAMING_SECTION)
    assert registry.is_known(presence.SPOTIFY_SECTION)


def test_both_sections_have_a_registered_renderer():
    assert views.SECTION_RENDERERS[presence.GAMING_SECTION] is presence._render_gaming
    assert views.SECTION_RENDERERS[presence.SPOTIFY_SECTION] is presence._render_spotify


# ---------------------------------------------------------------------------
# The hot listener
# ---------------------------------------------------------------------------


def test_a_member_who_never_opted_in_costs_one_lookup_and_no_await():
    cog = _cog()
    before = _Member(activities=[])
    after = _Member(activities=[discord.Game(name="Celeste")])

    _run_without_awaiting(cog.on_presence_update(before, after))

    assert cog._sessions == {}
    assert cog._buffer.is_empty
    assert cog._stats["events"] == 0


def test_an_opted_in_member_is_processed_without_awaiting_either():
    """The ACCEPTED path must be await-free too - it is still the gateway."""
    cog = _cog()
    cog._opted.add(OWNER)

    _run_without_awaiting(
        cog.on_presence_update(
            _Member(activities=[]), _Member(activities=[discord.Game(name="Celeste")])
        )
    )

    assert list(cog._sessions) == [(OWNER, "Celeste")]


def test_a_started_game_opens_a_session_and_a_stopped_one_credits_minutes():
    cog = _cog()
    cog._opted.add(OWNER)

    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=1000.0)
    assert cog._sessions == {(OWNER, "Celeste"): 1000.0}
    assert cog._buffer.is_empty

    cog._apply(OWNER, [discord.Game(name="Celeste")], [], now=1000.0 + 90 * 60)
    assert cog._sessions == {}
    pending, _dropped = cog._buffer.drain()
    assert pending == {OWNER: {"Celeste": 90 * 60}}


def test_only_playing_activities_count_as_games():
    """Streaming, a custom status and Spotify are not games."""
    cog = _cog()
    cog._opted.add(OWNER)

    cog._apply(
        OWNER,
        [],
        [
            discord.Streaming(name="Celeste", url="https://twitch.tv/x"),
            discord.CustomActivity(name="brb"),
            _spotify(),
        ],
        now=0.0,
    )

    assert cog._sessions == {}


def test_the_same_member_seen_from_three_guilds_is_counted_once():
    """discord.py dispatches one presence update PER SHARED GUILD.

    The session key carries no guild, so the second and third copies of the
    same "started Celeste" (and of the same "stopped") are no-ops rather than a
    tripled total. This is the whole multi-guild dedup rule.
    """
    cog = _cog()
    cog._opted.add(OWNER)
    playing = [discord.Game(name="Celeste")]

    for _guild in range(3):
        cog._apply(OWNER, [], playing, now=500.0)
    assert cog._sessions == {(OWNER, "Celeste"): 500.0}

    for _guild in range(3):
        cog._apply(OWNER, playing, [], now=500.0 + 600)

    pending, _dropped = cog._buffer.drain()
    assert pending == {OWNER: {"Celeste": 600}}


def test_a_state_change_that_is_not_a_game_edge_does_nothing():
    """Status, custom text and rich-presence details tick constantly."""
    cog = _cog()
    cog._opted.add(OWNER)
    playing = [discord.Game(name="Celeste")]
    cog._apply(OWNER, [], playing, now=0.0)

    cog._apply(OWNER, playing, playing, now=300.0)

    assert cog._sessions == {(OWNER, "Celeste"): 0.0}
    assert cog._buffer.is_empty


def test_a_session_longer_than_the_cap_is_dropped_by_the_sweep():
    """A day-old session is a missed end event, not 24h of play."""
    cog = _cog()
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)

    dropped = cog._sweep_sessions(presence.SESSION_MAX_SECONDS + 1)

    assert dropped == 1
    assert cog._sessions == {}
    assert cog._buffer.is_empty, "an expired session must credit nothing"


def test_the_end_path_still_caps_a_wildly_long_session():
    """Belt to the sweep's braces: a clock jump must not write a week."""
    cog = _cog()
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)

    cog._apply(OWNER, [discord.Game(name="Celeste")], [], now=7 * 24 * 3600)

    pending, _dropped = cog._buffer.drain()
    assert pending == {OWNER: {"Celeste": presence.SESSION_MAX_SECONDS}}


def test_the_session_map_is_hard_capped():
    cog = _cog()
    cog._opted.add(OWNER)
    for index in range(presence.SESSION_CAP + 25):
        cog._sessions[(OWNER, "game-%d" % index)] = 0.0
    del cog._sessions[(OWNER, "game-0")]  # room for exactly one more

    cog._apply(OWNER, [], [discord.Game(name="fresh-1")], now=1.0)
    cog._apply(OWNER, [], [discord.Game(name="fresh-2")], now=1.0)

    assert len(cog._sessions) == presence.SESSION_CAP + 24
    assert cog._stats["dropped"] >= 1


def test_a_game_name_is_flattened_and_clipped_at_the_parse():
    """A rich presence can say anything, including a fake card heading."""
    hostile = "## Gaming IDs\n@everyone " + "x" * 500
    names = presence.playing_names([discord.Game(name=hostile)])
    (name,) = names
    assert "\n" not in name
    assert len(name) == presence.GAME_NAME_MAX


def test_a_blank_game_name_is_ignored():
    assert presence.playing_names([discord.Game(name="   ")]) == set()


# ---------------------------------------------------------------------------
# The buffer
# ---------------------------------------------------------------------------


def test_the_buffer_caps_users_and_games_and_counts_the_drops():
    buffer = presence.PresenceBuffer(user_cap=2, game_cap=2)

    assert buffer.add(1, "a", 60) is True
    assert buffer.add(1, "b", 60) is True
    assert buffer.add(1, "c", 60) is False
    assert buffer.add(2, "a", 60) is True
    assert buffer.add(3, "a", 60) is False
    assert buffer.dropped == 2


def test_the_buffer_accumulates_the_same_game():
    buffer = presence.PresenceBuffer()
    buffer.add(1, "Celeste", 60)
    buffer.add(1, "Celeste", 30)
    pending, _dropped = buffer.drain()
    assert pending == {1: {"Celeste": 90}}


def test_a_drain_detaches_and_resets():
    buffer = presence.PresenceBuffer()
    buffer.add(1, "Celeste", 60)
    pending, _dropped = buffer.drain()
    buffer.add(1, "Hades", 30)
    assert pending == {1: {"Celeste": 60}}
    assert buffer.drain()[0] == {1: {"Hades": 30}}


def test_restore_goes_back_through_the_caps():
    buffer = presence.PresenceBuffer(user_cap=1, game_cap=4)
    buffer.restore({1: {"a": 10}, 2: {"b": 10}})
    pending, dropped = buffer.drain()
    assert pending == {1: {"a": 10}}
    assert dropped == 1


def test_a_zero_length_session_is_neither_a_write_nor_a_drop():
    """Start and stop in the same instant: nothing to credit, nothing refused.

    Counting it as a drop made the drop counter claim the caps were being hit
    when they never were - the one thing instrumentation must not do.
    """
    cog = _cog()
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=100.0)
    cog._apply(OWNER, [discord.Game(name="Celeste")], [], now=100.0)

    assert cog._buffer.is_empty
    assert cog._stats["dropped"] == 0
    assert cog._stats["empty"] == 1


async def test_a_capped_buffer_entry_is_counted_as_one_drop_not_two(flushable):
    """The buffer counts its own refusals; the flush folds that total in once."""
    cog, _state = flushable
    cog._buffer = presence.PresenceBuffer(user_cap=1, game_cap=1)
    cog._opted.update({OWNER, FRIEND})
    for user_id, game in ((OWNER, "Celeste"), (FRIEND, "Hades")):
        cog._apply(user_id, [], [discord.Game(name=game)], now=0.0)
        cog._apply(user_id, [discord.Game(name=game)], [], now=600.0)

    assert cog._stats["dropped"] == 0, "the count lives in the buffer until the drain"

    await cog.flush(now=600.0)

    assert cog._stats["dropped"] == 1


def test_forget_erases_a_user_from_the_buffer_and_the_sessions():
    cog = _cog()
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)
    cog._apply(OWNER, [discord.Game(name="Celeste")], [], now=600.0)
    cog._apply(OWNER, [], [discord.Game(name="Hades")], now=600.0)
    cog._apply(FRIEND, [], [discord.Game(name="Hades")], now=600.0)

    cog._forget(OWNER)

    assert cog._buffer.is_empty
    assert list(cog._sessions) == [(FRIEND, "Hades")]


# ---------------------------------------------------------------------------
# The merge (pure)
# ---------------------------------------------------------------------------


def test_the_merge_accumulates_minutes_and_stamps_the_date():
    merged = presence.merge_games({}, {"Celeste": 90 * 60}, NOW)
    assert merged == {
        "games": [
            {"name": "Celeste", "minutes": 90, "last_played": NOW.isoformat()}
        ]
    }

    later = NOW + datetime.timedelta(hours=2)
    merged = presence.merge_games(merged, {"Celeste": 30 * 60}, later)
    assert merged["games"][0]["minutes"] == 120
    assert merged["games"][0]["last_played"] == later.isoformat()


def test_the_merge_keeps_the_ten_most_recently_played():
    payload = {}
    for index in range(15):
        payload = presence.merge_games(
            payload,
            {"game-%02d" % index: 60 * 60},
            NOW + datetime.timedelta(minutes=index),
        )

    names = [entry["name"] for entry in payload["games"]]
    assert len(names) == presence.MAX_GAMES
    assert names[0] == "game-14"
    assert "game-04" not in names


def test_a_game_untouched_for_a_month_falls_out_at_the_next_flush():
    old = presence.merge_games({}, {"Celeste": 60 * 60}, NOW)
    much_later = NOW + datetime.timedelta(days=presence.PURGE_AFTER_DAYS + 1)

    merged = presence.merge_games(old, {"Hades": 60 * 60}, much_later)

    assert [entry["name"] for entry in merged["games"]] == ["Hades"]


def test_the_merge_clips_a_hostile_name_and_refuses_an_absurd_number():
    merged = presence.merge_games(
        {}, {"A\nB " + "x" * 400: 60 * 60, "Nope": float("inf")}, NOW
    )
    (entry,) = merged["games"]
    assert "\n" not in entry["name"]
    assert len(entry["name"]) == presence.GAME_NAME_MAX


def test_the_merge_survives_a_junk_payload():
    for junk in ({}, {"games": "nope"}, {"games": [1, None, {"name": ""}]}, None):
        merged = presence.merge_games(junk, {"Celeste": 60 * 60}, NOW)
        assert merged["games"] == [
            {"name": "Celeste", "minutes": 60, "last_played": NOW.isoformat()}
        ]


def test_a_sub_minute_total_is_not_written():
    assert presence.merge_games({}, {"Celeste": 5}, NOW) == {"games": []}


def test_the_merged_payload_fits_the_framework_cap():
    """Ten maximal names, minutes and dates, measured through the real encoder."""
    payload = {}
    for index in range(presence.MAX_GAMES):
        payload = presence.merge_games(
            payload,
            {("g%02d" % index) + "x" * presence.GAME_NAME_MAX: 999 * 60 * 60},
            NOW + datetime.timedelta(minutes=index),
        )
    encoded = base.encode_payload(presence.GAMING_SECTION, payload)
    assert len(encoded.encode("utf-8")) < base.PAYLOAD_MAX_BYTES
    # Whole integers only: a float in exponent form is the one shape the
    # 8 KiB CHECK re-serialises longer than Python measured it.
    assert all(
        isinstance(entry["minutes"], int) for entry in payload["games"]
    )


# ---------------------------------------------------------------------------
# The flush
# ---------------------------------------------------------------------------


@pytest.fixture
def flushable(monkeypatch):
    """A cog whose storage seam is recorded instead of executed."""
    cog = _cog()
    state = {"payloads": {}, "writes": [], "drained_at_await": None}

    async def get_payloads(pool, connector, user_ids):
        state["drained_at_await"] = cog._buffer.user_count
        state["read"] = (connector, list(user_ids))
        return {uid: state["payloads"][uid] for uid in user_ids if uid in state["payloads"]}

    async def set_payload(pool, user_id, connector, payload, display_name=None):
        state["writes"].append((user_id, connector, payload))
        return True

    monkeypatch.setattr(storage, "get_payloads", get_payloads)
    monkeypatch.setattr(storage, "set_payload", set_payload)
    return cog, state


async def test_the_flush_drains_before_it_awaits_anything(flushable):
    """The buffer is detached in the FIRST statement, not after the read.

    Otherwise a minute recorded while the write was in flight would be wiped
    by the reset that follows it - the serverstats discipline, one table over.
    """
    cog, state = flushable
    state["payloads"][OWNER] = {}
    cog._buffer.add(OWNER, "Celeste", 600)

    await cog.flush(now=0.0)

    assert state["drained_at_await"] == 0
    assert cog._buffer.is_empty


async def test_the_flush_merges_into_the_existing_payload(flushable):
    cog, state = flushable
    state["payloads"][OWNER] = {
        "games": [{"name": "Celeste", "minutes": 30, "last_played": NOW.isoformat()}]
    }
    cog._buffer.add(OWNER, "Celeste", 30 * 60)

    await cog.flush(now=0.0)

    (user_id, connector, payload), = state["writes"]
    assert (user_id, connector) == (OWNER, presence.GAMING_SECTION)
    assert payload["games"][0]["minutes"] == 60


async def test_a_user_who_opted_out_mid_interval_is_not_written(flushable):
    """No row means no consent: the minutes are dropped, not deferred."""
    cog, state = flushable
    cog._buffer.add(OWNER, "Celeste", 600)

    await cog.flush(now=0.0)

    assert state["writes"] == []
    assert cog._buffer.is_empty


async def test_a_failed_write_is_handed_back_to_the_buffer(flushable, monkeypatch):
    cog, state = flushable
    state["payloads"][OWNER] = {}
    cog._buffer.add(OWNER, "Celeste", 600)

    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "set_payload", boom)
    await cog.flush(now=0.0)

    assert cog._buffer.drain()[0] == {OWNER: {"Celeste": 600}}


async def test_a_failed_read_hands_the_whole_generation_back(flushable, monkeypatch):
    cog, state = flushable
    cog._buffer.add(OWNER, "Celeste", 600)

    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "get_payloads", boom)
    await cog.flush(now=0.0)

    assert cog._buffer.drain()[0] == {OWNER: {"Celeste": 600}}


async def test_an_oversized_payload_is_refused_and_not_retried(flushable, monkeypatch):
    cog, state = flushable
    state["payloads"][OWNER] = {}
    cog._buffer.add(OWNER, "Celeste", 600)

    async def refuse(*args, **kwargs):
        raise base.InvalidPayload(presence.GAMING_SECTION, "too_large", 8192)

    monkeypatch.setattr(storage, "set_payload", refuse)
    await cog.flush(now=0.0)

    assert cog._buffer.is_empty, "retrying the same refused payload forever is the bug"


async def test_the_flush_defers_users_past_the_per_tick_ceiling(flushable, monkeypatch):
    """Bounded cost per tick, and NOTHING is lost - only postponed."""
    cog, state = flushable
    monkeypatch.setattr(presence, "FLUSH_USER_CAP", 2)
    for user_id in range(5):
        cog._buffer.add(user_id, "Celeste", 600)
        state["payloads"][user_id] = {}

    await cog.flush(now=0.0)

    assert len(state["read"][1]) == 2
    assert len(state["writes"]) == 2
    assert cog._buffer.user_count == 3


async def test_a_cancelled_read_hands_the_generation_back_and_re_raises(
    flushable, monkeypatch
):
    """CancelledError is a BaseException, so `except Exception` would miss it.

    And missing it is not academic: cog_unload cancels the loop and THEN runs a
    final flush, so a generation lost here is a generation the shutdown never
    writes.
    """
    cog, _state = flushable
    cog._buffer.add(OWNER, "Celeste", 600)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(storage, "get_payloads", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await cog.flush(now=0.0)

    assert cog._buffer.drain()[0] == {OWNER: {"Celeste": 600}}


async def test_a_flush_cancelled_mid_write_survives_into_the_final_flush(
    flushable, monkeypatch
):
    """The real shutdown sequence, end to end.

    The loop is cancelled while a write is in flight; the user being written
    AND the users the loop had not reached yet must both be back in the buffer,
    or cog_unload's final flush writes an empty generation over a real one.
    """
    cog, state = flushable
    state["payloads"][OWNER] = {}
    state["payloads"][FRIEND] = {}
    cog._buffer.add(OWNER, "Celeste", 600)
    cog._buffer.add(FRIEND, "Hades", 600)
    recording = storage.set_payload

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(storage, "set_payload", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await cog.flush(now=0.0)

    assert cog._buffer.user_count == 2, "nothing may be lost to the cancellation"

    monkeypatch.setattr(storage, "set_payload", recording)
    await cog.cog_unload()

    assert sorted(user_id for user_id, _c, _p in state["writes"]) == [OWNER, FRIEND]


async def test_cog_unload_gives_up_on_a_wedged_final_flush(monkeypatch):
    """The final flush is BOUNDED too, not just the wait on the cancelled loop.

    A wedged pool would otherwise hold a clean shutdown open for its whole
    command_timeout (60s) over a cosmetic aggregate. Giving up costs the last
    interval, which is exactly what a hard crash already costs.

    This is the HARD bound: the batched read never returns, so the cooperative
    deadline below never gets a turn and only the wait_for can end it.
    """
    cog = _cog()
    cog._buffer.add(OWNER, "Celeste", 600)
    never = asyncio.Event()

    async def hang(*args, **kwargs):
        await never.wait()

    monkeypatch.setattr(storage, "get_payloads", hang)
    monkeypatch.setattr(presence, "UNLOAD_FLUSH_TIMEOUT", 0.05)
    monkeypatch.setattr(presence, "UNLOAD_FLUSH_GRACE", 0.05)

    await asyncio.wait_for(cog.cog_unload(), timeout=1)  # must not hang

    # The cancelled read still handed its generation back (except BaseException).
    assert cog._buffer.drain()[0] == {OWNER: {"Celeste": 600}}


async def test_the_unload_deadline_is_cooperative_not_a_cancellation(
    flushable, monkeypatch
):
    """A slow pool defers the users it could not reach; it loses nothing.

    The bound covers up to FLUSH_USER_CAP sequential writes, not one statement,
    so it can genuinely expire on a merely BUSY pool. When it does, the flush has
    to stop between two users and hand the rest back - not be cancelled with a
    write in flight.
    """
    cog, state = flushable
    users = (OWNER, OWNER + 1, OWNER + 2)
    for user_id in users:
        cog._buffer.add(user_id, "Celeste", 600)
        state["payloads"][user_id] = {}

    # A clock that jumps past the deadline right after the first user: one
    # reading for the deadline itself, one for the flush's own ``now``, then one
    # per loop iteration.
    ticks = iter([0.0, 0.0, 0.0])
    monkeypatch.setattr(presence, "_monotonic", lambda: next(ticks, 99.0))
    monkeypatch.setattr(presence, "UNLOAD_FLUSH_TIMEOUT", 1.0)

    await cog.cog_unload()

    # Exactly one user was written; the other two went back to the buffer whole.
    assert len(state["writes"]) == 1
    written = state["writes"][0][0]
    deferred, _dropped = cog._buffer.drain()
    assert set(deferred) == set(users) - {written}
    assert all(games == {"Celeste": 600} for games in deferred.values())
    assert cog._stats["deferred"] == 2


async def test_the_periodic_flush_has_no_deadline(flushable, monkeypatch):
    """Only teardown is in a hurry: the loop's own flush passes no deadline.

    Pinned because a deadline leaking into the periodic path would silently turn
    every busy interval into a partial write.
    """
    cog, state = flushable
    for user_id in (OWNER, OWNER + 1, OWNER + 2):
        cog._buffer.add(user_id, "Celeste", 600)
        state["payloads"][user_id] = {}

    # Time races ahead of any deadline a caller could have set.
    monkeypatch.setattr(presence, "_monotonic", lambda: 10_000.0)

    await cog.flush()

    assert len(state["writes"]) == 3
    assert cog._buffer.is_empty


async def test_an_erased_user_is_purged_by_the_flush_itself(flushable):
    """The batched SELECT is the authority on consent, whatever told this cog.

    `profile clear` and /mydata deleteprofile delete the marker row from other
    cogs. Even with no hook at all, the flush that finds no row must write
    nothing AND disarm the collector, so the next event of that user is
    rejected by the O(1) opt-in test rather than recorded.
    """
    cog, state = flushable
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)
    cog._apply(OWNER, [discord.Game(name="Celeste")], [], now=600.0)
    cog._apply(OWNER, [], [discord.Game(name="Hades")], now=600.0)

    await cog.flush(now=600.0)

    assert state["writes"] == []
    assert cog._opted == set()
    assert cog._sessions == {}
    assert cog._buffer.is_empty
    assert cog._stats["forgotten"] == 1

    before = _Member(OWNER)
    after = _Member(OWNER, activities=[discord.Game(name="Hades")])
    _run_without_awaiting(cog.on_presence_update(before, after))
    assert cog._sessions == {}


async def test_a_row_that_dies_between_the_read_and_the_write_purges_too(
    flushable, monkeypatch
):
    """NotLinked is the same erasure, one round trip later."""
    cog, state = flushable
    cog._opted.add(OWNER)
    state["payloads"][OWNER] = {}
    cog._buffer.add(OWNER, "Celeste", 600)

    async def gone(*args, **kwargs):
        raise base.NotLinked(presence.GAMING_SECTION)

    monkeypatch.setattr(storage, "set_payload", gone)
    await cog.flush(now=0.0)

    assert cog._opted == set()
    assert cog._buffer.is_empty


async def test_the_flush_sweeps_stale_sessions(flushable):
    cog, _state = flushable
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)

    await cog.flush(now=presence.SESSION_MAX_SECONDS + 1)

    assert cog._sessions == {}


# ---------------------------------------------------------------------------
# Opting in and out
# ---------------------------------------------------------------------------


@pytest.fixture
def commandable(monkeypatch):
    """A cog whose four storage calls are recorded instead of executed."""
    cog = _cog()
    state = {"markers": set(), "unlinked": [], "rows": []}

    async def set_marker(pool, user_id, connector, external_id=None):
        state["markers"].add((user_id, connector, external_id))
        state["rows"].append(connector)
        return {"connector": connector, "created": True}

    async def unlink(pool, user_id, connector):
        state["unlinked"].append((user_id, connector))
        return True

    async def get_connections(pool, user_id):
        return [{"connector": name} for name in state["rows"]]

    monkeypatch.setattr(storage, "set_marker", set_marker)
    monkeypatch.setattr(storage, "unlink", unlink)
    monkeypatch.setattr(storage, "get_connections", get_connections)
    return cog, state


async def test_turning_gaming_on_writes_the_marker_and_arms_the_listener(commandable):
    cog, state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx, gaming="on")

    assert (OWNER, presence.GAMING_SECTION, None) in state["markers"]
    assert OWNER in cog._opted
    assert "Games" in _text(ctx)


async def test_turning_gaming_on_seeds_the_member_cache_once(commandable):
    """Without a cached member, parse_presence_update discards every event."""
    cog, _state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx, gaming="on")

    assert ctx.guild.queries == [
        {"user_ids": [OWNER], "cache": True, "presences": True}
    ]


async def test_the_seed_is_skipped_when_the_member_is_already_cached(commandable):
    cog, _state = commandable
    ctx = _Ctx(guild=_Guild(cached=[OWNER]))

    await cog.cmd_presence(ctx, gaming="on")

    assert ctx.guild.queries == []


async def test_a_gateway_that_will_not_answer_never_costs_the_opt_in(commandable):
    cog, state = commandable

    class _Rude(_Guild):
        async def query_members(self, **kwargs):
            raise RuntimeError("gateway said no")

    ctx = _Ctx(guild=_Rude())
    await cog.cmd_presence(ctx, gaming="on")

    assert OWNER in cog._opted
    assert (OWNER, presence.GAMING_SECTION, None) in state["markers"]


async def test_turning_spotify_on_never_arms_the_collector(commandable):
    """Spotify stores nothing, so it has no listener state at all."""
    cog, state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx, spotify="on")

    assert (OWNER, presence.SPOTIFY_SECTION, None) in state["markers"]
    assert cog._opted == set()


async def test_turning_gaming_off_unlinks_and_erases_what_was_in_memory(commandable):
    cog, state = commandable
    cog._opted.add(OWNER)
    cog._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)
    cog._apply(OWNER, [discord.Game(name="Celeste")], [], now=600.0)
    cog._apply(OWNER, [], [discord.Game(name="Hades")], now=600.0)
    ctx = _Ctx()

    await cog.cmd_presence(ctx, gaming="off")

    assert state["unlinked"] == [(OWNER, presence.GAMING_SECTION)]
    assert OWNER not in cog._opted
    assert cog._buffer.is_empty
    assert cog._sessions == {}


async def test_both_switches_move_in_one_call(commandable):
    cog, state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx, gaming="on", spotify="off")

    assert (OWNER, presence.GAMING_SECTION, None) in state["markers"]
    assert state["unlinked"] == [(OWNER, presence.SPOTIFY_SECTION)]


async def test_no_argument_reports_the_state_and_changes_nothing(commandable):
    cog, state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx)

    assert state["markers"] == set()
    assert state["unlinked"] == []
    answer = _text(ctx)
    assert "Games" in answer and "Spotify" in answer


async def test_an_opt_in_points_at_the_surface_that_publishes_the_section(commandable):
    cog, _state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx, gaming="on")

    assert "?profile panel" in _text(ctx)


async def test_every_answer_is_ephemeral(commandable):
    cog, _state = commandable
    ctx = _Ctx()

    await cog.cmd_presence(ctx, gaming="on")
    await cog.cmd_presence(ctx, gaming="off")
    await cog.cmd_presence(ctx)

    assert all(kwargs.get("ephemeral") for _args, kwargs in ctx.sends)


async def test_a_failed_write_says_so_and_does_not_claim_the_opt_in(commandable, monkeypatch):
    cog, _state = commandable

    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "set_marker", boom)
    ctx = _Ctx()
    await cog.cmd_presence(ctx, gaming="on")

    assert "later" in _text(ctx)
    assert OWNER not in cog._opted


async def test_the_opt_in_set_is_loaded_once_at_cog_load(monkeypatch):
    cog = _cog()

    async def get_opted_users(pool, connector):
        assert connector == presence.GAMING_SECTION
        return {OWNER, FRIEND}

    monkeypatch.setattr(storage, "get_opted_users", get_opted_users)
    monkeypatch.setattr(cog._flush_loop, "start", lambda *a, **k: None)
    await cog.cog_load()

    assert cog._opted == {OWNER, FRIEND}


async def test_a_failed_load_fails_closed(monkeypatch):
    """An unreadable consent cache must collect NOTHING, never everything."""
    cog = _cog()

    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "get_opted_users", boom)
    monkeypatch.setattr(cog._flush_loop, "start", lambda *a, **k: None)
    await cog.cog_load()

    assert cog._opted == set()


# ---------------------------------------------------------------------------
# Erasure paths that are NOT `/profile presence off`
# ---------------------------------------------------------------------------


def _armed_collector():
    """A collector holding, for OWNER, all three kinds of in-memory state."""
    collector = _cog()
    collector._opted.add(OWNER)
    collector._apply(OWNER, [], [discord.Game(name="Celeste")], now=0.0)
    collector._apply(OWNER, [discord.Game(name="Celeste")], [], now=600.0)
    collector._apply(OWNER, [], [discord.Game(name="Hades")], now=600.0)
    assert collector._sessions and not collector._buffer.is_empty
    return collector


def test_the_erasure_seam_disarms_the_collector_on_the_spot():
    collector = _armed_collector()

    assert presence.forget_collected_presence(_bot_with(collector), OWNER) is True

    assert collector._opted == set()
    assert collector._sessions == {}
    assert collector._buffer.is_empty


def test_the_erasure_seam_is_a_no_op_when_the_cog_is_not_loaded():
    """Best effort by design: the flush's own check is what backs it up."""
    assert presence.forget_collected_presence(_bot_with(None), OWNER) is False
    assert presence.forget_collected_presence(types.SimpleNamespace(), OWNER) is False


async def test_profile_clear_disarms_the_collector(monkeypatch):
    """`profile clear` deletes the marker row from ANOTHER cog.

    Without the hook the collector would keep recording someone who just
    erased themselves, for as long as the process lives.
    """
    collector = _armed_collector()

    async def delete_profile(pool, user_id):
        return {"user_profiles": 1}

    monkeypatch.setattr(profile_storage, "delete_profile", delete_profile)
    profiles = profile_cog.Profiles(_bot_with(collector))
    ctx = _Ctx()

    await profile_cog.Profiles.profile_clear.callback(profiles, ctx)

    assert collector._opted == set()
    assert collector._sessions == {}
    assert collector._buffer.is_empty


async def test_mydata_deleteprofile_disarms_the_collector(monkeypatch):
    """The same erasure, offered from the privacy surface."""
    collector = _armed_collector()

    # The WIDE verb: /mydata erases the profile AND the records that are not
    # profile data (the top.gg vote ledger), which `/profile clear` above does
    # not - see privacy.USER_DELETE_QUERIES.
    async def delete_user_data(pool, user_id):
        return {"user_profiles": 1}

    monkeypatch.setattr(privacy, "delete_user_data", delete_user_data)
    view = usersettings.ProfileDeletionView(
        types.SimpleNamespace(bot=_bot_with(collector)), OWNER
    )
    interaction = _Interaction()

    await view.confirm.callback(interaction)

    assert interaction.edited, "the user is still told their profile is gone"
    assert collector._opted == set()
    assert collector._sessions == {}
    assert collector._buffer.is_empty


# ---------------------------------------------------------------------------
# The storage seam
# ---------------------------------------------------------------------------


async def test_the_marker_upsert_never_touches_the_payload(fake_pool):
    fake_pool.fetchrow_return = Record(
        connector=presence.GAMING_SECTION,
        external_id=str(OWNER),
        display_name=None,
        linked_at=None,
        last_refresh=None,
        payload="{}",
        created=True,
    )

    stored = await storage.set_marker(fake_pool, OWNER, presence.GAMING_SECTION)

    (_method, query, args) = fake_pool.calls[-1]
    assert "ON CONFLICT (user_id, connector) DO UPDATE SET external_id" in query
    assert "payload" not in query.split("DO UPDATE SET")[1].split("RETURNING")[0]
    assert args == (OWNER, presence.GAMING_SECTION, str(OWNER))
    assert stored["created"] is True


async def test_the_marker_refuses_a_linkable_name(fake_pool):
    """A handle connector must not be creatable without a handle."""
    with pytest.raises(base.UnknownConnector):
        await storage.set_marker(fake_pool, OWNER, "steam")


async def test_link_still_refuses_the_presence_sections(fake_pool):
    with pytest.raises(base.UnknownConnector):
        await storage.link(
            fake_pool, OWNER, presence.GAMING_SECTION, base.LinkResult(external_id="x")
        )


async def test_unlink_accepts_a_presence_section_and_unpublishes_it(fake_pool):
    await storage.unlink(fake_pool, OWNER, presence.GAMING_SECTION)

    queries = [query for _m, query, _a in fake_pool.calls]
    assert any("DELETE FROM profile_connections" in query for query in queries)
    assert any("profile_visibility" in query for query in queries)


async def test_the_batched_read_is_one_statement_and_decodes_the_payload(fake_pool):
    fake_pool.fetch_return = [
        Record(user_id=OWNER, payload=json.dumps({"games": []})),
        Record(user_id=FRIEND, payload=None),
    ]

    payloads = await storage.get_payloads(
        fake_pool, presence.GAMING_SECTION, [OWNER, FRIEND, STRANGER]
    )

    assert payloads == {OWNER: {"games": []}, FRIEND: {}}
    assert len(fake_pool.calls) == 1
    assert "user_id = ANY($2::bigint[])" in fake_pool.calls[0][1]


async def test_the_batched_read_short_circuits_on_an_empty_list(fake_pool):
    assert await storage.get_payloads(fake_pool, presence.GAMING_SECTION, []) == {}
    assert fake_pool.calls == []


async def test_the_opted_users_read_returns_a_set(fake_pool):
    fake_pool.fetch_return = [Record(user_id=OWNER), Record(user_id=FRIEND)]

    opted = await storage.get_opted_users(fake_pool, presence.GAMING_SECTION)

    assert opted == {OWNER, FRIEND}
    assert "WHERE connector = $1" in fake_pool.calls[0][1]


# ---------------------------------------------------------------------------
# The live enrichment (zero I/O)
# ---------------------------------------------------------------------------


def test_enrich_attaches_the_live_spotify_listen():
    connections = [_connection(presence.SPOTIFY_SECTION)]
    member = _Member(activities=[_spotify()])

    presence.enrich_live(member, connections)

    assert connections[0]["live"] == {
        "title": "The Mother We Share",
        "artist": "Chvrches",
        "cover": "https://i.scdn.co/image/ab/cd",
        "url": "https://open.spotify.com/track/6Vjkc",
    }


def test_enrich_attaches_the_live_game():
    connections = [_connection(presence.GAMING_SECTION)]
    member = _Member(activities=[discord.Game(name="Celeste")])

    presence.enrich_live(member, connections)

    assert connections[0]["live"] == {"playing": "Celeste"}


def test_enrich_says_nothing_when_there_is_nothing_live():
    connections = [
        _connection(presence.SPOTIFY_SECTION),
        _connection(presence.GAMING_SECTION),
    ]

    presence.enrich_live(_Member(activities=[]), connections)

    assert all("live" not in row for row in connections)


def test_enrich_never_touches_a_section_the_user_did_not_opt_into():
    connections = [_connection("steam")]

    presence.enrich_live(_Member(activities=[_spotify()]), connections)

    assert "live" not in connections[0]


def test_enrich_survives_a_malformed_activity():
    connections = [_connection(presence.SPOTIFY_SECTION)]
    broken = _Member(activities=[_spotify(details=None, state=None)])

    presence.enrich_live(broken, connections)

    assert "live" not in connections[0]


def test_a_cover_that_is_not_an_absolute_url_is_dropped():
    """An unfetchable Thumbnail makes Discord reject the WHOLE card at send."""
    member = _Member(activities=[_spotify(assets={"large_image": "mp:external/x"})])
    now = presence.spotify_now_playing(member)
    assert "cover" not in now


# ---------------------------------------------------------------------------
# The renderers
# ---------------------------------------------------------------------------


async def test_the_gaming_section_draws_the_stored_top_games():
    container = _Container()
    connection = _connection(
        presence.GAMING_SECTION,
        payload={
            "games": [
                {"name": "Celeste", "minutes": 150},
                {"name": "Hades", "minutes": 45},
            ]
        },
    )

    await presence._render_gaming(
        container, registry.get(presence.GAMING_SECTION), None, connection, BIG_BUDGET
    )

    text = _rendered(container)
    assert "Celeste - 2h 30m" in text
    assert "Hades - 45m" in text


async def test_the_gaming_section_names_the_live_game_first():
    container = _Container()
    connection = _connection(
        presence.GAMING_SECTION,
        payload={"games": [{"name": "Hades", "minutes": 45}]},
    )
    connection["live"] = {"playing": "Celeste"}

    await presence._render_gaming(
        container, registry.get(presence.GAMING_SECTION), None, connection, BIG_BUDGET
    )

    lines = _rendered(container).split("\n")
    assert "Celeste" in lines[1]
    assert lines[2] == "Hades - 45m"


def test_the_section_is_labelled_for_what_it_really_holds():
    """A cumulative history titled "Now playing" is a lie most of the time.

    The stored payload is weeks of minutes per game; the live game is only an
    optional FIRST LINE. The label is shared with the visibility panel and with
    every "X is now visible to ..." answer, so it has to be true when nothing
    is playing - which is the normal case.
    """
    assert registry.get(presence.GAMING_SECTION).label == "Recently played"


async def test_the_shared_label_is_what_the_panel_shows_too():
    """One label, three surfaces: the card heading, the select and its options."""
    field = registry.get(presence.GAMING_SECTION)
    select = views._SectionVisibilitySelect(None, field, visibility.PRIVATE)

    assert select.placeholder == field.label
    assert all(option.label.startswith(field.label) for option in select.options)

    container = _Container()
    await presence._render_gaming(
        container,
        field,
        None,
        _connection(
            presence.GAMING_SECTION,
            payload={"games": [{"name": "Hades", "minutes": 45}]},
        ),
        BIG_BUDGET,
    )
    assert _rendered(container).split("\n")[0] == "**" + field.label + "**"


async def test_a_stale_row_is_filtered_at_the_render_as_well():
    """The lazy purge only fires on a flush that TOUCHES the row.

    A member who stopped playing (or stopped being seen) is never flushed
    again, so their row sits at its last state forever - and without this the
    card would still be listing it two years later.
    """
    container = _Container()
    now = datetime.datetime.now(UTC)
    connection = _connection(
        presence.GAMING_SECTION,
        payload={
            "games": [
                {
                    "name": "Ancient",
                    "minutes": 600,
                    "last_played": (
                        now - datetime.timedelta(days=presence.PURGE_AFTER_DAYS + 1)
                    ).isoformat(),
                },
                {
                    "name": "Hades",
                    "minutes": 45,
                    "last_played": (now - datetime.timedelta(days=1)).isoformat(),
                },
                # No date at all: real playtime with a LOST stamp, which is not
                # evidence of age - it stays, exactly as merge_games keeps it.
                {"name": "Undated", "minutes": 20},
            ]
        },
    )

    await presence._render_gaming(
        container, registry.get(presence.GAMING_SECTION), None, connection, BIG_BUDGET
    )

    text = _rendered(container)
    assert "Ancient" not in text
    assert "Hades" in text and "Undated" in text


async def test_the_gaming_section_is_omitted_when_there_is_nothing_to_say():
    container = _Container()

    await presence._render_gaming(
        container,
        registry.get(presence.GAMING_SECTION),
        None,
        _connection(presence.GAMING_SECTION),
        BIG_BUDGET,
    )

    assert container.items == []


async def test_the_gaming_section_reclips_a_row_written_by_a_past_version():
    """A game title is line-leading text on SOMEBODY ELSE's card.

    Flattening alone is not enough: it removes the newline and leaves the
    "## " exactly where markdown wants it, so the row would render as a
    heading over the real sections of the profile being looked at.
    """
    container = _Container()
    connection = _connection(
        presence.GAMING_SECTION,
        payload={"games": [{"name": "## Fake\nheading" + "x" * 400, "minutes": 10}]},
    )

    await presence._render_gaming(
        container, registry.get(presence.GAMING_SECTION), None, connection, BIG_BUDGET
    )

    body = _rendered(container).split("\n")[1]
    assert not body.startswith("#")
    assert body.startswith("\N{ZERO WIDTH SPACE}")
    assert len(body) < 200


@pytest.mark.parametrize("prefix", ["## ", "-# ", "> "])
async def test_a_spotify_track_cannot_grow_a_line_of_its_own(prefix):
    """Two guards, both needed, on text Spotify hands over verbatim.

    The clip FLATTENS, so a title can never split the row it sits in; and the
    row is then defused, so a translation free to put ``{track}`` first cannot
    turn that title into a heading in one locale and in no other.
    """
    container = _Container()
    connection = _connection(presence.SPOTIFY_SECTION)
    connection["live"] = {
        "title": "song\n" + prefix + "Fake heading",
        "artist": "Ghost",
    }

    await presence._render_spotify(
        container, registry.get(presence.SPOTIFY_SECTION), None, connection, BIG_BUDGET
    )

    lines = _rendered(container).split("\n")
    assert len(lines) == 2, "a title must never open a line of its own"
    for line in lines:
        assert not line.lstrip().startswith(("#", "-#", ">"))
    assert profile_views_defuses(prefix)


def profile_views_defuses(prefix):
    """The shared helper really is what makes a leading prefix inert."""
    defused = views.defuse_lines(prefix + "Fake heading")
    return defused.startswith("\N{ZERO WIDTH SPACE}")


async def test_the_gaming_section_stops_at_the_budget_it_was_handed():
    container = _Container()
    connection = _connection(
        presence.GAMING_SECTION,
        payload={
            "games": [
                {"name": "game-%02d" % index, "minutes": 60} for index in range(10)
            ]
        },
    )

    await presence._render_gaming(
        container,
        registry.get(presence.GAMING_SECTION),
        None,
        connection,
        views.SectionBudget(text=40, components=30),
    )

    heading = "**" + registry.get(presence.GAMING_SECTION).label + "**"
    assert len(_rendered(container)) <= 40 + len(heading)


async def test_the_spotify_section_draws_the_live_listen_only():
    container = _Container()
    connection = _connection(presence.SPOTIFY_SECTION)
    connection["live"] = {
        "title": "The Mother We Share",
        "artist": "Chvrches",
        "url": "https://open.spotify.com/track/6Vjkc",
        "cover": "https://i.scdn.co/image/ab",
    }

    await presence._render_spotify(
        container, registry.get(presence.SPOTIFY_SECTION), None, connection, BIG_BUDGET
    )

    text = _rendered(container)
    assert "Chvrches - The Mother We Share" in text
    assert "https://open.spotify.com/track/6Vjkc" in text
    assert isinstance(container.items[0], discord.ui.Section)


async def test_a_track_title_cannot_open_a_markdown_link_of_its_own():
    """The title is the LABEL of a link, and a ']' closes a label.

    A track uploaded as ``song](https://evil.example/phish) Free Nitro [click``
    turns the row into a link to somebody else's domain, drawn on the card of
    whoever is being looked at. Both brackets are escaped, so the url the
    renderer chose stays the only structural part of the line.
    """
    container = _Container()
    connection = _connection(presence.SPOTIFY_SECTION)
    connection["live"] = {
        "title": "song](https://evil.example/phish) Free Nitro [click here",
        "artist": "Ghost",
        "url": "https://open.spotify.com/track/6Vjkc",
    }

    await presence._render_spotify(
        container, registry.get(presence.SPOTIFY_SECTION), None, connection, BIG_BUDGET
    )

    text = _rendered(container)
    # The hostile opener is defanged, and the ONLY live link opener left is the
    # closing one of the row the renderer built.
    openers = [index for index in range(len(text)) if text.startswith("](", index)]
    assert [text[index - 1] == "\\" for index in openers] == [True, False]
    assert "\\](https://evil.example/phish)" in text
    assert "\\[click here" in text
    assert text.endswith("](https://open.spotify.com/track/6Vjkc)")
    # Still one line, still one row: the flattening guard is untouched.
    assert len(text.split("\n")) == 2


async def test_the_spotify_section_is_omitted_with_nothing_playing():
    """Silence asserts nothing; an empty section asserts something false."""
    container = _Container()

    await presence._render_spotify(
        container,
        registry.get(presence.SPOTIFY_SECTION),
        None,
        _connection(presence.SPOTIFY_SECTION),
        BIG_BUDGET,
    )

    assert container.items == []


async def test_the_spotify_payload_is_never_a_source_of_truth():
    """Even a row someone hand-wrote a track into renders nothing."""
    container = _Container()
    connection = _connection(
        presence.SPOTIFY_SECTION, payload={"title": "Leaked", "artist": "Ghost"}
    )

    await presence._render_spotify(
        container, registry.get(presence.SPOTIFY_SECTION), None, connection, BIG_BUDGET
    )

    assert container.items == []


# ---------------------------------------------------------------------------
# Visibility: the presence sections obey the same matrix as every other one
# ---------------------------------------------------------------------------


def _viewer(viewer_id, shares_guild):
    return visibility.ViewerContext(
        owner_id=OWNER, viewer_id=viewer_id, shares_guild=shares_guild
    )


def _matrix_case(section):
    """``(member, connections, needle)`` for one presence section.

    Spotify is parametrised alongside gaming on purpose: it is the section that
    stores NOTHING and is enriched live at render time, so "the visibility
    matrix applies to it too" is a claim about a different code path, not a
    copy of the gaming one.
    """
    if section == presence.GAMING_SECTION:
        member = _Member(activities=[discord.Game(name="Celeste")])
        connections = [
            _connection(
                section, payload={"games": [{"name": "Celeste", "minutes": 60}]}
            )
        ]
        return member, connections, "Celeste"
    return _Member(activities=[_spotify()]), [_connection(section)], "Chvrches"


@pytest.mark.parametrize(
    "section", [presence.GAMING_SECTION, presence.SPOTIFY_SECTION]
)
@pytest.mark.parametrize(
    "level, viewer_id, shares_guild, visible",
    [
        ("private", OWNER, True, True),
        ("private", FRIEND, True, False),
        ("server", FRIEND, True, True),
        ("server", STRANGER, False, False),
        ("public", STRANGER, False, True),
    ],
)
async def test_the_presence_sections_follow_the_visibility_matrix(
    section, level, viewer_id, shares_guild, visible
):
    member, connections, needle = _matrix_case(section)
    presence.enrich_live(member, connections)

    card = await views.build_profile_card(
        member,
        {"user_id": OWNER},
        {section: level},
        _viewer(viewer_id, shares_guild),
        connections,
    )

    text = "\n".join(_texts(card)) if card is not None else ""
    assert (needle in text) is visible


async def test_a_published_section_with_no_marker_row_draws_nothing():
    """Publishing is a choice about an audience, not evidence of consent."""
    member = _Member(activities=[discord.Game(name="Celeste")])

    card = await views.build_profile_card(
        member,
        {"user_id": OWNER},
        {presence.GAMING_SECTION: "public"},
        _viewer(STRANGER, False),
        [],
    )

    assert card is None


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


class _ExportPool:
    def __init__(self):
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return None

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return None

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "FROM profile_connections" in query:
            return [
                {
                    "connector": presence.GAMING_SECTION,
                    "external_id": str(OWNER),
                    "display_name": None,
                    "linked_at": "then",
                    "last_refresh": None,
                    "payload": json.dumps(
                        {"games": [{"name": "Celeste", "minutes": 60}]}
                    ),
                }
            ]
        return []


async def test_mydata_exports_the_collected_aggregate():
    """Everything this cog stores about a person is in their own archive."""
    data, _avatars = await privacy.collect_user_export(_ExportPool(), OWNER)

    (row,) = data["profile_connections"]
    assert row["connector"] == presence.GAMING_SECTION
    assert row["payload"] == {"games": [{"name": "Celeste", "minutes": 60}]}


def test_the_forget_path_already_covers_the_markers():
    """No new table means no new delete - and that is the point."""
    assert "profile_connections" in dict(privacy.PROFILE_DELETE_QUERIES)


def test_the_sections_are_declared_in_the_schema_check():
    import os
    import re

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
    )
    with open(path, encoding="utf-8") as handle:
        schema = handle.read()
    check = re.search(
        r"profile_connections_connector_known\s*CHECK \(connector IN \((.*?)\)\)",
        schema,
        re.S,
    )
    assert check is not None
    for section in base.PRESENCE_SECTIONS:
        assert "'%s'" % section in check.group(1)
