"""AniList feed interactive surfaces: control panel, tracked-releases manager, action buttons.

Everything the admin (or a card-clicker) touches: the palette constants, the
``/anilistfeed`` control panel and its selects/buttons/modals, the per-feed
tracked-releases manager and its confirm picker, the one-shot notice/list cards,
and the persistent ``alf:like`` / ``alf:reply`` / ``alf:add`` DynamicItem buttons
whose ``custom_id`` templates are load-bearing for cards already posted to
Discord. The button callbacks delegate to the action backends in
``feed_delivery``; nothing here imports ``feed`` or ``feed_render`` (one-way).
"""

from __future__ import annotations

import logging

import discord

from .feed_delivery import _run_add, _run_like, _run_reply
from tools import anilist_feed as af
from tools import i18n, interactions
from tools.i18n import N_, _
from tools.views import _DISABLEABLE, AuthorLayoutView, AuthorView, LocaleModal

log = logging.getLogger(__name__)


# AniList brand blue, the accent for the management-command embeds.
ANILIST_BLUE = 0x02A9FF

# Accent colours for the feed management panel's Components V2 container: green
# when the selected feed is enabled, red when disabled, and the neutral card
# blue (:data:`CARD_ACCENT`) when no feed exists yet.
PANEL_ENABLED = 0x2ECC71
PANEL_DISABLED = 0xE74C3C

# Accent for the activity/digest cards: the media's own cover colour when it has
# one, else this fixed AniList blue (used for every text activity and any list
# activity whose cover carries no colour).
CARD_ACCENT = 0x3DB4F2


# custom_id templates. The three literal prefixes are disjoint so discord.py's
# fullmatch dispatch can never route a like click to the reply/add handler or
# vice versa; ``aid`` is the activity id and ``mid`` the media id (both positive
# ints, so each id part is short and the whole id stays well under the 100-char
# custom_id limit).
LIKE_TEMPLATE = r"alf:like:(?P<aid>\d+)"
REPLY_TEMPLATE = r"alf:reply:(?P<aid>\d+)"
ADD_TEMPLATE = r"alf:add:(?P<mid>\d+)"


class FeedLikeButton(discord.ui.DynamicItem[discord.ui.Button], template=LIKE_TEMPLATE):
    """Persistent heart button that toggles the clicker's like on the activity."""

    def __init__(self, activity_id):
        self.activity_id = activity_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="\N{HEAVY BLACK HEART}",
                custom_id="alf:like:{aid}".format(aid=activity_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]))

    async def callback(self, interaction):
        await _run_like(interaction, self.activity_id)


class FeedReplyButton(
    discord.ui.DynamicItem[discord.ui.Button], template=REPLY_TEMPLATE
):
    """Persistent speech-bubble button that opens the reply modal for the clicker."""

    def __init__(self, activity_id):
        self.activity_id = activity_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="\N{SPEECH BALLOON}",
                custom_id="alf:reply:{aid}".format(aid=activity_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]))

    async def callback(self, interaction):
        await _run_reply(interaction, self.activity_id)


class FeedAddButton(discord.ui.DynamicItem[discord.ui.Button], template=ADD_TEMPLATE):
    """Persistent plus button that adds the media to the clicker's planning list.

    Keyed on the MEDIA id (not the activity id), so it only appears on list
    activities that carry one; a click adds that title to the clicking user's
    AniList planning list, or reports the status it is already tracked under.
    """

    def __init__(self, media_id):
        self.media_id = media_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="\N{HEAVY PLUS SIGN}",
                custom_id="alf:add:{mid}".format(mid=media_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["mid"]))

    async def callback(self, interaction):
        await _run_add(interaction, self.media_id)


# --- Management panel --------------------------------------------------------
#
# The interactive feed control panel opened by the bare ``/anilistfeed``
# command (an :class:`~tools.views.AuthorView`). It edits ONE feed at a time -
# ``selected_channel_id`` - defaulting to the guild's only feed, or its first
# feed when there are two (a select lets the admin switch). Every mutation
# writes straight to the DB through a cog helper, then the panel reloads its
# state fresh from the DB and re-renders in place, mirroring the WelcomePanel
# pattern in ``cogs/config/welcome.py``.

_TYPE_LABELS = {
    "ANIME_LIST": N_("Anime"),
    "MANGA_LIST": N_("Manga"),
    "TEXT": N_("Posts"),
}


