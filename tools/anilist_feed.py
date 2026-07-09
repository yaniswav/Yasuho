"""Pure, testable core for the AniList activity feed.

A guild may configure up to two feed channels; each feed follows a set of
AniList users and the bot posts their new activities (list progress and text
posts) into the channel. A single global poller (a later lot) fetches the raw
activities in batches and hands them to the helpers here.

This module is deliberately free of any discord.py, database or network use:
it only shapes and filters data (AniList markdown -> Discord markdown, routing
activities to channels, splitting a burst into full cards + a digest, and
normalising list progress) so the cog can lean on well-tested logic and the
tests can run without a bot. Every Discord/DB side effect lives in the cog.

It is also translation-free on purpose (no ``_()`` imports): it returns raw
data (statuses, numbers, cleaned text) and the cog does all user-facing wording
and localisation in a later lot.
"""

from __future__ import annotations

import re

# --- Feed policy ------------------------------------------------------------
MAX_FEEDS_PER_GUILD = 2  # at most 2 feed channels per guild
MAX_FOLLOWS_PER_FEED = 25  # at most 25 followed AniList users per feed
MAX_FULL_POSTS_PER_TICK = 5  # rich cards per channel per tick; the rest coalesce

# Activity types a feed may post. AniList's private ``MESSAGE`` type (profile
# wall posts / direct messages) is deliberately excluded: those are not public
# activity and must never be mirrored.
ALLOWED_TYPES = ("ANIME_LIST", "MANGA_LIST", "TEXT")
DEFAULT_TYPES = ALLOWED_TYPES  # a fresh feed follows every allowed type

# Embed body cap for a text activity. Well under Discord's 4096 description
# limit, leaving room for the spoiler-safe truncation suffix.
TEXT_LIMIT = 2048


# --- AniList markdown -> Discord markdown -----------------------------------
# AniList text activities use an AniList-flavoured markdown that differs from
# Discord's. The patterns below are compiled once and applied in a fixed order
# so they cannot mis-interact (see convert_text). Order matters most for the
# centered/spoiler pair: the centered form (~~~text~~~) is stripped BEFORE the
# spoiler form (~!text!~) so a crafted ``~~~!...!~~~`` is read as centered, not
# as a stray spoiler.

# <br> -> newline; every other HTML tag is stripped conservatively.
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# ~~~text~~~ centered block: strip the markers, keep the inner text.
_CENTER_RE = re.compile(r"~~~(.+?)~~~", re.DOTALL)

# ~!text!~ spoiler -> ||text|| (non-greedy so adjacent spoilers stay separate,
# DOTALL so a spoiler may span multiple lines). THE critical conversion:
# leaking a spoiler is the worst failure of this feature.
_SPOILER_RE = re.compile(r"~!(.+?)!~", re.DOTALL)

# __text__ is BOLD on AniList, but Discord renders __ as underline -> **text**.
_BOLD_RE = re.compile(r"__(.+?)__", re.DOTALL)

# img(url) / img220(url) / Img420(url) / img40%(url): case-insensitive, optional
# width digits and an optional trailing '%'. Removed from the text; http(s) urls
# are collected so the cog can set the first as the embed image.
_IMAGE_RE = re.compile(r"\bimg\d*%?\(([^)]*)\)", re.IGNORECASE)

# youtube(url) / webm(url) video embeds -> the bare url (clickable in Discord).
_VIDEO_RE = re.compile(r"\b(?:youtube|webm)\(([^)]*)\)", re.IGNORECASE)

# Whitespace tidy-up after the substitutions above.
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def convert_text(raw, limit=TEXT_LIMIT):
    """Convert AniList-flavoured markdown to Discord markdown.

    Returns ``(clean_text, first_image_url_or_None)``:

    * ``clean_text`` is Discord-ready markdown, spoiler-safe and truncated to
      ``limit`` characters (a trailing ``...`` is appended when it is cut).
    * ``first_image_url_or_None`` is the first http(s) ``img(...)`` url found
      OUTSIDE any spoiler, for the cog to use as the embed image; ``None`` when
      there is none. Images hidden inside a spoiler are removed from the text
      but never surfaced, so an author's hidden image cannot leak.

    The conversion runs a fixed pipeline: normalise HTML, strip centered blocks,
    convert spoilers, convert bold, extract/remove images, inline video embeds,
    then tidy whitespace and truncate. Ordinary Discord-compatible markup
    (``[text](url)`` links, bare urls) is left untouched.
    """
    if not raw:
        return "", None
    text = str(raw)

    # 1. HTML: <br> becomes a newline; drop any other tag AniList let through.
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)

    # 2. Centered blocks BEFORE spoilers (so ~~~...~~~ wins over a stray ~!...!~).
    text = _CENTER_RE.sub(r"\1", text)

    # 3. Spoilers -> Discord spoiler bars.
    text = _SPOILER_RE.sub(r"||\1||", text)

    # 4. Bold: AniList __ -> Discord ** (Discord's __ is underline).
    text = _BOLD_RE.sub(r"**\1**", text)

    # 5. Images: remove the markup, collect embed-eligible urls (http(s) and not
    #    inside a spoiler). Parity of '||' before the match tells us whether the
    #    image sits inside an open spoiler; those are dropped silently.
    image_urls = []

    def _strip_image(match):
        url = (match.group(1) or "").strip()
        inside_spoiler = match.string[: match.start()].count("||") % 2 == 1
        if not inside_spoiler and url.lower().startswith(("http://", "https://")):
            image_urls.append(url)
        return ""

    text = _IMAGE_RE.sub(_strip_image, text)

    # 6. Video embeds -> the bare (clickable) url.
    text = _VIDEO_RE.sub(lambda m: (m.group(1) or "").strip(), text)

    # 7. Tidy the whitespace the removals may have left behind.
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    text = text.strip()

    text = _truncate(text, limit)
    return text, (image_urls[0] if image_urls else None)


