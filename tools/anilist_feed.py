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
MAX_SUBS_PER_FEED = 50  # at most 50 explicitly-subscribed titles per feed channel
MAX_FULL_POSTS_PER_TICK = 5  # rich cards per channel per tick; the rest coalesce


def sub_cap_exceeded(current_count, already_subscribed):
    """True when adding a NEW title subscription would exceed the per-feed cap.

    ``current_count`` is how many titles the feed already subscribes to;
    ``already_subscribed`` is whether the title being added is one of them. An
    already-subscribed title re-confirms harmlessly (it only refreshes the cached
    display title, adds no row), so it is never blocked - only a genuinely new
    title at or past :data:`MAX_SUBS_PER_FEED` is rejected. Pure and total.
    """

    return not already_subscribed and current_count >= MAX_SUBS_PER_FEED

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

# Each converted spoiler is wrapped in these private sentinel characters instead
# of a literal '||'. Downstream steps (image extraction, truncation) read spoiler
# boundaries from the sentinels, so a stray '||' the user typed as prose
# ("yes || no") can never masquerade as a spoiler bar and flip the parity. The
# sentinels are swapped for real Discord '||' bars only at the very end, once
# those boundary-sensitive steps have run. The sentinels keep the CONVERTER
# internally consistent; a separate step (see convert_text) escapes user-typed
# '|' so the FINAL string cannot mix user '||' with the emitted bars and shift
# Discord's positional '||' pairing.
_SPOILER_OPEN = "\x00"
_SPOILER_CLOSE = "\x01"

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

    The conversion runs a fixed pipeline: escape user-typed ``|`` (so a raw
    ``||`` cannot pair with an emitted spoiler bar), normalise HTML, strip
    centered blocks, convert spoilers, convert bold, extract/remove images,
    inline video embeds, then tidy whitespace and truncate. Ordinary
    Discord-compatible markup (``[text](url)`` links, bare urls) is left
    untouched.
    """
    if not raw:
        return "", None
    text = str(raw)

    # 0. Drop any stray sentinel characters so user text cannot forge a spoiler
    #    boundary the converter relies on below.
    text = text.replace(_SPOILER_OPEN, "").replace(_SPOILER_CLOSE, "")

    # 0b. Escape every user-typed literal '|'. Sentinels keep the CONVERTER's own
    #     spoiler bars internally consistent, but the final string still mixes
    #     those emitted '||' with any '||' the user typed as prose, and Discord
    #     pairs '||' markers positionally: an odd number of user '||' before an
    #     emitted spoiler shifts the pairing so the emitted closing bar goes
    #     unpaired and the hidden content renders in the clear. Escaping each pipe
    #     ('|' -> '\|') makes user pipes render as literal '|' that can never pair
    #     with an emitted bar. Applied to the whole raw text (spoiler content
    #     included, where an escaped pipe is still correct).
    text = text.replace("|", "\\|")

    # 1. HTML: <br> becomes a newline; drop any other tag AniList let through.
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)

    # 2. Centered blocks BEFORE spoilers (so ~~~...~~~ wins over a stray ~!...!~).
    text = _CENTER_RE.sub(r"\1", text)

    # 3. Spoilers -> sentinel-marked spans (converted to Discord '||' at the end).
    text = _SPOILER_RE.sub(_SPOILER_OPEN + r"\1" + _SPOILER_CLOSE, text)

    # 4. Bold: AniList __ -> Discord ** (Discord's __ is underline).
    text = _BOLD_RE.sub(r"**\1**", text)

    # 5. Images: remove the markup, collect embed-eligible urls (http(s) and not
    #    inside a spoiler). A pending open sentinel (more opens than closes before
    #    the match) means the image sits in a spoiler; those are dropped silently.
    image_urls = []

    def _strip_image(match):
        url = (match.group(1) or "").strip()
        before = match.string[: match.start()]
        inside_spoiler = before.count(_SPOILER_OPEN) > before.count(_SPOILER_CLOSE)
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

    # 8. Now that image extraction and truncation have placed every boundary,
    #    swap the sentinels for real Discord spoiler bars.
    text = text.replace(_SPOILER_OPEN, "||").replace(_SPOILER_CLOSE, "||")
    return text, (image_urls[0] if image_urls else None)


def _truncate(text, limit):
    """Cap ``text`` at ``limit`` chars with a spoiler-safe ``...`` suffix.

    Returns ``text`` unchanged when it already fits. Otherwise it is cut to
    ``limit`` characters and ``...`` is appended. The critical edge: if the cut
    lands inside an unclosed spoiler span, the partial content would be shown in
    the clear - so we close that span (append the close sentinel) BEFORE the
    ellipsis, keeping the exposed fragment hidden. Spoiler state is read from the
    converter's own sentinels, never from a raw ``||`` count, so a literal ``||``
    the user typed cannot flip the parity and unhide a real spoiler. A cut that
    splits a user's ``||`` leaves a lone trailing ``|``; that is dropped so it
    neither leaks nor renders.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if cut.endswith("|") and not cut.endswith("||"):
        cut = cut[:-1].rstrip()
    if cut.count(_SPOILER_OPEN) > cut.count(_SPOILER_CLOSE):
        cut += _SPOILER_CLOSE
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


