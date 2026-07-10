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

# Friendly labels for the AniList list statuses (CURRENT is type-aware below).
_STATUS_LABELS = {
    "CURRENT": "Watching",
    "PLANNING": "Planning",
    "COMPLETED": "Completed",
    "DROPPED": "Dropped",
    "PAUSED": "Paused",
    "REPEATING": "Repeating",
}

# Accept friendly words / aliases (not just the raw enum) when a user types a status.
_STATUS_ALIASES = {
    "watching": "CURRENT", "reading": "CURRENT", "current": "CURRENT",
    "watch": "CURRENT", "read": "CURRENT",
    "planning": "PLANNING", "plan": "PLANNING", "planned": "PLANNING",
    "ptw": "PLANNING", "plantowatch": "PLANNING", "plantoread": "PLANNING",
    "completed": "COMPLETED", "complete": "COMPLETED", "done": "COMPLETED",
    "finished": "COMPLETED",
    "dropped": "DROPPED", "drop": "DROPPED",
    "paused": "PAUSED", "pause": "PAUSED", "hold": "PAUSED", "onhold": "PAUSED",
    "repeating": "REPEATING", "repeat": "REPEATING",
    "rewatching": "REPEATING", "rereading": "REPEATING",
}


def _status_label(status, media=None):
    """Friendly label for a MediaListStatus (CURRENT -> Watching, or Reading for manga)."""

    if not status:
        return None
    upper = status.upper()
    if upper == "CURRENT" and media is not None and _media_unit(media) == "chapter":
        return "Reading"
    return _STATUS_LABELS.get(upper, status.title())


def _parse_status(text):
    """Map a friendly word, alias, or raw enum to a MediaListStatus (or None)."""

    if not text:
        return None
    key = text.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in _STATUS_ALIASES:
        return _STATUS_ALIASES[key]
    upper = text.strip().upper()
    return upper if upper in VALID_STATUSES else None


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


# AniList's named profile colours -> a matching embed colour.
_PROFILE_COLOURS = {
    "blue": 0x3DB4F2,
    "purple": 0xC063FF,
    "pink": 0xFC9DD6,
    "orange": 0xEF881A,
    "red": 0xE13333,
    "green": 0x4CCB48,
    "gray": 0x677B94,
    "grey": 0x677B94,
}


def _profile_colour(value):
    """Map an AniList profile colour (a named colour or "#hex") to an int."""

    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in _PROFILE_COLOURS:
        return _PROFILE_COLOURS[value]
    if value.startswith("#"):
        try:
            return int(value[1:], 16)
        except ValueError:
            return None
    return None


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
    """Render a raw AniList score, dropping a trailing ``.0`` on whole numbers.

    Format-agnostic: returns the bare in-format number (e.g. ``"8"`` or
    ``"8.5"``), so it stays the right editable value to pre-fill a text input
    regardless of the viewer's score format. For decorated display use
    :func:`render_score`.
    """

    if score is None:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return str(score)
    if value.is_integer():
        return str(int(value))
    return str(score)


# --- Score formats ----------------------------------------------------------
#
# AniList users pick one of five list score formats (Viewer.mediaListOptions.
# scoreFormat). A score is stored, returned and accepted BY THE MUTATION all in
# the viewer's own format, so the bot renders/parses in that format directly and
# never converts. ``POINT_100`` is the historical default and the silent
# fallback whenever the format is unknown or cannot be fetched.
DEFAULT_SCORE_FORMAT = "POINT_100"

SCORE_FORMATS = frozenset(
    {"POINT_100", "POINT_10_DECIMAL", "POINT_10", "POINT_5", "POINT_3"}
)

# POINT_3's three AniList faces (sad / neutral / happy), keyed by the 1-3 value.
_POINT_3_FACES = {1: "🙁", 2: "😐", 3: "🙂"}

# Short numeric-range placeholders shown in the score input (not translated:
# they are bare digit ranges).
_SCORE_HINTS = {
    "POINT_100": "0-100",
    "POINT_10": "0-10",
    "POINT_10_DECIMAL": "0.0-10.0",
    "POINT_5": "0-5",
    "POINT_3": "1-3",
}


def _coerce_score(value):
    """Best-effort float from a raw score value, or None if not numeric."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_format(score_format):
    """Normalise a score format, falling back to the default when unknown."""

    return score_format if score_format in SCORE_FORMATS else DEFAULT_SCORE_FORMAT


def render_score(value, score_format=DEFAULT_SCORE_FORMAT):
    """Render an ENTRY score for display in the viewer's score format.

    Returns ``None`` for an unset score - both ``None`` and ``0`` (AniList's
    "0 means no score" convention) - so callers can just skip a falsy result.
    A set score renders as:

      * POINT_100         -> ``"85"``
      * POINT_10          -> ``"8/10"``
      * POINT_10_DECIMAL  -> ``"8.5/10"``
      * POINT_5           -> star glyphs, e.g. ``"★★★★☆"``
      * POINT_3           -> a face emoji, e.g. ``"🙂"``

    Unknown formats fall back to POINT_100 semantics.
    """

    num = _coerce_score(value)
    if not num:  # None or 0.0 -> unset per AniList convention.
        return None

    fmt = _score_format(score_format)
    if fmt == "POINT_10":
        return "{}/10".format(int(round(num)))
    if fmt == "POINT_10_DECIMAL":
        return "{:.1f}/10".format(num)
    if fmt == "POINT_5":
        n = max(0, min(5, int(round(num))))
        return "★" * n + "☆" * (5 - n)
    if fmt == "POINT_3":
        return _POINT_3_FACES.get(int(round(num)), _POINT_3_FACES[2])
    return str(int(round(num)))  # POINT_100


def parse_score(text, score_format=DEFAULT_SCORE_FORMAT):
    """Parse score input in the viewer's format to a float, or None if invalid.

    The returned value is sent to SaveMediaListEntry as-is (AniList interprets
    it in the viewer's format). ``0`` is a valid input meaning "unset" and is
    returned as ``0.0`` rather than rejected. Out-of-range or malformed input
    (e.g. a decimal where the format is integer) returns ``None``. Unknown
    formats fall back to POINT_100 semantics.
    """

    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        return None

    fmt = _score_format(score_format)
    if fmt == "POINT_10_DECIMAL":
        lo, hi, integer = 0.0, 10.0, False
    elif fmt == "POINT_10":
        lo, hi, integer = 0.0, 10.0, True
    elif fmt == "POINT_5":
        lo, hi, integer = 0.0, 5.0, True
    elif fmt == "POINT_3":
        lo, hi, integer = 0.0, 3.0, True
    else:  # POINT_100
        lo, hi, integer = 0.0, 100.0, True

    if num < lo or num > hi:
        return None
    if integer and not num.is_integer():
        return None
    return num


def score_hint(score_format=DEFAULT_SCORE_FORMAT):
    """Short numeric-range placeholder for the viewer's score format."""

    return _SCORE_HINTS[_score_format(score_format)]


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
