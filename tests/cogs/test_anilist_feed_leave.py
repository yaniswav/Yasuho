"""Leaving an AniList feed is always possible for the member themselves.

THE FINDING. ``/anilistfeed me`` was a single toggle behind two JOIN gates: the
feed's ``self_add`` setting, and a LIVE AniList token (the leave branch sat after
a ``VIEWER_QUERY``). So the one person most likely to want out - somebody who
had already run ``/anilist logout`` - could not stop their activity being
mirrored into somebody else's channel, and neither could a member of a feed
whose owner had since turned member self-add off. That is a data-subject
problem: the person cannot make it stop.

THE FIX, and what is pinned here:

* leaving runs FIRST and takes neither gate;
* it is TOKEN-FREE: the member's AniList id is read from the durable mapping an
  airing / chapter opt-in already stores, and failing that from the self-add
  trail (``anilist_follows.added_by``);
* it works REGARDLESS OF WHO ADDED THEM - a moderator-added follow of their
  account is left on the id like any other;
* the trail fallback cannot be turned into a moderator footgun: a caller with
  Manage Server (whose ``added_by`` rows are OTHER PEOPLE, stamped by
  ``/anilistfeed follow``) never leaves through it, and an ambiguous trail
  leaves nothing;
* JOINING is unchanged - both gates still apply;
* the mute a moderator placed still survives the member's leave (the invariant
  the mute lot landed: ``clear_mute=False``).

Offline: the cog is built with ``__new__`` and fed a fake pool that answers by
SQL fragment, like tests/cogs/test_anilist_feed_mutes.py.
"""

import types

import pytest

from cogs.anilist.feed import AniListFeed

GUILD = 1
CHANNEL = 100
MEMBER = 4242
THEIR_ANILIST_ID = 7


# --- Fakes ------------------------------------------------------------------


class _Pool:
    """Answers reads by SQL fragment; records every statement it is given."""

    def __init__(self, *, rows=None, fetches=None, values=None):
        self.calls = []
        self._rows = rows or {}
        self._fetches = fetches or {}
        self._values = values or {}

    def _match(self, table, sql, default):
        for fragment, value in table.items():
            if fragment in sql:
                return value
        return default

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._match(self._rows, sql, None)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._match(self._fetches, sql, [])

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self._match(self._values, sql, None)

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "DELETE 1"

    def acquire(self):
        return _Ctx(self)

    def transaction(self):
        return _Ctx(self)

    def statements(self):
        return [sql for _method, sql, _args in self.calls]


class _Ctx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _MeCtx:
    def __init__(self, *, manage_guild=False, author_id=MEMBER):
        self.guild = types.SimpleNamespace(id=GUILD)
        self.author = types.SimpleNamespace(
            id=author_id,
            display_name="member",
            guild_permissions=types.SimpleNamespace(manage_guild=manage_guild),
        )
        self.sent = []

    def typing(self):
        return _Typing()

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return content


class _FakeAniList:
    """The AniList cog ``/anilistfeed me`` borrows for the JOIN path."""

    def __init__(self, status="ok", viewer_id=THEIR_ANILIST_ID):
        self.status = status
        self.viewer_id = viewer_id
        self.graphql_calls = 0

    async def _token_status(self, user_id):
        return (self.status, "token" if self.status == "ok" else None)

    async def _graphql(self, query, variables, token=None):
        self.graphql_calls += 1
        return {"data": {"Viewer": {"id": self.viewer_id, "name": "reader"}}}


def _cog(pool, anilist=None):
    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = types.SimpleNamespace(
        db_pool=pool,
        get_cog=lambda name: anilist if name == "AniList" else None,
    )
    return cog


async def _resolved(channel_id):
    return channel_id, None


async def _run_me(cog, ctx):
    cog._resolve_target = lambda _ctx: _resolved(CHANNEL)
    return await AniListFeed.anilistfeed_me.callback(cog, ctx)


def _optin_row(anilist_user_id=THEIR_ANILIST_ID):
    return {"anilist_user_id": anilist_user_id}


# --- Leaving with no token at all -------------------------------------------


async def test_an_unlinked_member_can_still_leave():
    """THE fix: no token anywhere, and the leave lands.

    The id comes from the airing / chapter opt-in mapping, which is written at
    opt-in time and survives ``/anilist logout``.
    """

    pool = _Pool(rows={"anilist_airing_optins": _optin_row()})
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert any("DELETE FROM anilist_follows" in sql for sql in pool.statements())
    assert "left the AniList feed" in ctx.sent[-1]


async def test_leaving_never_asks_anilist_for_a_token():
    """No ``_token_status``, no VIEWER call: the leave path is token-free."""

    pool = _Pool(rows={"anilist_airing_optins": _optin_row()})
    anilist = _FakeAniList(status="ok")
    cog = _cog(pool, anilist=anilist)

    await _run_me(cog, _MeCtx())

    assert anilist.graphql_calls == 0


async def test_a_member_can_leave_a_feed_that_no_longer_lets_members_join():
    """``self_add`` is a JOIN policy. Reinstate it above the leave and this fails."""

    pool = _Pool(
        rows={
            "anilist_airing_optins": _optin_row(),
            "SELECT self_add": {"self_add": False},
        }
    )
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "left the AniList feed" in ctx.sent[-1]
    assert any("DELETE FROM anilist_follows" in sql for sql in pool.statements())


