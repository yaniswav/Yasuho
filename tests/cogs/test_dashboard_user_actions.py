"""Unit tests for the USER-scoped dashboard executors (``mydata_export``).

Two different things are guarded here:

* the executor's own contract - it DMs the archive, reports ``cooldown`` with an
  exact ``retryAfter``, tells ``dm_closed`` apart from ``failed``, and never
  builds an archive it is not allowed to send;
* the fact that the Discord command ``?mydata export`` and this executor share
  ONE rate limiter. That is tested in BOTH directions against a stateful fake
  that implements the real claim statement's semantics (an atomic test-and-set
  on one stored timestamp per user), and by driving the command's REAL callback
  - so "shared" is demonstrated, not asserted.

No network and no DB: the pool models the claim, the archive build is stubbed at
the ``tools.privacy`` seams, and the user is a recorder.
"""

from __future__ import annotations

import json
import math
import types

import discord
import pytest
from discord.ext import commands

from cogs.community import usersettings
from cogs.system import dashboard_user_actions as dua
from tools import privacy, rendering

# ---------------------------------------------------------------------------
# A pool that really implements the shared cooldown claim.
# ---------------------------------------------------------------------------


class SlotPool:
    """In-memory ``mydata_export_cooldown`` with the claim's exact semantics.

    ``fetchrow`` recognises the claim statement and behaves like
    ``INSERT ... ON CONFLICT DO UPDATE ... WHERE last_export_at <= now() - window``
    over one timestamp PER USER (the table's primary key): the slot is granted
    only once the window has elapsed, and a refusal reports the seconds left,
    rounded UP. ``now`` is virtual, so a test moves time without sleeping.
    """

    def __init__(self, now=0.0):
        self.now = now
        self.slots = {}
        self.queries = []

    def advance(self, seconds):
        self.now += seconds

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "mydata_export_cooldown" in query and "INSERT" in query:
            user_id, window = args
            prior = self.slots.get(user_id)
            if prior is None or prior <= self.now - window:
                self.slots[user_id] = self.now
                return {"granted": True, "retry_after": 0}
            # CEIL(): a caller told to wait N seconds must not still be refused
            # after waiting exactly N.
            remaining = math.ceil((prior + window) - self.now)
            return {"granted": False, "retry_after": remaining}
        raise AssertionError("unexpected fetchrow: %r" % query)  # pragma: no cover


class FakeUser:
    def __init__(self, user_id=4242, raise_on_send=None):
        self.id = user_id
        self.raise_on_send = raise_on_send
        self.sent = []
        self.contents = []

    async def send(self, *args, **kwargs):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(kwargs.get("file"))
        self.contents.append(kwargs.get("content"))


class FakeBot:
    """A bot whose user cache holds ``user``; ``fetch_user`` is the fallback.

    Both lookups HONOUR the id they are given (they return the user only when it
    matches), so "collected for A, delivered to B" cannot pass here by the fake
    handing the same object back whatever it is asked for.
    """

    def __init__(self, pool, user=None, cached=True, fetch_error=None):
        self.db_pool = pool
        self._user = user
        self._cached = cached
        self._fetch_error = fetch_error
        self.fetched = []

    def _match(self, user_id):
        if self._user is None or self._user.id != user_id:
            return None
        return self._user

    def get_user(self, user_id):
        return self._match(user_id) if self._cached else None

    async def fetch_user(self, user_id):
        self.fetched.append(user_id)
        if self._fetch_error is not None:
            raise self._fetch_error
        user = self._match(user_id)
        if user is None:
            raise LookupError("unknown user %s" % user_id)
        return user


def _forbidden():
    """A real ``discord.Forbidden`` without touching the network."""
    response = types.SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, "cannot send messages to this user")


