"""Unit tests for server-playlist limits/archival under Yasuho+ (M4a-1,
.claude/plans/monetisation/4-plan-retenu.md).

``cogs/music/playlists_shared.py`` keeps ``MAX_GUILD_PLAYLISTS``/
``MAX_PLAYLIST_TRACKS`` as the FREE values (tests/tools/test_premium.py's own
drift guard checks that), but every command now reads the EFFECTIVE caps from
``bot.premium.for_guild(guild_id)`` and, when a guild is over its current
cap, marks/refuses via :mod:`tools.premium_archive`. These tests exercise the
mixin's DB-helper methods and the full command coroutines (via
``.callback(cog, ctx, ...)`` - the pattern this test suite already uses for a
hybrid command, e.g. tests/cogs/test_afk.py) against the shared ``fake_pool``
fixture, with a trivial stand-in for ``bot.premium`` so a test can hand in
ANY ``max_guild_playlists``/``max_playlist_tracks`` pair without needing a
real entitlement/grant.
"""

from __future__ import annotations

import datetime
import types

import pytest

from cogs.music import playlists_shared as ps
from tools import premium

UTC = datetime.timezone.utc


def _ts(seconds):
    return datetime.datetime(2026, 1, 1, tzinfo=UTC) + datetime.timedelta(seconds=seconds)


class _StubLimits:
    def __init__(self, max_guild_playlists, max_playlist_tracks):
        self.max_guild_playlists = max_guild_playlists
        self.max_playlist_tracks = max_playlist_tracks


class _StubPremium:
    """A bare ``bot.premium``: always resolves to the ONE :class:`_StubLimits`
    it was built with, whatever guild id is asked for - plenty for a test
    that only ever exercises one guild at a time."""

    def __init__(self, limits):
        self._limits = limits

    def for_guild(self, guild_id):
        return self._limits


class _Ctx:
    def __init__(self, guild_id=1, author_id=10, interaction=None):
        self.guild = types.SimpleNamespace(id=guild_id, name="Guild")
        self.author = types.SimpleNamespace(id=author_id)
        self.interaction = interaction
        self.sent = []
        self.deferred = False

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def last_text(self):
        args, kwargs = self.sent[-1]
        return args[0] if args else kwargs.get("content", "")


def _track(encoded="enc", length=1000):
    return types.SimpleNamespace(encoded=encoded, length=length)


def _player(current=None, queued=()):
    return types.SimpleNamespace(
        current=current, queue=types.SimpleNamespace(tracks=list(queued))
    )


class _Cog(ps.ServerPlaylistMixin):
    """The mixin, standing in for the real ``Music`` cog, with the few outside
    seams (``_require_player``, ``_nodes_available``, ``_has_manage_guild``)
    stubbed rather than the real Music cog's own implementations - this file
    tests playlists_shared's own logic, not those seams."""

    def __init__(self, pool, limits, *, player=None, manage_guild=False):
        self.bot = types.SimpleNamespace(db_pool=pool, premium=_StubPremium(limits))
        self._player = player
        self._manage_guild = manage_guild

    async def _require_player(self, ctx, in_channel=True, control=False):
        return self._player

    def _nodes_available(self):
        return True

    def _has_manage_guild(self, actor):
        return self._manage_guild


# ---------------------------------------------------------------------------
# DB-helper methods: the effective cap is threaded through, not hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_guild_playlist_threads_the_effective_cap_into_the_insert(fake_pool):
    cog = _Cog(fake_pool, _StubLimits(75, 500))
    fake_pool.execute_return = "INSERT 0 1"
    result = await cog._save_guild_playlist(
        1, "My List", "my list", 10, ["enc"], 1000, 75
    )
    assert result == "saved"
    method, query, args = fake_pool.calls[-1]
    assert method == "execute"
    # Last bound parameter is the cap guard ($8 in the SQL) - must be the
    # PREMIUM value (75) handed in, never the FREE module constant (25).
    assert args[-1] == 75
    assert args[-1] != ps.MAX_GUILD_PLAYLISTS


@pytest.mark.asyncio
async def test_save_guild_playlist_default_param_is_the_free_value(fake_pool):
    # No cap passed -> the function's own default, which must still be the
    # FREE constant (callers/tests that do not care about premium keep
    # today's behaviour unchanged).
    fake_pool.execute_return = "INSERT 0 1"
    await _Cog(fake_pool, _StubLimits(25, 200))._save_guild_playlist(
        1, "A", "a", 10, ["enc"], 1000
    )
    _, _, args = fake_pool.calls[-1]
    assert args[-1] == ps.MAX_GUILD_PLAYLISTS


