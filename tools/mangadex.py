"""Pure, testable core for MangaDex-based manga chapter alerts.

cogs/anilist/chapters.py polls MangaDex for new chapters of the manga a
guild/user tracks and posts (or DMs) an alert with a one-click read link.
This module owns the only non-trivial decisions that logic needs - AniList ->
MangaDex mapping, feed normalisation, and the dedup + cursor core that decides
what to alert - as pure functions so the cog stays a thin I/O shell and the
tests need no network.

It is deliberately free of any aiohttp/discord.py/database use: it only shapes,
matches and filters data. The only concession to I/O is a handful of pure
request BUILDERS (url + params + headers) so the exact MangaDex contract lives
here and is unit-tested; the actual HTTP call belongs to the cog. It is also
translation-free (no ``_()``): it returns raw data and the cog does all
user-facing wording and localisation.

MangaDex facts this module is built on (live-verified 2026-07-10):

* An identifiable ``User-Agent`` is REQUIRED by MangaDex's ToS; every request
  builder here stamps :data:`USER_AGENT`.
* There is NO AniList-id filter on ``/manga``. Mapping is a title search whose
  candidates are ALL scanned for the exact ``attributes.links.al`` string - never
  "take the first hit" (a search for "Kingdom" ranks the wanted manga third). A
  miss is a recorded state, not an error (see :func:`pick_mapping`).
* There is NO batch chapter endpoint; the chapter source is the per-manga feed
  ``GET /manga/{uuid}/feed`` ordered by ``readableAt`` desc, NEVER the global
  ``/chapter`` feed (500-item cap, gap-unsafe).
* The SAME logical chapter appears multiple times (one row per scanlation
  group). Alert identity is therefore ``(volume, chapter)`` PER MANGA - not the
  per-row chapter UUID - so the groups collapse to one alert (see
  :func:`chapter_key`).
* ``attributes.externalUrl`` non-null means an official link-only stub (e.g.
  MangaPlus for One Piece): there is no MangaDex reader page, so the alert must
  link out to that url instead (see :func:`reader_url`).
* ``readableAt`` is the publication timestamp we cursor on. THE trap: a second
  scanlation upload of an ALREADY-alerted chapter arrives with a LATER
  ``readableAt``, so a naive "readableAt > cursor" re-alerts it. The cursor alone
  cannot fix this; :func:`plan_chapter_alerts` also carries a bounded memory of
  already-alerted ``(volume, chapter)`` keys and never re-alerts one, whatever
  its ``readableAt`` says.
"""

from __future__ import annotations

from datetime import datetime, timezone

# --- MangaDex contract ------------------------------------------------------
BASE_URL = "https://api.mangadex.org"

# The reader page a normal (non-stub) chapter links to.
READER_BASE = "https://mangadex.org/chapter"

# Identifiable User-Agent, REQUIRED by MangaDex's ToS. Bump the version here if
# the client behaviour ever changes materially.
USER_AGENT = "Yasuho-DiscordBot/1.0 (github.com/yaniswav/Yasuho)"

# Default language for chapter alerts. English scanlations are what the tracker
# targets; the request builder keeps it a parameter so a later lot can widen it.
DEFAULT_LANGUAGE = "en"

# How many search candidates to scan for the AniList-id match. MangaDex ranks by
# relevance, but the wanted title is not always first (see the module docstring),
# so we pull a small page and scan ALL of it.
SEARCH_LIMIT = 10

# How many of the newest chapters to pull from a per-manga feed each poll. A
# short window is enough between ticks; the cursor + seen memory guard the rest.
FEED_LIMIT = 5


# --- Request builders (pure; the cog performs the actual HTTP) ---------------
#
# Each returns ``(url, params, headers)``. ``params`` is a LIST of ``(key,
# value)`` pairs, not a dict, because the feed needs a repeated bracketed key
# (``translatedLanguage[]``) that a dict cannot express; aiohttp accepts a list
# of pairs directly.


def _headers():
    """The headers every MangaDex request must carry (the ToS User-Agent)."""

    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def search_manga_request(title, limit=SEARCH_LIMIT):
    """Build the ``GET /manga`` title-search request for the mapping step.

    Returns ``(url, params, headers)``. The response's ``data`` list is handed to
    :func:`pick_mapping` to find the candidate whose ``links.al`` matches the
    AniList id. ``limit`` is clamped to MangaDex's 1..100 window.
    """

    limit = max(1, min(int(limit), 100))
    url = BASE_URL + "/manga"
    params = [
        ("title", str(title)),
        ("limit", str(limit)),
    ]
    return url, params, _headers()


