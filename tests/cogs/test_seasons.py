"""Unit tests for cogs.community.seasons.Seasons (leveling seasons, S1).

The engine that closes a calendar month: it freezes the month's top 3 into
season_podiums EXACTLY ONCE, then - only for the caller that actually won that
insert - moves the optional champion role and posts the optional announce.

What is pinned here (the cog side, against fakes):

* exactly-once: an already-frozen season short-circuits before the expensive
  query, and a caller whose INSERT returned nothing (a lost race, or a month
  with no XP at all) runs NO side effect;
* a caller that names the closed season is obeyed verbatim, and one that
  cannot gets it RESOLVED from the data (the guild's latest month with XP
  before the current one) - never "the month before now", which would skip a
  guild that stayed silent for a whole month;
* the champion role is REPLACE-shaped (previous holder stripped, winner
  granted), does NOT depend on the member cache (get_member then fetch_member,
  and last season's rank 1 read from season_podiums rather than Role.members)
  and is best effort at every step (deleted role, role above the bot, winner
  gone, HTTP failure) - the podium always stands;
* the announce is opt-in, lands only where the guild's announce_mode actually
  resolves a channel, renders in the GUILD's locale, and pings users only.

The period-key maths and the announce-channel resolution are pure and live in
tests/tools/test_leveling_service.py; the rollover DETECTION (the leveling
cog's in-memory marker) lives in tests/cogs/test_leveling.py.
"""

import datetime
import logging
import types

import discord
import pytest

from cogs.community.seasons import Seasons
from tools import i18n, leveling

# ---------------------------------------------------------------------------
# Fakes: role / member / guild / channel, shaped just enough for the hierarchy
# guard (role.is_default(), role.managed, role < guild.me.top_role), the
# role moves, and one observable channel.send.
# ---------------------------------------------------------------------------


class _FakeRole:
    def __init__(self, role_id, position=5, managed=False, default=False, members=()):
        self.id = role_id
        self.position = position
        self.managed = managed
        self._default = default
        self.mention = f"<@&{role_id}>"
        self.members = list(members)

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __repr__(self):
        return f"_FakeRole({self.id})"


class _FakeMember:
    def __init__(self, member_id, roles=()):
        self.id = member_id
        self.mention = f"<@{member_id}>"
        self.roles = list(roles)
        self.added = []
        self.removed = []
        self.add_raises = None
        self.remove_raises = None

    async def add_roles(self, role, reason=None):
        if self.add_raises is not None:
            raise self.add_raises
        self.added.append((role, reason))
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        if self.remove_raises is not None:
            raise self.remove_raises
        self.removed.append((role, reason))
        self.roles = [r for r in self.roles if r.id != role.id]


class _FakeChannel:
    def __init__(self, channel_id=500):
        self.id = channel_id
        self.sends = []
        self.raises = None

    async def send(self, content=None, **kwargs):
        if self.raises is not None:
            raise self.raises
        # Capture the ACTIVE locale at send time: the announce must render
        # inside the guild's locale block, not after it was reset.
        self.sends.append((content, kwargs, i18n.current_locale.get()))


class _FakeGuild:
    """A guild with a member CACHE and a separate member DIRECTORY.

    The split is the whole point: the bot runs with
    chunk_guilds_at_startup=False and MESSAGE_CREATE never caches a member, so
    ``get_member`` returning None for somebody who is very much still in the
    guild is the NORMAL case in production. ``members`` seeds the cache
    (get_member hits), ``uncached`` seeds only the directory (get_member misses,
    fetch_member resolves) and ``gone`` members are in neither.
    """

    def __init__(
        self,
        guild_id=1,
        roles=(),
        members=(),
        channels=None,
        bot_top_position=100,
        uncached=(),
    ):
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self.preferred_locale = "en-US"
        self._roles = {r.id: r for r in roles}
        self._members = {m.id: m for m in members}
        self._directory = {m.id: m for m in (*members, *uncached)}
        self._channels = dict(channels or {})
        self.fetches = []
        self.me = types.SimpleNamespace(
            top_role=_FakeRole(0, position=bot_top_position)
        )

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, member_id):
        return self._members.get(member_id)

    async def fetch_member(self, member_id):
        self.fetches.append(member_id)
        member = self._directory.get(member_id)
        if member is None:
            raise discord.NotFound(_FakeHTTPResponse(), "Unknown Member")
        return member

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeHTTPResponse:
    status = 429
    reason = "Too Many Requests"


