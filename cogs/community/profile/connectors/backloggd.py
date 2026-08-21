"""Purpose: the Backloggd connector - handle = Backloggd username, scraped.

Backloggd has no public API, so this module talks to the profile PAGE
(``https://www.backloggd.com/u/{username}/``) and reads it with BeautifulSoup.
The parsing below is a Python PORT of the selector logic in Backloggd-API
(https://github.com/Qewertyy/Backloggd-API, ``src/lib/user.ts`` and
``src/utils/game.ts``) - Copyright 2023 Qewertyy, MIT License - re-expressed
against ``bs4`` instead of ``cheerio`` but walking the exact same ids and
classes: ``#profile-favorites``, ``#profile-journal``, ``#profile-stats``,
``.review-card``, and the ``stars-top`` element whose
inline ``width:NN%`` style is Backloggd's only expression of a star rating
(``NN / 100 * 5``).

Scraping is a courtesy, not a right, so three things are deliberate:

* :func:`parse_profile` is PURE (no network) and every selector runs through
  ``bs4``'s ordinary ``None`` / empty-list behaviour when an id is missing -
  it never raises on a page that looks different than expected. A vendored
  fixture pins today's markup (``tests/fixtures/backloggd/*.html``); if
  Backloggd changes its layout, the FIXTURE TEST goes red (a human notices),
  while production degrades to less data or, if the whole page shape is gone,
  an empty section - never a crash. The framework's own
  ``render_sections`` (views.py) is the second line of defence: an exception
  from this connector's renderer still falls back to the "Linked" badge
  rather than taking the whole profile card down.
* :data:`REFRESH_TTL_SECONDS` is 12 hours - scraping is polite by being rare,
  and Backloggd offers nothing that ages faster than a daily habit anyway.
* Everything the parse keeps is BOUNDED before it can be stored: titles and
  dates are clipped, counters refused past nine digits (``int()`` itself
  raises past 4300), star widths clamped to 0-5 so no ``inf`` can become a
  JSON ``Infinity`` Postgres would reject, and urls filtered down to
  absolute http(s) so an unusable Thumbnail cannot make Discord refuse the
  whole profile card. A scraped page is third-party input, and
  ``profile_connections.payload`` has a hard 8 KiB cap that REFUSES the
  write - a hostile title must cost a truncated line, never the user's link.
* :func:`_fetch_profile_html` stamps the same identifiable ``User-Agent`` and
  ``Referer`` / ``Turbolinks-Referrer`` headers the upstream library uses
  (Backloggd's Rails app expects the Turbolinks header on a full page load),
  and treats a 404 as :class:`~.base.InvalidHandle` rather than "the site is
  down".

Like lastfm.py, this module takes its lazily-created
``aiohttp.ClientSession`` from the package's own ``sessions`` registry (the
``Connector`` interface carries no ``bot`` reference - see that module's
docstring for the full reasoning, and sessions.py's for who closes it) and
self-registers both its ``Connector`` and its section renderer at import
time, discovered by the package's existing auto-discovery.

``beautifulsoup4`` is imported ONLY in this module (a test pins that): no
other connector needs an HTML parser, and AniList/Steam/osu (a parallel P4
lot) talk JSON APIs.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse

import discord
from bs4 import BeautifulSoup

from .. import views as profile_views
from . import base, sessions
from tools.http import TIMEOUT
from tools.i18n import N_, _

log = logging.getLogger(__name__)

BASE_URL = "https://www.backloggd.com"

# Identifiable, same posture as tools/mangadex.py's USER_AGENT: a scraper
# should say who it is.
USER_AGENT = "Yasuho-DiscordBot/1.0 (+github.com/yaniswav/Yasuho; profile connector)"

# Backloggd usernames: alphanumeric plus underscore/hyphen. Offline shape
# check only - existence is confirmed by the profile-page round trip in
# :meth:`link`. Case is preserved (never lowercased): Backloggd's own URLs are
# case-preserving, and this is the exact string re-fetched on every refresh.
HANDLE_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{2,30}\Z")

# Scraping courtesy: refresh at most every 12 hours (see the module
# docstring). Metadata for the future scheduling hook - not enforced here,
# same posture as lastfm.py's REFRESH_TTL_SECONDS.
REFRESH_TTL_SECONDS = 12 * 60 * 60

# A Backloggd profile page is a few hundred KB. 2 MiB is generous next to
# that and puts a ceiling on what a misbehaving (or hostile) endpoint can make
# this process buffer: ``response.text()`` reads until EOF, so without a cap
# the only limit is whatever fits down the pipe inside the 15s timeout.
MAX_PAGE_BYTES = 2 * 1024 * 1024

# How many profile pages may be parsed OFF the event loop at once, and how long
# a parse waits for one of those slots.
#
# The 2 MiB cap above bounds what one parse can cost; it does not bound what
# that cost does to everything else. ``BeautifulSoup`` is a synchronous CPU
# walk, so parsing on the loop froze every guild - every command, every poller,
# every heartbeat - for as long as the walk took, and a page near the cap is
# seconds, not milliseconds. The parse therefore runs in a thread, and behind a
# ceiling: without one, a burst of ``/profile link backloggd`` would hand the
# default executor (shared with every Pillow render, see tools/rendering.py) as
# many threads as there are callers.
#
# The shape is copied from :func:`tools.rendering.run_image_job` - two slots, a
# few seconds of queue wait, and the WAIT is what times out (a thread already
# running cannot be cancelled, and unwinding the semaphore under it would break
# the ceiling it exists to enforce). That function itself cannot be reused here:
# it takes a ``bot`` for its semaphore and loop, and a ``Connector`` is built by
# a bare module import with no bot anywhere (the same reason this package owns
# its own session registry - see sessions.py).
#
# WHY THE DEFAULT EXECUTOR AND NOT A POOL OF ITS OWN, unlike
# cogs/utility/searchweb.py, which owns a two-thread pool for the same class of
# hazard. The doctrine is about how long a borrowed thread is HELD, not about
# who owns it. The semaphore above is what bounds concurrency, and it bounds it
# either way; the only question left is whether the two threads this can occupy
# are worth taking out of the shared pool. A soup walk is CPU that finishes -
# sub-second on a normal page, bounded by the byte cap on a hostile one - so
# borrowing is fine. The wiki lookup is a 15s NETWORK wait that would sit on a
# shared thread doing nothing, which is why that one pays for its own.
_PARSE_CONCURRENCY = 2
_PARSE_ACQUIRE_TIMEOUT = 5.0
_parse_semaphore = asyncio.Semaphore(_PARSE_CONCURRENCY)


async def parse_profile_off_loop(html):
    """:func:`parse_profile`, run in a thread behind the parse ceiling.

    Raises :exc:`asyncio.TimeoutError` when no slot came free in time - the
    caller degrades to the same typed "try later" a network failure gets, which
    is honest: a parse queue this deep means the answer would have been late
    anyway.
    """
    await asyncio.wait_for(_parse_semaphore.acquire(), _PARSE_ACQUIRE_TIMEOUT)
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, parse_profile, html)
    finally:
        _parse_semaphore.release()


async def _get_session():
    return await sessions.get_session("backloggd")


async def _read_bounded(response):
    """The response body as text, refused past :data:`MAX_PAGE_BYTES`.

    One byte over the cap is read on purpose - that is how a truncated read is
    told apart from a page that merely ends exactly there - and an oversized
    body is a typed :class:`~.base.ConnectorUnavailable`, not a silently
    half-parsed document.
    """
    raw = await response.content.read(MAX_PAGE_BYTES + 1)
    if len(raw) > MAX_PAGE_BYTES:
        raise base.ConnectorUnavailable("backloggd", "remote")
    return raw.decode(getattr(response, "charset", None) or "utf-8", errors="replace")


async def _fetch_profile_html(username):
    """GET the profile page; return its HTML text or raise a typed error."""
    # Quoted even though HANDLE_PATTERN already refuses anything outside
    # [A-Za-z0-9_-]: this function is also reached from refresh(), whose
    # handle comes back out of the database rather than through link()'s
    # validation, and a path segment is the one place a stray '/' or '?'
    # would silently retarget the request.
    quoted = urllib.parse.quote(str(username), safe="")
    referer = "{base}/search/users/{user}".format(base=BASE_URL, user=quoted)
    headers = {
        "User-Agent": USER_AGENT,
        "Turbolinks-Referrer": referer,
        "Referer": referer,
        "Accept": "text/html",
    }
    session = await _get_session()
    try:
        async with session.get(
            "{base}/u/{user}".format(base=BASE_URL, user=quoted),
            headers=headers,
            timeout=TIMEOUT,
        ) as response:
            if response.status == 404:
                raise base.InvalidHandle("backloggd", "not_found")
            if response.status != 200:
                raise base.ConnectorUnavailable("backloggd", "remote")
            return await _read_bounded(response)
    except base.ConnectorError:
        raise
    except Exception as exc:  # timeout / connection reset / ...
        raise base.ConnectorUnavailable("backloggd", "remote") from exc


# ---------------------------------------------------------------------------
# Pure parsing (ported from Backloggd-API's user.ts / game.ts). No network -
# unit-tested directly against the vendored fixtures.
# ---------------------------------------------------------------------------

# The number is BOUNDED in length and read as one blob of digits-and-dots: an
# unbounded ``\d+`` on a mangled page happily matches ten thousand nines,
# float()s that to ``inf``, and ships a JSON ``Infinity`` - which json.dumps
# writes without complaint and Postgres' jsonb cast then REJECTS, turning a
# scraped oddity into a failed link. Splitting the digits instead (``\d{1,3}``)
# would be worse than useless: "1000%" would quietly match its own tail as
# "000" and yield a confident 0.0. Whatever this captures is validated by
# :func:`_calculate_rating`, which refuses anything outside 0-5.
_WIDTH_RE = re.compile(r"width:\s*([0-9.]{1,12})%")

# 3 items is what the profile card has room for (favourites and recently
# played alike); Backloggd itself only ever shows a handful in each block.
_LIST_CAP = 3

# ---------------------------------------------------------------------------
# Bounds. Everything below comes from a page a third party serves, and lands
# in ``profile_connections.payload``, which base.PAYLOAD_MAX_BYTES caps at
# 8 KiB - a cap that REJECTS the whole write when it is exceeded. A single
# absurd ``alt`` attribute must cost a truncated title, never the user's
# link, so each field is clipped at the parse instead of being trusted and
# discovered too late.
# ---------------------------------------------------------------------------

_NAME_MAX = 120
_DATE_MAX = 32
# Backloggd shows three stat counters today; a redesign that grows the block
# must not grow the payload without bound (see _stat_key's fallback slug).
_STATS_MAX = 8
# Reviews are read for their ratings only (see _merge_review_ratings) and
# never stored, but the walk itself is over third-party markup.
_REVIEW_CAP = 12
# Nine digits is a billion games played - past that it is not a counter.
_COUNT_DIGITS_MAX = 9


def _clip(value, limit):
    """A bounded, stripped string, or ``None`` when there is nothing left."""
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit]


# The framework's own filter, consumed rather than restated: absolute http(s)
# or nothing, and over-long is DROPPED (half a url is not a url). Every
# connector in this package uses the same one, on both sides of the payload.
_safe_url = base.safe_url


def _calculate_rating(style):
    """``stars-top``'s inline ``width:NN%`` -> a rating out of 5, or ``None``.

    Ported from ``calculateRating`` in game.ts: the site expresses a star
    rating as nothing but this percentage width. Clamped to the 0-5 range the
    stars can actually mean, so a broken style attribute cannot put a number
    the card would print as "12.5/5" into the payload.
    """
    match = _WIDTH_RE.search(style or "")
    if not match:
        return None
    try:
        percentage = float(match.group(1))
    except ValueError:
        return None
    rating = round(percentage / 100 * 5, 1)
    if rating < 0 or rating > 5:
        return None
    return rating


def _extract_game(element):
    """Port of ``extractGame``: name + cover from ``div.overflow-wrapper``,
    an optional ``played-date`` and an optional static star rating.

    Returns ``None`` when there is no name/image pair - a game card that
    Backloggd itself would not consider real, never raised on.
    """
    wrapper = element.select_one("div.overflow-wrapper")
    img = wrapper.find("img") if wrapper is not None else None
    name = img.get("alt") if img is not None else None
    image = img.get("src") if img is not None else None
    if not name or not image:
        return None
    clipped = _clip(name, _NAME_MAX)
    if clipped is None:
        return None
    game = {"name": clipped}
    # The cover is kept for a future renderer, but only when it is a URL the
    # card could actually be handed - see :func:`_safe_url`. Its absence never
    # drops the game, which the name alone makes real.
    cover = _safe_url(image)
    if cover is not None:
        game["image"] = cover
    date_el = element.select_one("p.mb-0.played-date")
    if date_el is not None:
        date_text = _clip(date_el.get_text(strip=True), _DATE_MAX)
        if date_text:
            game["date"] = date_text
    stars_top = element.select_one("div.star-ratings-static div.stars-top")
    if stars_top is not None:
        rating = _calculate_rating(stars_top.get("style"))
        if rating is not None:
            game["rating"] = rating
    return game


def _extract_recent_reviews(soup):
    """Port of ``extractRecentReviews``: every ``.review-card`` in the page,
    with its rating and the (collapsed) review text."""
    reviews = []
    for card in soup.select(".review-card")[:_REVIEW_CAP]:
        body = card.select_one("div[review_id]") or card.select_one(
            ".review-body"
        )
        review_id = body.get("review_id") if body is not None else None
        game = _extract_game(card)
        if not review_id or game is None:
            continue
        entry = dict(game)
        # `find(id=...)` and NOT `select_one("#collapseReview" + review_id)`:
        # the id is an attribute a third party wrote, and interpolating it
        # into a CSS selector lets a page with `review_id="1 ,x["` raise
        # soupsieve's SelectorSyntaxError straight out of a function whose
        # whole contract is that it never raises. This form compares the
        # attribute as a plain string - nothing to inject into.
        text_el = card.find(id="collapseReview{id}".format(id=review_id))
        entry["review"] = text_el.get_text(strip=True) if text_el is not None else ""
        reviews.append(entry)
    return reviews


def _stat_key(label):
    """Best-effort normalisation of a stats-block label to a stable key.

    Backloggd's three counters are "Games Played", "Played in <year>" and
    "Games Backloggd" today; a future label the site adds falls back to a
    slug of itself rather than being dropped, so a redesign degrades to an
    ugly-but-present key instead of silently losing a stat.
    """
    text = (label or "").strip()[:_NAME_MAX]
    if text.startswith("Games Played"):
        return "played"
    if text.startswith("Played in"):
        return "played_this_year"
    if text.startswith("Games Backloggd"):
        return "backlog"
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "stat"


def _extract_stats(soup):
    """Port of the ``userStatsDiv.each`` loop in user.ts: each stat block's
    ``h1`` (the count, inside an ``a``) paired with its ``h4`` (the label)."""
    stats = {}
    container = soup.select_one("#profile-stats")
    if container is None:
        return stats
    for child in container.find_all(recursive=False):
        if len(stats) >= _STATS_MAX:
            break
        anchor = child.find("a", recursive=False)
        h1 = anchor.find("h1", recursive=False) if anchor is not None else None
        h4 = child.find("h4", recursive=False)
        if h1 is None or h4 is None:
            continue
        digits = re.sub(r"\D", "", h1.get_text(strip=True))
        # Length-checked BEFORE int(): CPython refuses to parse an integer
        # literal past sys.int_info.str_digits_check_threshold (4300 by
        # default) and raises ValueError, so a mangled counter would escape
        # this "never raises" parse. Nothing real has ten digits either.
        if not digits or len(digits) > _COUNT_DIGITS_MAX:
            continue
        stats[_stat_key(h4.get_text(strip=True))] = int(digits)
    return stats


def _extract_favorites(soup):
    """Port of the ``favoritesDiv.each`` loop: up to 3 favourite games, the
    ``ultimate_fav`` one (if within the first 3) flagged ``most_favorite``."""
    container = soup.select_one("#profile-favorites")
    if container is None:
        return []
    favorites = []
    for child in container.find_all(recursive=False):
        if len(favorites) >= _LIST_CAP:
            break
        game = _extract_game(child)
        if game is None:
            continue
        classes = child.get("class") or []
        if "ultimate_fav" in classes:
            game["most_favorite"] = True
        favorites.append(game)
    return favorites


def _extract_journal(soup):
    """Port of the ``recentlyPlayedDiv.each`` loop: up to 3 recently played
    games (name, cover, played date, and a rating when Backloggd renders one
    statically alongside the entry)."""
    container = soup.select_one("#profile-journal")
    if container is None:
        return []
    played = []
    for child in container.find_all(recursive=False):
        if len(played) >= _LIST_CAP:
            break
        game = _extract_game(child)
        if game is not None:
            played.append(game)
    return played


def _merge_review_ratings(recently_played, reviews):
    """Best-effort: when a recently-played game was ALSO recently reviewed,
    attach that review's rating - so "recently played" can carry a note
    (Backloggd's inline ``rating-hover`` journal entries rarely expose a
    static rating of their own; the ``.review-card`` block almost always
    does) without inventing a second top-level list the payload spec did not
    ask for. Purely additive: a game with its own inline rating already keeps
    it, and a miss leaves the entry exactly as :func:`_extract_game` built it.
    """
    by_name = {
        review["name"]: review["rating"]
        for review in reviews
        if review.get("rating") is not None
    }
    for game in recently_played:
        if "rating" not in game and game.get("name") in by_name:
            game["rating"] = by_name[game["name"]]
    return recently_played


def _avatar(soup):
    meta = soup.select_one("meta[property='og:image']")
    content = meta.get("content") if meta is not None else None
    return _safe_url(content)


def _display_name(soup):
    header = soup.select_one(".main-header")
    text = header.get_text(strip=True) if header is not None else ""
    return _clip(text, base.DISPLAY_NAME_MAX)


def parse_profile(html):
    """The whole profile page -> a display-ready dict. Pure; never raises on
    a page that does not match today's markup (see the module docstring)."""
    soup = BeautifulSoup(html or "", "html.parser")
    reviews = _extract_recent_reviews(soup)
    recently_played = _merge_review_ratings(_extract_journal(soup), reviews)
    return {
        "display_name": _display_name(soup),
        "avatar": _avatar(soup),
        "stats": _extract_stats(soup),
        "favorites": _extract_favorites(soup),
        "recently_played": recently_played,
    }