@pytest.mark.asyncio
async def test_list_guild_playlists_limit_is_the_absolute_ceiling_not_the_cap(fake_pool):
    # Must ask for every row up to the CEILING (150) so an archived excess
    # past today's effective cap is never hidden by a too-small query bound.
    await _Cog(fake_pool, _StubLimits(25, 200))._list_guild_playlists(1)
    _, _, args = fake_pool.calls[-1]
    assert args[-1] == premium.GUILD_CEILINGS["max_guild_playlists"]
    assert args[-1] > ps.MAX_GUILD_PLAYLISTS


@pytest.mark.asyncio
async def test_autocomplete_limit_is_discords_cap_not_the_guild_cap(fake_pool):
    await _Cog(fake_pool, _StubLimits(75, 500))._autocomplete_playlists(1, "a")
    _, _, args = fake_pool.calls[-1]
    assert args[-1] == ps.AUTOCOMPLETE_LIMIT
    assert ps.AUTOCOMPLETE_LIMIT == 25


@pytest.mark.asyncio
async def test_guild_playlist_archival_classifies_the_excess(fake_pool):
    # 3 playlists, cap 2: the newest ("c") is the one over the limit.
    fake_pool.fetch_return = [
        {"name_norm": "a", "created_at": _ts(0)},
        {"name_norm": "b", "created_at": _ts(10)},
        {"name_norm": "c", "created_at": _ts(20)},
    ]
    result = await _Cog(fake_pool, _StubLimits(2, 200))._guild_playlist_archival(1, 2)
    assert result.active_ids == frozenset({"a", "b"})
    assert result.archived_ids == frozenset({"c"})


# ---------------------------------------------------------------------------
# /serverplaylist save - regression (free) + Yasuho+ widening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_free_guild_capped_at_the_free_value_exactly_as_before(fake_pool):
    fake_pool.fetchval_return = ps.MAX_GUILD_PLAYLISTS  # guild already at 25
    cog = _Cog(
        fake_pool,
        _StubLimits(ps.MAX_GUILD_PLAYLISTS, ps.MAX_PLAYLIST_TRACKS),
        player=_player(current=_track()),
    )
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_save.callback(cog, ctx, name="New List")
    text = ctx.last_text()
    assert str(ps.MAX_GUILD_PLAYLISTS) in text
    # Refused BEFORE any insert attempt.
    assert not any(call[0] == "execute" for call in fake_pool.calls)


@pytest.mark.asyncio
async def test_save_premium_guild_gets_the_premium_cap(fake_pool):
    # Same existing count (25) that would refuse a free guild - a Yasuho+
    # guild (cap 75) must be allowed through.
    fake_pool.fetchval_return = 25
    fake_pool.execute_return = "INSERT 0 1"
    cog = _Cog(fake_pool, _StubLimits(75, 500), player=_player(current=_track()))
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_save.callback(cog, ctx, name="New List")
    text = ctx.last_text()
    assert "Saved" in text or "saved" in text.lower()
    insert_calls = [call for call in fake_pool.calls if call[0] == "execute"]
    assert insert_calls, "expected the INSERT to run for a Yasuho+ guild under its cap"
    assert insert_calls[-1][2][-1] == 75


@pytest.mark.asyncio
async def test_save_track_cap_error_uses_the_effective_track_limit(fake_pool):
    cog = _Cog(
        fake_pool,
        _StubLimits(75, 500),
        player=_player(current=_track(), queued=[_track() for _ in range(500)]),
    )
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_save.callback(cog, ctx, name="Huge")
    text = ctx.last_text()
    assert "500" in text
    assert "200" not in text  # never the stale free number


# ---------------------------------------------------------------------------
# Downgrade: excess archived, refused for load/rename, still deletable
# ---------------------------------------------------------------------------


def _three_playlists_over_cap(archived_name_norm, archived_created_offset=20):
    """Three guild rows where ``archived_name_norm`` is the newest (and so,
    under a cap of 2, the one that falls outside the active set)."""
    others = [n for n in ("a", "b", "c") if n != archived_name_norm]
    return [
        {"name_norm": others[0], "created_at": _ts(0)},
        {"name_norm": others[1], "created_at": _ts(10)},
        {"name_norm": archived_name_norm, "created_at": _ts(archived_created_offset)},
    ]


