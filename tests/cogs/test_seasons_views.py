"""Unit tests for the S2 seasons surfaces: ``/halloffame`` and the
``/levelconfig seasons`` admin panel.

Covers cogs/community/leveling/seasons.py's S2 additions (the hall-of-fame browsing
queries, the two admin writes, and the two command bodies) together with
cogs/community/leveling/seasons_views.py's two Components V2 views, driven against the
REAL :class:`Seasons` cog and ``fake_pool`` (the same style as
tests/cogs/test_automod_panel.py) so the SQL these surfaces actually run is
exercised, not just mocked away.

What is pinned here:

* navigation is bounded to seasons that actually exist, and never issues a
  query that could load a guild's whole season history (every hop is a tiny,
  fixed number of indexed ``season_podiums`` lookups);
* opening the hall of fame materializes a missing closed month first
  (``ensure_season_snapshot``, spied), and a guild with no season at all gets
  a sober empty message rather than an empty card;
* a season's podium renders every place in rank order;
* the seasons panel refuses to turn the announce toggle ON unless
  announce_mode is "fixed" with a channel set, but always allows turning it
  OFF; the champion-role write is refused for a role the bot could not
  actually manage; the clear button always succeeds; and the panel is
  author-gated and locale-resolving like every other AuthorLayoutView.
"""

import logging
import types

import discord

from cogs.community.leveling.seasons import Seasons
from cogs.community.leveling.seasons_views import HallOfFameCard, SeasonsPanel
from tools import i18n


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeRole(discord.Object):
    """A ``discord.Object`` subclass, not a plain stand-in: discord.py's
    ``RoleSelect(default_values=...)`` isinstance-checks against
    ``Role``/``Object`` (verified against the real library), so a plain fake
    would be rejected before the panel's own hierarchy guard ever runs."""

    def __init__(self, role_id, position=5, managed=False, default=False):
        super().__init__(id=role_id, type=discord.Role)
        self.position = position
        self.managed = managed
        self._default = default
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class _FakeGuild:
    def __init__(self, guild_id=1, name="guild", roles=(), bot_top_position=100):
        self.id = guild_id
        self.name = name
        self._roles = {r.id: r for r in roles}
        self.me = types.SimpleNamespace(top_role=_FakeRole(0, position=bot_top_position))

    def get_role(self, role_id):
        return self._roles.get(role_id)


class _Ctx:
    def __init__(self, guild=None, author_id=1):
        self.guild = guild or _FakeGuild()
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.sends = []
        # Ordered trace of what the command body did, so a test can pin that the
        # slash interaction was ACKNOWLEDGED before the slow work started.
        self.trace = []

    async def defer(self, **kwargs):
        self.trace.append("defer")

    async def send(self, *args, **kwargs):
        self.trace.append("send")
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=999)


def _make_cog(fake_pool):
    return Seasons(types.SimpleNamespace(db_pool=fake_pool))


def _wire_hof(fake_pool, seasons):
    """Wire ``fake_pool`` to behave like ``season_podiums`` for one guild.

    ``seasons``: ``{period_key: [(rank, user_id, xp), ...]}``. Every query the
    S2 read surfaces issue is routed off this single dict, PK-scan style (see
    cogs/community/leveling/seasons.py's "hall of fame browsing queries").
    """
    keys = sorted(seasons)  # lexical == chronological (zero-padded 'M%Y-%m')

    async def _fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "period_key < $2" in query:
            older = [k for k in keys if k < args[1]]
            return older[-1] if older else None
        if "period_key > $2" in query:
            newer = [k for k in keys if k > args[1]]
            return newer[0] if newer else None
        return keys[-1] if keys else None  # the latest-season query

    async def _fetch(query, *args):
        fake_pool.calls.append(("fetch", query, args))
        rows = seasons.get(args[1], [])
        return [{"rank": r, "user_id": u, "xp": x} for r, u, x in rows]

    fake_pool.fetchval = _fetchval
    fake_pool.fetch = _fetch


