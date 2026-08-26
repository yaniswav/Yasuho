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
import inspect
import json
import logging
import types

import discord
import pytest

from cogs.system import dashboard_actions
from tools import autoroom, i18n, settings

# The id the fake action rows carry in ``requested_by``: the authenticated
# dashboard user who asked, and now the ACTOR whose rank gates the four
# role-publishing kinds.
ACTOR_ID = 4242


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
        requested_by=ACTOR_ID,
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
        # ``requested_by`` is the ACTOR column: the authenticated dashboard user
        # who asked. It defaults to ACTOR_ID (a row the dashboard wrote normally)
        # so only the tests that are ABOUT a missing actor have to say so.
        self.rows[action_id] = {
            "guild_id": guild_id,
            "user_id": user_id,
            "requested_by": requested_by,
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
            # Same discipline as user_id: the column is only visible to the
            # dispatcher when the RETURNING actually lists it, so dropping it
            # from the claim SQL turns every actor-gated action into a refusal
            # instead of silently passing.
            if "requested_by" in query:
                claimed["requested_by"] = row["requested_by"]
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
            # The in-flight mark excludes ids THIS process is handling, whatever
            # their age - the same guard step 2 carries, modelled from the
            # `id = ANY($3)` clause and its parameter so a test of a long-running
            # executor caught by a reconnect-time reconcile stays red without it.
            _stale_minutes, result_json = args[0], args[1]
            mark_guarded = "id = ANY(" in query
            inflight = set(args[2]) if mark_guarded and len(args) > 2 else set()
            for action_id, row in self.rows.items():
                if action_id in inflight:
                    continue
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
        self.calls.append(("fetchval", query, args))
        if "INSERT INTO reaction_roles" in query:
            # The guild-scoped upsert RETURNS the role it wrote (None would mean
            # the row belongs to another guild); args are
            # (message_id, emoji, role_id, guild_id).
            return args[2]
        # Otherwise only reached via settings.get_guild inside
        # resolve_guild_locale; an unconfigured guild reads no locale row.
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


# ---------------------------------------------------------------------------
# The dashboard ACTOR: the Member behind a row's ``requested_by``, resolved by
# the dispatcher and handed to the five role-publishing executors.
# ---------------------------------------------------------------------------

class FakeActor:
    """Stand-in for the actor Member: exactly what the configurer half reads.

    ``modchecks.role_hierarchy_error_for`` asks three things and nothing else -
    the actor's id (against ``guild.owner_id``), whether they are an
    Administrator, and their top role - so the fake carries three attributes.
    """

    def __init__(self, top_role, user_id=ACTOR_ID, administrator=False):
        self.id = user_id
        self.top_role = top_role
        self.guild_permissions = types.SimpleNamespace(administrator=administrator)


def _actor_with(top_role, **kwargs):
    return FakeActor(top_role, **kwargs)


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
    def __init__(
        self,
        channels=None,
        has_me=True,
        preferred_locale="en",
        roles=None,
        members=None,
        owner_id=111,
    ):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        # The member CACHE, deliberately sparse: only what get_member answers.
        self._members = dict(members or {})
        # A real member object rather than a bare sentinel: when a verify_role IS
        # configured the executor asks whether Yasuho can hand it out, which
        # reads her top role. (_fake_me lives with the reaction-role fakes below;
        # it is called here at instantiation time, not at import.)
        self.me = _fake_me() if has_me else None
        self.preferred_locale = preferred_locale
        self.owner_id = owner_id

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, user_id):
        return self._members.get(user_id)


class FakeVerifyView:
    """Stand-in for the persistent VerifyView (avoids importing discord.ui)."""

    instances = 0

    def __init__(self):
        FakeVerifyView.instances += 1


def _verify_actor(top_position=900, **kwargs):
    """The actor for a verify_button_post call.

    Only consulted when a ``verify_role`` is configured (the button is a
    self-grant of THAT role); ranked above everything by default so the tests
    that are not about the gate are unaffected. FakeRole is defined further down
    with the reaction-role fakes - resolved at call time, not at import.
    """
    return _actor_with(FakeRole(7_000, position=top_position), **kwargs)


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
        bot, 100, {"channel_id": "555"},
        _verify_actor(),
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
        bot, 100, {"channel_id": "555", "message": "Welcome! Tap to verify."},
        _verify_actor(),
    )

    _, kwargs = channel.sent[0]
    assert kwargs["embed"].description == "Welcome! Tap to verify."


async def test_verify_button_post_guild_unavailable(verify_env):
    bot = FakeBot(ActionsPool(), guilds={})  # bot is not in guild 100
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"},
        _verify_actor(),
    )
    assert result == {"ok": False, "error": "guild_unavailable"}


async def test_verify_button_post_channel_not_found(verify_env):
    guild = FakeGuild(channels={})  # channel 555 does not exist
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"},
        _verify_actor(),
    )
    assert result == {"ok": False, "error": "channel_not_found"}


async def test_verify_button_post_rejects_non_text_channel(verify_env):
    guild = FakeGuild(channels={555: FakeVoiceChannel(555)})
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"},
        _verify_actor(),
    )
    assert result == {"ok": False, "error": "not_text_channel"}


async def test_verify_button_post_missing_send_permission(verify_env):
    channel = FakeTextChannel(channel_id=555, can_send=False)
    guild = FakeGuild(channels={555: channel})
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"},
        _verify_actor(),
    )
    assert result == {"ok": False, "error": "missing_send_permission"}
    assert channel.sent == []  # nothing posted


@pytest.mark.parametrize("channel_id", [None, "abc", "", "not-a-number"])
async def test_verify_button_post_bad_channel_id(verify_env, channel_id):
    guild = FakeGuild(channels={})
    bot = FakeBot(ActionsPool(), guilds={100: guild})
    payload = {} if channel_id is None else {"channel_id": channel_id}
    result = await dashboard_actions._exec_verify_button_post(bot, 100, payload, _verify_actor())
    assert result == {"ok": False, "error": "bad_channel_id"}


async def test_verify_button_post_full_flow_via_handle_action(verify_env):
    """End-to-end through the queue: claim -> verify executor -> done + result.

    The row carries the default ``requested_by`` and the actor is in the member
    cache, so the dispatcher's actor gate resolves without a fetch.
    """
    channel = FakeTextChannel(channel_id=555)
    guild = FakeGuild(
        channels={555: channel}, members={ACTOR_ID: _verify_actor()}
    )
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
    """Minimal pool that records reaction_roles writes for the executor tests.

    ``delete_status`` is what the guild-scoped DELETE reports back: "DELETE 1"
    (a row of THIS guild matched) or "DELETE 0" (nothing matched - the row
    belongs to another guild, or is already gone). A fake that always answered
    "DELETE 1" made the cross-tenant cache-eviction bug untestable, which is
    exactly how it shipped: the executor discarded the status entirely.

    ``upsert_result`` is what the guild-scoped UPSERT returns: the written
    role_id, or None when the (message_id, emoji) row is owned by ANOTHER guild
    and the ``ON CONFLICT ... WHERE guild_id = EXCLUDED.guild_id`` branch was
    skipped. Same lesson as ``delete_status``: a fake that always reported
    success made the cross-tenant overwrite untestable.
    """

    def __init__(self, delete_status="DELETE 1", upsert_result=888):
        self.executed = []
        self.delete_status = delete_status
        self.upsert_result = upsert_result

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if "DELETE FROM reaction_roles" in query:
            return self.delete_status
        return "INSERT 0 1"

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if "INSERT INTO reaction_roles" in query:
            return self.upsert_result
        return None


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
    """A guild role plus the three things an assignability guard reads.

    ``position`` drives the ``role < me.top_role`` comparison (roles order by
    position), ``managed`` marks an integration-owned role and ``is_default()``
    marks @everyone - none of those can ever be handed out.
    """

    def __init__(self, role_id=888, position=1, managed=False, default=False):
        self.id = role_id
        self.position = position
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        # Faithful to discord.Role.__lt__, tiebreak included: equal positions are
        # ordered by id, and the OLDER role (smaller id) is the higher one. The
        # at-bot-top case relies on that tiebreak rather than on position alone.
        if self.position != other.position:
            return self.position < other.position
        return self.id > other.id

    def __ge__(self, other):
        # The configurer half compares ``role >= actor.top_role``; on a total
        # order that is exactly "not below". A role is >= itself (equal position,
        # equal id -> __lt__ is False), like discord.Role.
        return not self.__lt__(other)


# Yasuho's own member object: only her top role matters to the guards. Its id is
# deliberately LARGER than every role the tests compare against, because a bot's
# managed role is created when it is invited - later than the roles that were
# already there - and at equal positions the younger (bigger id) role is the
# LOWER one. That is what makes the at-bot-top case a refusal.
def _fake_me(top_position=10):
    return types.SimpleNamespace(top_role=FakeRole(999_000, position=top_position))


class FakeReactionGuild:
    def __init__(self, channels=None, roles=None, has_me=True, members=None,
                 owner_id=111):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        self._members = dict(members or {})
        self.me = _fake_me() if has_me else None
        self.owner_id = owner_id

    def get_member(self, user_id):
        return self._members.get(user_id)

    def get_channel_or_thread(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


def _role_actor(top_position=900, **kwargs):
    """The actor for the reaction-role and role-menu executors (shared FakeRole).

    Default rank is far above every role these tests publish (and above Yasuho's
    own top role at position 10), so the configurer half passes unless a test
    deliberately lowers it.
    """
    return _actor_with(FakeRole(7_000, position=top_position), **kwargs)


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
        _role_actor(),
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
        _role_actor(),
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
        _role_actor(),
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
        _role_actor(),
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
        _role_actor(),
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
        _role_actor(),
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
        _role_actor(),
    )
    assert result == {"ok": False, "error": "bad_role"}
    assert pool.executed == []


