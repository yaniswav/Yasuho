"""/serverplaylist import + unlink: the command flow over stubbed DB helpers.

The SQL itself (caps inside the statement, replace, unlink) is exercised against
a real Postgres in test_music_playlist_sync_pg.py; here the DB helpers are
recorders so each test reads as one decision of the command.
"""

from __future__ import annotations

import types

import pytest

from cogs.music import always_on, playlist_sync
from cogs.music import playlists_shared as ps
from tools import premium
from tools.quotas import QuotaRegistry


class _Limits:
    def __init__(self, guilds=25, tracks=200, linked=0):
        self.max_guild_playlists = guilds
        self.max_playlist_tracks = tracks
        self.max_linked_playlists = linked


class _Premium:
    def __init__(self, limits):
        self._limits = limits

    def for_guild(self, guild_id):
        return self._limits


class _Ctx:
    def __init__(self):
        self.guild = types.SimpleNamespace(id=1, name="Guild")
        self.author = types.SimpleNamespace(id=10)
        self.interaction = None
        self.sent = []
        self.events = []

    async def defer(self, *a, **k):
        self.events.append("defer")

    async def send(self, *args, **kwargs):
        self.events.append("send")
        self.sent.append((args, kwargs))
        return types.SimpleNamespace()

    def text(self):
        args, kwargs = self.sent[-1]
        return args[0] if args else kwargs.get("content", "")


class _Result:
    def __init__(self, tracks=None, *, error=False, empty=False, bare=None, severity="common"):
        self._error, self._empty = error, empty
        self.exception = types.SimpleNamespace(severity=severity)
        self.result = bare if bare is not None else (
            types.SimpleNamespace(name="PL", tracks=tracks) if tracks is not None else None
        )

    def is_error(self):
        return self._error

    def is_empty(self):
        return self._empty


def _tracks(n):
    return [types.SimpleNamespace(encoded=f"e{i}", length=1000) for i in range(n)]


class _Client:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.queries = result, raises, []

    async def search_track(self, query, source=None):
        self.queries.append(query)
        if self.raises:
            raise self.raises
        return self.result


class _Cog(ps.ServerPlaylistMixin, playlist_sync.PlaylistSyncMixin):
    def __init__(self, limits, client, *, manage=False, row=None, count=0, linked=0):
        self.bot = types.SimpleNamespace(
            db_pool=object(), premium=_Premium(limits), sl_client=client
        )
        self.always_on = always_on.AlwaysOnStore()
        self.quotas = QuotaRegistry()
        self.client = client
        self.manage, self.row, self.count, self.linked = manage, row, count, linked
        self.archived = False
        self.new_calls, self.replace_calls = [], []
        self.replace_result = "replaced"

    def _has_manage_guild(self, actor):
        return self.manage

    @staticmethod
    def _format_retry_delay(seconds):
        return f"{int(seconds)}s"

    async def _load_uncached(self, url):
        self.client.queries.append(url)
        if self.client.raises:
            raise self.client.raises
        return self.client.result

    def _nodes_available(self):
        return True

    async def _fetch_guild_playlist(self, guild_id, norm):
        return self.row

    async def _guild_playlist_count(self, guild_id):
        return self.count

    async def _linked_count(self, guild_id):
        return self.linked

    async def _guild_playlist_archival(self, guild_id, cap):
        return types.SimpleNamespace(is_active=lambda key: not self.archived)

    async def _import_new_playlist(self, *args, **kwargs):
        self.new_calls.append((args, kwargs))
        return "saved"

    async def _replace_playlist(self, guild_id, norm, **kwargs):
        self.replace_calls.append((guild_id, norm, kwargs))
        return self.replace_result


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    async def ok(query):
        return None

    monkeypatch.setattr(playlist_sync.urlguard, "check_query", ok)


