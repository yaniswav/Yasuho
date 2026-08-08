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

import asyncio
import json
import logging
import types

import discord
import pytest

from cogs.system import dashboard_actions
from tools import i18n, settings

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
        self.rows = {}  # id -> dict(guild_id, user_id, kind, payload, status, ...)

    def add(
        self,
        action_id,
        guild_id,
        kind,
        payload,
        status="pending",
        stale=False,
        fresh_claim=False,
        user_id=None,
    ):
        # ``fresh_claim`` models a 'running' row whose updated_at is recent (just
        # claimed by a live handler of this process); reconcile's age-guarded
        # step-2 must NOT reset such a row. Default False = an orphan of a dead
        # previous process (stale updated_at), which step-2 DOES reset.
        #
        # ``guild_id`` / ``user_id`` are the two SCOPE columns; the real table's
        # dashboard_actions_scope_valid CHECK allows exactly one of them to be
        # set, and the tests that exercise a rejected scope pass that pair
        # explicitly (a doubly/never scoped row is what the CHECK refuses, and is
        # asserted probe-side against a real Postgres).
        self.rows[action_id] = {
            "guild_id": guild_id,
            "user_id": user_id,
            "kind": kind,
            "payload": payload,
            "status": status,
            "result": None,
            "stale": stale,
            "fresh_claim": fresh_claim,
        }

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "WHERE id = $1 AND status = 'pending'" in query:  # the claim
            action_id = args[0]
            row = self.rows.get(action_id)
            if row is None or row["status"] != "pending":
                return None
            row["status"] = "running"
            # The claim SETs updated_at = now(), so a just-claimed row is fresh.
            row["fresh_claim"] = True
            # Only the columns the real RETURNING lists, so a dispatcher reading
            # anything else would fail here rather than silently pass.
            claimed = {
                "guild_id": row["guild_id"],
                "kind": row["kind"],
                "payload": row["payload"],
            }
            if "user_id" in query:
                claimed["user_id"] = row["user_id"]
            return claimed
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
            # reconcile step-2: reset orphaned running rows. Model the age-guard
            # faithfully: only when the query carries the `updated_at < now() -
            # ... second` clause is a freshly claimed row (recent updated_at)
            # spared. Without that clause (the pre-fix SQL) EVERY running row is
            # reset - so a test of the live-claim race stays red before the fix.
            #
            # The second guard is the in-flight mark: reconcile excludes the ids
            # THIS process is handling, whatever their age. Modelled from the
            # `id = ANY($2)` clause and its parameter, so a test of a
            # longer-than-the-window executor stays red without it.
            age_guarded = "updated_at <" in query and "second" in query
            mark_guarded = "id = ANY(" in query
            inflight = set(args[1]) if mark_guarded and len(args) > 1 else set()
            for action_id, row in self.rows.items():
                if row["status"] != "running":
                    continue
                if age_guarded and row["fresh_claim"]:
                    continue
                if action_id in inflight:
                    continue
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


@pytest.fixture(autouse=True)
def _clear_autoroom_locks():
    """The per-guild autoroom lock map is module-global (one lock per guild that
    has ever run a dashboard autoroom action); each test gets its own event loop,
    so start from an empty map rather than reusing a lock across loops."""
    dashboard_actions._AUTOROOM_LOCKS.clear()
    yield
    dashboard_actions._AUTOROOM_LOCKS.clear()


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


async def test_claim_stamps_updated_at():
    """The reconcile step-2 age-guard only protects a live claim if the claim
    itself refreshes updated_at. Assert the claim SQL sets it to now() as it
    flips the row to 'running'; without that the guard would protect nothing."""
    captured = []

    class _CapturePool:
        async def fetchrow(self, query, *args):
            captured.append(query)
            return None

    await dashboard_actions._claim(_CapturePool(), 1)

    assert len(captured) == 1
    assert "status = 'running'" in captured[0]
    assert "updated_at = now()" in captured[0]


# ---------------------------------------------------------------------------
# Scope threading: a row names EITHER a guild OR a user, and the KIND - never
# the populated column - decides which one its executor is handed.
# ---------------------------------------------------------------------------


async def test_claim_returns_both_scope_columns():
    """The dispatcher can only choose a scope if the claim brings both back."""
    captured = []

    class _CapturePool:
        async def fetchrow(self, query, *args):
            captured.append(query)
            return None

    await dashboard_actions._claim(_CapturePool(), 1)

    assert "RETURNING guild_id, user_id, kind, payload" in captured[0]


async def test_guild_kind_is_dispatched_with_the_guild_id(monkeypatch):
    """Non-regression: a guild row behaves EXACTLY as before this lot."""
    seen = []

    async def _exec(bot, scope_id, payload):
        seen.append(scope_id)
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, 100, "test_kind", {})
    bot = FakeBot(pool)

    assert await dashboard_actions.handle_action(bot, 1) == "done"
    assert seen == [100]
    assert pool.rows[1]["status"] == "done"


