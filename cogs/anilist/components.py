import logging

import discord

from .helpers import (
    _clean_description,
    _format_fuzzy_date,
    _format_ranking,
    _format_score,
    _media_colour,
    _media_title,
    _media_unit,
    _progress_max,
    _status_label,
    _step_season,
)
from .queries import MEDIA_QUERY, PAGE_QUERY, SAVE_ENTRY_QUERY
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

        super().__init__(placeholder="Pick a title...", options=options)

    async def callback(self, interaction):
        try:
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
                    "Could not load that title.", ephemeral=True
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
                    "Something went wrong loading that title.", ephemeral=True
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
                    f"No anime found for {season.title()} {year}.", ephemeral=True
                )

            view = SeasonView(self.cog, media, self.author_id, season, year)
            view.message = await interaction.edit_original_response(
                content=f"**{season.title()} {year} anime** - pick one for details:",
                view=view,
            )
        except Exception:
            log.exception("AniList season navigation failed")
            try:
                await interaction.followup.send(
                    "Something went wrong loading that season.", ephemeral=True
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


class EditSelect(discord.ui.Select):
    """Disambiguation dropdown: choose which media a text edit targets."""

    def __init__(self, cog, candidates, author_id, field, value):
        self.cog = cog
        self.author_id = author_id
        self.field = field
        self.value = value
        self.candidates = {str(m.get("id")): m for m in candidates}

        options = []
        for media in candidates[:25]:
            mtype = media.get("type") or "?"
            romaji = (media.get("title") or {}).get("romaji") or "Unknown"
            year = media.get("seasonYear") or "?"
            label = f"[{mtype}] {romaji} ({year})"
            options.append(
                discord.SelectOption(label=label[:100], value=str(media.get("id")))
            )

        super().__init__(placeholder="Pick the right title...", options=options)

    async def callback(self, interaction):
        try:
            media = self.candidates.get(self.values[0])
            if not media:
                return await interaction.response.send_message(
                    "Could not load that title.", ephemeral=True
                )

            for child in self.view.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"Updating **{_media_title(media)}**...", view=self.view
            )
            await self.cog._apply_edit(
                interaction, self.author_id, media, self.field, self.value
            )
        except Exception:
            log.exception("AniList edit select failed")
            try:
                await interaction.followup.send(
                    "Something went wrong updating that entry.", ephemeral=True
                )
            except Exception:
                pass


