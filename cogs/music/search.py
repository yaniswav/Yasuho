"""Premium music search: the /search LavaSearch browser and the /play picker.

This module owns two opt-in-friendly search surfaces layered on top of the
existing playback engine, and nothing else:

* ``/search <query>`` opens :class:`SearchBrowser`, a Components V2 tabbed
  browser (Tracks / Albums / Artists / Playlists) fed by the Lavalink
  **LavaSearch** plugin. Each tab is a select of up to twenty entries; picking
  one routes its URI through the cog's EXACT ``_play_query`` seam, so a search
  pick queues identically to a plain ``/play <url>``. The browser is ephemeral
  and author-gated and stays open for repeated picks.
* An OPT-IN ``/play`` picker: when a member has turned on the "Play search
  picker" preference AND ``/play`` receives a NON-URL query, they get a compact
  ephemeral select of the top five track matches instead of the first result
  being queued outright. URLs always bypass it, and with the preference OFF
  (the default) ``/play`` behaves byte-for-byte as before.

Layering. This module never imports the music engine or its UI at import time
(only ``discord``, ``sonolink``-agnostic duck typing, and shared ``tools``), so
it stays import-safe and unit-testable in isolation. It drives playback purely
through the ``cog`` handed to its coroutines (``cog._search`` for the plain
track loader, ``cog._play_query`` for the connect/queue/snapshot seam,
``cog._nodes_available`` and ``cog.bot``). The one interaction adapter it needs,
``_ModalPlayContext``, is imported lazily inside the pick callbacks (which only
run once the bot - and therefore the whole ``cogs.music`` package - is loaded),
so no import cycle is created and the module can be imported before the engine.

Fetch seams. sonolink exposes NO native LavaSearch call - its high-level
``search_track`` maps to Lavalink ``/loadtracks`` (single-type search), while the
plugin lives at ``/loadsearch`` (multi-type). So the browser reaches the plugin
through ``node.send("GET", "/loadsearch", ...)`` - the same authenticated,
credential-free REST seam the SponsorBlock integration uses - and parses the raw
JSON into bounded, immutable :class:`SearchResults`. The default query prefix is
``spsearch`` (the only source that returns all four result types); a Spotify
outage (``/loadsearch`` errors) degrades to a tracks-only ``ytsearch`` result via
the plain loader, with the other tabs showing a friendly unavailable note, so a
Spotify hiccup never kills ``/search`` outright.

Scale. Every structure here is per-invocation and bounded: LavaSearch returns at
most twenty entries per type, and the parser clamps to ``MAX_PER_TAB`` regardless,
so a :class:`SearchResults` holds at most eighty small frozen ``Entry`` records
and a browser view holds one Container with four buttons and one <=20-option
select. There is no shared/global state, no cache, no background task and no
timer beyond each view's finite ``timeout`` (after which it disables and is
garbage-collected). ``/search`` is one ``/loadsearch`` round trip (plus at most
one fallback ``/loadtracks`` on a Spotify outage); the ``/play`` picker adds, for
a NON-URL slash ``/play`` only, a single in-process-cached preference read and
one ``/loadtracks`` it would have made anyway. All the option/label/prefix/pick
logic is pure and O(entries).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import discord

from tools import interactions, settings
from tools.formats import random_colour
from tools.i18n import N_, _, ngettext
from tools.views import AuthorLayoutView, AuthorView

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# LavaSearch returns up to 20 entries per type; clamp defensively so a select
# never exceeds Discord's 25-option ceiling and the structures stay bounded.
MAX_PER_TAB = 20

# The /play picker offers the top five track matches (Discord shows them all
# without scrolling and it mirrors "the first result" -> "one of the first few").
PLAY_PICKER_LIMIT = 5

# Per-user preference gating the /play picker. Mirrors AUTOPLAY_PREF_KEY: the key
# is a bare literal shared with cogs/community/usersettings.py's PREFS entry, and
# both sides read/write it by this literal. Default OFF so /play is unchanged
# until a member opts in.
SEARCH_PICKER_PREF_KEY = "music_search_picker"
PICKER_DEFAULT = False

# Query prefixes. spsearch is the only source LavaSearch fills with all four
# result types (live-verified: 20 tracks/albums/artists/playlists); a ytsearch
# /loadsearch returns only text suggestions, so the ytsearch degrade path uses
# the plain /loadtracks loader (cog._search) for tracks instead.
DEFAULT_SEARCH_PREFIX = "spsearch"
FALLBACK_SEARCH_PREFIX = "ytsearch"

# The plugin endpoint and the result types we ask for. node.send prefixes "/v4"
# to a leading-slash path (see sponsorblock.categories_path), yielding
# "/v4/loadsearch?query=...&types=track,album,artist,playlist".
LOADSEARCH_PATH = "/loadsearch"
LAVASEARCH_TYPES = ("track", "album", "artist", "playlist")
LAVASEARCH_TYPES_PARAM = ",".join(LAVASEARCH_TYPES)

# Discord select-option field caps.
_LABEL_LIMIT = 100
_DESC_LIMIT = 100

# Matches a URL scheme like "http://" / "https://" so /play can bypass the picker
# for anything Lavalink will resolve as a direct link rather than a text search.
_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)

# Tab identifiers, order, labels and emojis for the browser's segmented control.
TAB_TRACKS = "tracks"
TAB_ALBUMS = "albums"
TAB_ARTISTS = "artists"
TAB_PLAYLISTS = "playlists"
_TAB_ORDER = (TAB_TRACKS, TAB_ALBUMS, TAB_ARTISTS, TAB_PLAYLISTS)
_TAB_LABELS = {
    TAB_TRACKS: N_("Tracks"),
    TAB_ALBUMS: N_("Albums"),
    TAB_ARTISTS: N_("Artists"),
    TAB_PLAYLISTS: N_("Playlists"),
}
_TAB_EMOJI = {
    TAB_TRACKS: "\U0001f3b5",  # musical note
    TAB_ALBUMS: "\U0001f4bf",  # optical disc
    TAB_ARTISTS: "\U0001f3a4",  # microphone
    TAB_PLAYLISTS: "\U0001f4dc",  # scroll
}


# ---------------------------------------------------------------------------
# Parsed result structures (bounded, immutable, locale-free)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One playable/loadable search result across every tab.

    ``title`` is a track title or a collection name; ``subtitle`` is the author /
    creator (the bare artist name for an artist entry); ``uri`` is the URL that
    the play seam resolves (a track URL loads one track, an album/artist/playlist
    URL loads as a playlist - all live-verified). ``total_tracks`` is set for
    collections, ``length_ms`` for tracks. Every field is raw API data with no
    translatable fallback baked in, so parsing stays pure and locale-independent;
    the display fallbacks live at render time.
    """

    title: str
    subtitle: str
    uri: str
    artwork: str | None = None
    total_tracks: int | None = None
    length_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SearchResults:
    """The four result lists plus a Spotify-degraded flag.

    Each list is a tuple clamped to ``MAX_PER_TAB`` at parse time, so the whole
    record is bounded and hashable. ``degraded`` is True only when a Spotify
    outage forced the tracks-only ytsearch fallback, which the browser turns into
    a friendly note on the album/artist/playlist tabs.
    """

    tracks: tuple[Entry, ...] = ()
    albums: tuple[Entry, ...] = ()
    artists: tuple[Entry, ...] = ()
    playlists: tuple[Entry, ...] = ()
    degraded: bool = False

    def has_any(self) -> bool:
        return bool(self.tracks or self.albums or self.artists or self.playlists)

    def entries_for(self, tab: str) -> tuple[Entry, ...]:
        return {
            TAB_TRACKS: self.tracks,
            TAB_ALBUMS: self.albums,
            TAB_ARTISTS: self.artists,
            TAB_PLAYLISTS: self.playlists,
        }.get(tab, ())


