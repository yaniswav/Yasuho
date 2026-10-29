"""Birthdays: validation, per-server isolation, the daily fire, privacy wiring."""

import datetime
import inspect
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.community.birthdays import cog as bd_cog
from cogs.community.birthdays import panel, store
from cogs.community.birthdays.cog import Birthdays
from tools import i18n, premium, privacy, retention, settings

UTC = datetime.timezone.utc
CONFIG = {"enabled": True, "channel_id": 9, "role_id": 5, "hour": 9, "timezone": "UTC"}


class Pool:
    """An in-memory birthdays table plus a query journal."""

    def __init__(self):
        self.rows = {}  # (guild, user) -> (month, day)
        self.queries = []
        self.timers = []
        self.claims = {}

    async def execute(self, query, *args):
        self.queries.append(query)
        if query.startswith("INSERT INTO birthdays"):
            self.rows[(args[0], args[1])] = (args[2], args[3])
        elif query.startswith("DELETE FROM birthdays"):
            self.rows.pop((args[0], args[1]), None)

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "birthday_state" in query:
            guild, day = args
            if self.claims.get(guild, datetime.date.min) < day:
                self.claims[guild] = day
                return guild
            return None
        return 1 if self.rows.pop((args[0], args[1]), None) is not None else None

    async def fetch(self, query, *args):
        self.queries.append(query)
        guild = args[0]
        if "month = $2" in query:
            month, day, feb29 = args[1:4]
            return [
                {"user_id": u}
                for (g, u), (m, d) in sorted(self.rows.items())
                if g == guild and ((m, d) == (month, day) or (feb29 and (m, d) == (2, 29)))
            ]
        return [
            {"user_id": u, "month": m, "day": d}
            for (g, u), (m, d) in self.rows.items()
            if g == guild
        ]

    def acquire(self):
        pool = self

        class _Conn:
            def transaction(self_):
                class _Tx:
                    async def __aenter__(s):
                        return None

                    async def __aexit__(s, *exc):
                        return False

                return _Tx()

            async def execute(self_, query, *args):
                pool.timers.append((query, args))

        class _Acq:
            async def __aenter__(s):
                return _Conn()

            async def __aexit__(s, *exc):
                return False

        return _Acq()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    settings.invalidate_all()
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=dict(CONFIG)))
    monkeypatch.setattr(i18n, "resolve_guild_locale", AsyncMock(return_value="en"))
    yield
    settings.invalidate_all()


def make_cog(plus=False, pool=None, guild=None):
    limits = premium.GUILD_PREMIUM if plus else premium.GUILD_FREE
    bot = types.SimpleNamespace(
        db_pool=pool or Pool(),
        premium=types.SimpleNamespace(for_guild=lambda gid: limits),
        get_cog=lambda name: None,
        get_guild=lambda gid: guild,
    )
    return Birthdays(bot)


def member(uid, roles=()):
    m = types.SimpleNamespace(
        id=uid, mention=f"<@{uid}>", display_name=f"u{uid}", roles=list(roles),
        add_roles=AsyncMock(), remove_roles=AsyncMock(),
    )
    m.guild = types.SimpleNamespace(id=1)
    return m


def make_guild(members, role_holders=(), role_ok=True, cached=None):
    role = types.SimpleNamespace(
        id=5, position=1, managed=False, is_default=lambda: False,
        permissions=discord.Permissions.none(),
    )
    role.members = list(role_holders)
    sent = []
    channel = types.SimpleNamespace(
        permissions_for=lambda me: discord.Permissions(view_channel=True, send_messages=True),
        send=AsyncMock(side_effect=lambda text, **kw: sent.append((text, kw))),
    )
    me = types.SimpleNamespace(
        guild_permissions=discord.Permissions(manage_roles=role_ok),
        top_role=types.SimpleNamespace(position=10),
    )
    guild = types.SimpleNamespace(
        id=1, name="Srv", me=me,
        get_member=lambda uid: (members if cached is None else cached).get(uid),
        get_role=lambda rid: role if rid == 5 else None,
        get_channel=lambda cid: channel if cid == 9 else None,
    )
    guild.sent = sent
    guild.queried = []

    async def query_members(user_ids, limit, cache):
        guild.queried.append((list(user_ids), limit, cache))
        return [members[u] for u in user_ids if u in members]

    guild.query_members = query_members
    return guild, role


