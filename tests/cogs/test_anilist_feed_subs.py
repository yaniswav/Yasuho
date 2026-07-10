"""Regression tests for the tracked-releases subscription guard on the feed cog.

Only the pre-DB validation of :meth:`AniListFeed._add_channel_sub` is exercised
here: the guard returns before any pool access, so the cog is built with
``__new__`` (no bot, no database, no Discord). The invariant under test is that a
subscription with no usable cached display title is rejected rather than stored -
a MANGA with title=NULL can never be MangaDex-mapped, so it would become a silent
no-op the admin was falsely told is tracked.
"""

from cogs.anilist.feed import AniListFeed
from tools.i18n import _

_REJECT = _("I couldn't read that title - try searching again.")


def _cog():
    # Bypass __init__: the guard we test runs before any self.bot / db access.
    return AniListFeed.__new__(AniListFeed)


async def test_add_channel_sub_rejects_empty_title():
    cog = _cog()
    for title in (None, "", "   "):
        assert await cog._add_channel_sub(1, 2, 3, "MANGA", title, 4) == _REJECT
        assert await cog._add_channel_sub(1, 2, 3, "ANIME", title, 4) == _REJECT


async def test_add_channel_sub_rejects_bad_media():
    cog = _cog()
    assert await cog._add_channel_sub(1, 2, None, "MANGA", "Berserk", 4) == _REJECT
    assert await cog._add_channel_sub(1, 2, 3, "CHARACTER", "Berserk", 4) == _REJECT
