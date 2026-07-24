"""AniList feed card actions: like / reply / add-to-planning as the clicking user.

The persistent feed-card buttons act AS the clicking user through the AniList
account they linked with ``/anilist login``. This module owns that flow end to
end: the authenticated GraphQL pipeline (:func:`_authed_graphql`), the typed
AniList errors it raises (shared with the poller in ``feed.py``), the per-user
debounce and token resolution, and the three action backends
(:func:`_run_like` / :func:`_run_add` / :func:`_run_reply`) plus the reply modal
and the post-add configure view. The DynamicItem button widgets that trigger
these live in ``feed_views`` and call in here; the activity card that embeds them
lives in ``feed_render``. Import direction stays one-way (views/render -> here).
"""

from __future__ import annotations

import logging

import discord

from .components import EditEntryModal
from .helpers import API_URL
from .queries import SAVE_ENTRY_QUERY
from tools import i18n, interactions
from tools.cooldowns import Cooldowns
from tools.http import TIMEOUT, get_session
from tools.i18n import N_, _, ngettext
from tools.views import LocaleModal

log = logging.getLogger(__name__)


class _RateLimited(Exception):
    """Raised on a 429 so the tick can set an embargo and bail cleanly."""

    def __init__(self, retry_after):
        super().__init__("AniList rate limited (retry after %ss)" % retry_after)
        self.retry_after = retry_after


class _FetchError(Exception):
    """Any non-429 network / HTTP / GraphQL failure while fetching."""


class _AuthError(Exception):
    """A 401 on an authenticated call: the user's AniList link is invalid now."""


class _GoneError(Exception):
    """A 400/404 (or data-less GraphQL error): the target activity is gone."""


def _parse_retry_after(value, default=60):
    """Parse a Retry-After header (AniList sends integer seconds)."""

    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


def _media_title(media):
    """Best display title for a media dict (userPreferred first)."""

    title = (media or {}).get("title") or {}
    return (
        title.get("userPreferred")
        or title.get("romaji")
        or title.get("english")
        or _("Unknown title")
    )


# --- Interactive Like / Reply -----------------------------------------------
#
# The feed card carries two persistent buttons that act AS the clicking user,
# through the AniList account they linked with ``/anilist login``. They are
# :class:`discord.ui.DynamicItem` buttons so they keep working forever - even on
# cards posted before a restart - because dispatch matches the custom_id against
# a globally-registered template and rebuilds the item from the live message,
# never from a stored (and long-gone) view. The activity id is the only state
# and it rides inside the custom_id.

# ``ToggleLikeV2`` returns a LikeableUnion; the two inline fragments read the
# result for the only two activity kinds our feed ever renders (a MessageActivity
# never appears here). ``LikeableType.ACTIVITY`` targets an activity by id.
TOGGLE_LIKE_MUTATION = """
mutation ($id: Int, $type: LikeableType) {
  ToggleLikeV2(id: $id, type: $type) {
    __typename
    ... on ListActivity { isLiked likeCount }
    ... on TextActivity { isLiked likeCount }
  }
}
"""

# ``SaveActivityReply`` posts a reply on the activity as the authenticated user.
SAVE_REPLY_MUTATION = """
mutation ($activityId: Int, $text: String) {
  SaveActivityReply(activityId: $activityId, text: $text) {
    id
  }
}
"""

