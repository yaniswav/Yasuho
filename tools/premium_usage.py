"""Real usage of every premium-raisable limit (L4: .claude/plans/monetisation/).

``?premiumadmin usage`` (cogs/system/premium.py) is the owner's one-shot
diagnostic for setting limits from DATA rather than guesses: for every
GuildLimits/UserLimits field that a refusal can actually be shown for (see
tools/premium_upsell.py), this module runs ONE bounded aggregate query over
the whole fleet, computing percentiles IN SQL (PG11-compatible
``percentile_cont``/``FILTER``) - never a per-row Python loop - and reports
how many scopes are already pressing against the FREE ceiling.

THIS MODULE IS PURE SQL TEXT + PURE PYTHON. No cog, no Discord, no
``bot.db_pool`` reference lives here - :func:`fetch_all` takes a bare pool
and :func:`render_report` takes plain dicts, so both halves are testable
with no network and no live database (the owner-run PG11 probe is a manual
verification step, not something this module's own test suite depends on).

ONE QUERY PER RESOURCE, SHAPED THE SAME WAY. Every :data:`RESOURCE_SPECS`
entry's ``query`` groups the underlying table by its scope (a guild, a user,
a feed, a playlist, ...), then aggregates ONCE over that per-scope count:

    SELECT count(*)                                          AS scopes,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY cnt)  AS median,
           percentile_cont(0.9)  WITHIN GROUP (ORDER BY cnt)  AS p90,
           percentile_cont(0.99) WITHIN GROUP (ORDER BY cnt)  AS p99,
           max(cnt)                                           AS max,
           count(*) FILTER (WHERE cnt >= $1)                  AS at_80,
           count(*) FILTER (WHERE cnt >= $2)                  AS at_100
    FROM (SELECT <scope cols>, count(*) AS cnt FROM <table> GROUP BY <scope cols>) s

``$1``/``$2`` are the 80%/100%-of-FREE-limit thresholds (:func:`thresholds`),
computed in Python and bound as query parameters - never a division inside
the query, which is what keeps this safe for the one resource whose FREE
value is 0 (``music_247``: see :data:`RESOURCE_SPECS`' own note on it).
"scopes with >= 1 item" is never a separate COUNT: a scope with zero rows
never appears in the inner GROUP BY at all, so the outer ``count(*)`` IS
that number, by construction - the two resources whose underlying column can
genuinely be zero while the row still exists (``playlist_tracks``,
``voice_hubs``) filter it out explicitly in their own inner query.

COVERAGE (why this file, not a hand-maintained report). Every field of
:class:`tools.premium.GuildLimits`/:class:`tools.premium.UserLimits` must
either map to exactly one entry in :data:`RESOURCE_SPECS` (via its ``field``)
or be named in :data:`EXCLUDED_GUILD_FIELDS`/:data:`EXCLUDED_USER_FIELDS`
with a one-line reason. :func:`missing_resource_mappings` is the pure check
tests/tools/test_premium_usage.py runs against the REAL dataclasses, so a
future field (the ticket-field addition landing alongside this lot, or any
later one) that is neither mapped nor excluded fails that test loudly rather
than silently missing from the report - the exact "guards need a negative
control" lesson this tree already applies elsewhere (see
tests/tools/test_premium_upsell_sites.py's own docstring). This module never
hardcodes "the current field list": it reads ``dataclasses.fields(...)``
itself, so a field RENAME or VALUE change in tools/premium.py (another lot's
own work) needs no edit here - only a genuinely NEW, unmapped field does.

TWO FIELDS ARE DELIBERATELY NOT "ONE QUERY = ONE FIELD":
``tickets_open_per_guild`` has no ``field`` of its own (nothing in
GuildLimits caps "open tickets in this guild" directly - the cap is
per-member, ``max_tickets_open_per_user``) and exists purely as the
context the plan's "per member (and per guild)" ask wants; it is never
counted by :func:`missing_resource_mappings` because it never claims a
field. ``max_guild_playlists``/``max_playlist_tracks`` map to the two
HALVES of "server playlists per guild (and tracks per playlist)" - two
separate scopes (a guild; a playlist), two separate :data:`RESOURCE_SPECS`
entries, each claiming its own field.
"""

from __future__ import annotations

import dataclasses
import math

from tools import premium

# ---------------------------------------------------------------------------
# Resource specs
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ResourceSpec:
    """One measured resource: a scope, the GuildLimits/UserLimits field it
    maps to (``None`` for a context-only resource - see the module docstring),
    and the ONE bounded query that reports its usage."""

    key: str
    label: str
    scope: str  # "guild" | "user"
    field: str | None
    query: str