def _http_exc(message="boom"):
    return discord.HTTPException(_FakeHTTPResponse(), message)


def _make_bot(fake_pool):
    return types.SimpleNamespace(db_pool=fake_pool)


def _podium_rows(*triples):
    """(rank, user_id, xp) triples -> asyncpg-Record-shaped mappings."""
    return [{"rank": r, "user_id": u, "xp": x} for r, u, x in triples]


def _config_row(**overrides):
    """A level_config season row as _SEASON_CONFIG_SQL returns it."""
    row = {
        "season_champion_role_id": None,
        "season_announce": False,
        "announce_mode": "channel",
        "announce_channel_id": None,
    }
    row.update(overrides)
    return row


_JULY = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=datetime.timezone.utc)
_CLOSED = "M2026-06"  # the season _JULY closes


def _arrange(
    fake_pool,
    *,
    exists=None,
    podium=None,
    config=None,
    previous_champion=None,
    closed_month=_CLOSED,
):
    """Wire the engine's queries onto the FakePool.

    ``fetch`` is the snapshot INSERT ... RETURNING and ``fetchrow`` the config
    read - one each, so the plain per-method returns cover them. ``fetchval``
    now serves THREE different queries, so it is routed by SQL instead:
    ``closed_month`` (which month a caller that did not name one is asking
    about), ``exists`` (the exactly-once probe) and ``previous_champion`` (last
    season's rank 1, the cache-independent REPLACE target).
    """
    fake_pool.fetch_return = list(podium or [])
    fake_pool.fetchrow_return = config

    async def _fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "FROM xp_period" in query:
            return closed_month
        if "rank = 1" in query:
            return previous_champion
        return exists

    fake_pool.fetchval = _fetchval


def _queries(fake_pool, needle):
    """Every recorded call whose SQL contains ``needle``."""
    return [c for c in fake_pool.calls if needle in c[1]]


def _exists_probe(fake_pool):
    return _queries(fake_pool, "SELECT 1 FROM season_podiums")[0]


def _month_resolutions(fake_pool):
    """The 'which month closed?' lookups (never the snapshot, which also reads
    xp_period)."""
    return _queries(fake_pool, "period_key LIKE 'M%'")


@pytest.fixture(autouse=True)
def _stub_locale(monkeypatch):
    """Keep resolve_guild_locale off the (fake) DB by default; announce tests
    that care about the locale override it themselves."""

    async def _resolve(_bot, _guild):
        return "en"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _resolve)


# ---------------------------------------------------------------------------
# Snapshot core: exactly-once, defaults, ordering, failure tolerance
# ---------------------------------------------------------------------------


async def test_snapshot_freezes_the_closed_month_podium(fake_pool):
    _arrange(fake_pool, podium=_podium_rows((1, 11, 900), (2, 22, 700), (3, 33, 10)))
    cog = Seasons(_make_bot(fake_pool))

    podium = await cog.ensure_season_snapshot(_FakeGuild(1), now=_JULY)

    assert podium == [(1, 11, 900), (2, 22, 700), (3, 33, 10)]
    inserts = [c for c in fake_pool.calls if c[0] == "fetch"]
    assert len(inserts) == 1
    _method, query, args = inserts[0]
    assert "INSERT INTO season_podiums" in query
    assert "ON CONFLICT (guild_id, period_key, rank) DO NOTHING" in query
    # The tie-break is part of the contract: a re-run must never reshuffle a
    # stored podium, so equal XP is always ordered by the LOWER user id.
    assert "ORDER BY xp DESC, user_id ASC" in query
    assert args == (1, "M2026-06", leveling.SEASON_PODIUM_SIZE)


