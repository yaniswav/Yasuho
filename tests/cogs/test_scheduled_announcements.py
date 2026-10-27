"""Calendar, persistence and dispatch contracts for scheduled announcements."""

import asyncio
import copy
import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord
import pytest

from cogs.community.announcements_schedule import next_occurrence
from cogs.community.announcements_store import render, validate
from cogs.community.reminders import Reminder
from cogs.community.scheduled_announcements import ScheduledAnnouncements
from tools import privacy, retention

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 1, 1, 9, tzinfo=UTC)


def local(text):
    return datetime.datetime.fromisoformat(text)


def at(text):
    return local(text).replace(tzinfo=UTC)


def announcement(**changes):
    row = dict(
        id=1, guild_id=3, channel_id=9, author_id=5, content="hello", embed=None,
        role_id=None, timezone="UTC", local_time=local("2026-01-01T09:00"),
        recurrence="daily", enabled=True, next_run_at=NOW, failures=0,
        last_error=None, revision=1, created_at=NOW, updated_at=NOW,
    )
    row.update(changes)
    return row


@pytest.mark.parametrize("recurrence,anchor,after,expected", [
    ("daily", "2026-01-01T09:00", "2026-01-01T09:00", "2026-01-02T09:00"),
    ("weekly", "2026-01-05T09:00", "2026-01-06T09:00", "2026-01-12T09:00"),
    ("monthly", "2026-01-31T09:00", "2026-01-31T09:00", "2026-02-28T09:00"),
    ("monthly", "2026-01-31T09:00", "2026-02-28T09:00", "2026-03-31T09:00"),
    ("monthly", "2024-01-31T09:00", "2024-01-31T09:00", "2024-02-29T09:00"),
    ("daily", "1900-01-01T09:00", "2026-01-01T09:00", "2026-01-02T09:00"),
    ("weekly", "2026-02-02T09:00", "2026-01-01T09:00", "2026-02-02T09:00"),
    ("none", "2026-01-01T10:00", "2026-01-01T09:00", "2026-01-01T10:00"),
    ("none", "2026-01-01T09:00", "2026-01-01T09:00", None),
])
def test_calendar_occurrences(recurrence, anchor, after, expected):
    result = next_occurrence(local(anchor), "UTC", recurrence, at(after))
    assert result == (at(expected) if expected else None)


@pytest.mark.parametrize("recurrence,anchor,after,expected", [
    ("daily", "2026-03-28T09:00", "2026-03-28T08:00", "2026-03-29T07:00"),
    ("daily", "2026-10-24T09:00", "2026-10-24T07:00", "2026-10-25T08:00"),
    ("weekly", "2026-03-23T09:00", "2026-03-23T08:00", "2026-03-30T07:00"),
    ("weekly", "2026-10-19T09:00", "2026-10-19T07:00", "2026-10-26T08:00"),
    ("monthly", "2026-02-28T09:00", "2026-03-28T08:00", "2026-04-28T07:00"),
    ("daily", "2026-03-28T02:30", "2026-03-28T01:30", "2026-03-29T01:30"),
    ("daily", "2026-03-28T02:30", "2026-03-29T01:30", "2026-03-30T00:30"),
    ("daily", "2026-10-24T02:30", "2026-10-24T00:30", "2026-10-25T00:30"),
    ("daily", "2026-10-24T02:30", "2026-10-25T00:30", "2026-10-26T01:30"),
    ("none", "2026-03-29T02:30", "2026-03-28T00:00", "2026-03-29T01:30"),
])
def test_paris_dst(recurrence, anchor, after, expected):
    assert next_occurrence(local(anchor), "Europe/Paris", recurrence, at(after)) == at(expected)


def test_nuuk_dst_gap_crosses_local_midnight():
    assert next_occurrence(
        local("2026-03-27T23:30"), "America/Nuuk", "daily", at("2026-03-29T01:15"),
    ) == at("2026-03-29T01:30")


