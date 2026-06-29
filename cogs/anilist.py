import datetime
import logging
import re

import aiohttp
import discord
from discord.ext import commands

from tools import crypto
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)

API_URL = "https://graphql.anilist.co"
TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
REDIRECT_URI = "https://anilist.co/api/v2/oauth/pin"

MEDIA_QUERY = """
query ($id: Int, $search: String, $type: MediaType) {
  Media(id: $id, search: $search, type: $type) {
    id
    idMal
    type
    title { romaji english native }
    format
    status
    episodes
    chapters
    duration
    averageScore
    meanScore
    popularity
    favourites
    genres
    siteUrl
    bannerImage
    coverImage { large color }
    description(asHtml: false)
    season
    seasonYear
    studios(isMain: true) { nodes { name } }
    trailer { site id }
    relations {
      edges {
        relationType
        node { id title { romaji } format type }
      }
    }
    characters(sort: ROLE, perPage: 12) {
      edges { role node { name { full } } }
    }
    recommendations(sort: RATING_DESC, perPage: 10) {
      nodes { mediaRecommendation { id title { romaji } format } }
    }
    rankings { rank type context allTime }
    stats {
      scoreDistribution { score amount }
      statusDistribution { status amount }
    }
  }
}
"""

# Lightweight search used to gather a handful of candidates for the picker.
CANDIDATE_QUERY = """
query ($search: String, $type: MediaType) {
  Page(perPage: 10) {
    media(search: $search, type: $type) {
      id
      title { romaji english }
      format
      seasonYear
    }
  }
}
"""

# Cross-type search used to disambiguate list edits (anime vs manga).
SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search) {
      id
      type
      format
      title { romaji english }
      episodes
      chapters
      seasonYear
    }
  }
}
"""

# Browse query for trending / popular / seasonal listings.
PAGE_QUERY = """
query ($sort: [MediaSort], $type: MediaType, $season: MediaSeason, $seasonYear: Int) {
  Page(perPage: 25) {
    media(sort: $sort, type: $type, season: $season, seasonYear: $seasonYear) {
      id
      title { romaji english }
      format
      averageScore
      episodes
      seasonYear
    }
  }
}
"""

USER_STATS_QUERY = """
query ($name: String) {
  User(name: $name) {
    name
    avatar { large }
    siteUrl
    statistics {
      anime {
        count
        meanScore
        minutesWatched
        episodesWatched
        genres(limit: 6, sort: COUNT_DESC) { genre count }
      }
      manga { count meanScore chaptersRead }
    }
  }
}
"""

CHARACTER_QUERY = """
query ($search: String) {
  Character(search: $search) {
    name { full native }
    image { large }
    description(asHtml: false)
    siteUrl
  }
}
"""

STUDIO_QUERY = """
query ($search: String) {
  Studio(search: $search) {
    name
    siteUrl
    media(sort: POPULARITY_DESC, perPage: 10) {
      nodes { title { romaji } }
    }
  }
}
"""

VIEWER_QUERY = """
query { Viewer { id name } }
"""

SAVE_ENTRY_QUERY = """
mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus, $score: Float) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status, score: $score) {
    id
    status
    progress
    score
    media { title { romaji } }
  }
}
"""

MEDIA_LIST_QUERY = """
query ($userId: Int, $type: MediaType, $status: MediaListStatus) {
  MediaListCollection(userId: $userId, type: $type, status: $status) {
    lists {
      entries {
        progress
        score
        media { title { romaji } episodes chapters }
      }
    }
  }
}
"""

# The authenticated viewer's own list entry for a media. ``mediaListEntry`` is
# only resolved per-viewer when the request carries that user's OAuth token.
MEDIA_ENTRY_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    mediaListEntry {
      status
      score
      progress
      progressVolumes
      repeat
      startedAt { year month day }
      completedAt { year month day }
    }
  }
}
"""

VALID_STATUSES = {
    "CURRENT",
    "PLANNING",
    "COMPLETED",
    "DROPPED",
    "PAUSED",
    "REPEATING",
}

