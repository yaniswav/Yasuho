"""Purpose: package entry point - exposes the profile cog to core's extension
discovery (a package whose __init__ defines ``setup`` is loaded whole).

THE social profile: one global row per user (no guild_id - a profile follows the
person, not the server), with per-FIELD visibility that starts fully private and
only opens where its owner says so.

Layout:
* registry.py   - which fields exist, their caps and their validators (pure);
* visibility.py - who may see which field, absent row = private (pure);
* storage.py    - the single-statement reads and writes;
* cog.py        - the Discord commands, re-homed from cogs/community/profiles.py;
* presence.py   - the two opt-in DISCORD-PRESENCE sections (games played,
                  Spotify live) - its own cog, because it owns a hot gateway
                  listener and a flush loop that have nothing to do with the
                  command surface;
* connectors/   - the external-account framework (its own cog, see below).

This lot is the socle. The rendered card and the visibility panel are P2; the
AniList / Steam / Last.fm / osu! / Backloggd / presence connectors are P3-P5 and
already have their names reserved in the registry, so they need no schema change.

THREE cogs, one extension: core's discovery stops at the first ``__init__`` that
defines ``setup`` and does not descend further, so ``connectors/`` is a
sub-package rather than an extension of its own and is added here. They stay
separate cogs because a hybrid subcommand must live in the same cog as its
group, and ``connections`` is a root group of its own (``profile`` cannot adopt
it across cogs). ``ProfilePresence`` declares no command at all for the same
reason in reverse: ``/profile presence`` belongs to the ``profile`` group, so
it is DECLARED in cog.py and delegates its body here through ``get_cog`` -
the same fold as ``/levelconfig``.

Data lifecycle: user-scoped, so the guild purge (tools/retention.py) does not and
must not touch it. Export and deletion live on the USER path in tools/privacy.py
(``collect_user_export`` and ``delete_user_profile``).

Typography rule: ASCII '-' and '...' only.
"""

from .cog import Profiles
from .connectors import ProfileConnectors
from .presence import ProfilePresence

__all__ = ("Profiles", "ProfileConnectors", "ProfilePresence", "setup")


async def setup(bot):
    await bot.add_cog(Profiles(bot))
    await bot.add_cog(ProfileConnectors(bot))
    await bot.add_cog(ProfilePresence(bot))
