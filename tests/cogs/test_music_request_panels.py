"""Music request panels: post/remove, persistence, limits, lapse, modal -> /play.

Real ``Music`` methods on an ``object.__new__`` instance with the settings blob,
the entitlement verdict and Discord stubbed. No message text is ever read.
"""

from __future__ import annotations

import types

import discord
import pytest

import cogs.music.music as music_mod
from cogs.music import guild_config, requests
from tools import settings

GID = 11


class _Msg:
    def __init__(self, mid):
        self.id = mid
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Text(discord.TextChannel):
    """A TextChannel stand-in (isinstance holds) with scripted I/O."""

    def __init__(self, cid, guild, can_send=True):
        self.id = cid
        self.guild = guild
        self.can_send = can_send
        self.sent = []
        self.gone = set()
        self.partials = []

    @property
    def mention(self):
        return "<#{0}>".format(self.id)

    def permissions_for(self, member):
        return types.SimpleNamespace(view_channel=self.can_send, send_messages=self.can_send)

    async def send(self, content=None, **kwargs):
        msg = _Msg(1000 + self.id + len(self.sent))
        self.sent.append((content, kwargs, msg))
        return msg

    async def fetch_message(self, mid):
        if mid in self.gone:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="x"), "gone")
        return _Msg(mid)

    def get_partial_message(self, mid):
        msg = _Msg(mid)
        self.partials.append(msg)
        return msg


class _Guild:
    def __init__(self):
        self.id = GID
        self.me = object()
        self.channels = {}

    def get_channel_or_thread(self, cid):
        return self.channels.get(cid)


def _guild_with(*cids):
    guild = _Guild()
    for cid in cids:
        guild.channels[cid] = _Text(cid, guild)
    return guild


class _Ctx:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel
        self.author = types.SimpleNamespace(id=5)
        self.interaction = object()
        self.out = []
        self.deferred = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)

    async def send(self, content=None, **kwargs):
        self.out.append((content, kwargs))


@pytest.fixture(autouse=True)
def _clear_debounces():
    requests._REQUEST_DEBOUNCE._seen.clear()
    requests._PRESS_DEBOUNCE._seen.clear()


@pytest.fixture
def blob(monkeypatch):
    data = {}

    async def fake_get(pool, gid, key, default=None):
        return data.get(key, default)

    async def fake_set(pool, gid, key, value):
        data[key] = value

    monkeypatch.setattr(settings, "get_guild", fake_get)
    monkeypatch.setattr(settings, "set_guild", fake_set)
    return data


@pytest.fixture
def limit(monkeypatch):
    state = {"n": 1}
    monkeypatch.setattr(
        requests,
        "resolve_guild_limits",
        lambda bot, gid: types.SimpleNamespace(max_request_panels=state["n"]),
    )
    return state


