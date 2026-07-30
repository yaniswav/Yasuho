"""Purpose: the two DISCORD-PRESENCE profile sections - the games a member's
status shows, and what they are listening to on Spotify - both strictly
opt-in, both fed by the gateway rather than by a handle anyone types.

This is the P5 half of the connector story. The five P4 connectors are an
account the user names and a third party the bot polls; these two are the
opposite: the data walks in on its own, on the hottest global event the bot
receives, about people who never asked for it. Everything below is shaped by
that difference.

---------------------------------------------------------------------------
Consent: the row IS the opt-in
---------------------------------------------------------------------------

Nothing is collected, kept or shown for a member without a marker row in
``profile_connections`` (``presence_gaming`` / ``spotify_presence``, with the
owner's own id as ``external_id`` - see connectors/storage.set_marker). No
row, no listener work, no section, ever. ``/profile presence`` is the only
thing that creates one, and ``off`` deletes it through the same
``connectors.storage.unlink`` every other section uses - which also drops the
visibility line, so a section can never stay published over nothing.

The marker is opt-in to COLLECTION. Showing it is still the ordinary
visibility question (every section is born private), so the opt-in answer
points at ``profile panel`` rather than publishing anything on the user's
behalf.

WITHDRAWING that consent has three doors, not one: ``off``, ``profile clear``
and ``/mydata deleteprofile``, and the last two run in other cogs. Both halves
of this module answer them. IMMEDIATELY, through
:func:`forget_collected_presence`, which those two paths call so the very next
event is already rejected; and STRUCTURALLY, in :meth:`ProfilePresence.flush`,
whose batched read is treated as the authority on consent - a pending user
with no marker row is written nothing and dropped from the collector on the
spot. The second half is what makes the guarantee hold even when the first
never fires (the cog was not loaded, a fourth door appears later).

---------------------------------------------------------------------------
What is stored, and what deliberately is not
---------------------------------------------------------------------------

``presence_gaming`` stores AGGREGATES ONLY: a bounded top-``MAX_GAMES`` list of
``{name, minutes, last_played}``. No timeline, no per-session record, no
timestamp of any individual play - "you played Celeste from 21:04 to 23:47 on
Tuesday" is exactly the shape this design refuses to be able to answer, and
the only way to guarantee that is never to write it down. A game nobody has
touched for :data:`PURGE_AFTER_DAYS` days falls out at the next flush that
touches its owner's row AND is filtered again at the render, because a member
who stopped playing is never flushed again (see :data:`PURGE_AFTER_DAYS`).

``spotify_presence`` stores NOTHING at all - its payload stays ``{}`` for the
life of the row. The now-playing line is read LIVE from ``member.activities``
at the moment somebody runs ``/profile view`` (see :func:`enrich_live`), out
of the gateway cache, with zero network and zero database. Stop listening, or
turn on Spotify's own private session, and the section simply goes quiet;
there is no history to leak because there is no history.

---------------------------------------------------------------------------
The member-cache limitation, stated rather than "fixed"
---------------------------------------------------------------------------

VERIFIED in discord.py 2.7.1 (``ConnectionState.parse_presence_update``): a
PRESENCE_UPDATE whose member is not in the guild's cache is DISCARDED before
any listener sees it, and this bot runs ``chunk_guilds_at_startup=False``. So
presence collection is best-effort by construction: it sees a member once
that member is cached (they spoke, they were fetched, an event carried them).

The opt-in seeds that cache once, for the invoking guild only
(``guild.query_members(user_ids=[id], cache=True, presences=True)``): one
gateway request, on a rare deliberate action, for exactly the person who asked
for it. A restart empties the member cache and the collection goes quiet again
until each member is seen anew. That is ACCEPTED and documented; it is never
to be "repaired" by chunking guilds globally, which would trade a handful of
missed minutes on an opt-in cosmetic feature for a permanent memory and
gateway cost across every guild the bot is in.

---------------------------------------------------------------------------
Scale story
---------------------------------------------------------------------------

``on_presence_update`` is one of the loudest events on the gateway: with
presences on, every status, custom-status, activity and rich-presence tick of
every member of every guild lands here, and it is dispatched ONCE PER GUILD
shared with the member. At 1000+ guilds that is realistically hundreds of
events a second, all day.

* REJECTION IS THE HOT PATH and it is one set membership test on
  ``self._opted`` followed by a return. What is PROVEN about it: there is no
  await anywhere on that path, so the coroutine never suspends the event loop
  and no I/O is ever started (a test drives it with ``coro.send(None)`` and
  asserts ``StopIteration``, which is what "zero awaitable created" actually
  means). What is NOT claimed: the dispatch itself still allocates - discord.py
  wraps every listener call in a Task, and ``after.id`` is a property - and
  neither is this module's to remove. Opted-in members are a hand-counted
  minority, so essentially every event costs exactly that.
* An ACCEPTED event costs two small set comprehensions over an activity list
  Discord itself caps at a handful of entries, plus O(1) dict work.
* MEMORY: ``_opted`` is 8 bytes plus set overhead per opted-in user (10k
  opt-ins is well under a megabyte, and it can only grow by a deliberate human
  action). ``_sessions`` is hard-capped at :data:`SESSION_CAP` entries and
  ``_buffer`` at :data:`PENDING_USER_CAP` users x :data:`PENDING_GAME_CAP`
  games; past either, work is DROPPED and counted rather than allowed to grow
  the process.
* DATABASE: zero writes on the event path. The flush is one loop every
  :data:`FLUSH_INTERVAL` seconds, and it is bounded at
  :data:`FLUSH_USER_CAP` users per tick - so the worst-case cost of this whole
  cog is ONE batched read plus at most :data:`FLUSH_USER_CAP` single-row
  updates every five minutes, whatever the traffic. Users over the cap are not
  dropped, they are carried into the next tick.
* RENDER: both sections are pure memory. ``presence_gaming`` reads the payload
  the card already fetched; ``spotify_presence`` reads ``member.activities``,
  which ``/profile view`` already holds.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

import discord
from discord.ext import commands, tasks

from . import views as profile_views
from .connectors import base, storage
from tools.i18n import _

log = logging.getLogger(__name__)

# The two sections this cog owns. Taken from the framework's own tuple rather
# than restated, so a rename there cannot leave an orphan here.
GAMING_SECTION, SPOTIFY_SECTION = base.PRESENCE_SECTIONS

# One batched flush every 5 minutes, bot-wide - the same clock as the voice-XP
# sweep and the serverstats collector. It makes the write rate a function of
# TIME, not of how much anybody played, and losing at most one interval of
# aggregate minutes to a hard crash costs nothing that matters.
FLUSH_INTERVAL = 300

# Hard ceiling on live game sessions held between two flushes. A session is one
# (user, game) pair, so this is 4096 concurrent games across every opted-in
# member - orders of magnitude past any realistic opt-in population, and small
# enough that a runaway (a gateway hiccup that drops the "stopped playing"
# edge for thousands of members at once) can never grow the process without
# bound. Past it, new sessions are DROPPED and counted.
SESSION_CAP = 4096

# A session older than this is not evidence of play, it is an END EVENT THAT
# NEVER CAME (the member fell out of the cache, the gateway dropped the edge,
# a client went to sleep mid-game). The sweep drops it WITHOUT crediting
# anything: inventing 24 hours of Celeste for someone who closed their laptop
# would be worse than losing a session. The same number caps the credit on the
# normal end path as a belt - a monotonic clock that jumped, or a hand-built
# session in a test, must not be able to write an absurd total.
SESSION_MAX_SECONDS = 24 * 60 * 60

# Buffer bounds, the same posture as serverstats' StatsBuffer: legitimate load
# never comes close, and only a pathological case reaches them, at which point
# the work is dropped and counted instead of held.
PENDING_USER_CAP = 4096
PENDING_GAME_CAP = 16

# How many users one flush tick may write. The read is batched into a single
# statement, but each merge is a read-modify-write of ONE user's JSON payload,
# so the writes are individual by nature. This is what turns "however many
# people stopped playing in the last five minutes" into a stated, bounded cost
# (see the scale story). Nothing is lost past it: the overflow is folded back
# into the buffer and written on the next tick.
FLUSH_USER_CAP = 200

# How many games the stored aggregate keeps, most recently played first. Ten
# names, their minutes and their dates sit far under base.PAYLOAD_MAX_BYTES
# (8 KiB) with room to spare - and the numbers are plain integers, so the
# margin this leaves is the numeric one that cap's own comment asks for.
MAX_GAMES = 10

# A game name is THIRD-PARTY TEXT rendered on somebody ELSE's profile card: it
# is whatever string a member's game client (or a hand-crafted rich presence)
# put in their status. Clipped at the parse, so nothing longer ever reaches the
# session map, the payload or the card - and clipped again at the render,
# because the payload may be a row a past version of this module wrote.
GAME_NAME_MAX = 60

# A game untouched for this long stops being "what they have been playing".
# TWO half-measures that only add up together, and the docstring says so rather
# than promising a sweep this module deliberately does not run:
#   * LAZY AT THE FLUSH - merge_games drops the stale entries of the row it
#     already has in hand. Never a sweep of its own, which would be a scan over
#     every opted-in user for a cosmetic list;
#   * FILTERED AT THE RENDER - a member who stopped playing (or stopped being
#     seen) is never flushed again, so their row can sit at its last state for
#     months. The renderer therefore re-applies the same cutoff, and that is
#     what makes the 30 days true on the card rather than only in the merge.
PURGE_AFTER_DAYS = 30

# Bound on the Spotify strings read live out of the gateway cache. Same reason
# as GAME_NAME_MAX: a track title is whatever was uploaded to Spotify, and it
# is being drawn on a card that belongs to someone else.
TRACK_TEXT_MAX = 120

# How long cog_unload waits for a cancelled in-flight flush to unwind before it
# runs the final one. Generous next to a handful of single-row updates, tiny
# next to a shutdown: the point is that teardown is bounded even if the pool is
# wedged.
UNLOAD_CANCEL_TIMEOUT = 5


# ---------------------------------------------------------------------------
# Pure helpers: no bot, no database, no I/O. Everything the listener, the
# merge and the two renderers decide is testable as plain data.
# ---------------------------------------------------------------------------


def _monotonic():
    """The session clock, in its own function so a test can freeze it.

    ``time.monotonic`` and never ``time.time``: a session's length must not
    change because the host adjusted its wall clock in the middle of it.
    """
    return time.monotonic()


def _one_line(text):
    """Flatten third-party text that shares a line with a label.

    Same discipline (and the same reason) as views.py's own ``_one_line``: a
    newline inside a card row lets its author forge what looks like a section
    header on somebody else's profile.
    """
    return " ".join(str(text).split())


def clean_game_name(name):
    """A bounded, single-line game name, or ``None`` when there is nothing left.

    Applied at the PARSE - before a name can become a dict key in the session
    map or the buffer - so a hostile rich presence cannot spend memory it was
    never granted, and applied again at the render for rows written earlier.
    """
    if name is None:
        return None
    text = _one_line(name)
    if not text:
        return None
    return text[:GAME_NAME_MAX]


def playing_names(activities):
    """The set of GAME names in an activity list (``playing`` activities only).

    A set, not a list: Discord sends the same activity twice often enough
    (two clients, a rich presence and its plain twin), and the caller diffs
    two of these to find what started and what stopped.

    Streaming, listening (Spotify) and custom statuses are deliberately not
    games and never enter here. Nothing raises: an activity object from a
    future library version that lacks ``type`` or ``name`` is skipped.
    """
    names = set()
    for activity in activities or ():
        if getattr(activity, "type", None) != discord.ActivityType.playing:
            continue
        name = clean_game_name(getattr(activity, "name", None))
        if name:
            names.add(name)
    return names


def spotify_activity(member):
    """The member's live Spotify activity, or ``None``.

    Reads the gateway cache only - no network, no await, nothing stored. The
    FIRST one wins: a member can technically carry several listening
    activities, and a card shows one line.
    """
    for activity in getattr(member, "activities", None) or ():
        if isinstance(activity, discord.Spotify):
            return activity
    return None


def _escape_link_label(text):
    """Neutralise the two characters that can end a markdown link LABEL.

    The Spotify row draws ``[{artist} - {title}]({url})`` around text Spotify
    hands over verbatim, so a title carrying a ``]`` closes the label early and
    everything after it becomes markdown the track's uploader wrote: a title of
    ``x](https://evil.example) `` renders as a link to THEIR domain, under a
    label of their choosing, on somebody else's profile card. Escaping both
    brackets is what keeps the url the only structural part of the line.

    ``\\[`` is Discord's own escape and renders as a plain bracket, so a track
    legitimately titled "[Remix]" still reads correctly.
    """
    return str(text).replace("[", "\\[").replace("]", "\\]")


def _clip(text, limit=TRACK_TEXT_MAX):
    flattened = _one_line(text) if text is not None else ""
    if not flattened:
        return None
    return flattened[:limit]


def spotify_now_playing(member):
    """``{"title", "artist", "url"?, "cover"?}`` for a live listen, or ``None``.

    Never raises and never returns a half-line: without both a title and an
    artist there is no sentence to write, so the section says nothing at all
    rather than a bold heading over a blank.
    """
    activity = spotify_activity(member)
    if activity is None:
        return None
    try:
        title = _clip(activity.title)
        artist = _clip(activity.artist)
        if not title or not artist:
            return None
        now = {"title": title, "artist": artist}
        # base.safe_url, not a truth test: an unfetchable Thumbnail url makes
        # Discord reject the WHOLE message at SEND time, long after
        # render_sections could still fall back to anything (see its own
        # docstring). ``album_cover_url`` is '' when Spotify sent no asset,
        # and ``track_url`` is a bare prefix when there is no track id.
        cover = base.safe_url(getattr(activity, "album_cover_url", None))
        if cover:
            now["cover"] = cover
        if getattr(activity, "track_id", None):
            url = base.safe_url(getattr(activity, "track_url", None))
            if url:
                now["url"] = url
        return now
    except Exception:
        # A malformed activity object must cost one section, never the card.
        log.exception("Failed to read the live Spotify activity")
        return None


def _parse_stamp(value):
    """An aware datetime out of a stored ISO string, or ``None``.

    The payload is a row on disk: a past version of this module, a hand edit
    or the dashboard may have left anything there, and this runs inside
    ``/profile view``.
    """
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def _stored_games(payload):
    """The ``{name: (minutes, stamp)}`` an existing payload holds.

    Tolerant by construction - anything that is not a well-formed entry is
    dropped rather than allowed to break a merge or a card.
    """
    games = {}
    entries = payload.get("games") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return games
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = clean_game_name(entry.get("name"))
        if not name:
            continue
        minutes = base.safe_number(entry.get("minutes"))
        if minutes is None or minutes < 0:
            minutes = 0
        games[name] = (float(minutes), _parse_stamp(entry.get("last_played")))
    return games


def merge_games(payload, additions, now, *, max_games=MAX_GAMES):
    """Fold ``{game: seconds}`` into the stored aggregate; return a new payload.

    Pure. The whole write rule of ``presence_gaming`` lives here:

    * minutes ACCUMULATE per game and are rounded to a whole number - the
      payload holds plain integers on purpose, because a float big enough to
      need exponent form is the one way an 8 KiB estimate can under-measure
      what Postgres then refuses (see base.PAYLOAD_MAX_BYTES);
    * ``last_played`` is the only timestamp that exists, and it is overwritten,
      never appended to - there is no session history to keep;
    * a game untouched for :data:`PURGE_AFTER_DAYS` is dropped, lazily, here;
    * the result is the :data:`MAX_GAMES` most recently played, so a member who
      opens fifty different games does not grow their row;
    * a game that still rounds to zero minutes is dropped: "0m" is not a fact
      worth putting on a card.
    """
    cutoff = now - datetime.timedelta(days=PURGE_AFTER_DAYS)
    merged = {}
    for name, (minutes, stamp) in _stored_games(payload).items():
        if stamp is not None and stamp < cutoff:
            continue
        merged[name] = (minutes, stamp)

    for raw_name, seconds in (additions or {}).items():
        name = clean_game_name(raw_name)
        if not name:
            continue
        added = base.safe_number(seconds)
        if added is None or added <= 0:
            continue
        previous = merged.get(name)
        total = (previous[0] if previous is not None else 0.0) + added / 60.0
        merged[name] = (min(total, base.MAX_SANE_NUMBER), now)

    games = []
    for name, (minutes, stamp) in merged.items():
        whole = int(round(minutes))
        if whole <= 0:
            continue
        entry = {"name": name, "minutes": whole}
        if stamp is not None:
            entry["last_played"] = stamp.isoformat()
        games.append((stamp, entry))

    # Most recently played first; an entry with no usable stamp sorts last
    # rather than being dropped - it is real playtime with a lost date.
    floor = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    games.sort(key=lambda item: item[0] or floor, reverse=True)
    return {"games": [entry for _stamp, entry in games[:max_games]]}


# ---------------------------------------------------------------------------
# The bounded buffer (the serverstats StatsBuffer posture, one table over)
# ---------------------------------------------------------------------------


class PresenceBuffer:
    """``{user_id: {game: seconds}}`` between two flushes, hard-bounded.

    Every mutator is synchronous, O(1) and allocation-light: the listener calls
    them from the gateway hot path and must never await. Both caps drop work
    and COUNT it rather than let the process grow - a dropped entry costs a few
    uncounted minutes on a cosmetic card, which is always the better trade.
    """

    __slots__ = ("_pending", "_user_cap", "_game_cap", "_dropped")

    def __init__(self, user_cap=PENDING_USER_CAP, game_cap=PENDING_GAME_CAP):
        self._pending: dict[int, dict[str, float]] = {}
        self._user_cap = user_cap
        self._game_cap = game_cap
        self._dropped = 0

    def add(self, user_id, game, seconds):
        """Credit ``seconds`` of ``game`` to ``user_id``. False == dropped."""
        if seconds <= 0:
            return False
        games = self._pending.get(user_id)
        if games is None:
            if len(self._pending) >= self._user_cap:
                self._dropped += 1
                return False
            games = {}
            self._pending[user_id] = games
        if game not in games and len(games) >= self._game_cap:
            self._dropped += 1
            return False
        games[game] = games.get(game, 0.0) + seconds
        return True

    def forget(self, user_id):
        """Drop everything buffered for one user - what an opt-OUT means.

        Minutes collected before the user said stop are not written after they
        said stop, even for the few seconds the interval had left to run.
        """
        self._pending.pop(user_id, None)

    def drain(self):
        """Detach everything collected so far and reset the live buffer.

        Cleared BEFORE the flush awaits anything, so the listener keeps
        counting into a fresh generation while the write is in flight (the
        serverstats discipline); whatever fails to write is handed back with
        :meth:`restore`.
        """
        pending = self._pending
        dropped = self._dropped
        self._pending = {}
        self._dropped = 0
        return pending, dropped

    def restore(self, pending):
        """Fold a failed (or deferred) generation back in, still capped.

        Goes through :meth:`add`, so a database blip - or the per-tick user
        ceiling - can never turn into unbounded memory: at worst the overflow
        is dropped and counted like any other.
        """
        for user_id, games in (pending or {}).items():
            for game, seconds in games.items():
                self.add(user_id, game, seconds)

    @property
    def dropped(self):
        return self._dropped

    @property
    def user_count(self):
        return len(self._pending)

    @property
    def is_empty(self):
        return not self._pending


# ---------------------------------------------------------------------------
# The live-enrichment seam the profile cog calls before rendering a card
# ---------------------------------------------------------------------------


def enrich_live(member, connections):
    """Attach the LIVE half of both presence sections to the rows the card got.

    ``/profile view`` has already read ``profile_connections`` and already
    holds the :class:`discord.Member`, so this is pure memory: no await, no
    query, no network. It writes a ``live`` key onto the matching connection
    dicts (they are plain dicts from ``connectors.storage._row_to_connection``,
    built for this caller and nobody else) and returns them unchanged
    otherwise.

    Only sections the owner actually opted into are touched - the row has to
    exist first - and the caller's visibility check still decides whether the
    section is drawn at all. Never raises: a card must not fail over a status.
    """
    try:
        for connection in connections or ():
            if not isinstance(connection, dict):
                continue
            name = connection.get("connector")
            if name == SPOTIFY_SECTION:
                now = spotify_now_playing(member)
                if now:
                    connection["live"] = now
            elif name == GAMING_SECTION:
                # Sorted, then first: a member can be "playing" two things at
                # once (two clients, a launcher and its game), the card shows
                # one line, and a card that reshuffles between two viewers for
                # no reason is worse than an arbitrary but STABLE choice.
                playing = sorted(playing_names(getattr(member, "activities", None)))
                if playing:
                    connection["live"] = {"playing": playing[0]}
    except Exception:
        log.exception("Failed to enrich the live presence sections")
    return connections


# ---------------------------------------------------------------------------
# The renderers
# ---------------------------------------------------------------------------


def _game_line(name, minutes):
    """One ``game - time`` row, as ONE whole msgid per shape.

    Two complete strings rather than a composed "{game} - " + a translated
    duration: a translator has to be able to move the number, the unit and the
    name relative to each other, and a fragment glued at the call site is
    exactly what takes that away.
    """
    hours, rest = divmod(int(minutes), 60)
    if hours:
        return _("{game} - {hours}h {minutes}m").format(
            game=name, hours=hours, minutes=rest
        )
    return _("{game} - {minutes}m").format(game=name, minutes=rest)


async def _render_gaming(container, field, viewer, connection, budget):
    """The ``presence_gaming`` section: the live game, then the top games.

    Draws from the stored payload plus whatever :func:`enrich_live` attached -
    no network, by contract. Adds NOTHING when there is neither, and the
    framework then rolls the leading separator back with it: a member who
    opted in an hour ago and has not played since gets no empty heading.
    """
    payload = connection.get("payload") or {}
    live = connection.get("live") or {}
    lines = ["**" + _(field.label) + "**"]

    playing = clean_game_name(live.get("playing"))
    if playing:
        lines.append(_("Playing {game} right now").format(game=playing))

    # The SECOND half of the 30-day purge (see PURGE_AFTER_DAYS). The lazy one
    # only fires on a flush that touches the row, and a member who stopped
    # playing is never flushed again - so without this the card would keep
    # showing a two-year-old list forever. The cutoff is applied to the payload
    # as read, which makes it true whatever the row's age.
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=PURGE_AFTER_DAYS
    )

    # Sized against the room the card actually has left rather than trusting
    # MAX_GAMES to fit: the budget is a frozen snapshot of what is free after
    # every other section, and a renderer that overflows is dropped WHOLE.
    spent = sum(len(line) for line in lines)
    for entry in (payload.get("games") or [])[:MAX_GAMES]:
        if not isinstance(entry, dict):
            continue
        # Re-clipped here and not only at the merge: this row was written by a
        # past version of this module, and it is being drawn on the card of
        # whoever is being looked at, not of whoever wrote it.
        name = clean_game_name(entry.get("name"))
        minutes = base.safe_number(entry.get("minutes"))
        if not name or minutes is None or minutes < 1:
            continue
        # A stamp-less entry is real playtime with a LOST date, not an old one
        # (merge_games keeps it and sorts it last), so it is not filtered here
        # either - only a date that is provably past the cutoff drops the row.
        stamp = _parse_stamp(entry.get("last_played"))
        if stamp is not None and stamp < cutoff:
            continue
        line = _game_line(name, minutes)
        spent += len(line) + 1
        if spent > budget.text:
            break
        lines.append(line)

    if len(lines) == 1:
        return
    # Defused, not just flattened. A game row LEADS its line with a name a
    # member's game client chose, so a title of "## Gaming IDs" would render as
    # a heading standing over the real sections of somebody else's card -
    # flattening removes the newline and leaves the "## " exactly where
    # markdown wants it. See views.defuse_lines.
    container.add_item(
        discord.ui.TextDisplay(profile_views.defuse_lines("\n".join(lines)))
    )


async def _render_spotify(container, field, viewer, connection, budget):
    """The ``spotify_presence`` section: the live listen, or NOTHING.

    There is no payload to fall back on - this section stores nothing at all
    (see the module docstring) - so an absent ``live`` means the section is
    omitted entirely. That is deliberate and not a missing "listening to
    nothing right now": an empty section would assert something about a
    person's evening, and silence asserts nothing.
    """
    live = connection.get("live") or {}
    title = _clip(live.get("title"))
    artist = _clip(live.get("artist"))
    if not title or not artist:
        return

    track = "{artist} - {title}".format(artist=artist, title=title)
    url = base.safe_url(live.get("url"))
    if url:
        # A markdown link, so the url is the only structural part of the line -
        # which is only true once the label's own brackets are escaped, or the
        # track title itself could close it and open a link of its own (see
        # _escape_link_label). The clip already flattened the text; this is the
        # second half of the same guard.
        track = "[{track}]({url})".format(
            track=_escape_link_label(track), url=url
        )
    lines = [
        "**" + _(field.label) + "**",
        _("Listening to {track}").format(track=track),
    ]
    # Defused for the same reason as the game rows, and for one more: the
    # English msgid happens to start with "Listening to", but a translation is
    # free to put the placeholder first, and then a track titled "## " would be
    # line-leading in that locale and in no other. See views.defuse_lines.
    text = discord.ui.TextDisplay(profile_views.defuse_lines("\n".join(lines)))
    cover = base.safe_url(live.get("cover"))
    if cover:
        container.add_item(
            discord.ui.Section(text, accessory=discord.ui.Thumbnail(cover))
        )
    else:
        container.add_item(text)


profile_views.register_section_renderer(GAMING_SECTION, _render_gaming)
profile_views.register_section_renderer(SPOTIFY_SECTION, _render_spotify)


# ---------------------------------------------------------------------------
# The seam every OTHER erasure path calls
# ---------------------------------------------------------------------------


def forget_collected_presence(bot, user_id):
    """Tell the live collector to drop a user whose profile was just erased.

    ``/profile presence gaming off`` is not the only door out: ``profile
    clear`` and ``/mydata deleteprofile`` delete every profile row a person
    has, marker included, and they run in other cogs. Deleting the row without
    this call leaves the user armed in ``_opted``, so the collector would keep
    recording someone who just asked to be forgotten - for as long as the
    process lives.

    BEST EFFORT, deliberately: it returns False (and never raises) when the
    presence cog is not loaded or the call fails, because a failure here must
    not turn a completed erasure into an error message for the user. The
    guarantee does not rest on it either - the flush re-checks the marker row
    for every pending user and purges the ones that no longer have one, so the
    erasure wins structurally even if this seam is never reached.
    """
    getter = getattr(bot, "get_cog", None)
    if getter is None:
        return False
    try:
        cog = getter("ProfilePresence")
        if cog is None:
            return False
        cog.forget_user(user_id)
    except Exception:
        log.exception("presence: could not drop the collected state of %s", user_id)
        return False
    return True


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class ProfilePresence(commands.Cog):
    """Opt-in Discord-presence profile sections: games played, Spotify live."""

    def __init__(self, bot):
        self.bot = bot
        # The ONLY thing the hot listener reads. Filled once at cog_load from
        # the marker rows and kept in step by the opt-in / opt-out command, so
        # the gateway path never touches the database.
        self._opted: set[int] = set()
        # (user_id, game) -> monotonic start. One entry per user+game however
        # many guilds reported it - which is what stops a member the bot shares
        # three servers with from being counted three times (see _apply).
        self._sessions: dict[tuple[int, str], float] = {}
        self._buffer = PresenceBuffer()
        # Cumulative instrumentation (scale story). Each counter means exactly
        # one thing, and each thing is counted in exactly ONE place - a number
        # that double-counts is worse than no number at all:
        #   events   - presence updates that passed the opt-in test;
        #   started  - sessions opened;
        #   ended    - sessions closed by a real "stopped playing" edge;
        #   empty    - closed sessions worth 0 seconds: neither a write nor a
        #              drop, and they used to be counted as drops;
        #   expired  - sessions dropped by the sweep, their end event never came;
        #   dropped  - work refused by a CAP. The session cap counts here
        #              directly; the buffer's own caps count inside the buffer
        #              and are folded in ONCE, at the drain;
        #   deferred - users postponed by the per-tick write ceiling (nothing
        #              lost, they are handed straight back to the buffer);
        #   forgotten- users purged by the flush because their row was gone
        #              (opt-out, `profile clear` or /mydata deleteprofile);
        #   flushes  - flush ticks that reached the write loop. A tick with an
        #              empty buffer returns before it and is NOT counted, so
        #              this is "useful ticks", not "loop iterations";
        #   written  - rows actually updated.
        self._stats = {
            "events": 0,
            "started": 0,
            "ended": 0,
            "empty": 0,
            "flushes": 0,
            "written": 0,
            "deferred": 0,
            "dropped": 0,
            "expired": 0,
            "forgotten": 0,
        }

    # -- lifecycle ----------------------------------------------------------

    async def cog_load(self):
        """Load the opt-in set once, then start the flush loop.

        Runs inside setup_hook, before the gateway delivers a single event, so
        the listener sees a populated set from the very first presence update.
        A failure here leaves the set empty - collection stays off until the
        next opt-in, which is the correct way for a consent cache to fail -
        and never blocks startup.
        """
        try:
            self._opted = await storage.get_opted_users(
                self.bot.db_pool, GAMING_SECTION
            )
        except Exception:
            log.exception("Failed to load the presence opt-in set")
            self._opted = set()
        self._flush_loop.start()

    async def cog_unload(self):
        """Cancel the loop, wait for a cancelled flush to unwind, then flush.

        Same three-step shape as the serverstats collector, for the same
        reason: a flush cancelled mid-write hands its generation back to the
        buffer while it unwinds, and that unwind runs in the LOOP's task, so
        flushing here without waiting would write an already-drained buffer.
        Bounded and swallowing, because teardown must never hang or raise over
        a cosmetic aggregate.
        """
        task = self._flush_loop.get_task()
        self._flush_loop.cancel()
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=UNLOAD_CANCEL_TIMEOUT)
        if self._buffer.is_empty:
            return
        try:
            await self.flush()
        except Exception:
            log.exception("presence: final flush on unload failed")

    # -- the hot listener ---------------------------------------------------

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        """Track game start/stop edges for OPTED-IN members only.

        HOT GLOBAL event - see the module's scale story. The first statement is
        the whole design: a set membership test that rejects essentially every
        event this bot will ever receive, with no allocation and, crucially, no
        await anywhere in the body, so the coroutine completes without ever
        suspending the event loop.

        Nothing here touches the database. Everything it learns goes into two
        bounded in-memory maps that the flush loop drains on its own clock.
        """
        if after.id not in self._opted:
            return
        try:
            self._apply(after.id, before.activities, after.activities)
        except Exception:
            log.exception("presence: failed to apply a presence update")

    def _apply(self, user_id, before_activities, after_activities, now=None):
        """Diff two activity lists into session starts and stops.

        Synchronous and O(activities). ``now`` is injectable for tests and
        defaults to ``time.monotonic()`` - a clock that cannot jump backwards
        when the host adjusts its wall time mid-session.

        MULTI-GUILD DEDUPLICATION lives in the two loops below. discord.py
        dispatches one presence update PER GUILD shared with the member, so the
        same "started Celeste" arrives two or three times; the session key is
        ``(user, game)`` with NO guild in it, an already-open session is left
        alone, and ``pop`` lets only the first stop credit anything. The other
        copies are no-ops rather than a doubled total.

        The early return matters as much: rich presence, custom statuses and
        plain online/idle flips all arrive here, and none of them changes the
        set of games. Comparing the two sets first is what keeps the common
        accepted event down to two small comprehensions and a return.
        """
        if now is None:
            now = _monotonic()
        self._stats["events"] += 1
        before_games = playing_names(before_activities)
        after_games = playing_names(after_activities)
        if before_games == after_games:
            return
        for game in after_games - before_games:
            key = (user_id, game)
            if key in self._sessions:
                continue
            if len(self._sessions) >= SESSION_CAP:
                # Backstop only (see SESSION_CAP): drop rather than grow.
                self._stats["dropped"] += 1
                continue
            self._sessions[key] = now
            self._stats["started"] += 1
        for game in before_games - after_games:
            started = self._sessions.pop((user_id, game), None)
            if started is None:
                continue
            self._stats["ended"] += 1
            # The belt to the sweep's braces: a monotonic clock that jumped
            # (or a session built by hand in a test) must not be able to write
            # a day and a half of Celeste.
            elapsed = min(max(now - started, 0.0), SESSION_MAX_SECONDS)
            if elapsed <= 0:
                # Start and stop inside the same instant (two clients racing,
                # a launcher that flickered). There is nothing to credit and
                # nothing was refused: this is neither a write nor a DROP, and
                # counting it as one made the drop counter say the caps were
                # being hit when they were not.
                self._stats["empty"] += 1
                continue
            # The buffer counts its OWN refusals (see PresenceBuffer._dropped)
            # and the flush folds that total into self._stats once, at the
            # drain. Counting the False here as well is what double-counted it.
            self._buffer.add(user_id, game, elapsed)

    def _sweep_sessions(self, now):
        """Drop sessions whose end event never came; return how many.

        Not a credit: see :data:`SESSION_MAX_SECONDS`. Runs once per flush
        tick over a map capped at :data:`SESSION_CAP`, so its worst case is a
        few thousand comparisons every five minutes.
        """
        cutoff = now - SESSION_MAX_SECONDS
        stale = [key for key, started in self._sessions.items() if started < cutoff]
        for key in stale:
            del self._sessions[key]
        self._stats["expired"] += len(stale)
        return len(stale)

    def _forget(self, user_id):
        """Erase every trace of one user from memory - what opting OUT means.

        The row and the visibility line are deleted by the storage seam; these
        two maps are the only other place a minute of theirs could be hiding,
        and a flush that ran a second later must not resurrect it.
        """
        self._buffer.forget(user_id)
        for key in [key for key in self._sessions if key[0] == user_id]:
            del self._sessions[key]

    def forget_user(self, user_id):
        """PUBLIC seam: stop collecting for a user whose data was just erased.

        ``/profile presence gaming off`` is not the only way a marker row dies:
        ``profile clear`` and ``/mydata deleteprofile`` delete every profile row
        the person has, including this one, and neither goes through this cog.
        Without this call the collector would keep an erased user in
        ``_opted`` and keep recording them until the next restart - which is
        the one thing an erasure must not leave behind.

        Synchronous, O(sessions of that user) and never raises: it is called
        from a delete path that has already succeeded, and a failure here must
        not turn a completed erasure into an error message.

        This is the IMMEDIATE half of the guarantee. The structural half lives
        in :meth:`flush`, which purges any pending user whose row the batched
        read cannot find - so the erasure wins even if this call never happens
        (the cog was not loaded, a third writer appears later).
        """
        self._opted.discard(user_id)
        self._forget(user_id)

    # -- the flush ----------------------------------------------------------

    @tasks.loop(seconds=FLUSH_INTERVAL)
    async def _flush_loop(self):
        try:
            await self.flush()
        except Exception:
            log.exception("presence flush iteration failed")

    @_flush_loop.before_loop
    async def _before_flush_loop(self):
        await self.bot.wait_until_ready()

    @_flush_loop.error
    async def _flush_loop_error(self, error):
        log.exception("presence flush crashed; restarting", exc_info=error)
        self._flush_loop.restart()

    async def flush(self, now=None):
        """Merge one interval of collected minutes into the stored aggregates.

        DRAIN BEFORE AWAIT, without exception: the buffer is detached in the
        first statement of this method, so every minute the listener records
        while the write is in flight lands in a fresh generation instead of
        being wiped by the reset that follows a successful write.

        AT-LEAST-ONCE, stated honestly (the serverstats posture): a failure
        after the update committed folds the same generation back in, so a very
        rare crash window can double-count one interval of one user's minutes.
        Aggregate minutes on a cosmetic card, not money - and the alternative,
        dropping a generation on every blip, loses real data far more often.

        CANCELLATION IS A FAILURE LIKE ANY OTHER, and the one that matters most:
        cog_unload cancels the loop and THEN runs a final flush, so the drained
        generation has to survive a CancelledError thrown into either await or
        the shutdown writes an empty buffer over it. See the ``except
        BaseException`` clauses below and the serverstats twin they copy.

        THE ROW IS THE CONSENT, and this method re-checks it: a pending user the
        batched read cannot find no longer HAS a marker row - they opted out,
        cleared their profile or ran /mydata deleteprofile while the interval
        was accumulating - so nothing is written for them AND they are purged
        from the in-memory collector on the spot. That is what makes the
        erasure win structurally, whatever the delete path forgot to call.
        """
        pending, dropped = self._buffer.drain()
        if dropped:
            # Once per flush, never per event: the cap exists to bound memory,
            # and a per-drop log would be its own flood.
            self._stats["dropped"] += dropped
            log.warning(
                "presence: buffer cap reached, dropped %d game entry(ies) "
                "this interval",
                dropped,
            )
        if now is None:
            now = _monotonic()
        self._sweep_sessions(now)
        if not pending:
            return

        # The per-tick ceiling. Deferred users are handed straight back, so
        # nothing is lost - only postponed by one interval.
        user_ids = list(pending)
        if len(user_ids) > FLUSH_USER_CAP:
            deferred = {uid: pending.pop(uid) for uid in user_ids[FLUSH_USER_CAP:]}
            self._buffer.restore(deferred)
            self._stats["deferred"] += len(deferred)
            user_ids = user_ids[:FLUSH_USER_CAP]

        stamp = datetime.datetime.now(datetime.timezone.utc)
        pool = self.bot.db_pool
        try:
            payloads = await storage.get_payloads(pool, GAMING_SECTION, user_ids)
        except Exception:
            log.exception("presence: failed to read the aggregates to merge")
            self._buffer.restore(pending)
            return
        except BaseException:
            # BaseException, not Exception, ON PURPOSE: cog_unload cancels this
            # loop and then runs a final flush, so the window that matters is a
            # CancelledError thrown into the await above. CancelledError is a
            # BaseException, so an `except Exception` would skip the restore and
            # the drained generation would be GONE before the final flush could
            # write it - the shutdown would persist an empty buffer over a real
            # interval of minutes. Nothing is swallowed: it is re-raised.
            self._buffer.restore(pending)
            raise

        # Everything not yet decided. A cancellation between two writes has to
        # hand back the users the loop never reached as well as the one it was
        # in the middle of, and this is what remembers them.
        remaining = dict(pending)
        failed = {}
        written = 0
        for user_id, games in pending.items():
            # An absent row means the marker is GONE: the user opted out, ran
            # `profile clear` or /mydata deleteprofile while this interval was
            # accumulating. Their minutes are dropped, not kept for later - and
            # the collector forgets them outright, so the very next event of
            # theirs is rejected by the O(1) opt-in test. The row is the
            # consent, so this SELECT is the authority on it: the erasure wins
            # here even when no delete path told this cog anything.
            payload = payloads.get(user_id)
            if payload is None:
                self.forget_user(user_id)
                self._stats["forgotten"] += 1
                remaining.pop(user_id, None)
                continue
            try:
                merged = merge_games(payload, games, stamp)
                await storage.set_payload(pool, user_id, GAMING_SECTION, merged)
                written += 1
            except base.NotLinked:
                # The row went away between the read and this write - the same
                # erasure as above, one round trip later.
                self.forget_user(user_id)
                self._stats["forgotten"] += 1
            except base.InvalidPayload:
                # Refused by the caps, and it would be refused again with the
                # same input - retrying forever would be the actual bug.
                log.warning(
                    "presence: refused an oversized aggregate for %s", user_id
                )
            except Exception:
                log.exception("presence: failed to store the aggregate for %s", user_id)
                failed[user_id] = games
            except BaseException:
                # Cancelled mid-write (see the method docstring): this user's
                # minutes are not written and neither is anyone's the loop has
                # not reached, so both go back to the buffer before the
                # cancellation continues on its way.
                self._buffer.restore(remaining)
                self._buffer.restore(failed)
                raise
            remaining.pop(user_id, None)
        if failed:
            self._buffer.restore(failed)
        self._stats["flushes"] += 1
        self._stats["written"] += written
        log.debug("presence flush: merged %d aggregate(s)", written)

    # -- the command --------------------------------------------------------

    async def _seed_member_cache(self, ctx):
        """Make sure the opting-in member is in the invoking guild's cache.

        Without this, ``parse_presence_update`` discards their events outright
        (see the module docstring): the bot runs with
        ``chunk_guilds_at_startup=False``, so a member it has not seen is not
        cached, and the opt-in would silently collect nothing.

        ONE gateway request, on a rare deliberate action, for exactly the
        person who asked - never a chunk of the guild. Skipped entirely when
        the member is already cached, which is the common case. Best effort in
        every direction: a gateway that does not answer costs the seed, never
        the opt-in.
        """
        guild = ctx.guild
        if guild is None or guild.get_member(ctx.author.id) is not None:
            return
        try:
            await guild.query_members(
                user_ids=[ctx.author.id], cache=True, presences=True
            )
        except Exception:
            log.warning(
                "presence: could not seed the member cache for %s", ctx.author.id
            )

    async def _turn_on(self, ctx, section):
        """Create (or confirm) one marker row; return True on success."""
        try:
            await storage.set_marker(self.bot.db_pool, ctx.author.id, section)
        except Exception:
            log.exception("Failed to store the %s marker", section)
            return False
        if section == GAMING_SECTION:
            self._opted.add(ctx.author.id)
        return True

    async def _turn_off(self, ctx, section):
        """Delete one marker row and un-publish its section.

        The in-memory state is dropped FIRST: if the delete then fails, the
        worst case is that a still-consenting user stops being collected until
        they say "on" again, which is the failure this must fall towards.
        """
        if section == GAMING_SECTION:
            self.forget_user(ctx.author.id)
        try:
            await storage.unlink(self.bot.db_pool, ctx.author.id, section)
        except Exception:
            log.exception("Failed to remove the %s marker", section)
            return False
        return True

    async def _status_lines(self, user_id):
        """``[(label, is_on), ...]`` for both sections, or None on a failure."""
        try:
            connections = await storage.get_connections(self.bot.db_pool, user_id)
        except Exception:
            log.exception("Failed to read the presence markers for %s", user_id)
            return None
        linked = {row["connector"] for row in connections}
        return [(section, section in linked) for section in base.PRESENCE_SECTIONS]

    async def cmd_presence(self, ctx, *, gaming=None, spotify=None):
        """The body of ``/profile presence``, invoked by the Profiles cog.

        Lives here rather than next to the command because a hybrid subcommand
        must be declared in the same cog as its group (the house lesson from
        the ``/levelconfig`` fold), and the ``profile`` group belongs to
        ``cog.py`` - so the command is declared there and delegates here,
        exactly like that fold does.
        """
        async with ctx.typing(ephemeral=True):
            changes = [(GAMING_SECTION, gaming), (SPOTIFY_SECTION, spotify)]
            wanted = [(section, choice) for section, choice in changes if choice]
            if not wanted:
                await self._answer_status(ctx)
                return

            failed = False
            turned_on = False
            for section, choice in wanted:
                if str(choice).strip().lower() == "on":
                    ok = await self._turn_on(ctx, section)
                    turned_on = turned_on or ok
                else:
                    ok = await self._turn_off(ctx, section)
                failed = failed or not ok
            if failed:
                await ctx.send(
                    _("Failed to update your profile, please try again later."),
                    ephemeral=True,
                )
                return
            if turned_on:
                await self._seed_member_cache(ctx)
            await self._answer_status(ctx)

    async def _answer_status(self, ctx):
        """Say what is on, what is off, and where to publish it.

        One whole sentence per state rather than a label plus a translated
        "On" - a status line that reads as a sentence is what a translator can
        actually move around, and it is what tells a user what the switch
        DOES, which "Now playing: On" never does.
        """
        status = await self._status_lines(ctx.author.id)
        if status is None:
            await ctx.send(
                _("Failed to update your profile, please try again later."),
                ephemeral=True,
            )
            return
        lines = []
        published = False
        for section, is_on in status:
            if section == GAMING_SECTION:
                lines.append(
                    _("Games: I record what you play and show it on your profile.")
                    if is_on
                    else _("Games: off - I record nothing about what you play.")
                )
            else:
                lines.append(
                    _("Spotify: your profile shows what you are listening to, live.")
                    if is_on
                    else _("Spotify: off - your profile shows no listening.")
                )
            published = published or is_on
        if published:
            # The same wording every other section's opt-in uses, pointed at
            # the panel: the text `profile visibility` command deliberately
            # only offers the sections the profile itself STORES, and the
            # panel is the surface that covers the connector ones.
            lines.append(
                _(
                    "Only you can see this for now. Use `{command}` to show it "
                    "to the servers you share."
                ).format(command="{0}profile panel".format(ctx.clean_prefix))
            )
        await ctx.send(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
