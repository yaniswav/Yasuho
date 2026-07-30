"""Purpose: the reference implementation of :class:`~.base.Connector` - the one
connector P3 ships, and the double every test in this package drives.

It exists for two reasons. First, an interface nobody implements is a guess: the
example proves the contract is implementable end to end (validate offline, hand
back a normalised handle, produce a display payload) before P4 writes five of
them against real APIs. Second, the cog, the storage seam and the privacy path
all need SOMETHING to link in order to be tested at all, and testing them
against a real third party would put the network in the offline suite.

IT IS NEVER REGISTERED AT IMPORT TIME. Production loads this package with an
EMPTY registry, so every connector answers 'coming soon' until P4 lands the real
modules; a test pins that (a fake AniList silently serving users would be worse
than no AniList). Callers that want it - the tests - construct it under one of
the reserved section names and register it themselves, then unregister.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import re

from .base import (
    LINKABLE,
    Connector,
    InvalidHandle,
    LinkResult,
    UnknownConnector,
)

# Deliberately strict and deliberately offline: letters, digits and a few
# separators, 2 to 32 characters. Every v1 handle fits (an osu! or Last.fm
# username, an AniList name, a SteamID64), and a shape check that needs no
# network is exactly what the contract asks a connector to do first.
HANDLE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.\-]{1,31}\Z")


class ExampleConnector(Connector):
    """A handle-only connector: validates a username, caches it, fetches nothing.

    Constructed with the section it stands in for, because the registry only
    accepts reserved names and a test wants to exercise a REAL routing path
    rather than a special case carved out for fakes.
    """

    # NOT wrapped in N_: this connector never reaches a real user, so its
    # strings must not reach a real translator either.
    label = "Example connection"
    handle_hint = "your username, 2 to 32 letters, digits, . _ or -"

    def __init__(self, name):
        if name not in LINKABLE:
            raise UnknownConnector(name)
        self.name = name

    async def link(self, user_id, raw_input):
        """Accept or refuse the handle from its shape alone - no round trip."""
        handle = (raw_input or "").strip()
        if not HANDLE_PATTERN.match(handle):
            raise InvalidHandle(self.name, "format")
        # The stored id is canonical (lowercased) while the display name keeps
        # the capitalisation the user typed: the same split every real
        # connector makes between "what we query with" and "what we show".
        return LinkResult(
            external_id=handle.lower(),
            display_name=handle,
            payload={"handle": handle},
        )

    async def refresh(self, user_id, connection):
        """Return the display cache. A real connector would fetch here."""
        return {
            "handle": connection["external_id"],
            "display_name": connection.get("display_name"),
        }
