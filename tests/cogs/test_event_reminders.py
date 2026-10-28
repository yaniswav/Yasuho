"""Discord event reminders: timers per (event, offset), tiers, stale/gone skips."""

import datetime
import json
import types
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.config import event_reminders as er
from cogs.config.event_reminders import EventReminderPanel, EventReminders
from tools import premium, settings

NOW = discord.utils.utcnow()
H = datetime.timedelta(hours=1)


class Conn:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *exc):
                return False

        return _Tx()

    async def execute(self, query, *args):
        if "pg_advisory_xact_lock" in query:
            return
        assert query.startswith("DELETE FROM timers")
        guild = args[1]
        if "event_id" in query:
            event, keep_due_by = args[2], None
        else:
            event, keep_due_by = None, args[2]
        self.pool.timers = [
            t for t in self.pool.timers
            if not (
                t["guild_id"] == guild
                and (event is None or t["event_id"] == event)
                and (keep_due_by is None or t["expires"] > keep_due_by)
            )
        ]

    async def executemany(self, query, rows):
        assert "INSERT INTO timers" in query
        for fire_at, extra in rows:
            data = json.loads(extra)
            self.pool.timers.append(
                {"expires": fire_at, "extra": data, "guild_id": str(data["guild_id"]),
                 "event_id": str(data["event_id"])}
            )


class Pool:
    def __init__(self, blobs=None, enabled=()):
        self.blobs = dict(blobs or {})
        self.enabled = list(enabled)
        self.timers = []
        self.queries = []

    def acquire(self):
        conn = Conn(self)

        class _Acq:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *exc):
                return False

        return _Acq()

    async def fetchval(self, query, *args):
        self.queries.append(query)
        blob = self.blobs.get(args[0])
        return None if blob is None else json.dumps(blob)

    async def fetch(self, query, *args):
        self.queries.append(query)
        return [{"guild_id": g} for g in self.enabled]

    async def execute(self, query, *args):
        self.queries.append(query)
        self.blobs.setdefault(args[0], {})[args[1]] = json.loads(args[2])


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    settings.invalidate_all()
    yield
    settings.invalidate_all()


CONFIG = {"enabled": True, "channel_id": 9}
PLUS_CONFIG = {**CONFIG, "offsets": [1440, 60, 15], "role_id": 5}


def _event(eid=100, start=None, status=discord.EventStatus.scheduled, gid=1):
    return types.SimpleNamespace(
        id=eid, guild_id=gid, status=status, start_time=start or NOW + 48 * H,
        name="Movie night", location=None, channel=None,
        url=f"https://discord.com/events/{gid}/{eid}",
    )


def _guild(events=(), role=None, gid=1):
    channel = types.SimpleNamespace(
        id=9, send=AsyncMock(),
        permissions_for=lambda me: discord.Permissions(view_channel=True, send_messages=True),
    )
    role = role or types.SimpleNamespace(id=5, is_default=lambda: False, mentionable=True)
    table = {e.id: e for e in events}
    guild = types.SimpleNamespace(
        id=gid, me=object(), preferred_locale=discord.Locale.american_english,
        scheduled_events=list(table.values()), get_scheduled_event=table.get,
        get_channel=lambda cid: channel if cid == 9 else None,
        get_role=lambda rid: role if rid == 5 else None,
    )
    return guild, channel


def _cog(pool, guilds=(), plus=False):
    limits = premium.GUILD_PREMIUM if plus else premium.GUILD_FREE
    table = {g.id: g for g in guilds}
    bot = types.SimpleNamespace(
        db_pool=pool, premium=types.SimpleNamespace(for_guild=lambda gid: limits),
        get_guild=table.get, get_cog=lambda name: None,
    )
    return EventReminders(bot)


def _offsets(pool):
    return sorted(t["extra"]["offset_minutes"] for t in pool.timers)


def _extra(event, offset):
    return {
        "guild_id": event.guild_id, "event_id": event.id,
        "offset_minutes": offset, "start_time": event.start_time.isoformat(),
    }


async def test_create_schedules_the_free_15_minutes_even_with_custom_offsets_stored():
    pool = Pool({1: {"event_reminders": {**PLUS_CONFIG}}})
    cog = _cog(pool)
    await cog.on_scheduled_event_create(_event())
    # Rows cover every configured offset; the tier test happens at fire time.
    assert _offsets(pool) == [15, 60, 1440]


async def test_default_config_has_a_single_15_minute_row():
    pool = Pool({1: {"event_reminders": CONFIG}})
    await _cog(pool).on_scheduled_event_create(_event())
    assert _offsets(pool) == [15]


async def test_past_fire_times_are_not_scheduled():
    pool = Pool({1: {"event_reminders": PLUS_CONFIG}})
    await _cog(pool).on_scheduled_event_create(_event(start=NOW + 30 * datetime.timedelta(minutes=1)))
    assert _offsets(pool) == [15]


