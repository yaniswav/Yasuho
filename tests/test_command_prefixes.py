"""Servers answer to their text prefix, the bot mention and slash commands.

The per-guild prefix (bot.prefixes) overrides the default; the mention always
works, including once Discord stops delivering message content. DMs keep the
default text prefix. Custom commands are reachable by prefix, mention and
`/custom`.
"""

import types

import pytest

import core

from cogs.config.customcommands import CustomCommands

BOT_ID = 999


def _bot(prefixes=None):
    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=BOT_ID),
        prefixes=prefixes if prefixes is not None else {},
    )


def _message(guild_id):
    guild = types.SimpleNamespace(id=guild_id) if guild_id else None
    return types.SimpleNamespace(guild=guild)


async def test_guild_prefixes_are_the_server_prefix_and_the_mentions():
    result = await core.get_prefix(_bot(prefixes={1: "y!"}), _message(1))
    assert set(result) == {"y!", f"<@{BOT_ID}> ", f"<@!{BOT_ID}> "}
    default = await core.get_prefix(_bot(), _message(2))
    assert core.DEFAULT_PREFIX in default


async def test_dm_keeps_the_text_prefix():
    assert await core.get_prefix(_bot(), _message(None)) == core.DEFAULT_PREFIX


# -- /custom ------------------------------------------------------------------


class _Ctx:
    def __init__(self):
        self.guild = types.SimpleNamespace(id=1, name="Guild", member_count=10)
        self.author = types.SimpleNamespace(id=42, mention="<@42>")
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Pool:
    def __init__(self):
        self.executed = []

    async def execute(self, *args):
        self.executed.append(args)


def _cog(commands_map):
    cog = CustomCommands(types.SimpleNamespace(db_pool=_Pool()))
    cog._cache[1] = commands_map
    cog._uses[1] = {name: 0 for name in commands_map}
    return cog


async def test_custom_runs_by_name_or_alias_and_scopes_mentions():
    cog = _cog({"greet": {"type": "text", "content": "hi @everyone", "aliases": ["hello"]}})
    ctx = _Ctx()
    await cog.custom.callback(cog, ctx, name=" HELLO ")
    (args, kwargs), = ctx.sent
    assert kwargs["content"] == "hi @everyone"
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False
    assert mentions.users == [ctx.author]
    assert cog._uses[1]["greet"] == 1


async def test_custom_unknown_name_answers_privately():
    cog = _cog({})
    ctx = _Ctx()
    await cog.custom.callback(cog, ctx, name="nope")
    (args, kwargs), = ctx.sent
    assert kwargs.get("ephemeral") is True


async def test_custom_shares_the_cooldown_with_the_mention_path():
    cog = _cog({"greet": {"type": "text", "content": "hi", "cooldown": 60}})
    ctx = _Ctx()
    await cog.custom.callback(cog, ctx, name="greet")
    await cog.custom.callback(cog, ctx, name="greet")
    assert "content" in ctx.sent[0][1]
    assert ctx.sent[1][1].get("ephemeral") is True
    # The mention path sees the same cooldown and stays silent.
    assert cog._cooling(1, "greet", 42, {"cooldown": 60}) is True


async def test_custom_autocomplete_filters_this_guilds_names():
    cog = _cog({"rules": {}, "roles": {}, "welcome": {}})
    interaction = types.SimpleNamespace(guild=types.SimpleNamespace(id=1))
    choices = await cog._custom_autocomplete(interaction, "ro")
    assert [c.value for c in choices] == ["roles"]


@pytest.mark.parametrize("response", [
    {"type": "text", "content": ""},
    {"type": "embed", "embed": {}},
])
async def test_custom_empty_response_sends_nothing_public(response):
    cog = _cog({"blank": response})
    ctx = _Ctx()
    await cog.custom.callback(cog, ctx, name="blank")
    (args, kwargs), = ctx.sent
    assert kwargs.get("ephemeral") is True


async def test_bare_custom_still_reaches_a_stored_command_named_custom():
    # Stored before /custom existed: `@bot custom` with no argument runs it.
    cog = _cog({"custom": {"type": "text", "content": "legacy"}})
    ctx = _Ctx()
    ctx.invoked_with = "custom"
    await cog.custom.callback(cog, ctx)
    (args, kwargs), = ctx.sent
    assert kwargs["content"] == "legacy"
