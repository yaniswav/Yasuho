import datetime
import re

from tools.formats import random_colour

API_URL = "https://graphql.anilist.co"
TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
REDIRECT_URI = "https://anilist.co/api/v2/oauth/pin"

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


def _progress_max(media):
    """Return the total episodes/chapters for clamping progress, or None.

    Returns ``None`` when the total is unknown (e.g. an ongoing series), so
    callers should treat a missing value as "no upper bound".
    """

    if _media_unit(media) == "chapter":
        total = media.get("chapters")
    else:
        total = media.get("episodes")
    return total if total else None


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


def _clean_description(text):
    """Strip HTML, collapse whitespace and truncate AniList descriptions."""

    if not text:
        return ""

    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 600:
        text = text[:600].rstrip() + "..."
    return text