@pytest.fixture
def stub_export(monkeypatch):
    """Stub the two personal-data seams and count their runs.

    Counting is the point: the ordering guarantee ("never build the archive
    before the cooldown gate passes") is only checkable by observing that the
    collector did NOT run.
    """
    calls = {"collect": 0, "build": 0, "users": []}

    async def _collect(pool, user_id):
        calls["collect"] += 1
        calls["users"].append(user_id)
        return {"user_id": user_id}, []

    def _build(data, avatar_rows, **kwargs):
        calls["build"] += 1
        return [("yasuho-data-1-of-1.zip", b"zip-bytes")]

    async def _run_image_job(bot, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(privacy, "collect_user_export", _collect)
    monkeypatch.setattr(privacy, "build_export_archives", _build)
    monkeypatch.setattr(rendering, "run_image_job", _run_image_job)
    return calls


@pytest.fixture(autouse=True)
def _stub_locale(monkeypatch):
    """The DM note is rendered in the RECIPIENT's locale, which is read from
    their settings; the pools here model the cooldown table alone."""

    async def _resolve(bot, *, user_id, **kwargs):
        return "en"

    monkeypatch.setattr(dua.i18n, "resolve_locale", _resolve)


@pytest.fixture(autouse=True)
def _no_real_files(monkeypatch):
    """``discord.File`` wants a real file-like; the archives here are bytes."""
    monkeypatch.setattr(
        discord, "File", lambda fp, filename=None: ("file", filename, fp)
    )


def _ran(calls):
    return {"collect": calls["collect"], "build": calls["build"]}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_export_is_delivered_by_dm(stub_export):
    user = FakeUser()
    bot = FakeBot(SlotPool(), user=user)

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result == {"ok": True, "delivered": "dm"}
    assert [name for _kind, name, _fp in user.sent] == ["yasuho-data-1-of-1.zip"]
    assert _ran(stub_export) == {"collect": 1, "build": 1}


async def test_every_archive_part_is_sent(monkeypatch, stub_export):
    def _build_three(data, avatar_rows, **kwargs):
        return [(f"part-{index}.zip", b"x") for index in (1, 2, 3)]

    monkeypatch.setattr(privacy, "build_export_archives", _build_three)
    user = FakeUser()
    bot = FakeBot(SlotPool(), user=user)

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result == {"ok": True, "delivered": "dm"}
    assert [name for _kind, name, _fp in user.sent] == [
        "part-1.zip",
        "part-2.zip",
        "part-3.zip",
    ]


async def test_the_first_part_carries_a_line_of_context(monkeypatch, stub_export):
    """A dashboard-triggered DM is otherwise an unexplained file drop from a bot.
    The note rides the FIRST part only - repeating it on every part of a
    multi-part archive would be noise."""

    def _build_two(data, avatar_rows, **kwargs):
        return [(f"part-{index}.zip", b"x") for index in (1, 2)]

    monkeypatch.setattr(privacy, "build_export_archives", _build_two)
    user = FakeUser()
    bot = FakeBot(SlotPool(), user=user)

    await dua._exec_mydata_export(bot, 4242, {})

    assert user.contents[0] is not None
    assert "export" in user.contents[0]
    assert user.contents[1] is None


async def test_a_locale_lookup_failure_still_delivers_the_export(
    monkeypatch, stub_export
):
    """The note is a courtesy; resolving it is not allowed to cost somebody the
    export they are owed."""

    async def _explode(bot, *, user_id, **kwargs):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(dua.i18n, "resolve_locale", _explode)
    user = FakeUser()
    bot = FakeBot(SlotPool(), user=user)

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result == {"ok": True, "delivered": "dm"}
    assert user.contents[0] is not None


async def test_the_export_is_collected_for_the_claimed_user_only(stub_export):
    """The scope id is authoritative: the payload holds nothing that could point
    the export at somebody else, and the collector runs on THAT id."""
    bot = FakeBot(SlotPool(), user=FakeUser())

    await dua._exec_mydata_export(bot, 4242, {"user_id": "1", "channel_id": "2"})

    assert stub_export["users"] == [4242]


async def test_a_user_missing_from_the_cache_is_fetched(stub_export):
    user = FakeUser()
    bot = FakeBot(SlotPool(), user=user, cached=False)

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert bot.fetched == [4242]
    assert result == {"ok": True, "delivered": "dm"}


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


async def test_second_export_inside_the_window_is_refused_with_retry_after(
    stub_export,
):
    pool = SlotPool()
    bot = FakeBot(pool, user=FakeUser())

    first = await dua._exec_mydata_export(bot, 4242, {})
    pool.advance(600)  # ten minutes into the hour
    second = await dua._exec_mydata_export(bot, 4242, {})

    assert first["ok"] is True
    assert second == {
        "ok": False,
        "reason": "cooldown",
        "retryAfter": privacy.EXPORT_COOLDOWN_SECONDS - 600,
    }


async def test_the_archive_is_not_built_when_the_cooldown_refuses(stub_export):
    """Ordering guarantee: the gate runs BEFORE anything is read or packed, so a
    refused request costs one statement, not a full archive."""
    pool = SlotPool()
    user = FakeUser()
    bot = FakeBot(pool, user=user)

    await dua._exec_mydata_export(bot, 4242, {})
    assert _ran(stub_export) == {"collect": 1, "build": 1}

    pool.advance(1)
    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result["reason"] == "cooldown"
    # Nothing collected, nothing packed, nothing sent for the refused run.
    assert _ran(stub_export) == {"collect": 1, "build": 1}
    assert len(user.sent) == 1


async def test_the_slot_is_free_again_once_the_window_elapses(stub_export):
    pool = SlotPool()
    bot = FakeBot(pool, user=FakeUser())

    await dua._exec_mydata_export(bot, 4242, {})
    pool.advance(privacy.EXPORT_COOLDOWN_SECONDS)
    second = await dua._exec_mydata_export(bot, 4242, {})

    assert second == {"ok": True, "delivered": "dm"}


async def test_the_cooldown_is_per_user(stub_export):
    """One user's export must never rate-limit another's - the table is keyed by
    user_id, and the fake keeps one slot per key exactly like the PRIMARY KEY."""
    pool = SlotPool()
    bot_one = FakeBot(pool, user=FakeUser(1))
    bot_two = FakeBot(pool, user=FakeUser(2))

    assert (await dua._exec_mydata_export(bot_one, 1, {}))["ok"] is True
    assert (await dua._exec_mydata_export(bot_two, 2, {}))["ok"] is True
    assert sorted(pool.slots) == [1, 2]


# ---------------------------------------------------------------------------
# Failure shapes
# ---------------------------------------------------------------------------


async def test_closed_dms_are_reported_as_dm_closed(stub_export):
    bot = FakeBot(SlotPool(), user=FakeUser(raise_on_send=_forbidden()))

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result == {"ok": False, "reason": "dm_closed"}


async def test_a_non_forbidden_delivery_error_is_failed_not_dm_closed(stub_export):
    """dm_closed is the failure the user can fix; nothing else may masquerade as
    it, or the dashboard tells them to open their DMs for nothing."""
    bot = FakeBot(SlotPool(), user=FakeUser(raise_on_send=RuntimeError("hiccup")))

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result == {"ok": False, "reason": "failed"}


async def test_a_build_failure_is_reported_as_failed(monkeypatch, stub_export):
    async def _boom(pool, user_id):
        raise RuntimeError("postgres://user:hunter2@localhost/db is down")

    monkeypatch.setattr(privacy, "collect_user_export", _boom)
    user = FakeUser()
    bot = FakeBot(SlotPool(), user=user)

    result = await dua._exec_mydata_export(bot, 4242, {})

    # A fixed code, never the exception text (which can carry a DSN).
    assert result == {"ok": False, "reason": "failed"}
    assert "hunter2" not in repr(result)
    assert user.sent == []


async def test_an_unresolvable_user_is_failed_without_burning_the_slot(stub_export):
    """The user is resolved BEFORE the slot is claimed, so an action for someone
    the bot cannot reach never costs that person their hourly export."""
    pool = SlotPool()
    bot = FakeBot(pool, user=None, cached=False, fetch_error=RuntimeError("gone"))

    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result == {"ok": False, "reason": "failed"}
    assert pool.slots == {}  # the slot was never taken
    assert _ran(stub_export) == {"collect": 0, "build": 0}


# ---------------------------------------------------------------------------
# The shared limiter, in BOTH directions, driving the real command callback.
# ---------------------------------------------------------------------------


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    """Just enough Context for the prefix path of ``?mydata export``."""

    def __init__(self, author, bot):
        self.author = author
        self.bot = bot
        self.interaction = None
        self.sent = []

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


async def _run_command(bot, user):
    """Invoke the REAL command callback, unwrapped by discord.py."""
    cog = usersettings.UserSettings(bot)
    ctx = FakeCtx(author=user, bot=bot)
    await usersettings.UserSettings.mydata_export.callback(cog, ctx)
    return ctx


async def test_the_command_locks_the_dashboard_executor_out(stub_export):
    """Direction 1: exporting from Discord cools the dashboard action down."""
    pool = SlotPool()
    user = FakeUser()
    bot = FakeBot(pool, user=user)

    await _run_command(bot, user)
    assert len(user.sent) == 1  # the command DM'd the archive

    pool.advance(60)
    result = await dua._exec_mydata_export(bot, 4242, {})

    assert result["reason"] == "cooldown"
    assert result["retryAfter"] == privacy.EXPORT_COOLDOWN_SECONDS - 60


async def test_the_dashboard_executor_locks_the_command_out(stub_export):
    """Direction 2: exporting from the dashboard cools ``?mydata export`` down -
    and with the same UX as before, because the command raises the very
    exception discord.py's own cooldown decorator used to raise."""
    pool = SlotPool()
    user = FakeUser()
    bot = FakeBot(pool, user=user)

    assert (await dua._exec_mydata_export(bot, 4242, {}))["ok"] is True
    pool.advance(120)

    with pytest.raises(commands.CommandOnCooldown) as excinfo:
        await _run_command(bot, user)

    assert excinfo.value.retry_after == privacy.EXPORT_COOLDOWN_SECONDS - 120
    assert excinfo.value.cooldown.per == privacy.EXPORT_COOLDOWN_SECONDS
    assert excinfo.value.type is commands.BucketType.user
    # Refused before anything was built or sent a second time.
    assert _ran(stub_export) == {"collect": 1, "build": 1}
    assert len(user.sent) == 1


async def test_the_command_claims_before_it_builds(stub_export):
    """The command's own ordering guarantee, the twin of the executor's."""
    pool = SlotPool()
    user = FakeUser()
    bot = FakeBot(pool, user=user)

    await _run_command(bot, user)

    claim_index = next(
        index
        for index, (query, _args) in enumerate(pool.queries)
        if "mydata_export_cooldown" in query
    )
    assert claim_index == 0  # nothing hit the DB before the gate
    assert _ran(stub_export) == {"collect": 1, "build": 1}


def test_the_command_no_longer_carries_a_process_local_bucket():
    """The in-process bucket is exactly what the DB clock replaces; leaving it
    would put a second, restart-resettable limiter back in front of the shared
    one."""
    assert not usersettings.UserSettings.mydata_export._buckets.valid
    # The sibling destructive commands keep theirs: they are cheap and local.
    assert usersettings.UserSettings.mydata_deleteprofile._buckets.valid
    assert usersettings.UserSettings.mydata_deleteavatars._buckets.valid


# ---------------------------------------------------------------------------
# Full flow through the real queue: a mydata_export row, no kind monkeypatched.
# ---------------------------------------------------------------------------


class QueuePool(SlotPool):
    """SlotPool + the ``dashboard_actions`` claim/finish statements.

    Enough of both tables for one action to travel the REAL path: claimed by
    ``dashboard_actions.handle_action``, dispatched through the shipped registry
    and ``_USER_KINDS`` (nothing monkeypatched), finalised back into the row.
    """

    def __init__(self):
        super().__init__()
        self.rows = {}

    def add(self, action_id, kind, *, guild_id=None, user_id=None):
        self.rows[action_id] = {
            "guild_id": guild_id,
            "user_id": user_id,
            "kind": kind,
            "payload": {},
            "status": "pending",
            "result": None,
        }

    async def fetchrow(self, query, *args):
        if "WHERE id = $1 AND status = 'pending'" in query:
            row = self.rows.get(args[0])
            if row is None or row["status"] != "pending":
                return None
            row["status"] = "running"
            return {
                "guild_id": row["guild_id"],
                "user_id": row["user_id"],
                "kind": row["kind"],
                "payload": row["payload"],
            }
        return await super().fetchrow(query, *args)

    async def execute(self, query, *args):
        if "WHERE id = $3" in query:  # finish
            status, result_json, action_id = args
            row = self.rows[action_id]
            row["status"] = status
            row["result"] = json.loads(result_json)
            return "UPDATE 1"
        raise AssertionError("unexpected execute: %r" % query)  # pragma: no cover


async def test_a_mydata_export_row_travels_the_real_queue_to_the_dm(stub_export):
    from cogs.system import dashboard_actions

    pool = QueuePool()
    pool.add(1, "mydata_export", user_id=4242)
    user = FakeUser()
    bot = FakeBot(pool, user=user)

    assert await dashboard_actions.handle_action(bot, 1) == "done"

    assert pool.rows[1]["result"] == {"ok": True, "delivered": "dm"}
    assert stub_export["users"] == [4242]  # dispatched with the USER id
    assert len(user.sent) == 1


async def test_a_mydata_export_row_written_with_a_guild_id_is_refused(stub_export):
    """The scope guard, exercised on the SHIPPED kind rather than a fake one:
    the guild id must never stand in for a user id here."""
    from cogs.system import dashboard_actions

    pool = QueuePool()
    pool.add(1, "mydata_export", guild_id=999)
    user = FakeUser()
    bot = FakeBot(pool, user=user)

    assert await dashboard_actions.handle_action(bot, 1) == "failed"

    assert pool.rows[1]["result"] == {"ok": False, "error": "bad_scope"}
    assert _ran(stub_export) == {"collect": 0, "build": 0}
    assert user.sent == []
    assert pool.slots == {}  # not even the cooldown slot was touched
