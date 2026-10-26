"""Link / invite filtering runs as ONE Discord native AutoMod rule.

The bot never reads message text for these filters (the Message Content
intent is requested for moderation logging only). It keeps its one managed
rule - owned by id, not by name - in step with the saved settings and
exemptions, and applies the server's own escalation when Discord reports that
the rule blocked a message.
"""

import asyncio
import re
import types

import discord
import pytest

from cogs.moderation import automod
from cogs.moderation.automod import AutoMod

GUILD = 9
NAME = AutoMod.FILTER_RULE_NAME
KEY = AutoMod.FILTER_RULE_KEY


class _Rule:
    def __init__(self, rule_id, name):
        self.id = rule_id
        self.name = name
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def _role(role_id, *, manage_messages=False, administrator=False, default=False):
    return types.SimpleNamespace(
        id=role_id,
        permissions=types.SimpleNamespace(
            manage_messages=manage_messages, administrator=administrator
        ),
        is_default=lambda: default,
    )


class _Guild:
    def __init__(self, rules=(), *, error=None, roles=()):
        self.id = GUILD
        self.rules = list(rules)
        self.roles = list(roles)
        self.created = []
        self.error = error
        self.fetched_members = []

    async def fetch_automod_rules(self):
        await asyncio.sleep(0)  # a real await, so concurrent syncs interleave
        if self.error is not None:
            raise self.error
        return list(self.rules)

    async def create_automod_rule(self, **kwargs):
        rule = _Rule(100 + len(self.created), kwargs["name"])
        rule.kwargs = kwargs
        self.created.append(rule)
        self.rules.append(rule)
        return rule

    async def fetch_member(self, user_id):
        self.fetched_members.append(user_id)
        return _member(user_id)


def _member(user_id, *, manage_messages=False, bot=False):
    return types.SimpleNamespace(
        id=user_id, bot=bot, mention=f"<@{user_id}>",
        guild_permissions=types.SimpleNamespace(manage_messages=manage_messages),
    )


def _http_error(status):
    return discord.HTTPException(types.SimpleNamespace(status=status, reason="x"), "no")


def _cog(monkeypatch, *, antilink=False, antiinvite=False, roles=(), channels=(), stored=None):
    cog = AutoMod(types.SimpleNamespace(db_pool=object(), user=types.SimpleNamespace(id=1)))

    async def _enabled(_guild_id):
        return antilink, False, antiinvite

    blob = {
        "automod_exempt_roles": list(roles),
        "automod_exempt_channels": list(channels),
    }
    if stored is not None:
        blob[KEY] = stored

    async def _get_guild(_pool, _gid, key, default=None):
        return blob.get(key, default)

    async def _set_guild(_pool, _gid, key, value):
        blob[key] = value

    cog._enabled = _enabled
    cog.blob = blob
    monkeypatch.setattr(automod.settings, "get_guild", _get_guild)
    monkeypatch.setattr(automod.settings, "set_guild", _set_guild)
    return cog


async def test_one_rule_carries_both_filters_and_its_id_is_stored(monkeypatch):
    cog = _cog(monkeypatch, antilink=True, antiinvite=True)
    guild = _Guild()
    assert await cog.sync_native_filters(guild) is True

    (rule,) = guild.created
    kwargs = rule.kwargs
    assert kwargs["name"] == NAME
    assert kwargs["trigger"].type == discord.AutoModRuleTriggerType.keyword
    assert kwargs["trigger"].regex_patterns == (
        AutoMod.FILTER_PATTERNS["invite"] + AutoMod.FILTER_PATTERNS["link"]
    )
    assert kwargs["actions"][0].type == discord.AutoModRuleActionType.block_message
    assert kwargs["enabled"] is True
    assert cog.blob[KEY] == rule.id


async def test_ownership_is_by_id_so_a_renamed_rule_is_still_ours(monkeypatch):
    cog = _cog(monkeypatch, antilink=False, stored=5)
    renamed = _Rule(5, "Renamed by an admin")
    guild = _Guild(rules=[renamed])
    assert await cog.sync_native_filters(guild) is True
    assert guild.created == []
    assert renamed.edits[-1]["enabled"] is False
    # The block action is always restored, whatever an admin changed.
    assert renamed.edits[-1]["actions"][0].type == discord.AutoModRuleActionType.block_message


async def test_concurrent_syncs_create_the_rule_once(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True)
    guild = _Guild()
    results = await asyncio.gather(
        cog.sync_native_filters(guild), cog.sync_native_filters(guild)
    )
    assert results == [True, True]
    assert len(guild.created) == 1


async def test_nothing_is_created_while_both_filters_are_off(monkeypatch):
    cog = _cog(monkeypatch)
    guild = _Guild()
    assert await cog.sync_native_filters(guild) is True
    assert guild.created == [] and KEY not in cog.blob


@pytest.mark.parametrize("error", [
    discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no"),
    _http_error(400),  # e.g. the six-keyword-rule limit
])
async def test_a_refused_sync_reports_failure(monkeypatch, error):
    cog = _cog(monkeypatch, antilink=True)
    assert await cog.sync_native_filters(_Guild(error=error)) is False


async def test_moderator_roles_are_exempt_and_caps_are_reported(monkeypatch):
    cog = _cog(monkeypatch, antilink=True, roles=[1, 2])
    roles = [
        _role(GUILD, manage_messages=True, default=True),  # @everyone never counts
        _role(3, manage_messages=True),
        _role(4, administrator=True),
        _role(5),
    ]
    guild = _Guild(roles=roles)
    assert await cog.sync_native_filters(guild) is True
    exempt = [o.id for o in guild.created[0].kwargs["exempt_roles"]]
    assert exempt == [1, 2, 3, 4]

    capped = _cog(monkeypatch, antilink=True, roles=range(1, 26))
    guild = _Guild()
    assert await capped.sync_native_filters(guild) is False  # truncated, said so
    assert len(guild.created[0].kwargs["exempt_roles"]) == 20