def extra(planned):
    return {"guild_id": 1, "planned": planned.isoformat()}


# -- pure helpers --------------------------------------------------------------


def test_date_validation():
    assert store.valid_date(2, 29) and store.valid_date(12, 31)
    assert not store.valid_date(4, 31) and not store.valid_date(2, 30)
    assert not store.valid_date(13, 1) and not store.valid_date(1, 0)


def test_normalise_types_everything():
    cfg = store.normalise(
        {"enabled": "yes", "channel_id": "9", "role_id": 0, "hour": 24,
         "timezone": "Mars/Base", "message": "  "}
    )
    assert cfg == {"enabled": False, "channel_id": 9, "role_id": None, "hour": 9,
                   "timezone": "UTC", "message": None}
    assert store.normalise({"hour": 7, "timezone": "Europe/Paris", "message": "x" * 900})["message"] == "x" * 500
    assert store.normalise(None)["enabled"] is False


def test_next_fire_respects_timezone_and_is_strictly_after_now():
    now = datetime.datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    assert store.next_fire(now, 9, "UTC") == datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    exact = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert store.next_fire(exact, 9, "UTC") == exact + datetime.timedelta(days=1)
    # 09:00 in Paris (UTC+1 after the October switch) is 08:00 UTC
    assert store.next_fire(now, 9, "Europe/Paris") == datetime.datetime(2026, 10, 29, 8, 0, tzinfo=UTC)


def test_feb29_is_celebrated_on_feb28_in_non_leap_years_only():
    assert store.matches_today(datetime.date(2027, 2, 28)) == (2, 28, True)
    assert store.matches_today(datetime.date(2028, 2, 28)) == (2, 28, False)
    assert store.matches_today(datetime.date(2028, 2, 29)) == (2, 29, False)
    assert store.next_occurrence(datetime.date(2027, 2, 1), 2, 29) == datetime.date(2027, 2, 28)
    assert store.next_occurrence(datetime.date(2027, 3, 1), 2, 29) == datetime.date(2028, 2, 29)


# -- set / remove / isolation --------------------------------------------------


def ctx(guild_id=1, uid=10):
    c = MagicMock()
    c.guild = types.SimpleNamespace(id=guild_id)
    c.author = types.SimpleNamespace(id=uid)
    c.defer = AsyncMock()
    c.send = AsyncMock()
    return c


async def test_set_refuses_impossible_dates_and_stores_nothing():
    cog = make_cog()
    for day, month in ((31, 4), (30, 2)):
        c = ctx()
        await cog.birthday_set.callback(cog, c, day, month)
        assert "does not exist" in c.send.await_args.args[0]
    assert cog.bot.db_pool.rows == {}


async def test_set_accepts_feb29_and_is_per_server():
    cog = make_cog()
    await cog.birthday_set.callback(cog, ctx(1), 29, 2)
    await cog.birthday_set.callback(cog, ctx(2), 5, 3)
    assert cog.bot.db_pool.rows == {(1, 10): (2, 29), (2, 10): (3, 5)}
    c = ctx(1)
    await cog.birthday_remove.callback(cog, c)
    assert cog.bot.db_pool.rows == {(2, 10): (3, 5)}  # the other server keeps its own
    c = ctx(1)
    await cog.birthday_remove.callback(cog, c)
    assert "had not shared" in c.send.await_args.args[0]


async def test_set_refuses_where_the_feature_is_off():
    cog = make_cog()
    settings.get_guild.return_value = {"enabled": False}
    c = ctx()
    await cog.birthday_set.callback(cog, c, 5, 3)
    assert cog.bot.db_pool.rows == {}
    assert "not set up" in c.send.await_args.args[0]