@pytest.fixture
def upsells(monkeypatch):
    calls = []

    async def fake_refusal(bot, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(requests.premium_upsell, "for_guild_refusal", fake_refusal)
    monkeypatch.setattr(requests.premium_upsell, "is_guild_already_top_tier", lambda b, g: False)
    monkeypatch.setattr(requests.premium_upsell, "invoker_is_admin", lambda m: True)
    return calls


@pytest.fixture
def cog():
    c = object.__new__(music_mod.Music)
    c.bot = types.SimpleNamespace(db_pool=object())
    return c


def _stored(blob):
    return [(e["c"], e["m"]) for e in blob.get(guild_config.KEY_REQUEST_PANELS, [])]


# --- pure ------------------------------------------------------------------


def test_panel_status_active_paused_missing():
    panels = [(1, 10), (2, 20), (3, 30)]
    assert requests.panel_status(panels, 10, 1) == requests.STATUS_ACTIVE
    assert requests.panel_status(panels, 20, 1) == requests.STATUS_PAUSED
    assert requests.panel_status(panels, 30, 3) == requests.STATUS_ACTIVE
    assert requests.panel_status(panels, 99, 3) == requests.STATUS_MISSING
    assert requests.panel_status([], 10, 1) == requests.STATUS_MISSING


def test_coerce_panels_is_total_over_junk():
    raw = [{"c": "1", "m": "10"}, {"c": 2}, "x", {"c": 3, "m": 10}, {"c": 4, "m": 40}]
    assert guild_config.coerce_panels(raw) == [(1, 10), (4, 40)]
    assert guild_config.coerce_panels(None) == []
    assert guild_config.coerce_panels({"c": 1}) == []
    many = [{"c": i + 1, "m": i + 100} for i in range(30)]
    assert len(guild_config.coerce_panels(many)) == guild_config.MAX_STORED_PANELS


# --- post / remove -----------------------------------------------------------


async def test_post_stores_the_location_and_sends_a_persistent_view(cog, blob, limit, upsells):
    guild = _guild_with(1)
    ctx = _Ctx(guild, guild.channels[1])
    await cog.requests_post(ctx, None)
    channel = guild.channels[1]
    content, kwargs, msg = channel.sent[0]
    assert "Song requests" in content
    view = kwargs["view"]
    assert view.timeout is None
    assert [type(i).__name__ for i in view.children] == ["RequestButton"]
    assert _stored(blob) == [(1, msg.id)]
    assert ctx.deferred == [{"ephemeral": True}]
    assert "posted" in ctx.out[-1][0]


async def test_post_refuses_without_send_permission(cog, blob, limit, upsells):
    guild = _guild_with(1)
    guild.channels[1].can_send = False
    ctx = _Ctx(guild, guild.channels[1])
    await cog.requests_post(ctx, None)
    assert guild.channels[1].sent == [] and _stored(blob) == []
    assert "cannot send" in ctx.out[-1][0]


async def test_free_limit_one_panel_then_upsell(cog, blob, limit, upsells):
    guild = _guild_with(1, 2)
    await cog.requests_post(_Ctx(guild, guild.channels[1]), None)
    ctx = _Ctx(guild, guild.channels[2])
    await cog.requests_post(ctx, None)
    assert guild.channels[2].sent == []
    assert len(_stored(blob)) == 1
    assert "already has 1 request panel" in ctx.out[-1][0]
    assert upsells and upsells[0]["limit_key"] == "max_request_panels"


async def test_plus_allows_three_then_refuses_the_fourth(cog, blob, limit, upsells):
    limit["n"] = 3
    guild = _guild_with(1, 2, 3, 4)
    for cid in (1, 2, 3, 4):
        await cog.requests_post(_Ctx(guild, guild.channels[cid]), None)
    assert [c for c, _m in _stored(blob)] == [1, 2, 3]
    assert guild.channels[4].sent == []


async def test_one_panel_per_channel(cog, blob, limit, upsells):
    limit["n"] = 3
    guild = _guild_with(1)
    await cog.requests_post(_Ctx(guild, guild.channels[1]), None)
    ctx = _Ctx(guild, guild.channels[1])
    await cog.requests_post(ctx, None)
    assert len(guild.channels[1].sent) == 1
    assert "already has a request panel" in ctx.out[-1][0]


async def test_deleted_panel_is_pruned_on_the_next_post_and_frees_the_slot(cog, blob, limit, upsells):
    guild = _guild_with(1, 2)
    await cog.requests_post(_Ctx(guild, guild.channels[1]), None)
    stale = _stored(blob)[0][1]
    guild.channels[1].gone.add(stale)
    await cog.requests_post(_Ctx(guild, guild.channels[2]), None)
    assert [c for c, _m in _stored(blob)] == [2]


async def test_deleted_channel_is_pruned_without_crashing(cog, blob, limit, upsells):
    guild = _guild_with(2)
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}]
    await cog.requests_post(_Ctx(guild, guild.channels[2]), None)
    assert [c for c, _m in _stored(blob)] == [2]


async def test_unknown_fetch_errors_keep_the_panel(cog, blob, limit, upsells):
    guild = _guild_with(1, 2)
    channel = guild.channels[1]

    async def boom(mid):
        raise discord.HTTPException(types.SimpleNamespace(status=500, reason="x"), "err")

    channel.fetch_message = boom
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}]
    await cog.requests_post(_Ctx(guild, guild.channels[2]), None)
    # Kept (and so the free slot is still taken): unknown is never "gone".
    assert [c for c, _m in _stored(blob)] == [1]
    assert guild.channels[2].sent == []


async def test_post_save_failure_takes_the_dead_panel_down(cog, blob, limit, upsells, monkeypatch):
    guild = _guild_with(1)

    async def fail(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(settings, "set_guild", fail)
    with pytest.raises(RuntimeError):
        await cog.requests_post(_Ctx(guild, guild.channels[1]), None)
    assert guild.channels[1].sent[0][2].deleted


async def test_remove_drops_the_record_and_the_message(cog, blob, limit, upsells):
    limit["n"] = 3
    guild = _guild_with(1, 2)
    await cog.requests_post(_Ctx(guild, guild.channels[1]), None)
    await cog.requests_post(_Ctx(guild, guild.channels[2]), None)
    ctx = _Ctx(guild, guild.channels[1])
    await cog.requests_remove(ctx, None)
    assert [c for c, _m in _stored(blob)] == [2]
    assert guild.channels[1].partials and guild.channels[1].partials[0].deleted
    again = _Ctx(guild, guild.channels[1])
    await cog.requests_remove(again, None)
    assert "no request panel" in again.out[-1][0]


# --- button / modal ------------------------------------------------------------


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class _Resp:
    def __init__(self):
        self.sent = []
        self.modal = None
        self.deferred = None

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kwargs):
        self.deferred = kwargs


