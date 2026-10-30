"""V4a: airing ping roles, per-feed activity filters, watch-along threads.

Offline, like the other AniList cog tests: cogs are built with ``__new__`` and fed
hand-rolled fakes. Every NEGATIVE CONTROL named in a docstring is a guard that was
removed by hand while writing the test and shown to fail it (see the lot report).
"""

import types

import discord
import pytest

from cogs.anilist import airing as ai
from cogs.anilist import airing_roles as ar
from cogs.anilist import feed_policy as af
from cogs.anilist import watchalong as wa_mod
from cogs.anilist.feed import AniListFeed
from tools import i18n, premium

# --- Fakes --------------------------------------------------------------------


class Role:
    def __init__(self, role_id, *, mentionable=True, default=False):
        self.id = role_id
        self.mentionable = mentionable
        self._default = default
        self.mention = "<@&%d>" % role_id

    def is_default(self):
        return self._default


class Me:
    def __init__(self, mention_everyone=False):
        self.guild_permissions = types.SimpleNamespace(mention_everyone=mention_everyone)


class Guild:
    def __init__(self, guild_id=1, roles=(), me=None):
        self.id = guild_id
        self.me = me or Me()
        self._roles = {r.id: r for r in roles}

    def get_role(self, role_id):
        return self._roles.get(role_id)


class Channel:
    def __init__(self, guild, channel_id=10, mention_everyone=None):
        self.id = channel_id
        self.guild = guild
        self.sent = []
        self._mention_everyone = mention_everyone

    def permissions_for(self, member):
        if self._mention_everyone is not None:  # a channel overwrite decides
            return types.SimpleNamespace(mention_everyone=self._mention_everyone)
        return types.SimpleNamespace(
            mention_everyone=member.guild_permissions.mention_everyone
        )

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class Thread(discord.Thread):
    """A discord.Thread without a connection state."""

    mention = "<#555>"

    def __init__(self, guild, *, archived=False, locked=False, manage_threads=False,
                 mention_everyone=False):
        self.id = 555
        self.guild = guild
        self.archived = archived
        self.locked = locked
        self._manage = manage_threads
        self._mention_everyone = mention_everyone
        self.events = []

    def permissions_for(self, member):
        return types.SimpleNamespace(
            manage_threads=self._manage, mention_everyone=self._mention_everyone
        )

    async def edit(self, **kwargs):
        self.events.append(("edit", kwargs))
        self.archived = kwargs.get("archived", self.archived)

    async def send(self, content=None, **kwargs):
        self.events.append(("send", content, kwargs))


class Resolver:
    def __init__(self, limits):
        self.limits = limits

    def for_guild(self, guild_id, *, now=None):
        return self.limits


@pytest.fixture(autouse=True)
def _locale(monkeypatch):
    async def _en(bot, guild):
        return "en"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _en)


def _row(media_id=7, episode=3, title="Frieren"):
    return {
        "media_id": media_id,
        "episode": episode,
        "airing_at": 1_700_000_100,
        "media": {"id": media_id, "title": {"userPreferred": title}, "siteUrl": "https://x"},
    }


def _texts(view):
    out = []

    def walk(items):
        for item in items:
            if isinstance(item, discord.ui.TextDisplay):
                out.append(item.content)
            walk(getattr(item, "children", ()) or ())

    walk(view.children)
    return out


# --- 1. role mention on a channel post ------------------------------------------


def test_ping_rule_mirrors_event_reminders():
    both, neither = (True, True), (False, False)
    mentionable = Role(1, mentionable=True)
    locked = Role(2, mentionable=False)
    everyone = Role(3, default=True)
    assert ar.role_pingable_by_manager(mentionable, *neither)
    assert not ar.role_pingable_by_manager(locked, *neither)
    assert not ar.role_pingable_by_manager(locked, True, False)  # bot lacks it
    assert not ar.role_pingable_by_manager(locked, False, True)  # manager lacks it
    assert ar.role_pingable_by_manager(locked, *both)
    assert not ar.role_pingable_by_manager(everyone, *both)  # never @everyone


def test_allowed_mentions_is_exactly_the_role():
    mentions = ar.ping_mentions(Role(42)).to_dict()
    assert mentions == {"parse": [], "roles": [42]}


