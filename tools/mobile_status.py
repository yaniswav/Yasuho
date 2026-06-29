"""Make the bot appear with the mobile (phone) status icon.

Discord shows the mobile indicator when the gateway IDENTIFY payload reports a
mobile client. discord.py has no official option for this, so we patch the
gateway's send_as_json to rewrite the browser/device properties on the IDENTIFY
op only. Purely cosmetic - it changes nothing about how the bot behaves.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

_patched = False


def enable_mobile_status():
    """Patch the gateway so the bot connects as a mobile client (idempotent)."""
    global _patched
    if _patched:
        return
    _patched = True

    original = discord.gateway.DiscordWebSocket.send_as_json

    async def send_as_json(self, data):
        try:
            if isinstance(data, dict) and data.get("op") == self.IDENTIFY:
                props = data.get("d", {}).get("properties")
                if isinstance(props, dict):
                    props["browser"] = "Discord Android"
                    props["device"] = "Discord Android"
        except Exception:
            log.exception("Failed to set mobile identify properties")
        return await original(self, data)

    discord.gateway.DiscordWebSocket.send_as_json = send_as_json
    log.info("Mobile status indicator enabled.")