async def test_guild_kind_ignores_a_user_id_that_should_not_be_there(monkeypatch):
    """The scope comes from the KIND. Even if a user_id somehow sat on a guild
    row, a guild kind still acts on the guild - it can never be redirected at a
    user by a column it does not read."""
    seen = []

    async def _exec(bot, scope_id, payload):
        seen.append(scope_id)
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, 100, "test_kind", {}, user_id=999)
    bot = FakeBot(pool)

    assert await dashboard_actions.handle_action(bot, 1) == "done"
    assert seen == [100]


async def test_user_kind_is_dispatched_with_the_user_id(monkeypatch):
    """A kind in _USER_KINDS gets the row's user_id as its scope argument."""
    seen = []

    async def _exec(bot, scope_id, payload):
        seen.append(scope_id)
        return {"ok": True, "delivered": "dm"}

    _register(monkeypatch, "user_kind", _exec)
    monkeypatch.setattr(
        dashboard_actions, "_USER_KINDS", frozenset({"user_kind"})
    )
    pool = ActionsPool()
    pool.add(1, None, "user_kind", {}, user_id=4242)
    bot = FakeBot(pool)

    assert await dashboard_actions.handle_action(bot, 1) == "done"
    assert seen == [4242]
    assert pool.rows[1]["result"] == {"ok": True, "delivered": "dm"}


async def test_user_kind_on_a_guild_row_is_refused_as_bad_scope(monkeypatch):
    """A user kind written onto a guild-scoped row must NOT run with the guild
    id standing in for a user id - that would export the wrong person's data."""
    ran = []

    async def _exec(bot, scope_id, payload):
        ran.append(scope_id)
        return {"ok": True}

    _register(monkeypatch, "user_kind", _exec)
    monkeypatch.setattr(
        dashboard_actions, "_USER_KINDS", frozenset({"user_kind"})
    )
    pool = ActionsPool()
    pool.add(1, 100, "user_kind", {})  # guild-scoped row, user_id NULL
    bot = FakeBot(pool)

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert ran == []  # the executor never even started
    assert pool.rows[1]["result"] == {"ok": False, "error": "bad_scope"}


async def test_guild_kind_on_a_user_row_is_refused_as_bad_scope(monkeypatch):
    """The mirror case: a guild kind on a user row has no guild to act on."""
    ran = []

    async def _exec(bot, scope_id, payload):
        ran.append(scope_id)
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, None, "test_kind", {}, user_id=4242)
    bot = FakeBot(pool)

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert ran == []
    assert pool.rows[1]["result"] == {"ok": False, "error": "bad_scope"}


def test_scope_id_picks_the_column_the_kind_declares():
    row = {"guild_id": 100, "user_id": 4242}
    assert dashboard_actions._scope_id("verify_button_post", row) == 100
    assert dashboard_actions._scope_id("mydata_export", row) == 4242


def test_scope_id_refuses_a_row_missing_the_column():
    """A row shape that predates (or postdates) this claim must be refused
    rather than raise inside the dispatcher."""
    assert dashboard_actions._scope_id("mydata_export", {"guild_id": 100}) is None
    assert dashboard_actions._scope_id("verify_button_post", {}) is None
    assert dashboard_actions._scope_id("mydata_export", None) is None


def test_user_kinds_matches_the_user_executor_registry():
    """_USER_KINDS is a literal, so a kind added to dashboard_user_actions and
    forgotten here would be dispatched with a NULL guild id and refused. This is
    the guard that makes the duplication safe."""
    from cogs.system import dashboard_user_actions

    assert dashboard_actions._USER_KINDS == frozenset(
        dashboard_user_actions.EXECUTORS
    )
    # ...and every one of them really is in the single dispatch table.
    assert dashboard_actions._USER_KINDS <= set(dashboard_actions._EXECUTORS)


def test_no_guild_kind_is_listed_as_user_scoped():
    """The other direction of the same drift: a guild kind accidentally added to
    _USER_KINDS would silently stop receiving its guild id.

    Expressed as "every registered kind that is not a user executor", so this
    covers the WHOLE registry rather than a hand-picked sample that would quietly
    stop being representative.
    """
    from cogs.system import dashboard_user_actions

    guild_kinds = set(dashboard_actions._EXECUTORS) - set(
        dashboard_user_actions.EXECUTORS
    )
    assert guild_kinds  # the sample is not empty by accident
    assert not (guild_kinds & dashboard_actions._USER_KINDS)


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


async def test_reconcile_does_not_reset_freshly_claimed_running_row(monkeypatch):
    """The finding: the listener is attached BEFORE reconcile runs, so a live
    handler of this process can already hold a just-claimed 'running' row (recent
    updated_at). The age-guarded step-2 must NOT reset it - otherwise step-3
    re-claims and re-runs the executor, doubling the side effect (double panel /
    menu, cap exceeded by one)."""
    ran = []

    async def _exec(bot, guild_id, payload):
        ran.append(payload.get("tag"))
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    # A row a live handler claimed moments ago: running + recent updated_at. It is
    # mid-flight (side effect maybe done, terminal status not yet written).
    pool.add(1, 100, "test_kind", {"tag": "live"}, status="running", fresh_claim=True)
    bot = FakeBot(pool)

    await dashboard_actions.reconcile(bot)

    # Left running (not reset), never re-claimed, executor never re-ran for it.
    assert pool.rows[1]["status"] == "running"
    assert ran == []
    assert not any(c[0] == "fetchrow" for c in pool.calls)