def manga_feed_request(mangadex_id, language=DEFAULT_LANGUAGE, limit=FEED_LIMIT, offset=0):
    """Build the per-manga chapter-feed request for one manga UUID.

    Returns ``(url, params, headers)`` for
    ``GET /manga/{uuid}/feed?translatedLanguage[]=<lang>&order[readableAt]=desc``.
    The response is normalised by :func:`parse_chapter_feed`. This is the ONLY
    supported chapter source (the global ``/chapter`` feed is gap-unsafe).

    ``offset`` (clamped to >= 0) lets the caller page BACKWARD through the
    newest-first feed: the cog walks pages of ``limit`` until it reaches a chapter
    at or below its stored cursor, so a manga whose round-robin poll interval
    widened past a single page cannot silently skip the older overflow.
    """

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    url = "{base}/manga/{uuid}/feed".format(base=BASE_URL, uuid=mangadex_id)
    params = [
        ("translatedLanguage[]", str(language)),
        ("order[readableAt]", "desc"),
        ("limit", str(limit)),
        ("offset", str(offset)),
    ]
    return url, params, _headers()


# --- AniList -> MangaDex mapping --------------------------------------------


def _data_list(payload):
    """Return the candidate/chapter list from a MangaDex response or a bare list.

    Accepts either a full response dict (``{"data": [...]}``) or an already
    unwrapped list, so callers may pass ``payload`` or ``payload["data"]``.
    Anything else yields ``[]`` (never raises).
    """

    if isinstance(payload, dict):
        data = payload.get("data")
    else:
        data = payload
    return data if isinstance(data, list) else []


def pick_mapping(candidates, anilist_id):
    """Return the MangaDex UUID whose ``links.al`` equals ``anilist_id``, else None.

    ``candidates`` is a ``/manga`` search response (dict) or its ``data`` list.
    Every candidate is scanned - NEVER just the first - and the one whose
    ``attributes.links.al`` (a string on MangaDex) equals ``str(anilist_id)``
    wins, because relevance ranking does not put the wanted title first. A miss
    (``None``) is a legitimate recorded state for a niche title, not an error.
    Pure and total: malformed candidates are skipped, never raised on.
    """

    target = str(anilist_id)
    for candidate in _data_list(candidates):
        if not isinstance(candidate, dict):
            continue
        attrs = candidate.get("attributes")
        if not isinstance(attrs, dict):
            continue
        links = attrs.get("links")
        if not isinstance(links, dict):
            continue
        al = links.get("al")
        if al is not None and str(al) == target:
            uuid = candidate.get("id")
            if uuid:
                return uuid
    return None


# --- Feed normalisation -----------------------------------------------------


def reader_url(chapter):
    """The link an alert opens: the MangaDex reader, or an official stub's url.

    When ``externalUrl`` is set the chapter is a link-only stub (e.g. MangaPlus)
    with no MangaDex reader page, so the stub url is returned as-is. Otherwise the
    canonical ``mangadex.org/chapter/{id}`` reader url is built. Returns ``None``
    only when neither a stub url nor a chapter id is available.
    """

    external = chapter.get("externalUrl")
    if external:
        return external
    cid = chapter.get("id")
    if not cid:
        return None
    return "{base}/{id}".format(base=READER_BASE, id=cid)


def parse_chapter_feed(payload):
    """Normalise a per-manga feed response into a list of chapter dicts.

    Accepts the full response (dict with ``data``) or its ``data`` list. Each
    kept chapter is ``{id, volume, chapter, title, readableAt, translatedLanguage,
    externalUrl, url}`` where ``url`` is :func:`reader_url` (the stub url for an
    official-only chapter, else the MangaDex reader page). ``volume`` and
    ``chapter`` stay raw strings (``"38"`` / ``"110.5"``) or ``None``. An entry
    with no id or no ``attributes`` object is skipped, never raised on, so one
    malformed row cannot break a poll.
    """

    out = []
    for entry in _data_list(payload):
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        attrs = entry.get("attributes")
        if not cid or not isinstance(attrs, dict):
            continue
        chapter = {
            "id": cid,
            "volume": attrs.get("volume"),
            "chapter": attrs.get("chapter"),
            "title": attrs.get("title"),
            "readableAt": attrs.get("readableAt"),
            "translatedLanguage": attrs.get("translatedLanguage"),
            "externalUrl": attrs.get("externalUrl"),
        }
        chapter["url"] = reader_url(chapter)
        out.append(chapter)
    return out


# --- Identity & ordering helpers --------------------------------------------


def chapter_key(chapter):
    """The per-manga dedup identity of a chapter: its canonical number.

    The same logical chapter is uploaded once per scanlation group, each row a
    distinct chapter UUID. The volume is deliberately EXCLUDED from the
    identity: groups routinely disagree on it (one tags ``vol=2 ch=386``,
    another just ``ch=386``), and keying on the pair would alert the same
    chapter twice. The chapter number is canonicalised numerically so ``"386"``,
    ``386`` and ``"386.0"`` collapse; a non-numeric label keeps its stripped
    text. The rare series that restarts numbering per volume would collapse
    same-numbered chapters - a silently suppressed duplicate there beats the
    common double-alert here.

    A numberless row (oneshot, or a volume-only upload) falls back to
    ``("id", <chapter id>)`` to stay distinct rather than collapsing every
    oneshot together. Returns ``None`` only for a row with no identity at all
    (no number, no id), which the planner then skips. Pure and total.
    """

    number = chapter.get("chapter")
    if number is not None:
        text = str(number).strip()
        if text:
            parsed = _to_number(text)
            return ("ch", format(parsed, "g") if parsed is not None else text)
    cid = chapter.get("id")
    return ("id", str(cid)) if cid else None