# Ordered so we can step forwards/backwards through the seasonal calendar.
SEASONS = ("WINTER", "SPRING", "SUMMER", "FALL")


def _media_title(media):
    """Return a friendly "Romaji (English)" title for a media dict."""

    title = media.get("title") or {}
    romaji = title.get("romaji") or "Unknown"
    english = title.get("english")
    if english and english != romaji:
        return f"{romaji} ({english})"
    return romaji


def _media_colour(media):
    """Use the cover image's accent colour ("#aabbcc") as an int, else random."""

    colour = (media.get("coverImage") or {}).get("color")
    if isinstance(colour, str) and colour.startswith("#"):
        try:
            return int(colour[1:], 16)
        except ValueError:
            pass
    return random_colour()


def _media_unit(media, *, plural=False):
    """Return the progress unit word ("episode"/"chapter") for a media dict.

    Manga track chapters, everything else tracks episodes. Relies on the
    ``type`` field, falling back to whichever count the media actually has.
    """

    mtype = media.get("type")
    if mtype == "MANGA":
        is_manga = True
    elif mtype == "ANIME":
        is_manga = False
    else:
        is_manga = bool(media.get("chapters")) and not media.get("episodes")

    word = "chapter" if is_manga else "episode"
    return word + "s" if plural else word


def _format_ranking(ranking):
    """Format an AniList ranking dict as ``"#3 Most Popular (all time)"``.

    Returns ``None`` when the ranking lacks a rank or context to display.
    """

    rank = ranking.get("rank")
    context = (ranking.get("context") or "").strip()
    if not rank or not context:
        return None

    if ranking.get("allTime") and "all time" in context.lower():
        label = re.sub(r"\s*all time", "", context, flags=re.IGNORECASE).strip()
        label = f"{label.title()} (all time)"
    else:
        label = context.title()
    return f"#{rank} {label}"


def _format_fuzzy_date(date):
    """Format an AniList fuzzy date dict (year/month/day) as ``YYYY-MM-DD``.

    Month and day may be missing; returns ``None`` when there is no year.
    """

    if not date:
        return None
    year = date.get("year")
    if not year:
        return None
    month = date.get("month")
    day = date.get("day")
    if month and day:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if month:
        return f"{year:04d}-{month:02d}"
    return str(year)


def _format_score(score):
    """Render a raw AniList score, dropping a trailing ``.0`` on whole numbers."""

    if score is None:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return str(score)
    if value.is_integer():
        return str(int(value))
    return str(score)


def _current_season(now=None):
    """Return the ``(SEASON, year)`` matching the given UTC datetime."""

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if now.month in (12, 1, 2):
        season = "WINTER"
    elif now.month in (3, 4, 5):
        season = "SPRING"
    elif now.month in (6, 7, 8):
        season = "SUMMER"
    else:
        season = "FALL"
    return season, now.year


def _step_season(season, year, *, forward=True):
    """Step one season forward/backward, rolling the year at the boundaries."""

    try:
        index = SEASONS.index(season)
    except ValueError:
        return _current_season()

    if forward:
        index += 1
        if index >= len(SEASONS):
            return SEASONS[0], year + 1
        return SEASONS[index], year

    index -= 1
    if index < 0:
        return SEASONS[-1], year - 1
    return SEASONS[index], year


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
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )

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


class ResultView(discord.ui.View):
    """Author-restricted wrapper around a :class:`ResultSelect`."""

    def __init__(self, cog, results, author_id, media_type, timeout=120):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message = None
        self.add_item(ResultSelect(cog, results, author_id, media_type))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SeasonView(discord.ui.View):
    """Seasonal browser: a title picker plus previous/next season navigation."""

    def __init__(self, cog, results, author_id, season, year, timeout=180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.season = season
        self.year = year
        self.message = None
        self.add_item(ResultSelect(cog, results, author_id, "ANIME"))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

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

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )

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