@pytest.mark.parametrize(
    "role",
    [
        FakeRole(888, position=10),  # exactly the bot's top role
        FakeRole(888, position=11),  # above the bot's top role
        FakeRole(888, position=1, managed=True),  # integration-owned
        FakeRole(888, position=1, default=True),  # @everyone
    ],
    ids=["at-bot-top", "above-bot-top", "managed", "everyone"],
)
async def test_reaction_role_add_refuses_unassignable_role(role):
    """A role Yasuho could never hand out must not become a mapping.

    Without this the dashboard could persist a mapping whose grant 403s on every
    single reaction, and leave a stray bot reaction on the message advertising a
    role nobody can get. The message is the SAME guard the button-panel executor
    and the /buttonrole builder already apply.
    """
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(channels={555: channel}, roles={888: role})
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
        _role_actor(),
    )

    assert result == {
        "ok": False,
        "error": "role_not_assignable",
        "failures": [{"role_id": "888", "reason": "role_not_assignable"}],
    }
    # Refused BEFORE the reaction: no row, no cache entry, no stray reaction.
    assert pool.executed == []
    assert cog.cache == {}
    assert channel.message.reactions == []


async def test_reaction_role_add_assignable_role_below_the_bot_still_works():
    """Counter-test to the guard: a normal role under the bot is still mapped."""
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(
        channels={555: channel}, roles={888: FakeRole(888, position=9)}
    )
    cog = FakeCog()
    pool = RRPool()
    bot = _rr_bot(pool, guild, cog)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
        _role_actor(),
    )

    assert result["ok"] is True
    assert cog.cache[(777, "🎮")] == 888


async def test_reaction_role_add_without_me_is_unavailable():
    """No guild.me = nothing to compare the role against; refuse, never crash."""
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(
        channels={555: channel}, roles={888: FakeRole(888)}, has_me=False
    )
    pool = RRPool()
    bot = _rr_bot(pool, guild, FakeCog())

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "🎮", "role_id": "888"},
        _role_actor(),
    )

    assert result == {"ok": False, "error": "guild_unavailable"}
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
        _role_actor(),
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
    result = await dashboard_actions._exec_reaction_role_add(bot, 100, payload, _role_actor())
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
    result = await dashboard_actions._exec_reaction_role_add(bot, 100, payload, _role_actor())
    assert result == {"ok": False, "error": "bad_emoji"}
    assert pool.executed == []


async def test_reaction_role_add_full_flow_via_handle_action():
    """End-to-end through the queue: claim -> add executor -> done + result + cache."""
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(
        channels={555: channel},
        roles={888: FakeRole(888)},
        members={ACTOR_ID: _role_actor()},
    )
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


async def test_reaction_role_remove_keeps_cache_when_no_row_matched():
    """DELETE 0 = the mapping is NOT this guild's, so the cache must not move.

    The cache key is ``(message_id, emoji)`` with NO guild in it. A manage-guild
    user of guild B who fires this executor with guild A's message id gets a
    no-op DELETE (the WHERE is guild-scoped) - but an unconditional pop would
    still kill guild A's LIVE mapping while its row survives, so the breakage
    would last until the next restart and look like nothing at all in the table.
    """
    cog = FakeCog()
    cog.cache[(777, "🎮")] = 888  # guild A's live mapping
    pool = RRPool(delete_status="DELETE 0")
    bot = _rr_bot(pool, guild=None, cog=cog)

    result = await dashboard_actions._exec_reaction_role_remove(
        bot, 100, {"message_id": "777", "emoji": "🎮"}
    )

    assert result == {"ok": True}
    assert pool.executed[0][1] == (777, "🎮", 100)  # still guild-scoped
    assert cog.cache == {(777, "🎮"): 888}  # untouched


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


async def test_reconcile_does_not_expire_a_long_running_action_this_process_holds():
    """Step 1 must carry the same in-flight guard as step 2.

    A reconnect-time reconcile (not just boot) can run while a legitimately long
    executor - mydata_export packing and uploading an archive - is still alive on
    a row older than the stale window. Without the in-flight exclusion on step 1,
    it stamps that live row failed/expired out from under its own executor: the
    dashboard shows "expired" for work in progress and a retry hits the taken
    cooldown. Boot is unaffected (the set is empty there, so a genuine orphan of
    a dead previous process still expires - covered by the sweep test above).
    """
    pool = ActionsPool()
    pool.add(1, None, "mydata_export", {}, status="running", stale=True, user_id=100)
    bot = FakeBot(pool)

    # This process is actively handling id 1 (the mark is up from before the
    # claim until after the terminal write, per _inflight's contract).
    with dashboard_actions._inflight(1):
        await dashboard_actions.reconcile(bot)

    # The live row is untouched - not expired out from under the executor.
    assert pool.rows[1]["status"] == "running"
    assert pool.rows[1]["result"] is None


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


def _br_actor(top_position=900, **kwargs):
    """The actor for the button-panel executor (BRRole ordering)."""
    return _actor_with(BRRole(7000, "Staff", position=top_position), **kwargs)


class BRMe:
    """Stand-in for guild.me: only needs a top_role to compare against."""

    def __init__(self, top_role_position=1000):
        self.top_role = BRRole(0, "Bot", position=top_role_position)


class BRGuild:
    def __init__(self, channels=None, roles=None, has_me=True, members=None,
                 owner_id=111):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        self._members = dict(members or {})
        self.me = BRMe() if has_me else None
        self.owner_id = owner_id

    def get_member(self, user_id):
        return self._members.get(user_id)

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
    """Pool modelling the acquire()/transaction() persist path, the scoped
    DELETE ... RETURNING of the delete executor and the scoped ownership SELECT
    of the edit one.

    ``inserted`` / ``deleted`` are the ONLY two ways this module writes
    ``button_roles``, so a test proving "the rows were left untouched" asserts
    both are empty.

    ``panel_rows`` are the rows STORED for the panel. The edit executor no longer
    reads their content - only whether any exist and which channel they name - so
    the SELECT it issues is ``SELECT DISTINCT channel_id``, and this fake
    collapses duplicates the way Postgres would. Returning the raw list instead
    would let a two-button panel look like rows that disagree on the channel.
    """

    def __init__(self, delete_return=None, panel_rows=None):
        self.inserted = []
        self.deleted = []
        self.delete_calls = []
        self.select_calls = []
        self._delete_return = delete_return or []
        self._panel_rows = list(panel_rows or [])

    def acquire(self):
        return _BRAcquire(self)

    async def fetch(self, query, *args):
        if "SELECT DISTINCT channel_id" in query:  # the edit executor's read
            self.select_calls.append((query, args))
            channels = []
            for row in self._panel_rows:
                if row["channel_id"] not in channels:
                    channels.append(row["channel_id"])
            return [{"channel_id": channel_id} for channel_id in channels]
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
        _br_actor(),
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
        _br_actor(),
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
        bot, 100, _panel_payload(buttons=[{"role_id": "888", "style": 2}]),
        _br_actor(),
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
        _br_actor(),
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
    result = await dashboard_actions._exec_button_panel_post(bot, 100, payload, _br_actor())
    assert result == {"ok": False, "error": "bad_channel_id"}
    assert pool.inserted == []


async def test_button_panel_post_guild_unavailable(button_env):
    pool = BRPool()
    bot = _br_bot(pool, guild=None)  # bot not in guild 100
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload(), _br_actor())
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.inserted == []


async def test_button_panel_post_channel_not_found(button_env):
    guild = BRGuild(channels={}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload(), _br_actor())
    assert result == {"ok": False, "error": "channel_not_found"}
    assert pool.inserted == []


async def test_button_panel_post_rejects_non_text_channel(button_env):
    guild = BRGuild(channels={555: FakeVoiceChannel(555)}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload(), _br_actor())
    assert result == {"ok": False, "error": "not_text_channel"}
    assert pool.inserted == []


async def test_button_panel_post_missing_send_permission(button_env):
    channel = FakeTextChannel(channel_id=555, can_send=False)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload(), _br_actor())
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
    result = await dashboard_actions._exec_button_panel_post(bot, 100, payload, _br_actor())
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
    result = await dashboard_actions._exec_button_panel_post(bot, 100, payload, _br_actor())
    assert result == {"ok": False, "error": "too_many_buttons"}
    assert channel.sent == []


async def test_button_panel_post_bad_role(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={})  # role 888 absent
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload(), _br_actor())
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
    result = await dashboard_actions._exec_button_panel_post(bot, 100, _panel_payload(), _br_actor())
    assert result == {
        "ok": False,
        "error": "role_not_assignable",
        "failures": [{"role_id": "888", "reason": "role_not_assignable"}],
    }
    assert channel.sent == []
    assert pool.inserted == []


async def test_button_panel_post_empty_embed(button_env):
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888)})
    pool = BRPool()
    bot = _br_bot(pool, guild)
    result = await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(embed={}),  # no visible content
        _br_actor(),
    )
    assert result == {"ok": False, "error": "empty_embed"}
    # Nothing posted, persisted or registered for an empty embed.
    assert channel.sent == []
    assert pool.inserted == []
    assert bot.added_views == []


async def test_button_panel_post_full_flow_via_handle_action(button_env):
    """End-to-end through the queue: claim -> post executor -> done + result."""
    channel = FakeTextChannel(channel_id=555)
    guild = BRGuild(
        channels={555: channel},
        roles={888: BRRole(888, "Gamer")},
        members={ACTOR_ID: _br_actor()},
    )
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
# button_panel_edit executor: re-render an existing panel's COMPONENTS in place
# (same message id) from the PAYLOAD's buttons - the same ``buttons`` shape
# button_panel_post takes - re-validating every role at publish time and refusing
# the whole edit if any of them fails. The stored rows are still read,
# guild-scoped, but ONLY to prove the message is a panel of this guild and to say
# which channel it lives in; their content is never rendered.
# ---------------------------------------------------------------------------


class BREditMessage:
    """The live panel message: records every edit() kwarg set it is given."""

    def __init__(self, message_id=777, fail_edit=False):
        self.id = message_id
        self.edits = []
        self._fail_edit = fail_edit

    async def edit(self, **kwargs):
        if self._fail_edit:
            raise RuntimeError("edit blew up")
        self.edits.append(kwargs)
        return self


