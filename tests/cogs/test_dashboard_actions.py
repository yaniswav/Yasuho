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
            _stale_minutes, result_json = args
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
        if "INSERT INTO reaction_roles" in query:  # reaction_role_add upsert
            return "INSERT 0 1"
        if "DELETE FROM reaction_roles" in query:  # reaction_role_remove
            return "DELETE 1"
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
    def __init__(self, pool, guilds=None, cogs=None):
        self.db_pool = pool
        self._guilds = guilds or {}
        self._cogs = cogs or {}
        # The reaction-role remove executor consults the gateway message cache
        # (best-effort unreact); empty by default so that path is a clean no-op.
        self.cached_messages = []
        # The button-panel post executor re-registers the persistent view via
        # bot.add_view; record each (view, message_id) so a test can assert it.
        self.added_views = []

    def get_guild(self, gid):
        return self._guilds.get(gid)

    def get_cog(self, name):
        return self._cogs.get(name)

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))


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
# reaction_role_add / reaction_role_remove executors: re-validate against live
# state, drive the reaction on the real message, and keep the cog cache in sync.
# ---------------------------------------------------------------------------


class RRPool:
    """Minimal pool that records reaction_roles writes for the executor tests."""

    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if "DELETE FROM reaction_roles" in query:
            return "DELETE 1"
        return "INSERT 0 1"


class FakeMessage:
    def __init__(self, message_id=777, fail_add=False):
        self.id = message_id
        self._fail_add = fail_add
        self.reactions = []

    async def add_reaction(self, emoji):
        if self._fail_add:
            # Mirrors a real Forbidden/HTTPException; the executor catches any
            # Exception and maps it to a short code (never a stack).
            raise RuntimeError("missing add-reactions permission")
        self.reactions.append(emoji)


class FakeReactionChannel:
    def __init__(self, channel_id=555, message=None, fail_fetch=False):
        self.id = channel_id
        self.message = message
        self._fail_fetch = fail_fetch

    async def fetch_message(self, mid):
        if self._fail_fetch or self.message is None:
            raise RuntimeError("unknown message")
        return self.message


class FakeRole:
    def __init__(self, role_id=888):
        self.id = role_id


class FakeReactionGuild:
    def __init__(self, channels=None, roles=None, has_me=True):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        self.me = object() if has_me else None

    def get_channel_or_thread(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


class FakeCog:
    """Stand-in for the ReactionRoles cog: just the in-memory cache the executor
    live-patches (and on_raw_reaction_add reads)."""

    def __init__(self):
        self.cache = {}


def _rr_bot(pool, guild=None, cog=None):
    guilds = {100: guild} if guild is not None else {}
    cogs = {"ReactionRoles": cog} if cog is not None else {}
    return FakeBot(pool, guilds=guilds, cogs=cogs)


async def test_reaction_role_add_success():
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )

    # snowflakes come back as STRINGS (never JS numbers on the far side).
    assert result == {
        "ok": True,
        "message_id": "777",
        "emoji": "🎮",
        "role_id": "888",
    }
    # Reacted on the LIVE message with the emoji.
    assert channel.message.reactions == ["🎮"]
    # Upsert used the AUTHORITATIVE guild_id (100, from the claimed row) + role.
    assert len(pool.executed) == 1
    query, args = pool.executed[0]
    assert "INSERT INTO reaction_roles" in query
    assert "ON CONFLICT (message_id, emoji)" in query
    assert args == (777, "🎮", 888, 100)
    # Cog cache live-patched so on_raw_reaction_add honours it without a restart.
    assert cog.cache[(777, "🎮")] == 888


async def test_reaction_role_add_strips_variation_selector():
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog)

    heart = "❤️"  # red heart + U+FE0F variation selector
    stored = "❤"  # what the table + cache must hold (FE0F stripped)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": heart, "role_id": "888"},
    )

    assert result["emoji"] == stored
    # add_reaction gets the ORIGINAL emoji (with FE0F); the DB + cache use the
    # STRIPPED form so an incoming reaction payload matches.
    assert channel.message.reactions == [heart]
    _, args = pool.executed[0]
    assert args[1] == stored
    assert cog.cache[(777, stored)] == 888
    assert (777, heart) not in cog.cache


async def test_reaction_role_add_works_without_cog_loaded():
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog=None)  # cog absent -> cache patch is a no-op

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )

    assert result["ok"] is True
    assert len(pool.executed) == 1  # still persisted


