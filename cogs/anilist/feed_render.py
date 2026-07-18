"""AniList feed rendering: one activity as a card, a busy tick's remainder as a digest.

The spoiler-safe, security-grade post/embed construction. :class:`ActivityCard`
turns one normalised activity into a Components V2 card and :class:`ActivityDigest`
coalesces a busy tick's remainder into one compact card; both lean on the pure
helpers in ``tools.anilist_feed`` (``af``) for the spoiler-safe text conversion,
colour parsing and grouping. The card's action row embeds the persistent
DynamicItem buttons from ``feed_views``; the media-title helper is shared with the
add action and lives in ``feed_delivery``. Import direction is one-way (this
module consumes ``feed_views`` and ``feed_delivery``, never the reverse).
"""

from __future__ import annotations

import logging

import discord

from .feed_delivery import _media_title
from .feed_views import CARD_ACCENT, FeedAddButton, FeedLikeButton, FeedReplyButton
from tools import anilist_feed as af
from tools.i18n import N_, _, ngettext

log = logging.getLogger(__name__)


def _colour_from_media(media):
    """Cover accent colour ("#aabbcc") as an int, else the card blue.

    Parses the media's ``coverImage.color`` with the pure, defensive
    :func:`tools.anilist_feed.parse_hex_colour`; a missing media, missing colour
    or malformed value all fall back to :data:`CARD_ACCENT`.
    """

    colour = (media or {}).get("coverImage") or {}
    return af.parse_hex_colour(colour.get("color")) or CARD_ACCENT


# AniList ListActivity ``status`` is a lowercase verb phrase ("watched episode",
# "plans to watch", ...). Each maps to a localisable action template; the
# progress-bearing ones interpolate ``{progress}`` (normalised elsewhere), the
# rest ignore it. ``N_`` marks the msgids for extraction at import time; ``_()``
# resolves them at render time (the documented store-then-translate trick). An
# UNKNOWN status is never in this map and degrades to its raw text (see
# :func:`_list_action`), so a status AniList adds later renders verbatim instead
# of crashing.
_LIST_ACTION_TEMPLATES = {
    "watched episode": N_("watched episode {progress} of"),
    "rewatched episode": N_("rewatched episode {progress} of"),
    "read chapter": N_("read chapter {progress} of"),
    "reread chapter": N_("reread chapter {progress} of"),
    "completed": N_("completed"),
    "plans to watch": N_("plans to watch"),
    "plans to read": N_("plans to read"),
    "paused watching": N_("paused watching"),
    "paused reading": N_("paused reading"),
    "dropped": N_("dropped"),
}


def _list_action(status, progress):
    """Localised action phrase for a ListActivity, e.g. 'watched episode 5 of'.

    A known status maps to a template (with ``{progress}`` filled in where the
    template uses it; templates without it simply ignore the argument). An
    unknown status degrades to its raw text plus any progress so a newly-added
    AniList status still renders. Internal whitespace is collapsed so a missing
    progress never leaves a double space ('watched episode  of').
    """

    key = (status or "").strip().lower()
    template = _LIST_ACTION_TEMPLATES.get(key)
    if template is not None:
        phrase = _(template).format(progress=progress)
    else:
        phrase = " ".join(part for part in (status or "", progress) if part)
    return " ".join(phrase.split())


def _bold_link(text, url):
    """A bold masked link ``**[text](url)**``, or bold text when no url.

    Square brackets are stripped from ``text`` so a title like
    ``Re:Zero [Director's Cut]`` cannot break the ``[...]`` markup.
    """

    label = str(text or "").replace("[", "").replace("]", "")
    if url:
        return "**[{label}]({url})**".format(label=label, url=url)
    return "**{label}**".format(label=label)


def _card_subline(activity, media):
    """Small ``-#`` metadata line, or ``None`` when there is nothing to show.

    Assembles, in order and only when present: the media format, a relative
    timestamp, and non-zero like / reply counts. ``media`` is ``None`` for text
    activities (no format shown).
    """

    parts = []
    fmt = (media or {}).get("format")
    if fmt:
        parts.append(str(fmt).replace("_", " "))
    created = activity.get("created_at")
    if created:
        parts.append("<t:{ts}:R>".format(ts=int(created)))
    likes = activity.get("like_count") or 0
    if likes:
        parts.append(ngettext("{n} like", "{n} likes", likes).format(n=likes))
    replies = activity.get("reply_count") or 0
    if replies:
        parts.append(ngettext("{n} reply", "{n} replies", replies).format(n=replies))
    if not parts:
        return None
    return "-# " + " - ".join(parts)


