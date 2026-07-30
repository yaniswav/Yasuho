"""Purpose: the one aiohttp session registry the connectors share, so a
session created by a lazy first request is also a session something can CLOSE.

Every network connector used to hold its own module-level ``ClientSession`` in
a private global and nothing ever closed it: reloading the profile extension
left the old session - its connector pool, its keep-alive sockets, its
resolver - behind for the whole life of the process, and a shutdown ended with
aiohttp's "Unclosed client session" on the way out.

The bot-wide session in ``tools/http.py`` is not the answer here: it needs a
``bot`` to look up, and a :class:`~.base.Connector` is constructed by a bare
module import, before a bot object necessarily exists anywhere in this process
(see the package docstring). What the connectors were missing was not a
session, it was an OWNER. So: one dict, one session per connector name,
created exactly as lazily as before, plus a :func:`close_all` that
``Profiles.cog_unload`` calls.

Re-entrant by construction - a session that was closed is simply recreated by
the next request - which is what makes closing safe even while a link is in
flight: the in-flight call fails its own typed "remote" failure at worst, and
the next one opens a fresh session.

The map is bounded by the number of REGISTERED connectors (at most seven, see
base.LINKABLE): the keys are connector names, never anything a user typed.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import aiohttp

from tools.http import TIMEOUT

log = logging.getLogger(__name__)

# connector name -> its session. See the module docstring for why this cannot
# grow with anything user-driven.
_SESSIONS: dict[str, aiohttp.ClientSession] = {}


async def get_session(name):
    """The session for one connector, created on first use.

    Async because every caller already awaits it (and because aiohttp wants a
    running loop to bind its connector to) - there is nothing to await here.
    """
    session = _SESSIONS.get(name)
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=TIMEOUT)
        _SESSIONS[name] = session
    return session


async def close_all():
    """Close every session this package opened; never raises.

    Cleared FIRST, so a connector whose request lands mid-teardown opens a
    fresh session rather than reusing one that is being closed underneath it.
    A failure to close one session must not leave the others open, which is
    why each is guarded on its own.
    """
    sessions = list(_SESSIONS.items())
    _SESSIONS.clear()
    for name, session in sessions:
        if session.closed:
            continue
        try:
            await session.close()
        except Exception:
            log.exception("Failed to close the %s connector HTTP session", name)