async def test_reconcile_never_resets_a_claim_this_process_is_still_working_on(
    monkeypatch,
):
    """The age guard alone is only a heuristic about time.

    An executor may legitimately run LONGER than the grace window -
    ``mydata_export`` packs an archive and uploads several megabytes to Discord -
    which makes its claim both live and old, i.e. indistinguishable from an
    orphan by age. The in-flight mark is what tells them apart: reconcile must
    leave the row alone and must not re-drive it, or the two runs race to write
    the terminal status and the row can end as ``failed`` for work that really
    was delivered.
    """
    runs = []

    async def _exec(bot, guild_id, payload):
        runs.append(payload.get("tag"))
        if len(runs) == 1:
            # This executor is slow: by the time the boot sweep lands, its claim
            # is older than the grace window (what fresh_claim=False models).
            pool.rows[1]["fresh_claim"] = False
            await dashboard_actions.reconcile(bot)
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    pool.add(1, 100, "test_kind", {"tag": "slow"}, status="pending")
    bot = FakeBot(pool)

    assert await dashboard_actions.handle_action(bot, 1) == "done"

    # Ran exactly once, and the terminal status is the real run's.
    assert runs == ["slow"]
    assert pool.rows[1]["status"] == "done"
    assert pool.rows[1]["result"] == {"ok": True}


async def test_the_inflight_mark_is_a_refcount_and_is_always_released():
    """A notify and the boot sweep can enter for the SAME id (only one wins the
    claim); the loser leaving must not clear the winner's mark. And a crash must
    not leave a permanent mark - that row could never be recovered again."""
    assert dashboard_actions._INFLIGHT_ACTIONS == {}

    with dashboard_actions._inflight(7):
        with dashboard_actions._inflight(7):
            assert dashboard_actions._INFLIGHT_ACTIONS == {7: 2}
        # The inner exit does NOT clear the outer's mark.
        assert dashboard_actions._INFLIGHT_ACTIONS == {7: 1}
    assert dashboard_actions._INFLIGHT_ACTIONS == {}

    try:
        with dashboard_actions._inflight(7):
            raise RuntimeError("executor exploded")
    except RuntimeError:
        pass
    assert dashboard_actions._INFLIGHT_ACTIONS == {}


async def test_reconcile_resets_and_redrives_stale_orphan_running_row(monkeypatch):
    """Non-regression of the orphan path: a 'running' row left by a DEAD previous
    process (stale updated_at) IS reset to pending and re-driven through the claim
    so a notify lost during the restart is not stranded."""
    ran = []

    async def _exec(bot, guild_id, payload):
        ran.append(payload.get("tag"))
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    pool = ActionsPool()
    # fresh_claim defaults to False -> models a stale (older-than-grace) claim.
    pool.add(1, 100, "test_kind", {"tag": "orphan"}, status="running")
    bot = FakeBot(pool)

    await dashboard_actions.reconcile(bot)

    assert pool.rows[1]["status"] == "done"
    assert ran == ["orphan"]


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


def test_mydata_export_is_registered_and_user_scoped():
    assert "mydata_export" in dashboard_actions._EXECUTORS
    assert "mydata_export" in dashboard_actions._USER_KINDS


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


# ---------------------------------------------------------------------------
# autoroom_hub_create / autoroom_hub_delete executors: validate the payload
# server-side BEFORE touching the cog, then drive TemporaryRooms._add_hub /
# ._remove_hub (which create/delete the REAL category + trigger channel and
# re-index the cog). Success is detected STRUCTURALLY - both cog methods return
# a translated human string on every path - by diffing the saved hub list.
# ---------------------------------------------------------------------------


