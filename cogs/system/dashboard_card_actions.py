"""Dashboard -> bot action queue: the server rank/welcome card background.

Companion module to ``cogs/system/dashboard_actions.py`` (which owns the queue,
the claim and the result write-back), merged into its single kind table like
``dashboard_announcement_actions``. The image goes through the Leveling cog's
own ``set_rank_background`` seam, so the dashboard gets the exact pipeline of
the Discord panel: byte cap, pixel ceiling, WebP ladder, cache invalidation.

Payloads:

* ``card_background_set``: ``{"image": <base64 of the raw file>,
  "content_type"?: "image/png|image/jpeg|image/webp"}``. The raw file is capped
  at 8 MiB (``rank_card.MAX_SOURCE_BYTES``); the base64 text is length-checked
  BEFORE it is decoded.
* ``card_background_clear``: ``{}``.

The image never outlives the action: ``dashboard_actions`` blanks
``payload.image`` in every final status write.

Success: ``{"ok": true}``. Error codes: ``bad_payload`` (missing, not a string
or not valid base64), ``source_too_large``, ``image_too_large``,
``unsupported_format``, ``decode_failed``, ``encoded_too_large``,
``engine_unavailable`` (Leveling cog not loaded), ``busy`` (2 uploads already
being processed bot-wide; retry shortly).

Typography rule: ASCII '-' and '...' only.
"""

import base64
import binascii
import math

from cogs.community.leveling import rank_card
from cogs.community.leveling.rank_card_user import _InflightUploads

# Base64 turns 3 bytes into 4 characters, padding included.
_MAX_B64_CHARS = math.ceil(rank_card.MAX_SOURCE_BYTES / 3) * 4

# Bot-wide, module-level: each upload holds up to 8 MiB plus its decode.
_MAX_INFLIGHT_UPLOADS = 2
_INFLIGHT = _InflightUploads(_MAX_INFLIGHT_UPLOADS)

_ERROR_CODES = (
    (rank_card.SourceTooLarge, "source_too_large"),
    (rank_card.ImageTooLarge, "image_too_large"),
    (rank_card.UnsupportedFormat, "unsupported_format"),
    (rank_card.DecodeFailed, "decode_failed"),
    (rank_card.EncodedTooLarge, "encoded_too_large"),
)


async def _exec_set(bot, guild_id, payload):
    image = payload.get("image")
    if not isinstance(image, str) or not image:
        return {"ok": False, "error": "bad_payload"}
    if len(image) > _MAX_B64_CHARS:
        return {"ok": False, "error": "source_too_large"}
    engine = bot.get_cog("Leveling")
    if engine is None:
        return {"ok": False, "error": "engine_unavailable"}
    if not _INFLIGHT.acquire():
        return {"ok": False, "error": "busy"}
    try:
        try:
            data = base64.b64decode(image, validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "error": "bad_payload"}
        content_type = payload.get("content_type")
        if not isinstance(content_type, str):
            content_type = None
        try:
            await engine.set_rank_background(guild_id, data, content_type)
        except rank_card.RankCardError as exc:
            for cls, code in _ERROR_CODES:
                if isinstance(exc, cls):
                    return {"ok": False, "error": code}
            raise
        return {"ok": True}
    finally:
        _INFLIGHT.release()


async def _exec_clear(bot, guild_id, payload):
    engine = bot.get_cog("Leveling")
    if engine is None:
        return {"ok": False, "error": "engine_unavailable"}
    await engine.clear_rank_card(guild_id, target="background")
    return {"ok": True}


EXECUTORS = {
    "card_background_set": _exec_set,
    "card_background_clear": _exec_clear,
}