# The clicking viewer's own status for a media, plus its title for the reply
# copy. ``mediaListEntry`` resolves against the AUTHENTICATED viewer only when
# the request carries that user's OAuth token - the same per-viewer resolution
# the media editor and update wizard already rely on (see ``MEDIA_ENTRY_QUERY``
# and ``SEARCH_ENTRY_QUERY`` in ``queries.py``, and ``AniListBase._viewer_entry``).
# ``userPreferred`` honours the viewer's title-language setting; romaji is the
# fallback. The follow-up add reuses ``SAVE_ENTRY_QUERY`` from ``queries.py``.
ADD_LOOKUP_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    type
    title { userPreferred romaji english }
    episodes
    chapters
    mediaListEntry { status }
  }
}
"""


# Human-readable words for the viewer's current list status on a media, mirroring
# the wording used by the media editor's status picker (see ``components.py``).
# ``N_`` marks the msgids for extraction; ``_()`` resolves them at click time. An
# unknown status degrades to its raw enum value.
_ADD_STATUS_WORDS = {
    "CURRENT": N_("Watching"),
    "PLANNING": N_("Planning"),
    "COMPLETED": N_("Completed"),
    "DROPPED": N_("Dropped"),
    "PAUSED": N_("Paused"),
    "REPEATING": N_("Repeating"),
}


def _status_word(status):
    """Localised word for an existing list status, or the raw enum if unknown."""

    template = _ADD_STATUS_WORDS.get((status or "").upper())
    return _(template) if template is not None else (status or "")

# The longest reply AniList's box accepts comfortably; keeps us inside Discord's
# modal input limit too.
REPLY_MAX_LENGTH = 1500

# One shared per-user debounce for both action buttons (not a durable rate
# limit, just an in-memory anti-hammer). 3s between clicks per user.
_ACTION_DEBOUNCE = Cooldowns(3.0)


def _activity_url(activity_id):
    """The canonical AniList permalink for an activity id.

    Deterministic from the id alone, so the reply confirmation can link back to
    the activity even on a card rebuilt after a restart (where the card object
    no longer carries the original ``siteUrl``).
    """

    return "https://anilist.co/activity/{aid}".format(aid=activity_id)


def _throttle_for(client):
    """Return the ONE shared interactive throttle, or None when unavailable.

    The single :class:`~cogs.anilist.throttle.AniListThrottle` instance lives on
    the AniList cog (``AniListBase.__init__``). The feed surface reads it through
    that same cog so the buttons, the lookup commands and the admin searches all
    consume ONE process-wide window - two instances would mean two ceilings, which
    would defeat the point. Degrades to None (never blocks) if the cog is somehow
    unavailable, mirroring ``components._deny_if_throttled``.
    """

    get_cog = getattr(client, "get_cog", None)
    if get_cog is None:
        return None
    return getattr(get_cog("AniList"), "_throttle", None)


async def _authed_graphql(bot, token, query, variables):
    """POST an authenticated GraphQL request to AniList as the linked user.

    The bearer token is placed ONLY in the Authorization header - never logged,
    never echoed, never woven into a raised exception (the raised errors carry
    fixed, tokenless messages). Maps AniList's responses to the typed feed
    errors so the click handlers can render a clean, localised hint:

      * 429            -> :class:`_RateLimited` (with Retry-After seconds);
      * 401            -> :class:`_AuthError` (link revoked/invalid);
      * 400 / 404      -> :class:`_GoneError` (activity deleted);
      * a data-less GraphQL error -> :class:`_GoneError` (most often deleted);
      * anything else  -> :class:`_FetchError` (generic failure).
    """

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
    }
    payload = {"query": query, "variables": variables}

    try:
        async with get_session(bot).post(
            API_URL, json=payload, headers=headers, timeout=TIMEOUT
        ) as r:
            status = r.status
            if status == 429:
                # Record and log on the SAME shared counter / WARNING level the
                # lookup path (AniListBase._graphql) uses, so the operator can
                # correlate interactive throttling with poller embargoes.
                throttle = _throttle_for(bot)
                if throttle is not None:
                    throttle.note_throttled()
                log.warning(
                    "AniList returned HTTP 429 on an interactive feed action; the "
                    "shared per-IP budget is under pressure and the airing / feed "
                    "/ chapter pollers may be embargoed"
                )
                raise _RateLimited(
                    _parse_retry_after(r.headers.get("Retry-After"))
                )
            if status == 401:
                raise _AuthError()
            try:
                data = await r.json()
            except Exception:
                data = None
            if status in (400, 404):
                raise _GoneError()
            if data is None:
                raise _FetchError("AniList HTTP %s with no JSON body" % status)
    except (_RateLimited, _AuthError, _GoneError, _FetchError):
        raise
    except Exception as exc:
        # aiohttp errors reference the URL/reason, never request headers, so the
        # token cannot leak here; still, keep the message generic and tokenless.
        raise _FetchError("network failure talking to AniList") from exc

    # A logical GraphQL error with no data payload is, in practice, an activity
    # that was deleted between the card being posted and the click.
    if isinstance(data, dict) and data.get("errors") and not data.get("data"):
        raise _GoneError()
    return data


async def _feed_ephemeral(interaction, message, *, view=None):
    """Deliver an ephemeral reply to a feed-action interaction, first or follow-up.

    Returns the sent message when the reply went out as a follow-up (so a caller
    can bind ``view.message`` for a clean timeout), else ``None``. ``view`` is
    only attached when provided - Discord rejects an explicit ``view=None``.
    """

    kwargs = {"ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(message, **kwargs)
        await interaction.response.send_message(message, **kwargs)
    except discord.HTTPException:
        log.debug("AniList feed: could not deliver an ephemeral action reply")
    return None


async def _check_debounce(interaction):
    """Gate a click behind the per-user debounce.

    Returns ``True`` when the click may proceed; otherwise sends an ephemeral
    'slow down' and returns ``False``. Touches the window only on an allowed
    click so a burst of denied clicks does not extend it indefinitely.
    """

    if _ACTION_DEBOUNCE.is_active(interaction.user.id):
        await _feed_ephemeral(
            interaction, _("You are clicking too fast - give it a moment.")
        )
        return False
    _ACTION_DEBOUNCE.touch(interaction.user.id)
    return True


async def _deny_feed_action(interaction):
    """Gate a feed-card action behind the shared interactive throttle.

    The feed buttons act AS the clicking user through a per-user OAuth token, so
    they never route through ``AniListBase._graphql`` (which carries the ceiling
    for the lookup commands). This applies the SAME shared throttle here: the
    per-user/guild window first, then the process-wide backstop - the exact
    check-then-hit order of ``AniListBase._graphql`` - so a click storm cannot
    burn the shared per-IP budget the airing / feed / chapter pollers depend on.
    Returns ``True`` (having already replied with a terse ephemeral 'slow down')
    when the click must stop, else ``False`` after consuming one interactive AND
    one global slot. Best-effort: a missing throttle never blocks a click.
    """

    throttle = _throttle_for(interaction.client)
    if throttle is None:
        return False
    allowed = throttle.allow_interactive(interaction.user.id, interaction.guild_id)
    if allowed:
        allowed = throttle.allow_global()
    if allowed:
        return False
    await _feed_ephemeral(
        interaction,
        _(
            "Slow down a little - too many AniList requests right now. "
            "Give it a few seconds and try again."
        ),
    )
    return True


async def _resolve_token(interaction):
    """Resolve the clicker's AniList token, or reply with the right hint.

    Returns the decrypted token string on success (a local value only, never
    logged), or ``None`` after having sent the appropriate ephemeral hint:
    not linked -> point at ``/anilist login``; expired or undecryptable ->
    ask them to re-link.
    """

    anilist = interaction.client.get_cog("AniList")
    if anilist is None:
        await _feed_ephemeral(
            interaction, _("AniList actions are unavailable right now.")
        )
        return None

    status, token = await anilist._token_status(interaction.user.id)
    if status == "missing":
        await _feed_ephemeral(
            interaction,
            _(
                "Link your AniList account first with `/anilist login`, then "
                "you can like and reply straight from the feed."
            ),
        )
        return None
    if status != "ok" or not token:
        await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link is no longer valid - re-link it with "
                "`/anilist login`."
            ),
        )
        return None
    return token


async def _run_like(interaction, activity_id):
    """Toggle the clicking user's like on the activity, then confirm ephemerally."""

    # Component callbacks run in their own task, where the invocation locale was
    # never set: resolve it first so every _() below renders in the user's tongue.
    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    # Shared interactive throttle before any AniList round-trip (protects the
    # pollers' per-IP share); the cheaper debounce above is the first line.
    if await _deny_feed_action(interaction):
        return
    token = await _resolve_token(interaction)
    if token is None:
        return

    # The mutation is a network round-trip that can outlast the 3s window; defer
    # first, then follow up with the outcome.
    await interactions.defer(
        interaction, ephemeral=True, thinking=True, surface="anilist feed like"
    )

    try:
        data = await _authed_graphql(
            interaction.client,
            token,
            TOGGLE_LIKE_MUTATION,
            {"id": activity_id, "type": "ACTIVITY"},
        )
    except _RateLimited:
        return await _feed_ephemeral(
            interaction, _("AniList is rate limiting me right now - try again shortly.")
        )
    except _AuthError:
        return await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link seems invalid now - re-link it with "
                "`/anilist login`."
            ),
        )
    except _GoneError:
        return await _feed_ephemeral(
            interaction, _("This activity no longer exists on AniList.")
        )
    except _FetchError:
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    result = ((data or {}).get("data") or {}).get("ToggleLikeV2") or {}
    liked = bool(result.get("isLiked"))
    count = result.get("likeCount") or 0
    if liked:
        message = ngettext(
            "Liked - this activity now has {n} like.",
            "Liked - this activity now has {n} likes.",
            count,
        ).format(n=count)
    else:
        message = ngettext(
            "Like removed - this activity now has {n} like.",
            "Like removed - this activity now has {n} likes.",
            count,
        ).format(n=count)
    await _feed_ephemeral(interaction, message)