class BREditChannel(FakeTextChannel):
    """A channel that can hand back the panel message (or fail to)."""

    def __init__(self, channel_id=555, message=None, fail_fetch=False):
        super().__init__(channel_id=channel_id)
        self.message = message
        self._fail_fetch = fail_fetch
        self.fetched = []

    async def fetch_message(self, message_id):
        self.fetched.append(message_id)
        if self._fail_fetch or self.message is None:
            raise RuntimeError("unknown message")
        return self.message


def _panel_row(role_id, label="Gamer", emoji=None, style=1, channel_id=555):
    """One STORED ``button_roles`` row, in the shape the SELECT returns.

    The edit executor reads nothing here but "do any rows exist" and "which
    channel", so the rest is the panel's OLD button set - the one the dashboard
    has not replaced yet, because it writes only after our ok. These tests
    deliberately keep it DIFFERENT from the payload.
    """
    return {
        "channel_id": channel_id,
        "role_id": role_id,
        "label": label,
        "emoji": emoji,
        "style": style,
    }


def _edit_button(role_id="888", label="Gamers", emoji=None, style=1):
    """One payload button, in the shape ``button_panel_post`` already accepts."""
    button = {"role_id": role_id, "style": style}
    if label is not None:
        button["label"] = label
    if emoji is not None:
        button["emoji"] = emoji
    return button


def _edit_payload(message_id="777", channel_id="555", buttons=None):
    """The payload: which message to re-render, where it lives, and the BUTTONS.

    The buttons are in the payload so the dashboard can write its rows AFTER our
    ok instead of before enqueuing - one shape, produced on both sides.
    """
    payload = {"message_id": message_id, "channel_id": channel_id}
    payload["buttons"] = [_edit_button()] if buttons is None else buttons
    return payload


def _edit_env(rows, roles, actor=None, message=None, channel=None):
    """A ready button_panel_edit call: pool rows + guild roles + live message."""
    message = message if message is not None else BREditMessage(777)
    channel = channel if channel is not None else BREditChannel(555, message=message)
    actor = actor if actor is not None else _br_actor()
    guild = BRGuild(
        channels={555: channel}, roles=roles, members={ACTOR_ID: actor}
    )
    pool = BRPool(panel_rows=rows)
    return channel, message, pool, actor, _br_bot(pool, guild)


def test_button_panel_edit_is_registered_and_actor_gated():
    """It PUBLISHES self-assignable roles, so it must take the resolved actor.

    Left out of _ACTOR_KINDS it would be the gate's back door: post a harmless
    role past the check, then edit the panel to republish a dangerous one
    unchecked.
    """
    assert "button_panel_edit" in dashboard_actions._EXECUTORS
    assert "button_panel_edit" in dashboard_actions._ACTOR_KINDS


async def test_button_panel_edit_rerenders_the_components_in_place(button_env):
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, "Old label")],  # the set the dashboard replaces
        roles={888: BRRole(888, "Gamer"), 999: BRRole(999, "Artist")},
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(
            buttons=[
                _edit_button("888", "Gamers", "\U0001F3AE", 1),
                _edit_button("999", "Artists", None, 3),
            ]
        ),
        actor,
    )

    assert result == {
        "ok": True,
        "message_id": "777",
        "channel_id": "555",
        "buttons": 2,
    }
    # The EXISTING message is edited - nothing is sent, so the id (and every
    # link pinned to it) survives.
    assert channel.sent == []
    assert channel.fetched == [777]
    assert len(message.edits) == 1
    # COMPONENTS ONLY: the edit names the view and NOTHING else, so the embed -
    # which is stored nowhere - is left exactly as it was.
    assert list(message.edits[0]) == ["view"]
    assert message.edits[0]["view"].rows == [
        (888, "Gamers", "\U0001F3AE", 1),
        (999, "Artists", None, 3),
    ]


async def test_button_panel_edit_never_writes_button_roles(button_env):
    """THE ORDERING THIS CONTRACT EXISTS FOR. The executor renders and answers;
    the dashboard writes its rows AFTER the ok. So a successful edit must leave
    ``button_roles`` untouched - if this executor wrote them, the writer would be
    back to writing before knowing."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, "Old label")], roles={888: BRRole(888, "Gamer")}
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), actor
    )

    assert result["ok"] is True
    assert pool.inserted == [] and pool.deleted == []


async def test_button_panel_edit_reregisters_the_persistent_view(button_env):
    """Without this the in-memory view would keep answering with the OLD button
    set until the next boot - the buttons on screen and the ones that work would
    be two different panels."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )

    await dashboard_actions._exec_button_panel_edit(bot, 100, _edit_payload(), actor)

    assert len(bot.added_views) == 1
    view, mid = bot.added_views[0]
    assert mid == 777  # the SAME message, not a new one
    assert view.rows == [(888, "Gamers", None, 1)]


async def test_button_panel_edit_takes_the_buttons_from_the_payload_not_the_rows(
    button_env,
):
    """THE CONTRACT FLIP. The stored rows still hold the panel's OLD button set
    at this instant - the dashboard replaces them only after our ok - so reading
    them would render the wrong panel. Every button comes from the payload."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, "Stale", "\U0001F3AE", 4)],  # the old set, ignored
        roles={888: BRRole(888, "Gamer"), 999: BRRole(999, "Artist")},
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(buttons=[_edit_button("999", "Artists", None, 3)]),
        actor,
    )

    assert result["ok"] is True
    # Not the stored 888/"Stale": the panel the operator just asked for.
    assert message.edits[0]["view"].rows == [(999, "Artists", None, 3)]


async def test_button_panel_edit_renders_the_payload_order(button_env):
    """The buttons land in the order they were sent, like a post's. (Reading the
    rows forced ``ORDER BY role_id`` - a deterministic order, but never the one
    the operator arranged.)"""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)],
        roles={888: BRRole(888, "Gamer"), 999: BRRole(999, "Artist")},
    )

    await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(
            buttons=[_edit_button("999", "Artists"), _edit_button("888", "Gamers")]
        ),
        actor,
    )

    assert [row[0] for row in message.edits[0]["view"].rows] == [999, 888]


async def test_button_panel_edit_normalises_the_payload_button_like_a_post(button_env):
    """Same ``_panel_button`` seam as the post executor, so one shape is bounded
    one way: a missing label falls back to the role name, an out-of-range style
    to secondary, a blank emoji to None."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )

    await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(buttons=[_edit_button("888", label=None, emoji="   ", style=99)]),
        actor,
    )

    assert message.edits[0]["view"].rows == [(888, "Gamer", None, 2)]


async def test_button_panel_edit_dedupes_a_role_named_twice(button_env):
    """The stored key is ``(message_id, role_id)``, so the table can only ever
    hold one row per role: rendering two buttons for one role would publish a
    panel the dashboard cannot write back. First mention wins."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(
            buttons=[_edit_button("888", "First"), _edit_button("888", "Second")]
        ),
        actor,
    )

    assert result["buttons"] == 1
    assert message.edits[0]["view"].rows == [(888, "First", None, 1)]


async def test_button_panel_edit_names_a_repeated_bad_role_once(button_env):
    """Dedup runs BEFORE the gate, as on a post, so the answer for a repeat is
    the answer already collected for its first mention - never the same id twice
    in ``failures``."""
    actor = _br_actor(top_position=3)
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)],
        roles={888: BRRole(888, "Staff", position=5)},  # above the actor
        actor=actor,
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(buttons=[_edit_button("888"), _edit_button("888")]),
        actor,
    )

    assert result["failures"] == [{"role_id": "888", "reason": "role_above_actor"}]


async def test_button_panel_edit_reads_the_rows_guild_scoped(button_env):
    """OWNERSHIP. The buttons no longer come from this read, but it is the only
    proof the message is a panel OF THIS GUILD: the primary key carries no guild,
    so an unqualified read would let a manage-guild user of guild B re-render
    (and re-register) guild A's panel by naming its id."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )

    await dashboard_actions._exec_button_panel_edit(bot, 100, _edit_payload(), actor)

    query, args = pool.select_calls[0]
    assert "WHERE message_id = $1 AND guild_id = $2" in query
    assert args == (777, 100)  # the AUTHORITATIVE guild id, not a payload one


async def test_button_panel_edit_refuses_a_message_with_no_rows(button_env):
    """No rows for that (message, guild) pair: another guild's panel, or a
    message that is no panel at all.

    This is what keeps the kind narrow. Without the check it would edit ANY
    message Yasuho ever authored in this guild - it calls ``message.edit`` on
    whatever id it is handed - which is strictly wider than anything the
    dashboard can do today.
    """
    channel, message, pool, actor, bot = _edit_env(rows=[], roles={888: BRRole(888)})

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), actor
    )

    assert result == {"ok": False, "error": "panel_not_found"}
    assert channel.fetched == []
    assert message.edits == []


async def test_button_panel_edit_refuses_a_channel_that_is_not_the_stored_one(
    button_env,
):
    """A message cannot change channel, so a disagreement is a stale or crafted
    request - never something to act on. Still derived from the STORED rows."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, channel_id=555)], roles={888: BRRole(888, "Gamer")}
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(channel_id="42"), actor
    )

    assert result == {"ok": False, "error": "channel_mismatch"}
    assert message.edits == []


async def test_button_panel_edit_refuses_rows_that_disagree_on_the_channel(
    button_env,
):
    """The PK is (message_id, role_id), so nothing forces one message's rows to
    agree on channel_id. Reading the first row would make the verdict depend on
    which one came back first - a coin toss decides whether a crafted set is
    caught. A split set is refused outright, whichever channel was asked for."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, channel_id=555), _panel_row(999, channel_id=42)],
        roles={888: BRRole(888, "Gamer"), 999: BRRole(999, "Artist")},
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(channel_id="555"), actor
    )

    assert result == {"ok": False, "error": "channel_mismatch"}
    assert message.edits == []
    assert channel.fetched == []