async def test_reaction_role_add_guild_unavailable():
    pool = RRPool()
    bot = _rr_bot(pool, guild=None)  # bot not in guild 100
    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.executed == []


async def test_reaction_role_add_channel_not_found():
    guild = FakeReactionGuild(channels={}, roles={888: FakeRole(888)})
    pool = RRPool()
    bot = _rr_bot(pool, guild, FakeCog())
    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )
    assert result == {"ok": False, "error": "channel_not_found"}
    assert pool.executed == []


async def test_reaction_role_add_message_not_found():
    channel = FakeReactionChannel(555, message=None)  # fetch_message raises
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog)
    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )
    assert result == {"ok": False, "error": "message_not_found"}
    assert pool.executed == []
    assert cog.cache == {}


async def test_reaction_role_add_bad_role():
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={})  # role 888 absent
    pool = RRPool()
    bot = _rr_bot(pool, guild, FakeCog())
    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )
    assert result == {"ok": False, "error": "bad_role"}
    assert pool.executed == []


async def test_reaction_role_add_cant_add_reaction():
    channel = FakeReactionChannel(555, message=FakeMessage(777, fail_add=True))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog)
    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
    )
    assert result == {"ok": False, "error": "cant_add_reaction"}
    # The reaction failed, so NOTHING was persisted or cached.
    assert pool.executed == []
    assert cog.cache == {}


@pytest.mark.parametrize("channel_id", [None, "abc", "", "not-a-number"])
async def test_reaction_role_add_bad_channel_id(channel_id):
    guild = FakeReactionGuild(channels={}, roles={888: FakeRole(888)})
    pool = RRPool()
    bot = _rr_bot(pool, guild, FakeCog())
    payload = {"message_id": "777", "emoji": "🎮", "role_id": "888"}
    if channel_id is not None:
        payload["channel_id"] = channel_id
    result = await dashboard_actions._exec_reaction_role_add(bot, 100, payload)
    assert result == {"ok": False, "error": "bad_channel_id"}
    assert pool.executed == []


@pytest.mark.parametrize("emoji", [None, "", "   "])
async def test_reaction_role_add_rejects_empty_emoji(emoji):
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RRPool()
    bot = _rr_bot(pool, guild, FakeCog())
    payload = {"channel_id": "555", "message_id": "777", "role_id": "888"}
    if emoji is not None:
        payload["emoji"] = emoji
    result = await dashboard_actions._exec_reaction_role_add(bot, 100, payload)
    assert result == {"ok": False, "error": "bad_emoji"}
    assert pool.executed == []


async def test_reaction_role_add_full_flow_via_handle_action():
    """End-to-end through the queue: claim -> add executor -> done + result + cache."""
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: FakeRole(888)})
    cog = FakeCog()
    pool = ActionsPool()
    pool.add(
        1,
        guild_id=100,
        kind="reaction_role_add",
        payload={
            "channel_id": "555",
            "message_id": "777",
            "emoji": "🎮",
            "role_id": "888",
        },
    )
    bot = FakeBot(pool, guilds={100: guild}, cogs={"ReactionRoles": cog})

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"]["ok"] is True
    assert cog.cache[(777, "🎮")] == 888


async def test_reaction_role_remove_deletes_and_pops_cache():
    cog = FakeCog()
    cog.cache[(777, "🎮")] = 888
    pool = RRPool()
    bot = _rr_bot(pool, guild=None, cog=cog)  # no guild -> best-effort unreact skips

    result = await dashboard_actions._exec_reaction_role_remove(
        bot, 100, {"message_id": "777", "emoji": "🎮"}
    )

    assert result == {"ok": True}
    assert len(pool.executed) == 1
    query, args = pool.executed[0]
    assert "DELETE FROM reaction_roles" in query
    # Guild-scoped delete with the AUTHORITATIVE guild_id (100).
    assert args == (777, "🎮", 100)
    # Cache entry popped so on_raw_reaction_add stops granting immediately.
    assert (777, "🎮") not in cog.cache


async def test_reaction_role_remove_strips_variation_selector():
    cog = FakeCog()
    stored = "❤"
    cog.cache[(777, stored)] = 888
    pool = RRPool()
    bot = _rr_bot(pool, guild=None, cog=cog)

    await dashboard_actions._exec_reaction_role_remove(
        bot, 100, {"message_id": "777", "emoji": "❤️"}
    )

    _, args = pool.executed[0]
    assert args[1] == stored  # FE0F stripped before the delete
    assert (777, stored) not in cog.cache