class _ConfigureEntryView(discord.ui.View):
    """One-button ephemeral follow-up to configure a freshly added entry.

    Attached to the "Added ... to your planning." confirmation, it turns a
    two-step chore (add, then go find the entry to set progress/score) into one
    gesture. The button opens the shared :class:`EditEntryModal` for the media
    just added, pre-selected to PLANNING; the modal resolves the clicker's token
    lazily at submit (never logged or stored). The confirmation is ephemeral, so
    only the adding user can ever see or click this - no extra author gate is
    needed.
    """

    def __init__(self, cog, media, *, timeout=120):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.media = media
        self.message = None
        self.configure.label = _("Set progress / score")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Set progress / score", style=discord.ButtonStyle.primary, emoji="✏️"
    )
    async def configure(self, interaction, button):
        # (Label is localised in __init__; the decorator needs a placeholder.)
        # Component callbacks run in their own task, where the invocation locale
        # was never set: resolve it so the modal renders in the user's tongue.
        await i18n.apply_interaction_locale(interaction)
        try:
            score_format = await self.cog._get_score_format(interaction.user.id)
            await interaction.response.send_modal(
                EditEntryModal(
                    self.cog,
                    self.media,
                    entry={"status": "PLANNING"},
                    score_format=score_format,
                )
            )
        except discord.HTTPException:
            log.debug("AniList feed: could not open the configure-entry modal")