# -- the daily fire --------------------------------------------------------------


async def fire(cog, planned, now):
    cog_now = discord.utils.utcnow
    discord.utils.utcnow = lambda: now
    try:
        return await cog._daily(extra(planned))
    finally:
        discord.utils.utcnow = cog_now


async def test_fire_moves_the_role_and_mentions_only_todays_members():
    pool = Pool()
    pool.rows = {(1, 10): (10, 28), (1, 11): (10, 28), (1, 12): (3, 3), (2, 13): (10, 28)}
    yesterday = member(12)
    members = {10: member(10), 12: yesterday}  # 11 left the server, 13 is in another one
    guild, role = make_guild(members, role_holders=[yesterday])
    yesterday.roles.append(role)
    cog = make_cog(pool=pool, guild=guild)
    planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert await fire(cog, planned, planned + datetime.timedelta(seconds=2)) == "sent"
    yesterday.remove_roles.assert_awaited_once()
    members[10].add_roles.assert_awaited_once()
    text, kw = guild.sent[0]
    assert "<@10>" in text and "<@11>" not in text and "<@13>" not in text
    am = kw["allowed_mentions"]
    assert [u.id for u in am.users] == [10] and am.roles is False and am.everyone is False
    # the next day was scheduled before anything was delivered
    assert any("INSERT INTO timers" in q for q, _ in pool.timers)


async def test_fire_on_feb28_of_a_non_leap_year_celebrates_feb29():
    pool = Pool()
    pool.rows = {(1, 10): (2, 29)}
    members = {10: member(10)}
    guild, _role = make_guild(members)
    cog = make_cog(pool=pool, guild=guild)
    planned = datetime.datetime(2027, 2, 28, 9, 0, tzinfo=UTC)
    assert await fire(cog, planned, planned) == "sent"
    leap = datetime.datetime(2028, 2, 28, 9, 0, tzinfo=UTC)
    guild.sent.clear()
    assert await fire(cog, leap, leap) == "skip_nobody"


async def test_late_fire_window():
    pool = Pool()
    pool.rows = {(1, 10): (10, 28)}
    guild, _role = make_guild({10: member(10)})
    cog = make_cog(pool=pool, guild=guild)
    planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert await fire(cog, planned, planned + datetime.timedelta(hours=11, minutes=59)) == "sent"
    guild.sent.clear()
    pool.timers.clear()
    assert await fire(cog, planned, planned + datetime.timedelta(hours=12, minutes=1)) == "skip_late"
    assert guild.sent == []
    assert any("INSERT INTO timers" in q for q, _ in pool.timers)  # still rescheduled


async def test_not_plus_uses_the_default_message_and_plus_uses_the_template():
    settings.get_guild.return_value = {**CONFIG, "message": "Party {users} at {server}"}
    for plus, expected in ((False, "Happy birthday"), (True, "Party <@10> at Srv")):
        pool = Pool()
        pool.rows = {(1, 10): (10, 28)}
        guild, _role = make_guild({10: member(10)})
        cog = make_cog(plus=plus, pool=pool, guild=guild)
        planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
        await fire(cog, planned, planned)
        assert guild.sent[0][0].startswith(expected)


async def test_disabled_guild_skips_quietly():
    settings.get_guild.return_value = {**CONFIG, "enabled": False}
    cog = make_cog()
    planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert await fire(cog, planned, planned) == "skip_disabled"
    assert cog.bot.db_pool.timers == []


def test_render_caps_the_text_and_the_mentions():
    guild = types.SimpleNamespace(name="S" * 30)
    many = [member(i) for i in range(100)]
    text = bd_cog.render_message(guild, many, "{users} {server}" * 60)
    assert len(text) <= 2000
    assert "<@39>" in bd_cog.render_message(guild, many) and "<@40>" not in bd_cog.render_message(guild, many)


# -- member leaves, hot path ----------------------------------------------------