def test_recurrence_requires_aware_boundary_and_naive_anchor():
    with pytest.raises(ValueError):
        next_occurrence(NOW, "UTC", "daily", NOW)
    with pytest.raises(ValueError):
        next_occurrence(NOW.replace(tzinfo=None), "UTC", "daily", NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        next_occurrence(NOW.replace(tzinfo=None), "UTC", "hourly", NOW)


@pytest.mark.parametrize("changes", [
    {"timezone": "Not/AZone"}, {"content": "", "embed": None},
    {"content": "  "}, {"content": "x" * 2001}, {"recurrence": "hourly"},
    # The embed is an embed_creator config: it must render something and stay
    # under the stored size cap.
    {"embed": {}}, {"embed": ["not", "a", "dict"]},
    {"embed": {"description": "x" * 17000}},
    {"local_time": NOW}, {"role_id": 0},
    {"enabled": "yes"}, {"channel_id": False},
])
def test_write_validation(changes):
    with pytest.raises(ValueError):
        validate(announcement(**changes))


@pytest.mark.parametrize("role_id", [None, 42])
def test_render_uses_real_types_and_suppresses_all_other_mentions(role_id):
    content, embed, mentions = render(announcement(
        role_id=role_id, content="@everyone @here <@5> <@&99>",
        embed=json.dumps({"title": "Title", "description": "Body", "color": 123}),
    ))
    assert isinstance(embed, discord.Embed)
    assert embed.title == "Title" and embed.colour.value == 123
    assert isinstance(mentions, discord.AllowedMentions)
    payload = mentions.to_dict()
    assert payload["parse"] == []
    assert mentions.replied_user is False
    assert payload.get("roles", []) == ([42] if role_id else [])
    assert content.startswith("<@&42> ") if role_id else content.startswith("@everyone")


class Transaction:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        await self.pool.lock.acquire()
        self.saved = copy.deepcopy((self.pool.rows, self.pool.timers))
        self.pool.events.append("begin")

    async def __aexit__(self, kind, exc, tb):
        if kind is not None:
            self.pool.rows, self.pool.timers = self.saved
        self.pool.events.append("rollback" if kind else "commit")
        self.pool.lock.release()


class Connection:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def transaction(self):
        return Transaction(self.pool)

    async def fetchval(self, query, *args):
        self.pool.queries.append((query, args))
        return sum(r["guild_id"] == args[0] for r in self.pool.rows.values())

    async def fetchrow(self, query, *args):
        pool = self.pool
        pool.queries.append((query, args))
        if query.startswith("SELECT"):
            row = pool.rows.get(args[-1])
            if row is not None and (len(args) == 1 or row["guild_id"] == args[0]):
                return dict(row)
            return None
        if query.startswith("DELETE FROM timers"):
            timer = pool.timers.get(args[0])
            if timer and timer["announcement_id"] == args[1]:
                pool.events.append("claim")
                return pool.timers.pop(args[0])
            return None
        if query.startswith("DELETE FROM scheduled_announcements"):
            row = pool.rows.get(args[1])
            if row is None or row["guild_id"] != args[0]:
                return None
            del pool.rows[row["id"]]
            pool.timers = {k: t for k, t in pool.timers.items() if t["announcement_id"] != row["id"]}
            return dict(row)
        if query.startswith("INSERT INTO scheduled_announcements"):
            row = announcement(id=max(pool.rows, default=0) + 1, guild_id=args[0])
            fields = ("channel_id", "author_id", "content", "embed", "role_id", "timezone", "local_time", "recurrence", "enabled", "next_run_at")
            row.update(zip(fields, args[1:]))
            pool.rows[row["id"]] = row
            return dict(row)
        if "failures=CASE" in query:
            row = pool.rows.get(args[0])
            if row is None or row["revision"] != args[1]:
                return None
            row["last_error"] = args[2]
            row["failures"] = 0 if args[2] is None else row["failures"] + 1
            if row["failures"] >= 3:
                row.update(enabled=False, next_run_at=None)
            return dict(row)
        if query.startswith("UPDATE scheduled_announcements SET channel_id"):
            row = pool.rows[args[1]]
            fields = ("channel_id", "author_id", "content", "embed", "role_id", "timezone", "local_time", "recurrence", "enabled", "next_run_at")
            row.update(zip(fields, args[2:]))
            row.update(revision=row["revision"] + 1, failures=0, last_error=None)
            return dict(row)
        raise AssertionError(query)

    async def execute(self, query, *args):
        pool = self.pool
        pool.queries.append((query, args))
        if query.startswith("SELECT pg_advisory"):
            return "SELECT 1"
        if query.startswith("DELETE FROM timers"):
            pool.timers = {k: t for k, t in pool.timers.items() if t["announcement_id"] != args[0]}
        elif query.startswith("INSERT INTO timers"):
            if pool.fail_insert:
                raise RuntimeError("insert failed")
            pool.events.append("insert")
            timer_id = pool.next_timer
            pool.next_timer += 1
            pool.timers[timer_id] = dict(
                id=timer_id, event="announcement", expires=args[0], extra=args[1],
                announcement_id=args[2], claimed_at=None,
            )
        elif query.startswith("UPDATE scheduled_announcements SET enabled=false"):
            pool.rows[args[0]].update(enabled=False, next_run_at=None, last_error="calendar-error")
        elif query.startswith("UPDATE scheduled_announcements"):
            pool.rows[args[0]].update(next_run_at=args[1], enabled=args[2])
        elif query.startswith("DELETE FROM scheduled_announcements"):
            for key in list(pool.rows):
                if pool.rows[key]["guild_id"] == args[0]:
                    del pool.rows[key]
                    pool.timers = {k: t for k, t in pool.timers.items() if t["announcement_id"] != key}
        else:
            raise AssertionError(query)
        return "DELETE 1"


class Pool:
    def __init__(self, row=None):
        self.rows = {row["id"]: row} if row else {}
        self.timers = {10: dict(id=10, event="announcement", expires=row["next_run_at"], extra={}, announcement_id=row["id"], claimed_at=None)} if row else {}
        self.events = []
        self.queries = []
        self.lock = asyncio.Lock()
        self.next_timer = 11
        self.fail_insert = False

    def acquire(self):
        return Connection(self)

    async def fetchrow(self, query, *args):
        async with self.acquire() as conn:
            async with conn.transaction():
                return await conn.fetchrow(query, *args)

    async def execute(self, query, *args):
        async with self.acquire() as conn:
            async with conn.transaction():
                return await conn.execute(query, *args)

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return [dict(r) for r in self.rows.values() if r["guild_id"] == args[0]]


def engine(pool):
    channel = SimpleNamespace(
        id=9, mention="<#9>", guild=SimpleNamespace(id=3), type=discord.ChannelType.text,
        permissions_for=lambda me: discord.Permissions(view_channel=True, send_messages=True, embed_links=True),
        send=AsyncMock(),
    )
    guild = SimpleNamespace(
        id=3, name="Guild", member_count=10, me=object(), get_channel=lambda cid: channel
    )
    bot = SimpleNamespace(
        db_pool=pool, get_guild=lambda gid: guild,
        fetch_channel=AsyncMock(return_value=channel),
    )
    cog = ScheduledAnnouncements(bot)
    reminder = object.__new__(Reminder)
    reminder.bot = bot
    reminder._have_data = asyncio.Event()
    bot.get_cog = lambda name: cog if name == "ScheduledAnnouncements" else reminder
    dispatched = []
    bot.dispatch = lambda event, extra: dispatched.append((event, extra))
    return cog, reminder, channel, dispatched


async def fire(cog, reminder, pool, dispatched):
    timer = next(iter(pool.timers.values()))
    await reminder._deliver_at_most_once(dict(timer))
    if dispatched:
        event, extra = dispatched.pop()
        assert event == "announcement_timer_complete"
        await cog.on_announcement_timer_complete(extra)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(discord.utils, "utcnow", lambda: NOW)


async def test_reschedule_commits_before_delivery_and_replay_cannot_send():
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    timer = dict(pool.timers[10])

    async def sent(*args, **kwargs):
        assert pool.events == ["begin", "claim", "insert", "commit"]
        assert next(iter(pool.timers.values()))["expires"] == NOW + datetime.timedelta(days=1)

    channel.send.side_effect = sent
    await fire(cog, reminder, pool, dispatched)
    await reminder._deliver_at_most_once(timer)
    assert not dispatched
    channel.send.assert_awaited_once()


async def test_reschedule_survives_a_crash_after_claim():
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    old = dict(pool.timers[10])
    await reminder._deliver_at_most_once(old)
    # Drop the unhandled event, then construct a fresh engine on the durable DB.
    cog, reminder, channel, dispatched = engine(pool)
    await reminder._deliver_at_most_once(old)
    assert not dispatched
    assert len(pool.timers) == 1
    assert next(iter(pool.timers.values()))["expires"] > NOW
    channel.send.assert_not_awaited()


async def test_failed_reschedule_rolls_back_claim():
    pool = Pool(announcement())
    pool.fail_insert = True
    cog, reminder, channel, dispatched = engine(pool)
    with pytest.raises(RuntimeError, match="insert failed"):
        await reminder._deliver_at_most_once(dict(pool.timers[10]))
    assert 10 in pool.timers and not dispatched
    assert pool.events[-1] == "rollback"


@pytest.mark.parametrize("changes", [
    {"timezone": "Not/AZone"},
    {"timezone": "/Not/AZone"},
    {"local_time": datetime.datetime(9999, 12, 31, 9), "next_run_at": datetime.datetime(9999, 12, 31, 9, tzinfo=UTC)},
])
async def test_calendar_error_disables_and_commits_claim(changes, caplog):
    pool = Pool(announcement(**changes))
    cog, reminder, channel, dispatched = engine(pool)
    await reminder._deliver_at_most_once(dict(pool.timers[10]))
    assert not dispatched and not pool.timers
    channel.send.assert_not_awaited()
    assert pool.rows[1]["enabled"] is False
    assert pool.rows[1]["next_run_at"] is None
    assert pool.rows[1]["last_error"] == "calendar-error"
    assert pool.rows[1]["revision"] == 1
    assert pool.events[-1] == "commit"
    assert "ANNOUNCE-DISABLED id=1 reason=calendar-error" in caplog.text


@pytest.mark.parametrize("planned,expected", [
    (None, NOW + datetime.timedelta(days=1)),
    (NOW + datetime.timedelta(days=2), NOW + datetime.timedelta(days=3)),
])
async def test_claim_uses_planned_boundary_or_falls_back_to_timer(planned, expected):
    pool = Pool(announcement(next_run_at=planned))
    pool.timers[10]["expires"] = NOW
    cog, reminder, channel, dispatched = engine(pool)
    claimed = await cog.store.claim_and_reschedule(dict(pool.timers[10]), now=NOW)
    assert claimed["extra"]["scheduled_at"] == (planned if planned is not None else NOW)
    assert pool.rows[1]["next_run_at"] == expected
    assert next(iter(pool.timers.values()))["expires"] == expected


@pytest.mark.parametrize("recurrence", ["daily", "none"])
async def test_late_occurrence_skips_and_fast_forwards(recurrence, caplog):
    pool = Pool(announcement(
        recurrence=recurrence, local_time=local("2000-01-01T09:00"),
        next_run_at=NOW - datetime.timedelta(hours=1, seconds=1),
    ))
    cog, reminder, channel, dispatched = engine(pool)
    with caplog.at_level("INFO"):
        await fire(cog, reminder, pool, dispatched)
    channel.send.assert_not_awaited()
    assert caplog.text.count("ANNOUNCE-SKIP-LATE") == 1
    assert len(pool.timers) == (0 if recurrence == "none" else 1)
    if recurrence == "none":
        assert pool.rows[1]["enabled"] is False
        assert "ANNOUNCE-DISABLED" in caplog.text
    else:
        assert next(iter(pool.timers.values()))["expires"] == NOW + datetime.timedelta(days=1)


async def test_one_hour_late_is_still_delivered():
    pool = Pool(announcement(next_run_at=NOW - datetime.timedelta(hours=1)))
    cog, reminder, channel, dispatched = engine(pool)
    await fire(cog, reminder, pool, dispatched)
    channel.send.assert_awaited_once()


async def test_postponed_one_shot_uses_planned_time_for_late_cutoff(caplog):
    planned = NOW - datetime.timedelta(hours=2)
    pool = Pool(announcement(
        recurrence="none", local_time=planned.replace(tzinfo=None), next_run_at=planned,
    ))
    # The unloaded engine rewrites only expires to a recent retry time.
    pool.timers[10]["expires"] = NOW
    cog, reminder, channel, dispatched = engine(pool)
    with caplog.at_level("INFO"):
        await fire(cog, reminder, pool, dispatched)
    channel.send.assert_not_awaited()
    assert not pool.timers and pool.rows[1]["enabled"] is False
    assert "ANNOUNCE-SKIP-LATE id=1" in caplog.text


@pytest.mark.parametrize("failure", ["gone", "forbidden", "notfound", "permissions", "embed_links", "wrong_guild", "wrong_type"])
async def test_three_failures_disable_and_remove_next_timer(failure, caplog):
    pool = Pool(announcement(embed={"title": "Hello"}))
    cog, reminder, channel, dispatched = engine(pool)
    if failure == "gone":
        cog.bot.get_guild(3).get_channel = lambda cid: None
        cog.bot.fetch_channel.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
    elif failure in ("forbidden", "notfound"):
        kind = discord.Forbidden if failure == "forbidden" else discord.NotFound
        status = 403 if failure == "forbidden" else 404
        channel.send.side_effect = kind(SimpleNamespace(status=status, reason=failure), "private message")
    elif failure == "permissions":
        channel.permissions_for = lambda me: discord.Permissions.none()
    elif failure == "embed_links":
        channel.permissions_for = lambda me: discord.Permissions(view_channel=True, send_messages=True)
    elif failure == "wrong_guild":
        channel.guild.id = 100
    else:
        channel.type = discord.ChannelType.voice
    for count in range(1, 4):
        # Move the pending occurrence to the test clock, as if the next day arrived.
        next(iter(pool.timers.values()))["expires"] = NOW
        pool.rows[1]["next_run_at"] = NOW
        await fire(cog, reminder, pool, dispatched)
        assert pool.rows[1]["failures"] == count
    assert not pool.rows[1]["enabled"] and not pool.timers
    assert pool.rows[1]["last_error"] == {
        "gone": "http_notfound", "forbidden": "http_forbidden", "notfound": "http_notfound",
        "permissions": "missing_permissions", "embed_links": "missing_embed_links",
        "wrong_guild": "not_text_channel", "wrong_type": "not_text_channel",
    }[failure]
    assert caplog.text.count("ANNOUNCE-FAIL") == 3
    assert caplog.text.count("ANNOUNCE-DISABLED") == 1
    assert "private message" not in caplog.text


async def test_success_resets_failure_streak_and_logs(caplog):
    pool = Pool(announcement(failures=2, last_error="Forbidden", role_id=42))
    cog, reminder, channel, dispatched = engine(pool)
    with caplog.at_level("INFO"):
        await fire(cog, reminder, pool, dispatched)
    assert pool.rows[1]["failures"] == 0 and pool.rows[1]["last_error"] is None
    assert "ANNOUNCE-SENT" in caplog.text
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert mentions.replied_user is False
    assert mentions.to_dict() == {"roles": [42], "parse": []}


async def test_create_list_get_and_cap():
    pool = Pool()
    cog, reminder, channel, dispatched = engine(pool)
    row = await cog.store.create(3, 9, 5, local_time=local("2026-01-02T09:00"), timezone="Europe/Paris", content="Hi")
    assert row["next_run_at"] == at("2026-01-02T08:00")
    assert await cog.store.get(3, row["id"]) == row
    assert await cog.store.get(99, row["id"]) is None
    assert await cog.store.list_for_guild(3) == [row]
    assert len(pool.timers) == 1 and reminder._have_data.is_set()
    pool.rows = {i: announcement(id=i) for i in range(1, 51)}
    with pytest.raises(ValueError, match="ceiling"):
        await cog.store.create(3, 9, 5, local_time=local("2026-01-02T09:00"), timezone="UTC", content="Hi")
    assert len(pool.timers) == 1


async def test_invalid_create_does_not_issue_sql():
    pool = Pool()
    cog, reminder, channel, dispatched = engine(pool)
    for values in ({"timezone": "bad"}, {"content": ""}, {"role_id": 42, "content": "x" * 2000}):
        with pytest.raises(ValueError):
            await cog.store.create(3, 9, 5, local_time=local("2026-01-02T09:00"), **{"timezone": "UTC", "content": "hi", **values})
    assert pool.queries == []


async def test_update_replaces_timer_and_old_dispatch_loses_claim():
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    old = dict(pool.timers[10])
    edited = await cog.store.update(3, 1, local_time=local("2026-01-02T12:00"), content="new")
    assert edited["revision"] == 2
    assert len(pool.timers) == 1 and 10 not in pool.timers
    assert next(iter(pool.timers.values()))["expires"] == at("2026-01-02T12:00")
    await reminder._deliver_at_most_once(old)
    assert not dispatched
    assert any("FOR UPDATE" in q for q, _ in pool.queries)


async def test_enable_disable_delete_sync_and_guild_scoping():
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    assert await cog.store.update(99, 1, content="intruder") is None
    assert not await cog.store.delete(99, 1)
    await cog.store.set_enabled(3, 1, False)
    assert not pool.timers and not pool.rows[1]["enabled"]
    assert pool.rows[1]["next_run_at"] is None
    await cog.store.set_enabled(3, 1, True)
    assert len(pool.timers) == 1
    old = dict(next(iter(pool.timers.values())))
    assert await cog.store.delete(3, 1)
    assert not await cog.store.delete(3, 1)
    await reminder._deliver_at_most_once(old)
    assert not pool.rows and not pool.timers and not dispatched


@pytest.mark.parametrize("edit_first", [False, True])
async def test_concurrent_edit_and_dispatch_leave_one_timer(edit_first):
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    old = dict(pool.timers[10])
    operations = [cog.store.update(3, 1, content="edited"), reminder._deliver_at_most_once(old)]
    if not edit_first:
        operations.reverse()
    await asyncio.gather(*operations)
    assert len(pool.timers) == 1 and pool.rows[1]["content"] == "edited"
    assert len(dispatched) == (0 if edit_first else 1)
    if dispatched:
        # An already claimed delivery may finish, but cannot disable the edit.
        snapshot = dispatched[0][1]["announcement"]
        assert await cog.store.record_result(snapshot, "Forbidden") is None
        assert pool.rows[1]["failures"] == 0


async def test_delete_after_claim_cancels_successor_and_result_cannot_recreate():
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    await reminder._deliver_at_most_once(dict(pool.timers[10]))
    snapshot = dispatched[0][1]["announcement"]
    assert await cog.store.delete(3, 1)
    assert await cog.store.record_result(snapshot, "Forbidden") is None
    assert not pool.rows and not pool.timers


async def test_unloaded_engine_postpones_instead_of_stalling_the_queue(caplog):
    # Raising would retry this same earliest row forever and stall reminders.
    pool = SimpleNamespace(execute=AsyncMock())
    reminder = object.__new__(Reminder)
    reminder.bot = SimpleNamespace(db_pool=pool, get_cog=lambda name: None)
    with caplog.at_level("WARNING"):
        await reminder._deliver_at_most_once(
            {"id": 10, "event": "announcement", "announcement_id": 1}
        )
    query, timer_id = pool.execute.await_args.args
    assert query.startswith("UPDATE timers SET expires") and timer_id == 10
    assert "ANNOUNCE-POSTPONED timer=10" in caplog.text


def test_everyone_role_is_never_a_valid_ping():
    with pytest.raises(ValueError, match="role_id"):
        validate(announcement(role_id=3))
    assert validate(announcement(role_id=4))["role_id"] == 4


def _missing_schema_guards(ddl):
    required = (
        "CREATE TABLE IF NOT EXISTS scheduled_announcements",
        "char_length(content) <= 2000", "octet_length(embed::text) <= 16384",
        "'none', 'daily', 'weekly', 'monthly'",
        "NULLIF(btrim(content), '') IS NOT NULL OR embed IS NOT NULL",
        "REFERENCES scheduled_announcements(id) ON DELETE CASCADE",
        "CREATE UNIQUE INDEX IF NOT EXISTS timers_announcement_idx",
        "CREATE INDEX IF NOT EXISTS scheduled_announcements_guild_idx",
    )
    return [guard for guard in required if guard not in ddl]


def test_schema_constraints_and_timer_cascade_have_negative_control():
    ddl = (Path(__file__).resolve().parents[2] / "schema.sql").read_text()
    assert not _missing_schema_guards(ddl)
    for guard in ("char_length(content) <= 2000", "REFERENCES scheduled_announcements(id) ON DELETE CASCADE"):
        assert guard in _missing_schema_guards(ddl.replace(guard, "synthetic missing constraint"))


def _missing_data_coverage(deletes, export_source, guild_queries):
    missing = []
    if deletes.get("scheduled_announcements") != "DELETE FROM scheduled_announcements WHERE author_id = $1":
        missing.append("erasure")
    if "FROM scheduled_announcements WHERE author_id = $1" not in export_source:
        missing.append("export")
    if guild_queries.get("scheduled_announcements") != "DELETE FROM scheduled_announcements WHERE guild_id = $1":
        missing.append("retention")
    return missing


def test_retention_and_privacy_coverage_have_negative_controls():
    import inspect

    deletes = dict(privacy.USER_DELETE_QUERIES)
    guilds = dict(retention.GUILD_DELETE_QUERIES)
    source = inspect.getsource(privacy.collect_user_export)
    assert not _missing_data_coverage(deletes, source, guilds)
    assert "erasure" in _missing_data_coverage({}, source, guilds)
    assert "export" in _missing_data_coverage(deletes, "synthetic empty export", guilds)
    assert "retention" in _missing_data_coverage(deletes, source, {})
    assert "SELECT guild_id FROM scheduled_announcements" in retention.STORED_GUILD_IDS_QUERY


async def test_authored_announcements_export_decodes_embed():
    class ExportPool:
        async def fetchval(self, query, *args):
            return None

        async def fetchrow(self, query, *args):
            return None

        async def fetch(self, query, *args):
            if "FROM scheduled_announcements" in query:
                assert "WHERE author_id = $1" in query and args == (5,)
                return [announcement(embed='{"title":"Hello"}')]
            return []

    data, _avatars = await privacy.collect_user_export(ExportPool(), 5)
    assert data["scheduled_announcements"][0]["embed"] == {"title": "Hello"}


def test_local_anchor_remains_nine_across_both_dst_changes():
    zone = ZoneInfo("Europe/Paris")
    for month, day in ((3, 28), (10, 24)):
        anchor = datetime.datetime(2026, month, day, 9)
        before = anchor.replace(tzinfo=zone).astimezone(UTC)
        after = next_occurrence(anchor, "Europe/Paris", "daily", before)
        assert after.astimezone(zone).hour == 9


async def test_one_shot_success_disables_without_a_successor():
    pool = Pool(announcement(recurrence="none"))
    cog, reminder, channel, dispatched = engine(pool)
    await fire(cog, reminder, pool, dispatched)
    channel.send.assert_awaited_once()
    assert not pool.timers
    assert pool.rows[1]["enabled"] is False
    assert pool.rows[1]["next_run_at"] is None


@pytest.mark.parametrize("delete_first", [False, True])
async def test_concurrent_delete_and_dispatch_cannot_resurrect(delete_first):
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    old = dict(pool.timers[10])
    operations = [cog.store.delete(3, 1), reminder._deliver_at_most_once(old)]
    if not delete_first:
        operations.reverse()
    await asyncio.gather(*operations)
    assert not pool.rows and not pool.timers
    assert len(dispatched) == (0 if delete_first else 1)


async def test_failed_update_keeps_old_timer_and_unknown_fields_are_rejected():
    pool = Pool(announcement())
    cog, reminder, channel, dispatched = engine(pool)
    original = copy.deepcopy(pool.timers)
    with pytest.raises(ValueError, match="Unknown"):
        await cog.store.update(3, 1, guild_id_override=99)
    with pytest.raises(ValueError, match="IANA"):
        await cog.store.update(3, 1, timezone="invalid")
    assert pool.timers == original
    pool.fail_insert = True
    with pytest.raises(RuntimeError, match="insert failed"):
        await cog.store.update(3, 1, content="edited")
    assert pool.timers == original
    assert pool.rows[1]["content"] == "hello" and pool.rows[1]["revision"] == 1


async def test_reenabling_an_expired_one_shot_requires_a_new_time():
    pool = Pool(announcement(recurrence="none", enabled=False, next_run_at=None))
    pool.timers.clear()
    cog, reminder, channel, dispatched = engine(pool)
    with pytest.raises(ValueError, match="future"):
        await cog.store.set_enabled(3, 1, True)
    assert not pool.timers and not pool.rows[1]["enabled"]
    await cog.store.update(3, 1, enabled=True, local_time=local("2026-01-02T09:00"))
    assert len(pool.timers) == 1


async def test_unmanaged_announcement_event_is_ignored_by_engine():
    pool = Pool()
    cog, reminder, channel, dispatched = engine(pool)
    await cog.on_announcement_timer_complete({"legacy_event": True})
    channel.send.assert_not_awaited()
    assert not pool.queries