async def test_leaving_still_leaves_a_moderators_mute_standing():
    """The mute lot's invariant, re-pinned on the new path: clear_mute=False."""

    pool = _Pool(rows={"anilist_airing_optins": _optin_row()})
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))

    await _run_me(cog, _MeCtx())

    statements = pool.statements()
    assert any("DELETE FROM anilist_follows" in sql for sql in statements)
    assert not any("DELETE FROM anilist_feed_mutes" in sql for sql in statements)


async def test_the_id_lookup_is_scoped_to_this_feed_and_this_member():
    pool = _Pool(rows={"anilist_airing_optins": _optin_row()})
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))

    await _run_me(cog, _MeCtx())

    lookup = next(
        (sql, args)
        for method, sql, args in pool.calls
        if method == "fetchrow" and "anilist_airing_optins" in sql
    )
    assert lookup[1] == (GUILD, CHANNEL, MEMBER)
    # Both opt-in tables are consulted, so either tracker's mapping is enough.
    assert "anilist_chapter_optins" in lookup[0]


# --- Leaving through the self-add trail -------------------------------------


async def test_a_member_with_no_opt_in_leaves_through_their_own_add_trail():
    """No token, no opt-in row: the follow THEY added is proof enough."""

    pool = _Pool(
        fetches={"added_by = $3": [{"anilist_user_id": THEIR_ANILIST_ID}]},
    )
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "left the AniList feed" in ctx.sent[-1]


async def test_a_moderators_add_trail_is_never_treated_as_their_own_row():
    """``/anilistfeed follow`` stamps the moderator on OTHER people's rows.

    Leaving through that trail would delete somebody else's follow (up to the
    whole feed) for a moderator who just wanted to toggle their own membership.
    A moderator is never the stuck data subject: they have
    ``/anilistfeed unfollow``.
    """

    pool = _Pool(
        fetches={"added_by = $3": [{"anilist_user_id": 11}, {"anilist_user_id": 12}]},
        rows={"SELECT self_add": {"self_add": False}},
    )
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))
    ctx = _MeCtx(manage_guild=True)

    await _run_me(cog, ctx)

    assert not any("DELETE FROM anilist_follows" in sql for sql in pool.statements())
    assert "does not let members join themselves" in ctx.sent[-1]


async def test_an_ambiguous_add_trail_leaves_nothing():
    """Two candidate rows and no resolvable id: refuse rather than guess."""

    pool = _Pool(
        fetches={"added_by = $3": [{"anilist_user_id": 11}, {"anilist_user_id": 12}]},
        rows={"SELECT self_add": {"self_add": False}},
    )
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))

    await _run_me(cog, _MeCtx())

    assert not any("DELETE FROM anilist_follows" in sql for sql in pool.statements())


# --- Joining is unchanged ---------------------------------------------------


async def test_joining_still_needs_the_self_add_setting():
    pool = _Pool(rows={"SELECT self_add": {"self_add": False}})
    cog = _cog(pool, anilist=_FakeAniList())
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "does not let members join themselves" in ctx.sent[-1]
    assert not any("INSERT INTO anilist_follows" in sql for sql in pool.statements())


async def test_joining_still_needs_a_live_link():
    pool = _Pool(rows={"SELECT self_add": {"self_add": True}})
    cog = _cog(pool, anilist=_FakeAniList(status="missing"))
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "Link your AniList account first" in ctx.sent[-1]


async def test_a_linked_member_still_joins_on_one_viewer_call():
    """The leave probe must not double this command's AniList budget."""

    pool = _Pool(
        rows={"SELECT self_add": {"self_add": True}},
        values={"SELECT COUNT(*) FROM anilist_follows": 0},
    )
    anilist = _FakeAniList(status="ok")
    cog = _cog(pool, anilist=anilist)
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "joined the AniList feed" in ctx.sent[-1]
    assert anilist.graphql_calls == 1


async def test_a_linked_member_whose_follow_only_the_viewer_call_can_match_leaves():
    """The token rung still exists further down: a member with no opt-in row and
    no trail (a moderator added them) leaves on the live viewer id."""

    pool = _Pool(
        rows={"SELECT self_add": {"self_add": True}},
        values={"SELECT 1 FROM anilist_follows": 1},
    )
    cog = _cog(pool, anilist=_FakeAniList(status="ok"))
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "left the AniList feed" in ctx.sent[-1]
    statements = pool.statements()
    assert any("DELETE FROM anilist_follows" in sql for sql in statements)
    assert not any("DELETE FROM anilist_feed_mutes" in sql for sql in statements)


@pytest.mark.parametrize("manage_guild", [False, True])
async def test_no_feed_at_all_is_still_answered_before_anything_else(manage_guild):
    pool = _Pool()
    cog = _cog(pool, anilist=_FakeAniList())
    ctx = _MeCtx(manage_guild=manage_guild)

    async def _no_feed(_ctx):
        return None, "no feed here"

    cog._resolve_target = _no_feed
    await AniListFeed.anilistfeed_me.callback(cog, ctx)

    assert ctx.sent == ["no feed here"]
    assert pool.calls == []