async def test_a_caller_without_a_period_key_resolves_the_last_active_month(
    fake_pool,
):
    """A guild active in JUNE and totally silent in JULY, asked in AUGUST: the
    closed season is JUNE, not the empty month before now. The resolution asks
    xp_period for the latest month strictly before the current one."""
    _arrange(
        fake_pool, podium=_podium_rows((1, 11, 900)), closed_month="M2026-06"
    )
    cog = Seasons(_make_bot(fake_pool))
    august = datetime.datetime(2026, 8, 3, tzinfo=datetime.timezone.utc)

    await cog.ensure_season_snapshot(_FakeGuild(1), now=august)

    resolve = _month_resolutions(fake_pool)[0]
    assert resolve[2] == (1, "M2026-08")  # everything strictly before NOW's month
    assert _exists_probe(fake_pool)[2] == (1, "M2026-06")


async def test_a_guild_with_no_closed_month_is_a_quiet_noop(fake_pool):
    """A brand new guild (or one whose old rows retention already dropped) has
    no season to freeze - and must not fall back to inventing one."""
    _arrange(fake_pool, podium=_podium_rows((1, 11, 900)), closed_month=None)
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(_FakeGuild(1), now=_JULY) == []
    assert _queries(fake_pool, "INSERT INTO season_podiums") == []


async def test_explicit_period_key_is_honoured_without_any_resolution(fake_pool):
    """The activity path always names the month its marker knows closed, so it
    must never pay the resolution query - nor let it override the answer."""
    _arrange(
        fake_pool, podium=_podium_rows((1, 11, 900)), closed_month="M2026-06"
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(_FakeGuild(1), "M2025-03", now=_JULY)

    assert _month_resolutions(fake_pool) == []
    assert _exists_probe(fake_pool)[2] == (1, "M2025-03")


async def test_already_frozen_season_short_circuits_before_any_work(fake_pool):
    """The cheap PK probe answers a repeat trigger (restart, LRU eviction, a
    read surface opening twice) - no sort over the month's rows, no config
    read, no side effect."""
    role = _FakeRole(50)
    winner = _FakeMember(11)
    _arrange(
        fake_pool,
        exists=1,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50, season_announce=True),
    )
    cog = Seasons(_make_bot(fake_pool))

    podium = await cog.ensure_season_snapshot(
        _FakeGuild(1, roles=[role], members=[winner]), _CLOSED, now=_JULY
    )

    assert podium == []
    assert [c[0] for c in fake_pool.calls] == ["fetchval"]
    assert winner.added == []


async def test_month_without_activity_freezes_nothing(fake_pool):
    _arrange(fake_pool, podium=[], config=_config_row(season_announce=True))
    cog = Seasons(_make_bot(fake_pool))

    podium = await cog.ensure_season_snapshot(_FakeGuild(1), _CLOSED, now=_JULY)

    assert podium == []
    # No config read at all: nothing was inserted, so there is nothing to
    # celebrate and no side effect may run.
    assert [c[0] for c in fake_pool.calls] == ["fetchval", "fetch"]