async def test_channel_post_pings_only_the_role():
    guild = Guild(roles=[Role(42)])
    channel = Channel(guild)
    cog = object.__new__(ai.AniListAiring)
    cog.bot = types.SimpleNamespace(get_channel=lambda cid: channel)
    await cog._deliver_channel(10, _row(), role_id=42)
    (sent,) = channel.sent
    assert sent["allowed_mentions"].to_dict() == {"parse": [], "roles": [42]}
    assert "<@&42>" in _texts(sent["view"])[0]


@pytest.mark.parametrize(
    "role_id, roles, me",
    [
        (None, [Role(42)], Me()),  # no mapping
        (42, [], Me()),  # the role was deleted
        (42, [Role(42, mentionable=False)], Me(False)),  # bot cannot ping it
        (42, [Role(42, default=True)], Me(True)),  # @everyone is never pinged
    ],
)
async def test_channel_post_without_a_usable_role_pings_nobody(role_id, roles, me):
    """Negative control: dropping the role_pingable_by_bot guard makes the
    unmentionable-role case ping."""
    channel = Channel(Guild(roles=roles, me=me))
    cog = object.__new__(ai.AniListAiring)
    cog.bot = types.SimpleNamespace(get_channel=lambda cid: channel)
    await cog._deliver_channel(10, _row(), role_id=role_id)
    (sent,) = channel.sent
    assert sent["allowed_mentions"].to_dict() == {"parse": []}
    assert not any("<@&" in t for t in _texts(sent["view"]))


def _roles_rows():
    return [
        {"guild_id": 1, "media_id": 30, "role_id": 3, "created_at": 3},
        {"guild_id": 1, "media_id": 10, "role_id": 1, "created_at": 1},
        {"guild_id": 1, "media_id": 20, "role_id": 2, "created_at": 2},
        {"guild_id": 2, "media_id": 10, "role_id": 9, "created_at": 1},
    ]


def test_lapse_keeps_the_oldest_mapping_only():
    """Negative control: ignoring the limit (returning every row) fails this."""
    free = types.SimpleNamespace(premium=Resolver(premium.GUILD_FREE))
    assert ar.usable_roles(free, _roles_rows()) == {(1, 10): 1, (2, 10): 9}
    plus = types.SimpleNamespace(premium=Resolver(premium.GUILD_PREMIUM))
    assert len(ar.usable_roles(plus, _roles_rows())) == 4


def test_archival_ties_break_by_media_id():
    rows = [
        {"guild_id": 1, "media_id": 5, "role_id": 1, "created_at": 1},
        {"guild_id": 1, "media_id": 4, "role_id": 2, "created_at": 1},
    ]
    assert ar.active_ids(rows, 1) == {4}


async def test_lapsed_mapping_is_not_pinged_but_stays_stored():
    guild = Guild(roles=[Role(1), Role(2)])
    channel = Channel(guild)
    stored = _roles_rows()[:3]
    writes = []

    class Pool:
        async def fetch(self, sql, *args):
            return stored

        async def execute(self, sql, *args):
            writes.append(sql)

    cog = object.__new__(ai.AniListAiring)
    cog.bot = types.SimpleNamespace(
        get_channel=lambda cid: channel,
        db_pool=Pool(),
        premium=Resolver(premium.GUILD_FREE),
    )
    rows = {(20, 3): _row(20, 3)}
    await cog._deliver_channel_posts([(1, 10, 20, 3)], rows)  # archived mapping
    assert channel.sent[0]["allowed_mentions"].to_dict() == {"parse": []}
    assert writes == [] and len(stored) == 3  # nothing deleted


# --- 2. feed filters ----------------------------------------------------------


def _act(aid, status, type_="ANIME_LIST"):
    return {"id": aid, "type": type_, "user_id": 1, "status": status, "is_adult": False}


ACTS = [
    _act(1, "completed"),
    _act(2, "plans to watch"),
    _act(3, "watched episode"),
    _act(4, "dropped"),
    _act(5, "paused reading", "MANGA_LIST"),
    _act(6, None, "TEXT"),
    _act(7, "some new status"),
]


def _ids(filters):
    return [a["id"] for a in ACTS if af.passes_filters(a, set(filters))]