def _query(from_sql, *, filter_zero=False):
    """Build the shared outer aggregate over ``SELECT <cols>, count(*) AS cnt
    FROM ... GROUP BY <cols>`` (``from_sql``) - see the module docstring's
    "ONE QUERY PER RESOURCE" section for the exact shape. ``filter_zero``
    guards the two resources whose inner count can genuinely be zero while
    the scope row still exists (a playlist with no tracks, a guild with an
    empty ``autorooms`` array) - everywhere else a zero-count scope simply
    never appears in ``from_sql``'s own GROUP BY, so no filter is needed.
    """
    inner = f"SELECT * FROM ({from_sql}) s0 WHERE cnt >= 1" if filter_zero else from_sql
    return (
        "SELECT count(*) AS scopes, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY cnt) AS median, "
        "percentile_cont(0.9) WITHIN GROUP (ORDER BY cnt) AS p90, "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY cnt) AS p99, "
        "max(cnt) AS max, "
        "count(*) FILTER (WHERE cnt >= $1) AS at_80, "
        "count(*) FILTER (WHERE cnt >= $2) AS at_100 "
        f"FROM ({inner}) s"
    )


# Guild-scoped resources.
_GUILD_PLAYLISTS_SQL = (
    "SELECT guild_id, count(*) AS cnt FROM guild_playlists GROUP BY guild_id"
)
_PLAYLIST_TRACKS_SQL = (
    "SELECT guild_id, name_norm, track_count AS cnt FROM guild_playlists"
)
_ANILIST_FEEDS_SQL = (
    "SELECT guild_id, count(*) AS cnt FROM anilist_feeds GROUP BY guild_id"
)
_ANILIST_FOLLOWS_SQL = (
    "SELECT guild_id, channel_id, count(*) AS cnt FROM anilist_follows "
    "GROUP BY guild_id, channel_id"
)
_ANILIST_SUBS_SQL = (
    "SELECT guild_id, channel_id, count(*) AS cnt FROM anilist_channel_subs "
    "GROUP BY guild_id, channel_id"
)
_ROLE_MENUS_SQL = "SELECT guild_id, count(*) AS cnt FROM role_menus GROUP BY guild_id"
_VOICE_HUBS_SQL = (
    "SELECT guild_id, CASE WHEN jsonb_typeof(settings->'autorooms') = 'array' "
    "THEN jsonb_array_length(settings->'autorooms') END AS cnt "
    "FROM guild_settings"
)
_TICKETS_OPEN_PER_MEMBER_SQL = (
    "SELECT guild_id, opener_id, count(*) AS cnt FROM tickets "
    "WHERE status = 'open' GROUP BY guild_id, opener_id"
)
_TICKETS_OPEN_PER_GUILD_SQL = (
    "SELECT guild_id, count(*) AS cnt FROM tickets "
    "WHERE status = 'open' GROUP BY guild_id"
)
# music_247: a BOOLEAN capability (FREE_MUSIC_247 = False), not a numeric
# cap - every row is one guild with 24/7 currently configured, so the inner
# query's "count" is always exactly 1 per scope. Percentiles collapse to 1
# and the 80%/100%-of-FREE thresholds collapse to 0 (see :func:`thresholds`),
# which makes at_80/at_100 both equal to "scopes" - documented, not a bug:
# there is no partial-use reading of a binary feature.
_MUSIC_247_SQL = "SELECT guild_id, 1 AS cnt FROM music_247"

# User-scoped resources.
_FAVOURITES_SQL = (
    "SELECT user_id, count(*) AS cnt FROM music_favorites GROUP BY user_id"
)
_PENDING_REMINDERS_SQL = (
    "SELECT extra->>'author_id' AS author_id, count(*) AS cnt FROM timers "
    "WHERE event = 'reminder' AND extra->>'author_id' IS NOT NULL "
    "GROUP BY extra->>'author_id'"
)
_RECURRING_REMINDERS_SQL = (
    "SELECT extra->>'author_id' AS author_id, count(*) AS cnt FROM timers "
    "WHERE event = 'reminder' AND extra->>'author_id' IS NOT NULL "
    "AND extra->>'repeat_seconds' IS NOT NULL "
    "GROUP BY extra->>'author_id'"
)