async def test_button_panel_edit_accepts_a_panel_whose_rows_share_one_channel(
    button_env,
):
    """The read is ``SELECT DISTINCT channel_id``: a five-button panel is five
    stored rows naming ONE channel, which must not read as rows that disagree."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(rid, channel_id=555) for rid in (700, 800, 888, 900, 950)],
        roles={888: BRRole(888, "Gamer")},
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), actor
    )

    assert result["ok"] is True


@pytest.mark.parametrize("channel_id", [None, "abc", ""])
async def test_button_panel_edit_bad_channel_id(button_env, channel_id):
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )
    payload = _edit_payload()
    if channel_id is None:
        del payload["channel_id"]
    else:
        payload["channel_id"] = channel_id

    result = await dashboard_actions._exec_button_panel_edit(bot, 100, payload, actor)

    assert result == {"ok": False, "error": "bad_channel_id"}
    assert pool.select_calls == []


@pytest.mark.parametrize("message_id", [None, "abc", ""])
async def test_button_panel_edit_bad_message_id(button_env, message_id):
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )
    payload = _edit_payload()
    if message_id is None:
        del payload["message_id"]
    else:
        payload["message_id"] = message_id

    result = await dashboard_actions._exec_button_panel_edit(bot, 100, payload, actor)

    assert result == {"ok": False, "error": "message_not_found"}
    assert pool.select_calls == []


@pytest.mark.parametrize("buttons", [[], {}, "888"])
async def test_button_panel_edit_refuses_an_unusable_button_list(button_env, buttons):
    """An empty list is NOT "strip the panel" - that is ``button_panel_delete``,
    which drops the rows too. Refused exactly like a post's, and refused BEFORE
    the row lookup: the verdict comes purely from what the caller sent, so it
    costs no DB round trip and says nothing about the message it named."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )
    payload = _edit_payload(buttons=buttons)

    result = await dashboard_actions._exec_button_panel_edit(bot, 100, payload, actor)

    assert result == {"ok": False, "error": "no_buttons"}
    assert pool.select_calls == []
    assert message.edits == []


async def test_button_panel_edit_names_an_old_contract_payload_as_such(button_env):
    """THE ONE BREAKING CHANGE, ANSWERED AS ONE. This kind shipped taking only
    ``{message_id, channel_id}`` and reading the buttons out of the table, and
    that shape is already deployed. An enqueue still in it is refused - but with
    its OWN code, not ``no_buttons``: an absent FIELD is a caller on the old
    contract, an empty LIST is an operator who added no button, and telling a
    stale dashboard to "add at least one button" would send it fixing the wrong
    thing. Fail-closed all the same: nothing read, nothing edited, nothing
    registered."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )
    payload = _edit_payload()
    del payload["buttons"]  # the pre-payload contract, verbatim

    result = await dashboard_actions._exec_button_panel_edit(bot, 100, payload, actor)

    assert result == {"ok": False, "error": "buttons_missing"}
    assert pool.select_calls == []  # payload-only verdict, no DB round trip
    assert message.edits == []
    assert channel.fetched == []
    assert bot.added_views == []


@pytest.mark.parametrize(
    "buttons",
    [
        ["888", None],  # nothing usable at all
        [{"role_id": "888", "style": 1}, "999"],  # one good entry, one malformed
    ],
)
async def test_button_panel_edit_refuses_an_entry_that_is_not_an_object(
    button_env, buttons
):
    """A malformed entry REFUSES THE WHOLE EDIT rather than being skipped - the
    one place this kind deliberately parts with the post one.

    The post executor writes the rows itself, from what it rendered, so dropping
    an entry leaves message and table agreeing. Here the DASHBOARD writes them,
    from ITS OWN list, after our ok: rendering 4 of the 5 entries it sent would
    hand it an ok to write 5 rows against, and the returned count says how many
    went out, never WHICH. So it answers like any other unnameable role - bare
    ``bad_role``, no id echoed back.
    """
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(buttons=buttons), actor
    )

    assert result == {"ok": False, "error": "bad_role"}
    assert message.edits == []
    assert channel.fetched == []
    assert bot.added_views == []


async def test_button_panel_edit_refuses_more_buttons_than_discord_allows(button_env):
    """A view of 26 buttons would raise at construction. Re-checked against the
    cog's own MAX_BUTTONS, on the PAYLOAD list now - the cap the post executor
    applies, to the same field."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(buttons=[_edit_button(str(rid)) for rid in range(800, 826)]),
        actor,
    )

    assert result == {"ok": False, "error": "too_many_buttons"}
    assert pool.select_calls == []  # payload-only verdict, no DB round trip
    assert message.edits == []


async def test_button_panel_edit_revalidates_a_role_that_moved_above_the_actor(
    button_env,
):
    """THE PUBLISH-TIME CHECK. An edit re-publishes every button it renders, and
    ``ButtonRoleButton.callback`` grants straight off its custom_id with no rank
    check - so this is the only gate the click path will ever see. A role that
    was fine when the panel was posted can since have moved above the actor."""
    actor = _br_actor(top_position=3)
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, "Staff")],
        roles={888: BRRole(888, "Staff", position=5)},  # since raised above them
        actor=actor,
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(buttons=[_edit_button("888", "Staff")]), actor
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [{"role_id": "888", "reason": "role_above_actor"}],
    }
    # Message untouched AND rows untouched.
    assert message.edits == []
    assert channel.fetched == []
    assert pool.inserted == [] and pool.deleted == []
    assert bot.added_views == []


async def test_button_panel_edit_revalidates_a_role_that_became_unassignable(
    button_env,
):
    """Same gate, other half: a role that has since become integration-managed
    (a booster role, another bot's role) keeps the bot-half code."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888, "Booster")],
        roles={888: BRRole(888, "Booster", managed=True)},
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(buttons=[_edit_button("888", "Booster")]), actor
    )

    assert result == {
        "ok": False,
        "error": "role_not_assignable",
        "failures": [{"role_id": "888", "reason": "role_not_assignable"}],
    }
    assert message.edits == []


async def test_button_panel_edit_refuses_the_whole_edit_for_one_bad_role(button_env):
    """PARTIAL FAILURE REFUSES EVERYTHING. Rendering 4 of 5 buttons would publish
    a panel nobody asked for and hand the dashboard an ok to write five rows
    against - its panel list reads the TABLE, so it would show five buttons for a
    message carrying four with nothing anywhere able to notice."""
    actor = _br_actor(top_position=3)
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)],
        roles={
            700: BRRole(700, "A", position=1),
            800: BRRole(800, "B", position=1),
            888: BRRole(888, "Staff", position=5),  # above the actor
            900: BRRole(900, "C", position=1),
            950: BRRole(950, "D", position=1),
        },
        actor=actor,
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(
            buttons=[_edit_button(str(rid)) for rid in (700, 800, 888, 900, 950)]
        ),
        actor,
    )

    assert result["ok"] is False
    assert result["failures"] == [{"role_id": "888", "reason": "role_above_actor"}]
    # Not four buttons out of five: none.
    assert message.edits == []
    assert pool.inserted == [] and pool.deleted == []


async def test_button_panel_edit_names_every_refused_role(button_env):
    """A panel can hold 25 buttons: naming only the first would make the operator
    fix them one failed action at a time."""
    actor = _br_actor(top_position=3)
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)],
        roles={
            700: BRRole(700, "Booster", managed=True),  # bot half
            888: BRRole(888, "Staff", position=5),  # actor half
            950: BRRole(950, "Fine", position=1),
        },
        actor=actor,
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(buttons=[_edit_button(str(rid)) for rid in (700, 888, 950)]),
        actor,
    )

    # Every id, each with ITS OWN reason - and the dominant code on top.
    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [
            {"role_id": "700", "reason": "role_not_assignable"},
            {"role_id": "888", "reason": "role_above_actor"},
        ],
    }
    assert message.edits == []


async def test_button_panel_edit_refuses_a_role_that_is_not_of_this_guild(button_env):
    """A role that does not resolve is not a gate refusal (there is nothing left
    to judge), so it keeps the module's ``bad_role`` code - but it still names the
    id, and it still refuses the WHOLE edit rather than dropping that button."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)],
        roles={888: BRRole(888, "Gamer")},  # 999 deleted, or never this guild's
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot,
        100,
        _edit_payload(buttons=[_edit_button("888"), _edit_button("999")]),
        actor,
    )

    assert result == {"ok": False, "error": "bad_role", "role_id": "999"}
    assert message.edits == []
    assert pool.inserted == [] and pool.deleted == []


@pytest.mark.parametrize("role_id", [None, "abc", ""])
async def test_button_panel_edit_bad_role_id_names_nothing(button_env, role_id):
    """An unparsable id has nothing to name, and the payload's value is never
    echoed back: bare ``bad_role``, exactly like a post's."""
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}
    )
    button = _edit_button()
    if role_id is None:
        del button["role_id"]
    else:
        button["role_id"] = role_id

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(buttons=[button]), actor
    )

    assert result == {"ok": False, "error": "bad_role"}
    assert message.edits == []


async def test_button_panel_edit_guild_unavailable(button_env):
    pool = BRPool(panel_rows=[_panel_row(888)])
    bot = FakeBot(pool, guilds={})

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), _br_actor()
    )

    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.select_calls == []


async def test_button_panel_edit_without_a_me_member_refuses(button_env):
    """No guild.me = the bot half cannot be asked; "I could not check" must not
    render as a per-role refusal that is really a missing cache."""
    guild = BRGuild(channels={}, roles={}, has_me=False)
    pool = BRPool(panel_rows=[_panel_row(888)])
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), _br_actor()
    )

    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.select_calls == []


async def test_button_panel_edit_channel_gone(button_env):
    guild = BRGuild(channels={}, roles={888: BRRole(888, "Gamer")},
                    members={ACTOR_ID: _br_actor()})
    pool = BRPool(panel_rows=[_panel_row(888)])
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), _br_actor()
    )

    assert result == {"ok": False, "error": "channel_not_found"}


async def test_button_panel_edit_message_gone(button_env):
    channel = BREditChannel(555, message=None)  # fetch_message raises
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")},
        channel=channel, message=BREditMessage(777),
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), actor
    )

    assert result == {"ok": False, "error": "message_not_found"}
    assert bot.added_views == []