def _interaction(guild, message_id, uid=77):
    return types.SimpleNamespace(
        guild=guild,
        message=types.SimpleNamespace(id=message_id) if message_id else None,
        user=types.SimpleNamespace(id=uid),
        response=_Resp(),
        followup=_Followup(),
        channel=object(),
        client=None,
    )


async def test_active_panel_button_opens_the_modal(cog, blob, limit, upsells):
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}]
    it = _interaction(_guild_with(1), 10)
    await cog.open_request_modal(it)
    assert isinstance(it.response.modal, requests.RequestModal)


async def test_unknown_panel_says_no_longer_active(cog, blob, limit, upsells):
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}]
    it = _interaction(_guild_with(1), 99)
    await cog.open_request_modal(it)
    assert it.response.modal is None
    assert "no longer active" in it.response.sent[0][0]


async def test_lapse_pauses_the_extra_panels_but_the_first_keeps_working(cog, blob, limit, upsells):
    blob[guild_config.KEY_REQUEST_PANELS] = [
        {"c": 1, "m": 10},
        {"c": 2, "m": 20},
        {"c": 3, "m": 30},
    ]
    limit["n"] = 3
    live = _interaction(_guild_with(1), 20)
    await cog.open_request_modal(live)
    assert live.response.modal is not None
    limit["n"] = 1  # the subscription lapsed
    first = _interaction(_guild_with(1), 10, uid=78)
    await cog.open_request_modal(first)
    assert first.response.modal is not None
    extra = _interaction(_guild_with(1, 2, 3), 20)
    await cog.open_request_modal(extra)
    assert extra.response.modal is None
    assert "paused" in extra.response.sent[0][0]
    # Nothing was deleted by the lapse.
    assert len(_stored(blob)) == 3


async def test_request_button_is_reconstructed_from_its_custom_id():
    match = requests.RequestButton.__discord_ui_compiled_template__.fullmatch("music:request")
    assert match is not None
    item = await requests.RequestButton.from_custom_id(None, None, match)
    assert isinstance(item, requests.RequestButton)
    assert item.item.custom_id == "music:request"
    assert requests.RequestButton.__discord_ui_compiled_template__.fullmatch("music:other") is None


async def test_button_callback_delegates_to_the_cog(cog):
    seen = []

    async def fake_open(interaction):
        seen.append(interaction)

    cog.open_request_modal = fake_open
    it = types.SimpleNamespace(client=types.SimpleNamespace(get_cog=lambda n: cog))
    item = requests.RequestButton()
    await requests.RequestButton.callback(item, it)
    assert seen == [it]


def test_cog_registers_and_unregisters_the_dynamic_item():
    c = object.__new__(music_mod.Music)
    added, removed = [], []
    c.bot = types.SimpleNamespace(
        is_ready=lambda: False,
        add_dynamic_items=lambda *i: added.extend(i),
        remove_dynamic_items=lambda *i: removed.extend(i),
    )
    import asyncio

    asyncio.run(music_mod.Music.cog_load(c))
    assert added == [requests.RequestButton]
    c._idle_check = types.SimpleNamespace(cancel=lambda: None)
    c.lyrics_sessions = types.SimpleNamespace(shutdown=lambda: None)
    c.track_failures = types.SimpleNamespace(shutdown=lambda: None)
    music_mod.Music.cog_unload(c)
    assert removed == [requests.RequestButton]


async def test_submit_defers_then_runs_play_for_that_member(cog, monkeypatch):
    requests._REQUEST_DEBOUNCE._seen.clear()
    calls = []

    async def fake_play(ctx, query):
        calls.append((ctx.author.id, ctx.channel, query, it.response.deferred))

    cog._play_query = fake_play
    home = object()
    player = types.SimpleNamespace(home=home)
    guild = types.SimpleNamespace(voice_client=player)
    it = _interaction(guild, 10, uid=321)
    await cog.submit_request(it, "  lofi beats  ")
    assert calls == [(321, home, "lofi beats", {"ephemeral": True})]


async def test_submit_without_a_session_keeps_the_panel_channel(cog):
    requests._REQUEST_DEBOUNCE._seen.clear()
    seen = []

    async def fake_play(ctx, query):
        seen.append(ctx.channel)

    cog._play_query = fake_play
    it = _interaction(types.SimpleNamespace(voice_client=None), 10)
    await cog.submit_request(it, "x")
    assert seen == [it.channel]