async def test_reaction_role_remove_bad_message_id_does_not_delete():
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild=None, cog=cog)
    result = await dashboard_actions._exec_reaction_role_remove(
        bot, 100, {"message_id": "not-a-number", "emoji": "🎮"}
    )
    assert result == {"ok": False, "error": "message_not_found"}
    assert pool.executed == []


async def test_reaction_role_remove_works_without_cog_loaded():
    pool = RRPool()
    bot = _rr_bot(pool, guild=None, cog=None)
    result = await dashboard_actions._exec_reaction_role_remove(
        bot, 100, {"message_id": "777", "emoji": "🎮"}
    )
    assert result == {"ok": True}
    assert len(pool.executed) == 1  # still deleted


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


def test_reaction_role_executors_are_registered():
    assert "reaction_role_add" in dashboard_actions._EXECUTORS
    assert "reaction_role_remove" in dashboard_actions._EXECUTORS


def test_button_panel_executors_are_registered():
    assert "button_panel_post" in dashboard_actions._EXECUTORS
    assert "button_panel_delete" in dashboard_actions._EXECUTORS


# ---------------------------------------------------------------------------
# button_panel_post / button_panel_delete executors: re-validate against live
# state, render the embed + post a ButtonRoleView REUSED from the cog, persist
# one row per button (message-authoritative) and re-register the persistent view.
# ---------------------------------------------------------------------------


class FakeButtonRoleView:
    """Stand-in for the cog's persistent ButtonRoleView (no discord.ui needed)."""

    instances = 0

    def __init__(self, rows):
        FakeButtonRoleView.instances += 1
        self.rows = list(rows)


class _FakeButtonRolesModule:
    """Stand-in for cogs.config.buttonroles: just what the executor reuses."""

    MAX_BUTTONS = 25
    ButtonRoleView = FakeButtonRoleView


class FakeEmbed:
    def __init__(self, blob):
        self.blob = blob or {}


class _FakeEmbedCreator:
    """Stand-in for tools.embed_creator: render() + embed_has_content()."""

    @staticmethod
    def render(blob):
        return FakeEmbed(blob)

    @staticmethod
    def embed_has_content(embed):
        b = embed.blob
        return bool(
            b.get("title")
            or b.get("description")
            or b.get("fields")
            or b.get("image")
            or b.get("thumbnail")
            or (b.get("author") or {}).get("name")
            or (b.get("footer") or {}).get("text")
        )


class BRRole:
    """Stand-in for discord.Role: id/name plus just enough of the
    assignability surface (is_default/managed/position ordering) for the
    dashboard button-panel executor's guard to exercise."""

    def __init__(self, role_id, name="Role", *, default=False, managed=False, position=1):
        self.id = role_id
        self.name = name
        self.managed = managed
        self.position = position
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class BRMe:
    """Stand-in for guild.me: only needs a top_role to compare against."""

    def __init__(self, top_role_position=1000):
        self.top_role = BRRole(0, "Bot", position=top_role_position)


