"""Shared aiohttp configuration for cogs that call external HTTP APIs.

Several utility/fun cogs and the AniList package each open their own
``aiohttp.ClientSession``. They should all cap requests with the same timeout so
a slow or hung endpoint can never block an interaction for long; this module is
the single source of truth for that cap (previously a ``_HTTP_TIMEOUT`` constant
copy-pasted into every such cog).
"""

from __future__ import annotations

import aiohttp

# Cap outbound HTTP calls so a slow or hung endpoint can't block an interaction.
TIMEOUT = aiohttp.ClientTimeout(total=15)
