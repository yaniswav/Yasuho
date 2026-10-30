"""Saved server effect (Yasuho+): apply-once hook, save seam, dashboard action.

Real ``Music`` methods on an ``object.__new__`` instance (the test_music_247
approach) with the premium verdict, the settings blob and the filter node
stubbed. Each guard has a negative control (free guild, restore, Off, ...).
"""

from __future__ import annotations

import types

import pytest

import cogs.music.music as music_mod
from cogs.music import effects, guild_config
from cogs.system import dashboard_music_actions as dma
from tools import settings
from tools.quotas import QuotaRegistry

GID = 42


class _Player:
    def __init__(self):
        self.effect_preset = None
        self.default_effect_done = False
        self.set_filters_calls = 0
        self.played_ids = set()
        self._automix_skips = 0
        self.home = None
        self.radio_genre = None
        self.channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=GID))
        self.guild = self.channel.guild

    async def set_filters(self, filters, **kwargs):
        self.set_filters_calls += 1


@pytest.fixture
def store(monkeypatch):
    blob = {}
    writes = []

    async def fake_get(pool, gid, key, default=None):
        return blob.get(key, default)

    async def fake_set(pool, gid, key, value):
        writes.append((gid, key, value))
        blob[key] = value

    monkeypatch.setattr(settings, "get_guild", fake_get)
    monkeypatch.setattr(settings, "set_guild", fake_set)
    blob["writes"] = writes
    return blob