# ---------------------------------------------------------------------------
# Pure parsing (pinned against recorded live fixtures in the tests)
# ---------------------------------------------------------------------------


def parse_search_results(raw: object) -> SearchResults:
    """Parse a raw LavaSearch ``/loadsearch`` payload into :class:`SearchResults`.

    Defensive by construction: a non-dict payload (a 204 decodes to ``None``), a
    missing/!list category, a non-dict item or an item without a usable URI are
    all skipped rather than raising, and every category is clamped to
    ``MAX_PER_TAB``. Never degraded - the fetch layer sets that flag.
    """
    if not isinstance(raw, dict):
        return SearchResults()
    return SearchResults(
        tracks=_parse_list(raw.get("tracks"), _parse_track_item),
        albums=_parse_list(raw.get("albums"), _parse_collection_item),
        artists=_parse_list(raw.get("artists"), _parse_collection_item),
        playlists=_parse_list(raw.get("playlists"), _parse_collection_item),
    )


def _parse_list(items: object, parse_one) -> tuple[Entry, ...]:
    if not isinstance(items, list):
        return ()
    out: list[Entry] = []
    for item in items[:MAX_PER_TAB]:
        entry = parse_one(item)
        if entry is not None:
            out.append(entry)
    return tuple(out)


def _parse_track_item(item: object) -> Entry | None:
    """A LavaSearch track: display fields live under ``info`` (uri required)."""
    if not isinstance(item, dict):
        return None
    info = item.get("info")
    if not isinstance(info, dict):
        return None
    uri = info.get("uri")
    if not uri or not isinstance(uri, str):
        return None
    return Entry(
        title=info.get("title") or "",
        subtitle=info.get("author") or "",
        uri=uri,
        artwork=info.get("artworkUrl"),
        length_ms=info.get("length") if isinstance(info.get("length"), int) else None,
    )