# --- Airing tracker ---------------------------------------------------------
#
# The opt-in airing tracker (cogs/anilist/airing.py) DMs a user when a new
# episode of a title on their CURRENT anime list airs. Its only non-trivial
# decisions - who to notify, and how far to advance the poll cursor under page
# truncation - live here as pure functions so the cog stays a thin I/O shell and
# the logic is unit-tested with no bot, database or network.


def plan_airing_notifications(aired, lists_by_user):
    """Pick the ``(user_id, media_id, episode)`` notifications an aired batch yields.

    ``aired`` is the list of aired schedule rows actually processed this tick,
    each a dict carrying ``media_id`` (int) and ``episode`` (int). ``lists_by_user``
    maps a Discord user id to that user's cached CURRENT anime list as
    ``{media_id: progress}`` (``progress`` is how many episodes they have marked
    watched; callers store an int, 0 for none).

    A user is notified for an aired row when the row's media is on their cached
    list AND their progress is strictly below the aired episode - i.e. the
    episode is new to them. Progress is never regressed here (the Seen button
    re-checks it at click time too); this only picks who has an unseen airing.

    Returns a flat list of ``(user_id, media_id, episode)`` tuples in a stable
    order - aired-row order, then user id ascending - so delivery and the tests
    are deterministic. A row missing a media id or episode is skipped.
    """
    plan = []
    for row in aired:
        media_id = row.get("media_id")
        episode = row.get("episode")
        if media_id is None or episode is None:
            continue
        for user_id in sorted(lists_by_user):
            progress = lists_by_user[user_id].get(media_id)
            if progress is None:
                continue  # media not on this user's cached list
            if progress < episode:
                plan.append((user_id, media_id, episode))
    return plan


def advance_airing_cursor(cursor, fetched_airing_ats, capped=False):
    """New ``last_airing_at`` cursor after a poll, clamped under truncation.

    ``cursor`` is the current high-water mark (unix seconds); ``fetched_airing_ats``
    are the ``airingAt`` of every schedule row ACTUALLY fetched this tick (the
    poll queries ``airingAt_greater = cursor`` sorted by TIME ascending, so these
    are the oldest unseen airings first). ``capped`` is True when the fetch stopped
    on the page cap with an unfetched tail still pending.

    Normally the cursor advances to the maximum ``airingAt`` fetched and never
    regresses. Under truncation (``capped``) the unfetched tail has an ``airingAt``
    GREATER THAN OR EQUAL TO the last fetched row: a same-second sibling of that
    last row can sit in the tail, and advancing to exactly the max would let the
    strict ``airingAt_greater`` filter skip it next tick. So a capped advance stops
    one second BELOW the max fetched and that whole final second is re-selected next
    tick (the already-seen rows in it are simply re-sent; a page cap needs 250+
    in-window airings, so this is a theoretical edge, never a normal path). An empty
    fetch leaves the cursor untouched, so a row that lands in the window slightly
    late is never skipped by an over-eager jump to ``now``.
    """
    if not fetched_airing_ats:
        return cursor
    top = max(fetched_airing_ats)
    if capped:
        top -= 1
    return max(cursor, top)