class BRGuild:
    def __init__(self, channels=None, roles=None, has_me=True):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        self.me = BRMe() if has_me else None

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_channel_or_thread(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


class _BRTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _BRConn:
    """Fake connection: models conn.transaction() + execute() + executemany()."""

    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return _BRTxn()

    async def execute(self, query, *args):
        if "DELETE FROM button_roles" in query:
            self.pool.deleted.append(args[0])
        return "DELETE"

    async def executemany(self, query, records):
        assert "INSERT INTO button_roles" in query
        self.pool.inserted.extend(records)
        return None


class _BRAcquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _BRConn(self.pool)

    async def __aexit__(self, *exc):
        return False


class BRPool:
    """Pool modelling the acquire()/transaction() persist path + the scoped
    DELETE ... RETURNING of the delete executor."""

    def __init__(self, delete_return=None):
        self.inserted = []
        self.deleted = []
        self.delete_calls = []
        self._delete_return = delete_return or []

    def acquire(self):
        return _BRAcquire(self)

    async def fetch(self, query, *args):
        assert "DELETE FROM button_roles" in query  # the delete executor
        self.delete_calls.append(args)
        return self._delete_return


class ButtonActionsPool(ActionsPool):
    """ActionsPool (claim/finish/reconcile) PLUS the acquire() persist path, so a
    button_panel_post can be driven end-to-end through handle_action."""

    def __init__(self):
        super().__init__()
        self.inserted = []
        self.deleted = []

    def acquire(self):
        return _BRAcquire(self)


@pytest.fixture
def button_env(monkeypatch):
    """Patch the lazy buttonroles + embed_creator seams and discord.TextChannel so
    the executor runs without the discord.py-2.x UI stack (absent on the 3.7 box)."""
    FakeButtonRoleView.instances = 0
    monkeypatch.setattr(
        dashboard_actions, "_button_roles_module", lambda: _FakeButtonRolesModule
    )
    monkeypatch.setattr(dashboard_actions, "_embed_creator", lambda: _FakeEmbedCreator)
    monkeypatch.setattr(discord, "TextChannel", FakeTextChannel)
    yield


def _panel_payload(buttons=None, embed=None, channel_id="555"):
    return {
        "channel_id": channel_id,
        "embed": embed if embed is not None else {"description": "Pick a role."},
        "buttons": buttons
        if buttons is not None
        else [{"role_id": "888", "label": "Gamer", "style": 1}],
    }


def _br_bot(pool, guild=None):
    return FakeBot(pool, guilds={100: guild} if guild is not None else {})


async def test_button_panel_post_success(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(
        channels={555: channel},
        roles={888: BRRole(888, "Gamer"), 999: BRRole(999, "Artist")},
    )
    pool = BRPool()
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(
            buttons=[
                {"role_id": "888", "label": "Gamer", "emoji": "🎮", "style": 1},
                {"role_id": "999", "label": "Artist", "style": 3},
            ]
        ),
    )

    assert result == {
        "ok": True,
        "message_id": "999888777666555444",
        "channel_id": "555",
    }
    # Posted exactly one message carrying the embed + the reused ButtonRoleView.
    assert len(channel.sent) == 1
    _, kwargs = channel.sent[0]
    assert isinstance(kwargs["embed"], FakeEmbed)
    assert isinstance(kwargs["view"], FakeButtonRoleView)
    # One row per button, message-authoritative (DELETE then INSERT).
    assert pool.deleted == [999888777666555444]
    assert pool.inserted == [
        (999888777666555444, 100, 555, 888, "Gamer", "🎮", 1),
        (999888777666555444, 100, 555, 999, "Artist", None, 3),
    ]
    # Persistent view re-registered for THIS message so it survives a restart.
    assert len(bot.added_views) == 1
    view, mid = bot.added_views[0]
    assert mid == 999888777666555444
    assert isinstance(view, FakeButtonRoleView)
    assert view.rows == [(888, "Gamer", "🎮", 1), (999, "Artist", None, 3)]


async def test_button_panel_post_dedupes_roles(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888, "Gamer")})
    pool = BRPool()
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(
            buttons=[
                {"role_id": "888", "label": "First", "style": 1},
                {"role_id": "888", "label": "Duplicate", "style": 4},
            ]
        ),
    )

    assert result["ok"] is True
    # The duplicate role produced no second row (mirrors the (message, role) PK).
    assert len(pool.inserted) == 1
    assert pool.inserted[0][3] == 888
    assert pool.inserted[0][4] == "First"


async def test_button_panel_post_empty_label_falls_back_to_role_name(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888, "Gamer")})
    pool = BRPool()
    bot = _br_bot(pool, guild)

    await dashboard_actions._exec_button_panel_post(
        bot, 100, _panel_payload(buttons=[{"role_id": "888", "style": 2}])
    )

    assert pool.inserted[0][4] == "Gamer"  # label defaulted to the role name


async def test_button_panel_post_coerces_bad_style_to_secondary(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)

    await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(buttons=[{"role_id": "888", "label": "X", "style": 9}]),
    )

    assert pool.inserted[0][6] == 2  # style 9 (Link/premium/unknown) -> secondary


@pytest.mark.parametrize("channel_id", [None, "abc", "", "not-a-number"])
async def test_button_panel_post_bad_channel_id(button_env, channel_id):
    guild = BRGuild(channels={}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    payload = _panel_payload()
    if channel_id is None:
        payload.pop("channel_id")
    else:
        payload["channel_id"] = channel_id
    result = await dashboard_actions._exec_button_panel_post(bot, 100, payload)
    assert result == {"ok": False, "error": "bad_channel_id"}
    assert pool.inserted == []


async def test_button_panel_post_guild_unavailable(button_env):
    pool = BRPool()
    bot = _br_bot(pool, guild=None)  # bot not in guild 100
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload())
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.inserted == []


async def test_button_panel_post_channel_not_found(button_env):
    guild = BRGuild(channels={}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload())
    assert result == {"ok": False, "error": "channel_not_found"}
    assert pool.inserted == []


