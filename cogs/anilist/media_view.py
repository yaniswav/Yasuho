"""The AniList READ path: search results, the media card, and season browsing.

The surface a member LOOKS at, as opposed to the forms that change an entry in
:mod:`cogs.anilist.edit_forms`:

* :class:`ResultView` / :class:`ResultSelect` - pick the right title out of a
  multi-hit search;
* :class:`MediaView` - the media card itself, the package's main read surface;
* :class:`SeasonView` - step forward and back through airing seasons;
* :class:`StatusSelect` - the one-tap list-status picker shown to a logged-in
  viewer, and :class:`CompletePromptView`, the one-shot nudge offered after a +1
  lands on the final episode/chapter.

The last two are the seam where reading turns into writing: they hand off to the
cog's ``_apply_edit`` and to :class:`~cogs.anilist.edit_forms.EditEntryModal`,
which is why the import runs media_view -> edit_forms and never the reverse.
"""

import logging

import discord

from .edit_forms import EditEntryModal, _deny_if_throttled
from .helpers import (
    DEFAULT_SCORE_FORMAT,
    _clean_description,
    _format_fuzzy_date,
    _format_ranking,
    _media_colour,
    _media_title,
    _media_unit,
    _progress_max,
    _step_season,
    render_score,
)
from .queries import MEDIA_QUERY, PAGE_QUERY
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Interactive components (discord.ui)
# ----------------------------------------------------------------------
class ResultSelect(discord.ui.Select):
    """Dropdown of search candidates that expands into a full MediaView."""

    def __init__(self, cog, results, author_id, media_type):
        self.cog = cog
        self.author_id = author_id
        self.media_type = media_type

        options = []
        for media in results[:25]:
            title = _media_title(media)
            fmt = media.get("format") or "?"
            year = media.get("seasonYear") or "?"
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    description=f"{fmt} - {year}"[:100],
                    value=str(media.get("id")),
                )
            )

        super().__init__(placeholder=_("Pick a title..."), options=options)

    async def callback(self, interaction):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            # Remember the menu we came from so the MediaView can offer "Back".
            parent_view = self.view
            parent_content = (
                interaction.message.content if interaction.message else None
            )

            await interaction.response.defer()
            data = await self.cog._graphql(
                MEDIA_QUERY, {"id": int(self.values[0])}
            )
            media = ((data or {}).get("data") or {}).get("Media")
            if not media:
                return await interaction.followup.send(
                    _("Could not load that title."), ephemeral=True
                )

            token = await self.cog._get_token(self.author_id)
            view = MediaView(
                self.cog,
                media,
                self.author_id,
                token=token,
                parent_view=parent_view,
                parent_content=parent_content,
            )
            view.message = await interaction.edit_original_response(
                content=None, embed=view.overview_embed(), view=view
            )
        except Exception:
            log.exception("AniList result select failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong loading that title."), ephemeral=True
                )
            except Exception:
                pass