class _FeedSwitchSelect(discord.ui.Select):
    """Pick which of the guild's (up to two) feeds the panel is editing."""

    def __init__(self, panel):
        self._owner = panel
        options = []
        for feed in panel.feeds:
            cid = feed["channel_id"]
            options.append(
                discord.SelectOption(
                    label=panel.feed_option_label(cid)[:100],
                    value=str(cid),
                    default=cid == panel.selected_channel_id,
                )
            )
        super().__init__(
            placeholder=_("Switch feed..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            await self._owner.reload_and_refresh(
                interaction, selected_channel_id=int(self.values[0])
            )
        except Exception:
            log.exception("AniList feed panel switch select failed")
            await interactions.notify_failure(interaction)


class _FeedChannelSelect(discord.ui.ChannelSelect):
    """No feed selected: create one here. A feed selected: move it here."""

    def __init__(self, panel):
        self._owner = panel
        defaults = []
        cid = panel.selected_channel_id
        if cid:
            # Only a text/news channel may be a default here: the select is
            # restricted to those types, and Discord rejects a default value
            # whose type is outside channel_types. A legacy thread-based feed
            # (get_channel returns None for a thread) simply gets no default.
            channel = panel.guild.get_channel(cid)
            if channel is not None and channel.type in (
                discord.ChannelType.text,
                discord.ChannelType.news,
            ):
                defaults = [channel]
        placeholder = (
            _("Move this feed to...")
            if cid is not None
            else _("Pick a channel to create a feed...")
        )
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            default_values=defaults,
        )

    async def callback(self, interaction):
        try:
            target = self.values[0]
            cog = self._owner.cog
            if self._owner.selected_channel_id is None:
                error = await cog._create_feed(self._owner.guild.id, target.id)
            else:
                error = await cog._move_feed(
                    self._owner.guild.id, self._owner.selected_channel_id, target.id
                )
            if error:
                return await interactions.reply(interaction, error)
            await self._owner.reload_and_refresh(
                interaction, selected_channel_id=target.id
            )
        except Exception:
            log.exception("AniList feed panel channel select failed")
            await interactions.notify_failure(interaction)