async def test_button_panel_edit_failed_edit_is_reported(button_env):
    """The dashboard has not written anything yet, so a failed edit leaves the
    panel exactly as the table still describes it - the edit simply did not
    land, and its ok never came."""
    message = BREditMessage(777, fail_edit=True)
    channel, message, pool, actor, bot = _edit_env(
        rows=[_panel_row(888)], roles={888: BRRole(888, "Gamer")}, message=message
    )

    result = await dashboard_actions._exec_button_panel_edit(
        bot, 100, _edit_payload(), actor
    )

    assert result == {"ok": False, "error": "edit_failed"}
    assert pool.inserted == [] and pool.deleted == []
    assert bot.added_views == []  # not registered on a failed edit


async def test_button_panel_edit_is_refused_without_an_actor(button_env):
    """End to end through the dispatcher: a row with no ``requested_by`` never
    reaches the executor, so an edit can never republish on the bot half alone."""
    channel = BREditChannel(555, message=BREditMessage(777))
    guild = BRGuild(channels={555: channel}, roles={888: BRRole(888, "Gamer")})
    pool = ButtonActionsPool()
    pool.add(
        1,
        guild_id=100,
        kind="button_panel_edit",
        payload=_edit_payload(),
        requested_by=None,
    )
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_missing"}
    assert channel.fetched == []


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
    def __init__(self, channels=None, roles=None, has_me=True, preferred_locale="en",
                 members=None, owner_id=111):
        self.id = 100
        self._channels = channels or {}
        self._roles = roles or {}
        self._members = dict(members or {})
        self.owner_id = owner_id
        # A real member object (not a bare sentinel): the option filter now asks
        # whether Yasuho could actually grant each picked role, which reads her
        # top role.
        self.me = _fake_me() if has_me else None
        self.preferred_locale = preferred_locale

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_channel_or_thread(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, user_id):
        return self._members.get(user_id)


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
        _role_actor(),
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
        bot, 100, _menu_payload(options=[{"role_id": "888", "label": "Blue"}]),
        _role_actor(),
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
        _role_actor(),
    )

    assert result["ok"] is True
    stored = json.loads(pool.inserted[0][3])
    assert [o["role_id"] for o in stored["options"]] == [888]


async def test_role_menu_post_filters_roles_yasuho_cannot_grant(rolemenu_env):
    """Same guard as the reaction/button executors, applied per option.

    An option Yasuho can never hand out (@everyone, integration-managed, at or
    above her own top role) would 403 on every pick with the failure swallowed at
    the grant site, so the member just sees nothing happen. Drop it here, exactly
    like a foreign role, and keep the options that work.
    """
    channel = FakeRMChannel(555)
    guild = RMGuild(
        channels={555: channel},
        roles={
            888: FakeRole(888, position=1),
            777: FakeRole(777, position=1, managed=True),
            666: FakeRole(666, position=1, default=True),
            555: FakeRole(555, position=11),  # above her top role
        },
    )
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot,
        100,
        _menu_payload(
            options=[
                {"role_id": "888", "label": "Blue"},
                {"role_id": "777", "label": "Booster"},
                {"role_id": "666", "label": "everyone"},
                {"role_id": "555", "label": "Admin"},
            ]
        ),
        _role_actor(),
    )

    assert result["ok"] is True
    stored = json.loads(pool.inserted[0][3])
    assert [o["role_id"] for o in stored["options"]] == [888]


async def test_role_menu_post_refuses_when_no_option_is_grantable(rolemenu_env):
    """All-unassignable is rejected wholesale, like all-foreign: posting a menu
    whose every pick 403s is worse than telling the dashboard it failed."""
    channel = FakeRMChannel(555)
    guild = RMGuild(
        channels={555: channel}, roles={777: FakeRole(777, position=1, managed=True)}
    )
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot, 100, _menu_payload(options=[{"role_id": "777", "label": "Booster"}]),
        _role_actor(),
    )

    assert result == {"ok": False, "error": "bad_role_all"}
    assert channel.sent == []  # refused BEFORE anything was posted
    assert pool.inserted == []


async def test_role_menu_post_bad_role_all(rolemenu_env):
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={})  # no roles at all
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot, 100, _menu_payload(options=[{"role_id": "888", "label": "Blue"}]),
        _role_actor(),
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
    result = await dashboard_actions._exec_role_menu_post(bot, 100, payload, _role_actor())
    assert result == {"ok": False, "error": "no_options"}
    assert channel.sent == []


async def test_role_menu_post_too_many_menus(rolemenu_env):
    channel = FakeRMChannel(555)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool(count=25)  # already at MAX_MENUS_PER_GUILD
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload(), _role_actor())

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
    result = await dashboard_actions._exec_role_menu_post(bot, 100, payload, _role_actor())
    assert result == {"ok": False, "error": "bad_channel_id"}
    assert pool.inserted == []


async def test_role_menu_post_guild_unavailable(rolemenu_env):
    pool = RMPool()
    bot = _rm_bot(pool, guild=None)  # bot not in guild 100
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload(), _role_actor())
    assert result == {"ok": False, "error": "guild_unavailable"}
    assert pool.inserted == []


async def test_role_menu_post_channel_not_found(rolemenu_env):
    guild = RMGuild(channels={}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload(), _role_actor())
    assert result == {"ok": False, "error": "channel_not_found"}
    assert pool.inserted == []


async def test_role_menu_post_rejects_non_text_channel(rolemenu_env):
    guild = RMGuild(channels={555: FakeVoiceChannel(555)}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload(), _role_actor())
    assert result == {"ok": False, "error": "not_text_channel"}
    assert pool.inserted == []


async def test_role_menu_post_missing_send_permission(rolemenu_env):
    channel = FakeRMChannel(555, can_send=False)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)
    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload(), _role_actor())
    assert result == {"ok": False, "error": "missing_send_permission"}
    assert channel.sent == []
    assert pool.inserted == []


async def test_role_menu_post_edit_failure_deletes_and_reports(rolemenu_env):
    msg = FakeRMMessage(fail_edit=True)
    channel = FakeRMChannel(555, message=msg)
    guild = RMGuild(channels={555: channel}, roles={888: FakeRole(888)})
    pool = RMPool()
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(bot, 100, _menu_payload(), _role_actor())

    assert result == {"ok": False, "error": "post_failed"}
    # The orphan (view-less) message is cleaned up; nothing persisted or registered.
    assert msg.deleted is True
    assert pool.inserted == []
    assert bot.added_views == []


async def test_role_menu_post_full_flow_via_handle_action(rolemenu_env):
    """End-to-end through the queue: claim -> post executor -> done + result."""
    channel = FakeRMChannel(555)
    guild = RMGuild(
        channels={555: channel},
        roles={888: FakeRole(888)},
        members={ACTOR_ID: _role_actor()},
    )
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


async def test_role_menu_delete_keeps_menu_id_when_no_row_matched(rolemenu_env):
    """No row matched = the menu is NOT this guild's, so _menu_ids must not move.

    ``_menu_ids`` is keyed by message id ALONE. Discarding on a miss would let a
    manage-guild user of guild B unhook guild A's live menu from the cog's
    on_raw_message_delete pruning (the row survives, so the menu would linger in
    the table forever once its message is deleted) - the cross-tenant twin of the
    reaction-role cache pop.
    """
    guild = RMGuild(channels={})
    cog = FakeRMCog()
    cog._menu_ids.add(777)  # guild A's live menu
    pool = RMPool(delete_return=[])
    bot = _rm_bot(pool, guild, cog)

    result = await dashboard_actions._exec_role_menu_delete(
        bot, 100, {"message_id": "777"}
    )

    assert result == {"ok": True}
    assert pool.delete_calls == [(777, 100)]  # still guild-scoped
    assert cog._menu_ids == {777}  # untouched


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
# re-index the cog). Both cog methods answer with a RECORD (HubCreation /
# HubRemoval) whose ``.message`` is a translated human string on every path, so
# create success is still detected STRUCTURALLY - by diffing the saved hub list,
# never by reading the record - while delete reads the record's ``failed`` to
# tell a clean removal from a config drop over channels Discord refused.
# ---------------------------------------------------------------------------


class FakeRoomsCog:
    """Stand-in for the TemporaryRooms cog: the three methods the executors use.

    ``_load_hubs`` serves the guild's saved hubs (the before/after picture the
    create executor diffs), ``_add_hub`` records its exact kwargs and - unless
    seeded to refuse - appends a hub and returns the "Created ..." outcome, just
    as the real one does only after actually saving. ``_remove_hub`` records the
    id, drops the hub and returns its own outcome. No Discord objects needed: the
    real channel work happens inside the cog, which is not under test here.

    Both methods answer with the REAL records (``tools.autoroom.HubCreation`` /
    ``HubRemoval``), never a bare string: that is the seam this file pins, so a
    cog that went back to answering with a sentence alone - and an executor that
    started reporting an unqualified success again - fails here.
    """

    CREATED_HUB_ID = "newhub01"
    CREATED_CHANNEL_ID = 4242

    def __init__(
        self,
        hubs=None,
        refuse_add=False,
        orphan_category_id=None,
        removal_deleted=(444, 555),
        removal_failed=(),
        removal_removed=True,
    ):
        self.hubs = list(hubs or [])
        self.loads = []
        self.add_calls = []
        self.remove_calls = []
        # The gettext locale that was ACTIVE on each call: the real cog builds
        # its messages with _(), so the executor must set the guild's language
        # around the call or every dashboard message comes back in English.
        self.locales = []
        self._refuse_add = refuse_add
        # Set on the refusal path only: the half-created hub whose leftover
        # category the cog could not roll back either.
        self._orphan_category_id = orphan_category_id
        # What Discord let the cog delete, and what it refused.
        self._removal_deleted = tuple(removal_deleted)
        self._removal_failed = tuple(removal_failed)
        self._removal_removed = removal_removed

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
            # A budget refusal (or a failed creation): the real cog returns its
            # message WITHOUT saving, plus the leftover category it could not
            # roll back, if any.
            return autoroom.HubCreation(
                message="This server is at Discord's limit of 50 categories.",
                orphan_category_id=self._orphan_category_id,
            )
        self.hubs.append(
            _ar_hub(
                hub_id=self.CREATED_HUB_ID,
                hub_channel_id=self.CREATED_CHANNEL_ID,
                label=label,
            )
        )
        return autoroom.HubCreation(
            message="Created the **%s** hub. Members can join <#%s> now."
            % (label, self.CREATED_CHANNEL_ID)
        )

    async def _remove_hub(self, guild, hub_id):
        self.locales.append(i18n.current_locale.get())
        self.remove_calls.append((guild, hub_id))
        if not self._removal_removed:
            # The hub was already gone from the config when the cog re-read it.
            return autoroom.HubRemoval(
                message="That hub no longer exists.", removed=False
            )
        self.hubs = [hub for hub in self.hubs if hub["id"] != hub_id]
        if self._removal_failed:
            return autoroom.HubRemoval(
                message="Removed the **Ranked** hub from the settings, but %d of "
                "its channels could not be deleted." % len(self._removal_failed),
                deleted=self._removal_deleted,
                failed=self._removal_failed,
            )
        return autoroom.HubRemoval(
            message="Removed the **Ranked** hub.", deleted=self._removal_deleted
        )


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
        return autoroom.HubCreation(message="Created the **%s** hub." % label)


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