def _panel_state(**overrides):
    state = {
        "champion_role_id": None,
        "season_announce": False,
        "announce_mode": "channel",
        "announce_channel_id": None,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Navigation: bounded to existing seasons, indexed hop by hop
# ---------------------------------------------------------------------------
async def test_navigation_walks_older_and_newer_bounded(fake_pool, make_interaction):
    seasons = {
        "M2026-04": [(1, 10, 100)],
        "M2026-05": [(1, 11, 200)],
        "M2026-06": [(1, 12, 300)],
    }
    _wire_hof(fake_pool, seasons)
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(1)

    view = HallOfFameCard(cog, guild, 1, "M2026-06", seasons["M2026-06"], True, False)
    view.message = types.SimpleNamespace()

    # Oldest -> newest: Next disabled at the top, Prev enabled below it.
    assert view.has_newer is False
    assert view.has_older is True

    interaction = make_interaction()
    await view._older(interaction)
    assert view.period_key == "M2026-05"
    assert view.has_older is True
    assert view.has_newer is True

    interaction2 = make_interaction()
    await view._older(interaction2)
    assert view.period_key == "M2026-04"
    assert view.has_older is False  # bottomed out: the oldest season on record
    assert view.has_newer is True

    # A further Prev at the bound is a no-op (deferred, never changes state).
    interaction3 = make_interaction()
    await view._older(interaction3)
    assert view.period_key == "M2026-04"
    assert interaction3.defers  # guarded even without the disabled button

    interaction4 = make_interaction()
    await view._newer(interaction4)
    assert view.period_key == "M2026-05"
    interaction5 = make_interaction()
    await view._newer(interaction5)
    assert view.period_key == "M2026-06"
    assert view.has_newer is False  # back at the top


async def test_navigation_never_loads_the_whole_season_history(fake_pool, make_interaction):
    """Every hop costs a small, fixed number of PK-served lookups - never a
    query that could return every season a guild ever had."""
    seasons = {f"M2026-{m:02d}": [(1, 1, 10)] for m in range(1, 11)}
    _wire_hof(fake_pool, seasons)
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(1)
    view = HallOfFameCard(cog, guild, 1, "M2026-10", seasons["M2026-10"], True, False)
    view.message = types.SimpleNamespace()

    fake_pool.calls.clear()
    await view._older(make_interaction())

    assert len(fake_pool.calls) == 3  # older key, podium rows, older-of-older key
    for _method, query, _args in fake_pool.calls:
        assert "LIMIT" in query or "period_key = $2" in query


async def test_page_flip_never_pings_the_members_it_reveals(fake_pool, make_interaction):
    """A page flip swaps in a DIFFERENT season's members, and the bot's
    client-wide default allows user mentions (core.Yasuho: users=True), which
    discord.py folds into every edit. Without an explicit suppression a
    stranger's browsing would notify people about a season they won months
    ago - so every edit must carry AllowedMentions.none()."""
    seasons = {"M2026-05": [(1, 11, 200)], "M2026-06": [(1, 12, 300)]}
    _wire_hof(fake_pool, seasons)
    cog = _make_cog(fake_pool)
    view = HallOfFameCard(
        cog, _FakeGuild(1), 1, "M2026-06", seasons["M2026-06"], True, False
    )
    view.message = types.SimpleNamespace()

    older = make_interaction()
    await view._older(older)
    assert older.edits[0][1]["allowed_mentions"].users is False

    newer = make_interaction()
    await view._newer(newer)
    assert newer.edits[0][1]["allowed_mentions"].users is False


def test_podium_rendered_with_correct_ranks(fake_pool):
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(1)
    podium = [(1, 11, 900), (2, 22, 700), (3, 33, 10)]
    view = HallOfFameCard(cog, guild, 1, "M2026-06", podium, False, False)

    text = _dump_text(view)
    # Rank order is preserved and every member is mentioned.
    assert text.index("<@11>") < text.index("<@22>") < text.index("<@33>")
    assert "900 XP" in text and "700 XP" in text and "10 XP" in text


def _dump_text(view):
    chunks = []

    def walk(item):
        content = getattr(item, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        for child in getattr(item, "children", None) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# cmd_halloffame: materializes on open, sober empty state
# ---------------------------------------------------------------------------
async def test_cmd_halloffame_materializes_via_ensure_season_snapshot(fake_pool):
    _wire_hof(fake_pool, {"M2026-06": [(1, 1, 10)]})
    cog = _make_cog(fake_pool)
    calls = []

    async def _spy(guild, *args, **kwargs):
        calls.append(guild)
        ctx.trace.append("snapshot")

    cog.ensure_season_snapshot = _spy
    ctx = _Ctx(guild=_FakeGuild(1))

    await cog.cmd_halloffame(ctx)

    assert calls == [ctx.guild]  # the on-demand materialization the contract promises
    assert isinstance(ctx.sends[0][1]["view"], HallOfFameCard)
    # The snapshot can fetch members, move the champion role and post the
    # announce - way past the 3s an un-deferred slash interaction gets, so the
    # ack has to come FIRST or the whole command dies on "did not respond".
    assert ctx.trace[0] == "defer"
    assert ctx.trace.index("defer") < ctx.trace.index("snapshot")


async def test_cmd_halloffame_defers_before_the_empty_state_too(fake_pool):
    """The ack is unconditional: even the guild with no season at all pays the
    snapshot resolution before we know there is nothing to show."""
    _wire_hof(fake_pool, {})
    cog = _make_cog(fake_pool)
    cog.ensure_season_snapshot = _no_op_snapshot
    ctx = _Ctx(guild=_FakeGuild(1))

    await cog.cmd_halloffame(ctx)

    assert ctx.trace == ["defer", "send"]


async def test_cmd_halloffame_empty_state_is_a_sober_message(fake_pool):
    _wire_hof(fake_pool, {})  # no season ever frozen for this guild
    cog = _make_cog(fake_pool)
    cog.ensure_season_snapshot = _no_op_snapshot
    ctx = _Ctx(guild=_FakeGuild(1))

    await cog.cmd_halloffame(ctx)

    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert "view" not in kwargs  # a plain message, no card at all
    assert args  # a sober text, not silence


async def _no_op_snapshot(guild, *args, **kwargs):
    return []


async def test_cmd_halloffame_opens_on_latest_next_disabled(fake_pool):
    _wire_hof(
        fake_pool,
        {"M2026-05": [(1, 1, 10)], "M2026-06": [(1, 2, 20)]},
    )
    cog = _make_cog(fake_pool)
    cog.ensure_season_snapshot = _no_op_snapshot
    ctx = _Ctx(guild=_FakeGuild(1))

    await cog.cmd_halloffame(ctx)

    view = ctx.sends[0][1]["view"]
    assert view.period_key == "M2026-06"
    assert view.has_newer is False
    assert view.has_older is True


# ---------------------------------------------------------------------------
# Seasons panel: announce toggle guard
# ---------------------------------------------------------------------------
async def test_toggle_refused_without_a_fixed_channel(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    state = _panel_state(announce_mode="channel", announce_channel_id=None)
    panel = SeasonsPanel(cog, _FakeGuild(5), 1, state)
    panel.message = types.SimpleNamespace()
    interaction = make_interaction()

    await panel.toggle_announce(interaction)

    assert panel.state["season_announce"] is False
    assert not any("season_announce" in c[1] for c in fake_pool.calls if c[0] == "execute")
    assert interaction.sent  # an ephemeral refusal, explaining what to do first


async def test_toggle_accepted_with_a_fixed_channel(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    state = _panel_state(announce_mode="fixed", announce_channel_id=555)
    panel = SeasonsPanel(cog, _FakeGuild(5), 1, state)
    panel.message = types.SimpleNamespace()

    await panel.toggle_announce(make_interaction())

    assert panel.state["season_announce"] is True
    write = next(c for c in fake_pool.calls if c[0] == "execute")
    assert write[2] == (5, True)


async def test_turning_the_announce_off_is_always_allowed(fake_pool, make_interaction):
    """Even with a misconfigured (non-fixed) announce_mode, OFF must succeed -
    only turning it ON while inert is refused."""
    cog = _make_cog(fake_pool)
    state = _panel_state(
        season_announce=True, announce_mode="channel", announce_channel_id=None
    )
    panel = SeasonsPanel(cog, _FakeGuild(5), 1, state)
    panel.message = types.SimpleNamespace()

    await panel.toggle_announce(make_interaction())

    assert panel.state["season_announce"] is False
    write = next(c for c in fake_pool.calls if c[0] == "execute")
    assert write[2] == (5, False)


# ---------------------------------------------------------------------------
# Seasons panel: champion role select + clear
# ---------------------------------------------------------------------------
async def test_role_above_the_bot_is_refused(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(5, bot_top_position=10)
    role = _FakeRole(50, position=999)  # above the bot's top role
    panel = SeasonsPanel(cog, guild, 1, _panel_state())
    panel.message = types.SimpleNamespace()

    await panel.set_champion_role(make_interaction(), role)

    assert panel.state["champion_role_id"] is None
    assert not any(c[0] == "execute" for c in fake_pool.calls)


async def test_role_managed_is_refused(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(5)
    role = _FakeRole(50, managed=True)
    panel = SeasonsPanel(cog, guild, 1, _panel_state())
    panel.message = types.SimpleNamespace()

    await panel.set_champion_role(make_interaction(), role)

    assert panel.state["champion_role_id"] is None


async def test_assignable_role_is_accepted(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(5, bot_top_position=100)
    role = _FakeRole(50, position=10)
    panel = SeasonsPanel(cog, guild, 1, _panel_state())
    panel.message = types.SimpleNamespace()

    await panel.set_champion_role(make_interaction(), role)

    assert panel.state["champion_role_id"] == 50
    write = next(c for c in fake_pool.calls if c[0] == "execute")
    assert write[2] == (5, 50)


async def test_clear_role_always_succeeds(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    guild = _FakeGuild(5, roles=[_FakeRole(50)])
    panel = SeasonsPanel(cog, guild, 1, _panel_state(champion_role_id=50))
    panel.message = types.SimpleNamespace()

    await panel.clear_champion_role(make_interaction())

    assert panel.state["champion_role_id"] is None
    write = next(c for c in fake_pool.calls if c[0] == "execute")
    assert write[2] == (5, None)


# ---------------------------------------------------------------------------
# Panel plumbing: author-gate + locale (shared AuthorLayoutView contract)
# ---------------------------------------------------------------------------
async def test_panel_rejects_a_non_author_interaction(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = SeasonsPanel(cog, _FakeGuild(5), 1, _panel_state())
    interaction = make_interaction(user_id=999)

    allowed = await panel.interaction_check(interaction)

    assert allowed is False
    assert interaction.sent
    assert "isn't for you" in interaction.sent[0][0][0]


async def test_panel_resolves_the_clicker_locale(fake_pool, make_interaction, monkeypatch):
    cog = _make_cog(fake_pool)
    panel = SeasonsPanel(cog, _FakeGuild(5), 1, _panel_state())
    interaction = make_interaction(user_id=1)
    calls = []

    async def _spy(interaction_arg):
        calls.append(interaction_arg)

    monkeypatch.setattr(i18n, "apply_interaction_locale", _spy)

    allowed = await panel.interaction_check(interaction)

    assert allowed is True
    assert calls == [interaction]


# ---------------------------------------------------------------------------
# Pager observability: a crashing hop must ANSWER the clicker
# ---------------------------------------------------------------------------
async def _boom(*args, **kwargs):
    raise RuntimeError("db down")


async def test_a_failing_prev_hop_tells_the_clicker(fake_pool, make_interaction, caplog):
    """A bare `log.exception` leaves the member on Discord's own opaque "This
    interaction failed" - indistinguishable from a dead bot. The hop still
    fails soft (never re-raised into the callback task), but it now also
    answers, with the house generic failure msgid."""
    _wire_hof(fake_pool, {"M2026-05": [(1, 11, 200)], "M2026-06": [(1, 12, 300)]})
    cog = _make_cog(fake_pool)
    cog.older_season_key = _boom
    view = HallOfFameCard(cog, _FakeGuild(1), 1, "M2026-06", [(1, 12, 300)], True, False)
    view.message = types.SimpleNamespace()

    interaction = make_interaction()
    with caplog.at_level(logging.ERROR):
        await view._older(interaction)  # must not raise

    assert interaction.sent  # the clicker got an ephemeral answer
    assert interaction.sent[0][1]["ephemeral"] is True
    assert "Hall of fame prev failed" in caplog.text


async def test_a_failing_next_hop_tells_the_clicker(fake_pool, make_interaction, caplog):
    _wire_hof(fake_pool, {"M2026-05": [(1, 11, 200)], "M2026-06": [(1, 12, 300)]})
    cog = _make_cog(fake_pool)
    cog.newer_season_key = _boom
    view = HallOfFameCard(cog, _FakeGuild(1), 1, "M2026-05", [(1, 11, 200)], False, True)
    view.message = types.SimpleNamespace()

    interaction = make_interaction()
    with caplog.at_level(logging.ERROR):
        await view._newer(interaction)

    assert interaction.sent
    assert "Hall of fame next failed" in caplog.text


def test_the_pager_failure_reuses_an_existing_msgid():
    """Zero new strings for this fix: the wording is the house generic already
    translated in every locale (cogs/config/announcements.py and friends), so
    the pager answers in the clicker's language on day one - no extraction, no
    translation round trip."""
    import pathlib

    import cogs.community.leveling.seasons_views as views_module

    source_path = pathlib.Path(views_module.__file__)
    repo_root = source_path.parents[3]  # cogs/community/leveling/x.py -> repo root
    assert source_path.read_text(encoding="utf-8").count(
        '_("Something went wrong.")'
    ) == 2
    po = (repo_root / "locales/fr/LC_MESSAGES/yasuho.po").read_text(encoding="utf-8")
    assert 'msgid "Something went wrong."' in po