class EditSelectView(discord.ui.View):
    """Author-restricted wrapper around an :class:`EditSelect`."""

    def __init__(self, cog, candidates, author_id, field, value, timeout=120):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message = None
        self.add_item(EditSelect(cog, candidates, author_id, field, value))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class EditEntryModal(discord.ui.Modal, title="Edit list entry"):
    """Collect a new status/progress/score, pre-filled from the user's entry."""

    status = discord.ui.TextInput(
        label="Status",
        placeholder="CURRENT / PLANNING / COMPLETED / DROPPED / PAUSED / REPEATING",
        required=False,
        style=discord.TextStyle.short,
        max_length=12,
    )
    progress = discord.ui.TextInput(
        label="Progress (episode/chapter)",
        required=False,
        style=discord.TextStyle.short,
        max_length=6,
    )
    score = discord.ui.TextInput(
        label="Score",
        required=False,
        max_length=6,
    )

    def __init__(self, cog, media, token, entry=None):
        super().__init__()
        self.cog = cog
        self.media = media
        self.token = token

        # Pre-fill each field from the viewer's existing entry, if any. The
        # TextInputs are deep-copied per instance, so these defaults never leak.
        entry = entry or {}
        current_status = entry.get("status")
        if current_status:
            self.status.default = current_status
        current_progress = entry.get("progress")
        if current_progress is not None:
            self.progress.default = str(current_progress)
        current_score = _format_score(entry.get("score"))
        if current_score and current_score != "0":
            self.score.default = current_score

    async def on_submit(self, interaction):
        variables = {"mediaId": self.media.get("id")}
        status_raw = (self.status.value or "").strip()
        progress_raw = (self.progress.value or "").strip()
        score_raw = (self.score.value or "").strip()

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

        if status_raw:
            status = status_raw.upper()
            if status not in VALID_STATUSES:
                return await interaction.response.send_message(
                    "Status must be one of: "
                    + ", ".join(sorted(VALID_STATUSES)) + ".",
                    ephemeral=True,
                )
            variables["status"] = status

        if (
            "progress" not in variables
            and "score" not in variables
            and "status" not in variables
        ):
            return await interaction.response.send_message(
                "Nothing to update - fill in status, progress and/or score.",
                ephemeral=True,
            )

        try:
            data = await self.cog._graphql(
                SAVE_ENTRY_QUERY, variables, token=self.token
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


class MediaView(discord.ui.View):
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
        super().__init__(timeout=timeout)
        self.cog = cog
        self.media = media
        self.author_id = author_id
        self.token = token
        self.parent_view = parent_view
        self.parent_embed = parent_embed
        self.parent_content = parent_content
        self.message = None

        # The "Back" button (row 2) only makes sense when we came from a menu.
        if self.parent_view is None:
            for child in list(self.children):
                if getattr(child, "row", None) == 2:
                    self.remove_item(child)

        # The quick-action row only makes sense for linked users.
        if self.token is None:
            for child in list(self.children):
                if getattr(child, "row", None) == 1:
                    self.remove_item(child)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

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
        embed.description = self.cog._clean_description(media.get("description"))

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

    # -- back to the originating menu (row 2, only when we have a parent) --
    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=2)
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

    # -- quick actions (row 1, linked users only) ----------------------
    @discord.ui.button(label="Watching", style=discord.ButtonStyle.success, row=1)
    async def watching_button(self, interaction, button):
        await self._set_status(interaction, "CURRENT")

    @discord.ui.button(label="Completed", style=discord.ButtonStyle.success, row=1)
    async def completed_button(self, interaction, button):
        await self._set_status(interaction, "COMPLETED")

    @discord.ui.button(label="Planning", style=discord.ButtonStyle.secondary, row=1)
    async def planning_button(self, interaction, button):
        await self._set_status(interaction, "PLANNING")

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, row=1)
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

    async def _set_status(self, interaction, status):
        try:
            data = await self.cog._graphql(
                SAVE_ENTRY_QUERY,
                {"mediaId": self.media.get("id"), "status": status},
                token=self.token,
            )
            entry = ((data or {}).get("data") or {}).get("SaveMediaListEntry")
            if not entry:
                return await interaction.response.send_message(
                    "Could not update that entry.", ephemeral=True
                )

            name = (
                (entry.get("media") or {}).get("title") or {}
            ).get("romaji") or _media_title(self.media)
            await interaction.response.send_message(
                f"Set **{name}** to {entry.get('status')}.", ephemeral=True
            )
        except Exception:
            log.exception("AniList quick status update failed")
            try:
                await interaction.response.send_message(
                    "Something went wrong updating that entry.", ephemeral=True
                )
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
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

    def __init__(self, cog, author_id):
        super().__init__()
        self.cog = cog
        self.author_id = author_id

    async def on_submit(self, interaction):
        try:
            name = await self.cog._exchange_code(self.author_id, self.code.value)
            if name is None:
                return await interaction.response.send_message(
                    "That code did not work, try `/anilist login` again.",
                    ephemeral=True,
                )
            await interaction.response.send_message(
                f"Connected as {name}!", ephemeral=True
            )
        except Exception:
            log.exception("AniList login modal failed")
            try:
                await interaction.response.send_message(
                    "Something went wrong linking your account.", ephemeral=True
                )
            except Exception:
                pass