@pytest.fixture
def premium_on(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(
        music_mod,
        "resolve_guild_limits",
        lambda bot, gid: types.SimpleNamespace(music_saved_effects=state["on"]),
    )
    return state


@pytest.fixture
def cog(monkeypatch, premium_on):
    c = object.__new__(music_mod.Music)
    c.bot = types.SimpleNamespace(db_pool=object())
    c.quotas = QuotaRegistry()

    async def noop(*a, **k):
        return None

    c.lyrics_sessions = types.SimpleNamespace(notify_track=noop)
    c.skip_votes = types.SimpleNamespace(notify_track=noop)
    c._snapshot = noop
    c._send_controller = noop
    return c


def _event(title="Song"):
    track = types.SimpleNamespace(
        identifier="t1", title=title, author="A", length=200_000, extras=None
    )
    return types.SimpleNamespace(track=track)


async def _start(cog, player):
    await music_mod.Music.on_sonolink_track_start(cog, player, _event())


async def test_applied_once_on_first_track_start(cog, store):
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    player = _Player()
    await _start(cog, player)
    assert player.effect_preset == "nightcore"
    assert player.set_filters_calls == 1
    assert player.default_effect_done is True
    # Second track: nothing re-applied (Q5 once per player).
    await _start(cog, player)
    assert player.set_filters_calls == 1


async def test_not_applied_when_free(cog, store, premium_on):
    premium_on["on"] = False
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    player = _Player()
    await _start(cog, player)
    assert player.effect_preset is None
    assert player.set_filters_calls == 0


async def test_not_applied_when_restored(cog, store):
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    player = _Player()
    player.default_effect_done = True  # what _restore_one sets
    await _start(cog, player)
    assert player.effect_preset is None


async def test_explicit_effect_wins(cog, store):
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    player = _Player()
    player.effect_preset = "bassboost"
    await _start(cog, player)
    assert player.effect_preset == "bassboost"
    assert player.set_filters_calls == 0


async def test_effect_picked_during_the_read_wins(cog, store, monkeypatch):
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    player = _Player()
    real = guild_config.default_effect

    async def slow(pool, gid):
        value = await real(pool, gid)
        player.effect_preset = "bassboost"  # a member picked one meanwhile
        return value

    monkeypatch.setattr(guild_config, "default_effect", slow)
    await _start(cog, player)
    assert player.effect_preset == "bassboost"
    assert player.set_filters_calls == 0


async def test_full_ceiling_plays_unfiltered_without_error(cog, store):
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    for other in range(1000, 1000 + 40):
        assert cog.quotas.filtered_players.acquire(other)
    player = _Player()
    await _start(cog, player)
    assert player.effect_preset is None
    assert player.set_filters_calls == 0


async def test_unset_or_unknown_key_is_harmless(cog, store):
    player = _Player()
    await _start(cog, player)
    assert player.effect_preset is None
    store[guild_config.KEY_DEFAULT_EFFECT] = "no-such-preset"
    player2 = _Player()
    await _start(cog, player2)
    assert player2.effect_preset is None


async def test_save_seam_codes(cog, store, premium_on):
    assert await cog._save_default_effect(GID, "nightcore") == "saved"
    assert store[guild_config.KEY_DEFAULT_EFFECT] == "nightcore"
    assert await cog._save_default_effect(GID, "off") == "cleared"
    assert store[guild_config.KEY_DEFAULT_EFFECT] is None
    assert await guild_config.default_effect(None, GID) is None
    assert await cog._save_default_effect(GID, "bogus") == "unknown_preset"
    n = len(store["writes"])
    premium_on["on"] = False
    assert await cog._save_default_effect(GID, "nightcore") == "not_premium"
    assert len(store["writes"]) == n  # nothing written when refused


def test_key_stays_out_of_the_dashboard_key_set():
    assert guild_config.KEY_DEFAULT_EFFECT not in guild_config.KEYS


async def test_non_manager_cannot_save(cog, store, monkeypatch):
    async def no(player, actor):
        return False

    monkeypatch.setattr(cog, "_is_music_manager", no, raising=False)
    text, view = await cog._request_default_effect(
        _Player(), types.SimpleNamespace(id=GID), types.SimpleNamespace(id=1), "nightcore"
    )
    assert "Manage Server" in text
    assert store["writes"] == []


async def test_manager_save_and_free_refusal(cog, store, premium_on, monkeypatch):
    async def yes(player, actor):
        return True

    monkeypatch.setattr(cog, "_is_music_manager", yes, raising=False)
    guild = types.SimpleNamespace(id=GID)
    actor = types.SimpleNamespace(id=1)
    text, _view = await cog._request_default_effect(_Player(), guild, actor, "nightcore")
    assert "default effect" in text and store[guild_config.KEY_DEFAULT_EFFECT] == "nightcore"

    asked = {}

    async def fake_refusal(bot, **kw):
        asked.update(kw)
        return None

    monkeypatch.setattr(music_mod.premium_upsell, "for_guild_refusal", fake_refusal)
    monkeypatch.setattr(music_mod.premium_upsell, "invoker_is_admin", lambda a: True)
    monkeypatch.setattr(
        music_mod.premium_upsell, "is_guild_already_top_tier", lambda b, g: False
    )
    premium_on["on"] = False
    text, view = await cog._request_default_effect(_Player(), guild, actor, "bassboost")
    assert asked["limit_key"] == "music_saved_effects" and asked["kind"] == "unlock"
    assert view is None
    assert store[guild_config.KEY_DEFAULT_EFFECT] == "nightcore"  # unchanged


def test_filter_command_keeps_its_free_shape():
    params = {p.name: p for p in music_mod.Music.filter_command.app_command.parameters}
    assert params["preset"].required is True
    assert params["save_as_default"].required is False


# -- dashboard action ---------------------------------------------------


class _DashCog:
    def __init__(self, code):
        self.code = code
        self.calls = []

    async def _save_default_effect(self, gid, key):
        self.calls.append((gid, key))
        return self.code


def _bot(cog):
    return types.SimpleNamespace(get_cog=lambda name: cog)


@pytest.mark.parametrize(
    "code, payload, expected",
    [
        ("saved", {"preset": "nightcore"}, {"ok": True}),
        ("cleared", {"preset": None}, {"ok": True}),
        ("not_premium", {"preset": "nightcore"}, {"ok": False, "reason": "not_premium"}),
        ("unknown_preset", {"preset": "zzz"}, {"ok": False, "reason": "unknown_preset"}),
        ("unknown_preset", {"preset": 5}, {"ok": False, "reason": "unknown_preset"}),
    ],
)
async def test_dashboard_action_codes(code, payload, expected):
    cog = _DashCog(code)
    out = await dma._exec_music_default_effect_set(_bot(cog), GID, payload)
    assert out == expected


async def test_dashboard_null_maps_to_off_and_registry():
    cog = _DashCog("cleared")
    await dma._exec_music_default_effect_set(_bot(cog), GID, {"preset": None})
    assert cog.calls == [(GID, effects.OFF_KEY)]
    assert dma.EXECUTORS["music_default_effect_set"] is dma._exec_music_default_effect_set


# -- review fixes ---------------------------------------------------------


@pytest.mark.parametrize("sent", ["Nightcore", "NIGHTCORE", " nightcore "])
async def test_a_label_or_other_case_stores_the_canonical_key(cog, store, sent):
    """Regression (review P2): the seam validated with resolve_preset (case-
    insensitive, labels too) but stored the raw input, which apply_preset then
    could not find. The dashboard is the caller that sends labels."""
    bot = types.SimpleNamespace(get_cog=lambda name: cog)
    out = await dma._exec_music_default_effect_set(bot, GID, {"preset": sent})
    assert out == {"ok": True}
    assert store[guild_config.KEY_DEFAULT_EFFECT] == "nightcore"
    player = _Player()
    await _start(cog, player)
    assert player.effect_preset == "nightcore"
    assert player.set_filters_calls == 1


@pytest.mark.parametrize("sent", ["OFF", "Off", "off"])
async def test_off_in_any_case_clears(cog, store, sent):
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    assert await cog._save_default_effect(GID, sent) == "cleared"
    assert store[guild_config.KEY_DEFAULT_EFFECT] is None


async def test_clearing_needs_no_premium_on_every_surface(cog, store, premium_on, monkeypatch):
    """Review P3: a lapsed server can always remove what it saved - the seam,
    the slash/button path and the dashboard action alike."""
    premium_on["on"] = False

    async def yes(player, actor):
        return True

    monkeypatch.setattr(cog, "_is_music_manager", yes, raising=False)
    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    assert await cog._save_default_effect(GID, effects.OFF_KEY) == "cleared"
    assert store[guild_config.KEY_DEFAULT_EFFECT] is None

    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    text, view = await cog._request_default_effect(
        _Player(), types.SimpleNamespace(id=GID), types.SimpleNamespace(id=1), "off"
    )
    assert "cleared" in text and view is None
    assert store[guild_config.KEY_DEFAULT_EFFECT] is None

    store[guild_config.KEY_DEFAULT_EFFECT] = "nightcore"
    bot = types.SimpleNamespace(get_cog=lambda name: cog)
    out = await dma._exec_music_default_effect_set(bot, GID, {"preset": None})
    assert out == {"ok": True}
    assert store[guild_config.KEY_DEFAULT_EFFECT] is None
    # Negative control: SAVING still needs Yasuho+.
    assert await cog._save_default_effect(GID, "nightcore") == "not_premium"
    assert store[guild_config.KEY_DEFAULT_EFFECT] is None


async def test_restore_marks_the_effect_done_before_play(monkeypatch):
    """Review P2: TrackStart can land before play()'s REST reply, so the flag
    must already be set when play() is called."""
    from tests.cogs.test_music_queue_cap import (
        _restore_cog,
        _restore_now,
        _restore_row,
        _RestorePlayer,
    )

    seen = []

    class _Watching(_RestorePlayer):
        async def play(self, track, **kwargs):
            seen.append(getattr(self, "default_effect_done", None))
            await super().play(track, **kwargs)

    player = _Watching()
    player.default_effect_done = False
    cog = _restore_cog(player, monkeypatch)
    await cog._restore_one(_restore_row(1), _restore_now())
    assert seen == [True]
