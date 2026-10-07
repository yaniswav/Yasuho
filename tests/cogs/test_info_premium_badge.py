"""The Yasuho+ badge: a footer on /info server, only for a Yasuho+ server."""

import datetime
import types

from cogs.utility import info as info_cog
from tools import premium


class _Ctx:
    def __init__(self, guild):
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


def _guild():
    return types.SimpleNamespace(
        id=42,
        name="Guild",
        icon=None,
        owner=None,
        created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        member_count=10,
        text_channels=[],
        voice_channels=[],
        roles=[],
        premium_tier=0,
        premium_subscription_count=0,
    )


class _Resolver:
    def __init__(self, limits=None, error=None):
        self.limits = limits
        self.error = error

    def for_guild(self, guild_id):
        if self.error:
            raise self.error
        return self.limits


async def _footer(bot):
    cog = object.__new__(info_cog.Info)
    cog.bot = bot
    ctx = _Ctx(_guild())
    await cog.info_server.callback(cog, ctx)
    return ctx.sent[0]["embed"].footer.text


async def test_a_yasuho_plus_server_gets_the_badge():
    bot = types.SimpleNamespace(premium=_Resolver(premium.GUILD_PREMIUM))
    assert await _footer(bot) == "Yasuho+ server"


async def test_a_free_server_gets_no_badge():
    bot = types.SimpleNamespace(premium=_Resolver(premium.GUILD_FREE))
    assert await _footer(bot) is None


async def test_a_failing_lookup_shows_no_badge():
    bot = types.SimpleNamespace(premium=_Resolver(error=RuntimeError("boom")))
    assert await _footer(bot) is None
    assert await _footer(types.SimpleNamespace()) is None