class LoginView(discord.ui.View):
    """Author-restricted view exposing a modal to enter the OAuth PIN."""

    def __init__(self, cog, author_id, timeout=300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction, button):
        try:
            await interaction.response.send_modal(
                LoginModal(self.cog, self.author_id)
            )
        except Exception:
            log.exception("AniList login modal launch failed")
            try:
                await interaction.response.send_message(
                    "Could not open the code form.", ephemeral=True
                )
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class AniList(commands.Cog):
    """AniList lookups plus per-user account linking to edit your lists."""

    def __init__(self, bot):
        self.bot = bot

        try:
            self.client_id = config_loader.get("AniList", "clientId")
        except Exception:
            self.client_id = ""

        try:
            self.client_secret = config_loader.get("AniList", "clientSecret")
        except Exception:
            self.client_secret = ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _graphql(self, query, variables, token=None):
        """POST a GraphQL request to AniList. Returns the parsed JSON or None."""

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = "Bearer " + token

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    API_URL,
                    json={"query": query, "variables": variables},
                    headers=headers,
                ) as r:
                    return await r.json()
        except Exception:
            log.exception("AniList GraphQL request failed")
            return None

    def _clean_description(self, text):
        """Strip HTML, collapse whitespace and truncate AniList descriptions."""

        if not text:
            return ""

        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 600:
            text = text[:600].rstrip() + "..."
        return text

    async def _store_token(self, user_id, access_token, expires_in):
        """Persist the encrypted access token (never the plaintext)."""

        encrypted = crypto.encrypt(access_token)
        expires = None
        if expires_in:
            expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                seconds=expires_in
            )

        query = """
            INSERT INTO anilist_tokens (user_id, token, expires)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET token = $2, expires = $3;
            """
        await self.bot.db_pool.execute(query, user_id, encrypted, expires)

    async def _get_token(self, user_id):
        """Return the decrypted access token, or None if missing/expired."""

        query = "SELECT token, expires FROM anilist_tokens WHERE user_id = $1;"
        row = await self.bot.db_pool.fetchrow(query, user_id)
        if row is None:
            return None

        if row["expires"] and row["expires"] < datetime.datetime.now(
            datetime.timezone.utc
        ):
            return None

        return crypto.decrypt(row["token"])

    async def _viewer_entry(self, user_id, media_id):
        """Return ``(entry, logged_in)`` for the user's list entry on a media.

        ``entry`` is the authenticated viewer's ``mediaListEntry`` (or ``None``
        when the media is not on their list), and ``logged_in`` is ``True`` only
        when a valid token was found. The query is sent with the user's OAuth
        token so AniList resolves the entry per-viewer; the token is never
        logged.
        """

        if media_id is None:
            return None, False

        token = await self._get_token(user_id)
        if not token:
            return None, False

        data = await self._graphql(
            MEDIA_ENTRY_QUERY, {"id": media_id}, token=token
        )
        entry = (
            ((data or {}).get("data") or {}).get("Media") or {}
        ).get("mediaListEntry")
        return entry, True

    async def _exchange_code(self, user_id, code):
        """Exchange an OAuth PIN for a token and store it.

        Returns the AniList viewer name on success, or ``None`` on failure.
        The token and code are never logged or echoed.
        """

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": REDIRECT_URI,
            "code": (code or "").strip(),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(TOKEN_URL, json=payload) as r:
                    data = await r.json()
        except Exception:
            log.exception("AniList token exchange failed")
            return None

        access_token = (data or {}).get("access_token")
        if not access_token:
            return None

        await self._store_token(user_id, access_token, data.get("expires_in"))

        viewer = await self._graphql(VIEWER_QUERY, {}, token=access_token)
        name = (((viewer or {}).get("data") or {}).get("Viewer") or {}).get("name")
        return name or "AniList user"

    async def _search_candidates(self, title):
        """Return up to ~10 search candidates across both anime and manga.

        The lack of a type filter is deliberate: it lets the edit flow tell
        the user that, e.g., "Berserk" exists as both an anime and a manga.
        """

        data = await self._graphql(SEARCH_QUERY, {"search": title})
        page = ((data or {}).get("data") or {}).get("Page") or {}
        return page.get("media") or []

    async def _reply(self, sender, content):
        """Send ``content`` via either a Context or an Interaction."""

        try:
            if isinstance(sender, discord.Interaction):
                if sender.response.is_done():
                    await sender.followup.send(content, ephemeral=True)
                else:
                    await sender.response.send_message(content, ephemeral=True)
            else:
                await sender.send(content)
        except Exception:
            log.exception("AniList reply failed")

    async def _apply_edit(self, sender, user_id, media, field, value):
        """Apply a single ``field`` edit to ``user_id``'s list entry for ``media``.

        ``field`` is one of ``progress``/``status``/``score``. ``sender`` may be
        a Context or an Interaction; the type-aware confirmation is routed
        accordingly (episode vs chapter).
        """

        token = await self._get_token(user_id)
        if not token:
            return await self._reply(
                sender, "Link your account first with `/anilist login`."
            )

        variables = {"mediaId": media.get("id")}
        if field == "progress":
            variables["progress"] = value
            variables["status"] = "CURRENT"
        elif field == "status":
            variables["status"] = value
        elif field == "score":
            variables["score"] = value

        data = await self._graphql(SAVE_ENTRY_QUERY, variables, token=token)
        entry = ((data or {}).get("data") or {}).get("SaveMediaListEntry")
        if not entry:
            return await self._reply(sender, "Could not update that entry.")

        name = ((entry.get("media") or {}).get("title") or {}).get(
            "romaji"
        ) or _media_title(media)

        if field == "progress":
            unit = _media_unit(media)
            message = (
                f"Set **{name}** to {unit} {entry.get('progress')} "
                f"({entry.get('status')})."
            )
        elif field == "status":
            message = f"Set **{name}** to {entry.get('status')}."
        else:
            message = f"Scored **{name}** {entry.get('score')}."

        await self._reply(sender, message)

    async def _edit_flow(self, ctx, title, field, value):
        """Resolve ``title`` (disambiguating anime/manga) then apply an edit."""

        token = await self._get_token(ctx.author.id)
        if not token:
            return await ctx.send("Link your account first with `/anilist login`.")

        async with ctx.typing():
            candidates = await self._search_candidates(title)

        if not candidates:
            return await ctx.send(f"No result for **{title}**.")

        if len(candidates) == 1:
            return await self._apply_edit(
                ctx, ctx.author.id, candidates[0], field, value
            )

        view = EditSelectView(self, candidates, ctx.author.id, field, value)
        view.message = await ctx.send(
            content=f"Multiple matches for **{title}** - pick the right one:",
            view=view,
        )

    # ------------------------------------------------------------------
    # Lookup commands (no auth required)
    # ------------------------------------------------------------------
    async def _media_lookup(self, ctx, search, media_type):
        """Search AniList and present results via the interactive flow."""

        async with ctx.typing():
            data = await self._graphql(
                CANDIDATE_QUERY, {"search": search, "type": media_type}
            )
            candidates = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []
            if not candidates:
                return await ctx.send("No result.")

            token = await self._get_token(ctx.author.id)

            # A single match jumps straight to the full media view.
            if len(candidates) == 1:
                full = await self._graphql(
                    MEDIA_QUERY, {"id": candidates[0]["id"]}
                )
                media = ((full or {}).get("data") or {}).get("Media")
                if not media:
                    return await ctx.send("No result.")
                view = MediaView(self, media, ctx.author.id, token=token)
                view.message = await ctx.send(
                    embed=view.overview_embed(), view=view
                )
                return

            view = ResultView(self, candidates, ctx.author.id, media_type)
            view.message = await ctx.send(
                content=f"Found {len(candidates)} results for **{search}** - "
                "pick one:",
                view=view,
            )

    async def _browse(self, ctx, variables, media_type, label):
        """Run a PAGE_QUERY browse and offer the results as a picker."""

        async with ctx.typing():
            data = await self._graphql(PAGE_QUERY, variables)
            media = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []
            if not media:
                return await ctx.send("No result.")

            view = ResultView(self, media, ctx.author.id, media_type)
            view.message = await ctx.send(
                content=f"**{label}** - pick one for details:", view=view
            )

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anime(self, ctx, *, search: str):
        """Look up an anime on AniList."""

        await self._media_lookup(ctx, search, "ANIME")

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def manga(self, ctx, *, search: str):
        """Look up a manga on AniList."""

        await self._media_lookup(ctx, search, "MANGA")

    @commands.hybrid_command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def trending(self, ctx):
        """Browse the anime trending on AniList right now."""

        await self._browse(
            ctx,
            {"sort": ["TRENDING_DESC"], "type": "ANIME"},
            "ANIME",
            "Trending anime",
        )

    @commands.hybrid_command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def popular(self, ctx):
        """Browse the most popular anime on AniList."""

        await self._browse(
            ctx,
            {"sort": ["POPULARITY_DESC"], "type": "ANIME"},
            "ANIME",
            "Popular anime",
        )

    @commands.hybrid_command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def seasonal(self, ctx, season: str = None, year: int = None):
        """Browse anime from a season (defaults to the current season)."""

        if season:
            season = season.upper()
            if season not in SEASONS:
                return await ctx.send(
                    "Season must be one of: WINTER, SPRING, SUMMER, FALL."
                )
            if year is None:
                year = datetime.datetime.now(datetime.timezone.utc).year
        else:
            current_season, current_year = _current_season()
            season = current_season
            if year is None:
                year = current_year

        async with ctx.typing():
            data = await self._graphql(
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
                return await ctx.send(f"No anime found for {season.title()} {year}.")

            view = SeasonView(self, media, ctx.author.id, season, year)
            view.message = await ctx.send(
                content=f"**{season.title()} {year} anime** - pick one for details:",
                view=view,
            )

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def character(self, ctx, *, search: str):
        """Look up a character on AniList."""

        async with ctx.typing():
            data = await self._graphql(CHARACTER_QUERY, {"search": search})
            char = ((data or {}).get("data") or {}).get("Character")
            if not char:
                return await ctx.send("No character found.")

            name = char.get("name") or {}
            full = name.get("full") or "Unknown"
            native = name.get("native")
            title = f"{full} ({native})" if native else full

            embed = discord.Embed(
                title=title,
                url=char.get("siteUrl"),
                description=self._clean_description(char.get("description")),
                colour=random_colour(),
            )
            image = char.get("image") or {}
            if image.get("large"):
                embed.set_thumbnail(url=image["large"])
            await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def studio(self, ctx, *, search: str):
        """Look up an animation studio on AniList."""

        async with ctx.typing():
            data = await self._graphql(STUDIO_QUERY, {"search": search})
            studio = ((data or {}).get("data") or {}).get("Studio")
            if not studio:
                return await ctx.send("No studio found.")

            embed = discord.Embed(
                title=studio.get("name") or "Unknown studio",
                url=studio.get("siteUrl"),
                colour=random_colour(),
            )

            nodes = ((studio.get("media") or {}).get("nodes")) or []
            titles = [
                (n.get("title") or {}).get("romaji")
                for n in nodes
                if (n.get("title") or {}).get("romaji")
            ]
            if titles:
                embed.add_field(
                    name="Popular productions",
                    value="\n".join(f"- {t}" for t in titles[:10]),
                    inline=False,
                )
            await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Account group (OAuth PIN flow + list editing)
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="anilist")
    async def anilist(self, ctx):
        """Link your AniList account and edit your lists."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @anilist.command(name="login")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_login(self, ctx):
        """Start linking your AniList account."""

        if not self.client_id or not self.client_secret or not crypto.is_configured():
            return await ctx.send("AniList account linking is not configured.")

        authorize_url = (
            "https://anilist.co/api/v2/oauth/authorize?client_id="
            + self.client_id
            + "&redirect_uri="
            + REDIRECT_URI
            + "&response_type=code"
        )
        instructions = (
            "Authorize the bot here:\n"
            f"{authorize_url}\n\n"
            "Authorize, copy the code AniList shows you, then press "
            "**Enter code** below (or run `/anilist code <code>`)."
        )

        view = LoginView(self, ctx.author.id)

        try:
            view.message = await ctx.author.send(instructions, view=view)
        except discord.Forbidden:
            view.message = await ctx.send(instructions, view=view, ephemeral=True)
            return

        await ctx.send("Check your DMs.")

    @anilist.command(name="code")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_code(self, ctx, *, code: str):
        """Finish linking with the PIN code AniList gave you."""

        if not self.client_id or not self.client_secret or not crypto.is_configured():
            return await ctx.send("AniList account linking is not configured.")

        # Hide the PIN if it was posted in a guild text channel.
        if ctx.message is not None and ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        name = await self._exchange_code(ctx.author.id, code)
        if name is None:
            return await ctx.send(
                "That code did not work, try `/anilist login` again.",
                ephemeral=ctx.interaction is not None,
            )

        await ctx.send(
            f"Connected as {name}!", ephemeral=ctx.interaction is not None
        )

    @anilist.command(name="logout")
    async def anilist_logout(self, ctx):
        """Unlink your AniList account."""

        await self.bot.db_pool.execute(
            "DELETE FROM anilist_tokens WHERE user_id = $1;", ctx.author.id
        )
        await ctx.send(
            "Your AniList account has been unlinked.",
            ephemeral=ctx.interaction is not None,
        )

    @anilist.command(name="update")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_update(self, ctx, progress: int, *, title: str):
        """Set your progress on a title and mark it as currently watching/reading."""

        if progress < 0:
            return await ctx.send("Progress must be zero or a positive number.")

        await self._edit_flow(ctx, title, "progress", progress)

    @anilist.command(name="status")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_status(self, ctx, status: str, *, title: str):
        """Set the status of a title on your list."""

        status = status.upper()
        if status not in VALID_STATUSES:
            return await ctx.send(
                "Status must be one of: " + ", ".join(sorted(VALID_STATUSES)) + "."
            )

        await self._edit_flow(ctx, title, "status", status)

    @anilist.command(name="score")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_score(self, ctx, score: float, *, title: str):
        """Score a title on your AniList list."""

        if score < 0:
            return await ctx.send("Score must be zero or a positive number.")

        await self._edit_flow(ctx, title, "score", score)

    @anilist.command(name="profile")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_profile(self, ctx, *, name: str = None):
        """Show AniList stats for a user (defaults to your linked account)."""

        async with ctx.typing():
            if name is None:
                token = await self._get_token(ctx.author.id)
                if not token:
                    return await ctx.send(
                        "Provide a name or link your account with "
                        "`/anilist login`."
                    )
                viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
                name = (
                    ((viewer or {}).get("data") or {}).get("Viewer") or {}
                ).get("name")
                if not name:
                    return await ctx.send("Could not resolve your AniList account.")

            data = await self._graphql(USER_STATS_QUERY, {"name": name})
            user = ((data or {}).get("data") or {}).get("User")
            if not user:
                return await ctx.send("No AniList user found.")

            stats = user.get("statistics") or {}
            anime = stats.get("anime") or {}
            manga = stats.get("manga") or {}

            embed = discord.Embed(
                title=user.get("name") or name,
                url=user.get("siteUrl"),
                colour=random_colour(),
            )
            avatar = user.get("avatar") or {}
            if avatar.get("large"):
                embed.set_thumbnail(url=avatar["large"])

            days = (anime.get("minutesWatched") or 0) / 1440
            embed.add_field(name="Anime", value=str(anime.get("count") or 0))
            embed.add_field(name="Days watched", value=f"{days:.1f}")
            embed.add_field(name="Mean score", value=str(anime.get("meanScore") or 0))

            genres = anime.get("genres") or []
            top = ", ".join(
                g.get("genre") for g in genres[:6] if g.get("genre")
            )
            if top:
                embed.add_field(name="Top genres", value=top, inline=False)

            embed.add_field(name="Manga", value=str(manga.get("count") or 0))
            embed.add_field(
                name="Chapters read", value=str(manga.get("chaptersRead") or 0)
            )
            embed.add_field(name="Manga mean", value=str(manga.get("meanScore") or 0))

            await ctx.send(embed=embed)

    @anilist.command(name="list")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_list(
        self, ctx, media_type: str = "anime", status: str = "CURRENT"
    ):
        """Show your anime/manga list, filtered by status (defaults to CURRENT)."""

        media_type = media_type.lower()
        if media_type not in ("anime", "manga"):
            return await ctx.send("Media type must be `anime` or `manga`.")

        status = status.upper()
        if status not in VALID_STATUSES:
            return await ctx.send(
                "Status must be one of: " + ", ".join(sorted(VALID_STATUSES)) + "."
            )

        token = await self._get_token(ctx.author.id)
        if not token:
            return await ctx.send("Link your account first with `/anilist login`.")

        gql_type = media_type.upper()
        unit = "chapters" if gql_type == "MANGA" else "episodes"

        async with ctx.typing():
            viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
            user = ((viewer or {}).get("data") or {}).get("Viewer")
            if not user:
                return await ctx.send("Could not reach your AniList account.")

            data = await self._graphql(
                MEDIA_LIST_QUERY,
                {"userId": user["id"], "type": gql_type, "status": status},
                token=token,
            )
            collection = (
                ((data or {}).get("data") or {}).get("MediaListCollection") or {}
            )

            lines = []
            for lst in collection.get("lists") or []:
                for entry in lst.get("entries") or []:
                    media = entry.get("media") or {}
                    name = (media.get("title") or {}).get("romaji") or "Unknown"
                    total = (
                        media.get("chapters")
                        if gql_type == "MANGA"
                        else media.get("episodes")
                    ) or "?"
                    lines.append(
                        f"{name} - {entry.get('progress', 0)}/{total} {unit}"
                    )

            if not lines:
                return await ctx.send(
                    f"Nothing on your {status.title()} {media_type} list."
                )

        await Paginator(
            paginate_lines(lines, title=f"{status.title()} {media_type} list"),
            author_id=ctx.author.id,
        ).start(ctx)


async def setup(bot):
    await bot.add_cog(AniList(bot))