async def test_button_panel_post_rejects_non_text_channel(button_env):
    guild = BRGuild(channels={555: FakeVoiceChannel(555)}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload())
    assert result == {"ok": False, "error": "not_text_channel"}
    assert pool.inserted == []


async def test_button_panel_post_missing_send_permission(button_env):
    channel = FakeTextChannel(channel_id=555, can_send=False)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload())
    assert result == {"ok": False, "error": "missing_send_permission"}
    assert channel.sent == []
    assert pool.inserted == []


@pytest.mark.parametrize("buttons", [None, [], "notalist"])
async def test_button_panel_post_no_buttons(button_env, buttons):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    payload = _panel_payload()
    if buttons is None:
        payload.pop("buttons")
    else:
        payload["buttons"] = buttons
    result = await dashboard_actions._exec_button_panel_post(bot, 100, payload)
    assert result == {"ok": False, "error": "no_buttons"}
    assert channel.sent == []


async def test_button_panel_post_too_many_buttons(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    payload = _panel_payload(
        buttons=[{"role_id": "888", "style": 2} for _ in range(26)]
    )
    result = await dashboard_actions._exec_button_panel_post(bot, 100, payload)
    assert result == {"ok": False, "error": "too_many_buttons"}
    assert channel.sent == []


async def test_button_panel_post_bad_role(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={})  # role 888 absent
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload())
    assert result == {"ok": False, "error": "bad_role"}
    assert channel.sent == []
    assert pool.inserted == []


@pytest.mark.parametrize(
    "role",
    [
        BRRole(888, "@everyone", default=True),
        BRRole(888, "Integration", managed=True),
        BRRole(888, "Too High", position=2000),  # >= bot's top_role (1000)
    ],
    ids=["everyone", "managed", "above_bot_top_role"],
)
async def test_button_panel_post_rejects_unassignable_role(button_env, role):
    """Mirrors the /buttonrole builder's guard: a dashboard write can't persist
    a dead/dangerous role button (@everyone, managed, or >= our top role)."""
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: role})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload())
    assert result == {"ok": False, "error": "role_not_assignable"}
    assert channel.sent == []
    assert pool.inserted == []


async def test_button_panel_post_empty_embed(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(
        bot, 100, _panel_payload(embed={})  # no visible content
    )
    assert result == {"ok": False, "error": "empty_embed"}
    # Nothing posted, persisted or registered for an empty embed.
    assert channel.sent == []
    assert pool.inserted == []
    assert bot.added_views == []


async def test_button_panel_post_full_flow_via_handle_action(button_env):
    """End-to-end through the queue: claim -> post executor -> done + result."""
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888, "Gamer")})
    pool = ButtonActionsPool()
    pool.add(1, guild_id=100, kind="button_panel_post", payload=_panel_payload())
    bot = FakeBot(pool, guilds={100: guild})

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"]["ok"] is True
    assert pool.rows[1]["result"]["channel_id"] == "555"
    assert len(channel.sent) == 1
    assert len(pool.inserted) == 1
    assert len(bot.added_views) == 1


async def test_button_panel_delete_scoped(button_env):
    channel = FakeTextChannel(channel_id=555)

    class _StripMsg:
        def __init__(self):
            self.edited = None

        async def edit(self, **kwargs):
            self.edited = kwargs

    strip = _StripMsg()

    async def _fetch_message(mid):
        return strip

    channel.fetch_message = _fetch_message
    guild = BRGuild(channels={555: channel})
    pool = BRPool(delete_return=[{"channel_id": 555}])
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_delete(
        bot, 100, {"message_id": "777"}
    )

    assert result == {"ok": True}
    # Guild-scoped delete with the AUTHORITATIVE guild_id (100).
    assert pool.delete_calls == [(777, 100)]
    # Best-effort strip of the live buttons.
    assert strip.edited == {"view": None}


async def test_button_panel_delete_no_rows_is_still_ok(button_env):
    guild = BRGuild(channels={})
    pool = BRPool(delete_return=[])  # nothing matched (e.g. wrong guild)
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_delete(
        bot, 100, {"message_id": "777"}
    )
    assert result == {"ok": True}
    assert pool.delete_calls == [(777, 100)]


@pytest.mark.parametrize("message_id", [None, "abc", "", "not-a-number"])
async def test_button_panel_delete_bad_message_id(button_env, message_id):
    pool = BRPool()
    bot = _br_bot(pool, BRGuild())
    payload = {} if message_id is None else {"message_id": message_id}
    result = await dashboard_actions._exec_button_panel_delete(bot, 100, payload)
    assert result == {"ok": False, "error": "message_not_found"}
    assert pool.delete_calls == []