@pytest.fixture
def upsells(monkeypatch):
    calls = []

    async def fake(bot, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(ps.premium_upsell, "for_guild_refusal", fake)
    monkeypatch.setattr(ps.premium_upsell, "is_guild_already_top_tier", lambda b, g: False)
    monkeypatch.setattr(ps.premium_upsell, "invoker_is_admin", lambda m: True)
    return calls


URL = "https://open.spotify.com/playlist/abc?si=1"


async def _run(cog, ctx, url=URL, name="Road Trip", keep_synced=False):
    await ps.ServerPlaylistMixin.serverplaylist_import.callback(
        cog, ctx, url, name=name, keep_synced=keep_synced
    )


async def test_import_saves_the_tracks_and_defers_before_loading(upsells):
    client = _Client(_Result(_tracks(3)))
    cog, ctx = _Cog(_Limits(), client), _Ctx()
    await _run(cog, ctx)
    assert ctx.events[0] == "defer"
    (args, kwargs) = cog.new_calls[0]
    assert args[2] == "road trip" and list(args[4]) == ["e0", "e1", "e2"]
    assert kwargs["source_url"] is None and kwargs["next_sync_at"] is None
    assert "Imported **Road Trip** with 3 tracks" in ctx.text()
    assert client.queries == [URL]


async def test_import_truncates_to_the_limit_and_says_so(upsells):
    cog, ctx = _Cog(_Limits(tracks=2), _Client(_Result(_tracks(5)))), _Ctx()
    await _run(cog, ctx)
    args, _kw = cog.new_calls[0]
    assert list(args[4]) == ["e0", "e1"] and args[5] == 2000
    assert "with 2 tracks" in ctx.text()
    assert "3 tracks were over the playlist limit" in ctx.text()


async def test_import_no_truncation_note_at_exactly_the_limit(upsells):
    cog, ctx = _Cog(_Limits(tracks=3), _Client(_Result(_tracks(3)))), _Ctx()
    await _run(cog, ctx)
    assert "over the playlist limit" not in ctx.text()


async def test_import_refuses_when_the_guild_is_full(upsells):
    cog, ctx = _Cog(_Limits(guilds=2), _Client(_Result(_tracks(3))), count=2), _Ctx()
    await _run(cog, ctx)
    assert cog.new_calls == []
    assert upsells[0]["limit_key"] == "guild_playlists"


@pytest.mark.parametrize(
    "url, result, fragment",
    [
        ("http://127.0.0.1:2333/v4/info", None, "public links"),
        ("daft punk", None, "isn't a playlist"),
        ("https://x.test/" + "a" * 600, None, "isn't a playlist"),
        (URL, _Result(_tracks(1)[0:0], empty=True), "no tracks"),
        (URL, _Result(error=True), "couldn't load"),
        (URL, _Result(bare=types.SimpleNamespace(title="one track")), "isn't a playlist"),
        (URL, _Result([]), "no tracks"),
    ],
)
async def test_import_refusals_save_nothing(upsells, monkeypatch, url, result, fragment):
    if url.startswith("http://127"):
        monkeypatch.undo()  # the real SSRF guard, on a literal loopback IP
    cog, ctx = _Cog(_Limits(), _Client(result)), _Ctx()
    await _run(cog, ctx, url=url)
    assert cog.new_calls == [] and cog.replace_calls == []
    assert fragment in ctx.text()


async def test_import_without_a_node_reports_it(upsells):
    cog, ctx = _Cog(_Limits(), _Client(raises=RuntimeError())), _Ctx()
    await _run(cog, ctx)
    assert "no Lavalink node" in ctx.text() and cog.new_calls == []


async def test_existing_name_asks_before_replacing(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog, ctx = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row), _Ctx()
    await _run(cog, ctx)
    assert cog.replace_calls == [] and cog.new_calls == []
    args, kwargs = ctx.sent[-1]
    assert "already exists" in args[0] and "2 tracks" in args[0]
    view = kwargs["view"]
    assert view.author_id == 10
    edit = _Recorder()
    interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(edit_message=edit), user=ctx.author
    )
    await view._confirm(interaction)
    assert edit.calls[-1][1]["content"] == "Replaced the tracks of the playlist."
    assert cog.replace_calls[0][1] == "road trip"
    assert list(cog.replace_calls[0][2]["tracks"]) == ["e0", "e1"]
    assert cog.replace_calls[0][2]["source_url"] is None  # a plain re-import unlinks
    # the write is pinned to the row the prompt showed
    assert cog.replace_calls[0][2]["expect_creator"] == 10
    assert cog.replace_calls[0][2]["expect_created"] == "T0"


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *a, **k):
        self.calls.append((a, k))


async def test_replace_cancel_changes_nothing(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog, ctx = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row), _Ctx()
    await _run(cog, ctx)
    view = ctx.sent[-1][1]["view"]
    await view._cancel(types.SimpleNamespace(response=types.SimpleNamespace(edit_message=_Recorder())))
    assert cog.replace_calls == []


async def test_replace_is_creator_or_moderator_only(upsells):
    row = {"name": "Road Trip", "creator_id": 99, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog, ctx = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row), _Ctx()
    await _run(cog, ctx)
    assert "creator or a moderator" in ctx.text() and cog.replace_calls == []


async def test_replacing_an_archived_playlist_is_refused(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog, ctx = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row), _Ctx()
    cog.archived = True
    await _run(cog, ctx)
    assert "is archived" in ctx.text() and cog.replace_calls == []