def _truncate(text, limit):
    """Cap ``text`` at ``limit`` chars with a spoiler-safe ``...`` suffix.

    Returns ``text`` unchanged when it already fits. Otherwise it is cut to
    ``limit`` characters and ``...`` is appended. The critical edge: if the cut
    lands inside an unclosed ``||`` spoiler, the partial content would be shown
    in the clear - so we close the spoiler bar (append ``||``) BEFORE the
    ellipsis, keeping the exposed fragment hidden. A cut that splits a ``||``
    token leaves a lone trailing ``|``; that is dropped so it neither leaks nor
    renders.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if cut.endswith("|") and not cut.endswith("||"):
        cut = cut[:-1].rstrip()
    if cut.count("||") % 2 == 1:
        cut += "||"
    return cut + "..."


# --- Routing ----------------------------------------------------------------


def route_activities(activities, feeds):
    """Fan a batch of activities out to the channels that want them.

    ``activities`` are dicts (``id``, ``type``, ``user_id``, ``is_adult`` bool,
    ...). ``feeds`` are dicts (``channel_id``, ``types`` set/list,
    ``followed_ids`` set, ``allow_adult`` bool - the cog passes
    ``channel.is_nsfw()``). An activity reaches a feed when the feed follows its
    user AND the activity's type is in the feed's types; adult activities are
    dropped unless the feed allows them.

    Returns ``{channel_id: [activities]}`` with each channel's list sorted by
    ``id`` ascending, and channels that matched nothing omitted.
    """
    routed = {}
    for feed in feeds:
        channel_id = feed.get("channel_id")
        if channel_id is None:
            continue
        followed = set(feed.get("followed_ids") or ())
        types = set(feed.get("types") or ())
        allow_adult = bool(feed.get("allow_adult"))
        bucket = routed.setdefault(channel_id, [])
        for activity in activities:
            if activity.get("user_id") not in followed:
                continue
            if activity.get("type") not in types:
                continue
            if activity.get("is_adult") and not allow_adult:
                continue
            bucket.append(activity)
    return {
        channel_id: sorted(bucket, key=lambda a: a.get("id", 0))
        for channel_id, bucket in routed.items()
        if bucket
    }


# --- Burst coalescing -------------------------------------------------------


def plan_posts(activities, max_full=MAX_FULL_POSTS_PER_TICK):
    """Split one channel's activities into ``(full, digest)``.

    The first ``max_full`` activities each get a rich card (``full``); anything
    beyond that is the ``digest`` remainder, to be summarised in one compact
    message so a busy tick cannot spam a channel with dozens of embeds.
    """
    full = list(activities[:max_full])
    digest = list(activities[max_full:])
    return full, digest


def group_by_user(activities):
    """Group activities by ``user_id`` for the digest, preserving order.

    Returns ``{user_id: [activities]}`` with users in first-seen order and each
    user's activities in their original order, so the cog can render one line
    per user without re-sorting.
    """
    grouped = {}
    for activity in activities:
        grouped.setdefault(activity.get("user_id"), []).append(activity)
    return grouped


# --- Colour parsing ---------------------------------------------------------

# A CSS-style 6-digit hex colour, with or without the leading '#'. AniList's
# ``coverImage.color`` is a free-form string ("#e4a15d") that may be absent,
# empty or malformed, so parsing stays strict and defensive: anything that is
# not exactly six hex digits yields ``None`` and the cog falls back to a brand
# colour rather than raising.
_HEX_COLOUR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex_colour(text):
    """Parse a CSS hex colour string into a ``0xRRGGBB`` int, else ``None``.

    Accepts ``"#e4a15d"`` or ``"e4a15d"`` (case-insensitive, surrounding
    whitespace ignored). Returns ``None`` for anything that is not a 6-digit hex
    colour - a missing/``None`` value, an empty string, a 3-digit shorthand, a
    wrong length or a non-hex character - so the caller can substitute a default
    accent. Pure and total: it never raises on junk input.
    """
    if not isinstance(text, str):
        return None
    match = _HEX_COLOUR_RE.match(text.strip())
    if match is None:
        return None
    return int(match.group(1), 16)


# --- Progress normalisation -------------------------------------------------

_RANGE_DASH_RE = re.compile(r"\s*-\s*")


def normalize_progress(progress):
    """Normalise an AniList ListActivity progress string.

    A single value (``"3"``) is returned as-is; a range comes spaced
    (``"3 - 5"``) and is collapsed to ``"3-5"``. This is data only - the cog
    pairs it with the raw status ('watched episode', 'plans to watch', ...) and
    handles all i18n/wording. Junk passes through with only its dash spacing
    normalised.
    """
    if not progress:
        return ""
    return _RANGE_DASH_RE.sub("-", str(progress).strip())