def _parse_collection_item(item: object) -> Entry | None:
    """An album/artist/playlist: name under ``info``, the rest under ``pluginInfo``.

    The loadable URL lives at ``pluginInfo.url`` (an item without one is
    unplayable and dropped). The bare author/creator and track count come from
    ``pluginInfo`` too.
    """
    if not isinstance(item, dict):
        return None
    info = item.get("info") if isinstance(item.get("info"), dict) else {}
    plugin = item.get("pluginInfo") if isinstance(item.get("pluginInfo"), dict) else {}
    uri = plugin.get("url")
    if not uri or not isinstance(uri, str):
        return None
    total = plugin.get("totalTracks")
    return Entry(
        title=info.get("name") or "",
        subtitle=plugin.get("author") or "",
        uri=uri,
        artwork=plugin.get("artworkUrl"),
        total_tracks=total if isinstance(total, int) else None,
    )


def entry_from_playable(track: object) -> Entry | None:
    """Adapt a sonolink ``Playable`` into an :class:`Entry` (duck-typed).

    Used for the ytsearch degrade path and the /play picker, whose tracks come
    from the plain ``/loadtracks`` loader as ``Playable`` objects rather than raw
    LavaSearch dicts. Reads only the public ``Playable`` surface, so tests can
    pass simple stand-ins. A track without a URI is unplayable and dropped.
    """
    uri = getattr(track, "uri", None)
    if not uri:
        return None
    return Entry(
        title=getattr(track, "title", "") or "",
        subtitle=getattr(track, "author", "") or "",
        uri=uri,
        artwork=getattr(track, "artwork", None),
        length_ms=getattr(track, "length", None),
    )


# ---------------------------------------------------------------------------
# Pure decisions and rendering helpers
# ---------------------------------------------------------------------------


def build_query(prefix: str, raw: str) -> str:
    """Prefix a raw text query for Lavalink (e.g. ``spsearch:daft punk``)."""
    return "{prefix}:{query}".format(prefix=prefix, query=(raw or "").strip())


def decide_prefix(spsearch_error: bool) -> str:
    """Pick the effective search prefix given whether the spsearch attempt errored.

    ``spsearch`` normally; ``ytsearch`` (the tracks-only degrade) only when the
    Spotify-backed ``/loadsearch`` raised - an outage, not merely an empty match.
    """
    return FALLBACK_SEARCH_PREFIX if spsearch_error else DEFAULT_SEARCH_PREFIX