def test_filters_table():
    assert _ids([]) == [1, 2, 3, 4, 5, 6, 7]
    assert _ids(["completed_only"]) == [1, 6]  # text is governed by types alone
    assert _ids(["hide_planning"]) == [1, 3, 4, 5, 6, 7]
    assert _ids(["hide_progress"]) == [1, 2, 4, 5, 6, 7]
    assert _ids(["hide_dropped"]) == [1, 2, 3, 6, 7]
    assert _ids(["hide_planning", "hide_progress"]) == [1, 4, 5, 6, 7]
    assert _ids(["not_a_filter"]) == [1, 2, 3, 4, 5, 6, 7]  # unknown name ignored


def _feed(filters):
    return {
        "channel_id": 5,
        "types": set(af.ALLOWED_TYPES),
        "followed_ids": {1},
        "filters": filters,
    }


def test_route_applies_filters_per_feed_and_keeps_the_others_whole():
    """Negative control: removing the passes_filters call in route_activities
    makes the filtered feed receive all seven."""
    routed = af.route_activities(ACTS, [_feed({"completed_only"}), {**_feed(()), "channel_id": 6}])
    assert [a["id"] for a in routed[5]] == [1, 6]
    assert len(routed[6]) == 7


def _feed_cog(limits):
    cog = object.__new__(AniListFeed)
    cog.bot = types.SimpleNamespace(
        premium=Resolver(limits), get_channel=lambda cid: None
    )
    seen = {}

    async def _deliver_channel(feed, channel_id, items, pacer=None):
        seen[channel_id] = [a["id"] for a in items]

    cog._deliver_channel = _deliver_channel
    cog._allow_adult = lambda cid: False
    return cog, seen


async def _dispatch(cog):
    feeds = [{"guild_id": 1, "channel_id": 5, "types": list(af.ALLOWED_TYPES),
              "filters": ["completed_only"]}]
    await cog._dispatch(feeds, {(1, 5): {1}}, ACTS)


async def test_dispatch_filters_before_delivery_for_plus_only():
    """Negative control: dropping the anilist_feed_filters gate in _dispatch makes
    the lapsed (free) guild filter too."""
    cog, seen = _feed_cog(premium.GUILD_PREMIUM)
    await _dispatch(cog)
    assert seen[5] == [1, 6]
    cog, seen = _feed_cog(premium.GUILD_FREE)  # lapsed: stored filters ignored
    await _dispatch(cog)
    assert seen[5] == [1, 2, 3, 4, 5, 6, 7]


async def test_filter_command_is_refused_for_free(monkeypatch):
    from cogs.anilist import feed_filters as ff

    refused = []

    async def _refuse(bot, ctx, text, **kw):
        refused.append(kw)

    monkeypatch.setattr(ff.premium_upsell, "refuse_in_context", _refuse)
    cog = types.SimpleNamespace(bot=types.SimpleNamespace(premium=Resolver(premium.GUILD_FREE)))
    ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=1))
    await ff.run_filter_command(cog, ctx, {"completed_only": True})
    assert refused == [{"limit_key": "anilist_feed_filters", "kind": "unlock"}]


def test_filter_merge_keeps_stored_order_and_untouched_flags():
    assert af.FEED_FILTERS == ("completed_only", "hide_planning", "hide_progress", "hide_dropped")
    from cogs.anilist import feed_filters as ff

    assert ff.merge(["hide_dropped"], {"hide_planning": True, "hide_dropped": None}) == [
        "hide_planning",
        "hide_dropped",
    ]
    assert ff.merge(["hide_planning"], {"hide_planning": False}) == []


# --- 3. watch-along -------------------------------------------------------------


def _wa(guild_id=1, media_id=7, role_id=None, created=1):
    return {
        "guild_id": guild_id,
        "media_id": media_id,
        "channel_id": 9,
        "thread_id": 555,
        "role_id": role_id,
        "title": "Frieren",
        "created_at": created,
    }


def test_plan_posts_one_per_watchalong_of_the_aired_title():
    aired = [_row(7, 3), _row(8, 1), {"media_id": None, "episode": 1}]
    posts = wa_mod.plan_watchalong_posts(aired, [_wa(2, 7), _wa(1, 7), _wa(1, 99)])
    assert [(w["guild_id"], r["media_id"]) for w, r in posts] == [(1, 7), (2, 7)]