async def test_autoroom_hub_delete_reports_the_channels_discord_refused():
    """A refused deletion is a QUALIFIED success, never an unqualified "done".

    The cog drops the config whatever Discord answers (a hub stuck in the
    settings behind a refused delete would be worse), so ok stays True - but the
    result must let the web app say "config cleared, 2 channels still there"
    instead of "Hub deleted" over a category the user can still see.
    """
    cog = FakeRoomsCog(
        hubs=[_ar_hub(hub_id="abc12345")],
        removal_deleted=(555,),
        removal_failed=(444, 777),
    )
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )

    assert result["ok"] is True  # the durable half DID happen
    assert "error" not in result  # a qualified success is not a refusal
    assert result["failed"] == ["444", "777"]  # snowflakes as STRINGS
    assert result["deleted"] == ["555"]
    assert "could not be deleted" in result["message"]


async def test_autoroom_hub_delete_reports_an_empty_failed_list_when_gone():
    """The proof of "really gone": failed present and empty, not absent.

    The dashboard tests ``failed``, so the key has to be there on the happy path
    too - an absent key would read as falsy for the wrong reason.
    """
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")], removal_deleted=(444, 555))
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )

    assert result["ok"] is True
    assert result["failed"] == []
    assert result["deleted"] == ["444", "555"]
    assert result["message"] == "Removed the **Ranked** hub."


async def test_autoroom_hub_delete_hub_vanished_under_the_pre_check():
    """The cog re-reads the hub list; the dashboard's Node process writes it too.

    A hub removed between the executor's existence pre-check and the cog's own
    read touched NOTHING, so it must not be reported as a deletion - and it
    reuses the pre-check's code rather than inventing a second one.
    """
    cog = FakeRoomsCog(hubs=[_ar_hub(hub_id="abc12345")], removal_removed=False)
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_delete(
        bot, 100, {"hub_id": "abc12345"}
    )

    assert result == {"ok": False, "error": "hub_not_found"}
    assert len(cog.remove_calls) == 1  # it WAS attempted


async def test_autoroom_hub_create_forwards_the_category_it_could_not_roll_back():
    """Half-created + rollback refused: the leftover category is named.

    Still a refusal (nothing was saved), but the dashboard must be able to tell
    "your hub was not created" from "your hub was not created AND there is now
    an empty category to delete".
    """
    cog = FakeRoomsCog(refuse_add=True, orphan_category_id=8811)
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result["ok"] is False
    assert result["error"] == "create_failed"  # NOT promoted to a success
    assert result["orphan_category_id"] == "8811"  # snowflake as a STRING


async def test_autoroom_hub_create_omits_the_orphan_key_when_nothing_was_left():
    """Rollback worked (or there was nothing to roll back): the key is ABSENT.

    Its mere presence is the signal, so a clean failure must not carry it - a
    null would have the dashboard offering to clean up a category that is not
    there.
    """
    cog = FakeRoomsCog(refuse_add=True)
    bot = _ar_bot(cog=cog)

    result = await dashboard_actions._exec_autoroom_hub_create(
        bot, 100, _hub_payload()
    )

    assert result["ok"] is False
    assert "orphan_category_id" not in result


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
    # The channel lists survive the JSON write-back to the queue row.
    assert pool.rows[1]["result"]["deleted"] == ["444", "555"]
    assert pool.rows[1]["result"]["failed"] == []
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


# ---------------------------------------------------------------------------
# THE ACTOR GATE
#
# The four kinds that publish a role a member can then obtain are dispatched
# with the Member behind the row's ``requested_by`` and refuse to run unless
# that person outranks the role. Everything here is about the two halves of
# that: (1) the dispatcher resolves the actor and FAILS CLOSED - a sparse
# member cache means a miss is UNKNOWN, never "absent" - and (2) the executors
# ask ``modchecks.self_assignable_role_error`` on top of the bot-half check they
# already asked, each failure keeping its own code.
# ---------------------------------------------------------------------------


class ActorGuild:
    """A guild whose member cache is SPARSE, with the REST fetch counted.

    ``cached`` is what ``get_member`` answers (the gateway cache Yasuho actually
    has: chunk_guilds_at_startup is False, so it holds only members recently seen).
    ``fetched`` is what the ONE allowed ``fetch_member`` round trip would return;
    an id in neither raises ``NotFound`` (PROVEN absent), and ``fetch_error``
    makes that round trip fail the way a 403/5xx/timeout does - the case where
    membership stays UNKNOWN.
    """

    def __init__(self, cached=None, fetched=None, fetch_error=None, owner_id=111):
        self.id = 100
        self._cached = dict(cached or {})
        self._fetched = dict(fetched or {})
        self._fetch_error = fetch_error
        self.owner_id = owner_id
        self.cache_reads = []
        self.fetches = []

    def get_member(self, user_id):
        self.cache_reads.append(user_id)
        return self._cached.get(user_id)

    async def fetch_member(self, user_id):
        self.fetches.append(user_id)
        if self._fetch_error is not None:
            raise self._fetch_error
        if user_id not in self._fetched:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"), "Unknown Member"
            )
        return self._fetched[user_id]


def _http_error(status=500):
    return discord.HTTPException(
        types.SimpleNamespace(status=status, reason="Server Error"), "boom"
    )


def _actor_kind(monkeypatch, handler=None, kind="test_actor_kind"):
    """Register ``kind`` as an executor that TAKES AN ACTOR, and return the log.

    The synthetic kind keeps these tests about the dispatcher rather than about
    any one executor's payload validation; the real kinds are exercised further
    down.
    """
    runs = []

    async def _exec(bot, scope_id, payload, actor):
        runs.append((scope_id, payload, actor))
        return {"ok": True}

    monkeypatch.setitem(dashboard_actions._EXECUTORS, kind, handler or _exec)
    monkeypatch.setattr(
        dashboard_actions,
        "_ACTOR_KINDS",
        dashboard_actions._ACTOR_KINDS | {kind},
    )
    return runs


# --- the column has to come back from the claim at all ---------------------


async def test_the_claim_returns_the_actor_column():
    """No ``requested_by`` in the RETURNING = no actor = the gate cannot exist."""
    captured = []

    class _CapturePool:
        async def fetchrow(self, query, *args):
            captured.append(query)
            return None

    await dashboard_actions._claim(_CapturePool(), 1)

    assert "RETURNING guild_id, user_id, kind, payload, requested_by" in captured[0]


def test_actor_kinds_are_exactly_the_executors_that_take_an_actor():
    """The registry and the signatures must agree, in BOTH directions.

    A kind listed here whose executor takes three arguments would be called with
    four (a TypeError swallowed as ``internal_error``); an executor that takes an
    actor but is NOT listed would be called with three - and the role it
    publishes would go out ungated.
    """
    takes_actor = {
        kind
        for kind, executor in dashboard_actions._EXECUTORS.items()
        if len(inspect.signature(executor).parameters) >= 4
    }
    assert takes_actor == dashboard_actions._ACTOR_KINDS
    assert dashboard_actions._ACTOR_KINDS == {
        "verify_button_post",
        "reaction_role_add",
        "button_panel_post",
        "button_panel_edit",
        "role_menu_post",
    }


def test_actor_kinds_are_all_guild_scoped():
    """The actor is resolved against ``bot.get_guild(scope_id)``, so an actor kind
    that read a USER id as its scope would look a guild up by a user snowflake."""
    assert not (dashboard_actions._ACTOR_KINDS & dashboard_actions._USER_KINDS)


# --- resolution: the happy paths, and what they cost -----------------------


async def test_an_actor_kind_is_dispatched_with_the_resolved_member(monkeypatch):
    runs = _actor_kind(monkeypatch)
    actor = _actor_with(FakeRole(7_000, position=900))
    guild = ActorGuild(cached={ACTOR_ID: actor})
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={"x": 1})
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "done"
    assert runs == [(100, {"x": 1}, actor)]
    # A cache HIT costs nothing: no REST round trip at all.
    assert guild.fetches == []


async def test_a_cache_miss_costs_exactly_one_fetch_member(monkeypatch):
    """The sparse cache is resolved by ONE fetch, and the action then proceeds."""
    runs = _actor_kind(monkeypatch)
    actor = _actor_with(FakeRole(7_000, position=900))
    guild = ActorGuild(fetched={ACTOR_ID: actor})  # not cached, but in the guild
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "done"
    assert runs and runs[0][2] is actor
    assert guild.fetches == [ACTOR_ID]  # exactly one, never more


async def test_a_non_actor_kind_is_still_dispatched_with_three_arguments(monkeypatch):
    """Non-regression: nothing outside _ACTOR_KINDS changes shape or pays a lookup."""
    seen = []

    async def _exec(bot, scope_id, payload):
        seen.append(scope_id)
        return {"ok": True}

    _register(monkeypatch, "test_kind", _exec)
    guild = ActorGuild()
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_kind", payload={}, requested_by=None)
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "done"
    assert seen == [100]
    assert guild.cache_reads == [] and guild.fetches == []