# ---------------------------------------------------------------------------
# role_menu_post / role_menu_delete executors: re-validate against live state,
# reuse tools.role_menus.normalize_options + the cog's RoleMenuView (post-then-
# edit so the select's custom_id carries the real message id), persist the
# role_menus row (guild-authoritative) and re-register the persistent view.
# ---------------------------------------------------------------------------


class FakeRoleMenuView:
    """Stand-in for the cog's persistent RoleMenuView (no discord.ui needed)."""

    instances = 0

    def __init__(self, message_id, config):
        FakeRoleMenuView.instances += 1
        self.message_id = message_id
        self.config = config


class _FakeRoleMenusModule:
    """Stand-in for cogs.config.rolemenus: just what the executor reuses."""

    MAX_MENUS_PER_GUILD = 25
    RoleMenuView = FakeRoleMenuView


class FakeRMMessage:
    """The posted menu message: supports the edit(view=...) / delete() the
    post-then-edit custom_id trick + the best-effort strip on delete use."""

    def __init__(self, message_id=999888777666555444, fail_edit=False):
        self.id = message_id
        self._fail_edit = fail_edit
        self.edited = None
        self.deleted = False

    async def edit(self, **kwargs):
        if self._fail_edit:
            resp = types.SimpleNamespace(status=400, reason="Bad Request")
            raise discord.HTTPException(resp, "edit failed")
        self.edited = kwargs

    async def delete(self):
        self.deleted = True


class FakeRMChannel:
    def __init__(self, channel_id=555, can_send=True, message=None, fail_edit=False):
        self.id = channel_id
        self._can_send = can_send
        self._message = message or FakeRMMessage(fail_edit=fail_edit)
        self.sent = []

    def permissions_for(self, member):
        return FakePermissions(self._can_send)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return self._message