async def test_submit_is_debounced_and_blank_is_refused(cog):
    requests._REQUEST_DEBOUNCE._seen.clear()
    calls = []

    async def fake_play(ctx, query):
        calls.append(query)

    cog._play_query = fake_play
    guild = types.SimpleNamespace(voice_client=None)
    it = _interaction(guild, 10, uid=5)
    await cog.submit_request(it, "one")
    again = _interaction(guild, 10, uid=5)
    await cog.submit_request(again, "two")
    assert calls == ["one"]
    assert "too fast" in again.response.sent[0][0]
    blank = _interaction(guild, 10, uid=6)
    await cog.submit_request(blank, "   ")
    assert calls == ["one"] and "Give me a song" in blank.response.sent[0][0]


async def test_paused_press_answers_first_and_only_a_manager_gets_the_upsell(
    cog, blob, limit, monkeypatch
):
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}, {"c": 2, "m": 20}]
    order = []

    class _Up:
        line = "upsell line"

        def view(self):
            return "v"

    async def refusal(bot, **kwargs):
        order.append("upsell")
        return _Up()

    monkeypatch.setattr(requests.premium_upsell, "for_guild_refusal", refusal)
    monkeypatch.setattr(requests.premium_upsell, "is_guild_already_top_tier", lambda b, g: False)
    guild = _guild_with(1, 2)

    monkeypatch.setattr(requests.premium_upsell, "invoker_is_admin", lambda m: False)
    member = _interaction(guild, 20)
    await cog.open_request_modal(member)
    assert "paused" in member.response.sent[0][0]
    assert order == [] and member.followup.sent == []  # no DB write, no LIMIT-REACHED

    monkeypatch.setattr(requests.premium_upsell, "invoker_is_admin", lambda m: True)
    boss = _interaction(guild, 20, uid=78)
    await cog.open_request_modal(boss)
    assert boss.response.sent  # answered first
    assert order == ["upsell"] and boss.followup.sent[0][0] == "upsell line"


def test_deleted_first_panel_does_not_keep_the_next_one_paused():
    panels = [(1, 10), (2, 20)]
    assert requests.panel_status(panels, 20, 1) == requests.STATUS_PAUSED
    assert (
        requests.panel_status(panels, 20, 1, lambda cid: cid != 1)
        == requests.STATUS_ACTIVE
    )
    # The pressed panel itself is never skipped.
    assert requests.panel_status(panels, 10, 1, lambda cid: False) == requests.STATUS_ACTIVE


async def test_button_press_is_debounced_per_member(cog, blob, limit, upsells):
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}]
    guild = _guild_with(1)
    first = _interaction(guild, 10, uid=9)
    await cog.open_request_modal(first)
    assert first.response.modal is not None
    again = _interaction(guild, 10, uid=9)
    await cog.open_request_modal(again)
    assert again.response.modal is None and "too fast" in again.response.sent[0][0]
    other = _interaction(guild, 10, uid=10)
    await cog.open_request_modal(other)
    assert other.response.modal is not None


async def test_modal_expires_instead_of_living_forever(cog):
    modal = requests.RequestModal(cog)
    assert modal.timeout == requests.MODAL_TIMEOUT == 300


async def test_post_aborts_without_overwriting_when_the_read_fails(
    cog, blob, limit, upsells, monkeypatch
):
    blob[guild_config.KEY_REQUEST_PANELS] = [{"c": 1, "m": 10}]
    guild = _guild_with(1, 2)

    async def boom(pool, gid, key, default=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(settings, "get_guild", boom)
    ctx = _Ctx(guild, guild.channels[2])
    await cog.requests_post(ctx, None)
    assert guild.channels[2].sent == []
    assert blob[guild_config.KEY_REQUEST_PANELS] == [{"c": 1, "m": 10}]
    assert "went wrong" in ctx.out[-1][0]
    rm = _Ctx(guild, guild.channels[1])
    await cog.requests_remove(rm, None)
    assert blob[guild_config.KEY_REQUEST_PANELS] == [{"c": 1, "m": 10}]
    assert "went wrong" in rm.out[-1][0]


async def test_limit_message_is_plural_aware(cog, blob, limit, upsells):
    limit["n"] = 2
    guild = _guild_with(1, 2, 3)
    for cid in (1, 2):
        await cog.requests_post(_Ctx(guild, guild.channels[cid]), None)
    ctx = _Ctx(guild, guild.channels[3])
    await cog.requests_post(ctx, None)
    assert "already has 2 request panels," in ctx.out[-1][0]
    limit["n"] = 1
    blob[guild_config.KEY_REQUEST_PANELS] = blob[guild_config.KEY_REQUEST_PANELS][:1]
    ctx = _Ctx(guild, guild.channels[3])
    await cog.requests_post(ctx, None)
    assert "already has 1 request panel," in ctx.out[-1][0]
