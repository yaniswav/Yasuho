import logging

from discord.ext import commands

from .account import AccountMixin
from .base import AniListBase
from .feed import AniListFeed
from .lookup import LookupMixin

log = logging.getLogger(__name__)


class AniList(LookupMixin, AccountMixin, AniListBase, commands.Cog):
    """AniList lookups plus per-user account linking to edit your lists."""


async def setup(bot):
    await bot.add_cog(AniList(bot))
    await bot.add_cog(AniListFeed(bot))
