"""Purpose: package entry point - exposes the profile cog to core's extension
discovery (a package whose __init__ defines ``setup`` is loaded whole).

THE social profile: one global row per user (no guild_id - a profile follows the
person, not the server), with per-FIELD visibility that starts fully private and
only opens where its owner says so.

Layout:
* registry.py   - which fields exist, their caps and their validators (pure);
* visibility.py - who may see which field, absent row = private (pure);
* storage.py    - the single-statement reads and writes;
* cog.py        - the Discord commands, re-homed from cogs/community/profiles.py.

This lot is the socle. The rendered card and the visibility panel are P2; the
AniList / Steam / Last.fm / osu! / Backloggd / presence connectors are P3-P4 and
already have their names reserved in the registry, so they need no schema change.

Data lifecycle: user-scoped, so the guild purge (tools/retention.py) does not and
must not touch it. Export and deletion live on the USER path in tools/privacy.py
(``collect_user_export`` and ``delete_user_profile``).

Typography rule: ASCII '-' and '...' only.
"""

from .cog import Profiles

__all__ = ("Profiles", "setup")


async def setup(bot):
    await bot.add_cog(Profiles(bot))