async def _run_add(interaction, media_id):
    """Add the media to the clicking user's planning list, or say it is already there.

    Mirrors :func:`_run_like` exactly: apply the invocation locale, gate on the
    shared per-user debounce and the interactive throttle, then resolve the
    clicker's token (same not-linked / re-link hints). It then acts AS the
    clicking user (Bearer): first an authed lookup of their existing entry
    (``mediaListEntry`` resolves per-viewer), and only when the media is not
    already on their list a ``SaveMediaListEntry`` to PLANNING. The token stays a
    local; it is never logged or stored.
    """

    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    # Shared interactive throttle before any AniList round-trip (protects the
    # pollers' per-IP share); the cheaper debounce above is the first line.
    if await _deny_feed_action(interaction):
        return
    token = await _resolve_token(interaction)
    if token is None:
        return

    # Both round-trips can outlast the 3s window; defer, then follow up.
    await interactions.defer(
        interaction, ephemeral=True, thinking=True, surface="anilist feed add"
    )

    # 1) Look up the viewer's existing entry (and the title) as themselves.
    try:
        data = await _authed_graphql(
            interaction.client, token, ADD_LOOKUP_QUERY, {"id": media_id}
        )
    except _RateLimited:
        return await _feed_ephemeral(
            interaction, _("AniList is rate limiting me right now - try again shortly.")
        )
    except _AuthError:
        return await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link seems invalid now - re-link it with "
                "`/anilist login`."
            ),
        )
    except _GoneError:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    except _FetchError:
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    media = ((data or {}).get("data") or {}).get("Media") or {}
    if not media:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    title = _media_title(media)
    entry = media.get("mediaListEntry")
    if entry:
        return await _feed_ephemeral(
            interaction,
            _("**{title}** is already on your list ({status}).").format(
                title=title, status=_status_word(entry.get("status"))
            ),
        )

    # 2) Not tracked yet: add it to PLANNING as the clicking user.
    try:
        saved = await _authed_graphql(
            interaction.client,
            token,
            SAVE_ENTRY_QUERY,
            {"mediaId": media_id, "status": "PLANNING"},
        )
    except _RateLimited:
        return await _feed_ephemeral(
            interaction, _("AniList is rate limiting me right now - try again shortly.")
        )
    except _AuthError:
        return await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link seems invalid now - re-link it with "
                "`/anilist login`."
            ),
        )
    except _GoneError:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    except _FetchError:
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    if not ((saved or {}).get("data") or {}).get("SaveMediaListEntry"):
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    # Offer a one-gesture follow-up: a single button that opens the pre-filled
    # editor for the entry we just created, so setting progress/score never
    # means hunting the title down again. The AniList cog owns the token/GraphQL
    # helpers the modal needs; if it is somehow unavailable, the plain
    # confirmation still stands.
    cog = interaction.client.get_cog("AniList")
    view = _ConfigureEntryView(cog, media) if cog is not None else None
    sent = await _feed_ephemeral(
        interaction,
        _("Added **{title}** to your planning.").format(title=title),
        view=view,
    )
    if view is not None and sent is not None:
        view.message = sent