# --- FAIL CLOSED: every unresolved actor is a refusal, with its own code ----


async def test_a_null_requested_by_is_refused_as_actor_missing(monkeypatch):
    """The column is nullable, so this row shape is reachable - and it must NEVER
    fall back to the bot-half-only check."""
    runs = _actor_kind(monkeypatch)
    guild = ActorGuild()
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={}, requested_by=None)
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_missing"}
    assert runs == []  # the executor never ran
    assert guild.fetches == []  # and nothing was looked up


@pytest.mark.parametrize("value", [0, -1, True, "", "not-an-id", 1.5])
async def test_an_unusable_requested_by_is_refused_as_actor_missing(monkeypatch, value):
    runs = _actor_kind(monkeypatch)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={}, requested_by=value)
    bot = FakeBot(pool, guilds={100: ActorGuild()})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_missing"}
    assert runs == []


async def test_a_claim_without_the_column_is_refused_as_actor_missing(monkeypatch):
    """A row shape with no ``requested_by`` at all (an older query mid-deploy)
    reads as "no actor" and is refused, exactly like a bad scope - never raised
    into the dispatcher."""

    class LegacyClaimPool(ActionsPool):
        async def fetchrow(self, query, *args):
            claimed = await super().fetchrow(query, *args)
            if claimed is not None:
                claimed.pop("requested_by", None)
            return claimed

    runs = _actor_kind(monkeypatch)
    pool = LegacyClaimPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={100: ActorGuild()})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_missing"}
    assert runs == []


async def test_a_departed_actor_is_refused_as_actor_left_guild(monkeypatch):
    """PROVEN absent (404) is the one negative answer we trust - and it still
    refuses: someone who left cannot publish a role here."""
    runs = _actor_kind(monkeypatch)
    guild = ActorGuild()  # neither cached nor fetchable -> NotFound
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_left_guild"}
    assert runs == []
    assert guild.fetches == [ACTOR_ID]


@pytest.mark.parametrize(
    "error",
    [
        _http_error(500),
        _http_error(403),
        RuntimeError("session is closed"),
        asyncio.TimeoutError(),
    ],
)
async def test_an_unverifiable_actor_is_refused_as_actor_unverified(monkeypatch, error):
    """THE fail-closed case. The member cache is SPARSE (core.py sets
    chunk_guilds_at_startup=False), so a miss means UNKNOWN; when the one fetch
    that could settle it fails with anything other than a 404, the rank is
    unverifiable and the publication MUST NOT proceed."""
    runs = _actor_kind(monkeypatch)
    guild = ActorGuild(fetch_error=error)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_unverified"}
    assert runs == []
    assert guild.fetches == [ACTOR_ID]  # one attempt, then refuse - never a retry loop


async def test_a_fetch_that_yields_nothing_is_refused_as_actor_unverified(monkeypatch):
    """A fetch that returns None instead of raising is still not an answer."""

    class _NoneFetchGuild(ActorGuild):
        async def fetch_member(self, user_id):
            self.fetches.append(user_id)
            return None

    runs = _actor_kind(monkeypatch)
    guild = _NoneFetchGuild()
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={100: guild})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_unverified"}
    assert runs == []


async def test_a_get_member_that_raises_is_refused_not_run(monkeypatch):
    """Even an unexpected explosion inside the resolution is a REFUSAL: the
    dispatcher's catch-all maps it to ``actor_unverified``, never to a run."""

    class _BoomGuild(ActorGuild):
        def get_member(self, user_id):
            raise RuntimeError("cache is on fire")

    runs = _actor_kind(monkeypatch)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={100: _BoomGuild()})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "actor_unverified"}
    assert runs == []


async def test_an_actor_kind_without_a_live_guild_is_refused(monkeypatch):
    """No guild = no member to resolve; refused before the executor, with the
    same code the executors themselves use for a missing guild."""
    runs = _actor_kind(monkeypatch)
    pool = ActionsPool()
    pool.add(1, guild_id=100, kind="test_actor_kind", payload={})
    bot = FakeBot(pool, guilds={})

    assert await dashboard_actions.handle_action(bot, 1) == "failed"
    assert pool.rows[1]["result"] == {"ok": False, "error": "guild_unavailable"}
    assert runs == []


# --- the executors: the configurer half, alongside the bot half ------------


def _rr_env(role, actor):
    """A reaction-role executor call environment: channel + message + role."""
    channel = FakeReactionChannel(555, message=FakeMessage(777))
    guild = FakeReactionGuild(
        channels={555: channel}, roles={888: role}, members={ACTOR_ID: actor}
    )
    cog = FakeCog()
    pool = RRPool()
    return channel, guild, cog, pool, _rr_bot(pool, guild, cog)


async def test_reaction_role_add_refuses_a_role_at_or_above_the_actor():
    """A manage_guild user cannot publish, from the web app, a role /reactionrole
    would refuse them in Discord."""
    role = FakeRole(888, position=5)  # below the bot (10), above the actor (3)
    actor = _role_actor(top_position=3)
    channel, guild, cog, pool, bot = _rr_env(role, actor)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "\U0001F3AE", "role_id": "888"},
        actor,
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [{"role_id": "888", "reason": "role_above_actor"}],
    }
    # Refused BEFORE any side effect: no reaction, no row, no cache entry.
    assert channel.message.reactions == []
    assert pool.executed == []
    assert cog.cache == {}


async def test_reaction_role_add_lets_an_administrator_publish_a_higher_role():
    """Counter-test: the hierarchy half exempts an Administrator (and the owner),
    exactly as it does in Discord - the gate is a rank check, not a blanket no."""
    role = FakeRole(888, position=5)
    actor = _role_actor(top_position=3, administrator=True)
    channel, guild, cog, pool, bot = _rr_env(role, actor)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "\U0001F3AE", "role_id": "888"},
        actor,
    )

    assert result["ok"] is True
    assert cog.cache == {(777, "\U0001F3AE"): 888}


async def test_reaction_role_add_keeps_the_bot_half_code_for_an_unassignable_role():
    """The bot half is composed INSIDE self_assignable_role_error, so it would be
    covered either way - but it is kept as its own call, checked FIRST, so the
    dashboard still gets ``role_not_assignable`` (a role Yasuho can never hand
    out) rather than ``role_above_actor`` (a rank problem). Actor is the guild
    OWNER here, so the hierarchy half cannot be what refuses."""
    role = FakeRole(888, position=5, managed=True)  # integration-owned
    actor = _role_actor(top_position=1, user_id=111)  # 111 == guild.owner_id
    channel, guild, cog, pool, bot = _rr_env(role, actor)

    result = await dashboard_actions._exec_reaction_role_add(
        bot,
        100,
        {"channel_id": "555", "message_id": "777", "emoji": "\U0001F3AE", "role_id": "888"},
        actor,
    )

    assert result == {
        "ok": False,
        "error": "role_not_assignable",
        "failures": [{"role_id": "888", "reason": "role_not_assignable"}],
    }
    assert channel.message.reactions == []


async def test_button_panel_post_refuses_a_role_at_or_above_the_actor(button_env):
    channel = FakeTextChannel(channel_id=555)
    actor = _br_actor(top_position=3)
    guild = BRGuild(
        channels={555: channel},
        roles={888: BRRole(888, "Gamer", position=5)},
        members={ACTOR_ID: actor},
    )
    pool = BRPool()
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot, 100, _panel_payload(), actor
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [{"role_id": "888", "reason": "role_above_actor"}],
    }
    # Nothing posted, nothing persisted, no persistent view registered.
    assert channel.sent == []
    assert pool.inserted == []
    assert bot.added_views == []


async def test_button_panel_post_refuses_the_whole_panel_for_one_bad_button(button_env):
    """One button out of rank refuses the panel; a partially published panel
    would be a silent grant of exactly what was refused."""
    channel = FakeTextChannel(channel_id=555)
    actor = _br_actor(top_position=3)
    guild = BRGuild(
        channels={555: channel},
        roles={
            888: BRRole(888, "Gamer", position=1),  # fine
            999: BRRole(999, "Staff", position=5),  # above the actor
        },
        members={ACTOR_ID: actor},
    )
    pool = BRPool()
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(
            buttons=[{"role_id": "888", "label": "Gamer"}, {"role_id": "999"}]
        ),
        actor,
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [{"role_id": "999", "reason": "role_above_actor"}],
    }
    assert channel.sent == []
    assert pool.inserted == []


async def test_role_menu_post_refuses_when_an_option_is_above_the_actor(rolemenu_env):
    """The bot half FILTERS an ungrantable option, but a role above the ACTOR is
    a privilege they asked for: the whole menu is refused so they are told."""
    channel = FakeRMChannel(555)
    actor = _role_actor(top_position=3)
    guild = RMGuild(
        channels={555: channel},
        roles={888: FakeRole(888, position=1), 999: FakeRole(999, position=5)},
        members={ACTOR_ID: actor},
    )
    pool = RMPool(count=0)
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot,
        100,
        _menu_payload(options=[{"role_id": "888"}, {"role_id": "999"}]),
        actor,
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [{"role_id": "999", "reason": "role_above_actor"}],
    }
    assert channel.sent == []  # nothing posted...
    assert pool.inserted == []  # ...and nothing persisted


async def test_role_menu_post_ignores_the_actor_rank_of_a_filtered_option(rolemenu_env):
    """Ordering: an option Yasuho cannot grant is DROPPED (as before), so it never
    reaches the actor check - a menu is not refused over an option it would not
    have published anyway."""
    channel = FakeRMChannel(555)
    actor = _role_actor(top_position=3)
    guild = RMGuild(
        channels={555: channel},
        roles={
            888: FakeRole(888, position=1),  # grantable and below the actor
            999: FakeRole(999, position=50),  # above the BOT: filtered out
        },
        members={ACTOR_ID: actor},
    )
    pool = RMPool(count=0)
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot,
        100,
        _menu_payload(options=[{"role_id": "888"}, {"role_id": "999"}]),
        actor,
    )

    assert result["ok"] is True
    stored = json.loads(pool.inserted[0][3])
    assert [o["role_id"] for o in stored["options"]] == [888]