def _one_line(text):
    """Flatten to one line - a scraped game title riding into a Components V2
    card, same discipline as views.py's own ``_one_line``."""
    return " ".join(str(text).split())


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class BackloggdConnector(base.Connector):
    name = "backloggd"
    handle_hint = N_("your Backloggd username")

    async def _fetch(self, handle):
        html = await _fetch_profile_html(handle)
        # :func:`parse_profile` is written to degrade rather than raise, and
        # is pinned to real markup by the fixtures - but it walks a document
        # this bot does not control, so the belt gets a pair of braces: any
        # surprise from bs4 or from a page shape nobody anticipated becomes
        # the same typed "not your fault, try later" the network path already
        # raises, instead of an untyped traceback the cog can only answer
        # with its generic failure.
        try:
            parsed = await parse_profile_off_loop(html)
        except asyncio.TimeoutError:
            log.warning(
                "Backloggd parse slots are saturated; refusing %r for now", handle
            )
            raise base.ConnectorUnavailable("backloggd", "remote") from None
        except Exception as exc:
            log.exception("Failed to parse the Backloggd profile page for %r", handle)
            raise base.ConnectorUnavailable("backloggd", "remote") from exc
        payload = {
            key: value for key, value in parsed.items() if key != "display_name"
        }
        return payload, parsed.get("display_name")

    async def link(self, user_id, raw_input):
        handle = (raw_input or "").strip()
        if not HANDLE_PATTERN.match(handle):
            raise base.InvalidHandle(self.name, "format")
        payload, display_name = await self._fetch(handle)
        return base.LinkResult(
            external_id=handle,
            display_name=display_name or handle,
            payload=payload,
        )

    async def refresh(self, user_id, connection):
        handle = connection.get("external_id") or ""
        if not handle:
            raise base.NotLinked(self.name)
        payload, _display_name = await self._fetch(handle)
        return payload