RESOURCE_SPECS = [
    ResourceSpec(
        key="guild_playlists",
        label="server playlists / guild",
        scope="guild",
        field="max_guild_playlists",
        query=_query(_GUILD_PLAYLISTS_SQL),
    ),
    ResourceSpec(
        key="playlist_tracks",
        label="tracks / playlist",
        scope="guild",
        field="max_playlist_tracks",
        query=_query(_PLAYLIST_TRACKS_SQL, filter_zero=True),
    ),
    ResourceSpec(
        key="anilist_feeds",
        label="AniList feeds / guild",
        scope="guild",
        field="max_feeds_per_guild",
        query=_query(_ANILIST_FEEDS_SQL),
    ),
    ResourceSpec(
        key="anilist_follows",
        label="follows / feed",
        scope="guild",
        field="max_follows_per_feed",
        query=_query(_ANILIST_FOLLOWS_SQL),
    ),
    ResourceSpec(
        key="anilist_subs",
        label="subs / feed",
        scope="guild",
        field="max_subs_per_feed",
        query=_query(_ANILIST_SUBS_SQL),
    ),
    ResourceSpec(
        key="role_menus",
        label="role menus / guild",
        scope="guild",
        field="max_menus_per_guild",
        query=_query(_ROLE_MENUS_SQL),
    ),
    ResourceSpec(
        key="voice_hubs",
        label="voice hubs / guild",
        scope="guild",
        field="max_hubs",
        query=_query(_VOICE_HUBS_SQL, filter_zero=True),
    ),
    ResourceSpec(
        key="tickets_open_per_member",
        label="open tickets / member",
        scope="guild",
        field="max_tickets_open_per_user",
        query=_query(_TICKETS_OPEN_PER_MEMBER_SQL),
    ),
    ResourceSpec(
        key="tickets_open_per_guild",
        label="open tickets / guild",
        scope="guild",
        field=None,  # context only - see module docstring
        query=_query(_TICKETS_OPEN_PER_GUILD_SQL),
    ),
    ResourceSpec(
        key="music_247",
        label="24/7 music enabled",
        scope="guild",
        field="music_247",
        query=_query(_MUSIC_247_SQL),
    ),
    ResourceSpec(
        key="favourites",
        label="favourites / user",
        scope="user",
        field="max_favourites",
        query=_query(_FAVOURITES_SQL),
    ),
    ResourceSpec(
        key="reminders_pending",
        label="pending reminders / user",
        scope="user",
        field="max_pending_reminders",
        query=_query(_PENDING_REMINDERS_SQL),
    ),
    ResourceSpec(
        key="reminders_recurring",
        label="recurring reminders / user",
        scope="user",
        field="max_recurring_reminders",
        query=_query(_RECURRING_REMINDERS_SQL),
    ),
]

_SPEC_BY_KEY = {spec.key: spec for spec in RESOURCE_SPECS}

# Fields with no premium_upsell refusal site to measure against - documented,
# not an oversight (see tools/premium.py's module docstring for what each one
# actually is). A future field that is neither here nor mapped by a
# RESOURCE_SPECS entry fails test_premium_usage.py's coverage test.
EXCLUDED_GUILD_FIELDS = {
    "history_max_items": "truncates silently (no refusal, no premium_upsell site)",
    "serverstats_retention_days": "a retention window, not a count that can be refused",
    "premium_badge": "cosmetic boolean, nothing to refuse",
}
EXCLUDED_USER_FIELDS = {}


def missing_resource_mappings(fields, specs, excluded):
    """Every field name in ``fields`` that ``specs`` does not map (by its own
    ``.field``) and ``excluded`` does not document - the pure check behind
    the coverage test. ``fields``/``excluded`` are plain iterables of field
    names (:func:`coverage_gaps` is the real caller, passing
    ``dataclasses.fields(...)`` names and the module's own exclusion dicts);
    kept this generic so the test can also feed it a synthetic field list to
    prove the detector actually flags a gap (the negative control)."""
    mapped = {spec.field for spec in specs if spec.field is not None}
    return [name for name in fields if name not in mapped and name not in excluded]


def coverage_gaps():
    """:func:`missing_resource_mappings` run against the REAL
    GuildLimits/UserLimits dataclasses - what the owner would need to add a
    RESOURCE_SPECS entry (or an exclusion) for, right now. Empty when
    everything is accounted for; test_premium_usage.py asserts exactly that."""
    guild_fields = [f.name for f in dataclasses.fields(premium.GuildLimits)]
    user_fields = [f.name for f in dataclasses.fields(premium.UserLimits)]
    guild_specs = [s for s in RESOURCE_SPECS if s.scope == "guild"]
    user_specs = [s for s in RESOURCE_SPECS if s.scope == "user"]
    return missing_resource_mappings(
        guild_fields, guild_specs, EXCLUDED_GUILD_FIELDS
    ) + missing_resource_mappings(user_fields, user_specs, EXCLUDED_USER_FIELDS)


# ---------------------------------------------------------------------------
# Thresholds - pure, no SQL, no I/O
# ---------------------------------------------------------------------------


def thresholds(free_limit):
    """The two ``(at_80, at_100)`` query parameters for one resource's FREE
    value: ``at_100`` is the FREE limit itself, ``at_80`` is 80% of it rounded
    UP (``math.ceil``) so "at or over 80%" never includes a scope one whole
    unit short of that fraction (e.g. 80% of 25 is exactly 20, but 80% of 5 is
    4 - ceil keeps both exact rather than truncating 4.0 down to a weaker
    3). ``free_limit == 0`` (today, only ``music_247``) collapses both to 0 -
    documented at the call site, not a division, so there is nothing to
    divide by zero here in the first place."""
    if free_limit <= 0:
        return 0, 0
    return math.ceil(free_limit * 0.8), free_limit