# --- naming the offending roles: the ``failures`` list ---------------------


def test_role_refusal_precedence_is_deterministic():
    """``role_above_actor`` beats ``role_not_assignable``, whatever the order the
    failures were collected in: it is a fact about the ACTOR's own rank (the
    person reading the dashboard), where the other is a limit of Yasuho's
    position. The header code must not depend on button order."""
    above = {"role_id": "1", "reason": "role_above_actor"}
    cannot = {"role_id": "2", "reason": "role_not_assignable"}

    assert dashboard_actions._role_refusal([above, cannot])["error"] == "role_above_actor"
    assert dashboard_actions._role_refusal([cannot, above])["error"] == "role_above_actor"
    # ... and a refusal that is only ever the bot half keeps its own code.
    assert (
        dashboard_actions._role_refusal([cannot])["error"] == "role_not_assignable"
    )


def test_role_refusal_always_carries_a_list():
    """One shape for the consumer: even a single-role kind answers with a list."""
    one = dashboard_actions._role_refusal(
        [{"role_id": "9", "reason": "role_above_actor"}]
    )
    assert one["failures"] == [{"role_id": "9", "reason": "role_above_actor"}]
    assert isinstance(one["failures"], list)


async def test_button_panel_post_names_every_refused_role(button_env):
    """COLLECT, then refuse. Returning at the first bad role would ship a list
    that can never hold more than one element - and on a 25-button panel the
    operator would fix them one failed action at a time."""
    channel = FakeTextChannel(channel_id=555)
    actor = _br_actor(top_position=3)
    guild = BRGuild(
        channels={555: channel},
        roles={
            700: BRRole(700, "Booster", managed=True),  # bot half
            888: BRRole(888, "Staff", position=5),  # actor half
            950: BRRole(950, "Fine", position=1),  # publishable
        },
        members={ACTOR_ID: actor},
    )
    pool = BRPool()
    bot = _br_bot(pool, guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(
            buttons=[{"role_id": "700"}, {"role_id": "888"}, {"role_id": "950"}]
        ),
        actor,
    )

    # Every offender named, each with ITS OWN reason - a single group code would
    # print a sentence that is false for one of them.
    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [
            {"role_id": "700", "reason": "role_not_assignable"},
            {"role_id": "888", "reason": "role_above_actor"},
        ],
    }
    assert channel.sent == []
    assert pool.inserted == []


async def test_button_panel_post_dominant_code_ignores_button_order(button_env):
    """Same two roles, reversed: the header code is a documented precedence, not
    whichever button happened to come first."""
    channel = FakeTextChannel(channel_id=555)
    actor = _br_actor(top_position=3)
    guild = BRGuild(
        channels={555: channel},
        roles={
            700: BRRole(700, "Booster", managed=True),
            888: BRRole(888, "Staff", position=5),
        },
        members={ACTOR_ID: actor},
    )
    bot = _br_bot(BRPool(), guild)

    first = await dashboard_actions._exec_button_panel_post(
        bot, 100, _panel_payload(buttons=[{"role_id": "700"}, {"role_id": "888"}]), actor
    )
    second = await dashboard_actions._exec_button_panel_post(
        bot, 100, _panel_payload(buttons=[{"role_id": "888"}, {"role_id": "700"}]), actor
    )

    assert first["error"] == second["error"] == "role_above_actor"
    assert {f["role_id"] for f in first["failures"]} == {"700", "888"}
    assert {f["role_id"] for f in second["failures"]} == {"700", "888"}


async def test_button_panel_post_names_a_repeated_role_once(button_env):
    """The dedup runs before the gate, so a role named twice is one entry: the
    answer for a repeat is the answer already collected for its first mention."""
    channel = FakeTextChannel(channel_id=555)
    actor = _br_actor(top_position=3)
    guild = BRGuild(
        channels={555: channel},
        roles={888: BRRole(888, "Staff", position=5)},
        members={ACTOR_ID: actor},
    )
    bot = _br_bot(BRPool(), guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot,
        100,
        _panel_payload(buttons=[{"role_id": "888"}, {"role_id": "888", "label": "Dup"}]),
        actor,
    )

    assert result["failures"] == [{"role_id": "888", "reason": "role_above_actor"}]


async def test_button_panel_post_names_the_roles_rather_than_answering_no_buttons(
    button_env,
):
    """A panel whose EVERY button is refused must still say which roles: the
    empty check comes after the refusal, not before it."""
    channel = FakeTextChannel(channel_id=555)
    actor = _br_actor(top_position=3)
    guild = BRGuild(
        channels={555: channel},
        roles={888: BRRole(888, "Staff", position=5)},
        members={ACTOR_ID: actor},
    )
    bot = _br_bot(BRPool(), guild)

    result = await dashboard_actions._exec_button_panel_post(
        bot, 100, _panel_payload(buttons=[{"role_id": "888"}]), actor
    )

    assert result["error"] == "role_above_actor"
    assert result["failures"] == [{"role_id": "888", "reason": "role_above_actor"}]


async def test_role_menu_post_names_every_option_above_the_actor(rolemenu_env):
    """25 options, same rule - and an option the BOT half filtered out is not in
    the list: it was dropped, not refused, so naming it would be a lie."""
    channel = FakeRMChannel(555)
    actor = _role_actor(top_position=3)
    guild = RMGuild(
        channels={555: channel},
        roles={
            888: FakeRole(888, position=5),  # above the actor
            999: FakeRole(999, position=6),  # above the actor too
            777: FakeRole(777, position=50),  # above the BOT: filtered, not named
            666: FakeRole(666, position=1),  # publishable
        },
        members={ACTOR_ID: actor},
    )
    pool = RMPool(count=0)
    bot = _rm_bot(pool, guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot,
        100,
        _menu_payload(
            options=[
                {"role_id": "888"},
                {"role_id": "777"},
                {"role_id": "999"},
                {"role_id": "666"},
            ]
        ),
        actor,
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [
            {"role_id": "888", "reason": "role_above_actor"},
            {"role_id": "999", "reason": "role_above_actor"},
        ],
    }
    assert channel.sent == []
    assert pool.inserted == []


async def test_role_menu_post_names_the_roles_rather_than_answering_bad_role_all(
    rolemenu_env,
):
    """Every option above the actor: the refusal names them instead of the
    unhelpful ``bad_role_all`` (which means "nothing publishable here")."""
    channel = FakeRMChannel(555)
    actor = _role_actor(top_position=3)
    guild = RMGuild(
        channels={555: channel},
        roles={888: FakeRole(888, position=5)},
        members={ACTOR_ID: actor},
    )
    bot = _rm_bot(RMPool(count=0), guild)

    result = await dashboard_actions._exec_role_menu_post(
        bot, 100, _menu_payload(options=[{"role_id": "888"}]), actor
    )

    assert result["error"] == "role_above_actor"
    assert result["failures"] == [{"role_id": "888", "reason": "role_above_actor"}]


class VerifySettingsPool(ActionsPool):
    """ActionsPool whose guild_settings read answers a configured verify_role.

    Returned as a JSON STRING, which is what asyncpg hands back for a JSONB
    column when no codec is registered (tools.settings._load handles both).
    """

    def __init__(self, verify_role=None):
        super().__init__()
        self.verify_role = verify_role

    async def fetchval(self, query, *args):
        if "SELECT settings FROM guild_settings" in query:
            if self.verify_role is None:
                return None
            return json.dumps({"verify_role": self.verify_role})
        return await super().fetchval(query, *args)


async def test_verify_button_post_refuses_a_verify_role_above_the_actor(verify_env):
    """The Verify button is a one-click SELF-GRANT of the configured role, so
    publishing it is publishing that role - /verify setup refuses the same case."""
    channel = FakeTextChannel(channel_id=555)
    actor = _verify_actor(top_position=3)
    guild = FakeGuild(
        channels={555: channel},
        roles={888: FakeRole(888, position=5)},
        members={ACTOR_ID: actor},
    )
    bot = FakeBot(VerifySettingsPool(verify_role="888"), guilds={100: guild})

    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}, actor
    )

    assert result == {
        "ok": False,
        "error": "role_above_actor",
        "failures": [{"role_id": "888", "reason": "role_above_actor"}],
    }
    assert channel.sent == []


async def test_verify_button_post_refuses_a_verify_role_yasuho_cannot_grant(verify_env):
    """Same two-code discipline as the other three kinds: the bot half first."""
    channel = FakeTextChannel(channel_id=555)
    actor = _verify_actor()
    guild = FakeGuild(
        channels={555: channel},
        roles={888: FakeRole(888, position=5, managed=True)},
        members={ACTOR_ID: actor},
    )
    bot = FakeBot(VerifySettingsPool(verify_role="888"), guilds={100: guild})

    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}, actor
    )

    assert result == {
        "ok": False,
        "error": "role_not_assignable",
        "failures": [{"role_id": "888", "reason": "role_not_assignable"}],
    }
    assert channel.sent == []


async def test_verify_button_post_accepts_a_verify_role_below_the_actor(verify_env):
    channel = FakeTextChannel(channel_id=555)
    actor = _verify_actor()
    guild = FakeGuild(
        channels={555: channel},
        roles={888: FakeRole(888, position=5)},
        members={ACTOR_ID: actor},
    )
    bot = FakeBot(VerifySettingsPool(verify_role="888"), guilds={100: guild})

    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}, actor
    )

    assert result["ok"] is True
    assert len(channel.sent) == 1


async def test_verify_button_post_still_posts_when_no_role_is_configured(verify_env):
    """Non-regression on the documented order: the button may be posted first and
    the role set after - there is nothing to gate yet."""
    channel = FakeTextChannel(channel_id=555)
    actor = _verify_actor(top_position=1)
    guild = FakeGuild(channels={555: channel}, members={ACTOR_ID: actor})
    bot = FakeBot(VerifySettingsPool(verify_role=None), guilds={100: guild})

    result = await dashboard_actions._exec_verify_button_post(
        bot, 100, {"channel_id": "555"}, actor
    )

    assert result["ok"] is True
    assert len(channel.sent) == 1
