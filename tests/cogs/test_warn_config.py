"""Tests for configurable warn escalation (Lot A2).

Three surfaces, all against the conftest fakes (no network / DB / Discord):

* The presentation helpers + the Components V2 panel in
  ``cogs/moderation/warn_config.py`` (duration formatting, the case-embed
  "Auto-action" line, the panel building across states inside the CV2 budget,
  and each interactive component persisting through ``settings``).
* The shared ``tools.modactions`` seams both the warn command and AutoMod use:
  ``load_escalation_policy`` (default / configured / malformed) and
  ``apply_escalation_action`` (timeout/kick/ban, suppression, failure).
* The ``Moderation.warn`` command's escalation hook end to end against a routed
  fake pool: fires AT a threshold, not past it, kick-at-3 for an unconfigured
  guild, and a clear degrade when the action fails.
"""

import datetime
import types

from cogs.moderation.moderation import Moderation
from cogs.moderation.warn_config import (
    ACTION_CHOICES,
    WarnConfigPanel,
    _ActionSelect,
    _RemoveRuleSelect,
    escalation_dm,
    escalation_failure_notice,
    escalation_summary,
    format_duration,
)
from tools import modactions, settings
from tools import warn_escalation as we


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeGuild:
    def __init__(self, guild_id=1, name="guild", fail=False):
        self.id = guild_id
        self.name = name
        self.fail = fail
        self.kicked = []
        self.banned = []

    async def kick(self, member, reason=None):
        if self.fail:
            raise RuntimeError("missing permissions")
        self.kicked.append((member, reason))

    async def ban(self, member, reason=None):
        if self.fail:
            raise RuntimeError("missing permissions")
        self.banned.append((member, reason))


class _FakeMember:
    def __init__(self, member_id=2, fail=False):
        self.id = member_id
        self.mention = f"<@{member_id}>"
        self.fail = fail
        self.timeouts = []
        self.dms = []

    async def timeout(self, delta, reason=None):
        if self.fail:
            raise RuntimeError("missing permissions")
        self.timeouts.append((delta, reason))

    async def send(self, content):
        self.dms.append(content)


class _FakeBot:
    def __init__(self, pool):
        self.db_pool = pool
        self.user = types.SimpleNamespace(id=999)

    def get_cog(self, name):
        # No ModLog cog loaded -> funnel_action / funnel_suppress are no-ops.
        return None


class _FakeCtx:
    def __init__(self, guild, author_id=5):
        self.guild = guild
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.command = None
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=123)

    async def send_help(self, *args, **kwargs):
        self.sends.append(("HELP", args, kwargs))


def _make_cog(pool):
    return Moderation(_FakeBot(pool))


def _state(policy=None, pending="timeout"):
    return {
        "policy": policy if policy is not None else we.default_policy(),
        "pending_action": pending,
    }


# ---------------------------------------------------------------------------
# Catalog invariants (guard against engine/UI vocabulary drift)
# ---------------------------------------------------------------------------
def test_action_catalog_matches_the_engine_actions():
    assert {value for value, *_ in ACTION_CHOICES} == set(we.VALID_ACTIONS)


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def test_format_duration_units():
    assert "10" in format_duration(600) and "minute" in format_duration(600)
    assert "hour" in format_duration(3600)
    assert "day" in format_duration(86400)
    # 28-day cap renders as whole days.
    assert format_duration(we.MAX_TIMEOUT_SECONDS) == "28 day(s)"


def test_escalation_summary_per_action():
    assert "kicked" in escalation_summary(
        3, {"threshold": 3, "action": "kick", "duration": None}
    )
    assert "banned" in escalation_summary(
        7, {"threshold": 7, "action": "ban", "duration": None}
    )
    timeout = escalation_summary(
        2, {"threshold": 2, "action": "timeout", "duration": 600}
    )
    assert "timed out" in timeout and "10 minute" in timeout


def test_escalation_failure_and_dm_mention_the_count():
    notice = escalation_failure_notice(
        "<@2>", 3, {"threshold": 3, "action": "kick", "duration": None}
    )
    assert "<@2>" in notice and "3 warns" in notice
    dm = escalation_dm(
        "Cool Server", 3, {"threshold": 3, "action": "ban", "duration": None}
    )
    assert "Cool Server" in dm and "banned" in dm


