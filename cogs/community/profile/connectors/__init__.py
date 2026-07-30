"""Purpose: the connector framework of the social profile - one table, one
interface, one command group, and no connector yet.

Every external source the chantier promised (AniList, Steam, Last.fm, osu!,
Backloggd) is a P4 module that plugs in here. What this lot owns is everything
those modules must NOT each reinvent: where a linked account is stored, what a
link is allowed to write, how a failure is typed, when a section becomes
visible, and how the whole thing is exported and forgotten.

Layout:
* base.py    - the Connector contract, the typed errors, the bounded registry
               and the caps (pure: no bot, no database, no discord);
* example.py - the reference implementation, never registered in production;
* storage.py - the single-statement reads and writes on profile_connections;
* cog.py     - the `connections` command group.

This is a SUB-package, not an extension: core's discovery stops at the first
``__init__`` with a ``setup`` (``cogs/community/profile``), so the parent
package loads both cogs. There is no ``setup`` here on purpose - a second one
would either be ignored or double-load the cog depending on discovery order.

No credential lives in this package. AniList keeps its existing encrypted
``anilist_tokens`` row; every other v1 connector is a public handle. See
base.py for why a generic token table is deliberately NOT created in advance.

Data lifecycle: ``profile_connections`` is user-scoped (a user_id, no guild_id),
so the guild purge in tools/retention.py does not and must not touch it. It
joins the USER paths instead - ``collect_user_export`` and
``PROFILE_DELETE_QUERIES`` in tools/privacy.py.

Typography rule: ASCII '-' and '...' only.
"""

from .cog import ProfileConnectors

__all__ = ("ProfileConnectors",)
