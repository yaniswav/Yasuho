"""Shared aiohttp configuration for cogs that call external HTTP APIs.

The bot owns one ``aiohttp.ClientSession`` shared by utility, fun and AniList
cogs. Every request also uses the timeout defined here so a slow or hung
endpoint cannot block an interaction indefinitely.
"""

from __future__ import annotations

import aiohttp

# Cap outbound HTTP calls so a slow or hung endpoint can't block an interaction.
TIMEOUT = aiohttp.ClientTimeout(total=15)


def get_session(bot):
    """Return the bot-owned session or fail clearly during invalid lifecycle use."""
    session = getattr(bot, "http_session", None)
    if session is None or session.closed:
        raise RuntimeError("shared HTTP session is not available")
    return session
