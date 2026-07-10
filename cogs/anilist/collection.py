"""The interactive AniList collection dashboard (the ``/anilist list`` panel).

Where the old ``/anilist list`` printed read-only paginated text embeds, this
opens an author-restricted Components V2 :class:`CollectionView` for the
invoker's OWN list: a status filter, an anime/manga segmented control, a
page-at-a-time
entry picker and - once an entry is picked - an entry card whose ActionRow
(+1 / -1 / Complete / Drop / Edit) drives the SAME ``SaveMediaListEntry``
plumbing the media editor uses. Every quick action patches the local entry from
the mutation response and re-renders in place, so the refreshed card IS the
confirmation; Edit reuses the media editor's :class:`EditEntryModal` verbatim.

Assembly mirrors :mod:`cogs.anilist.hub`: :class:`CollectionMixin` folds into the
cog and both the command and the hub's "My list" button route through
:meth:`CollectionMixin._collection_payload`. The view is built with the invoker's
token at open and re-fetches with a freshly resolved token on every reload;
mutations resolve the token again at click time (the codebase-wide discipline).
"""

import logging

import discord

from .components import CompletePromptView, EditEntryModal
from .helpers import (
    DEFAULT_SCORE_FORMAT,
    _media_title,
    _media_unit,
    _progress_max,
    render_score,
)
from .queries import COLLECTION_QUERY, VIEWER_QUERY
from tools import i18n, interactions
from tools.i18n import _, ngettext

log = logging.getLogger(__name__)

# AniList brand blue, the dashboard container's accent (matches the hub and the
# feed activity cards).
COLLECTION_ACCENT = 0x3DB4F2


