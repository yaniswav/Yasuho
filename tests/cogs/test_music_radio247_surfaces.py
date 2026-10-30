"""24/7 playlist surfaces (Yasuho+ V3a-3): /music alwayson radio, the status
line and the dashboard action, all over the one ``_set_radio_playlist`` seam."""

from __future__ import annotations

import types

import pytest

from tests.cogs.test_music_247 import (
    _FakeBot,
    _FakeCtx,
    _FakeGuild,
    _make_cog,
    _RealVoiceChannel,
    _StubPremium,
)

import cogs.music.music as music_mod
from cogs.system import dashboard_music_actions as dma

GID = 1


class _Ctx(_FakeCtx):
    def __init__(self, guild):
        super().__init__(guild)
        self.deferred = 0

    async def defer(self, **kwargs):
        self.deferred += 1


async def _cog(fake_pool, *, premium=True, enabled=True, playlists=("Chill Mix",)):
    c = _make_cog(_FakeBot(pool=fake_pool, premium_resolver=_StubPremium(premium)))
    if enabled:
        await c.always_on.enable(fake_pool, GID, 10)
    c.refreshed = []
    c.radio247_refresh = lambda gid: c.refreshed.append(gid) or True
    norms = {p.lower(): p for p in playlists}

    async def _fetch_name(gid, norm):
        return norms.get(norm)

    c._fetch_guild_playlist_name = _fetch_name
    return c


def _ctx():
    return _Ctx(_FakeGuild(GID, channels={10: _RealVoiceChannel(10, guild=None)}))


def _writes(pool):
    return [c for c in pool.calls if c[0] == "execute" and "radio_playlist" in c[1]]


async def _run(c, ctx, playlist=None, order="shuffle"):
    await music_mod.Music.alwayson_radio.callback(c, ctx, playlist, order)


async def test_set_stores_refreshes_and_states_playlist_and_order(fake_pool):
    c = await _cog(fake_pool)
    ctx = _ctx()
    await _run(c, ctx, "chill mix", "order")
    assert ctx.deferred == 1
    assert c.always_on.radio(GID) == ("chill mix", False)
    assert c.refreshed == [GID]
    text = ctx.last_text()
    assert "Chill Mix" in text and "in order" in text

    ctx = _ctx()
    await _run(c, ctx, "Chill Mix")
    assert c.always_on.radio(GID) == ("chill mix", True)
    assert "shuffle" in ctx.last_text()


async def test_off_clears_and_refreshes(fake_pool):
    c = await _cog(fake_pool)
    await _run(c, _ctx(), "chill mix")
    ctx = _ctx()
    await _run(c, ctx, None)
    assert c.always_on.radio(GID) is None
    assert c.refreshed == [GID, GID]
    assert "off" in ctx.last_text()


async def test_not_premium_refuses_with_upsell_and_writes_nothing(fake_pool):
    c = await _cog(fake_pool, premium=False)
    ctx = _ctx()
    await _run(c, ctx, "chill mix")
    assert "/premium" in ctx.last_text()
    assert c.always_on.radio(GID) is None
    assert not _writes(fake_pool) and not c.refreshed


async def test_not_247_refuses(fake_pool):
    c = await _cog(fake_pool, enabled=False)
    ctx = _ctx()
    await _run(c, ctx, "chill mix")
    assert "/music alwayson enable" in ctx.last_text()
    assert not _writes(fake_pool) and not c.refreshed


async def test_playlist_not_found_refuses(fake_pool):
    c = await _cog(fake_pool)
    ctx = _ctx()
    await _run(c, ctx, "nope")
    assert "nope" in ctx.last_text()
    assert c.always_on.radio(GID) is None
    assert not _writes(fake_pool) and not c.refreshed


async def test_row_vanished_between_check_and_write_is_not_247(fake_pool):
    c = await _cog(fake_pool)
    fake_pool.execute_return = "UPDATE 0"
    code, _name = await c._set_radio_playlist(GID, "chill mix", True)
    assert code == "not_247" and not c.refreshed


async def test_status_line_both_states(fake_pool):
    c = await _cog(fake_pool)
    ctx = _ctx()
    await c._send_alwayson_status(ctx)
    assert "24/7 playlist: off" in ctx.last_text()

    await _run(c, _ctx(), "chill mix", "order")
    ctx = _ctx()
    await c._send_alwayson_status(ctx)
    assert "24/7 playlist: **Chill Mix** (in order)" in ctx.last_text()
    assert "chill mix" not in ctx.last_text()


async def test_free_status_unchanged_apart_from_the_new_line(fake_pool):
    c = await _cog(fake_pool, premium=False)
    ctx = _ctx()
    await c._send_alwayson_status(ctx)
    assert "inactive" in ctx.last_text() and "Yasuho+" in ctx.last_text()
    off = await _cog(fake_pool, enabled=False)
    ctx = _ctx()
    await off._send_alwayson_status(ctx)
    assert ctx.last_text() == "24/7 is off for this server. See /premium to turn it on."


# -- dashboard action -----------------------------------------------------


def _bot(c):
    return types.SimpleNamespace(get_cog=lambda name: c)


async def test_dashboard_set_and_off(fake_pool):
    c = await _cog(fake_pool)
    out = await dma._exec_music_247_radio_set(
        _bot(c), GID, {"playlist": "chill mix", "shuffle": False}
    )
    assert out == {"ok": True}
    assert c.always_on.radio(GID) == ("chill mix", False)
    assert c.refreshed == [GID]
    out = await dma._exec_music_247_radio_set(_bot(c), GID, {"playlist": None})
    assert out == {"ok": True} and c.always_on.radio(GID) is None
    assert dma.EXECUTORS["music_247_radio_set"] is dma._exec_music_247_radio_set


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"premium": False}, "not_premium"),
        ({"enabled": False}, "not_247"),
        ({"playlists": ()}, "playlist_not_found"),
    ],
)
async def test_dashboard_reason_codes(fake_pool, kwargs, reason):
    c = await _cog(fake_pool, **kwargs)
    out = await dma._exec_music_247_radio_set(
        _bot(c), GID, {"playlist": "chill mix", "shuffle": True}
    )
    assert out == {"ok": False, "reason": reason}
    assert not c.refreshed


@pytest.mark.parametrize(
    "payload",
    [
        {"playlist": 5},
        {"playlist": ["a"]},
        {"playlist": "chill mix", "shuffle": "yes"},
        {"playlist": "chill mix", "shuffle": 1},
    ],
)
async def test_dashboard_malformed_payload(fake_pool, payload):
    c = await _cog(fake_pool)
    out = await dma._exec_music_247_radio_set(_bot(c), GID, payload)
    assert out == {"ok": False, "reason": "bad_payload"}
    assert c.always_on.radio(GID) is None


async def test_dashboard_blank_playlist_and_no_cog(fake_pool):
    c = await _cog(fake_pool)
    out = await dma._exec_music_247_radio_set(_bot(c), GID, {"playlist": "  "})
    assert out == {"ok": False, "reason": "playlist_not_found"}
    out = await dma._exec_music_247_radio_set(_bot(None), GID, {"playlist": None})
    assert out == {"ok": False, "reason": "no_session"}