def is_url_query(query: str) -> bool:
    """True when ``query`` is a URL (scheme://...) that /play should resolve directly."""
    return bool(_URL_RE.match((query or "").strip()))


def should_show_picker(pref_on: bool, is_url: bool, has_interaction: bool) -> bool:
    """Whether /play should open the opt-in picker instead of queuing the first hit.

    Only when the member enabled it, the query is NOT a URL (URLs always bypass),
    and the invocation is a slash/interaction one (the picker is an ephemeral
    component surface, so a prefix ``/play`` with no interaction falls through to
    the classic path). Pure so the routing is unit-tested without Discord.
    """
    return bool(pref_on) and not is_url and bool(has_interaction)


def truncate(text: str, limit: int) -> str:
    """Clamp ``text`` to ``limit`` chars, using an ASCII ``...`` when it overflows."""
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _format_clock(total_ms: object) -> str:
    """Render a millisecond length as ``mm:ss`` / ``h:mm:ss`` (blank if unknown)."""
    if not isinstance(total_ms, int) or total_ms <= 0:
        return ""
    total_seconds = total_ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "{h}:{m:02d}:{s:02d}".format(h=hours, m=minutes, s=seconds)
    return "{m:02d}:{s:02d}".format(m=minutes, s=seconds)


def _entry_label(entry: Entry) -> str:
    """"Title - author" for the select, with a translatable title fallback.

    The separator is punctuation joining two data values, not translatable prose,
    so the pieces are concatenated rather than run through a message template.
    """
    title = entry.title or _("Unknown title")
    if entry.subtitle:
        return title + " - " + entry.subtitle
    return title


def _entry_description(entry: Entry, tab: str) -> str:
    """The select-option description: a duration for tracks, a track count else."""
    if tab == TAB_TRACKS:
        return _format_clock(entry.length_ms)
    if entry.total_tracks is not None:
        return ngettext("{count} track", "{count} tracks", entry.total_tracks).format(
            count=entry.total_tracks
        )
    return ""


def build_select_options(entries, tab: str) -> list[discord.SelectOption]:
    """Build the (bounded, truncated) select options for one tab's entries.

    Pure and side-effect free: the option value is the entry's index into
    ``entries`` (never the URI, which can exceed the 100-char value cap), the
    label is the truncated "title - author" line and the description is the
    duration / track-count line. Returns ``[]`` for an empty category.
    """
    options: list[discord.SelectOption] = []
    for index, entry in enumerate(entries[:MAX_PER_TAB]):
        description = truncate(_entry_description(entry, tab), _DESC_LIMIT)
        options.append(
            discord.SelectOption(
                label=truncate(_entry_label(entry), _LABEL_LIMIT),
                description=description or None,
                value=str(index),
            )
        )
    return options


def _initial_tab(results: SearchResults) -> str:
    """First tab (in display order) that has entries, defaulting to Tracks."""
    for tab in _TAB_ORDER:
        if results.entries_for(tab):
            return tab
    return TAB_TRACKS


def top_track_choices(result: object, limit: int) -> list[Entry]:
    """Normalise a ``search_track`` result to the top ``limit`` track entries.

    Duck-types the sonolink ``SearchResult`` (``is_error``/``is_empty``/``result``)
    and flattens its Playlist / list / single-track shapes, then adapts each
    ``Playable`` to an :class:`Entry`, dropping anything without a URI. Pure over
    the shapes the tests mirror, so no live node or database is needed.
    """
    if result is None:
        return []
    is_error = getattr(result, "is_error", None)
    is_empty = getattr(result, "is_empty", None)
    if callable(is_error) and is_error():
        return []
    if callable(is_empty) and is_empty():
        return []
    data = getattr(result, "result", None)
    if data is None:
        return []
    if hasattr(data, "tracks"):  # a Playlist
        tracks = list(data.tracks)
    elif isinstance(data, list):
        tracks = data
    else:
        tracks = [data]
    entries: list[Entry] = []
    for track in tracks[: max(limit, 0)]:
        entry = entry_from_playable(track)
        if entry is not None:
            entries.append(entry)
    return entries