async def test_member_remove_deletes_the_row_and_costs_nothing_when_unconfigured():
    pool = Pool()
    pool.rows = {(1, 10): (3, 3), (2, 10): (3, 3)}
    cog = make_cog(pool=pool)
    payload = types.SimpleNamespace(guild_id=1, user=types.SimpleNamespace(id=10))
    await cog.on_raw_member_remove(payload)
    assert pool.rows == {(2, 10): (3, 3)}
    settings.get_guild.return_value = None  # guild never configured birthdays
    pool.queries.clear()
    await cog.on_raw_member_remove(types.SimpleNamespace(guild_id=2, user=types.SimpleNamespace(id=10)))
    assert pool.queries == [] and (2, 10) in pool.rows


# -- structure and privacy --------------------------------------------------------


def test_hybrid_surface_and_panel_contract():
    assert "guild_only" in inspect.getsource(Birthdays) and Birthdays.birthday.fallback == "list"
    assert Birthdays.birthdays.checks  # /birthdays carries manage_guild


def test_privacy_and_retention_wiring():
    assert ("birthdays", "DELETE FROM birthdays WHERE user_id = $1") in privacy.USER_DELETE_QUERIES
    assert dict(retention.GUILD_DELETE_QUERIES)["birthdays"] == "DELETE FROM birthdays WHERE guild_id = $1"
    assert "FROM birthdays" in retention.STORED_GUILD_IDS_QUERY
    assert privacy.EXPORT_VERSION == 17 and '"birthdays_shared"' in inspect.getsource(privacy)


async def test_panel_role_pick_refuses_unmanageable_roles():
    guild, role = make_guild({})
    cog = make_cog(guild=guild)
    view = panel.BirthdayPanel.__new__(panel.BirthdayPanel)
    view.guild, view.cog, view.config = guild, cog, dict(store.normalise(CONFIG))
    view.save_and_rerender = AsyncMock()
    inter = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))
    role.position = 99  # above the bot
    await view.pick_role(inter, role)
    inter.response.send_message.assert_awaited_once()
    view.save_and_rerender.assert_not_awaited()
    role.position = 1
    await view.pick_role(inter, role)
    view.save_and_rerender.assert_awaited_once()


# -- review fixes ------------------------------------------------------------------


async def test_uncached_members_are_resolved_with_chunked_queries():
    pool = Pool()
    pool.rows = {(1, i): (10, 28) for i in range(100, 250)}
    members = {i: member(i) for i in range(100, 250) if i != 120}  # 120 left
    guild, _role = make_guild(members, cached={})  # empty member cache
    cog = make_cog(pool=pool, guild=guild)
    planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert await fire(cog, planned, planned) == "sent"
    assert [len(q[0]) for q in guild.queried] == [100, 50]
    assert all(q[1] == 100 and q[2] is True for q in guild.queried)
    assert "<@120>" not in guild.sent[0][0]  # a leaver is never mentioned


async def test_role_cleanup_reaches_yesterdays_member_missing_from_the_cache():
    pool = Pool()
    pool.rows = {(1, 12): (10, 27)}  # yesterday's birthday, not cached, not in role.members
    gone = member(12)
    guild, role = make_guild({12: gone}, cached={})
    gone.roles.append(role)
    cog = make_cog(pool=pool, guild=guild)
    planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert await fire(cog, planned, planned) == "skip_nobody"
    gone.remove_roles.assert_awaited_once()


async def test_two_runs_of_the_same_local_day_post_once():
    pool = Pool()
    pool.rows = {(1, 10): (10, 28)}
    guild, _role = make_guild({10: member(10)})
    cog = make_cog(pool=pool, guild=guild)
    first = datetime.datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    second = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)  # hour changed, same local day
    assert await fire(cog, first, first) == "sent"
    assert await fire(cog, second, second) == "skip_already"
    assert len(guild.sent) == 1
    next_day = datetime.datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
    pool.rows = {(1, 10): (10, 29)}
    assert await fire(cog, next_day, next_day) == "sent"


