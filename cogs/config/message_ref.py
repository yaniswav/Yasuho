"""Resolve a Discord message reference (jump link or bare id) to its ids.

Shared by the button-role and reaction-role builders, which both let an admin
point at a message by pasting its jump link or typing its id. This was a
byte-identical copy in each cog before it moved here.
"""

from __future__ import annotations

import re

# https://discord.com/channels/<guild>/<channel>/<message>
LINK_RE = re.compile(
    r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def parse_message_ref(text, default_channel_id):
    """Resolve a message id or jump link to (guild_id, channel_id, message_id).

    A jump link yields all three. A bare numeric id yields (None, the supplied
    default channel id, the message id). Returns None when nothing parses.
    """
    if not text:
        return None
    text = text.strip()
    match = LINK_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    if text.isdigit():
        return None, default_channel_id, int(text)
    return None