async def test_start_time_change_replaces_the_old_timers():
    pool = Pool({1: {"event_reminders": CONFIG}})
    cog = _cog(pool)
    before = _event(start=NOW + 48 * H)
    await cog.on_scheduled_event_create(before)
    after = _event(start=NOW + 72 * H)
    await cog.on_scheduled_event_update(before, after)
    assert len(pool.timers) == 1
    assert pool.timers[0]["extra"]["start_time"] == after.start_time.isoformat()


async def test_a_rename_alone_does_no_work():
    pool = Pool({1: {"event_reminders": CONFIG}})
    cog = _cog(pool)
    event = _event()
    await cog.on_scheduled_event_create(event)
    pool.timers.clear()
    await cog.on_scheduled_event_update(event, _event())
    assert pool.timers == []


@pytest.mark.parametrize("status", [discord.EventStatus.canceled, discord.EventStatus.active])
async def test_cancel_or_start_removes_pending_timers(status):
    pool = Pool({1: {"event_reminders": CONFIG}})
    cog = _cog(pool)
    event = _event()
    await cog.on_scheduled_event_create(event)
    await cog.on_scheduled_event_update(event, _event(status=status))
    assert pool.timers == []


async def test_delete_removes_only_that_events_timers():
    pool = Pool({1: {"event_reminders": CONFIG}})
    cog = _cog(pool)
    await cog.on_scheduled_event_create(_event(100))
    await cog.on_scheduled_event_create(_event(101))
    await cog.on_scheduled_event_delete(_event(100))
    assert [t["extra"]["event_id"] for t in pool.timers] == [101]


async def test_a_guild_without_the_feature_costs_no_query_once_cached():
    pool = Pool({1: {"locale": "en"}})
    cog = _cog(pool)
    await cog.on_scheduled_event_create(_event())  # warms the shared guild blob
    pool.queries.clear()
    event = _event()
    await cog.on_scheduled_event_create(event)
    await cog.on_scheduled_event_update(event, _event(start=NOW + 90 * H))
    await cog.on_scheduled_event_delete(event)
    assert pool.queries == [] and pool.timers == []


async def test_startup_reconcile_is_idempotent_and_skips_other_guilds():
    events = [_event(100), _event(101, start=NOW + 5 * H)]
    guild, _ch = _guild(events)
    other, _ch2 = _guild([_event(7, gid=2)], gid=2)
    pool = Pool({1: {"event_reminders": CONFIG}, 2: {"locale": "en"}}, enabled=[1])
    cog = _cog(pool, [guild, other])
    await cog.on_ready()
    first = sorted((t["extra"]["event_id"], t["extra"]["offset_minutes"]) for t in pool.timers)
    await cog.on_ready()
    second = sorted((t["extra"]["event_id"], t["extra"]["offset_minutes"]) for t in pool.timers)
    assert first == second == [(100, 15), (101, 15)]


async def test_disabling_from_the_panel_clears_the_guilds_timers():
    event = _event()
    guild, _ch = _guild([event])
    pool = Pool({1: {"event_reminders": CONFIG}})
    cog = _cog(pool, [guild])
    await cog.reconcile_guild(guild)
    assert len(pool.timers) == 1
    guild.get_channel = lambda cid: discord.Object(cid, type=discord.abc.GuildChannel)
    panel = EventReminderPanel(cog, guild, 7, er.normalise(CONFIG))
    panel.message = None
    interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(
            is_done=lambda: False, edit_message=AsyncMock(), defer=AsyncMock()
        )
    )
    await panel._toggle(interaction)
    assert pool.blobs[1]["event_reminders"]["enabled"] is False
    assert pool.timers == []


async def test_a_fresh_timer_sends_the_reminder_without_a_ping_for_a_free_guild():
    event = _event(start=NOW + 10 * datetime.timedelta(minutes=1))
    guild, channel = _guild([event])
    cog = _cog(Pool({1: {"event_reminders": PLUS_CONFIG}}), [guild], plus=False)
    await cog.on_event_reminder_timer_complete(_extra(event, 15))
    channel.send.assert_awaited_once()
    text = channel.send.await_args.args[0]
    assert "Movie night" in text and event.url in text and "<t:" in text
    assert "<@&5>" not in text
    assert channel.send.await_args.kwargs["allowed_mentions"].roles is False


async def test_yasuho_plus_pings_only_the_configured_role():
    event = _event(start=NOW + 10 * datetime.timedelta(minutes=1))
    guild, channel = _guild([event])
    cog = _cog(Pool({1: {"event_reminders": PLUS_CONFIG}}), [guild], plus=True)
    await cog.on_event_reminder_timer_complete(_extra(event, 15))
    assert channel.send.await_args.args[0].startswith("<@&5> ")
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert [r.id for r in mentions.roles] == [5]
    assert mentions.everyone is False and mentions.users is False


async def test_a_custom_offset_is_skipped_while_not_yasuho_plus():
    event = _event(start=NOW + 30 * H)
    guild, channel = _guild([event])
    cog = _cog(Pool({1: {"event_reminders": PLUS_CONFIG}}), [guild], plus=False)
    await cog.on_event_reminder_timer_complete(_extra(event, 1440))
    channel.send.assert_not_awaited()


