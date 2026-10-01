"""``?unmute`` must not crash when the guild has never configured a mute role.

Before the fix, passing ``None`` to ``member.remove_roles`` raised inside
discord.py, landing in the generic ``except Exception`` and getting logged
with ``log.exception`` as an unexpected failure, instead of a clear "there is
no mute role" answer.
"""

import logging
import types

import pytest

from cogs.moderation.moderation import Moderation


class _FakeGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.roles = []  # no mute role exists


class _FakeMember:
    def __init__(self, uid=2):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.roles = []

    async def remove_roles(self, *roles, reason=None):
        raise AssertionError("remove_roles must not be called with no mute role")


class _FakeCtx:
    def __init__(self, guild):
        self.guild = guild
        self.author = types.SimpleNamespace(id=1, mention="<@1>")
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace()


class _FakeDBPool:
    async def fetchval(self, *args, **kwargs):
        return None  # no muterole row either


class _FakeBot:
    def __init__(self):
        self.db_pool = _FakeDBPool()
        self.muteroles = {}


@pytest.mark.asyncio
async def test_unmute_with_no_mute_role_sends_clear_message_no_exception(caplog):
    cog = Moderation(_FakeBot())
    ctx = _FakeCtx(_FakeGuild())
    member = _FakeMember()

    with caplog.at_level(logging.ERROR):
        await cog.unmute.callback(cog, ctx, member)

    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert "mute role" in args[0].lower()
    # No log.exception ("Failed to unmute member") must have fired.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
