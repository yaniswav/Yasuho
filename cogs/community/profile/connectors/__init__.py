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
* sessions.py- the shared aiohttp session registry, so the sessions the
               connectors open lazily are also sessions ``cog_unload`` closes;
* storage.py - the single-statement reads and writes on profile_connections;
* cog.py     - the `connections` command group;
* anilist.py, steam.py, osu.py, lastfm.py, backloggd.py - the P4 modules
               themselves, each a single file that self-registers (see
               :func:`_discover`).

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

--------------------------------------------------------------------------
Auto-discovery
--------------------------------------------------------------------------

A P4 module (anilist.py, steam.py, ...) is expected to self-register by a
plain effect of being imported: a call to ``base.register(SomeConnector())``
sitting at its own module level, exactly like ``example.py`` would if it were
ever wired in, plus (for a section with a card renderer)
``views.register_section_renderer(...)``. :func:`_discover` is what makes
that automatic - it walks this package with ``pkgutil.iter_modules``, imports
every module that is not private and not one of the framework's own files
({base, storage, cog, example}), and lets each one register itself as a side
effect. A module that fails to import (a typo, a missing dependency) is
logged and skipped; the others still load, because one broken connector must
not take the other six down with it. Nothing here restates WHICH names are
valid - that whitelist lives once, in ``base.LINKABLE``, and an unknown or
duplicate name is refused by ``base.register`` itself.

The ``Connector`` interface (``link(user_id, raw_input)``, ``refresh(user_id,
connection)``) carries no ``bot``: a connector instance is constructed by a
bare module import, before the bot object necessarily exists anywhere in this
process, so it cannot receive one at construction time. Every real connector
therefore takes its ``aiohttp.ClientSession`` from ``sessions.py`` - one
lazily-created session per connector, owned by this package rather than by
the bot-wide one in ``tools/http.py`` (which needs a ``bot`` to look up) -
see lastfm.py's module docstring for the full reasoning, which every other
network-calling connector in this package follows. Those sessions are closed
by ``Profiles.cog_unload``, which is also what cancels the lazy refreshes
that use them. A connector that ALSO wants opportunistic access to the running bot
(the AniList connector, to share that cog's interactive throttle) exposes its
own optional ``bind_bot(bot)`` method; ``Profiles.__init__`` calls it, best
effort, on whichever connectors define it - see that cog's docstring.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from .cog import ProfileConnectors

log = logging.getLogger(__name__)

__all__ = ("ProfileConnectors",)

# Modules that are part of the framework itself, not a P4 connector, and must
# never be auto-imported a second time here (each is already imported by
# something else, or is deliberately never registered - see example.py).
_FRAMEWORK_MODULES = frozenset({"base", "storage", "cog", "example", "sessions"})


def _discover():
    """Import every P4 connector module in this package.

    Each import is independent and guarded: a module that raises on import is
    logged with the traceback and skipped, so a single broken connector
    (a syntax slip, a bad top-level call) cannot prevent AniList, Steam and
    osu! from registering. There is nothing to return - a module's only
    observable effect is the side effect of ``base.register`` (and, for a
    section with a card renderer, ``views.register_section_renderer``).
    """
    package = __name__
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_") or name in _FRAMEWORK_MODULES:
            continue
        try:
            importlib.import_module(f".{name}", package)
        except Exception:
            log.exception("Failed to import profile connector module %r", name)


_discover()