# ---------------------------------------------------------------------------
# Panel builds across states (presentational: assemble cleanly, stay in budget)
# ---------------------------------------------------------------------------
def _text_chars(view):
    total = 0

    def walk(item):
        nonlocal total
        content = getattr(item, "content", None)
        if isinstance(content, str):
            total += len(content)
        for child in getattr(item, "children", None) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return total


def test_panel_builds_with_default_policy(fake_pool):
    cog = _make_cog(fake_pool)
    view = WarnConfigPanel(cog, _FakeGuild(), 1, _state())
    assert len(view.children) == 1  # a single Container
    assert _text_chars(view) < 4000


def test_panel_builds_with_empty_policy(fake_pool):
    cog = _make_cog(fake_pool)
    view = WarnConfigPanel(cog, _FakeGuild(), 1, _state(policy=[]))
    assert _text_chars(view) < 4000


def test_panel_stays_in_budget_at_max_rules(fake_pool):
    cog = _make_cog(fake_pool)
    policy = [
        {"threshold": i, "action": "timeout", "duration": 600}
        for i in range(1, we.MAX_RULES + 1)
    ]
    view = WarnConfigPanel(cog, _FakeGuild(), 1, _state(policy=policy))
    assert _text_chars(view) < 4000


def test_action_select_preselects_pending_action(fake_pool):
    cog = _make_cog(fake_pool)
    panel = WarnConfigPanel(cog, _FakeGuild(), 1, _state(pending="ban"))
    select = _ActionSelect(panel)
    defaulted = [o.value for o in select.options if o.default]
    assert defaulted == ["ban"]
    assert {o.value for o in select.options} == set(we.VALID_ACTIONS)


def test_remove_select_lists_one_option_per_rule(fake_pool):
    cog = _make_cog(fake_pool)
    policy = [
        {"threshold": 3, "action": "timeout", "duration": 600},
        {"threshold": 5, "action": "kick", "duration": None},
    ]
    panel = WarnConfigPanel(cog, _FakeGuild(), 1, _state(policy=policy))
    select = _RemoveRuleSelect(panel)
    assert {o.value for o in select.options} == {"3", "5"}


# ---------------------------------------------------------------------------
# Panel callbacks persist through settings and re-render in place
# ---------------------------------------------------------------------------
def _panel(cog, guild_id=8, policy=None, pending="timeout"):
    panel = WarnConfigPanel(
        cog, _FakeGuild(guild_id=guild_id), 1, _state(policy=policy, pending=pending)
    )
    panel.message = types.SimpleNamespace()
    return panel