base.register(BackloggdConnector())


# ---------------------------------------------------------------------------
# The renderer: draws from ``connection["payload"]`` only, never the network.
# ---------------------------------------------------------------------------


def _format_stats_line(stats):
    parts = []
    if "played" in stats:
        parts.append(_("{count} played").format(count=stats["played"]))
    if "backlog" in stats:
        parts.append(_("{count} backlogged").format(count=stats["backlog"]))
    return " - ".join(parts)


def _format_game_names(games):
    shown = []
    for game in games[:_LIST_CAP]:
        name = game.get("name") if isinstance(game, dict) else None
        if not name:
            continue
        rating = game.get("rating")
        # isinstance and not `is not None`: the rating comes back out of a
        # jsonb column, and the ``:.1f`` below is a ValueError on anything a
        # past version (or a hand-edited row) may have left there.
        if isinstance(rating, (int, float)) and not isinstance(rating, bool):
            shown.append(
                "{name} ({rating:.1f}/5)".format(name=_one_line(name), rating=rating)
            )
        else:
            shown.append(_one_line(name))
    return shown


async def _render(container, field, viewer, connection, budget):
    payload = connection.get("payload") or {}
    lines = ["**" + _(field.label) + "**"]

    stats = payload.get("stats")
    stats_line = _format_stats_line(stats if isinstance(stats, dict) else {})
    if stats_line:
        lines.append(stats_line)

    favorites = _format_game_names(payload.get("favorites") or [])
    if favorites:
        # The msgid anilist.py already ships, verbatim (spelling included):
        # one string for translators instead of two that differ by a 'u'.
        lines.append(_("Favourites: {titles}").format(titles=", ".join(favorites)))

    recent = _format_game_names(payload.get("recently_played") or [])
    if recent:
        lines.append(_("Recently played: {names}").format(names=", ".join(recent)))

    text = discord.ui.TextDisplay("\n".join(lines))
    # Re-checked here and not only at the parse: the payload is a row that was
    # written by a PAST version of this module, and an unusable Thumbnail url
    # is the one failure this renderer cannot be forgiven for - it is rejected
    # by Discord when the card is SENT, long after render_sections' fallback
    # to the badge could still save the profile.
    avatar = _safe_url(payload.get("avatar"))
    if avatar:
        container.add_item(discord.ui.Section(text, accessory=discord.ui.Thumbnail(avatar)))
    else:
        container.add_item(text)


profile_views.register_section_renderer("backloggd", _render)
