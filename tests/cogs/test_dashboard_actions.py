"""Unit tests for the dashboard->bot action queue (``cogs.system.dashboard_actions``).

These exercise the PURE queue logic - the part that turns a claimed
``dashboard_actions`` row into an executor run and a written-back result - with
in-memory stand-ins for the only boundaries: a stateful fake pool that models
the atomic ``UPDATE ... WHERE status='pending' RETURNING`` claim (so idempotence
is real, not asserted), a fake bot, and (for the verify executor) fake Discord
guild/channel objects. The network / LISTEN connection and the reconnect
supervisor are NOT exercised here (they touch a real socket); only the claim ->
dispatch -> finish path and the boot reconciliation are, which is where all the
correctness + security logic lives.

Runs on the 3.7 box against discord.py 1.5.1: the cog module imports cleanly
there (it imports ``VerifyView`` LAZILY, so it never pulls in the 2.x-only
``discord.ui`` at import time), and these tests monkeypatch that lazy seam +
``discord.TextChannel`` so nothing here needs the 2.x UI stack either.
"""

from __future__ import annotations

import json
import types

import discord
import pytest

from cogs.system import dashboard_actions
from tools import settings


# ---------------------------------------------------------------------------
# Stateful fake pool: models the atomic claim + finish + reconcile UPDATEs.
# ---------------------------------------------------------------------------


class ActionsPool:
    """In-memory ``dashboard_actions`` table with claim/finish/reconcile semantics.

    ``fetchrow`` implements the atomic single-flight claim (a row can only be
    claimed while ``status='pending'``; the claim flips it to ``running``), so a
    second claim of the same id returns ``None`` exactly as Postgres would - this
    is what makes the idempotence test meaningful rather than mocked.
    """

    def __init__(self):
        self.calls = []
        self.rows = {}  # id -> dict(guild_id, kind, payload, status, result, stale)

    def add(self, action_id, guild_id, kind, payload, status="pending", stale=False):
        self.rows[action_id] = {
            "guild_id": guild_id,
            "kind": kind,
            "payload": payload,
            "status": status,
            "result": None,
            "stale": stale,
        }

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "WHERE id = $1 AND status = 'pending'" in query:  # the claim
            action_id = args[0]
            row = self.rows.get(action_id)
            if row is None or row["status"] != "pending":
                return None
            row["status"] = "running"
            return {
                "guild_id": row["guild_id"],
                "kind": row["kind"],
                "payload": row["payload"],
            }
        raise AssertionError("unexpected fetchrow: %r" % query)  # pragma: no cover

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if "WHERE id = $3" in query:  # finish: SET status=$1, result=$2 WHERE id=$3
            status, result_json, action_id = args
            row = self.rows.get(action_id)
            if row is not None:
                row["status"] = status
                row["result"] = json.loads(result_json)
            return "UPDATE 1"
        if "created_at < now()" in query:  # reconcile: expire the too-old
            (result_json,) = args
            for row in self.rows.values():
                if row["status"] in ("pending", "running") and row["stale"]:
                    row["status"] = "failed"
                    row["result"] = json.loads(result_json)
            return "UPDATE"
        if "SET status = 'pending'" in query and "WHERE status = 'running'" in query:
            for row in self.rows.values():  # reconcile: reset orphaned running
                if row["status"] == "running":
                    row["status"] = "pending"
            return "UPDATE"
        raise AssertionError("unexpected execute: %r" % query)  # pragma: no cover

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "WHERE status = 'pending' ORDER BY id" in query:
            return [
                {"id": aid}
                for aid in sorted(self.rows)
                if self.rows[aid]["status"] == "pending"
            ]
        raise AssertionError("unexpected fetch: %r" % query)  # pragma: no cover

    async def fetchval(self, query, *args):
        # Only reached via settings.get_guild inside resolve_guild_locale; an
        # unconfigured guild reads no locale row.
        self.calls.append(("fetchval", query, args))
        return None