class RMGuild:
    def __init__(self, channels=None, roles=None, has_me=True, preferred_locale="en"):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        self.me = object() if has_me else None
        self.preferred_locale = preferred_locale

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_channel_or_thread(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


class FakeRMCog:
    """Stand-in for the RoleMenus cog: just the in-memory id set the executor
    keeps in sync (so deleting a message still prunes the row)."""

    def __init__(self):
        self._menu_ids = set()


class RMPool:
    """Pool modelling the role_menus COUNT + INSERT ... ON CONFLICT persist and the
    scoped DELETE ... RETURNING of the delete executor. fetchval also answers the
    settings locale lookup (unconfigured -> None) reached via resolve_guild_locale."""

    def __init__(self, count=0, delete_return=None):
        self.count = count
        self.inserted = []
        self.delete_calls = []
        self._delete_return = delete_return or []

    async def fetchval(self, query, *args):
        if "SELECT COUNT(*) FROM role_menus" in query:
            return self.count
        return None  # settings.get_guild locale lookup: unconfigured guild

    async def execute(self, query, *args):
        assert "INSERT INTO role_menus" in query
        self.inserted.append(args)
        return "INSERT 0 1"

    async def fetch(self, query, *args):
        assert "DELETE FROM role_menus" in query
        self.delete_calls.append(args)
        return self._delete_return


class RoleMenuActionsPool(ActionsPool):
    """ActionsPool (claim/finish/reconcile) PLUS the role_menus INSERT persist path,
    so a role_menu_post can be driven end-to-end through handle_action. The COUNT
    fetchval falls through to ActionsPool.fetchval -> None (treated as 0 menus)."""

    def __init__(self):
        super().__init__()
        self.inserted = []

    async def execute(self, query, *args):
        if "INSERT INTO role_menus" in query:
            self.inserted.append(args)
            return "INSERT 0 1"
        return await super().execute(query, *args)


@pytest.fixture
def rolemenu_env(monkeypatch):
    """Patch the lazy rolemenus seam + discord.TextChannel so the executor runs
    without the discord.py-2.x UI stack (absent on the 3.7 box)."""
    FakeRoleMenuView.instances = 0
    monkeypatch.setattr(
        dashboard_actions, "_role_menus_module", lambda: _FakeRoleMenusModule
    )
    monkeypatch.setattr(discord, "TextChannel", FakeRMChannel)
    yield


def _rm_bot(pool, guild=None, cog=None):
    guilds = {100: guild} if guild is not None else {}
    cogs = {"RoleMenus": cog} if cog is not None else {}
    return FakeBot(pool, guilds=guilds, cogs=cogs)


def _menu_payload(options=None, channel_id="555", **config):
    cfg = {
        "options": options
        if options is not None
        else [{"role_id": "888", "label": "Blue"}]
    }
    cfg.update(config)
    return {"channel_id": channel_id, "config": cfg}


async def test_role_menu_post_success(rolemenu_env):
    channel = FakeRMChannel(555)
    guild = RMGuild(
        channels={555: channel},
        roles={888: FakeRole(888), 999: FakeRole(999)},
    )
    cog = FakeRMCog()
    pool = RMPool(count=0)
    bot = _rm_bot(pool, guild, cog)

    result = await dashboard_actions._exec_role_menu_post(
        bot,
        100,
        _menu_payload(
            title="Colours",
            description="Pick",
            colour=0x5865F2,
            exclusive=True,
            placeholder="Choose",
            options=[
                {"role_id": "888", "label": "Blue", "emoji": "🔵", "description": "cool"},
                {"role_id": "999", "label": "Red", "temp_seconds": 3600},
            ],
        ),
    )

    assert result == {"ok": True, "message_id": "999888777666555444", "menu": True}
    # Posted exactly one message carrying the embed; then edited to attach the view.
    assert len(channel.sent) == 1
    _, kwargs = channel.sent[0]
    assert isinstance(kwargs["embed"], discord.Embed)
    assert channel._message.edited is not None
    view = channel._message.edited["view"]
    assert isinstance(view, FakeRoleMenuView)
    # The view was built with the REAL message id (the custom_id trick) + a config
    # normalised through tools.role_menus.normalize_options (role_ids are ints).
    assert view.message_id == 999888777666555444
    assert view.config["exclusive"] is True
    assert view.config["placeholder"] == "Choose"
    assert [o["role_id"] for o in view.config["options"]] == [888, 999]
    # Persisted with the AUTHORITATIVE guild_id + the normalised JSONB config.
    assert len(pool.inserted) == 1
    args = pool.inserted[0]
    assert args[0] == 999888777666555444  # message id
    assert args[1] == 100  # authoritative guild id (from the claimed row)
    assert args[2] == 555  # channel id
    stored = json.loads(args[3])
    assert stored["exclusive"] is True
    assert stored["colour"] == 0x5865F2
    assert stored["options"][0]["role_id"] == 888
    assert stored["options"][1]["temp_seconds"] == 3600
    # Persistent view re-registered for THIS message; cog id set kept in sync.
    assert len(bot.added_views) == 1
    _, mid = bot.added_views[0]
    assert mid == 999888777666555444
    assert 999888777666555444 in cog._menu_ids


async def test_role_menu_post_defaults_exclusive_false_and_temp_zero(rolemenu_env):
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    await dashboard_actions._exec_role_menu_post(
        bot, 100, _menu_payload(options=[{"role_id": "888", "label": "Blue"}])
    )

    stored = json.loads(pool.inserted[0][3])
    assert stored["exclusive"] is False
    assert stored["options"][0]["temp_seconds"] == 0
    assert stored["options"][0]["emoji"] is None
    assert "placeholder" not in stored  # only set when provided


async def test_role_menu_post_filters_foreign_roles(rolemenu_env):
    """A foreign/gone role is dropped; a menu with a valid one still posts."""
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})  # 999 absent
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot,
        100,
        _menu_payload(
            options=[
                {"role_id": "888", "label": "Blue"},
                {"role_id": "999", "label": "Ghost"},
            ]
        ),
    )

    assert result["ok"] is True
    stored = json.loads(pool.inserted[0][3])
    assert [o["role_id"] for o in stored["options"]] == [888]


async def test_role_menu_post_bad_role_all(rolemenu_env):
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={})  # no roles at all
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot, 100, _menu_payload(options=[{"role_id": "888", "label": "Blue"}])
    )

    assert result == {"ok": False, "error": "bad_role_all"}
    assert channel.sent == []
    assert pool.inserted == []


@pytest.mark.parametrize("options", [None, [], "notalist", [{"label": "no role id"}]])
async def test_role_menu_post_no_options(rolemenu_env, options):
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    payload = _menu_payload()
    if options is None:
        payload["config"].pop("options")
    else:
        payload["config"]["options"] = options
    result = await dashboard_actions._exec_role_menu_post(bot, 100, payload)
    assert result == {"ok": False, "error": "no_options"}
    assert channel.sent == []