def _user_summary(acts):
    """Terse per-type counts for a digest line, e.g. '3 anime updates, 1 post'."""

    counts = {"ANIME_LIST": 0, "MANGA_LIST": 0, "TEXT": 0}
    for act in acts:
        kind = act.get("type")
        if kind in counts:
            counts[kind] += 1
    parts = []
    if counts["ANIME_LIST"]:
        n = counts["ANIME_LIST"]
        parts.append(ngettext("{n} anime update", "{n} anime updates", n).format(n=n))
    if counts["MANGA_LIST"]:
        n = counts["MANGA_LIST"]
        parts.append(ngettext("{n} manga update", "{n} manga updates", n).format(n=n))
    if counts["TEXT"]:
        n = counts["TEXT"]
        parts.append(ngettext("{n} post", "{n} posts", n).format(n=n))
    if not parts:  # only unexpected types seen: fall back to a plain total
        n = len(acts)
        parts.append(ngettext("{n} update", "{n} updates", n).format(n=n))
    return ", ".join(parts)


class ActivityCard(discord.ui.LayoutView):
    """One AniList activity as a polished Components V2 card.

    A coloured :class:`~discord.ui.Container` (the media's cover accent, else
    :data:`CARD_ACCENT`) holds the update. A list activity renders a
    :class:`~discord.ui.Section` - the cover as a :class:`~discord.ui.Thumbnail`
    accessory (its ``description`` alt text is the media title, for screen
    readers) beside a bold headline (username link + action + title link) and a
    small subline - while a text activity drops the thumbnail and renders the
    post body (plus any image via a :class:`~discord.ui.MediaGallery`) straight
    in the container. A trailing :class:`~discord.ui.ActionRow` carries the
    'AniList' link button plus the persistent Like and Reply buttons (see
    :class:`FeedLikeButton` / :class:`FeedReplyButton`), and - on a list activity
    with a media id - an Add-to-planning button (:class:`FeedAddButton`). Every
    field degrades independently, so a partial activity dict (missing media,
    avatar, progress, ...) never raises.

    Persistence: the Like / Reply buttons are :class:`discord.ui.DynamicItem`
    instances, so ``timeout=None`` and the card is persistent. discord.py stores
    a sent view only when it ``is_dispatchable()`` (see ``abc.Messageable.send``);
    this one now is, but it is fully dynamic (its only stateful items are the two
    DynamicItems), so no per-message entry is retained and no timeout task is
    created. The buttons keep working forever - on old cards, across restarts -
    because dispatch matches their custom_id against the globally-registered
    templates and rebuilds each item from the live message, never from this view.
    """

    def __init__(self, activity, *, timeout=None):
        super().__init__(timeout=timeout)
        try:
            self._build(activity)
        except Exception:  # a card must never break delivery of the whole batch
            log.exception("AniList feed: failed to build an activity card")
            self._fallback(_("An AniList update could not be rendered."))

    def _fallback(self, message):
        self.clear_items()
        container = discord.ui.Container(accent_colour=CARD_ACCENT)
        container.add_item(discord.ui.TextDisplay(message))
        self.add_item(container)

    def _build(self, activity):
        if activity.get("kind") == "TextActivity":
            container = discord.ui.Container(accent_colour=CARD_ACCENT)
            self._build_text(container, activity)
        else:
            container = discord.ui.Container(
                accent_colour=_colour_from_media(activity.get("media"))
            )
            self._build_list(container, activity)
        self._add_action_row(container, activity)
        self.add_item(container)

    def _build_list(self, container, activity):
        media = activity.get("media") or {}
        user_link = _bold_link(
            activity.get("user_name") or _("Someone"), activity.get("user_url")
        )
        title_link = _bold_link(
            _media_title(media), media.get("siteUrl") or activity.get("site_url")
        )
        action = _list_action(
            activity.get("status"), af.normalize_progress(activity.get("progress"))
        )
        # Collapse whitespace so a degenerate activity with no status at all
        # (empty action) does not leave a double space between name and title.
        headline = " ".join(
            _("{user} {action} {title}")
            .format(user=user_link, action=action, title=title_link)
            .split()
        )

        texts = [discord.ui.TextDisplay(headline)]
        subline = _card_subline(activity, media)
        if subline:
            texts.append(discord.ui.TextDisplay(subline))

        cover = media.get("coverImage") or {}
        thumb = cover.get("extraLarge") or cover.get("large")
        if thumb:
            # A Section requires an accessory; only build one when we have a
            # cover to hang on it, otherwise degrade to plain text displays.
            container.add_item(
                discord.ui.Section(
                    *texts,
                    accessory=discord.ui.Thumbnail(
                        thumb, description=str(_media_title(media))[:256]
                    ),
                )
            )
        else:
            for text in texts:
                container.add_item(text)

    def _build_text(self, container, activity):
        user_link = _bold_link(
            activity.get("user_name") or _("Someone"),
            activity.get("user_url") or activity.get("site_url"),
        )
        container.add_item(
            discord.ui.TextDisplay(
                _("{user} posted an update").format(user=user_link)
            )
        )
        clean, image = af.convert_text(activity.get("text"))
        if clean:
            container.add_item(discord.ui.TextDisplay(clean))
        if image:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=image)
            container.add_item(gallery)
        subline = _card_subline(activity, None)
        if subline:
            container.add_item(discord.ui.TextDisplay(subline))

    def _add_action_row(self, container, activity):
        # One ActionRow (max five buttons) with, in order: the 'AniList' link
        # button (only when we have a url), the persistent Like + Reply buttons
        # keyed on the activity id, and - only on a list activity that carries a
        # media id - the persistent Add-to-planning button keyed on the MEDIA id
        # (a text/digest card has no media, so it never gets one; this keeps the
        # busiest row at 4 of 5 slots). The activity id is present on every
        # rendered activity (``_normalize`` drops id-less ones), but stay
        # defensive so a degenerate dict cannot raise mid-build.
        activity_id = activity.get("id")
        url = activity.get("site_url")

        row = discord.ui.ActionRow()
        if url:
            row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=_("AniList"), url=url
                )
            )
        if activity_id is not None:
            row.add_item(FeedLikeButton(activity_id))
            row.add_item(FeedReplyButton(activity_id))
        if activity.get("kind") == "ListActivity":
            media_id = (activity.get("media") or {}).get("id")
            if media_id is not None:
                row.add_item(FeedAddButton(media_id))

        if not row.children:  # nothing to show: no separator, no empty row
            return
        container.add_item(discord.ui.Separator())
        container.add_item(row)