# ---------------------------------------------------------------------------
# Running the queries
# ---------------------------------------------------------------------------


def free_value(spec):
    """The FREE-tier value :data:`RESOURCE_SPECS` entry ``spec`` maps to, or
    ``None`` for a context-only resource (``field is None``)."""
    if spec.field is None:
        return None
    source = premium.GUILD_FREE if spec.scope == "guild" else premium.USER_FREE
    return getattr(source, spec.field)


def premium_value(spec):
    """The Yasuho+/Pack Confort twin of :func:`free_value`."""
    if spec.field is None:
        return None
    source = premium.GUILD_PREMIUM if spec.scope == "guild" else premium.USER_PREMIUM
    return getattr(source, spec.field)


async def fetch_one(pool, spec):
    """Run ``spec``'s own query and return a plain dict of its one row.

    ``free_value(spec)`` feeds :func:`thresholds` for the ``$1``/``$2``
    parameters (0/0 for the context-only ``tickets_open_per_guild``, which
    has no field and so no FREE value either - still a valid, if
    uninformative, pair of thresholds: see :func:`thresholds`'s own
    ``free_limit <= 0`` branch).
    """
    free = free_value(spec)
    at_80, at_100 = thresholds(free or 0)
    row = await pool.fetchrow(spec.query, at_80, at_100)
    if row is None:
        return {
            "scopes": 0,
            "median": None,
            "p90": None,
            "p99": None,
            "max": None,
            "at_80": 0,
            "at_100": 0,
        }
    return dict(row)


async def fetch_all(pool):
    """Every :data:`RESOURCE_SPECS` entry's row, keyed by ``.key``.

    One ``fetchrow`` per resource (bounded: a GROUP BY + aggregate, never a
    per-row Python loop over the underlying table) - fine to run on demand
    from an owner-only command, never on a hot path."""
    return {spec.key: await fetch_one(pool, spec) for spec in RESOURCE_SPECS}


# ---------------------------------------------------------------------------
# Rendering - pure, no I/O
# ---------------------------------------------------------------------------

_HEADER = (
    f"{'resource':<26}{'scopes':>7}{'median':>8}{'p90':>8}{'p99':>8}"
    f"{'max':>6}{'>=80%':>7}{'>=100%':>8}{'free':>6}{'plus':>6}"
)


def _fmt_num(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return f"{value:.1f}"
    return str(value)


def format_resource_line(spec, row):
    """One fixed-width line of the report for ``spec``, from ``row`` (the
    dict :func:`fetch_one` returns). Pure string formatting - no DB, no bot -
    so this is what tests/tools/test_premium_usage.py exercises directly with
    hand-built rows, rather than a live query."""
    free = free_value(spec)
    plus = premium_value(spec)
    return (
        f"{spec.label:<26}{_fmt_num(row.get('scopes')):>7}"
        f"{_fmt_num(row.get('median')):>8}{_fmt_num(row.get('p90')):>8}"
        f"{_fmt_num(row.get('p99')):>8}{_fmt_num(row.get('max')):>6}"
        f"{_fmt_num(row.get('at_80')):>7}{_fmt_num(row.get('at_100')):>8}"
        f"{_fmt_num(free):>6}{_fmt_num(plus):>6}"
    )


# Discord's own message budget (2000 chars) minus the ```/``` fence (6 chars
# + 2 newlines) and a small margin - see cogs/system/premium.py's own
# _join_within_budget for the established "stop before crossing, say how many
# are left out" shape this mirrors rather than duplicates outright (this one
# chunks into several CODE BLOCKS, each one a separate message, rather than
# truncating a single field).
_MESSAGE_BUDGET = 1900


def render_report(rows_by_key):
    """Every :data:`RESOURCE_SPECS` row rendered as fixed-width lines, chunked
    into code-block-ready message bodies under Discord's 2000-char limit.

    Returns a list of plain-text chunks (the caller wraps each in its own
    ```` ``` ```` fence and sends it as its own message) - never a single
    string, so the owner command never risks an HTTPException on a long
    fleet's worth of resources.
    """
    lines = [_HEADER]
    for spec in RESOURCE_SPECS:
        row = rows_by_key.get(spec.key, {})
        lines.append(format_resource_line(spec, row))

    chunks = []
    current = []
    current_len = 0
    for line in lines:
        added = len(line) + 1
        if current and current_len + added > _MESSAGE_BUDGET:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += added
    if current:
        chunks.append("\n".join(current))
    return chunks