def test_free_guild_has_no_active_watchalong_and_lapse_keeps_the_oldest():
    rows = [_wa(1, 7, created=2), _wa(1, 8, created=1), _wa(1, 9, created=3)]
    free = types.SimpleNamespace(premium=Resolver(premium.GUILD_FREE))
    assert wa_mod.active_watchalongs(free, rows) == []
    two = types.SimpleNamespace(
        premium=Resolver(types.SimpleNamespace(max_watchalongs=2))
    )
    assert [r["media_id"] for r in wa_mod.active_watchalongs(two, rows)] == [7, 8]


def _bot(thread, writes, fetch=None):
    class Pool:
        async def execute(self, sql, *args):
            writes.append(args)

    async def fetch_channel(cid):
        if fetch is not None:
            raise fetch
        return thread

    return types.SimpleNamespace(
        get_channel=lambda cid: thread if fetch is None else None,
        fetch_channel=fetch_channel,
        db_pool=Pool(),
    )


async def test_watchalong_posts_episode_with_spoiler_prompt_and_role_ping():
    guild = Guild(roles=[Role(42)])
    thread = Thread(guild)
    result = await wa_mod.deliver_watchalong(_bot(thread, []), _wa(role_id=42), _row())
    assert result == "sent"
    ((kind, content, kwargs),) = thread.events
    assert content.startswith("<@&42> Episode 3 of **Frieren** is out!")
    assert "spoiler" in content
    assert kwargs["allowed_mentions"].to_dict() == {"parse": [], "roles": [42]}


async def test_watchalong_post_carries_no_synopsis():
    row = _row()
    row["media"]["description"] = "SECRET PLOT TWIST"
    row["description"] = "SECRET PLOT TWIST"
    thread = Thread(Guild())
    await wa_mod.deliver_watchalong(_bot(thread, []), _wa(), row)
    assert "SECRET" not in thread.events[0][1]


async def test_watchalong_without_usable_role_pings_nobody():
    thread = Thread(Guild(roles=[Role(42, mentionable=False)], me=Me(False)))
    await wa_mod.deliver_watchalong(_bot(thread, []), _wa(role_id=42), _row())
    content, kwargs = thread.events[0][1], thread.events[0][2]
    assert "<@&" not in content
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}


async def test_watchalong_unarchives_before_posting():
    """Negative control: removing the unarchive edit leaves the thread archived
    when the post is sent."""
    thread = Thread(Guild(), archived=True)
    assert await wa_mod.deliver_watchalong(_bot(thread, []), _wa(), _row()) == "sent"
    assert [e[0] for e in thread.events] == ["edit", "send"]
    assert thread.events[0][1] == {"archived": False}


async def test_watchalong_locked_archived_thread_is_skipped_without_manage_threads():
    thread = Thread(Guild(), archived=True, locked=True)
    assert await wa_mod.deliver_watchalong(_bot(thread, []), _wa(), _row()) == "skipped"
    assert thread.events == []
    thread = Thread(Guild(), archived=True, locked=True, manage_threads=True)
    assert await wa_mod.deliver_watchalong(_bot(thread, []), _wa(), _row()) == "sent"


async def test_deleted_thread_ends_the_watchalong_lazily():
    """Negative control: without the NotFound branch the row is never deleted."""
    writes = []
    bot = _bot(None, writes, fetch=discord.NotFound(types.SimpleNamespace(status=404, reason=""), "gone"))
    assert await wa_mod.deliver_watchalong(bot, _wa(), _row()) == "ended"
    assert writes == [(1, 7)]


async def test_unreachable_thread_is_kept():
    writes = []
    bot = _bot(
        None, writes,
        fetch=discord.Forbidden(types.SimpleNamespace(status=403, reason=""), "no"),
    )
    assert await wa_mod.deliver_watchalong(bot, _wa(), _row()) == "skipped"
    assert writes == []


# --- zero new AniList requests ------------------------------------------------