class ResultView(AuthorView):
    """Author-restricted wrapper around a :class:`ResultSelect`."""

    def __init__(self, cog, results, author_id, media_type, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(ResultSelect(cog, results, author_id, media_type))


class SeasonView(AuthorView):
    """Seasonal browser: a title picker plus previous/next season navigation."""

    def __init__(self, cog, results, author_id, season, year, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.season = season
        self.year = year
        self.add_item(ResultSelect(cog, results, author_id, "ANIME"))

    async def _change_season(self, interaction, *, forward):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            await interaction.response.defer()
            season, year = _step_season(self.season, self.year, forward=forward)
            data = await self.cog._graphql(
                PAGE_QUERY,
                {
                    "sort": ["POPULARITY_DESC"],
                    "type": "ANIME",
                    "season": season,
                    "seasonYear": year,
                },
            )
            media = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []
            if not media:
                return await interaction.followup.send(
                    _("No anime found for {season} {year}.").format(
                        season=season.title(), year=year
                    ),
                    ephemeral=True,
                )

            view = SeasonView(self.cog, media, self.author_id, season, year)
            view.message = await interaction.edit_original_response(
                content=_(
                    "**{season} {year} anime** - pick one for details:"
                ).format(season=season.title(), year=year),
                view=view,
            )
        except Exception:
            log.exception("AniList season navigation failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong loading that season."), ephemeral=True
                )
            except Exception:
                pass

    @discord.ui.button(
        label="◀ Previous season", style=discord.ButtonStyle.secondary, row=1
    )
    async def previous_season(self, interaction, button):
        await self._change_season(interaction, forward=False)

    @discord.ui.button(
        label="Next season ▶", style=discord.ButtonStyle.secondary, row=1
    )
    async def next_season(self, interaction, button):
        await self._change_season(interaction, forward=True)


class StatusSelect(discord.ui.Select):
    """One-tap list-status picker for the authenticated viewer (logged-in)."""

    def __init__(self, cog, media, author_id):
        self.cog = cog
        self.media = media
        self.author_id = author_id

        watching = _("Reading") if _media_unit(media) == "chapter" else _("Watching")
        options = [
            discord.SelectOption(label=watching, value="CURRENT", emoji="▶️"),
            discord.SelectOption(label=_("Completed"), value="COMPLETED", emoji="✅"),
            discord.SelectOption(label=_("Planning"), value="PLANNING", emoji="📝"),
            discord.SelectOption(label=_("Paused"), value="PAUSED", emoji="⏸️"),
            discord.SelectOption(label=_("Dropped"), value="DROPPED", emoji="🗑️"),
            discord.SelectOption(label=_("Repeating"), value="REPEATING", emoji="🔁"),
        ]
        super().__init__(
            placeholder=_("Set status..."),
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            await interaction.response.defer()
            await self.cog._apply_edit(
                interaction, self.author_id, self.media, "status", self.values[0]
            )
            # Re-render so the dropdown resets instead of sticking on the choice.
            try:
                await interaction.edit_original_response(view=self.view)
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("AniList status select failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong updating that entry."), ephemeral=True
                )
            except Exception:
                pass


class CompletePromptView(AuthorView):
    """One-shot ephemeral prompt to mark an entry completed after +1 hits the end.

    Author-gated and single-use: the button runs the supplied ``on_confirm``
    coroutine once, then greys itself out; a short timeout disables it if
    ignored. ``on_confirm`` owns the actual save and its acknowledgement - this
    view only consumes the click to disable the button in place, so there is
    exactly one response on the prompt interaction.
    """

    def __init__(self, author_id, on_confirm, *, label, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self._on_confirm = on_confirm
        button = discord.ui.Button(
            label=label, style=discord.ButtonStyle.success, emoji="✅"
        )
        button.callback = self._confirm
        self.add_item(button)

    async def _confirm(self, interaction):
        try:
            for child in self.children:
                child.disabled = True
            self.stop()
            try:
                await interaction.response.edit_message(view=self)
            except discord.HTTPException:
                pass
            await self._on_confirm(interaction)
        except Exception:
            log.exception("AniList completion prompt failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong updating that entry."), ephemeral=True
                )
            except Exception:
                pass


class MediaView(AuthorView):
    """Tabbed view over a full media object with optional list actions."""

    def __init__(
        self,
        cog,
        media,
        author_id,
        token=None,
        parent_view=None,
        parent_embed=None,
        parent_content=None,
        timeout=180,
    ):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.media = media
        self.token = token
        self.parent_view = parent_view
        self.parent_embed = parent_embed
        self.parent_content = parent_content

        # Logged-in controls: a status dropdown (row 1) and action buttons
        # (row 2). The dropdown is added dynamically; the row-2 buttons are
        # declarative and stripped for logged-out users so no empty row shows.
        if self.token is None:
            for child in list(self.children):
                if getattr(child, "row", None) in (1, 2):
                    self.remove_item(child)
        else:
            self.add_item(StatusSelect(self.cog, self.media, self.author_id))

        # The "Back" button (row 3) only makes sense when we came from a menu.
        if self.parent_view is None:
            for child in list(self.children):
                if getattr(child, "row", None) == 3:
                    self.remove_item(child)

    # -- embed builders -------------------------------------------------
    def _base_embed(self):
        media = self.media
        embed = discord.Embed(colour=_media_colour(media), url=media.get("siteUrl"))

        cover = media.get("coverImage") or {}
        if cover.get("large"):
            embed.set_thumbnail(url=cover["large"])

        banner = media.get("bannerImage")
        if banner:
            embed.set_image(url=banner)

        footer = []
        genres = media.get("genres") or []
        if genres:
            footer.append(" • ".join(genres[:5]))
        popularity = media.get("popularity")
        if popularity is not None:
            footer.append(_("{popularity} in lists").format(popularity=popularity))
        if footer:
            embed.set_footer(text=" | ".join(footer))

        return embed

    def overview_embed(self):
        media = self.media
        embed = self._base_embed()
        embed.title = _media_title(media)
        embed.description = _clean_description(media.get("description"))

        if media.get("format"):
            embed.add_field(name=_("Format"), value=media["format"], inline=True)
        if media.get("episodes"):
            embed.add_field(
                name=_("Episodes"), value=str(media["episodes"]), inline=True
            )
        elif media.get("chapters"):
            embed.add_field(
                name=_("Chapters"), value=str(media["chapters"]), inline=True
            )

        score = media.get("averageScore")
        if score is not None:
            embed.add_field(name=_("Score"), value=f"{score}/100", inline=True)

        if media.get("status"):
            embed.add_field(name=_("Status"), value=media["status"], inline=True)

        studios = ((media.get("studios") or {}).get("nodes")) or []
        names = [s.get("name") for s in studios if s.get("name")]
        if names:
            embed.add_field(
                name=_("Studio"), value=", ".join(names[:3]), inline=True
            )

        season = media.get("season")
        year = media.get("seasonYear")
        if season and year:
            embed.add_field(
                name=_("Season"), value=f"{season.title()} {year}", inline=True
            )
        elif year:
            embed.add_field(name=_("Year"), value=str(year), inline=True)

        return embed

    def characters_embed(self):
        embed = self._base_embed()
        embed.title = _("{title} - Characters").format(title=_media_title(self.media))

        edges = ((self.media.get("characters") or {}).get("edges")) or []
        lines = []
        for edge in edges[:12]:
            node = edge.get("node") or {}
            name = (node.get("name") or {}).get("full")
            if not name:
                continue
            role = edge.get("role")
            if role:
                lines.append(f"**{role.title()}** - {name}")
            else:
                lines.append(name)

        embed.description = (
            "\n".join(lines) if lines else _("No character data.")
        )
        return embed

    def relations_embed(self):
        embed = self._base_embed()
        embed.title = _("{title} - Relations").format(title=_media_title(self.media))

        edges = ((self.media.get("relations") or {}).get("edges")) or []
        lines = []
        for edge in edges[:12]:
            node = edge.get("node") or {}
            title = (node.get("title") or {}).get("romaji")
            if not title:
                continue
            rel = edge.get("relationType")
            label = rel.replace("_", " ").title() if rel else _("Related")
            fmt = node.get("format")
            suffix = f" ({fmt})" if fmt else ""
            lines.append(f"**{label}:** {title}{suffix}")

        embed.description = "\n".join(lines) if lines else _("No relations.")
        return embed

    def recommendations_embed(self):
        embed = self._base_embed()
        embed.title = _("{title} - Recommendations").format(
            title=_media_title(self.media)
        )

        nodes = ((self.media.get("recommendations") or {}).get("nodes")) or []
        lines = []
        for node in nodes[:10]:
            rec = node.get("mediaRecommendation") or {}
            title = (rec.get("title") or {}).get("romaji")
            if not title:
                continue
            fmt = rec.get("format")
            suffix = f" ({fmt})" if fmt else ""
            lines.append(f"- {title}{suffix}")

        embed.description = (
            "\n".join(lines) if lines else _("No recommendations.")
        )
        return embed

    def _your_stats_value(
        self, viewer_entry, logged_in, score_format=DEFAULT_SCORE_FORMAT
    ):
        """Build the "Your stats" field text for the authenticated viewer."""

        if not logged_in:
            return _(
                "🔗 Link your AniList with `/anilist login` to see your "
                "personal stats."
            )
        if not viewer_entry:
            return _("Not on your list yet.")

        watch_word = (
            _("Reading") if _media_unit(self.media) == "chapter" else _("Watching")
        )
        labels = {
            "CURRENT": watch_word,
            "PLANNING": _("Planning"),
            "COMPLETED": _("Completed"),
            "DROPPED": _("Dropped"),
            "PAUSED": _("Paused"),
            "REPEATING": _("Repeating"),
        }

        lines = [_("On your list ✓")]
        status = viewer_entry.get("status")
        if status:
            lines.append(
                _("Status: {status}").format(
                    status=labels.get(status, str(status).title())
                )
            )

        score = render_score(viewer_entry.get("score"), score_format)
        if score:
            lines.append(_("Your score: {score}").format(score=score))

        progress = viewer_entry.get("progress")
        if progress is not None:
            total = (
                self.media.get("chapters") or self.media.get("episodes") or "?"
            )
            unit = _media_unit(self.media, plural=True)
            lines.append(
                _("Progress: {progress}/{total} {unit}").format(
                    progress=progress, total=total, unit=unit
                )
            )

        repeat = viewer_entry.get("repeat")
        if repeat:
            lines.append(_("Repeats: {repeat}").format(repeat=repeat))

        started = _format_fuzzy_date(viewer_entry.get("startedAt"))
        if started:
            lines.append(_("Started: {started}").format(started=started))
        completed = _format_fuzzy_date(viewer_entry.get("completedAt"))
        if completed:
            lines.append(_("Completed: {completed}").format(completed=completed))

        return "\n".join(lines)

    def stats_embed(
        self, viewer_entry=None, logged_in=False, score_format=DEFAULT_SCORE_FORMAT
    ):
        media = self.media
        embed = self._base_embed()
        embed.title = _("{title} - Stats").format(title=_media_title(media))

        your_value = self._your_stats_value(viewer_entry, logged_in, score_format)

        mean = media.get("meanScore")
        average = media.get("averageScore")
        popularity = media.get("popularity")
        favourites = media.get("favourites")

        stats = media.get("stats") or {}
        score_dist = stats.get("scoreDistribution") or []
        status_dist = stats.get("statusDistribution") or []
        rankings = media.get("rankings") or []

        # Some media (e.g. unreleased titles) carry no usable stats at all.
        if not any(
            (
                mean is not None,
                average is not None,
                popularity is not None,
                favourites is not None,
                score_dist,
                status_dist,
                rankings,
            )
        ):
            embed.description = _("No stats available.")
            embed.add_field(
                name=_("👤 Your stats"), value=your_value, inline=False
            )
            return embed

        if mean is not None:
            embed.add_field(name=_("Mean score"), value=f"{mean}/100", inline=True)
        if average is not None:
            embed.add_field(
                name=_("Average score"), value=f"{average}/100", inline=True
            )
        if popularity is not None:
            embed.add_field(
                name=_("Popularity"),
                value=_("{popularity} followers").format(
                    popularity=f"{popularity:,}"
                ),
                inline=True,
            )
        if favourites is not None:
            embed.add_field(
                name=_("Favourites"), value=f"{favourites:,}", inline=True
            )

        # Score distribution as a compact monospace bar chart.
        valid_scores = [
            d for d in score_dist if d.get("score") is not None
        ]
        if valid_scores:
            max_amount = max((d.get("amount") or 0) for d in valid_scores) or 1
            lines = []
            for d in sorted(valid_scores, key=lambda x: x.get("score") or 0):
                amount = d.get("amount") or 0
                filled = round((amount / max_amount) * 12)
                if amount and not filled:
                    filled = 1
                bar = "█" * filled
                lines.append(f"{str(d.get('score')).rjust(3)} │ {bar} {amount}")
            embed.add_field(
                name=_("Score distribution"),
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )

        # Status distribution with friendly labels.
        if status_dist:
            labels = {
                "CURRENT": _("Watching"),
                "PLANNING": _("Planning"),
                "COMPLETED": _("Completed"),
                "DROPPED": _("Dropped"),
                "PAUSED": _("Paused"),
                "REPEATING": _("Repeating"),
            }
            order = [
                "CURRENT",
                "PLANNING",
                "COMPLETED",
                "DROPPED",
                "PAUSED",
                "REPEATING",
            ]
            by_status = {
                d.get("status"): (d.get("amount") or 0) for d in status_dist
            }
            lines = []
            for status in order:
                if status in by_status:
                    lines.append(
                        _("{label}: {amount}").format(
                            label=labels[status],
                            amount=f"{by_status[status]:,}",
                        )
                    )
            for status, amount in by_status.items():
                if status not in order:
                    lines.append(
                        _("{label}: {amount}").format(
                            label=str(status).title(), amount=f"{amount:,}"
                        )
                    )
            if lines:
                embed.add_field(
                    name=_("Status distribution"),
                    value="\n".join(lines),
                    inline=False,
                )

        # A few meaningful rankings: all-time placements plus the best
        # contextual (seasonal/yearly) ones.
        if rankings:
            all_time = [r for r in rankings if r.get("allTime")]
            contextual = sorted(
                (r for r in rankings if not r.get("allTime")),
                key=lambda r: r.get("rank") or 9999,
            )
            lines = []
            for ranking in all_time + contextual[:2]:
                formatted = _format_ranking(ranking)
                if formatted:
                    lines.append(formatted)
            if lines:
                embed.add_field(
                    name=_("Rankings"),
                    value="\n".join(lines[:5]),
                    inline=False,
                )

        embed.add_field(name=_("👤 Your stats"), value=your_value, inline=False)
        return embed

    async def _show(self, interaction, builder):
        try:
            embed = builder()
        except Exception:
            log.exception("AniList media view section failed")
            return await interaction.response.send_message(
                _("Could not render that section."), ephemeral=True
            )
        await interaction.response.edit_message(embed=embed, view=self)

    # -- section buttons (row 0) ---------------------------------------
    @discord.ui.button(label="Overview", style=discord.ButtonStyle.primary, row=0)
    async def overview_button(self, interaction, button):
        await self._show(interaction, self.overview_embed)

    @discord.ui.button(label="Characters", style=discord.ButtonStyle.secondary, row=0)
    async def characters_button(self, interaction, button):
        await self._show(interaction, self.characters_embed)

    @discord.ui.button(label="Relations", style=discord.ButtonStyle.secondary, row=0)
    async def relations_button(self, interaction, button):
        await self._show(interaction, self.relations_embed)

    @discord.ui.button(
        label="Recommendations", style=discord.ButtonStyle.secondary, row=0
    )
    async def recommendations_button(self, interaction, button):
        await self._show(interaction, self.recommendations_embed)

    @discord.ui.button(label="📊 Stats", style=discord.ButtonStyle.secondary, row=0)
    async def stats_button(self, interaction, button):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            viewer_entry, logged_in = await self.cog._viewer_entry(
                interaction.user.id, self.media.get("id")
            )
            score_format = (
                await self.cog._get_score_format(interaction.user.id)
                if logged_in
                else DEFAULT_SCORE_FORMAT
            )
            embed = self.stats_embed(
                viewer_entry=viewer_entry,
                logged_in=logged_in,
                score_format=score_format,
            )
        except Exception:
            log.exception("AniList stats section failed")
            return await interaction.response.send_message(
                _("Could not render that section."), ephemeral=True
            )
        await interaction.response.edit_message(embed=embed, view=self)

    # -- back to the originating menu (row 3, only when we have a parent) --
    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=3)
    async def back_button(self, interaction, button):
        try:
            # Re-link the restored menu to this message so it stays interactive.
            self.parent_view.message = interaction.message
            await interaction.response.edit_message(
                content=self.parent_content,
                embed=self.parent_embed,
                view=self.parent_view,
            )
            # Stop our own timeout so it can't later clobber the restored menu.
            self.stop()
        except Exception:
            log.exception("AniList back navigation failed")
            try:
                await interaction.response.send_message(
                    _("Could not go back."), ephemeral=True
                )
            except Exception:
                pass

    # -- quick actions (row 2, linked users only) ----------------------
    @discord.ui.button(label="-1", style=discord.ButtonStyle.secondary, row=2)
    async def decrement_button(self, interaction, button):
        await self._step_progress(interaction, -1)

    @discord.ui.button(label="+1", style=discord.ButtonStyle.success, row=2)
    async def increment_button(self, interaction, button):
        await self._step_progress(interaction, +1)

    @discord.ui.button(label="✅ Complete", style=discord.ButtonStyle.success, row=2)
    async def complete_button(self, interaction, button):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            await interaction.response.defer()
            await self.cog._apply_edit(
                interaction, self.author_id, self.media, "complete", None
            )
        except Exception:
            log.exception("AniList complete action failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong updating that entry."), ephemeral=True
                )
            except Exception:
                pass

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=2)
    async def edit_button(self, interaction, button):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            # Pre-load the viewer's current entry so the modal opens pre-filled.
            viewer_entry, _logged_in = await self.cog._viewer_entry(
                interaction.user.id, self.media.get("id")
            )
            score_format = await self.cog._get_score_format(interaction.user.id)
            await interaction.response.send_modal(
                EditEntryModal(
                    self.cog,
                    self.media,
                    self.token,
                    entry=viewer_entry,
                    score_format=score_format,
                )
            )
        except Exception:
            log.exception("AniList edit modal launch failed")
            try:
                await interaction.response.send_message(
                    _("Could not open the edit form."), ephemeral=True
                )
            except Exception:
                pass

    async def _step_progress(self, interaction, delta):
        """Bump the viewer's progress by ``delta``, clamped to [0, max]."""

        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            await interaction.response.defer()
            entry, _logged_in = await self.cog._viewer_entry(
                interaction.user.id, self.media.get("id")
            )
            prior_status = (entry or {}).get("status")
            current = (entry or {}).get("progress") or 0
            new = current + delta
            if new < 0:
                new = 0
            maximum = _progress_max(self.media)
            if maximum is not None and new > maximum:
                new = maximum
            saved = await self.cog._apply_edit(
                interaction, self.author_id, self.media, "progress", new
            )
            if delta > 0 and saved:
                await self._maybe_prompt_complete(interaction, saved, prior_status)
        except Exception:
            log.exception("AniList progress step failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong updating progress."), ephemeral=True
                )
            except Exception:
                pass

    async def _maybe_prompt_complete(self, interaction, saved, prior_status):
        """Offer to complete when a +1 reached the finale without auto-completing.

        Sent as one ephemeral follow-up after the normal confirmation, with a
        single one-shot button that runs the same "complete" seam as the
        Complete button. Skipped when the total is unknown, when the new progress
        has not reached it, when the entry was already COMPLETED, or when AniList
        already flipped the status to COMPLETED in the save response.
        """

        total = _progress_max(self.media)
        if not total or saved.get("progress") != total:
            return
        if prior_status == "COMPLETED" or saved.get("status") == "COMPLETED":
            return

        async def _confirm(prompt_interaction):
            await self.cog._apply_edit(
                prompt_interaction, self.author_id, self.media, "complete", None
            )

        view = CompletePromptView(
            self.author_id, _confirm, label=_("Mark completed")
        )
        view.message = await interaction.followup.send(
            _("That was the last {unit} - mark **{title}** as completed?").format(
                unit=_media_unit(self.media), title=_media_title(self.media)
            ),
            view=view,
            ephemeral=True,
        )