async def test_a_stale_timer_is_skipped():
    event = _event(start=NOW + 10 * datetime.timedelta(minutes=1))
    guild, channel = _guild([event])
    cog = _cog(Pool({1: {"event_reminders": CONFIG}}), [guild])
    stale = {**_extra(event, 15), "start_time": (event.start_time - H).isoformat()}
    await cog.on_event_reminder_timer_complete(stale)
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("mode", ["gone", "canceled", "active", "disabled"])
async def test_gone_cancelled_started_or_disabled_is_skipped(mode):
    event = _event(start=NOW + 10 * datetime.timedelta(minutes=1))
    live = {"canceled": discord.EventStatus.canceled, "active": discord.EventStatus.active}
    if mode in live:
        event.status = live[mode]
    guild, channel = _guild([] if mode == "gone" else [event])
    blob = {**CONFIG, "enabled": False} if mode == "disabled" else CONFIG
    cog = _cog(Pool({1: {"event_reminders": blob}}), [guild])
    await cog.on_event_reminder_timer_complete(_extra(event, 15))
    channel.send.assert_not_awaited()


async def test_a_failed_send_is_logged_and_never_raises(caplog):
    event = _event(start=NOW + 10 * datetime.timedelta(minutes=1))
    guild, channel = _guild([event])
    channel.send.side_effect = discord.HTTPException(
        types.SimpleNamespace(status=500, reason="x"), "boom"
    )
    cog = _cog(Pool({1: {"event_reminders": CONFIG}}), [guild])
    with caplog.at_level("WARNING"):
        await cog.on_event_reminder_timer_complete(_extra(event, 15))
    assert "EVENTREMIND-FAIL guild=1 event=100 reason=http_httpexception" in caplog.text


async def test_the_role_picker_refuses_everyone_and_unpingable_roles():
    guild, _ch = _guild()
    guild.get_channel = lambda cid: discord.Object(cid, type=discord.abc.GuildChannel)
    panel = EventReminderPanel(_cog(Pool(), [guild], plus=True), guild, 7, er.normalise(CONFIG))
    for role, mention_everyone in (
        (types.SimpleNamespace(id=1, is_default=lambda: True, mentionable=True), True),
        (types.SimpleNamespace(id=6, is_default=lambda: False, mentionable=False), False),
    ):
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(
            response=response,
            user=types.SimpleNamespace(
                guild_permissions=discord.Permissions(mention_everyone=mention_everyone)
            ),
        )
        await panel.pick_role(interaction, role)
        response.send_message.assert_awaited_once()
        assert panel.config["role_id"] is None


async def test_customize_is_refused_for_a_free_guild_and_opens_for_plus():
    for plus in (False, True):
        guild, _ch = _guild()
        panel = EventReminderPanel(_cog(Pool(), [guild], plus=plus), guild, 7, er.normalise(None))
        response = types.SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = types.SimpleNamespace(response=response, user=types.SimpleNamespace(id=7))
        await panel._customize(interaction)
        assert response.edit_message.await_count == (1 if plus else 0)
        assert response.send_message.await_count == (0 if plus else 1)


def test_normalise_drops_junk_and_defaults_to_the_free_offset():
    assert er.normalise({"offsets": [5, "60", True, 60, 1440]})["offsets"] == [1440, 60]
    assert er.normalise({"offsets": []})["offsets"] == [15]
    assert er.normalise({"channel_id": "9", "role_id": 0})["channel_id"] == 9
    assert er.normalise(None)["enabled"] is False


async def test_a_guild_reconcile_keeps_rows_that_are_already_due():
    # Bot down across T-15: the due row must survive the startup reconcile.
    event = _event(start=NOW + 10 * datetime.timedelta(minutes=1))
    guild, _ch = _guild([event])
    pool = Pool({1: {"event_reminders": CONFIG}})
    cog = _cog(pool, [guild])
    due = NOW - datetime.timedelta(minutes=5)
    pool.timers.append({"expires": due, "extra": {}, "guild_id": "1", "event_id": "100"})
    await cog.reconcile_guild(guild)
    assert [t["expires"] for t in pool.timers] == [due]


async def test_a_private_event_is_not_announced_in_a_public_channel(monkeypatch):
    hidden = types.SimpleNamespace(mention="<#77>")
    event = _event(start=NOW + 15 * datetime.timedelta(minutes=1))
    event.channel = hidden
    guild, channel = _guild([event])
    monkeypatch.setattr(er, "everyone_can_read", lambda ch, *, unknown: ch is not hidden)
    cog = _cog(Pool({1: {"event_reminders": CONFIG}}), [guild])
    extra = {"guild_id": 1, "event_id": 100, "offset_minutes": 15,
             "start_time": event.start_time.isoformat()}
    assert await cog._deliver(extra) == "skip_private"
    channel.send.assert_not_awaited()
