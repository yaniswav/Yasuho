"""CustomCommands._run must never let a stored admin text ping arbitrary members.

The client's default AllowedMentions lets stored text ping anyone it names -
any member can trigger a custom command, so that text becomes a ping vector
under someone else's control. The text branch must scope the send down to
only the invoking member (no everyone, no roles, no reply-ping).
"""

import types

import discord
import pytest

from cogs.config.customcommands import CustomCommands


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return types.SimpleNamespace(id=1)


class _FakeGuild:
    id = 1
    name = "Guild"
    member_count = 10


class _FakeAuthor:
    id = 42
    mention = "<@42>"


class _FakeMessage:
    def __init__(self):
        self.guild = _FakeGuild()
        self.author = _FakeAuthor()
        self.channel = _FakeChannel()


class _FakeDBPool:
    async def execute(self, *args, **kwargs):
        return None


class _FakeBot:
    def __init__(self):
        self.db_pool = _FakeDBPool()


@pytest.mark.asyncio
async def test_text_branch_scopes_mentions_to_invoker_only():
    cog = CustomCommands(_FakeBot())
    message = _FakeMessage()
    response = {"type": "text", "content": "ping @everyone and <@999>"}

    await cog._run(message, "greet", response)

    assert len(message.channel.sent) == 1
    args, kwargs = message.channel.sent[0]
    mentions = kwargs.get("allowed_mentions")
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    assert mentions.users == [message.author]