async def test_lost_insert_race_runs_no_side_effect(fake_pool):
    """Two triggers can fire at once (a grant and a read surface). The INSERT
    returns rows ONLY to the one that actually wrote them, and that empty
    result is what keeps the loser silent - never a second announce."""
    role = _FakeRole(50)
    winner = _FakeMember(11)
    channel = _FakeChannel(500)
    guild = _FakeGuild(1, roles=[role], members=[winner], channels={500: channel})
    _arrange(
        fake_pool,
        exists=None,  # the probe raced too: still absent when we looked
        podium=[],  # ...but the INSERT conflicted on every rank
        config=_config_row(
            season_champion_role_id=50,
            season_announce=True,
            announce_mode="fixed",
            announce_channel_id=500,
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == []
    assert winner.added == []
    assert channel.sends == []


async def test_podium_is_returned_rank_ordered(fake_pool):
    """RETURNING has no guaranteed row order, so the engine sorts by rank -
    the champion must always be podium[0]."""
    _arrange(fake_pool, podium=_podium_rows((3, 33, 10), (1, 11, 900), (2, 22, 700)))
    cog = Seasons(_make_bot(fake_pool))

    podium = await cog.ensure_season_snapshot(_FakeGuild(1), now=_JULY)

    assert [rank for rank, _u, _x in podium] == [1, 2, 3]
    assert podium[0][1] == 11


async def test_a_podium_without_rank_1_crowns_nobody(fake_pool, caplog):
    """Every side effect crowns podium[0], so a RETURNING that does not start
    at rank 1 must run NONE of them.

    Representable in production: a concurrent trigger that inserted rank 1 a
    moment earlier leaves this caller only the ranks it did not have, and the
    ON CONFLICT DO NOTHING makes that a partial, non-empty result. Crowning the
    runner-up is strictly worse than crowning nobody - and the winner's own
    trigger already ran the effects.
    """
    channel = _FakeChannel(500)
    role = _FakeRole(50)
    runner_up = _FakeMember(22)
    guild = _FakeGuild(
        1, roles=[role], members=[runner_up], channels={500: channel}
    )
    _arrange(
        fake_pool,
        podium=_podium_rows((2, 22, 700), (3, 33, 10)),  # rank 1 lost the race
        config=_config_row(
            season_champion_role_id=50,
            season_announce=True,
            announce_mode="fixed",
            announce_channel_id=500,
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    with caplog.at_level(logging.WARNING, logger="cogs.community.seasons"):
        podium = await cog.ensure_season_snapshot(guild, _CLOSED, now=_JULY)

    assert podium == [(2, 22, 700), (3, 33, 10)]  # what we wrote is what we report
    assert runner_up.added == []
    assert channel.sends == []
    # Never silent: this is a race that should be visible if it ever happens.
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_db_failure_never_raises_and_freezes_nothing(fake_pool):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    fake_pool.fetchval = boom
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(_FakeGuild(1), now=_JULY) == []


async def test_none_guild_is_a_quiet_noop(fake_pool):
    cog = Seasons(_make_bot(fake_pool))
    assert await cog.ensure_season_snapshot(None, now=_JULY) == []
    assert fake_pool.calls == []


async def test_side_effect_failure_never_undoes_the_snapshot(fake_pool):
    """The podium row is committed before any side effect; a config-read blow-up
    must still report the season as closed."""
    _arrange(fake_pool, podium=_podium_rows((1, 11, 900)))

    async def boom(*args, **kwargs):
        raise RuntimeError("config read down")

    fake_pool.fetchrow = boom
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(_FakeGuild(1), now=_JULY) == [
        (1, 11, 900)
    ]


async def test_guild_without_a_level_config_row_runs_no_side_effect(fake_pool):
    _arrange(fake_pool, podium=_podium_rows((1, 11, 900)), config=None)
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(_FakeGuild(1), now=_JULY) == [
        (1, 11, 900)
    ]


# ---------------------------------------------------------------------------
# The backfill guard: freezing an OLD season is welcome, crowning it is not
# ---------------------------------------------------------------------------
#
# The S2 contract in ensure_season_snapshot's docstring: a caller may name any
# genuinely closed month, but the champion role and the announce belong to the
# LAST closed one. A backfill that crowned a months-old winner would strip the
# CURRENT champion of their role and ping a channel about a finished month.


async def test_a_backfilled_older_season_is_frozen_without_side_effects(fake_pool):
    channel = _FakeChannel(500)
    role = _FakeRole(50)
    winner = _FakeMember(11)
    guild = _FakeGuild(
        1, roles=[role], members=[winner], channels={500: channel}
    )
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        closed_month="M2026-06",  # the LAST closed month...
        config=_config_row(
            season_champion_role_id=50,
            season_announce=True,
            announce_mode="fixed",
            announce_channel_id=500,
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    # ...while the caller names a much older one.
    podium = await cog.ensure_season_snapshot(guild, "M2026-02", now=_JULY)

    assert podium == [(1, 11, 900)]  # the history IS frozen
    assert _exists_probe(fake_pool)[2] == (1, "M2026-02")
    assert winner.added == []  # but nobody is crowned
    assert channel.sends == []


async def test_the_caller_named_latest_closed_month_still_runs_the_effects(
    fake_pool,
):
    """The activity path always names a month - the one its marker just closed,
    which IS the latest. The guard must wave it through."""
    role = _FakeRole(50)
    winner = _FakeMember(11)
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        closed_month=_CLOSED,
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, _CLOSED, now=_JULY)

    assert [r.id for r, _reason in winner.added] == [50]


async def test_a_resolved_month_is_never_re_verified(fake_pool):
    """A key WE resolved is the latest closed month by construction - paying a
    second identical lookup to prove it would be pure waste."""
    role = _FakeRole(50)
    winner = _FakeMember(11)
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)  # no period_key

    assert len(_month_resolutions(fake_pool)) == 1  # the resolution, not a check
    assert [r.id for r, _reason in winner.added] == [50]


async def test_a_guild_with_no_season_knob_on_pays_no_verification(fake_pool):
    """The guard's lookup is only ever paid by a guild that actually opted into
    an effect - the overwhelming majority never touch it."""
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=None, season_announce=False),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(_FakeGuild(1), _CLOSED, now=_JULY)

    assert _month_resolutions(fake_pool) == []


async def test_a_failing_latest_month_check_still_crowns_the_winner(fake_pool):
    """Fail OPEN: the caller-named key is overwhelmingly the live rollover, so
    a DB hiccup on the verification must not cost that guild its champion."""
    role = _FakeRole(50)
    winner = _FakeMember(11)
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    real_fetchval = fake_pool.fetchval

    async def _fetchval(query, *args):
        if "period_key LIKE 'M%'" in query:
            raise RuntimeError("db down")
        return await real_fetchval(query, *args)

    fake_pool.fetchval = _fetchval
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, _CLOSED, now=_JULY)

    assert [r.id for r, _reason in winner.added] == [50]


# ---------------------------------------------------------------------------
# Champion role (optional, REPLACE-shaped, best effort)
# ---------------------------------------------------------------------------


async def test_champion_role_replaces_the_previous_holder(fake_pool):
    previous = _FakeMember(99)
    winner = _FakeMember(11)
    role = _FakeRole(50, members=[previous])
    previous.roles.append(role)
    guild = _FakeGuild(1, roles=[role], members=[previous, winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert [r.id for r, _reason in previous.removed] == [50]
    assert [r.id for r, _reason in winner.added] == [50]


async def test_champion_role_is_skipped_when_unset(fake_pool):
    winner = _FakeMember(11)
    guild = _FakeGuild(1, members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=None),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert winner.added == []


async def test_deleted_champion_role_is_tolerated(fake_pool):
    winner = _FakeMember(11)
    guild = _FakeGuild(1, roles=[], members=[winner])  # role 50 is gone
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]
    assert winner.added == []


async def test_champion_role_above_the_bot_is_skipped(fake_pool):
    previous = _FakeMember(99)
    winner = _FakeMember(11)
    role = _FakeRole(50, position=500, members=[previous])  # above the bot's top
    guild = _FakeGuild(
        1, roles=[role], members=[previous, winner], bot_top_position=100
    )
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]
    assert winner.added == []
    assert previous.removed == []  # nothing is attempted at all


async def test_managed_champion_role_is_skipped(fake_pool):
    winner = _FakeMember(11)
    role = _FakeRole(50, managed=True)
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert winner.added == []


async def test_winner_who_left_still_strips_the_previous_champion(fake_pool):
    """REPLACE means the role names the CURRENT champion or nobody - a winner
    who left the guild must not leave last season's holder wearing it."""
    previous = _FakeMember(99)
    role = _FakeRole(50, members=[previous])
    previous.roles.append(role)
    guild = _FakeGuild(1, roles=[role], members=[previous])  # winner 11 is gone
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]
    assert [r.id for r, _reason in previous.removed] == [50]