async def _run_reply(interaction, activity_id):
    """Open the reply modal for the clicking user (after locale + token checks)."""

    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    # Shared interactive throttle before opening the modal (its submit is the
    # AniList round-trip); the cheaper debounce above is the first line.
    if await _deny_feed_action(interaction):
        return
    # Fail fast with a clear hint before the user types a whole reply; the modal
    # re-fetches the token at submit time, so we deliberately drop this one and
    # never park the decrypted secret on the modal object while they type.
    if await _resolve_token(interaction) is None:
        return

    try:
        await interaction.response.send_modal(_ReplyModal(activity_id))
    except discord.HTTPException:
        log.debug("AniList feed: could not open the reply modal")


class _ReplyModal(LocaleModal):
    """One paragraph field that posts an AniList reply as the submitting user."""

    def __init__(self, activity_id):
        super().__init__(title=_("Reply on AniList"))
        self.activity_id = activity_id
        self.reply_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=REPLY_MAX_LENGTH,
            required=True,
            placeholder=_("Write your reply..."),
        )
        self.add_item(
            discord.ui.Label(text=_("Your reply"), component=self.reply_input)
        )

    async def on_submit(self, interaction):
        # Defer first: posting the reply is a network round-trip.
        await interactions.defer(
            interaction, ephemeral=True, thinking=True, surface="anilist feed reply modal"
        )

        # Re-resolve the token now (it may have expired while typing), keeping the
        # decrypted secret's lifetime confined to this submit task.
        token = await _resolve_token(interaction)
        if token is None:
            return

        text = (self.reply_input.value or "").strip()
        if not text:
            return await _feed_ephemeral(
                interaction, _("Your reply was empty - nothing was posted.")
            )

        try:
            data = await _authed_graphql(
                interaction.client,
                token,
                SAVE_REPLY_MUTATION,
                {"activityId": self.activity_id, "text": text},
            )
        except _RateLimited:
            return await _feed_ephemeral(
                interaction,
                _("AniList is rate limiting me right now - try again shortly."),
            )
        except _AuthError:
            return await _feed_ephemeral(
                interaction,
                _(
                    "Your AniList link seems invalid now - re-link it with "
                    "`/anilist login`."
                ),
            )
        except _GoneError:
            return await _feed_ephemeral(
                interaction, _("This activity no longer exists on AniList.")
            )
        except _FetchError:
            return await _feed_ephemeral(
                interaction, _("I could not reach AniList - try again shortly.")
            )

        reply = ((data or {}).get("data") or {}).get("SaveActivityReply") or {}
        if not reply.get("id"):
            return await _feed_ephemeral(
                interaction, _("AniList did not accept that reply - try again shortly.")
            )
        await _feed_ephemeral(
            interaction,
            _("Your reply was posted. [See it on AniList]({url})").format(
                url=_activity_url(self.activity_id)
            ),
        )