async def test_role_menu_post_too_many_menus(rolemenu_env):
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool(count=25)  # already at MAX_MENUS_PER_GUILD
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload())

    assert result == {"ok": False, "error": "too_many_menus"}
    assert channel.sent == []
    assert pool.inserted == []


@pytest.mark.parametrize("channel_id", [None, "abc", "", "not-a-number"])
async def test_role_menu_post_bad_channel_id(rolemenu_env, channel_id):
    guild = RMGuild(channels={}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    payload = _menu_payload()
    if channel_id is None:
        payload.pop("channel_id")
    else:
        payload["channel_id"] = channel_id
    result = await dashboard_actions._exec_role_menu_post(bot, 100, payload)
    assert result == {"ok": False, "error": "bad_channel_id"}
    assert pool.inserted == []


async def test_role_menu_post_guild_unavailable(rolemenu_env):
    pool = RMPool()
    bot = _rm_bot(pool, guild=None)  # bot not in guild 100
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload())
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.inserted == []


async def test_role_menu_post_channel_not_found(rolemenu_env):
    guild = RMGuild(channels={}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload())
    assert result == {"ok": False, "error": "channel_not_found"}
    assert pool.inserted == []


async def test_role_menu_post_rejects_non_text_channel(rolemenu_env):
    guild = RMGuild(channels={555: FakeVoiceChannel(555)}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload())
    assert result == {"ok": False, "error": "not_text_channel"}
    assert pool.inserted == []


async def test_role_menu_post_missing_send_permission(rolemenu_env):
    channel = FakeRMChannel(555, can_send=False)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload())
    assert result == {"ok": False, "error": "missing_send_permission"}
    assert channel.sent == []
    assert pool.inserted == []


async def test_role_menu_post_edit_failure_deletes_and_reports(rolemenu_env):
    msg = FakeRMMessage(fail_edit=True)
    channel = FakeRMChannel(555, message=msg)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload())

    assert result == {"ok": False, "error": "post_failed"}
    # The orphan (view-less) message is cleaned up; nothing persisted or registered.
    assert msg.deleted is True
    assert pool.inserted == []
    assert bot.added_views == []


async def test_role_menu_post_full_flow_via_handle_action(rolemenu_env):
    """End-to-end through the queue: claim -> post executor -> done + result."""
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RoleMenuActionsPool()
    pool.add(1, guild_id=100, kind="role_menu_post", payload=_menu_payload())
    bot = FakeBot(pool, guilds={100: guild})

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"]["ok"] is True
    assert pool.rows[1]["result"]["menu"] is True
    assert len(channel.sent) == 1
    assert len(pool.inserted) == 1
    assert len(bot.added_views) == 1


async def test_role_menu_delete_scoped(rolemenu_env):
    strip = FakeRMMessage()
    channel = FakeRMChannel(555)

    async def _fetch_message(mid):
        return strip

    channel.fetch_message = _fetch_message
    guild = RMGuild(channels={555: channel})
    cog = FakeRMCog()
    cog._menu_ids.add(777)
    pool = RMPool(delete_return=[{"channel_id": 555}])
    bot = _rm_bot(pool, guild, cog)

    result = await dashboard_actions._exec_role_menu_delete(
        bot, 100, {"message_id": "777"}
    )

    assert result == {"ok": True}
    # Guild-scoped delete with the AUTHORITATIVE guild_id (100).
    assert pool.delete_calls == [(777, 100)]
    # Best-effort strip of the live select + pruned from the cog's id set.
    assert strip.edited == {"view": None}
    assert 777 not in cog._menu_ids


async def test_role_menu_delete_no_rows_is_still_ok(rolemenu_env):
    guild = RMGuild(channels={})
    pool = RMPool(delete_return=[])  # nothing matched (e.g. wrong guild)
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_delete(
        bot, 100, {"message_id": "777"}
    )
    assert result == {"ok": True}
    assert pool.delete_calls == [(777, 100)]


@pytest.mark.parametrize("message_id", [None, "abc", "", "not-a-number"])
async def test_role_menu_delete_bad_message_id(rolemenu_env, message_id):
    pool = RMPool()
    bot = _rm_bot(pool, RMGuild())
    payload = {} if message_id is None else {"message_id": message_id}
    result = await dashboard_actions._exec_role_menu_delete(bot, 100, payload)
    assert result == {"ok": False, "error": "message_not_found"}
    assert pool.delete_calls == []


def test_role_menu_executors_are_registered():
    assert "role_menu_post" in dashboard_actions._EXECUTORS
    assert "role_menu_delete" in dashboard_actions._EXECUTORS