async def test_set_pending_action_updates_state_and_rerenders(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = _panel(cog)
    interaction = make_interaction()

    await panel.set_pending_action(interaction, "ban")

    assert panel.state["pending_action"] == "ban"
    assert len(interaction.edits) == 1  # view=-only in-place refresh


async def test_add_rule_persists_and_rerenders(fake_pool, make_interaction):
    settings._cache.clear()
    cog = _make_cog(fake_pool)
    panel = _panel(cog, guild_id=41, policy=[])
    interaction = make_interaction()

    await panel.add_rule(interaction, 3, "timeout", 600)

    assert panel.state["policy"] == [
        {"threshold": 3, "action": "timeout", "duration": 600}
    ]
    # persisted to the guild_settings blob
    assert any(
        c[0] == "execute" and "guild_settings" in c[1] for c in fake_pool.calls
    )
    assert len(interaction.edits) == 1


async def test_add_rule_at_cap_refuses(fake_pool, make_interaction):
    settings._cache.clear()
    cog = _make_cog(fake_pool)
    full = [
        {"threshold": i, "action": "kick", "duration": None}
        for i in range(1, we.MAX_RULES + 1)
    ]
    panel = _panel(cog, guild_id=42, policy=full)
    interaction = make_interaction()

    await panel.add_rule(interaction, 99, "ban", None)  # a genuinely new threshold

    assert interaction.sent, "an ephemeral refusal should be sent"
    assert "maximum" in interaction.sent[0][0][0]
    # nothing written, nothing re-rendered
    assert not any(c[0] == "execute" for c in fake_pool.calls)
    assert interaction.edits == []


async def test_add_rule_replacing_a_threshold_updates_action(fake_pool, make_interaction):
    settings._cache.clear()
    cog = _make_cog(fake_pool)
    panel = _panel(
        cog,
        guild_id=43,
        policy=[{"threshold": 3, "action": "kick", "duration": None}],
    )
    await panel.add_rule(make_interaction(), 3, "ban", None)
    assert panel.state["policy"] == [
        {"threshold": 3, "action": "ban", "duration": None}
    ]


async def test_remove_rule_persists(fake_pool, make_interaction):
    settings._cache.clear()
    cog = _make_cog(fake_pool)
    panel = _panel(
        cog,
        guild_id=44,
        policy=[
            {"threshold": 3, "action": "kick", "duration": None},
            {"threshold": 5, "action": "ban", "duration": None},
        ],
    )
    await panel.remove_rule(make_interaction(), 3)
    assert panel.state["policy"] == [
        {"threshold": 5, "action": "ban", "duration": None}
    ]


async def test_reset_default_writes_kick_at_three(fake_pool, make_interaction):
    settings._cache.clear()
    cog = _make_cog(fake_pool)
    panel = _panel(cog, guild_id=45, policy=[])
    await panel.reset_default(make_interaction())
    assert panel.state["policy"] == we.default_policy()


# ---------------------------------------------------------------------------
# tools.modactions.load_escalation_policy
# ---------------------------------------------------------------------------
async def test_load_policy_unconfigured_is_default(fake_pool):
    settings._cache.clear()
    fake_pool.fetchval_return = None  # no guild_settings row
    policy, showing_default = await modactions.load_escalation_policy(fake_pool, 501)
    assert policy == we.default_policy()
    assert showing_default is True


async def test_load_policy_configured_is_used(fake_pool):
    settings._cache.clear()
    fake_pool.fetchval_return = {
        "warn_escalation": [{"threshold": 5, "action": "kick", "duration": None}]
    }
    policy, showing_default = await modactions.load_escalation_policy(fake_pool, 502)
    assert policy == [{"threshold": 5, "action": "kick", "duration": None}]
    assert showing_default is False


async def test_load_policy_malformed_falls_back_and_logs(fake_pool, caplog):
    settings._cache.clear()
    fake_pool.fetchval_return = {"warn_escalation": "not-a-list"}
    with caplog.at_level("WARNING"):
        policy, showing_default = await modactions.load_escalation_policy(
            fake_pool, 503
        )
    assert policy == we.default_policy()
    assert showing_default is True
    assert any("malformed warn_escalation" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# tools.modactions.apply_escalation_action
# ---------------------------------------------------------------------------
async def test_apply_timeout_calls_member_timeout(fake_pool):
    bot = _FakeBot(fake_pool)
    guild = _FakeGuild()
    member = _FakeMember()
    rule = {"threshold": 3, "action": "timeout", "duration": 600}

    ok = await modactions.apply_escalation_action(bot, guild, member, rule)

    assert ok is True
    assert len(member.timeouts) == 1
    delta, _reason = member.timeouts[0]
    assert delta == datetime.timedelta(seconds=600)


async def test_apply_kick_calls_guild_kick(fake_pool):
    bot = _FakeBot(fake_pool)
    guild = _FakeGuild()
    member = _FakeMember()
    rule = {"threshold": 3, "action": "kick", "duration": None}

    ok = await modactions.apply_escalation_action(bot, guild, member, rule)

    assert ok is True
    assert len(guild.kicked) == 1


async def test_apply_ban_calls_guild_ban(fake_pool):
    bot = _FakeBot(fake_pool)
    guild = _FakeGuild()
    member = _FakeMember()
    rule = {"threshold": 7, "action": "ban", "duration": None}

    ok = await modactions.apply_escalation_action(bot, guild, member, rule)

    assert ok is True
    assert len(guild.banned) == 1


async def test_apply_action_failure_returns_false(fake_pool):
    bot = _FakeBot(fake_pool)
    guild = _FakeGuild(fail=True)  # guild.kick raises
    member = _FakeMember()
    rule = {"threshold": 3, "action": "kick", "duration": None}

    ok = await modactions.apply_escalation_action(bot, guild, member, rule)

    assert ok is False
    assert guild.kicked == []


# ---------------------------------------------------------------------------
# The Moderation.warn escalation hook, end to end against a routed fake pool.
# ---------------------------------------------------------------------------
class _WarnCmdPool:
    """Routes the warn command's three DB touches by query.

    create_case -> fetchrow (RETURNING case_number); bump_warn -> fetchval on
    the warns upsert (returns the new running count); the settings read ->
    fetchval on guild_settings (returns the stored blob or None).
    """

    def __init__(self, new_count, settings_blob=None, case_number=1):
        self.new_count = new_count
        self.settings_blob = settings_blob
        self.case_number = case_number
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "INSERT INTO cases" in query:
            return {"case_number": self.case_number}
        return None

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "warns" in query and "RETURNING warns_count" in query:
            return self.new_count
        if "SELECT settings FROM guild_settings" in query:
            return self.settings_blob
        return None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "INSERT 0 1"


def _warn_field(ctx, name):
    """The value of the embed field ``name`` from the first embed send, or None."""
    for args, kwargs in ctx.sends:
        embed = kwargs.get("embed")
        if embed is None:
            continue
        for field in embed.fields:
            if field.name == name:
                return field.value
    return None


async def test_warn_below_threshold_records_only(fake_pool):
    settings._cache.clear()
    pool = _WarnCmdPool(new_count=2)  # unconfigured -> default kick@3
    cog = _make_cog(pool)
    guild = _FakeGuild(guild_id=601)
    member = _FakeMember(2)
    ctx = _FakeCtx(guild)

    await Moderation.warn.callback(cog, ctx, member, reason="spam")

    assert guild.kicked == []
    assert _warn_field(ctx, "Warns") == "2"
    assert _warn_field(ctx, "Auto-action") is None


async def test_warn_default_kicks_at_three(fake_pool):
    settings._cache.clear()
    pool = _WarnCmdPool(new_count=3)  # unconfigured guild -> default kick at 3
    cog = _make_cog(pool)
    guild = _FakeGuild(guild_id=602)
    member = _FakeMember(2)
    ctx = _FakeCtx(guild)

    await Moderation.warn.callback(cog, ctx, member, reason="last straw")

    assert len(guild.kicked) == 1  # kicked at exactly 3
    assert "kicked" in _warn_field(ctx, "Auto-action")
    assert member.dms, "the kicked member should be DMed"


async def test_warn_does_not_refire_past_threshold(fake_pool):
    settings._cache.clear()
    pool = _WarnCmdPool(new_count=4)  # already past the default threshold of 3
    cog = _make_cog(pool)
    guild = _FakeGuild(guild_id=603)
    member = _FakeMember(2)
    ctx = _FakeCtx(guild)

    await Moderation.warn.callback(cog, ctx, member, reason="another")

    assert guild.kicked == []  # equals-only: 4 does not re-fire the rule at 3
    assert _warn_field(ctx, "Warns") == "4"
    assert _warn_field(ctx, "Auto-action") is None


async def test_warn_configured_timeout_fires(fake_pool):
    settings._cache.clear()
    blob = {
        "warn_escalation": [{"threshold": 3, "action": "timeout", "duration": 600}]
    }
    pool = _WarnCmdPool(new_count=3, settings_blob=blob)
    cog = _make_cog(pool)
    guild = _FakeGuild(guild_id=604)
    member = _FakeMember(2)
    ctx = _FakeCtx(guild)

    await Moderation.warn.callback(cog, ctx, member, reason="rowdy")

    assert len(member.timeouts) == 1
    assert guild.kicked == []
    assert "timed out" in _warn_field(ctx, "Auto-action")


async def test_warn_action_failure_degrades(fake_pool):
    settings._cache.clear()
    pool = _WarnCmdPool(new_count=3)  # default kick@3
    cog = _make_cog(pool)
    guild = _FakeGuild(guild_id=605, fail=True)  # kick will raise
    member = _FakeMember(2)
    ctx = _FakeCtx(guild)

    await Moderation.warn.callback(cog, ctx, member, reason="oops")

    # The warn is still recorded (a case embed was sent) and the Auto-action
    # line still explains the intent, but a clear failure notice follows and no
    # DM is sent.
    assert _warn_field(ctx, "Auto-action") is not None
    assert member.dms == []
    text_sends = [
        args[0]
        for args, kwargs in ctx.sends
        if args and isinstance(args[0], str)
    ]
    assert any("couldn't kick" in s for s in text_sends)
