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
* ``translatedLanguage[]`` REPEATS to mean OR (live-verified 2026-07-24), so one
  request covers a whole union of languages - a wider union costs params and rows,
  never an extra request. It is a filter on the REQUEST only: the alert identity
  above deliberately ignores the language, so a chapter alerts ONCE, at its first
  appearance in any requested language, and its later translations are silently
  deduplicated. That is what keeps the at-most-once guarantee intact while readers
  of different languages share one feed poll; the residual limitation is that a
  French reader can be alerted by the English release when it lands first (the link
  is then re-pointed per recipient, best-effort, by :func:`pick_variant`).
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

# Default translation language, and the SAFETY NET of every feed request: it is
# always part of the requested union (see :func:`feed_languages`), so a manga whose
# trackers all read some other language still surfaces its English releases and a
# tracker with no (or an unreadable) preference is never left with an empty feed.
DEFAULT_LANGUAGE = "en"

# The translation languages Yasuho offers, in picker order, as ``(code, name)``.
# The code is MangaDex's ``translatedLanguage`` value; the name is DATA, not prose
# - a stable ASCII hint shown beside the code in the picker - so it carries no
# ``_()`` and adds no msgid (this module is translation-free by design, see the
# module docstring).
#
# Every code here was verified live (2026-07-24) against
# ``GET /chapter?translatedLanguage[]=<code>``. That check also pinned WHY the list
# is a closed set rather than free text: MangaDex rejects a three-letter code with
# a 400 (``fil``), and silently accepts an unknown two-letter one (``xx``) while
# returning nothing at all - so an unvalidated value would either break a poll or,
# worse, turn it into a permanently empty feed. :func:`normalize_language` refuses
# anything outside this tuple.
LANGUAGES = (
    ("en", "English"),
    ("es", "Spanish"),
    ("es-la", "Spanish (Latin America)"),
    ("pt-br", "Portuguese (Brazil)"),
    ("pt", "Portuguese"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("ru", "Russian"),
    ("uk", "Ukrainian"),
    ("pl", "Polish"),
    ("tr", "Turkish"),
    ("ar", "Arabic"),
    ("id", "Indonesian"),
    ("vi", "Vietnamese"),
    ("th", "Thai"),
    ("zh", "Chinese (Simplified)"),
    ("zh-hk", "Chinese (Traditional)"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("el", "Greek"),
)

_LANGUAGE_NAMES = dict(LANGUAGES)

# How many languages one feed request may ask for. The union of a manga's trackers
# rides a SINGLE request (repeated ``translatedLanguage[]`` pairs are an OR), so a
# wider union costs no extra request - but it does dilute the fixed page window with
# more rows per logical chapter, so the union is bounded and the extra languages are
# dropped least-requested-first (:func:`feed_languages` orders by demand). Four
# covers every realistic mixed-language tracker set while keeping
# :func:`feed_page_limit` well inside MangaDex's 100-row page cap.
MAX_FEED_LANGUAGES = 4

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


def manga_feed_request(mangadex_id, languages=None, limit=None, offset=0):
    """Build the per-manga chapter-feed request for one manga UUID.

    Returns ``(url, params, headers)`` for
    ``GET /manga/{uuid}/feed?translatedLanguage[]=<lang>...&order[readableAt]=desc``.
    The response is normalised by :func:`parse_chapter_feed`. This is the ONLY
    supported chapter source (the global ``/chapter`` feed is gap-unsafe).

    ``languages`` is the union of translation languages this manga's trackers need
    (a single code or an iterable; ``None`` means English only). It is normalised,
    deduplicated and bounded by :func:`feed_languages` /
    :data:`MAX_FEED_LANGUAGES`, and emitted as one repeated
    ``translatedLanguage[]`` pair per language - which MangaDex reads as an OR
    (live-verified 2026-07-24: a two-language request returns both, interleaved by
    ``readableAt``). Widening the union therefore costs ZERO extra requests; it only
    adds params and rows.

    ``limit`` defaults to :func:`feed_page_limit` for that union, so a
    multi-language feed still covers about :data:`FEED_LIMIT` distinct chapters per
    page instead of a fraction of it. Both are clamped to MangaDex's 1..100 window.

    ``offset`` (clamped to >= 0) lets the caller page BACKWARD through the
    newest-first feed: the cog walks pages of ``limit`` until it reaches a chapter
    at or below its stored cursor, so a manga whose round-robin poll interval
    widened past a single page cannot silently skip the older overflow.
    """

    langs = feed_languages(languages)[:MAX_FEED_LANGUAGES]
    if limit is None:
        limit = feed_page_limit(langs)
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    url = "{base}/manga/{uuid}/feed".format(base=BASE_URL, uuid=mangadex_id)
    params = [("translatedLanguage[]", lang) for lang in langs]
    params.extend(
        [
            ("order[readableAt]", "desc"),
            ("limit", str(limit)),
            ("offset", str(offset)),
        ]
    )
    return url, params, _headers()


# --- Translation languages ---------------------------------------------------


def _as_codes(value):
    """Coerce a language argument to a list: ``None`` -> ``[]``, a str -> one code."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def language_name(code):
    """The display name for a language code, falling back to the code itself."""

    return _LANGUAGE_NAMES.get(str(code or "").strip().lower(), code)


def normalize_language(code):
    """Canonicalise a stored/preferred language code, or ``None`` when unusable.

    Accepts what the settings blobs and the i18n locales actually hold: any case,
    and either separator (``pt_BR``, ``pt-br``). A regional code we do not offer
    falls back to its base language when that IS offered (``es-mx`` -> ``es``), so a
    Discord-side locale maps to something MangaDex can serve. Anything outside
    :data:`LANGUAGES` yields ``None`` - never a pass-through - because MangaDex
    answers an unknown two-letter code with a silently EMPTY feed (verified live),
    which would look exactly like "no new chapters" forever. Pure and total.
    """

    if not code:
        return None
    text = str(code).strip().lower().replace("_", "-")
    if text in _LANGUAGE_NAMES:
        return text
    base = text.split("-", 1)[0]
    if base in _LANGUAGE_NAMES:
        return base
    return None


def feed_languages(codes):
    """The ordered language union one manga's feed request should ask for.

    ``codes`` is whatever its trackers want (DM readers' preferences and the guild
    language of each subscribed channel), with duplicates, junk and ``None`` all
    welcome. Returns :data:`DEFAULT_LANGUAGE` FIRST - it is the safety net and is
    always requested - then the other valid languages ordered by how many trackers
    asked for them (ties broken by code, so the result is fully deterministic).

    That demand ordering is what makes the :data:`MAX_FEED_LANGUAGES` clamp fair:
    the caller keeps the head of this list, so the languages dropped from an
    unusually diverse tracker set are always the least-requested ones. The full
    (unclamped) list is returned so the caller can SEE that it truncated and log it.
    Pure and total.
    """

    counts = {}
    for code in _as_codes(codes):
        lang = normalize_language(code)
        if lang is None or lang == DEFAULT_LANGUAGE:
            continue
        counts[lang] = counts.get(lang, 0) + 1
    ordered = sorted(counts, key=lambda lang: (-counts[lang], lang))
    return [DEFAULT_LANGUAGE] + ordered


def feed_page_limit(languages, base=FEED_LIMIT):
    """Rows to pull per feed page so a wider union still covers ~``base`` chapters.

    One logical chapter already occupies one row PER scanlation group; asking for N
    languages multiplies that again, so a fixed page would cover a fraction of the
    chapters it covers today and push the backward pagination into its page cap
    sooner. Scaling the page size with the union keeps the chapter coverage (and
    therefore the catch-up window) roughly constant for the SAME single request -
    more rows in one response, never more responses. Clamped to MangaDex's 100-row
    maximum. Pure and total.
    """

    count = max(1, min(len(_as_codes(languages)) or 1, MAX_FEED_LANGUAGES))
    return max(1, min(int(base) * count, 100))


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


def index_variants(chapters):
    """Index one manga's fetched feed as ``{chapter_key: {language: chapter}}``.

    A multi-language feed carries the SAME logical chapter once per language (and
    once per scanlation group within a language). The alert identity stays the
    chapter number alone - :func:`plan_chapter_alerts` is untouched and still fires
    once, at the first appearance in ANY requested language - so this index exists
    only for PRESENTATION: it lets the fan-out hand each recipient the row in their
    own language when this same fetch happened to contain it (see
    :func:`pick_variant`). The first row wins per language, i.e. the newest upload
    (the feed is ``readableAt`` DESC). Rows with no identity or no language are
    skipped. Pure and total.
    """

    out = {}
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        key = chapter_key(chapter)
        language = chapter.get("translatedLanguage")
        if key is None or not language:
            continue
        out.setdefault(key, {}).setdefault(str(language).strip().lower(), chapter)
    return out


def pick_variant(variants, chapter, language):
    """The ``language`` row of ``chapter``'s identity, else ``chapter`` unchanged.

    Best-effort by design: the alert has ALREADY been decided (and its identity
    already persisted as seen) for the whole audience, so this only ever swaps the
    link/metadata shown to one recipient. When their language is not in
    ``variants`` - it was not in this fetch's window, or has not been released yet -
    they simply get the row the alert fired on, never nothing. Pure and total.
    """

    lang = normalize_language(language)
    if lang is None:
        return chapter
    key = chapter_key(chapter) if isinstance(chapter, dict) else None
    if key is None:
        return chapter
    return (variants.get(key) or {}).get(lang) or chapter


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