# -- linking (Yasuho+) --------------------------------------------------------


async def test_keep_synced_on_a_free_guild_is_an_upsell_not_an_import(upsells):
    client = _Client(_Result(_tracks(2)))
    cog, ctx = _Cog(_Limits(linked=0), client, manage=True), _Ctx()
    await _run(cog, ctx, keep_synced=True)
    assert cog.new_calls == [] and client.queries == []  # refused before any load
    assert upsells[0]["limit_key"] == "linked_playlists"
    assert upsells[0]["kind"] == "unlock"
    assert upsells[0]["benefit"] == str(premium.GUILD_PREMIUM.max_linked_playlists)
    assert "Yasuho+" in ctx.text()


async def test_keep_synced_needs_manage_server(upsells):
    cog, ctx = _Cog(_Limits(linked=10), _Client(_Result(_tracks(2))), manage=False), _Ctx()
    await _run(cog, ctx, keep_synced=True)
    assert cog.new_calls == [] and "Only moderators" in ctx.text()


async def test_keep_synced_stores_the_link_and_a_schedule(upsells):
    cog, ctx = _Cog(_Limits(linked=10), _Client(_Result(_tracks(2))), manage=True), _Ctx()
    await _run(cog, ctx, keep_synced=True)
    _args, kwargs = cog.new_calls[0]
    assert kwargs["source_url"] == URL and kwargs["max_linked"] == 10
    assert kwargs["next_sync_at"] is not None
    assert "linked to open.spotify.com" in ctx.text()


async def test_keep_synced_refuses_past_the_linked_cap(upsells):
    cog, ctx = _Cog(_Limits(linked=2), _Client(_Result(_tracks(2))), manage=True, linked=2), _Ctx()
    await _run(cog, ctx, keep_synced=True)
    assert cog.new_calls == []
    assert upsells[0]["limit_key"] == "linked_playlists" and upsells[0]["kind"] == "raise"


async def test_relinking_an_already_linked_playlist_does_not_count_twice(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": "https://a.test/p", "created_at": "T0"}
    cog = _Cog(_Limits(linked=2), _Client(_Result(_tracks(2))), manage=True, row=row, linked=2)
    ctx = _Ctx()
    await _run(cog, ctx, keep_synced=True)
    assert "already exists" in ctx.text()  # reached the replace prompt, not the cap


# -- unlink ------------------------------------------------------------------


async def test_unlink_is_creator_or_moderator_only_and_reports_state(upsells, monkeypatch):
    row = {"name": "Road Trip", "creator_id": 99}
    cog, ctx = _Cog(_Limits(), None, row=row), _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_unlink.callback(cog, ctx, name="road trip")
    assert "creator or a moderator" in ctx.text()

    row = {"name": "Road Trip", "creator_id": 10}
    cog, ctx = _Cog(_Limits(), None, row=row), _Ctx()
    states = iter([True, False])

    async def unlink(guild_id, norm):
        return next(states)

    cog._unlink_playlist = unlink
    await ps.ServerPlaylistMixin.serverplaylist_unlink.callback(cog, ctx, name="road trip")
    assert "no longer linked" in ctx.text()
    await ps.ServerPlaylistMixin.serverplaylist_unlink.callback(cog, ctx, name="road trip")
    assert "isn't linked" in ctx.text()


# -- display -----------------------------------------------------------------


def test_link_line_shows_domain_last_sync_and_paused():
    import datetime

    synced = datetime.datetime.now(datetime.timezone.utc)
    nxt = synced + datetime.timedelta(days=7)
    assert ps.link_line({"source_url": None}) is None
    line = ps.link_line({"source_url": URL, "synced_at": synced, "next_sync_at": nxt})
    assert "open.spotify.com" in line and f"<t:{int(synced.timestamp())}:R>" in line
    paused = ps.link_line({"source_url": URL, "synced_at": synced, "next_sync_at": None})
    assert "open.spotify.com" in paused and "sync paused" in paused
    assert URL not in line  # only the domain is shown, never the full link


# -- replace confirm: the row is re-checked at click time ----------------------


async def _prompt(cog):
    ctx = _Ctx()
    await _run(cog, ctx)
    edit = _Recorder()
    interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(edit_message=edit), user=ctx.author
    )
    return ctx.sent[-1][1]["view"], interaction, edit


async def test_confirm_refuses_when_the_row_was_recreated(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row)
    view, interaction, edit = await _prompt(cog)
    cog.row = dict(row, created_at="T1")  # deleted and recreated meanwhile
    await view._confirm(interaction)
    assert cog.replace_calls == []
    assert "changed, nothing was replaced" in edit.calls[-1][1]["content"]