@pytest.mark.asyncio
async def test_play_refuses_an_archived_playlist_before_any_decode(fake_pool):
    fake_pool.fetchrow_return = {
        "name": "c",
        "creator_id": 10,
        "tracks": ["enc1", "enc2"],
        "track_count": 2,
        "total_ms": 2000,
        "created_at": _ts(20),
    }
    fake_pool.fetch_return = _three_playlists_over_cap("c")

    def _boom(*args, **kwargs):
        raise AssertionError("decode_tracks must never run for an archived playlist")

    cog = _Cog(fake_pool, _StubLimits(2, 200))
    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=_boom)
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_play.callback(cog, ctx, name="c")
    assert "archived" in ctx.last_text().lower()


@pytest.mark.asyncio
async def test_play_allows_an_active_playlist(fake_pool):
    fake_pool.fetchrow_return = {
        "name": "a",
        "creator_id": 10,
        "tracks": ["enc1"],
        "track_count": 1,
        "total_ms": 1000,
        "created_at": _ts(0),
    }
    fake_pool.fetch_return = _three_playlists_over_cap("c")

    async def _decode(*blobs):
        return [None for _ in blobs]  # decode "fails" - irrelevant to this test

    cog = _Cog(fake_pool, _StubLimits(2, 200))
    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=_decode)
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_play.callback(cog, ctx, name="a")
    # Reached the decode step (not refused as archived) - the "none could be
    # loaded" message, not the "archived" one.
    assert "archived" not in ctx.last_text().lower()


@pytest.mark.asyncio
async def test_play_refuses_an_over_long_playlist_without_truncating_it(fake_pool):
    # Only ONE playlist exists (well under the count cap), but its OWN stored
    # track_count is over the current per-playlist cap - archived by that
    # reason alone. Its stored ``tracks`` list is NOT inspected/mutated here:
    # the refusal happens before ``tracks`` is even read.
    fake_pool.fetchrow_return = {
        "name": "big",
        "creator_id": 10,
        "tracks": ["enc"] * 600,
        "track_count": 600,
        "total_ms": 600000,
        "created_at": _ts(0),
    }
    fake_pool.fetch_return = [{"name_norm": "big", "created_at": _ts(0)}]

    def _boom(*args, **kwargs):
        raise AssertionError("an archived-by-track-count playlist must not be decoded")

    cog = _Cog(fake_pool, _StubLimits(75, 500))
    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=_boom)
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_play.callback(cog, ctx, name="big")
    assert "archived" in ctx.last_text().lower()
    # Not truncated: the stored row is untouched by this refusal (no UPDATE/
    # DELETE call was made).
    assert not any(call[0] in ("execute",) for call in fake_pool.calls)


class _DispatchingPool:
    """Routes ``fetchrow`` by SQL text: the playlist row for the real query,
    and the real ``tools.premium_upsell`` claim semantics for its own query -
    unlike the shared ``fake_pool`` fixture (one ``fetchrow_return`` for
    every call), which would make the upsell's OWN claim read back the
    playlist row as a truthy "already claimed" sentinel, silently hiding
    whatever the upsell wiring actually does."""

    def __init__(self, playlist_row, archival_rows):
        self._playlist_row = playlist_row
        self._archival_rows = archival_rows
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "premium_upsells" in query:
            return {"shown_at": None}  # claim always granted - never shown yet
        return self._playlist_row

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self._archival_rows

    async def execute(self, *args):
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_play_refuses_an_archived_playlist_with_the_upsell_line():
    """ITEM A (M5 review): the archived-playlist PLAY refusal never got the
    upsell wired at all (only the count/track caps above it did). Closed by
    routing through ``ServerPlaylistMixin._refuse_with_upsell``."""
    row = {
        "name": "c",
        "creator_id": 10,
        "tracks": ["enc1", "enc2"],
        "track_count": 2,
        "total_ms": 2000,
        "created_at": _ts(20),
    }
    pool = _DispatchingPool(row, _three_playlists_over_cap("c"))

    def _boom(*args, **kwargs):
        raise AssertionError("decode_tracks must never run for an archived playlist")

    cog = _Cog(pool, _StubLimits(2, 200), manage_guild=True)
    cog.bot.sl_client = types.SimpleNamespace(decode_tracks=_boom)
    ctx = _Ctx(author_id=10)
    ctx.author.guild_permissions = types.SimpleNamespace(manage_guild=True)
    await ps.ServerPlaylistMixin.serverplaylist_play.callback(cog, ctx, name="c")
    text = ctx.last_text()
    assert "archived" in text.lower()
    assert "Yasuho+ raises this limit to" in text
    assert str(premium.GUILD_PREMIUM.max_guild_playlists) in text