class FakeRoomsCog:
    """Stand-in for the TemporaryRooms cog: the three methods the executors use.

    ``_load_hubs`` serves the guild's saved hubs (the before/after picture the
    create executor diffs), ``_add_hub`` records its exact kwargs and - unless
    seeded to refuse - appends a hub and returns the "Created ..." message, just
    as the real one does only after actually saving. ``_remove_hub`` records the
    id, drops the hub and returns its own message. No Discord objects needed: the
    real channel work happens inside the cog, which is not under test here.
    """

    CREATED_HUB_ID = "newhub01"
    CREATED_CHANNEL_ID = 4242

    def __init__(self, hubs=None, refuse_add=False):
        self.hubs = list(hubs or [])
        self.loads = []
        self.add_calls = []
        self.remove_calls = []
        # The gettext locale that was ACTIVE on each call: the real cog builds
        # its messages with _(), so the executor must set the guild's language
        # around the call or every dashboard message comes back in English.
        self.locales = []
        self._refuse_add = refuse_add

    async def _load_hubs(self, guild_id):
        self.loads.append(guild_id)
        return [dict(hub) for hub in self.hubs]

    async def _add_hub(
        self, guild, *, label, category_name, hub_name, template, user_limit
    ):
        self.locales.append(i18n.current_locale.get())
        self.add_calls.append(
            {
                "guild": guild,
                "label": label,
                "category_name": category_name,
                "hub_name": hub_name,
                "template": template,
                "user_limit": user_limit,
            }
        )
        if self._refuse_add:
            # A budget refusal: the real cog returns its message WITHOUT saving.
            return "This server is at Discord's limit of 50 categories."
        self.hubs.append(
            _ar_hub(
                hub_id=self.CREATED_HUB_ID,
                hub_channel_id=self.CREATED_CHANNEL_ID,
                label=label,
            )
        )
        return "Created the **%s** hub. Members can join <#%s> now." % (
            label,
            self.CREATED_CHANNEL_ID,
        )

    async def _remove_hub(self, guild, hub_id):
        self.locales.append(i18n.current_locale.get())
        self.remove_calls.append((guild, hub_id))
        self.hubs = [hub for hub in self.hubs if hub["id"] != hub_id]
        return "Removed the **Ranked** hub."