def test_native_patterns_match_what_the_python_filters_match():
    samples = {
        "link": ["see https://example.com/x", "discord.gg/abc"],
        "invite": ["join discord.gg/abc", "https://discord.com/invite/x-y"],
    }
    for key, texts in samples.items():
        patterns = AutoMod.FILTER_PATTERNS[key]
        assert all(len(p) <= 260 for p in patterns)
        compiled = [re.compile(p) for p in patterns]
        for text in texts:
            assert any(c.search(text) for c in compiled), (key, text)
        assert not any(c.search("nothing to see here") for c in compiled)


def _execution(rule_id, *, member, matched=None,
               action_type=discord.AutoModRuleActionType.block_message):
    return types.SimpleNamespace(
        guild=_Guild(), rule_id=rule_id, member=member, user_id=77,
        channel=types.SimpleNamespace(id=3), matched_keyword=matched,
        action=types.SimpleNamespace(type=action_type),
    )


def _punishments(monkeypatch, cog):
    calls = []

    async def _punish(guild, member, channel, *, notice, reason):
        calls.append((member.id, reason))

    monkeypatch.setattr(cog, "_punish", _punish)
    return calls


async def test_a_blocked_invite_triggers_the_server_escalation(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True, stored=5)
    calls = _punishments(monkeypatch, cog)
    invite = AutoMod.FILTER_PATTERNS["invite"][0]
    await cog.on_automod_action(_execution(5, member=_member(77), matched=invite))
    assert calls == [(77, "Posted a Discord invite link")]


async def test_an_uncached_member_is_fetched(monkeypatch):
    cog = _cog(monkeypatch, antilink=True, stored=5)
    calls = _punishments(monkeypatch, cog)
    execution = _execution(5, member=None, matched=AutoMod.FILTER_PATTERNS["link"][0])
    await cog.on_automod_action(execution)
    assert execution.guild.fetched_members == [77]
    assert calls == [(77, "Posted a disallowed link")]


@pytest.mark.parametrize("case", ["foreign_rule", "moderator", "alert_action", "filters_off"])
async def test_what_the_escalation_leaves_alone(monkeypatch, case):
    on = case != "filters_off"
    cog = _cog(monkeypatch, antilink=on, stored=5)
    calls = _punishments(monkeypatch, cog)
    if case == "foreign_rule":
        execution = _execution(8, member=_member(77))
    elif case == "moderator":
        execution = _execution(5, member=_member(77, manage_messages=True))
    elif case == "alert_action":
        execution = _execution(5, member=_member(77),
                               action_type=discord.AutoModRuleActionType.send_alert_message)
    else:
        execution = _execution(5, member=_member(77))
    await cog.on_automod_action(execution)
    assert calls == []


def test_the_bot_subscribes_to_automod_executions():
    import core

    source = open(core.__file__, encoding="utf-8").read()
    assert "intents.auto_moderation_execution = True" in source


async def test_a_deleted_rule_is_replaced_not_swapped_for_a_namesake(monkeypatch):
    cog = _cog(monkeypatch, antilink=True, stored=5)
    namesake = _Rule(7, NAME)  # someone else's rule that happens to share the name
    guild = _Guild(rules=[namesake])
    assert await cog.sync_native_filters(guild) is True
    assert namesake.edits == []
    (created,) = guild.created
    assert cog.blob[KEY] == created.id


async def test_one_guild_failing_does_not_stop_the_reconciliation(monkeypatch):
    cog = _cog(monkeypatch, antilink=True)
    guilds = {1: types.SimpleNamespace(id=1), 2: types.SimpleNamespace(id=2)}
    seen = []

    async def _sync(guild):
        seen.append(guild)
        if guild is guilds[1]:
            raise RuntimeError("boom")
        return True

    async def _fetch(*_args):
        return [{"guild_id": 1}, {"guild_id": 2}]

    async def _no_sleep(_s):
        return None

    cog.sync_native_filters = _sync
    cog.bot.db_pool = types.SimpleNamespace(fetch=_fetch)
    cog.bot.get_guild = guilds.get
    monkeypatch.setattr(automod.asyncio, "sleep", _no_sleep)
    await cog.reconcile_all()
    assert seen == [guilds[1], guilds[2]]


async def test_reconcile_requests_coalesce_into_one_task(monkeypatch):
    cog = _cog(monkeypatch)
    passes = []
    gate = asyncio.Event()

    async def _pass():
        passes.append(1)
        await gate.wait()

    cog.reconcile_all = _pass
    cog.request_reconcile()
    await asyncio.sleep(0)
    cog.request_reconcile()
    cog.request_reconcile()
    task = cog._reconcile_task
    gate.set()
    await task
    assert len(passes) == 2  # the running pass + exactly one queued rerun


async def test_a_new_moderator_role_resyncs_the_exemptions(monkeypatch):
    cog = _cog(monkeypatch, antilink=True, stored=5)
    synced = []

    async def _sync(guild):
        synced.append(guild)
        return True

    cog.sync_native_filters = _sync
    guild = _Guild()
    mod = _role(11, manage_messages=True)
    mod.guild = guild
    plain = _role(12)
    plain.guild = guild
    await cog.on_guild_role_create(plain)
    await cog.on_guild_role_create(mod)
    assert synced == [guild]