def _result_is_empty(result: object) -> bool:
    """True when a ``search_track`` result carries nothing playable (duck-typed)."""
    if result is None:
        return True
    is_error = getattr(result, "is_error", None)
    is_empty = getattr(result, "is_empty", None)
    if callable(is_error) and is_error():
        return True
    if callable(is_empty) and is_empty():
        return True
    return getattr(result, "result", None) is None


def _play_context(interaction: discord.Interaction):
    """Adapt an interaction into the cog's ``_play_query`` context (lazy import).

    Reuses the engine's ``_ModalPlayContext`` so a pick runs the byte-identical
    ``/play <query>`` body. Imported here (not at module load) so this module
    never participates in the cogs.music import cycle; by the time any pick fires
    the whole package is loaded.
    """
    from cogs.music.views import _ModalPlayContext

    return _ModalPlayContext(interaction)


# ---------------------------------------------------------------------------
# Fetch seams (live; validated by the sponsorblock node.send precedent)
# ---------------------------------------------------------------------------


async def fetch_lavasearch(node, prefixed_query: str) -> dict | None:
    """GET the LavaSearch plugin for ``prefixed_query`` via the node's REST seam.

    Reuses ``node.send`` - the node's own credentialed HTTP client - exactly like
    the SponsorBlock integration, so no credentials are handled here. Returns the
    decoded JSON object, or ``None`` for a 204 (no results). A Lavalink error
    (e.g. a Spotify outage 500) raises out of ``node.send`` for the caller to
    turn into the degrade path.
    """
    raw = await node.send(
        "GET",
        LOADSEARCH_PATH,
        params={"query": prefixed_query, "types": LAVASEARCH_TYPES_PARAM},
    )
    return raw if isinstance(raw, dict) else None


async def run_lavasearch(cog, query: str) -> SearchResults:
    """Fetch and parse LavaSearch for ``query``, degrading on a Spotify outage.

    Tries the all-types ``spsearch`` prefix first. If ``/loadsearch`` errors (a
    Spotify outage, or no connected node), degrades to a tracks-only ``ytsearch``
    result built from the plain ``/loadtracks`` loader, with the other tabs
    flagged unavailable. An empty-but-successful spsearch is NOT a degrade - it
    just yields an empty result and the caller reports "nothing found".
    """
    spsearch_error = False
    raw: dict | None = None
    try:
        node = cog.bot.sl_client.get_best_node()
        raw = await fetch_lavasearch(node, build_query(DEFAULT_SEARCH_PREFIX, query))
    except Exception:
        log.exception("LavaSearch spsearch failed for %r", query)
        spsearch_error = True

    if decide_prefix(spsearch_error) == DEFAULT_SEARCH_PREFIX:
        return parse_search_results(raw)
    return await _degraded_results(cog, query)


async def _degraded_results(cog, query: str) -> SearchResults:
    """Tracks-only ytsearch fallback via the plain loader (Spotify unavailable)."""
    result = await cog._search(query)
    tracks = tuple(top_track_choices(result, MAX_PER_TAB))
    return SearchResults(tracks=tracks, degraded=True)


# ---------------------------------------------------------------------------
# Command bodies (thin wiring the Music cog delegates to)
# ---------------------------------------------------------------------------