class SlowRoomsCog(FakeRoomsCog):
    """A cog whose ``_add_hub`` models the REAL read-modify-write of the blob.

    The real one loads the hub list, awaits a Discord round trip (creating the
    category + trigger channel), then appends and saves. Two concurrent creates
    for one guild therefore both save from a list read before either finished:
    without the executor's per-guild lock the second save silently drops the
    first hub. The ``sleep(0)`` is that round trip's suspension point.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._created = 0

    async def _add_hub(
        self, guild, *, label, category_name, hub_name, template, user_limit
    ):
        self.locales.append(i18n.current_locale.get())
        self.add_calls.append({"label": label})
        snapshot = list(self.hubs)  # read
        await asyncio.sleep(0)  # the channel-creation round trip
        self._created += 1
        snapshot.append(
            _ar_hub(
                hub_id="new%d" % self._created,
                hub_channel_id=4240 + self._created,
                label=label,
            )
        )
        self.hubs = snapshot  # write-back from a possibly stale snapshot
        return "Created the **%s** hub." % label


class FakeRoomsPermissions:
    def __init__(self, manage_channels=True):
        self.manage_channels = manage_channels


class FakeRoomsMe:
    def __init__(self, manage_channels=True):
        self.guild_permissions = FakeRoomsPermissions(manage_channels)


class FakeRoomsGuild:
    """The live guild object the executor hands straight to the cog."""

    def __init__(
        self,
        guild_id=100,
        has_me=True,
        manage_channels=True,
        preferred_locale="en",
    ):
        self.id = guild_id
        self.me = FakeRoomsMe(manage_channels) if has_me else None
        self.preferred_locale = preferred_locale


def _ar_hub(hub_id="abc12345", hub_channel_id=555, label="Ranked"):
    """A normalised hub dict, in the shape tools/autoroom.default_hub produces."""
    return {
        "id": hub_id,
        "label": label,
        "category_id": 444,
        "hub_channel_id": hub_channel_id,
        "template": "{user}'s room",
        "user_limit": 4,
        "max_rooms": 20,
        "private": False,
    }


def _ar_bot(pool=None, guild=None, cog=None):
    """Bot with a live guild 100 + the TemporaryRooms cog, unless told otherwise."""
    guilds = {} if guild is False else {100: guild if guild else FakeRoomsGuild()}
    cogs = {} if cog is None else {"TemporaryRooms": cog}
    return FakeBot(pool or ActionsPool(), guilds=guilds, cogs=cogs)


def _hub_payload(**overrides):
    payload = {
        "label": "Ranked",
        "category_name": "RANKED ROOMS",
        "hub_name": "Join to create",
        "template": "{user}'s room",
        "user_limit": 4,
    }
    payload.update(overrides)
    return payload


async def test_autoroom_hub_create_calls_add_hub_with_exact_kwargs():
    cog = FakeRoomsCog()
    guild = FakeRoomsGuild()
    bot = _ar_bot(guild=guild, cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload(label="  Ranked  ")  # stripped before the cog sees it
    )

    assert cog.add_calls == [
        {
            "guild": guild,  # the LIVE guild object, resolved from the claimed id
            "label": "Ranked",
            "category_name": "RANKED ROOMS",
            "hub_name": "Join to create",
            "template": "{user}'s room",
            "user_limit": 4,
        }
    ]
    # ok is derived from the hub actually appearing in the saved list, not from
    # the cog's (translated) message - which is passed through for display.
    assert result["ok"] is True
    assert result["hub_id"] == FakeRoomsCog.CREATED_HUB_ID
    assert result["hub_channel_id"] == "4242"  # snowflake as a STRING
    assert "Created the **Ranked** hub" in result["message"]


async def test_autoroom_hub_create_reports_refusal_as_create_failed():
    # The cog refused (a budget check) and saved nothing: no new hub id appears,
    # so the structural check reports failure and forwards the reason.
    cog = FakeRoomsCog(refuse_add=True)
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result["ok"] is False
    assert result["error"] == "create_failed"
    assert result["message"] == "This server is at Discord's limit of 50 categories."
    assert len(cog.add_calls) == 1  # it WAS attempted, just not saved


async def test_autoroom_hub_create_enforces_max_hubs_before_creating():
    # At MAX_HUBS the executor refuses BEFORE the cog can create any channel
    # (mirrors the _exec_role_menu_post cap gate).
    cog = FakeRoomsCog(
        hubs=[_ar_hub(hub_id="hub%d" % i, hub_channel_id=500 + i) for i in range(5)]
    )
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result == {"ok": False, "error": "too_many_hubs"}
    assert cog.add_calls == []


@pytest.mark.parametrize(
    "field, error",
    [
        ("label", "bad_label"),
        ("category_name", "bad_category_name"),
        ("hub_name", "bad_hub_name"),
        ("template", "bad_template"),
    ],
)
@pytest.mark.parametrize("value", [None, "", "   ", 42, "x" * 101])
async def test_autoroom_hub_create_rejects_bad_text(field, error, value):
    """Missing / empty / blank / non-string / over-100-char fields are refused."""
    cog = FakeRoomsCog()
    bot = _ar_bot(cog=cog)
    payload = _hub_payload()
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    result = await dashboard_actions._exec_autoroom_hub_create(bot, 100, payload)

    assert result == {"ok": False, "error": error}
    # Validated BEFORE the cog is touched: no channel is ever created.
    assert cog.add_calls == []
    assert cog.loads == []


@pytest.mark.parametrize("value", [None, "abc", "", -1, 100, True, "4.5", {}, [4]])
async def test_autoroom_hub_create_rejects_bad_user_limit(value):
    cog = FakeRoomsCog()
    bot = _ar_bot(cog=cog)
    payload = _hub_payload()
    if value is None:
        payload.pop("user_limit")
    else:
        payload["user_limit"] = value

    result = await dashboard_actions._exec_autoroom_hub_create(bot, 100, payload)

    assert result == {"ok": False, "error": "bad_user_limit"}
    assert cog.add_calls == []
    assert cog.loads == []


@pytest.mark.parametrize("user_limit", [0, 99, "12"])
async def test_autoroom_hub_create_accepts_in_range_user_limit(user_limit):
    cog = FakeRoomsCog()
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload(user_limit=user_limit)
    )

    assert result["ok"] is True
    assert cog.add_calls[0]["user_limit"] == int(user_limit)


async def test_autoroom_hub_create_guild_unavailable():
    cog = FakeRoomsCog()
    bot = _ar_bot(guild=False, cog=cog)  # bot is not in guild 100
    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert cog.add_calls == []


async def test_autoroom_hub_create_without_cog():
    bot = _ar_bot(cog=None)  # TemporaryRooms not loaded: the bot cannot act
    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )
    assert result == {"ok": False, "error": "guild_unavailable"}


async def test_autoroom_hub_create_full_flow_via_handle_action():
    """End-to-end through the queue: claim -> create executor -> done + result."""
    cog = FakeRoomsCog()
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="autoroom_hub_create", payload=_hub_payload())
    bot = FakeBot(pool, guilds={100: FakeRoomsGuild()}, cogs={"TemporaryRooms": cog})

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"]["ok"] is True
    assert pool.rows[1]["result"]["hub_id"] == FakeRoomsCog.CREATED_HUB_ID
    assert len(cog.add_calls) == 1


async def test_autoroom_hub_delete_calls_remove_hub():
    guild = FakeRoomsGuild()
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")])
    bot = _ar_bot(guild=guild, cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "  abc12345  "}  # stripped before the cog sees it
    )

    assert result["ok"] is True
    assert result["hub_id"] == "abc12345"
    assert result["message"] == "Removed the **Ranked** hub."
    assert cog.remove_calls == [(guild, "abc12345")]


async def test_autoroom_hub_delete_unknown_hub_is_not_found():
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")])
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "deadbeef"}  # not one of THIS guild's hubs
    )

    assert result == {"ok": False, "error": "hub_not_found"}
    assert cog.remove_calls == []


@pytest.mark.parametrize("hub_id", [None, "", "   ", 42, ["abc"], "x" * 65])
async def test_autoroom_hub_delete_bad_hub_id(hub_id):
    cog = FakeRoomsCog(hubs=[_ar_hub()])
    bot = _ar_bot(cog=cog)
    payload = {} if hub_id is None else {"hub_id": hub_id}

    result = await dashboard_actions._exec_autoroom_hub_delete(bot, 100, payload)

    assert result == {"ok": False, "error": "bad_hub_id"}
    assert cog.remove_calls == []
    assert cog.loads == []  # rejected before the cog is touched


async def test_autoroom_hub_delete_guild_unavailable():
    cog = FakeRoomsCog(hubs=[_ar_hub()])
    bot = _ar_bot(guild=False, cog=cog)
    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert cog.remove_calls == []


async def test_autoroom_hub_delete_without_cog():
    bot = _ar_bot(cog=None)
    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )
    assert result == {"ok": False, "error": "guild_unavailable"}


async def test_autoroom_hub_delete_full_flow_via_handle_action():
    """End-to-end through the queue: claim -> delete executor -> done + result."""
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")])
    pool = ActionsPool()
    pool.add(
        1, guild_id=100, kind="autoroom_hub_delete", payload={"hub_id": "abc12345"}
    )
    bot = FakeBot(pool, guilds={100: FakeRoomsGuild()}, cogs={"TemporaryRooms": cog})

    status = await dashboard_actions.handle_action(bot, 1)

    assert status == "done"
    assert pool.rows[1]["result"]["ok"] is True
    assert len(cog.remove_calls) == 1
    assert cog.hubs == []


# ---------------------------------------------------------------------------
# Autoroom concurrency / freshness / permissions: both executors do a
# read-modify-write of the single 'autorooms' JSONB blob, on a background task,
# against a cache the dashboard's Node process also writes.
# ---------------------------------------------------------------------------


async def test_autoroom_hub_create_invalidates_cache_before_reading(monkeypatch):
    """The settings LRU may hold a blob the dashboard has since overwritten, so
    the guild's entry is dropped BEFORE the first _load_hubs, not after."""
    cog = FakeRoomsCog()
    bot = _ar_bot(cog=cog)
    seen = []
    monkeypatch.setattr(
        settings,
        "invalidate_guild",
        lambda gid: seen.append((gid, list(cog.loads))),
    )

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result["ok"] is True
    # Exactly one invalidation, for THIS guild, with no hub read done yet.
    assert seen == [(100, [])]