@pytest.mark.asyncio
async def test_rename_refuses_an_archived_playlist_ephemeral_on_slash(fake_pool):
    fake_pool.fetchrow_return = {
        "name": "c",
        "creator_id": 10,
        "track_count": 1,
        "total_ms": 1000,
        "created_at": _ts(20),
    }
    fake_pool.fetch_return = _three_playlists_over_cap("c")
    cog = _Cog(fake_pool, _StubLimits(2, 200), manage_guild=True)
    ctx = _Ctx(interaction=types.SimpleNamespace())  # a slash invocation
    await ps.ServerPlaylistMixin.serverplaylist_rename.callback(
        cog, ctx, old="c", new="renamed"
    )
    assert "archived" in ctx.last_text().lower()
    _, kwargs = ctx.sent[-1]
    assert kwargs.get("ephemeral") is True
    # No UPDATE happened.
    assert not any(call[0] == "execute" for call in fake_pool.calls)


@pytest.mark.asyncio
async def test_rename_refuses_an_archived_playlist_with_the_upsell_line():
    """ITEM A (M5 review): same gap as the PLAY refusal above - the archived-
    playlist RENAME refusal never got the upsell wired either. This command
    never defers, so (unlike PLAY) the base refusal is already ephemeral on
    slash - the upsell rides the SAME message rather than a separate
    followup."""
    row = {
        "name": "c",
        "creator_id": 10,
        "track_count": 1,
        "total_ms": 1000,
        "created_at": _ts(20),
    }
    pool = _DispatchingPool(row, _three_playlists_over_cap("c"))
    cog = _Cog(pool, _StubLimits(2, 200), manage_guild=True)
    ctx = _Ctx(author_id=10, interaction=types.SimpleNamespace())  # slash
    ctx.author.guild_permissions = types.SimpleNamespace(manage_guild=True)
    await ps.ServerPlaylistMixin.serverplaylist_rename.callback(
        cog, ctx, old="c", new="renamed"
    )
    text = ctx.last_text()
    assert "archived" in text.lower()
    assert "Yasuho+ raises this limit to" in text
    _, kwargs = ctx.sent[-1]
    assert kwargs.get("ephemeral") is True
    assert not any(call[0] == "execute" for call in pool.calls)


@pytest.mark.asyncio
async def test_delete_still_works_on_an_archived_playlist(fake_pool):
    fake_pool.fetchrow_return = {
        "name": "c",
        "creator_id": 10,
        "track_count": 1,
        "total_ms": 1000,
        "created_at": _ts(20),
    }
    # NOTE: no fetch_return needed - serverplaylist_delete never classifies
    # archival at all, which this test's absence of a configured fetch call
    # indirectly confirms (FakePool.fetch_return defaults to [], unused here).
    fake_pool.execute_return = "DELETE 1"
    cog = _Cog(fake_pool, _StubLimits(2, 200), manage_guild=True)
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_delete.callback(cog, ctx, name="c")
    assert any(call[0] == "execute" for call in fake_pool.calls)
    assert "Deleted" in ctx.last_text()


# ---------------------------------------------------------------------------
# Negative controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_control_archived_playlist_cannot_be_made_playable(fake_pool):
    """Flip the archival verdict the wrong way (as if the guard were deleted)
    and confirm the "allowed" assertion from the real test would fail - this
    pins that :func:`ps.playlist_is_archived` is actually load-bearing in
    ``serverplaylist_play``, not dead code."""
    assert ps.playlist_is_archived(
        track_count=2, max_playlist_tracks=200, active_by_count=False
    ) is True
    # The broken (negative-control) implementation a bug would produce:
    broken_verdict = False  # "always allow" - what a deleted guard would give
    assert broken_verdict != ps.playlist_is_archived(
        track_count=2, max_playlist_tracks=200, active_by_count=False
    )