def _to_number(value):
    """Parse a MangaDex volume/chapter string to a float, else ``None``.

    Handles decimals like ``"110.5"``; a missing value or non-numeric label (a
    named oneshot) yields ``None`` so the caller can order it deterministically.
    """

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def chapter_sort_key(chapter):
    """A total sort key ordering chapters by ``(volume, chapter)`` number.

    Numeric volume/chapter sort naturally (``"110.5"`` after ``"110"``); a null or
    non-numeric volume/chapter sorts LAST within its group (the ``missing`` flag),
    so numbered chapters come before numberless oneshots and the sort never raises
    on junk.
    """

    vol = _to_number(chapter.get("volume"))
    num = _to_number(chapter.get("chapter"))
    return (vol is None, vol or 0.0, num is None, num or 0.0)


def _to_epoch(value):
    """Coerce a ``readableAt`` value to comparable epoch seconds, else ``None``.

    Accepts an ISO-8601 string (``"...Z"`` or an explicit offset), a ``datetime``
    (what asyncpg hands back from a ``TIMESTAMPTZ`` cursor), or a raw unix number.
    A naive datetime/string is read as UTC. Junk yields ``None`` so the planner
    can skip an undateable row rather than crash. Pure and total.
    """

    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --- The dedup + cursor core ------------------------------------------------


def plan_chapter_alerts(chapters, cursor_readable_at, seen_keys):
    """Decide which chapters to alert, advancing the cursor and seen memory.

    ``chapters`` is a normalised feed (see :func:`parse_chapter_feed`), which
    MangaDex returns NEWEST-first; this reorders oldest-first internally so
    first-seen wins and the cursor advances monotonically. ``cursor_readable_at``
    is the last processed ``readableAt`` (an ISO string, ``datetime`` or epoch;
    ``None`` on the very first run). ``seen_keys`` is the bounded set of already
    alerted :func:`chapter_key` identities.

    Returns ``(alerts, new_cursor, new_seen_keys)``:

    * ``alerts`` - the chapter dicts to post, OLDEST-first.
    * ``new_cursor`` - the raw ``readableAt`` of the newest processed chapter (the
      value round-trips: same type as an incoming string), never regressed below
      the incoming cursor.
    * ``new_seen_keys`` - ``seen_keys`` plus every fresh key handled this tick.

    The rules, in order:

    * FIRST RUN (``cursor_readable_at`` is ``None``): anti-backfill anchor. Alert
      NOTHING, seed ``new_seen_keys`` with every current key, and set the cursor
      to the newest ``readableAt`` so the next tick starts clean.
    * A chapter is a candidate only when its ``readableAt`` is strictly newer than
      the incoming cursor (no backfilling below the cursor).
    * A key already in the seen memory is NEVER alerted, whatever its
      ``readableAt`` says - this defeats the late-re-upload trap (a second group's
      later ``readableAt`` for an already-alerted chapter) AND collapses the
      multiple same-tick group rows for one chapter into a single alert.

    Pure and total: rows with no dateable ``readableAt`` or no identity key are
    skipped. Pruning the seen memory (by age or per-manga count) is the caller's
    job; this only ever grows the set it was given.
    """

    cursor = _to_epoch(cursor_readable_at)
    first_run = cursor is None
    running_seen = set(seen_keys or ())

    # Pair each chapter with its epoch + key, dropping undateable / identity-less
    # rows, then order OLDEST-first so first-seen wins and the cursor is monotone.
    dated = []
    for chapter in chapters:
        ts = _to_epoch(chapter.get("readableAt"))
        key = chapter_key(chapter)
        if ts is None or key is None:
            continue
        dated.append((ts, key, chapter))
    dated.sort(key=lambda item: item[0])

    alerts = []
    new_cursor = cursor_readable_at
    top_ts = cursor  # highest epoch processed so far (starts at the cursor)

    for ts, key, chapter in dated:
        # A chapter below/at the cursor is old ground: skip it and do NOT touch the
        # seen memory (the cursor already guards that range).
        if not first_run and ts <= cursor:
            continue
        # Advance the cursor high-water mark to the newest fresh readableAt, and
        # carry its RAW value so the stored cursor round-trips the API's format.
        if top_ts is None or ts > top_ts:
            top_ts = ts
            new_cursor = chapter.get("readableAt")
        # Already-alerted (or already handled this tick) -> never re-alert. This is
        # the trap guard and the same-chapter multi-group dedup in one check.
        if key in running_seen:
            continue
        running_seen.add(key)
        if not first_run:
            alerts.append(chapter)

    return alerts, new_cursor, running_seen