class ActivityDigest(discord.ui.LayoutView):
    """The coalesced remainder of a busy tick as one compact Components V2 card.

    A single :data:`CARD_ACCENT` container: a heading ('...and N more updates')
    and one terse line per user (bold profile link + per-type counts), capped at
    :attr:`MAX_USERS` so a huge burst cannot blow the component budget - the
    overflow collapses into a small '+N others' trailer. No thumbnails: compact
    by design. Purely presentational (no interactive components), so it is never
    stored or dispatched.
    """

    MAX_USERS = 10

    def __init__(self, items, *, timeout=600):
        super().__init__(timeout=timeout)
        try:
            self._build(items)
        except Exception:
            log.exception("AniList feed: failed to build the digest card")
            self.clear_items()
            container = discord.ui.Container(accent_colour=CARD_ACCENT)
            container.add_item(
                discord.ui.TextDisplay(_("More AniList activity"))
            )
            self.add_item(container)

    def _build(self, items):
        total = len(items)
        users = list(af.group_by_user(items).values())

        container = discord.ui.Container(accent_colour=CARD_ACCENT)
        container.add_item(
            discord.ui.TextDisplay(
                "### "
                + ngettext(
                    "...and {count} more update",
                    "...and {count} more updates",
                    total,
                ).format(count=total)
            )
        )
        container.add_item(discord.ui.Separator())

        lines = []
        for acts in users[: self.MAX_USERS]:
            first = acts[0]
            user_link = _bold_link(
                first.get("user_name") or _("Someone"),
                first.get("user_url") or first.get("site_url"),
            )
            lines.append(
                _("{user} - {summary}").format(
                    user=user_link, summary=_user_summary(acts)
                )
            )
        extra = len(users) - self.MAX_USERS
        if extra > 0:
            lines.append(
                "-# "
                + ngettext("+{count} other", "+{count} others", extra).format(
                    count=extra
                )
            )
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)