class _TypeToggleButton(discord.ui.Button):
    """One ANIME_LIST/MANGA_LIST/TEXT toggle; green on, grey off."""

    def __init__(self, panel, type_key):
        self._owner = panel
        self.type_key = type_key
        on = type_key in (panel.selected_feed["types"] or ())
        super().__init__(
            label=_(_TYPE_LABELS[type_key]),
            style=(
                discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
            ),
        )

    async def callback(self, interaction):
        try:
            types = set(self._owner.selected_feed["types"] or ())
            on = self.type_key in types
            if on and len(types) <= 1:
                return await interactions.reply(
                    interaction,
                    _("At least one activity type must stay enabled."),
                )
            if on:
                types.discard(self.type_key)
            else:
                types.add(self.type_key)
            await self._owner.cog._set_types(
                self._owner.guild.id, self._owner.selected_channel_id, types
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel type toggle failed")
            await interactions.notify_failure(interaction)


class _SelfAddToggleButton(discord.ui.Button):
    """Flips whether members may join/leave the feed with ``/anilistfeed me``."""

    def __init__(self, panel):
        self._owner = panel
        on = bool(panel.selected_feed["self_add"])
        super().__init__(
            label=_("Members can join: {state}").format(
                state=_("On") if on else _("Off")
            ),
            style=(
                discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
            ),
        )

    async def callback(self, interaction):
        try:
            await self._owner.cog._toggle_self_add(
                self._owner.guild.id, self._owner.selected_channel_id
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel self-add toggle failed")
            await interactions.notify_failure(interaction)


# --- Tracked-releases manager (per-feed explicit title subscriptions) --------
#
# The feed panel's "Tracked releases (N)" button opens this ephemeral manager for
# the selected feed. A feed's subscriptions are the EXPLICIT titles whose new
# episodes (ANIME) / chapters (MANGA) are posted in that channel - a circuit fully
# independent of the DM opt-ins and of who the feed follows. The manager lists the
# tracked titles, offers a modal-driven AniList search to add one (the top matches
# become a confirm select), and a paginated select to remove one; every mutation
# persists through a cog helper and re-renders the same ephemeral message. It is
# ephemeral, so only the admin who opened it can see or click it; both views are
# Components V2 LayoutViews built on the panel's house style, so - like the panel -
# they extend AuthorLayoutView (tools.views), which reapplies the invoker's locale
# and the author gate that AuthorView normally supplies (a LayoutView cannot
# subclass it).


class _TrackConfirmSelect(discord.ui.Select):
    """Pick which AniList search match to subscribe the feed to."""

    def __init__(self, view, candidates):
        self._view = view
        self._by_id = {}
        options = []
        for media in candidates[:25]:
            mid = media.get("id")
            mtype = media.get("type")
            if mid is None or mtype not in ("ANIME", "MANGA"):
                continue
            title = (media.get("title") or {})
            romaji = title.get("romaji") or title.get("english")
            kind = _("Anime") if mtype == "ANIME" else _("Manga")
            year = media.get("seasonYear") or "?"
            label = "[{kind}] {romaji}".format(
                kind=kind, romaji=romaji or _("Unknown title")
            )
            self._by_id[str(mid)] = media
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=str(year)[:100],
                    value=str(mid),
                )
            )
        super().__init__(
            placeholder=_("Pick the title to track..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            media = self._by_id.get(self.values[0])
            await self._view.confirm(interaction, media)
        except Exception:
            log.exception("AniList feed panel track-confirm select failed")
            await interactions.notify_failure(interaction)


class _SubsBackButton(discord.ui.Button):
    """Return from the confirm picker to the tracked-releases manager."""

    def __init__(self, view):
        self._view = view
        super().__init__(label=_("Back"), style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        try:
            await self._view.back(interaction)
        except Exception:
            log.exception("AniList feed panel track-confirm back failed")
            await interactions.notify_failure(interaction)


class _SubsConfirmView(AuthorLayoutView):
    """Ephemeral confirm picker shown after a title search returns matches.

    A single ANILIST_BLUE :class:`~discord.ui.Container` in the panel's house
    style: a ``###`` heading and short prompt, a :class:`_TrackConfirmSelect`
    turning the searched-for candidates into one select, and a Back button.
    Picking a match inserts the subscription (cap-enforced) and re-renders the
    manager; Back simply re-renders the manager unchanged. Reuses the manager's
    cog/guild/channel so it can rebuild it in place on the SAME ephemeral message.
    """

    def __init__(self, cog, guild, author_id, channel_id, candidates, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.channel_id = channel_id
        self._build(candidates)

    def _build(self, candidates):
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        container.add_item(
            discord.ui.TextDisplay(
                "### "
                + _("Track a title")
                + "\n"
                + _("Pick the exact title to track:")
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_TrackConfirmSelect(self, candidates)))
        container.add_item(discord.ui.ActionRow(_SubsBackButton(self)))
        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    async def confirm(self, interaction, media):
        if media is None:
            return await interactions.reply(
                interaction, _("I couldn't read that selection - try again.")
            )
        mid = media.get("id")
        mtype = media.get("type")
        title = (media.get("title") or {})
        cached = title.get("romaji") or title.get("english")
        error = await self.cog._add_channel_sub(
            self.guild.id, self.channel_id, mid, mtype, cached, interaction.user.id
        )
        note = (
            error
            if error
            else _("Now tracking **{title}** in this feed.").format(
                title=cached or str(mid)
            )
        )
        self.stop()
        await self.cog._render_subs_manager(
            interaction, self.guild, self.author_id, self.channel_id, note=note
        )

    async def back(self, interaction):
        self.stop()
        await self.cog._render_subs_manager(
            interaction, self.guild, self.author_id, self.channel_id
        )


class _TrackTitleModal(LocaleModal):
    """Ask for a title, search AniList (unauthenticated), then show the matches."""

    def __init__(self, manager):
        super().__init__(title=_("Track a title"))
        self.manager = manager
        self.query_field = discord.ui.TextInput(
            label=_("Title to search"),
            required=True,
            max_length=100,
        )
        self.add_item(self.query_field)

    async def on_submit(self, interaction):
        # Defer as a message update so we can edit the manager message in place
        # after the (possibly slow) AniList search.
        await interactions.defer(interaction, surface="anilist feed track-title modal")
        try:
            await self.manager.run_search(interaction, self.query_field.value)
        except Exception:
            log.exception("AniList feed panel track-title modal failed")
            await interactions.notify_failure(interaction)


class _TrackTitleButton(discord.ui.Button):
    """Open the search modal to add a title to this feed's tracked releases."""

    def __init__(self, manager):
        self._manager = manager
        super().__init__(label=_("Track a title"), style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        try:
            if self._manager.at_cap:
                return await interactions.reply(
                    interaction,
                    _(
                        "This feed already tracks the maximum of {max} titles. "
                        "Remove one first."
                    ).format(max=af.MAX_SUBS_PER_FEED),
                )
            await interaction.response.send_modal(_TrackTitleModal(self._manager))
        except Exception:
            log.exception("AniList feed panel track-title launch failed")
            await interactions.notify_failure(interaction)


class _RemoveSubSelect(discord.ui.Select):
    """Pick a currently-tracked title (by cached name) to stop tracking."""

    def __init__(self, manager, window):
        self._manager = manager
        options = []
        for row in window:
            mtype = row["media_type"]
            kind = _("Anime") if mtype == "ANIME" else _("Manga")
            title = row["title"] or str(row["media_id"])
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    description=kind,
                    value=str(row["media_id"]),
                )
            )
        super().__init__(
            placeholder=_("Stop tracking a title..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            media_id = int(self.values[0])
            await self._manager.cog._remove_channel_sub(
                self._manager.guild.id, self._manager.channel_id, media_id
            )
            # Stop the old view first so its timeout can never fire and clobber the
            # re-rendered message with a stale, disabled layout.
            self._manager.stop()
            await self._manager.cog._render_subs_manager(
                interaction,
                self._manager.guild,
                self._manager.author_id,
                self._manager.channel_id,
                page=self._manager.page,
            )
        except Exception:
            log.exception("AniList feed panel remove-sub select failed")
            await interactions.notify_failure(interaction)


class _SubsPageButton(discord.ui.Button):
    """Page the remove select through a feed's subscriptions (25 per page)."""

    def __init__(self, manager, *, forward, disabled):
        self._manager = manager
        self._forward = forward
        super().__init__(
            label=_("Next") if forward else _("Previous"),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction):
        try:
            page = self._manager.page + (1 if self._forward else -1)
            self._manager.stop()
            await self._manager.cog._render_subs_manager(
                interaction,
                self._manager.guild,
                self._manager.author_id,
                self._manager.channel_id,
                page=page,
            )
        except Exception:
            log.exception("AniList feed panel subs paging failed")
            await interactions.notify_failure(interaction)


class _SubsManagerView(AuthorLayoutView):
    """Ephemeral per-feed tracked-releases manager (list / add / remove).

    A single ANILIST_BLUE :class:`~discord.ui.Container` in the panel's house
    style: a ``###`` heading, an optional note paragraph, the "posted in
    {channel}" explanation, the subscribed titles as a bullet list with a ``-#``
    "Tracking N/max" subline, then the controls - a remove
    :class:`~discord.ui.ActionRow` select (paged at 25 per page), a Track-a-title
    button and, when there is more than one page, Previous/Next buttons. Every
    mutation persists through a cog helper and the cog re-renders this same
    ephemeral message from fresh DB state, so the list can never drift from what
    is stored.
    """

    PAGE_SIZE = 25

    def __init__(
        self, cog, guild, author_id, channel_id, subs, *, page=0, note=None,
        timeout=180,
    ):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.channel_id = channel_id
        self.subs = list(subs)
        self.note = note
        page_count = max(1, (len(self.subs) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(page, page_count - 1))
        self._page_count = page_count
        self._build()

    @property
    def at_cap(self):
        return len(self.subs) >= af.MAX_SUBS_PER_FEED

    def _build(self):
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)

        channel = self.guild.get_channel_or_thread(self.channel_id)
        label = channel.mention if channel is not None else str(self.channel_id)
        header_parts = ["### " + _("Tracked releases")]
        if self.note:
            # The note already carries its own inline emphasis (e.g. a bold
            # title), so it is added as its own paragraph, not re-wrapped in bold.
            header_parts.append(self.note)
        header_parts.append(
            _(
                "New episodes (anime) and chapters (manga) of these titles are "
                "posted in {channel}, independently of any DM alerts. Up to {max} "
                "titles per feed."
            ).format(channel=label, max=af.MAX_SUBS_PER_FEED)
        )
        container.add_item(discord.ui.TextDisplay("\n\n".join(header_parts)))

        lines = []
        for row in self.subs:
            kind = _("Anime") if row["media_type"] == "ANIME" else _("Manga")
            title = row["title"] or str(row["media_id"])
            lines.append("- [{kind}] {title}".format(kind=kind, title=title))
        listing = "\n".join(lines) if lines else _("No titles tracked yet.")
        if len(listing) > 3500:
            listing = listing[:3500].rstrip() + "\n..."
        tracking = "-# " + _("Tracking {count}/{max}").format(
            count=len(self.subs), max=af.MAX_SUBS_PER_FEED
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(listing + "\n\n" + tracking))

        # Controls: the remove select (own ActionRow, only when the page has
        # rows), then a button row with Track-a-title plus, when paged, Prev/Next.
        start = self.page * self.PAGE_SIZE
        window = self.subs[start : start + self.PAGE_SIZE]
        container.add_item(discord.ui.Separator())
        if window:
            container.add_item(discord.ui.ActionRow(_RemoveSubSelect(self, window)))
        button_row = discord.ui.ActionRow()
        button_row.add_item(_TrackTitleButton(self))
        if self._page_count > 1:
            button_row.add_item(
                _SubsPageButton(self, forward=False, disabled=self.page == 0)
            )
            button_row.add_item(
                _SubsPageButton(
                    self, forward=True, disabled=self.page >= self._page_count - 1
                )
            )
        container.add_item(button_row)

        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    async def run_search(self, interaction, query):
        """Search AniList for ``query`` and edit the manager into the confirm picker.

        Runs UNAUTHENTICATED (no token at any point). No matches leaves the manager
        in place with a note; matches replace it with a :class:`_SubsConfirmView` on
        the SAME ephemeral message so the whole add flow stays on one message.
        """

        candidates = await self.cog._search_channel_candidates(query)
        if not candidates:
            self.stop()
            return await self.cog._render_subs_manager(
                interaction,
                self.guild,
                self.author_id,
                self.channel_id,
                page=self.page,
                note=_("No AniList match for **{query}**.").format(
                    query=(query or "").strip() or "?"
                ),
            )
        confirm = _SubsConfirmView(
            self.cog, self.guild, self.author_id, self.channel_id, candidates
        )
        confirm.message = self.message
        self.stop()
        try:
            # A Components V2 message carries its content inside the view, so edit
            # with ``view=`` only (Discord rejects an ``embed=`` on such an edit).
            await interaction.edit_original_response(view=confirm)
        except discord.HTTPException:
            log.warning(
                "AniList feed: could not render the track-confirm picker", exc_info=True
            )


class _TrackedReleasesButton(discord.ui.Button):
    """Open the ephemeral tracked-releases manager for the selected feed.

    Manage-guild gated exactly like the sibling controls: the panel is only opened
    from the admin-only bare-panel path and is author-restricted, so no per-button
    permission check is needed. The count in the label is a snapshot from the last
    panel render; the manager itself always shows live state.
    """

    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Tracked releases ({count})").format(count=panel.subs_count),
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction):
        try:
            panel = self._owner
            await panel.cog._render_subs_manager(
                interaction,
                panel.guild,
                panel.author_id,
                panel.selected_channel_id,
                new=True,
            )
        except Exception:
            log.exception("AniList feed panel tracked-releases open failed")
            await interactions.notify_failure(interaction)


class _EnableButton(discord.ui.Button):
    """Enable/disable the selected feed; re-enabling clears fail_count."""

    def __init__(self, panel):
        self._owner = panel
        enabled = bool(panel.selected_feed["enabled"])
        super().__init__(
            label=_("Disable") if enabled else _("Enable"),
            style=(
                discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
            ),
        )

    async def callback(self, interaction):
        try:
            enabled = bool(self._owner.selected_feed["enabled"])
            await self._owner.cog._set_enabled(
                self._owner.guild.id, self._owner.selected_channel_id, not enabled
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel enable toggle failed")
            await interactions.notify_failure(interaction)


class _DeleteConfirmView(AuthorView):
    """Ephemeral Confirm/Cancel prompt for deleting the selected feed."""

    def __init__(self, panel, timeout=30):
        super().__init__(
            panel.author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.panel = panel
        self.confirm_button.label = _("Delete")
        self.cancel_button.label = _("Cancel")

    def build_embed(self):
        return discord.Embed(
            title=_("Delete this feed?"),
            description=_(
                "This permanently deletes the AniList feed in {channel} and "
                "everyone it follows. This cannot be undone."
            ).format(channel=self.panel.feed_label(self.panel.selected_channel_id)),
            colour=0xE74C3C,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction, button):
        try:
            await self.panel.cog._delete_feed_rows(
                self.panel.guild.id, self.panel.selected_channel_id
            )
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=_("Feed deleted."), embed=None, view=self
            )
            await self.panel.sync_message()
        except Exception:
            log.exception("AniList feed panel delete confirm failed")
            await interactions.notify_failure(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction, button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(
                content=_("Cancelled."), embed=None, view=self
            )
        except discord.HTTPException:
            pass


class _DeleteButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Delete feed"), style=discord.ButtonStyle.danger
        )

    async def callback(self, interaction):
        try:
            view = _DeleteConfirmView(self._owner)
            await interaction.response.send_message(
                embed=view.build_embed(), view=view, ephemeral=True
            )
        except Exception:
            log.exception("AniList feed panel delete launch failed")
            await interactions.notify_failure(interaction)


class AddFollowModal(LocaleModal):
    """Ask for an AniList username, resolve it, then follow it on the feed."""

    def __init__(self, panel):
        super().__init__(title=_("Add a follow"))
        self.panel = panel
        self.username_field = discord.ui.TextInput(
            label=_("AniList username"),
            required=True,
            max_length=50,
        )
        self.add_item(self.username_field)

    async def on_submit(self, interaction):
        await interactions.defer(
            interaction, ephemeral=True, thinking=True, surface="anilist feed add-follow modal"
        )
        try:
            cog = self.panel.cog
            user_id, name, _url, error = await cog._resolve_anilist_user(
                self.username_field.value
            )
            if error:
                return await interactions.reply(interaction, error)
            error = await cog._add_follow(
                self.panel.guild.id,
                self.panel.selected_channel_id,
                user_id,
                name,
                interaction.user.id,
            )
            if error:
                return await interactions.reply(interaction, error)
            await self.panel.reload_and_refresh(interaction)
            await interactions.reply(
                interaction, _("Now following **{name}**.").format(name=name)
            )
        except Exception:
            log.exception("AniList feed panel add-follow modal failed")
            await interactions.notify_failure(interaction)


class _AddFollowButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Add follow"), style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        try:
            if len(self._owner.follows) >= af.MAX_FOLLOWS_PER_FEED:
                return await interactions.reply(
                    interaction,
                    _("This feed already follows the maximum of {max} users.").format(
                        max=af.MAX_FOLLOWS_PER_FEED
                    ),
                )
            await interaction.response.send_modal(AddFollowModal(self._owner))
        except Exception:
            log.exception("AniList feed panel add-follow launch failed")
            await interactions.notify_failure(interaction)


class _RemoveFollowSelect(discord.ui.Select):
    """Pick a currently-followed user (by cached name) to unfollow."""

    def __init__(self, panel):
        self._owner = panel
        options = [
            discord.SelectOption(
                label=(row["anilist_username"] or str(row["anilist_user_id"]))[:100],
                value=str(row["anilist_user_id"]),
            )
            for row in panel.follows[:25]
        ]
        super().__init__(
            placeholder=_("Remove a follow..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            user_id = int(self.values[0])
            await self._owner.cog._remove_follow(
                self._owner.guild.id, self._owner.selected_channel_id, user_id
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel remove-follow select failed")
            await interactions.notify_failure(interaction)


async def _refresh_layout(interaction, message, view):
    """Edit a LayoutView panel in place with ``view=`` only (no embed/content).

    Mirrors :func:`tools.interactions.refresh_in_place` but never passes an embed:
    a Components V2 message carries its content inside the view and Discord
    rejects an ``embed=`` on such an edit. Tries the live interaction edit first,
    then falls back to editing the stored message when the interaction was
    already answered (e.g. a deferred modal submit).
    """

    await interactions.refresh_layout(
        interaction, message, view, surface="anilist feed panel"
    )


class AniListFeedPanel(discord.ui.LayoutView):
    """Author-restricted AniList feed control panel (the panel entry point).

    A single Components V2 :class:`~discord.ui.Container` whose accent tracks the
    selected feed's state - green enabled, red disabled, neutral card blue when
    no feed exists - giving it visual kinship with the activity cards it
    configures. Edits exactly one feed at a time (``selected_channel_id``); with
    two feeds a switch select sits under the header. With no feed at all only a
    creation ChannelSelect is shown. Every mutation persists through a cog helper
    and the panel reloads fresh state from the DB before re-rendering, so it can
    never drift from what is actually stored.

    LayoutView cannot subclass :class:`~tools.views.AuthorView` (that is a plain
    ``discord.ui.View``), so the author gate and locale resolution are
    reimplemented here in :meth:`interaction_check` exactly as AuthorView does
    them, and :meth:`on_timeout` disables every control and edits the message.
    """

    def __init__(
        self,
        cog,
        guild,
        author_id,
        feeds,
        selected_channel_id,
        follows,
        subs_count=0,
        timeout=180,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.message = None
        self.feeds = list(feeds)
        self.selected_channel_id = selected_channel_id
        self.follows = list(follows)
        self.subs_count = subs_count
        self._build()

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localize.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This panel isn't for you."), ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        """Disable every button/select in the layout (walks nested ActionRows)."""

        for child in self.walk_children():
            if isinstance(child, _DISABLEABLE):
                child.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @property
    def selected_feed(self):
        for feed in self.feeds:
            if feed["channel_id"] == self.selected_channel_id:
                return feed
        return None

    def feed_label(self, channel_id):
        """A clickable ``<#id>`` mention, for use in the panel's text."""

        channel = self.guild.get_channel_or_thread(channel_id)
        return channel.mention if channel is not None else str(channel_id)

    def feed_option_label(self, channel_id):
        """A plain-text label, for use in select option labels (no markdown)."""

        channel = self.guild.get_channel_or_thread(channel_id)
        return ("#" + channel.name) if channel is not None else str(channel_id)

    def _build(self):
        """(Re)assemble the layout from the current feed/follow state."""

        self.clear_items()
        feed = self.selected_feed

        if feed is None:
            accent = PANEL_DISABLED if self.feeds else CARD_ACCENT
        else:
            accent = PANEL_ENABLED if feed["enabled"] else PANEL_DISABLED
        container = discord.ui.Container(accent_colour=accent)

        # Zero-feed state: a friendly creation prompt plus the ChannelSelect.
        if not self.feeds:
            container.add_item(
                discord.ui.TextDisplay(
                    "### "
                    + _("AniList activity feed")
                    + "\n"
                    + _(
                        "This server has no AniList feed yet. Pick a channel "
                        "below to create one (up to {max} per server)."
                    ).format(max=af.MAX_FEEDS_PER_GUILD)
                )
            )
            container.add_item(discord.ui.ActionRow(_FeedChannelSelect(self)))
            container.add_item(
                discord.ui.TextDisplay(
                    "-# " + _("Only you can use these controls.")
                )
            )
            self.add_item(container)
            return

        # Header: title, a short reassurance, and the selected feed's channel +
        # status line (fail_count only when it is non-zero). Reading order is
        # header first (identity + state), then the scope selects right under it
        # (switch feed / move channel), then the Types and Follows configuration,
        # then the destructive actions - a clean top-down flow.
        status = _("Enabled") if feed["enabled"] else _("Disabled")
        if feed["fail_count"]:
            status = _("{status} ({count} recent failures)").format(
                status=status, count=feed["fail_count"]
            )
        header_lines = [
            "### " + _("AniList activity feed"),
            _(
                "Configure how AniList activity is mirrored into this server. "
                "Every change saves instantly."
            ),
            "**{channel}:** {mention}   **{status}:** {value}".format(
                channel=_("Channel"),
                mention=self.feed_label(feed["channel_id"]),
                status=_("Status"),
                value=status,
            ),
        ]
        if len(self.feeds) >= 2:
            header_lines.append(
                "-# "
                + _("Feeds")
                + ": "
                + ", ".join(self.feed_label(f["channel_id"]) for f in self.feeds)
            )
        container.add_item(discord.ui.TextDisplay("\n".join(header_lines)))

        if len(self.feeds) >= 2:
            container.add_item(discord.ui.ActionRow(_FeedSwitchSelect(self)))
        container.add_item(discord.ui.ActionRow(_FeedChannelSelect(self)))

        # Types: a label above the row of the three type toggles + the self-add
        # toggle (the buttons themselves carry the on/off state via their colour).
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("**" + _("Types") + "**"))
        type_row = discord.ui.ActionRow()
        for type_key in af.ALLOWED_TYPES:
            type_row.add_item(_TypeToggleButton(self, type_key))
        type_row.add_item(_SelfAddToggleButton(self))
        container.add_item(type_row)

        # Tracked releases: the feed's EXPLICIT title subscriptions (new episodes /
        # chapters posted in this channel). A circuit independent of the DM opt-ins
        # and of who the feed follows, so it gets its own button opening a dedicated
        # manager rather than sitting among the activity-type toggles.
        container.add_item(discord.ui.ActionRow(_TrackedReleasesButton(self)))

        # Follows: the followed-user list, then (when there are any) the remove
        # select, then the enable/delete/add-follow action row.
        container.add_item(discord.ui.Separator())
        if self.follows:
            names = ", ".join(
                row["anilist_username"] or str(row["anilist_user_id"])
                for row in self.follows
            )
            if len(names) > 900:
                names = names[:900].rstrip() + "..."
        else:
            names = _("no one yet")
        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Following ({count})").format(count=len(self.follows))
                + "**\n"
                + names
            )
        )
        if self.follows:
            container.add_item(discord.ui.ActionRow(_RemoveFollowSelect(self)))
        container.add_item(
            discord.ui.ActionRow(
                _EnableButton(self), _DeleteButton(self), _AddFollowButton(self)
            )
        )

        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    async def _reloaded(self, selected_channel_id):
        cog = self.cog
        feeds = await cog._feeds_for_guild(self.guild.id)
        if selected_channel_id is None:
            selected_channel_id = self.selected_channel_id
        channel_ids = {feed["channel_id"] for feed in feeds}
        if selected_channel_id not in channel_ids:
            selected_channel_id = feeds[0]["channel_id"] if feeds else None
        follows = (
            await cog._follows_for_feed(self.guild.id, selected_channel_id)
            if selected_channel_id is not None
            else []
        )
        subs_count = (
            await cog._channel_sub_count(self.guild.id, selected_channel_id)
            if selected_channel_id is not None
            else 0
        )
        new = AniListFeedPanel(
            cog,
            self.guild,
            self.author_id,
            feeds,
            selected_channel_id,
            follows,
            subs_count,
        )
        new.message = self.message
        return new

    async def reload_and_refresh(self, interaction, *, selected_channel_id=None):
        """Reload feed/follow state from the DB and re-render in place."""

        new = await self._reloaded(selected_channel_id)
        self.stop()
        await _refresh_layout(interaction, self.message, new)

    async def sync_message(self):
        """Re-render the stored panel message directly (used by the delete confirm)."""

        if self.message is None:
            return
        new = await self._reloaded(None)
        self.stop()
        try:
            await self.message.edit(view=new)
        except discord.HTTPException:
            pass


class _FeedNoticeView(discord.ui.LayoutView):
    """A one-shot ANILIST_BLUE notice card in the feed panel's house style.

    Non-interactive replacement for the classic ``discord.Embed`` replies of the
    ``/anilistfeed set`` / ``follow`` commands: a ``###`` heading over a body
    block inside a single :class:`~discord.ui.Container`. Carries no components,
    so it needs no author gating and spawns no timeout task.
    """

    def __init__(self, heading, body, *, timeout=None):
        super().__init__(timeout=timeout)
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        text = "### " + heading
        if body:
            text += "\n" + body
        container.add_item(discord.ui.TextDisplay(text))
        self.add_item(container)


class _FeedListView(discord.ui.LayoutView):
    """The ``/anilistfeed list`` output as a Components V2 card.

    A ``###`` heading over one titled block per feed (channel label in bold, then
    its status / types / follows lines), each block separated by a rule - the
    house-style equivalent of the per-feed embed fields it replaces. ``blocks`` is
    a list of ``(label, body)`` pairs. Non-interactive, so no gating or timeout.
    """

    def __init__(self, heading, blocks, *, timeout=None):
        super().__init__(timeout=timeout)
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        container.add_item(discord.ui.TextDisplay("### " + heading))
        for label, body in blocks:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "**{label}**\n{body}".format(label=label, body=body)
                )
            )
        self.add_item(container)