def _tick_cog(*, subs, watchalongs, roles):
    guild = Guild(roles=[Role(42)])
    channel = Channel(guild)
    thread = Thread(guild)
    cog = object.__new__(ai.AniListAiring)

    class Pool:
        async def fetch(self, sql, *args):
            return roles

    cog.bot = types.SimpleNamespace(
        db_pool=Pool(),
        premium=Resolver(premium.GUILD_PREMIUM),
        get_channel=lambda cid: thread if cid == 555 else channel,
    )
    cog._embargo_until = 0
    cog._list_cache = {}
    cog._list_fail_counts = {}
    cog._spaced = False
    cog._req_count = 0
    calls = []

    async def _graphql(query, variables):
        calls.append(variables)
        return {"data": {"Page": {"airingSchedules": [
            {"id": 1, "airingAt": 1_700_000_100, "episode": 3, "mediaId": 7,
             "media": _row()["media"]}]}}}

    async def _optins():
        return []

    async def _subs():
        return subs

    async def _watchalongs():
        return watchalongs

    async def _cursor():
        return 1_700_000_000

    async def _save(value):
        pass

    async def _no_space():
        cog._req_count += 1

    cog._graphql = _graphql
    cog._load_optins = _optins
    cog._load_channel_subs = _subs
    cog._load_watchalongs = _watchalongs
    cog._load_cursor = _cursor
    cog._save_cursor = _save
    cog._space = _no_space
    return cog, calls, channel, thread


SUBS = [{"guild_id": 1, "channel_id": 10, "media_id": 7}]
ROLES = [{"guild_id": 1, "media_id": 7, "role_id": 42, "created_at": 1}]


async def test_roles_and_watchalongs_add_zero_requests_per_cycle():
    """The airing cycle spends exactly the same AniList requests with or without
    ping roles and watch-alongs. Negative control: a per-feature fetch (one extra
    _graphql call in plan/deliver) raises the plain count above the baseline."""
    base, base_calls, channel0, _t = _tick_cog(subs=SUBS, watchalongs=[], roles=[])
    await base._tick()
    full, calls, channel1, thread1 = _tick_cog(
        subs=SUBS, watchalongs=[_wa(role_id=42)], roles=ROLES
    )
    await full._tick()
    assert len(base_calls) == len(calls) == 1
    assert full._req_count == base._req_count == 1
    # ...and the features really ran on that one request.
    assert "<@&42>" in _texts(channel1.sent[0]["view"])[0]
    # The channel post pinged the role first, so the watch-along does not repeat it.
    assert thread1.events and thread1.events[0][1].startswith("Episode 3")


async def test_a_watchalong_title_joins_the_union_without_any_sub():
    cog, calls, channel, thread = _tick_cog(subs=[], watchalongs=[_wa()], roles=[])
    await cog._tick()
    assert calls[0]["mediaIds"] == [7]
    assert channel.sent == [] and len(thread.events) == 1


# --- commands: limits and upsell ---------------------------------------------


class Ctx:
    def __init__(self):
        self.guild = types.SimpleNamespace(id=1, me=Me(False))
        self.author = types.SimpleNamespace(
            id=2, guild_permissions=types.SimpleNamespace(mention_everyone=False)
        )
        self.sent = []

    async def send(self, text=None, **kwargs):
        self.sent.append(text)

    async def defer(self):
        pass


def _cmd_cog(limits, *, tracked=True):
    class Pool:
        async def fetchrow(self, sql, *args):
            if "anilist_channel_subs" in sql:
                return {"media_id": 7, "title": "Frieren"} if tracked else None
            return None

        async def fetchval(self, sql, *args):
            return 0

    cog = wa_mod.WatchalongMixin()
    cog.bot = types.SimpleNamespace(db_pool=Pool(), premium=Resolver(limits))
    return cog


def _capture_refusals(monkeypatch, module):
    refusals = []

    async def _refuse(bot, ctx, text, **kw):
        refusals.append(kw)

    monkeypatch.setattr(module.premium_upsell, "refuse_in_context", _refuse)
    return refusals


async def test_pingrole_set_at_limit_refuses_with_upsell(monkeypatch):
    refusals = _capture_refusals(monkeypatch, ar)

    async def _set_role(*a):
        return False

    monkeypatch.setattr(ar, "set_role", _set_role)
    await ar.AiringRolesMixin.anilist_pingrole_set.callback(
        _cmd_cog(premium.GUILD_FREE), Ctx(), "7", Role(42)
    )
    assert refusals == [{"limit_key": "max_airing_roles", "benefit": "100"}]