class EditSelectView(AuthorView):
    """Author-restricted wrapper around an :class:`EditSelect`."""

    def __init__(self, cog, candidates, author_id, field, value, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(EditSelect(cog, candidates, author_id, field, value))


class EditEntryModal(discord.ui.Modal):
    """Edit status (a dropdown) + progress/score (fields), pre-filled from the entry."""

    def __init__(self, cog, media, token=None, entry=None):
        super().__init__(title=f"Edit: {_media_title(media)}"[:45])
        self.cog = cog
        self.media = media
        self.token = token
        entry = entry or {}

        unit = _media_unit(media)
        watching = "Reading" if unit == "chapter" else "Watching"
        current_status = entry.get("status")
        choices = [
            ("CURRENT", watching),
            ("PLANNING", "Planning"),
            ("COMPLETED", "Completed"),
            ("REPEATING", "Repeating"),
            ("PAUSED", "Paused"),
            ("DROPPED", "Dropped"),
        ]
        # Status is a real dropdown (Components V2 select-in-modal), the current
        # value pre-selected; min_values=0 so it can be left unchanged.
        self.status_select = discord.ui.Select(
            placeholder="Keep current status",
            min_values=0,
            max_values=1,
            required=False,
            options=[
                discord.SelectOption(
                    label=label, value=value, default=(value == current_status)
                )
                for value, label in choices
            ],
        )
        self.add_item(discord.ui.Label(text="Status", component=self.status_select))

        current_progress = entry.get("progress")
        self.progress_input = discord.ui.TextInput(
            required=False,
            style=discord.TextStyle.short,
            max_length=6,
            default=str(current_progress) if current_progress is not None else None,
        )
        self.add_item(
            discord.ui.Label(
                text=f"Progress ({unit}s)", component=self.progress_input
            )
        )

        score = _format_score(entry.get("score"))
        self.score_input = discord.ui.TextInput(
            required=False,
            max_length=6,
            default=score if score and score != "0" else None,
        )
        self.add_item(discord.ui.Label(text="Score", component=self.score_input))

    async def on_submit(self, interaction):
        variables = {"mediaId": self.media.get("id")}
        status_values = self.status_select.values
        progress_raw = (self.progress_input.value or "").strip()
        score_raw = (self.score_input.value or "").strip()

        try:
            if progress_raw:
                variables["progress"] = int(progress_raw)
            if score_raw:
                variables["score"] = float(score_raw)
        except ValueError:
            return await interaction.response.send_message(
                "Progress must be a whole number and score a number.",
                ephemeral=True,
            )

        if status_values:
            variables["status"] = status_values[0]

        if (
            "progress" not in variables
            and "score" not in variables
            and "status" not in variables
        ):
            return await interaction.response.send_message(
                "Nothing to update - pick a status or fill in progress/score.",
                ephemeral=True,
            )

        # The wizard opens this modal without a token; resolve it lazily here
        # (never logged). Direct callers may still pass one in.
        token = self.token
        if token is None:
            token = await self.cog._get_token(interaction.user.id)
        if not token:
            return await interaction.response.send_message(
                "Link your account first with `/anilist login`.", ephemeral=True
            )

        try:
            data = await self.cog._graphql(
                SAVE_ENTRY_QUERY, variables, token=token
            )
            entry = ((data or {}).get("data") or {}).get("SaveMediaListEntry")
            if not entry:
                return await interaction.response.send_message(
                    "Could not update that entry.", ephemeral=True
                )

            name = (
                (entry.get("media") or {}).get("title") or {}
            ).get("romaji") or _media_title(self.media)
            unit = _media_unit(self.media)
            await interaction.response.send_message(
                f"Updated **{name}** - {unit} {entry.get('progress')}, "
                f"score {entry.get('score')}.",
                ephemeral=True,
            )
        except Exception:
            log.exception("AniList edit modal failed")
            try:
                await interaction.response.send_message(
                    "Something went wrong updating that entry.", ephemeral=True
                )
            except Exception:
                pass


class TypeView(AuthorView):
    """Update wizard, step 1: pick anime vs manga among mixed candidates.

    Only the buttons for types actually present in ``candidates`` are shown.
    """

    def __init__(self, cog, candidates, author_id, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.candidates = candidates

        types_present = {m.get("type") for m in candidates if m.get("type")}
        if "ANIME" not in types_present:
            self.remove_item(self.anime_button)
        if "MANGA" not in types_present:
            self.remove_item(self.manga_button)

    async def _choose_type(self, interaction, media_type):
        try:
            subset = [
                m for m in self.candidates if m.get("type") == media_type
            ]
            if not subset:
                return await interaction.response.send_message(
                    "No matching titles of that type.", ephemeral=True
                )

            view = SeasonSelectView(self.cog, subset, self.author_id, media_type)
            await interaction.response.edit_message(
                content="Pick the exact title to update:", view=view
            )
            view.message = interaction.message
            self.stop()
        except Exception:
            log.exception("AniList update type selection failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Something went wrong.", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "Something went wrong.", ephemeral=True
                    )
            except Exception:
                pass

    @discord.ui.button(label="📺 Anime", style=discord.ButtonStyle.primary)
    async def anime_button(self, interaction, button):
        await self._choose_type(interaction, "ANIME")

    @discord.ui.button(label="📖 Manga", style=discord.ButtonStyle.success)
    async def manga_button(self, interaction, button):
        await self._choose_type(interaction, "MANGA")


class SeasonSelect(discord.ui.Select):
    """Update wizard, step 2: pick the exact title; opens a pre-filled modal."""

    def __init__(self, cog, candidates, media_type):
        self.cog = cog
        self.media_type = media_type
        self.candidates = {str(m.get("id")): m for m in candidates}

        options = []
        for media in candidates[:25]:
            romaji = (media.get("title") or {}).get("romaji") or "Unknown"
            year = media.get("seasonYear") or "?"
            label = f"{romaji} ({year})"
            options.append(
                discord.SelectOption(label=label[:100], value=str(media.get("id")))
            )

        super().__init__(placeholder="Pick the exact title...", options=options)

    async def callback(self, interaction):
        try:
            media = self.candidates.get(self.values[0])
            if not media:
                return await interaction.response.send_message(
                    "Could not load that title.", ephemeral=True
                )

            # Fetch the viewer's current entry BEFORE send_modal (allowed) so
            # the form opens pre-filled with their existing status/score/progress.
            entry, _ = await self.cog._viewer_entry(
                interaction.user.id, media.get("id")
            )
            await interaction.response.send_modal(
                EditEntryModal(self.cog, media, entry=entry)
            )
        except Exception:
            log.exception("AniList update season select failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Something went wrong opening the editor.", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "Something went wrong opening the editor.", ephemeral=True
                    )
            except Exception:
                pass


class SeasonSelectView(AuthorView):
    """Author-restricted wrapper around a :class:`SeasonSelect`."""

    def __init__(self, cog, candidates, author_id, media_type, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(SeasonSelect(cog, candidates, media_type))


class OnListSelect(discord.ui.Select):
    """Update wizard: pick among the titles the user *already tracks*.

    Each option is labelled with its type/title and described with the user's
    current status and progress, so the choice is unambiguous.
    """

    def __init__(self, cog, candidates):
        self.cog = cog
        self.candidates = {str(m.get("id")): m for m in candidates}

        options = []
        for media in candidates[:25]:
            mtype = media.get("type") or "?"
            romaji = (media.get("title") or {}).get("romaji") or "Unknown"
            year = media.get("seasonYear") or "?"
            label = f"[{mtype}] {romaji} ({year})"

            entry = media.get("mediaListEntry") or {}
            parts = []
            status = entry.get("status")
            if status:
                parts.append(_status_label(status, media))
            progress = entry.get("progress")
            if progress is not None:
                total = _progress_max(media)
                unit = _media_unit(media, plural=True)
                parts.append(f"{progress}/{total if total else '?'} {unit}")
            description = ", ".join(parts) if parts else None

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=description[:100] if description else None,
                    value=str(media.get("id")),
                )
            )

        super().__init__(placeholder="Pick which one to update...", options=options)

    async def callback(self, interaction):
        try:
            media = self.candidates.get(self.values[0])
            if not media:
                return await interaction.response.send_message(
                    "Could not load that title.", ephemeral=True
                )

            # Fetch the canonical entry BEFORE send_modal (allowed) to pre-fill.
            entry, _ = await self.cog._viewer_entry(
                interaction.user.id, media.get("id")
            )
            await interaction.response.send_modal(
                EditEntryModal(self.cog, media, entry=entry)
            )
        except Exception:
            log.exception("AniList update on-list select failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Something went wrong opening the editor.", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "Something went wrong opening the editor.", ephemeral=True
                    )
            except Exception:
                pass


class OnListSelectView(AuthorView):
    """Author-restricted wrapper around an :class:`OnListSelect`."""

    def __init__(self, cog, candidates, author_id, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(OnListSelect(cog, candidates))


class StatusSelect(discord.ui.Select):
    """One-tap list-status picker for the authenticated viewer (logged-in)."""

    def __init__(self, cog, media, author_id):
        self.cog = cog
        self.media = media
        self.author_id = author_id

        watching = "Reading" if _media_unit(media) == "chapter" else "Watching"
        options = [
            discord.SelectOption(label=watching, value="CURRENT", emoji="▶️"),
            discord.SelectOption(label="Completed", value="COMPLETED", emoji="✅"),
            discord.SelectOption(label="Planning", value="PLANNING", emoji="📝"),
            discord.SelectOption(label="Paused", value="PAUSED", emoji="⏸️"),
            discord.SelectOption(label="Dropped", value="DROPPED", emoji="🗑️"),
            discord.SelectOption(label="Repeating", value="REPEATING", emoji="🔁"),
        ]
        super().__init__(
            placeholder="Set status...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction):
        try:
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
                    "Something went wrong updating that entry.", ephemeral=True
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
            footer.append(f"{popularity} in lists")
        if footer:
            embed.set_footer(text=" | ".join(footer))

        return embed

    def overview_embed(self):
        media = self.media
        embed = self._base_embed()
        embed.title = _media_title(media)
        embed.description = _clean_description(media.get("description"))

        if media.get("format"):
            embed.add_field(name="Format", value=media["format"], inline=True)
        if media.get("episodes"):
            embed.add_field(
                name="Episodes", value=str(media["episodes"]), inline=True
            )
        elif media.get("chapters"):
            embed.add_field(
                name="Chapters", value=str(media["chapters"]), inline=True
            )

        score = media.get("averageScore")
        if score is not None:
            embed.add_field(name="Score", value=f"{score}/100", inline=True)

        if media.get("status"):
            embed.add_field(name="Status", value=media["status"], inline=True)

        studios = ((media.get("studios") or {}).get("nodes")) or []
        names = [s.get("name") for s in studios if s.get("name")]
        if names:
            embed.add_field(name="Studio", value=", ".join(names[:3]), inline=True)

        season = media.get("season")
        year = media.get("seasonYear")
        if season and year:
            embed.add_field(
                name="Season", value=f"{season.title()} {year}", inline=True
            )
        elif year:
            embed.add_field(name="Year", value=str(year), inline=True)

        return embed

    def characters_embed(self):
        embed = self._base_embed()
        embed.title = f"{_media_title(self.media)} - Characters"

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

        embed.description = "\n".join(lines) if lines else "No character data."
        return embed

    def relations_embed(self):
        embed = self._base_embed()
        embed.title = f"{_media_title(self.media)} - Relations"

        edges = ((self.media.get("relations") or {}).get("edges")) or []
        lines = []
        for edge in edges[:12]:
            node = edge.get("node") or {}
            title = (node.get("title") or {}).get("romaji")
            if not title:
                continue
            rel = edge.get("relationType")
            label = rel.replace("_", " ").title() if rel else "Related"
            fmt = node.get("format")
            suffix = f" ({fmt})" if fmt else ""
            lines.append(f"**{label}:** {title}{suffix}")

        embed.description = "\n".join(lines) if lines else "No relations."
        return embed

    def recommendations_embed(self):
        embed = self._base_embed()
        embed.title = f"{_media_title(self.media)} - Recommendations"

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

        embed.description = "\n".join(lines) if lines else "No recommendations."
        return embed

    def _your_stats_value(self, viewer_entry, logged_in):
        """Build the "Your stats" field text for the authenticated viewer."""

        if not logged_in:
            return (
                "🔗 Link your AniList with `/anilist login` to see your "
                "personal stats."
            )
        if not viewer_entry:
            return "Not on your list yet."

        watch_word = (
            "Reading" if _media_unit(self.media) == "chapter" else "Watching"
        )
        labels = {
            "CURRENT": watch_word,
            "PLANNING": "Planning",
            "COMPLETED": "Completed",
            "DROPPED": "Dropped",
            "PAUSED": "Paused",
            "REPEATING": "Repeating",
        }

        lines = ["On your list ✓"]
        status = viewer_entry.get("status")
        if status:
            lines.append(f"Status: {labels.get(status, str(status).title())}")

        score = _format_score(viewer_entry.get("score"))
        if score and score != "0":
            lines.append(f"Your score: {score}")

        progress = viewer_entry.get("progress")
        if progress is not None:
            total = (
                self.media.get("chapters") or self.media.get("episodes") or "?"
            )
            unit = _media_unit(self.media, plural=True)
            lines.append(f"Progress: {progress}/{total} {unit}")

        repeat = viewer_entry.get("repeat")
        if repeat:
            lines.append(f"Repeats: {repeat}")

        started = _format_fuzzy_date(viewer_entry.get("startedAt"))
        if started:
            lines.append(f"Started: {started}")
        completed = _format_fuzzy_date(viewer_entry.get("completedAt"))
        if completed:
            lines.append(f"Completed: {completed}")

        return "\n".join(lines)

    def stats_embed(self, viewer_entry=None, logged_in=False):
        media = self.media
        embed = self._base_embed()
        embed.title = f"{_media_title(media)} - Stats"

        your_value = self._your_stats_value(viewer_entry, logged_in)

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
            embed.description = "No stats available."
            embed.add_field(name="👤 Your stats", value=your_value, inline=False)
            return embed

        if mean is not None:
            embed.add_field(name="Mean score", value=f"{mean}/100", inline=True)
        if average is not None:
            embed.add_field(
                name="Average score", value=f"{average}/100", inline=True
            )
        if popularity is not None:
            embed.add_field(
                name="Popularity", value=f"{popularity:,} followers", inline=True
            )
        if favourites is not None:
            embed.add_field(
                name="Favourites", value=f"{favourites:,}", inline=True
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
                name="Score distribution",
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )

        # Status distribution with friendly labels.
        if status_dist:
            labels = {
                "CURRENT": "Watching",
                "PLANNING": "Planning",
                "COMPLETED": "Completed",
                "DROPPED": "Dropped",
                "PAUSED": "Paused",
                "REPEATING": "Repeating",
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
                        f"{labels[status]}: {by_status[status]:,}"
                    )
            for status, amount in by_status.items():
                if status not in order:
                    lines.append(
                        f"{str(status).title()}: {amount:,}"
                    )
            if lines:
                embed.add_field(
                    name="Status distribution",
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
                    name="Rankings",
                    value="\n".join(lines[:5]),
                    inline=False,
                )

        embed.add_field(name="👤 Your stats", value=your_value, inline=False)
        return embed

    async def _show(self, interaction, builder):
        try:
            embed = builder()
        except Exception:
            log.exception("AniList media view section failed")
            return await interaction.response.send_message(
                "Could not render that section.", ephemeral=True
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
            viewer_entry, logged_in = await self.cog._viewer_entry(
                interaction.user.id, self.media.get("id")
            )
            embed = self.stats_embed(
                viewer_entry=viewer_entry, logged_in=logged_in
            )
        except Exception:
            log.exception("AniList stats section failed")
            return await interaction.response.send_message(
                "Could not render that section.", ephemeral=True
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
                    "Could not go back.", ephemeral=True
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
            await interaction.response.defer()
            await self.cog._apply_edit(
                interaction, self.author_id, self.media, "complete", None
            )
        except Exception:
            log.exception("AniList complete action failed")
            try:
                await interaction.followup.send(
                    "Something went wrong updating that entry.", ephemeral=True
                )
            except Exception:
                pass

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=2)
    async def edit_button(self, interaction, button):
        try:
            # Pre-load the viewer's current entry so the modal opens pre-filled.
            viewer_entry, _ = await self.cog._viewer_entry(
                interaction.user.id, self.media.get("id")
            )
            await interaction.response.send_modal(
                EditEntryModal(
                    self.cog, self.media, self.token, entry=viewer_entry
                )
            )
        except Exception:
            log.exception("AniList edit modal launch failed")
            try:
                await interaction.response.send_message(
                    "Could not open the edit form.", ephemeral=True
                )
            except Exception:
                pass

    async def _step_progress(self, interaction, delta):
        """Bump the viewer's progress by ``delta``, clamped to [0, max]."""

        try:
            await interaction.response.defer()
            entry, _ = await self.cog._viewer_entry(
                interaction.user.id, self.media.get("id")
            )
            current = (entry or {}).get("progress") or 0
            new = current + delta
            if new < 0:
                new = 0
            maximum = _progress_max(self.media)
            if maximum is not None and new > maximum:
                new = maximum
            await self.cog._apply_edit(
                interaction, self.author_id, self.media, "progress", new
            )
        except Exception:
            log.exception("AniList progress step failed")
            try:
                await interaction.followup.send(
                    "Something went wrong updating progress.", ephemeral=True
                )
            except Exception:
                pass


class LoginModal(discord.ui.Modal, title="Enter your AniList code"):
    """Collect the OAuth PIN and finish linking without ever echoing it."""

    code = discord.ui.TextInput(
        label="Code",
        placeholder="Paste the code AniList showed you",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, cog, author_id, login_view=None):
        super().__init__()
        self.cog = cog
        self.author_id = author_id
        self.login_view = login_view

    async def on_submit(self, interaction):
        # Defer first: the token exchange is a network round-trip that can exceed
        # the 3s interaction window, which would otherwise fail the modal submit.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            pass
        try:
            name = await self.cog._exchange_code(self.author_id, self.code.value)
            if name is None:
                return await interaction.followup.send(
                    "That code did not work, try `/anilist login` again.",
                    ephemeral=True,
                )
            await interaction.followup.send(
                f"Connected as {name}!", ephemeral=True
            )
            # Once linked, replace the prompt (and its authorize link) with a
            # confirmation and stop the view so nothing lingers in the DM.
            view = self.login_view
            if view is not None and view.message is not None:
                try:
                    await view.message.edit(
                        content=f"✅ Linked as **{name}**.", view=None
                    )
                except discord.HTTPException:
                    pass
                view.stop()
        except Exception:
            log.exception("AniList login modal failed")
            try:
                await interaction.followup.send(
                    "Something went wrong linking your account.", ephemeral=True
                )
            except Exception:
                pass


class LoginView(AuthorView):
    """Author-restricted view exposing a modal to enter the OAuth PIN."""

    def __init__(self, cog, author_id, timeout=300):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction, button):
        try:
            await interaction.response.send_modal(
                LoginModal(self.cog, self.author_id, login_view=self)
            )
        except Exception:
            log.exception("AniList login modal launch failed")
            try:
                await interaction.response.send_message(
                    "Could not open the code form.", ephemeral=True
                )
            except Exception:
                pass