# ----------------------------------------------------------------------
# Interactive components (each holds the owning view as ``_owner`` so the
# nested ActionRow items resolve state the same way the feed panel does).
# ----------------------------------------------------------------------
class _StatusFilterSelect(discord.ui.Select):
    """Status filter, reusing the media editor's status vocabulary/emojis."""

    def __init__(self, owner):
        self._owner = owner
        watching = _("Reading") if owner.media_type == "manga" else _("Watching")
        current = owner.status
        options = [
            discord.SelectOption(
                label=watching, value="CURRENT", emoji="▶️",
                default=current == "CURRENT",
            ),
            discord.SelectOption(
                label=_("Planning"), value="PLANNING", emoji="📝",
                default=current == "PLANNING",
            ),
            discord.SelectOption(
                label=_("Completed"), value="COMPLETED", emoji="✅",
                default=current == "COMPLETED",
            ),
            discord.SelectOption(
                label=_("Paused"), value="PAUSED", emoji="⏸️",
                default=current == "PAUSED",
            ),
            discord.SelectOption(
                label=_("Dropped"), value="DROPPED", emoji="🗑️",
                default=current == "DROPPED",
            ),
            discord.SelectOption(
                label=_("Repeating"), value="REPEATING", emoji="🔁",
                default=current == "REPEATING",
            ),
        ]
        super().__init__(
            placeholder=_("Filter by status..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await self._owner._change_status(interaction, self.values[0])


class _TypeTabButton(discord.ui.Button):
    """One tab of the anime/manga segmented control.

    The ACTIVE type renders as a disabled primary tab - the dashboard's accent
    style (matching the Edit action) - which reads as "you are here" and blocks
    a pointless refetch. The INACTIVE type is an enabled secondary tab; clicking
    it flips to the other type via the same seam the old single toggle used
    (only the inactive tab is clickable, so a plain flip always lands on this
    tab's type).
    """

    def __init__(self, owner, media_type):
        self._owner = owner
        self._media_type = media_type
        active = owner.media_type == media_type
        label, emoji = (
            (_("Anime"), "📺") if media_type == "anime" else (_("Manga"), "📚")
        )
        super().__init__(
            label=label,
            emoji=emoji,
            style=(
                discord.ButtonStyle.primary
                if active
                else discord.ButtonStyle.secondary
            ),
            disabled=active,
        )

    async def callback(self, interaction):
        await self._owner._toggle_type(interaction)


class _EntrySelect(discord.ui.Select):
    """The current page of entries; picking one renders its entry card."""

    def __init__(self, owner):
        self._owner = owner
        options = []
        for entry in owner._page_entries():
            media = entry.get("media") or {}
            mid = media.get("id")
            total = _progress_max(media)
            progress = entry.get("progress") or 0
            desc = "{progress}/{total}".format(
                progress=progress, total=total if total else "?"
            )
            score = render_score(entry.get("score"), owner.score_format)
            if score:
                desc += "  " + score
            options.append(
                discord.SelectOption(
                    label=_media_title(media)[:100],
                    description=desc[:100],
                    value=str(mid),
                    default=(mid == owner.selected_media_id),
                )
            )
        super().__init__(
            placeholder=_("Pick an entry..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await self._owner._select_entry(interaction, int(self.values[0]))


class _PagePrevButton(discord.ui.Button):
    """Step to the previous page of 25 entries."""

    def __init__(self, owner):
        self._owner = owner
        super().__init__(
            emoji="◀️", style=discord.ButtonStyle.secondary, disabled=owner.page <= 0
        )

    async def callback(self, interaction):
        await self._owner._change_page(interaction, -1)


class _PageNextButton(discord.ui.Button):
    """Step to the next page of 25 entries."""

    def __init__(self, owner):
        self._owner = owner
        super().__init__(
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            disabled=owner.page >= owner._page_count() - 1,
        )

    async def callback(self, interaction):
        await self._owner._change_page(interaction, +1)


class _IncrementButton(discord.ui.Button):
    """+1 progress (clamped to the total); reuses the shared save plumbing."""

    def __init__(self, owner, media):
        self._owner = owner
        self._media = media
        super().__init__(label="+1", style=discord.ButtonStyle.success)

    async def callback(self, interaction):
        await self._owner._step(interaction, self._media, +1)


class _DecrementButton(discord.ui.Button):
    """-1 progress (clamped at 0)."""

    def __init__(self, owner, media):
        self._owner = owner
        self._media = media
        super().__init__(label="-1", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        await self._owner._step(interaction, self._media, -1)


class _CompleteButton(discord.ui.Button):
    """Mark the entry completed (status COMPLETED + progress to the total)."""

    def __init__(self, owner, media):
        self._owner = owner
        self._media = media
        super().__init__(
            label=_("Complete"), emoji="✅", style=discord.ButtonStyle.success
        )

    async def callback(self, interaction):
        await self._owner._mutate(interaction, self._media, "complete", None)


class _DropButton(discord.ui.Button):
    """Set the entry's status to DROPPED."""

    def __init__(self, owner, media):
        self._owner = owner
        self._media = media
        super().__init__(
            label=_("Drop"), emoji="🗑️", style=discord.ButtonStyle.danger
        )

    async def callback(self, interaction):
        await self._owner._mutate(interaction, self._media, "status", "DROPPED")


class _EditButton(discord.ui.Button):
    """Open the media editor's existing pre-filled edit modal for the entry."""

    def __init__(self, owner, media):
        self._owner = owner
        self._media = media
        super().__init__(
            label=_("Edit"), emoji="✏️", style=discord.ButtonStyle.primary
        )

    async def callback(self, interaction):
        await self._owner._open_edit(interaction, self._media)


class CollectionView(discord.ui.LayoutView):
    """Author-restricted Components V2 dashboard over the invoker's own list.

    A single AniList-blue :class:`~discord.ui.Container`: a header (type + status
    + count), a status filter select and an anime/manga segmented control, then
    the current
    page of entries as a select (prev/next only when the list exceeds one page),
    and - once an entry is picked - its card (cover thumbnail, bold title link,
    status/progress line, score line) above a +1 / -1 / Complete / Drop / Edit
    action row. Every quick action patches local state from the mutation response
    and re-renders in place, so the refreshed card is the confirmation.

    Gated exactly like :class:`~cogs.anilist.feed.AniListFeedPanel`: locale is
    resolved first, other users are denied with the shared "This panel isn't for
    you." wording, and a finite timeout disables every control. All edits are
    ``view=``-only, as a Components V2 message requires.
    """

    PAGE_SIZE = 25

    def __init__(
        self,
        cog,
        author_id,
        anilist_user_id,
        media_type,
        status,
        entries,
        *,
        score_format=DEFAULT_SCORE_FORMAT,
        timeout=180,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.anilist_user_id = anilist_user_id
        self.media_type = media_type
        self.status = status
        self.score_format = score_format
        self.message = None
        self.page = 0
        self.selected_media_id = None
        self._set_entries(entries)
        self._build()

    # -- state ---------------------------------------------------------
    def _set_entries(self, entries):
        """Adopt a fresh entry list, deduped by media id and title-sorted.

        Dedup is not cosmetic: a media that sits in two custom lists would
        otherwise yield two select options sharing a value, which Discord
        rejects.
        """

        self.entries = []
        self._by_id = {}
        for entry in entries or []:
            mid = (entry.get("media") or {}).get("id")
            if mid is None or mid in self._by_id:
                continue
            self._by_id[mid] = entry
            self.entries.append(entry)
        self.entries.sort(
            key=lambda e: (_media_title(e.get("media") or {}) or "").lower()
        )

    def _page_count(self):
        if not self.entries:
            return 1
        return (len(self.entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE

    def _page_entries(self):
        start = self.page * self.PAGE_SIZE
        return self.entries[start : start + self.PAGE_SIZE]

    def _type_word(self):
        return _("Anime") if self.media_type == "anime" else _("Manga")

    def _status_word(self, status):
        """Localised label for a list status, reusing the editor's vocabulary."""

        if status == "CURRENT":
            return _("Reading") if self.media_type == "manga" else _("Watching")
        return {
            "PLANNING": _("Planning"),
            "COMPLETED": _("Completed"),
            "PAUSED": _("Paused"),
            "DROPPED": _("Dropped"),
            "REPEATING": _("Repeating"),
        }.get(status, str(status).title())

    # -- gate / lifecycle ----------------------------------------------
    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localise.
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
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # -- layout --------------------------------------------------------
    def _header_text(self):
        total = len(self.entries)
        count = ngettext("{count} entry", "{count} entries", total).format(
            count=total
        )
        return (
            "## "
            + _("Your {type} list").format(type=self._type_word())
            + "\n-# "
            + self._status_word(self.status)
            + " - "
            + count
        )

    def _add_entry_card(self, container, entry):
        media = entry.get("media") or {}
        title = _media_title(media)
        url = media.get("siteUrl")
        title_line = (
            "**[{title}]({url})**".format(title=title, url=url)
            if url
            else "**{title}**".format(title=title)
        )

        total = _progress_max(media)
        progress = entry.get("progress") or 0
        total_text = str(total) if total else "?"
        if _media_unit(media) == "chapter":
            progress_line = _("Chapter {progress}/{total}").format(
                progress=progress, total=total_text
            )
        else:
            progress_line = _("Episode {progress}/{total}").format(
                progress=progress, total=total_text
            )

        lines = [
            title_line,
            self._status_word(entry.get("status") or self.status)
            + " - "
            + progress_line,
        ]
        score = render_score(entry.get("score"), self.score_format)
        if score:
            lines.append(_("Score: {score}").format(score=score))
        text = discord.ui.TextDisplay("\n".join(lines))

        cover = (media.get("coverImage") or {}).get("large")
        if cover:
            container.add_item(
                discord.ui.Section(text, accessory=discord.ui.Thumbnail(cover))
            )
        else:
            container.add_item(text)

        row = discord.ui.ActionRow()
        row.add_item(_IncrementButton(self, media))
        row.add_item(_DecrementButton(self, media))
        row.add_item(_CompleteButton(self, media))
        row.add_item(_DropButton(self, media))
        row.add_item(_EditButton(self, media))
        container.add_item(row)

    def _build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=COLLECTION_ACCENT)
        container.add_item(discord.ui.TextDisplay(self._header_text()))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_StatusFilterSelect(self)))
        container.add_item(
            discord.ui.ActionRow(
                _TypeTabButton(self, "anime"), _TypeTabButton(self, "manga")
            )
        )
        container.add_item(discord.ui.Separator())

        if not self.entries:
            container.add_item(
                discord.ui.TextDisplay(
                    _("Nothing on your {status} {media_type} list.").format(
                        status=self._status_word(self.status),
                        media_type=self._type_word(),
                    )
                )
            )
            self.add_item(container)
            return

        # Guard against a shrunken list (e.g. a reload returning fewer entries).
        total_pages = self._page_count()
        if self.page >= total_pages:
            self.page = total_pages - 1

        container.add_item(discord.ui.ActionRow(_EntrySelect(self)))
        if len(self.entries) > self.PAGE_SIZE:
            container.add_item(
                discord.ui.TextDisplay(
                    "-# "
                    + _("Page {current}/{total}").format(
                        current=self.page + 1, total=total_pages
                    )
                )
            )
            container.add_item(
                discord.ui.ActionRow(_PagePrevButton(self), _PageNextButton(self))
            )

        entry = self._by_id.get(self.selected_media_id)
        if entry is not None:
            container.add_item(discord.ui.Separator())
            self._add_entry_card(container, entry)

        self.add_item(container)

    # -- re-render helpers ---------------------------------------------
    async def _rerender(self, interaction):
        """Edit the dashboard message in place, ``view=``-only.

        Prefers the live interaction edit; falls back to the deferred original
        response and then the stored message, mirroring the feed panel's
        ``_refresh_layout``.
        """

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
                return
        except discord.HTTPException:
            pass
        try:
            await interaction.edit_original_response(view=self)
            return
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # -- callbacks -----------------------------------------------------
    async def _change_status(self, interaction, status):
        try:
            self.status = status
            self.page = 0
            self.selected_media_id = None
            await self._reload_entries(interaction)
        except Exception:
            log.exception("AniList collection status change failed")
            await interactions.notify_failure(interaction)

    async def _toggle_type(self, interaction):
        try:
            self.media_type = "manga" if self.media_type == "anime" else "anime"
            self.page = 0
            self.selected_media_id = None
            await self._reload_entries(interaction)
        except Exception:
            log.exception("AniList collection type toggle failed")
            await interactions.notify_failure(interaction)

    async def _reload_entries(self, interaction):
        """Re-fetch the collection for the current type/status and re-render.

        Resolves the token fresh (the dashboard is rebuilt with the invoker's
        token at every reload) and defers first, since the fetch is a network
        round-trip that can outlast the 3s window.
        """

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        token = await self.cog._get_token(self.author_id)
        if not token:
            return await interactions.reply(
                interaction, _("Link your account first with `/anilist login`.")
            )
        entries = await self.cog._fetch_collection(
            token, self.anilist_user_id, self.media_type, self.status
        )
        self._set_entries(entries)
        self._build()
        await self._rerender(interaction)

    async def _change_page(self, interaction, delta):
        try:
            self.page = max(0, min(self.page + delta, self._page_count() - 1))
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("AniList collection page change failed")
            await interactions.notify_failure(interaction)

    async def _select_entry(self, interaction, media_id):
        try:
            self.selected_media_id = media_id
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("AniList collection entry select failed")
            await interactions.notify_failure(interaction)

    async def _step(self, interaction, media, delta):
        """+1 / -1 the entry's progress, clamped to ``[0, total]``, then save."""

        entry = self._by_id.get(media.get("id"))
        prior_status = (entry or {}).get("status")
        current = (entry or {}).get("progress") or 0
        new = current + delta
        if new < 0:
            new = 0
        maximum = _progress_max(media)
        if maximum is not None and new > maximum:
            new = maximum
        saved = await self._mutate(interaction, media, "progress", new)
        if delta > 0 and saved:
            await self._maybe_prompt_complete(
                interaction, media, saved, prior_status
            )

    async def _mutate(self, interaction, media, field, value):
        """Run a quick edit at click time, patch local state, re-render in place.

        Returns the saved ``SaveMediaListEntry`` dict on success (so the +1 path
        can inspect the new progress/status), or ``None`` on any early exit.
        """

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        # Token at click time, like every other mutating AniList surface.
        token = await self.cog._get_token(self.author_id)
        if not token:
            await interactions.reply(
                interaction, _("Link your account first with `/anilist login`.")
            )
            return None
        try:
            saved = await self.cog._save_entry(token, media, field, value)
        except Exception:
            log.exception("AniList collection mutation failed")
            await interactions.notify_failure(interaction)
            return None
        if not saved:
            await interactions.reply(
                interaction, _("Could not update that entry.")
            )
            return None
        self._patch_entry(media.get("id"), saved)
        self._build()
        await self._rerender(interaction)
        return saved

    async def _maybe_prompt_complete(self, interaction, media, saved, prior_status):
        """Offer to complete when a +1 reached the finale without auto-completing.

        Same rule as the media editor's +1: the total is known and now reached,
        the entry was not already COMPLETED, and AniList did not auto-flip the
        status in the save response. The one-shot button runs the dashboard's own
        complete seam and refreshes the card in place.
        """

        total = _progress_max(media)
        if not total or saved.get("progress") != total:
            return
        if prior_status == "COMPLETED" or saved.get("status") == "COMPLETED":
            return

        async def _confirm(prompt_interaction):
            await self._complete_from_prompt(prompt_interaction, media)

        view = CompletePromptView(
            self.author_id, _confirm, label=_("Mark completed")
        )
        view.message = await interaction.followup.send(
            _("That was the last {unit} - mark **{title}** as completed?").format(
                unit=_media_unit(media), title=_media_title(media)
            ),
            view=view,
            ephemeral=True,
        )

    async def _complete_from_prompt(self, interaction, media):
        """Run the complete seam from the ephemeral prompt, refresh the dashboard.

        The prompt interaction has already been consumed (its button disabled
        itself in place), so this saves via follow-ups and edits the DASHBOARD
        message directly rather than the ephemeral prompt.
        """

        token = await self.cog._get_token(self.author_id)
        if not token:
            return await interactions.reply(
                interaction, _("Link your account first with `/anilist login`.")
            )
        try:
            saved = await self.cog._save_entry(token, media, "complete", None)
        except Exception:
            log.exception("AniList collection completion prompt failed")
            return await interactions.notify_failure(interaction)
        if not saved:
            return await interactions.reply(
                interaction, _("Could not update that entry.")
            )
        self._patch_entry(media.get("id"), saved)
        self._build()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _patch_entry(self, media_id, saved):
        """Fold a SaveMediaListEntry response into the cached entry in place."""

        entry = self._by_id.get(media_id)
        if entry is None:
            return
        for field in ("progress", "score", "status"):
            if saved.get(field) is not None:
                entry[field] = saved.get(field)

    async def _open_edit(self, interaction, media):
        """Open the media editor's existing pre-filled modal (reused verbatim).

        The modal resolves the token lazily at submit (token at click time), so
        none is parked on it here; it carries its own ephemeral confirmation.
        """

        try:
            entry = self._by_id.get(media.get("id"))
            await interaction.response.send_modal(
                EditEntryModal(
                    self.cog, media, entry=entry, score_format=self.score_format
                )
            )
        except Exception:
            log.exception("AniList collection edit launch failed")
            await interactions.notify_failure(interaction)


class CollectionMixin:
    """Cog mixin: build the collection dashboard for the command and the hub."""

    async def _fetch_collection(self, token, anilist_user_id, media_type, status):
        """Fetch every entry for a (user, type, status) as one page of state.

        ``media_type`` is ``"anime"``/``"manga"``; ``status`` an already-parsed
        MediaListStatus. Returns the raw entry dicts (each with its media); the
        view dedups/sorts them. The token is sent per-viewer and never logged.
        """

        data = await self._graphql(
            COLLECTION_QUERY,
            {
                "userId": anilist_user_id,
                "type": media_type.upper(),
                "status": status,
            },
            token=token,
        )
        collection = (
            ((data or {}).get("data") or {}).get("MediaListCollection") or {}
        )
        entries = []
        for lst in collection.get("lists") or []:
            for entry in lst.get("entries") or []:
                entries.append(entry)
        return entries

    async def _collection_payload(self, user_id, media_type, status):
        """Build the invoker's collection dashboard view.

        Returns ``(error, view)``: exactly one is set. ``error`` is a localised
        string (missing link / unreachable account); ``view`` is a ready
        :class:`CollectionView` otherwise. Shared by the ``list`` command and the
        hub's My list button.
        """

        token = await self._get_token(user_id)
        if not token:
            return _("Link your account first with `/anilist login`."), None
        viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
        user = ((viewer or {}).get("data") or {}).get("Viewer")
        if not user:
            return _("Could not reach your AniList account."), None
        entries = await self._fetch_collection(
            token, user["id"], media_type, status
        )
        score_format = await self._get_score_format(user_id)
        view = CollectionView(
            self,
            user_id,
            user["id"],
            media_type,
            status,
            entries,
            score_format=score_format,
        )
        return None, view