async def test_pingrole_set_stores_when_under_limit_and_checks_the_ping_rule(monkeypatch):
    calls = []

    async def _set_role(pool, guild_id, media_id, role_id, title, limit):
        calls.append((guild_id, media_id, role_id, title, limit))
        return True

    monkeypatch.setattr(ar, "set_role", _set_role)
    ctx = Ctx()
    await ar.AiringRolesMixin.anilist_pingrole_set.callback(
        _cmd_cog(premium.GUILD_FREE), ctx, "7", Role(42)
    )
    assert calls == [(1, 7, 42, "Frieren", 1)] and "I will ping <@&42>" in ctx.sent[0]
    calls.clear()
    ctx = Ctx()
    await ar.AiringRolesMixin.anilist_pingrole_set.callback(
        _cmd_cog(premium.GUILD_FREE), ctx, "7", Role(42, mentionable=False)
    )
    assert calls == [] and ctx.sent == ["You can't ping that role."]


async def test_watchalong_start_is_refused_for_free_without_touching_anything(monkeypatch):
    refusals = _capture_refusals(monkeypatch, wa_mod)
    await wa_mod.WatchalongMixin.anilist_watchalong_start.callback(
        _cmd_cog(premium.GUILD_FREE), Ctx(), "7", types.SimpleNamespace()
    )
    assert refusals == [{"limit_key": "max_watchalongs", "kind": "unlock"}]


class Parent(discord.TextChannel):
    """A text channel that records create_thread calls."""

    mention = "<#9>"

    def __init__(self, news=False):
        self.id = 9
        self._news = news
        self.created = []
        self.deleted = []

    def is_news(self):
        return self._news

    def permissions_for(self, member):
        return types.SimpleNamespace(
            view_channel=True, send_messages_in_threads=True, create_public_threads=True
        )

    async def create_thread(self, **kwargs):
        made = Thread(Guild())
        made.parent_id = 9
        made.id = 600 + len(self.created)
        made.delete = self._delete
        self.created.append(made)
        return made

    async def _delete(self):
        self.deleted.append(1)


def _patch_slot(monkeypatch, pre, added):
    async def _slot(db, guild_id, media_id, limit):
        return pre

    async def _add(*a):
        if isinstance(added, Exception):
            raise added
        return added

    monkeypatch.setattr(wa_mod, "slot_state", _slot)
    monkeypatch.setattr(wa_mod, "add_watchalong", _add)


async def _start(channel):
    ctx = Ctx()
    await wa_mod.WatchalongMixin.anilist_watchalong_start.callback(
        _cmd_cog(premium.GUILD_PREMIUM), ctx, "7", channel
    )
    return ctx


async def test_refused_start_never_creates_a_thread(monkeypatch):
    """Negative control: dropping the pre-check makes the 6th start create (then
    need to delete) a thread."""
    refusals = _capture_refusals(monkeypatch, wa_mod)
    _patch_slot(monkeypatch, ("limit", None), ("added", 1))
    parent = Parent()
    await _start(parent)
    assert parent.created == [] and refusals[0]["limit_key"] == "max_watchalongs"


async def test_existing_title_answers_already_exists_without_a_thread(monkeypatch):
    refusals = _capture_refusals(monkeypatch, wa_mod)
    _patch_slot(monkeypatch, ("exists", 777), ("added", 1))
    parent = Parent()
    ctx = await _start(parent)
    assert parent.created == [] and refusals == []
    assert "already exists: <#777>" in ctx.sent[0]


async def test_race_lost_on_the_title_deletes_only_our_thread(monkeypatch):
    """Negative control: treating every non-added state as the limit shows the
    upsell instead of 'already exists'."""
    refusals = _capture_refusals(monkeypatch, wa_mod)
    _patch_slot(monkeypatch, ("ok", None), ("exists", 777))
    parent = Parent()
    ctx = await _start(parent)
    assert len(parent.created) == 1 and parent.deleted == [1]
    assert refusals == [] and "already exists: <#777>" in ctx.sent[0]


async def test_race_lost_on_the_limit_deletes_the_thread_and_upsells(monkeypatch):
    refusals = _capture_refusals(monkeypatch, wa_mod)
    _patch_slot(monkeypatch, ("ok", None), ("limit", None))
    parent = Parent()
    await _start(parent)
    assert parent.deleted == [1] and refusals[0]["benefit"] == "5"


async def test_db_error_after_creating_the_thread_deletes_it_and_reraises(monkeypatch):
    _patch_slot(monkeypatch, ("ok", None), RuntimeError("db down"))
    parent = Parent()
    with pytest.raises(RuntimeError):
        await _start(parent)
    assert parent.deleted == [1]


