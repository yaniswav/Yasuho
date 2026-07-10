import logging

from discord.ext import commands

from .account import AccountMixin
from .airing import AiringMixin, AniListAiring
from .base import AniListBase
from .chapters import AniListChapters, ChaptersMixin
from .collection import CollectionMixin
from .feed import AniListFeed
from .hub import HubMixin
from .lookup import LookupMixin

log = logging.getLogger(__name__)


class AniList(
    LookupMixin,
    AccountMixin,
    AiringMixin,
    ChaptersMixin,
    HubMixin,
    CollectionMixin,
    AniListBase,
    commands.Cog,
):
    """AniList lookups plus per-user account linking to edit your lists."""


async def setup(bot):
    await bot.add_cog(AniList(bot))
    await bot.add_cog(AniListFeed(bot))
    await bot.add_cog(AniListAiring(bot))
    await bot.add_cog(AniListChapters(bot))