async def test_back_to_back_champion_keeps_the_role_without_a_re_add(fake_pool):
    role = _FakeRole(50)
    winner = _FakeMember(11, roles=[role])
    role.members = [winner]
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50, season_announce=False),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert winner.removed == []  # never stripped from itself
    assert winner.added == []  # nor pointlessly re-added


async def test_champion_grant_http_failure_is_swallowed(fake_pool):
    winner = _FakeMember(11)
    winner.add_raises = _http_exc()
    role = _FakeRole(50)
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]


async def test_champion_strip_http_failure_does_not_block_the_grant(fake_pool):
    previous = _FakeMember(99)
    previous.remove_raises = _http_exc()
    winner = _FakeMember(11)
    role = _FakeRole(50, members=[previous])
    previous.roles.append(role)
    guild = _FakeGuild(1, roles=[role], members=[previous, winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert [r.id for r, _reason in winner.added] == [50]


# --- member cache independence (chunk_guilds_at_startup=False) -------------
#
# The bot never chunks its guilds and MESSAGE_CREATE does not cache members, so
# guild.get_member is a coin flip in production - and the season winner has NO
# reason to be cached, since somebody ELSE's message triggered the rollover.
# Every one of these would pass against a fully cached fake guild; they exist
# precisely because prod is not that.


async def test_champion_role_is_granted_to_an_UNCACHED_winner(fake_pool):
    """The regression that mattered: cache-only, the #1 resolves to None, the
    role is never granted and the log claims they left the guild."""
    winner = _FakeMember(11)
    role = _FakeRole(50)
    guild = _FakeGuild(1, roles=[role], uncached=[winner])  # cache MISS on 11
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert guild.fetches == [11]  # exactly one fetch, once per guild per month
    assert [r.id for r, _reason in winner.added] == [50]


async def test_previous_champion_is_stripped_from_the_podium_table(fake_pool):
    """REPLACE cannot lean on Role.members: it reads the member CACHE, which is
    typically EMPTY here. Last season's rank 1 comes from season_podiums."""
    previous = _FakeMember(99)
    role = _FakeRole(50)  # role.members is empty, exactly like prod
    previous.roles.append(role)
    winner = _FakeMember(11)
    guild = _FakeGuild(1, roles=[role], uncached=[previous, winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
        previous_champion=99,
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    lookup = _queries(fake_pool, "rank = 1")[0]
    assert lookup[2] == (1, _CLOSED)  # strictly BEFORE the season being closed
    assert [r.id for r, _reason in previous.removed] == [50]
    assert [r.id for r, _reason in winner.added] == [50]


async def test_a_back_to_back_champion_is_never_stripped_from_itself(fake_pool):
    """The podium lookup can legitimately return the NEW winner."""
    role = _FakeRole(50)
    winner = _FakeMember(11, roles=[role])
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
        previous_champion=11,
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert winner.removed == []
    assert winner.added == []


async def test_a_previous_champion_who_no_longer_holds_the_role_costs_no_call(
    fake_pool,
):
    """An admin who already took the role back by hand must not cost a pointless
    rate-limited remove_roles."""
    previous = _FakeMember(99)  # resolvable, but roles is empty
    winner = _FakeMember(11)
    role = _FakeRole(50)
    guild = _FakeGuild(1, roles=[role], members=[previous, winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
        previous_champion=99,
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert previous.removed == []
    assert [r.id for r, _reason in winner.added] == [50]


async def test_a_winner_who_really_left_is_still_only_one_fetch(fake_pool):
    """Cache miss AND fetch NotFound: the member is genuinely gone. The podium
    stands, the previous champion is still stripped, nothing raises."""
    previous = _FakeMember(99)
    role = _FakeRole(50)
    previous.roles.append(role)
    guild = _FakeGuild(1, roles=[role], uncached=[previous])  # winner 11 is gone
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
        previous_champion=99,
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]
    assert [r.id for r, _reason in previous.removed] == [50]
    assert guild.fetches == [99, 11]


async def test_a_failing_previous_champion_lookup_still_grants_the_role(fake_pool):
    """A DB hiccup on the REPLACE lookup must never cost the winner their role."""
    winner = _FakeMember(11)
    role = _FakeRole(50)
    guild = _FakeGuild(1, roles=[role], members=[winner])
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(season_champion_role_id=50),
    )
    real_fetchval = fake_pool.fetchval

    async def _fetchval(query, *args):
        if "rank = 1" in query:
            raise RuntimeError("db down")
        return await real_fetchval(query, *args)

    fake_pool.fetchval = _fetchval
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert [r.id for r, _reason in winner.added] == [50]


# ---------------------------------------------------------------------------
# Announce (opt-in, channel resolved from the existing announce_mode)
# ---------------------------------------------------------------------------


def _announce_guild(channel_id=500):
    channel = _FakeChannel(channel_id)
    guild = _FakeGuild(1, channels={channel_id: channel})
    return guild, channel


async def test_announce_is_off_by_default(fake_pool):
    guild, channel = _announce_guild()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=False, announce_mode="fixed", announce_channel_id=500
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert channel.sends == []


async def test_announce_posts_the_podium_with_users_only_mentions(fake_pool):
    guild, channel = _announce_guild()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900), (2, 22, 700), (3, 33, 10)),
        config=_config_row(
            season_announce=True, announce_mode="fixed", announce_channel_id=500
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert len(channel.sends) == 1
    content, kwargs, _loc = channel.sends[0]
    assert "2026-06" in content  # the CLOSED month, humanized
    assert content.count("\n") == 3  # header + exactly three podium lines
    for user_id in (11, 22, 33):
        assert f"<@{user_id}>" in content
    mentions = kwargs["allowed_mentions"]
    assert (mentions.users, mentions.roles, mentions.everyone) == (True, False, False)


async def test_announce_renders_in_the_guild_locale(fake_pool, monkeypatch):
    async def _resolve(_bot, _guild):
        return "fr"

    monkeypatch.setattr(i18n, "resolve_guild_locale", _resolve)
    guild, channel = _announce_guild()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=True, announce_mode="fixed", announce_channel_id=500
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert channel.sends[0][2] == "fr"  # captured INSIDE the locale block
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE  # and reset after


async def test_announce_names_the_champion_role_when_it_moved(fake_pool):
    channel = _FakeChannel(500)
    role = _FakeRole(50)
    winner = _FakeMember(11)
    guild = _FakeGuild(
        1, roles=[role], members=[winner], channels={500: channel}
    )
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_champion_role_id=50,
            season_announce=True,
            announce_mode="fixed",
            announce_channel_id=500,
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    content, kwargs, _loc = channel.sends[0]
    assert "<@&50>" in content
    # The role mention is suppressed, so naming the champion role never pings
    # every member who ever held it.
    assert kwargs["allowed_mentions"].roles is False


async def test_announce_omits_the_champion_line_when_the_role_did_not_move(
    fake_pool,
):
    channel = _FakeChannel(500)
    guild = _FakeGuild(1, roles=[], channels={500: channel})  # role 50 deleted
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_champion_role_id=50,
            season_announce=True,
            announce_mode="fixed",
            announce_channel_id=500,
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    content, _kwargs, _loc = channel.sends[0]
    assert "<@&50>" not in content
    assert content.count("\n") == 1  # header + the single podium line


async def test_announce_mode_off_silences_seasons_too(fake_pool):
    guild, channel = _announce_guild()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=True, announce_mode="off", announce_channel_id=500
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert channel.sends == []


@pytest.mark.parametrize("mode", ["channel", "dm"])
async def test_announce_without_a_configured_channel_is_skipped(fake_pool, mode):
    """A season announce is guild-wide: no origin channel, no single member to
    DM. Only a configured channel can receive it - never an invented one."""
    guild, channel = _announce_guild()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=True, announce_mode=mode, announce_channel_id=None
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    await cog.ensure_season_snapshot(guild, now=_JULY)

    assert channel.sends == []


async def test_an_announce_that_can_never_land_is_logged_as_a_WARNING(
    fake_pool, caplog
):
    """announce_mode defaults to 'channel', which resolves to NO channel for a
    guild-wide announce. An admin who flips the season toggle alone therefore
    gets a silently dead feature, so the skip must be diagnosable in prod - a
    debug line would be invisible. At most once per guild per month."""
    guild, _channel = _announce_guild()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=True, announce_mode="channel", announce_channel_id=None
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    with caplog.at_level(logging.WARNING, logger="cogs.community.seasons"):
        await cog.ensure_season_snapshot(guild, now=_JULY)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "announce" in warnings[0].getMessage().lower()


async def test_missing_announce_channel_is_tolerated(fake_pool):
    guild = _FakeGuild(1, channels={})  # channel 500 was deleted
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=True, announce_mode="fixed", announce_channel_id=500
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]


async def test_announce_send_failure_never_undoes_the_snapshot(fake_pool):
    guild, channel = _announce_guild()
    channel.raises = _http_exc()
    _arrange(
        fake_pool,
        podium=_podium_rows((1, 11, 900)),
        config=_config_row(
            season_announce=True, announce_mode="fixed", announce_channel_id=500
        ),
    )
    cog = Seasons(_make_bot(fake_pool))

    assert await cog.ensure_season_snapshot(guild, now=_JULY) == [(1, 11, 900)]