async def test_autoroom_hub_delete_invalidates_cache_before_reading(monkeypatch):
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")])
    bot = _ar_bot(cog=cog)
    seen = []
    monkeypatch.setattr(
        settings,
        "invalidate_guild",
        lambda gid: seen.append((gid, list(cog.loads))),
    )

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )

    assert result["ok"] is True
    assert seen == [(100, [])]


async def test_autoroom_hub_create_serialises_concurrent_actions_for_one_guild():
    """Two creates for the same guild must not lose a hub.

    Each notification is handled in its own task, so without the per-guild lock
    the second create saves a hub list snapshotted before the first one's save:
    the first hub vanishes from the config while its category and voice trigger
    stay alive on Discord.
    """
    cog = SlowRoomsCog()
    bot = _ar_bot(cog=cog)

    results = await asyncio.gather(
        dashboard_actions._exec_autoroom_hub_create(
            bot, 100, _hub_payload(label="One")
        ),
        dashboard_actions._exec_autoroom_hub_create(
            bot, 100, _hub_payload(label="Two")
        ),
    )

    assert [r["ok"] for r in results] == [True, True]
    # Both hubs survived, and each executor reported the hub IT created.
    assert len(cog.hubs) == 2
    assert {h["label"] for h in cog.hubs} == {"One", "Two"}
    assert [r["hub_id"] for r in results] == ["new1", "new2"]
    assert {r["hub_id"] for r in results} == {h["id"] for h in cog.hubs}


class InterleavedRoomsCog(FakeRoomsCog):
    """A cog whose save lands after ANOTHER writer added a hub of its own.

    The dashboard's Node process writes the same 'autorooms' blob, so the
    reloaded list can hold more than one previously-unseen id. ``_add_hub``
    APPENDS the hub it just created, so the LAST unseen entry is ours.
    """

    async def _add_hub(self, guild, **kwargs):
        self.hubs.append(
            _ar_hub(hub_id="foreign1", hub_channel_id=7777, label="Elsewhere")
        )
        return await super()._add_hub(guild, **kwargs)


async def test_autoroom_hub_create_reports_the_hub_it_created():
    """With another writer's hub also unseen, the result must name OURS - the
    dashboard uses hub_id/hub_channel_id to link straight to the new hub."""
    cog = InterleavedRoomsCog()
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result["ok"] is True
    assert result["hub_id"] == FakeRoomsCog.CREATED_HUB_ID
    assert result["hub_channel_id"] == "4242"
    assert {h["id"] for h in cog.hubs} == {"foreign1", FakeRoomsCog.CREATED_HUB_ID}