async def test_a_replaced_row_looks_back_twelve_hours():
    cog = make_cog()
    now = datetime.datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    orig = discord.utils.utcnow
    discord.utils.utcnow = lambda: now
    try:
        await cog._write_timer(1, store.normalise(CONFIG), replace=True)  # 09:00 passed 6 h ago
        planned = cog.bot.db_pool.timers[-1][1][1]
        assert planned == datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
        await cog._write_timer(1, store.normalise({**CONFIG, "hour": 1}), replace=True)  # 14 h ago: tomorrow
        assert cog.bot.db_pool.timers[-1][1][1] == datetime.datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    finally:
        discord.utils.utcnow = orig


async def test_reschedule_is_retried_once_then_logged(caplog):
    guild, _role = make_guild({})
    cog = make_cog(guild=guild)
    calls = []

    async def flaky(*a, **kw):
        calls.append(kw.get("after"))
        if len(calls) == 1:
            raise RuntimeError("db down")

    cog._write_timer = flaky
    planned = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    await fire(cog, planned, planned)
    assert len(calls) == 2 and calls[1] == planned  # strictly after the planned slot
    calls.clear()

    async def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("db down")

    cog._write_timer = broken
    with caplog.at_level("WARNING"):
        assert await fire(cog, planned, planned) == "reschedule"
    assert len(calls) == 2 and "BIRTHDAY-FAIL" in caplog.text


async def test_list_filters_in_sql_and_hides_leavers():
    pool = Pool()
    pool.rows = {(1, 10): (10, 30), (1, 11): (10, 31), (1, 12): (12, 30)}
    guild, _role = make_guild({10: member(10)}, cached={})  # 11 left; 12 is beyond 30 days
    guild.id = 1
    cog = make_cog(pool=pool, guild=guild)
    c = ctx()
    c.guild = guild
    orig = discord.utils.utcnow
    discord.utils.utcnow = lambda: datetime.datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    try:
        await cog.birthday.callback(cog, c)
    finally:
        discord.utils.utcnow = orig
    c.defer.assert_awaited_once_with(ephemeral=True)
    sql = [q for q in pool.queries if "FROM birthdays" in q][0]
    assert "ANY" in sql and "LIMIT" not in sql
    desc = c.send.await_args.kwargs["embed"].description
    assert "30 October" in desc and "31 October" not in desc and "December" not in desc


async def test_panel_strips_the_previous_role_on_disable_or_change():
    holder = member(10)
    pool = Pool()
    pool.rows = {(1, 10): (10, 28)}
    guild, role = make_guild({10: holder})
    holder.roles.append(role)
    cog = make_cog(pool=pool, guild=guild)
    orig = discord.utils.utcnow
    discord.utils.utcnow = lambda: datetime.datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    try:
        await cog.strip_role(guild, 5, store.normalise(CONFIG))
    finally:
        discord.utils.utcnow = orig
    holder.remove_roles.assert_awaited_once()
    source = inspect.getsource(panel.BirthdayPanel.save_and_rerender)
    assert "strip_role" in source and "live_role_id" in inspect.getsource(panel.BirthdayPanel.__init__)


async def test_the_dashboard_reconcile_action_returns_the_next_run():
    from cogs.system import dashboard_actions

    cog = make_cog(guild=object())
    cog.bot.get_cog = lambda name: cog if name == "Birthdays" else None
    cog._write_timer = AsyncMock(return_value=None)
    run = dashboard_actions._EXECUTORS["birthdays_reconcile"]
    cog.config_for = AsyncMock(return_value=store.normalise(CONFIG))
    when = datetime.datetime(2030, 1, 1, 9, tzinfo=datetime.timezone.utc)
    cog._write_timer.return_value = when
    assert await run(cog.bot, 1, {}) == {"ok": True, "next_run": when.isoformat()}
    cog._write_timer.return_value = None
    assert await run(cog.bot, 1, {}) == {"ok": True, "next_run": None}
    cog.bot.get_guild = lambda gid: None
    assert await run(cog.bot, 1, {}) == {"ok": False, "error": "guild_unavailable"}