async def test_existing_thread_is_never_deleted_on_a_lost_race(monkeypatch):
    _capture_refusals(monkeypatch, wa_mod)
    _patch_slot(monkeypatch, ("ok", None), ("limit", None))
    thread = Thread(Guild())
    thread.parent_id = 9
    thread._type = discord.ChannelType.public_thread
    thread.permissions_for = lambda m: types.SimpleNamespace(
        view_channel=True, send_messages_in_threads=True
    )
    thread.delete = lambda: pytest.fail("must not delete a thread we did not create")
    await _start(thread)


async def test_announcement_channels_and_private_threads_are_rejected(monkeypatch):
    """Negative control: removing the type check lets both through."""
    _patch_slot(monkeypatch, ("ok", None), ("added", 1))
    news = Parent(news=True)
    ctx = await _start(news)
    assert news.created == [] and "not an announcement channel" in ctx.sent[0]
    private = Thread(Guild())
    private._type = discord.ChannelType.private_thread
    ctx = await _start(private)
    assert "not an announcement channel" in ctx.sent[0]


# --- review fixes: post-time ping scope, once per tick, no abort -----------------


async def test_post_time_ping_check_uses_the_channel_overwrites():
    """Negative control: reading the guild-wide permission pings in a channel
    where Mention Everyone is denied."""
    locked = Role(42, mentionable=False)
    denied = Channel(Guild(roles=[locked], me=Me(True)), mention_everyone=False)
    cog = object.__new__(ai.AniListAiring)
    cog.bot = types.SimpleNamespace(get_channel=lambda cid: denied)
    await cog._deliver_channel(10, _row(), role_id=42)
    assert denied.sent[0]["allowed_mentions"].to_dict() == {"parse": []}
    allowed = Channel(Guild(roles=[locked], me=Me(False)), mention_everyone=True)
    cog.bot = types.SimpleNamespace(get_channel=lambda cid: allowed)
    await cog._deliver_channel(10, _row(), role_id=42)
    assert allowed.sent[0]["allowed_mentions"].to_dict() == {"parse": [], "roles": [42]}


async def test_a_missing_bot_member_never_aborts_the_post():
    """Negative control: resolving the role outside the guarded block raises out of
    _deliver_channel and aborts the tick before the cursor is saved."""
    guild = Guild(roles=[Role(42)])
    guild.me = None
    channel = Channel(guild)
    channel.permissions_for = lambda member: member.guild_permissions
    cog = object.__new__(ai.AniListAiring)
    cog.bot = types.SimpleNamespace(get_channel=lambda cid: channel)
    await cog._deliver_channel(10, _row(), role_id=42)
    assert len(channel.sent) == 1
    assert channel.sent[0]["allowed_mentions"].to_dict() == {"parse": []}


async def test_a_role_pings_once_per_guild_and_title_per_tick():
    """Two feed channels and a watch-along on one title: the first post pings, the
    rest do not. Negative control: without the pinged set all three ping."""
    guild = Guild(roles=[Role(42)])
    ch1, ch2 = Channel(guild, 10), Channel(guild, 11)
    thread = Thread(guild)
    cog = object.__new__(ai.AniListAiring)

    class Pool:
        async def fetch(self, sql, *args):
            return [{"guild_id": 1, "media_id": 7, "role_id": 42, "created_at": 1}]

    cog.bot = types.SimpleNamespace(
        db_pool=Pool(),
        premium=Resolver(premium.GUILD_PREMIUM),
        get_channel=lambda cid: {10: ch1, 11: ch2}[cid],
    )
    pinged = set()
    rows = {(7, 3): _row(7, 3)}
    await cog._deliver_channel_posts([(1, 10, 7, 3), (1, 11, 7, 3)], rows, pinged)
    wa = _wa(role_id=42)
    await wa_mod.deliver_watchalong(_bot(thread, []), wa, _row(), pinged)
    assert ch1.sent[0]["allowed_mentions"].to_dict() == {"parse": [], "roles": [42]}
    assert ch2.sent[0]["allowed_mentions"].to_dict() == {"parse": []}
    assert not any("<@&" in t for t in _texts(ch2.sent[0]["view"]))
    assert "<@&" not in thread.events[0][1]
    # a different role on the same title still pings once
    thread2 = Thread(guild)
    await wa_mod.deliver_watchalong(_bot(thread2, []), _wa(role_id=43), _row(), pinged)
    assert thread2.events[0][2]["allowed_mentions"].to_dict() == {"parse": []}  # 43 absent