@pytest.mark.parametrize("has_me, manage_channels", [(False, True), (True, False)])
async def test_autoroom_hub_create_requires_manage_channels(has_me, manage_channels):
    """Parity with the /autoroom group's bot_has_permissions gate: without
    manage_channels the cog's channel creation would only raise Forbidden."""
    cog = FakeRoomsCog()
    guild = FakeRoomsGuild(has_me=has_me, manage_channels=manage_channels)
    bot = _ar_bot(guild=guild, cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result == {"ok": False, "error": "missing_manage_channels"}
    assert cog.add_calls == []
    assert cog.loads == []  # refused before the cog is touched at all


@pytest.mark.parametrize("has_me, manage_channels", [(False, True), (True, False)])
async def test_autoroom_hub_delete_requires_manage_channels(has_me, manage_channels):
    """A delete on a LIVE hub must NOT report ok when the bot cannot delete the
    channels: the cog swallows every Forbidden, so the config row would be
    dropped while the category and its rooms stayed alive on Discord."""
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")])
    guild = FakeRoomsGuild(has_me=has_me, manage_channels=manage_channels)
    bot = _ar_bot(guild=guild, cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )

    assert result == {"ok": False, "error": "missing_manage_channels"}
    assert result.get("ok") is not True
    assert cog.remove_calls == []
    assert cog.hubs  # the hub is still configured, matching Discord


async def test_autoroom_hub_messages_render_in_the_guild_locale(monkeypatch):
    """The cog translates with _() against the ambient locale; a queue task has
    none, so both executors must resolve and apply the guild's language."""
    calls = []

    async def _spy(bot, guild):
        calls.append(guild)
        return "fr"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _spy)

    guild = FakeRoomsGuild()
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")])
    bot = _ar_bot(guild=guild, cog=cog)

    await dashboard_actions._exec_autoroom_hub_delete(bot, 100, {"hub_id": "abc12345"})
    await dashboard_actions._exec_autoroom_hub_create(bot, 100, _hub_payload())

    assert calls == [guild, guild]  # resolved from the LIVE guild both times
    assert cog.locales == ["fr", "fr"]
    # ... and the locale is scoped to the call, never leaked into the task.
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE


def test_autoroom_hub_executors_are_registered():
    assert "autoroom_hub_create" in dashboard_actions._EXECUTORS
    assert "autoroom_hub_delete" in dashboard_actions._EXECUTORS


# ---------------------------------------------------------------------------
# The listen connection's own reconnect: the sweep is due again, not just at boot.
#
# The cousin of the cache-sync bug. LISTEN/NOTIFY does not buffer, so an action
# INSERTed and notified while THIS connection was down is lost exactly like a
# restart loses one - but until now the sweep only ever ran at boot, so such a
# row sat 'pending' until the next restart (i.e. possibly for days, or never).
# ---------------------------------------------------------------------------


class StubActions(dashboard_actions.DashboardActions):
    """The REAL supervisor loop with only the socket-touching seams stubbed.

    ``__init__`` is bypassed on purpose (it opens a connection); every attribute
    the loop touches is set here, so what is under test is the actual
    ``_supervise`` / ``_maybe_reconcile`` wiring rather than a paraphrase of it.
    """

    def __init__(self, bot, cycles=1):
        self.bot = bot
        self._conn = None
        self._closing = False
        self._supervisor = None
        self._reconciled = False
        self._connected_once = False
        self._reconcile_task = None
        self._handlers = set()
        self._dsn = "postgresql://stub"
        self._cycles = cycles
        self.events = []

    async def _connect_and_listen(self):
        self.events.append("listen")
        self._connected_once = True

    async def _watch_connection(self):
        self.events.append("watch")
        self._cycles -= 1
        if self._cycles <= 0:
            self._closing = True

    async def _teardown_connection(self):
        self._conn = None


def _supervised_bot(pool):
    bot = FakeBot(pool)
    bot.loop = asyncio.get_running_loop()

    async def _ready():
        return None

    bot.wait_until_ready = _ready
    return bot


async def _drain(cog):
    """Let the tasks the supervisor scheduled finish."""
    if cog._handlers:
        await asyncio.gather(*list(cog._handlers))


async def test_first_connection_reconciles_exactly_once(monkeypatch):
    calls = []

    async def _fake_reconcile(bot):
        calls.append(bot)

    monkeypatch.setattr(dashboard_actions, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_actions, "reconcile", _fake_reconcile)

    cog = StubActions(_supervised_bot(ActionsPool()), cycles=1)
    await cog._supervise()
    await _drain(cog)

    assert cog.events == ["listen", "watch"]
    assert len(calls) == 1


async def test_a_reconnect_re_runs_the_reconcile_sweep(monkeypatch, caplog):
    """The fix: a second successful connect means a delivery gap just closed."""
    calls = []

    async def _fake_reconcile(bot):
        calls.append(bot)

    monkeypatch.setattr(dashboard_actions, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_actions, "reconcile", _fake_reconcile)

    cog = StubActions(_supervised_bot(ActionsPool()), cycles=3)
    with caplog.at_level(logging.INFO, logger=dashboard_actions.log.name):
        await cog._supervise()
        await _drain(cog)

    # Three connects: boot + two reconnects = exactly one sweep per connect.
    assert cog.events == ["listen", "watch"] * 3
    assert len(calls) == 3
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "reconcile sweep is re-running" in message