async def run_search(cog, ctx, query: str) -> None:
    """Body of the /search command: fetch, then open the browser (or report empty).

    No voice requirement - browsing is free; voice is only checked at pick time by
    ``_play_query``'s own flow. All output is ephemeral on a slash invocation.
    """
    query = (query or "").strip()
    if not query:
        await ctx.send(_("Give me something to search for."), ephemeral=True)
        return
    if not cog._nodes_available():
        await ctx.send(
            _("Music is currently unavailable - no Lavalink node is connected."),
            ephemeral=True,
        )
        return

    await ctx.defer(ephemeral=True)
    results = await run_lavasearch(cog, query)
    if not results.has_any():
        await ctx.send(
            _("I couldn't find anything for **{query}**.").format(
                query=truncate(query, _LABEL_LIMIT)
            )
        )
        return

    view = SearchBrowser(cog, ctx.author.id, query, results)
    view.message = await ctx.send(
        view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


async def maybe_play_picker(cog, ctx, query: str) -> bool:
    """Maybe intercept /play with the opt-in picker; return True iff it handled it.

    Returns False (so ``/play`` runs its classic path unchanged) for a URL query,
    a prefix (non-interaction) invocation, the preference OFF, a preference-read
    failure, or no available node. Only when the member opted in AND the query is
    a plain slash text search does it defer ephemerally, fetch the top matches via
    the SAME loader ``/play`` uses (``cog._search`` -> ytsearch), and show the
    picker. Because it returns False in every other case, ``/play`` OFF is
    byte-identical to today.
    """
    is_url = is_url_query(query)
    has_interaction = ctx.interaction is not None
    # URLs and prefix invocations bypass without even reading the preference.
    if is_url or not has_interaction:
        return False
    try:
        pref_on = await settings.get_user(
            cog.bot.db_pool, ctx.author.id, SEARCH_PICKER_PREF_KEY, PICKER_DEFAULT
        )
    except Exception:
        log.exception(
            "Failed to read search-picker preference for %s", ctx.author.id
        )
        return False
    if not should_show_picker(pref_on, is_url, has_interaction):
        return False
    if not cog._nodes_available():
        # Let _play_query report node-unavailable exactly as it does today.
        return False

    await ctx.defer(ephemeral=True)
    result = await cog._search(query)
    choices = top_track_choices(result, PLAY_PICKER_LIMIT)
    if not choices:
        await ctx.send(
            _("I couldn't find anything for **{query}**.").format(
                query=truncate(query.strip(), _LABEL_LIMIT)
            )
        )
        return True

    view = PlayPickerView(cog, ctx.author.id, choices)
    view.message = await ctx.send(
        content=_("Pick the track you meant:"),
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return True


# ---------------------------------------------------------------------------
# UI - the /search tabbed browser
# ---------------------------------------------------------------------------


class _SearchTabButton(discord.ui.Button):
    """One tab of the Tracks/Albums/Artists/Playlists segmented control.

    The ACTIVE tab renders as a disabled primary button ("you are here", and no
    pointless re-render); every other tab is an enabled secondary button that
    switches to itself. Mirrors the collection dashboard's type tabs.
    """

    def __init__(self, browser: "SearchBrowser", tab: str) -> None:
        self._browser = browser
        self._tab = tab
        active = browser.active_tab == tab
        super().__init__(
            label=_(_TAB_LABELS[tab]),
            emoji=_TAB_EMOJI[tab],
            style=(
                discord.ButtonStyle.primary
                if active
                else discord.ButtonStyle.secondary
            ),
            disabled=active,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._browser.switch_tab(interaction, self._tab)
        except Exception:
            log.exception("Search tab switch failed")
            await interactions.notify_failure(interaction)


class _SearchEntrySelect(discord.ui.Select):
    """The active tab's entries; picking one queues it through the play seam."""

    def __init__(self, browser: "SearchBrowser", entries) -> None:
        self._browser = browser
        super().__init__(
            placeholder=_("Choose one to play..."),
            min_values=1,
            max_values=1,
            options=build_select_options(entries, browser.active_tab),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._browser.pick(interaction, int(self.values[0]))
        except Exception:
            log.exception("Search entry pick failed")
            await interactions.notify_failure(interaction)


class SearchBrowser(AuthorLayoutView):
    """Author-gated, ephemeral Components V2 browser over one LavaSearch result.

    A single accent Container: a header, the four-tab segmented control, then the
    active tab's entries as a select (or a friendly note when that tab is empty or
    Spotify-degraded). Switching a tab re-renders in place; picking an entry
    routes its URI through the cog's ``_play_query`` seam and leaves the browser
    open for more picks. Gating, locale resolution and timeout cleanup come from
    :class:`~tools.views.AuthorLayoutView`.
    """

    def __init__(self, cog, author_id: int, query: str, results: SearchResults, *, timeout: float = 180) -> None:
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.query = query
        self.results = results
        self.active_tab = _initial_tab(results)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                _("## Search results for **{query}**").format(
                    query=truncate(self.query, _LABEL_LIMIT)
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.ActionRow(
                *(_SearchTabButton(self, tab) for tab in _TAB_ORDER)
            )
        )
        container.add_item(discord.ui.Separator())

        entries = self.results.entries_for(self.active_tab)
        if entries:
            container.add_item(discord.ui.ActionRow(_SearchEntrySelect(self, entries)))
        elif self.results.degraded and self.active_tab != TAB_TRACKS:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "Spotify is unavailable right now, so only tracks are "
                        "available."
                    )
                )
            )
        else:
            container.add_item(
                discord.ui.TextDisplay(
                    _("No results in this category for **{query}**.").format(
                        query=truncate(self.query, _LABEL_LIMIT)
                    )
                )
            )

        container.add_item(
            discord.ui.TextDisplay(
                _("-# Only you can use this browser - pick as many as you like.")
            )
        )
        self.add_item(container)

    async def switch_tab(self, interaction: discord.Interaction, tab: str) -> None:
        """Flip the active tab and re-render the browser in place."""
        self.active_tab = tab
        self._build()
        await interaction.response.edit_message(view=self)

    async def pick(self, interaction: discord.Interaction, index: int) -> None:
        """Queue the picked entry through the EXACT ``_play_query`` seam.

        Re-renders first (acknowledging the interaction and resetting the select
        so the same or another entry can be picked again), then routes the entry's
        URI. Albums / playlists / artist URIs all load as playlists through the
        normal loader (live-verified); an artist whose URI resolves to nothing
        falls back to a plain track search of the artist name.
        """
        entries = self.results.entries_for(self.active_tab)
        if index < 0 or index >= len(entries):
            await interaction.response.edit_message(view=self)
            return
        entry = entries[index]
        await interaction.response.edit_message(view=self)

        route = entry.uri
        if self.active_tab == TAB_ARTISTS and entry.uri:
            probe = await self.cog._search(entry.uri)
            if _result_is_empty(probe):
                route = entry.subtitle or entry.title or entry.uri
        await self.cog._play_query(_play_context(interaction), route)


# ---------------------------------------------------------------------------
# UI - the opt-in /play picker
# ---------------------------------------------------------------------------


class _PlayPickerSelect(discord.ui.Select):
    """The top-five track matches; picking one queues it through the play seam."""

    def __init__(self, picker: "PlayPickerView") -> None:
        self._picker = picker
        super().__init__(
            placeholder=_("Choose one to play..."),
            min_values=1,
            max_values=1,
            options=build_select_options(picker.entries, TAB_TRACKS),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._picker.choose(interaction, int(self.values[0]))
        except Exception:
            log.exception("Play picker select failed")
            await interactions.notify_failure(interaction)


class PlayPickerView(AuthorView):
    """The compact ephemeral /play picker: a single select of the top matches.

    Picking routes the chosen track's URL through ``_play_query`` (a URL, so it
    always bypasses the picker recursively) and re-renders in place so the picker
    stays usable. Author-gated and timeout-cleaned by
    :class:`~tools.views.AuthorView`.
    """

    def __init__(self, cog, author_id: int, entries, *, timeout: float = 180) -> None:
        super().__init__(
            author_id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.cog = cog
        self.entries = tuple(entries)
        self.add_item(_PlayPickerSelect(self))

    async def choose(self, interaction: discord.Interaction, index: int) -> None:
        if index < 0 or index >= len(self.entries):
            await interaction.response.edit_message(view=self)
            return
        entry = self.entries[index]
        await interaction.response.edit_message(view=self)
        await self.cog._play_query(_play_context(interaction), entry.uri)