class FakeBot:
    def __init__(self, pool, guilds=None):
        self.db_pool = pool
        self._guilds = guilds or {}

    def get_guild(self, gid):
        return self._guilds.get(gid)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The tools.settings LRU is process-global; keep it from leaking across tests."""
    settings._cache.clear()
    yield
    settings._cache.clear()


# ---------------------------------------------------------------------------
# _parse_action_id: defensive parsing of the NOTIFY payload (a bare id string).
# ---------------------------------------------------------------------------


def test_parse_action_id_accepts_positive_decimal_string():
    assert dashboard_actions._parse_action_id("42") == 42


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "abc",
        "0",  # not positive
        "-5",  # not positive
        "1.5",  # not an int
        None,
        123,  # not a string
        "  ",
    ],
)
def test_parse_action_id_rejects_bad_payloads(payload):
    assert dashboard_actions._parse_action_id(payload) is None


# ---------------------------------------------------------------------------
# _coerce_payload: JSONB may arrive as a dict OR a JSON string; never raises.
# ---------------------------------------------------------------------------


def test_coerce_payload_passes_through_dict():
    assert dashboard_actions._coerce_payload({"a": 1}) == {"a": 1}


def test_coerce_payload_parses_json_string():
    assert dashboard_actions._coerce_payload('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("raw", ["not json", "[1,2,3]", "42", None, 7])
def test_coerce_payload_falls_back_to_empty_dict(raw):
    assert dashboard_actions._coerce_payload(raw) == {}


# ---------------------------------------------------------------------------
# handle_action: claim -> dispatch -> finish, with a synthetic executor so the
# queue mechanics are tested independently of any Discord fakery.
# ---------------------------------------------------------------------------


def _register(monkeypatch, kind, handler):
    monkeypatch.setitem(dashboard_actions._EXECUTORS, kind, handler)


async def test_handle_action_claims_and_runs_executor(monkeypatch):
    seen = []

    async def _exec(bot, guild_id, payload):
        seen.append((guild_id, payload))
        return {"ok": True, "echo": payload.get("x")}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_kind", payload={"x": "hi"})
    bot = FakeBot(pool)

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    # Executor got the AUTHORITATIVE guild_id from the claimed row + the payload.
    assert seen == [(100, {"x": "hi"})]
    assert pool.rows[1]["status"] == "done"
    assert pool.rows[1]["result"] == {"ok": True, "echo": "hi"}


async def test_handle_action_is_idempotent_second_call_is_noop(monkeypatch):
    """A duplicate notify (or a notify racing reconcile) must NOT re-run."""
    runs = []

    async def _exec(bot, guild_id, payload):
        runs.append(guild_id)
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_kind", payload={})
    bot = FakeBot(pool)

    first = await dashboard_actions.handle_action(bot, 1)
    second = await dashboard_actions.handle_action(bot, 1)

    assert first == "done"
    assert second is None  # already claimed/finished: silent no-op
    assert runs == [100]  # executor ran exactly ONCE


async def test_handle_action_missing_row_is_noop():
    pool = ActionsPool()  # empty table
    bot = FakeBot(pool)
    assert await dashboard_actions.handle_action(bot, 999) is None


async def test_handle_action_unknown_kind_marks_failed():
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="does_not_exist", payload={})
    bot = FakeBot(pool)

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "failed"
    assert pool.rows[1]["status"] == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "unknown_kind"}


async def test_handle_action_executor_exception_marks_failed_without_leaking(monkeypatch):
    async def _boom(bot, guild_id, payload):
        raise RuntimeError("secret connection string leaked here")

    _register(monkeypatch, "test_kind", _boom)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_kind", payload={})
    bot = FakeBot(pool)

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "failed"
    result = pool.rows[1]["result"]
    # A fixed code only - the exception text/stack is NEVER surfaced.
    assert result == {"ok": False, "error": "internal_error"}
    assert "secret" not in json.dumps(result)


async def test_handle_action_validation_failure_is_recorded_as_failed(monkeypatch):
    async def _exec(bot, guild_id, payload):
        return {"ok": False, "error": "channel_not_found"}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_kind", payload={})
    bot = FakeBot(pool)

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "failed"
    # The executor's own error code is preserved for the dashboard to display.
    assert pool.rows[1]["result"] == {"ok": False, "error": "channel_not_found"}


async def test_handle_action_non_dict_result_marked_failed(monkeypatch):
    async def _exec(bot, guild_id, payload):
        return "not a dict"

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_kind", payload={})
    bot = FakeBot(pool)

    status = await dashboard_actions.handle_action(bot, 1)
    assert status == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "internal_error"}


async def test_handle_action_survives_claim_error():
    class BoomPool(ActionsPool):
        async def fetchrow(self, query, *args):
            raise RuntimeError("db down")

    bot = FakeBot(BoomPool())
    # Must not raise: a DB blip can never take down the listener.
    assert await dashboard_actions.handle_action(bot, 1) is None


# ---------------------------------------------------------------------------
# verify_button_post executor: re-validates EVERYTHING against live state.
# ---------------------------------------------------------------------------


class FakePermissions:
    def __init__(self, send_messages):
        self.send_messages = send_messages


class FakeTextChannel:
    def __init__(self, channel_id=555, can_send=True):
        self.id = channel_id
        self._can_send = can_send
        self.sent = []

    def permissions_for(self, member):
        return FakePermissions(self._can_send)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return types.SimpleNamespace(id=999888777666555444)


class FakeVoiceChannel:
    """A non-text channel: exists, but must be rejected by the isinstance gate."""

    def __init__(self, channel_id=555):
        self.id = channel_id

    def permissions_for(self, member):  # pragma: no cover - never reached
        return FakePermissions(True)


class FakeGuild:
    def __init__(self, channels=None, has_me=True, preferred_locale="en"):
        self.id = 100
        self._channels = channels or {}
        self.me = object() if has_me else None
        self.preferred_locale = preferred_locale

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class FakeVerifyView:
    """Stand-in for the persistent VerifyView (avoids importing discord.ui)."""

    instances = 0

    def __init__(self):
        FakeVerifyView.instances += 1


@pytest.fixture
def verify_env(monkeypatch):
    """Patch the lazy VerifyView seam + discord.TextChannel so the executor runs
    without the discord.py-2.x UI stack (absent on the 3.7 box)."""
    FakeVerifyView.instances = 0
    monkeypatch.setattr(dashboard_actions, "_verify_view_cls", lambda: FakeVerifyView)
    monkeypatch.setattr(discord, "TextChannel", FakeTextChannel)
    yield


async def test_verify_button_post_success(verify_env):
    channel = FakeTextChannel(channel_id=555)
    guild = FakeGuild(channels={555: channel})
    bot = FakeBot(ActionsPool(), guilds={100: guild})

    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}
    )

    assert result == {
        "ok": True,
        "channel_id": "555",
        "message_id": "999888777666555444",
    }
    # Posted exactly one message carrying the embed + the persistent view.
    assert len(channel.sent) == 1
    _, kwargs = channel.sent[0]
    assert isinstance(kwargs["embed"], discord.Embed)
    assert isinstance(kwargs["view"], FakeVerifyView)
    assert FakeVerifyView.instances == 1


async def test_verify_button_post_uses_custom_message(verify_env):
    channel = FakeTextChannel(channel_id=555)
    guild = FakeGuild(channels={555: channel})
    bot = FakeBot(ActionsPool(), guilds={100: guild})

    await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555", "message": "Welcome! Tap to verify."}
    )

    _, kwargs = channel.sent[0]
    assert kwargs["embed"].description == "Welcome! Tap to verify."


async def test_verify_button_post_guild_unavailable(verify_env):
    bot = FakeBot(ActionsPool(), guilds={})  # bot is not in guild 100
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}
    )
    assert result == {"ok": False, "error": "guild_unavailable"}


async def test_verify_button_post_channel_not_found(verify_env):
    guild = FakeGuild(channels={})  # channel 555 does not exist
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}
    )
    assert result == {"ok": False, "error": "channel_not_found"}


async def test_verify_button_post_rejects_non_text_channel(verify_env):
    guild = FakeGuild(channels={555: FakeVoiceChannel(555)})
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}
    )
    assert result == {"ok": False, "error": "not_text_channel"}


async def test_verify_button_post_missing_send_permission(verify_env):
    channel = FakeTextChannel(channel_id=555, can_send=False)
    guild = FakeGuild(channels={555: channel})
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}
    )
    assert result == {"ok": False, "error": "missing_send_permission"}
    assert channel.sent == []  # nothing posted


@pytest.mark.parametrize("channel_id", [None, "abc", "", "not-a-number"])
async def test_verify_button_post_bad_channel_id(verify_env, channel_id):
    guild = FakeGuild(channels={})
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    payload = {} if channel_id is None else {"channel_id": channel_id}
    result = await dashboard_actions._exec_verify_button_post(bot, 100, payload)
    assert result == {"ok": False, "error": "bad_channel_id"}


async def test_verify_button_post_full_flow_via_handle_action(verify_env):
    """End-to-end through the queue: claim -> verify executor -> done + result."""
    channel = FakeTextChannel(channel_id=555)
    guild = FakeGuild(channels={555: channel})
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="verify_button_post", payload={"channel_id": "555"})
    bot = FakeBot(pool, guilds={100: guild})

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"]["ok"] is True
    assert pool.rows[1]["result"]["channel_id"] == "555"
    assert len(channel.sent) == 1


# ---------------------------------------------------------------------------
# reconcile: boot backstop for notifies missed during a restart.
# ---------------------------------------------------------------------------


async def test_reconcile_expires_stale_resets_orphans_and_drives_pending(monkeypatch):
    ran = []

    async def _exec(bot, guild_id, payload):
        ran.append(payload.get("tag"))
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    # 1: recent pending  -> should be driven to done.
    pool.add(1, 100, "test_kind", {"tag": "recent"}, status="pending")
    # 2: orphaned running (previous process died mid-run) -> reset then driven.
    pool.add(2, 100, "test_kind", {"tag": "orphan"}, status="running")
    # 3: stale pending (too old) -> expired to failed, executor NEVER runs.
    pool.add(3, 100, "test_kind", {"tag": "stale"}, status="pending", stale=True)
    bot = FakeBot(pool)

    await dashboard_actions.reconcile(bot)

    assert pool.rows[3]["status"] == "failed"
    assert pool.rows[3]["result"] == {"ok": False, "error": "expired"}
    assert pool.rows[1]["status"] == "done"
    assert pool.rows[2]["status"] == "done"
    # The stale row's executor never ran; the two recent ones did.
    assert set(ran) == {"recent", "orphan"}
    assert "stale" not in ran


async def test_reconcile_empty_table_is_noop():
    pool = ActionsPool()
    bot = FakeBot(pool)
    await dashboard_actions.reconcile(bot)  # must not raise
    # Only the two sweep UPDATEs + the pending SELECT ran; no claim/finish.
    assert not any(c[0] == "fetchrow" for c in pool.calls)


# ---------------------------------------------------------------------------
# Registry hygiene.
# ---------------------------------------------------------------------------


def test_verify_button_post_is_registered():
    assert "verify_button_post" in dashboard_actions._EXECUTORS