async def test_a_connect_then_die_loop_never_piles_up_sweeps(monkeypatch):
    """One sweep in flight at a time, whatever the reconnect rate.

    The backoff resets on every SUCCESSFUL connect, so a server that accepts the
    connection then immediately kills it (pgbouncer in transaction mode refusing
    LISTEN, connection churn) cycles about once a second. Without this guard each
    cycle would launch another full sweep - expire, orphan reset and a re-drive
    of every pending row - at exactly the moment the database is least able to
    take it. Concurrent sweeps stay CORRECT (every claim is an atomic
    ``WHERE status='pending' RETURNING``); they are simply a self-inflicted
    storm.
    """
    started = 0
    release = asyncio.Event()

    async def _slow_reconcile(bot):
        nonlocal started
        started += 1
        await release.wait()

    monkeypatch.setattr(dashboard_actions, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_actions, "reconcile", _slow_reconcile)

    cog = StubActions(_supervised_bot(ActionsPool()), cycles=5)
    await cog._supervise()

    assert cog.events.count("listen") == 5
    assert started == 1  # the four later connects found one already running

    release.set()
    await _drain(cog)
    assert started == 1


async def test_a_failed_connect_does_not_schedule_a_reconnect_sweep(monkeypatch):
    """``_connected_once`` flips only after add_listener succeeded, so a bot that
    never reached Postgres still treats its first real connect as the boot one."""
    calls = []

    async def _fake_reconcile(bot):
        calls.append(bot)

    monkeypatch.setattr(dashboard_actions, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(dashboard_actions, "reconcile", _fake_reconcile)

    class Flaky(StubActions):
        async def _connect_and_listen(self):
            if not self.events:
                self.events.append("failed")
                raise OSError("connection refused")
            await super()._connect_and_listen()

    cog = Flaky(_supervised_bot(ActionsPool()), cycles=1)
    await cog._supervise()
    await _drain(cog)

    assert cog.events == ["failed", "listen", "watch"]
    assert len(calls) == 1  # the boot sweep, not a reconnect one


async def test_the_reconnect_sweep_drives_an_action_enqueued_during_the_gap(
    monkeypatch,
):
    """End to end through the real sweep: the row the dropped notify stranded.

    Without this the user's request would sit 'pending' until the next restart,
    which is the whole point of the lot.
    """
    ran = []

    async def _exec(bot, guild_id, payload):
        ran.append(payload.get("tag"))
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    monkeypatch.setattr(dashboard_actions, "_BACKOFF_START", 0.0)

    pool = ActionsPool()
    bot = _supervised_bot(pool)
    swept = []

    class Enqueueing(StubActions):
        async def _connect_and_listen(self):
            if self._connected_once:
                # The dashboard INSERTed + notified while this process was NOT
                # listening: the notify is gone, and the BOOT sweep has already
                # run and seen nothing. Only a reconnect sweep can find this row.
                assert swept == [[]]
                pool.add(1, 100, "test_kind", {"tag": "during-gap"}, status="pending")
            await super()._connect_and_listen()

    real_reconcile = dashboard_actions.reconcile

    async def _recording_reconcile(inner_bot):
        swept.append(sorted(pool.rows))
        await real_reconcile(inner_bot)

    monkeypatch.setattr(dashboard_actions, "reconcile", _recording_reconcile)

    cog = Enqueueing(bot, cycles=2)
    await cog._supervise()
    await _drain(cog)

    # The boot sweep found an empty table; the reconnect sweep found the row.
    assert swept == [[], [1]]
    assert ran == ["during-gap"]
    assert pool.rows[1]["status"] == "done"


async def test_the_reconnect_sweep_respects_the_in_flight_guard(monkeypatch):
    """A runtime sweep is riskier than a boot one: this process has live work.

    An executor that outlived the gap has a claim that is BOTH live and old, so
    the age guard alone would read it as an orphan and step 3 would re-run its
    side effect. The in-flight mark is what tells them apart, and the reconnect
    path must go through it - hence driving the REAL reconcile here.
    """
    ran = []

    async def _exec(bot, guild_id, payload):
        ran.append(payload.get("tag"))
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    monkeypatch.setattr(dashboard_actions, "_BACKOFF_START", 0.0)

    pool = ActionsPool()
    # A row THIS process claimed before the gap and is still working on: running,
    # and old enough (fresh_claim=False) that the age guard would let it through.
    pool.add(1, 100, "test_kind", {"tag": "still-running"}, status="running")
    bot = _supervised_bot(pool)

    cog = StubActions(bot, cycles=2)
    with dashboard_actions._inflight(1):
        await cog._supervise()
        await _drain(cog)

        assert dashboard_actions._INFLIGHT_ACTIONS == {1: 1}
        assert pool.rows[1]["status"] == "running"  # never reset to pending
        assert ran == []  # and never re-driven

    # The mark is released with the block, exactly as a real handler releases it.
    assert dashboard_actions._INFLIGHT_ACTIONS == {}