async def test_confirm_refuses_when_the_clicker_lost_the_right(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row)
    view, interaction, edit = await _prompt(cog)
    cog.row = dict(row, creator_id=99)
    await view._confirm(interaction)
    assert cog.replace_calls == []


async def test_confirm_refuses_when_the_playlist_became_archived(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row)
    view, interaction, edit = await _prompt(cog)
    cog.archived = True
    await view._confirm(interaction)
    assert cog.replace_calls == [] and "changed" in edit.calls[-1][1]["content"]


async def test_confirm_reports_a_row_deleted_meanwhile(upsells):
    row = {"name": "Road Trip", "creator_id": 10, "track_count": 5, "source_url": None, "created_at": "T0"}
    cog = _Cog(_Limits(), _Client(_Result(_tracks(2))), row=row)
    view, interaction, edit = await _prompt(cog)
    cog.row = None
    await view._confirm(interaction)
    assert "no longer exists" in edit.calls[-1][1]["content"]


# -- cooldowns -----------------------------------------------------------------


async def test_a_second_import_within_30_seconds_is_refused_before_any_load(upsells):
    cog = _Cog(_Limits(), _Client(_Result(_tracks(2))))
    await _run(cog, _Ctx())
    ctx = _Ctx()
    await _run(cog, ctx, name="Other")
    assert len(cog.new_calls) == 1 and len(cog.client.queries) == 1
    assert "importing too fast" in ctx.text() and ctx.sent[-1][1]["ephemeral"] is True
    assert "defer" not in ctx.events


async def test_the_guild_ceiling_stops_a_crowd_of_different_members(upsells):
    cog = _Cog(_Limits(guilds=99), _Client(_Result(_tracks(1))))
    for member in range(5):
        ctx = _Ctx()
        ctx.author = types.SimpleNamespace(id=100 + member)
        await _run(cog, ctx, name=f"P{member}")
    ctx = _Ctx()
    ctx.author = types.SimpleNamespace(id=200)
    await _run(cog, ctx, name="P9")
    assert len(cog.client.queries) == 5
    assert "too many playlists lately" in ctx.text()


async def test_a_refusal_before_the_load_does_not_charge_the_quota(upsells):
    cog = _Cog(_Limits(linked=0), _Client(_Result(_tracks(2))), manage=True)
    await _run(cog, _Ctx(), keep_synced=True)  # upsell refusal
    await _run(cog, _Ctx())  # the same member can still import at once
    assert len(cog.new_calls) == 1


# -- the linked cap is its own message ------------------------------------------


async def test_a_linked_cap_race_says_linked_not_playlists(upsells):
    cog = _Cog(_Limits(linked=2), _Client(_Result(_tracks(2))), manage=True)

    async def full(*a, **k):
        return "link_full"

    cog._import_new_playlist = full
    ctx = _Ctx()
    await _run(cog, ctx, keep_synced=True)
    assert "maximum of 2 linked playlists" in ctx.text()
    assert upsells[-1]["limit_key"] == "linked_playlists"


# -- the list card stays under Discord's text limit ------------------------------


def test_fit_blocks_truncates_and_counts_the_rest():
    blocks = ["x" * 100] * 50
    shown, hidden = ps.fit_blocks(blocks, 1000)
    assert len(shown) + hidden == 50 and hidden > 0
    assert sum(len(b) + 1 for b in shown) <= 1000
    assert ps.fit_blocks(["a", "b"], 1000) == (["a", "b"], 0)
    assert ps.fit_blocks(["z" * 5000], 100) == (["z" * 5000], 0)  # first always shows


def test_a_card_of_150_linked_playlists_fits_4000_characters():
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [
        {
            "name": f"Playlist number {i} " + "x" * 40,
            "count": 500,
            "duration": "12:34:56",
            "creator_id": 123456789012345678,
            "created_ts": int(now.timestamp()),
            "archived": False,
            "link": ps.link_line(
                {"source_url": URL, "synced_at": now, "next_sync_at": now}
            ),
        }
        for i in range(150)
    ]
    card = ps._PlaylistListCard("G" * 100, rows)
    texts = []

    def walk(item):
        if isinstance(item, ps.discord.ui.TextDisplay):
            texts.append(item.content)
        for child in getattr(item, "children", []):
            walk(child)

    walk(card)
    assert sum(len(t) for t in texts) <= 4000
    assert "... and" in texts[-1] and "more playlists" in texts[-1]
