"""``Twitch.on_presence_update`` must cost one dict lookup in a guild that has
no Twitch setup.

WHY THIS LISTENER. ``presence_update`` is dispatched once per guild the member
shares with the bot, for every status flip, every custom-status edit and every
game start: after ``on_message`` it is the most frequent event this bot
receives. The body used to open with ``any(isinstance(a, discord.Streaming) ...)``
over ``before.activities`` and a ``next(...)`` over ``after.activities`` - two
generator frames per event, in every guild, before anything had asked whether
the guild uses Twitch at all. ``cogs/community/profile/presence.py`` shows the
shape this should have: one set-membership test that rejects essentially every
event before touching anything.

WHAT THE GATE PROMISES. ``Twitch._acts_in`` memoises, per guild, whether the
listener can act there AT ALL, and the promise is that a False verdict changes
nothing except cost. That promise is silence-shaped - "nothing happened" - so
it is checked here by a DETECTOR rather than by reading the code: every
observable effect the two branches can have (a channel send, a role added, a
role removed) is recorded, the detector is aimed at a configured guild where it
MUST report them, and only then at a gated-out guild where it must stay empty
WITH THE GATE FORCED OPEN. A gate that skipped real work would show up as the
difference between those two runs.

No database, no Discord: a recording pool, in-memory guild/member stand-ins, and
the real ``tools.settings`` LRU seeded by hand.
"""

import types

import discord
import pytest

from cogs.config import twitch
from cogs.config.twitch import LEGACY_ROLE_NAME, Twitch
from tools import settings

GUILD = 500
OTHER_GUILD = 501
MEMBER = 900
CHANNEL = 77
ROLE = 88

BLOB_KEY = (settings._GUILD[0], GUILD)


# ---------------------------------------------------------------------------
# Stand-ins. The member counts activity reads, which is how "the scan was not
# reached" becomes a measurement instead of an assumption.
# ---------------------------------------------------------------------------


class _Role:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


class _Channel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sends = []

    async def send(self, **kwargs):
        self.sends.append(kwargs)


class _Guild:
    def __init__(self, guild_id, roles=(), channel=None):
        self.id = guild_id
        self.name = "Test Guild"
        self.roles = list(roles)
        self._channel = channel

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_channel(self, channel_id):
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
        return None


class _Member:
    """Presence-update member whose ``activities`` reads are COUNTED."""

    def __init__(self, guild, activities=(), roles=()):
        self.id = MEMBER
        self.guild = guild
        self.display_name = "streamer"
        self.mention = "<@{}>".format(MEMBER)
        self.display_avatar = types.SimpleNamespace(url="https://cdn/avatar.png")
        self.roles = list(roles)
        self._activities = tuple(activities)
        self.activity_reads = 0
        self.added = []
        self.removed = []

    @property
    def activities(self):
        self.activity_reads += 1
        return self._activities

    async def add_roles(self, role, reason=None):
        self.added.append((role, reason))

    async def remove_roles(self, role, reason=None):
        self.removed.append((role, reason))


class _Pool:
    """Records every query. ``watch_row`` is what the watchlist lookup finds."""

    def __init__(self, watch_row=None):
        self.calls = []
        self.watch_row = watch_row

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", args))
        return None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", args))
        return self.watch_row

    async def execute(self, query, *args):
        self.calls.append(("execute", args))
        return "INSERT 0 1"


def _streaming():
    return discord.Streaming(name="live now", url="https://twitch.tv/x")


def _cog(watch_row=None):
    cog = Twitch.__new__(Twitch)
    cog.bot = types.SimpleNamespace(db_pool=_Pool(watch_row=watch_row))
    cog._acts_in = {}
    return cog


@pytest.fixture(autouse=True)
def _clean_settings_lru():
    """The tools.settings LRU is process-global; own it for the whole test."""
    settings._cache.clear()
    yield
    settings._cache.clear()


def _seed_blob(blob, guild_id=GUILD):
    settings._cache[(settings._GUILD[0], guild_id)] = {"twitch": blob}


def _seed_no_blob(guild_id=GUILD):
    """A guild whose settings row exists but carries no twitch key."""
    settings._cache[(settings._GUILD[0], guild_id)] = {"locale": "fr"}


# ---------------------------------------------------------------------------
# THE DETECTOR: every observable effect the listener can have.
# ---------------------------------------------------------------------------


def observable_effects(guild, member):
    """Everything the two branches of the listener can do, as a flat list.

    A channel send (the alert), a role added (go live) and a role removed (live
    ended) are the complete set - there is nothing else the body reaches for.
    """
    effects = []
    channel = guild._channel
    if channel is not None:
        effects.extend(("sent", kwargs) for kwargs in channel.sends)
    effects.extend(("role_added", role.id) for role, _r in member.added)
    effects.extend(("role_removed", role.id) for role, _r in member.removed)
    return effects


async def test_the_effect_detector_reports_a_guild_that_really_does_act():
    """NEGATIVE CONTROL, first: aim it where effects MUST appear.

    Without this, "no effects" in the gated-out case would be indistinguishable
    from a detector that watches the wrong objects.
    """
    channel = _Channel(CHANNEL)
    guild = _Guild(GUILD, roles=[_Role(ROLE, "Live streamers")], channel=channel)
    member = _Member(guild, activities=[_streaming()])
    cog = _cog(watch_row={"channel_id": CHANNEL})
    _seed_blob({"enabled": True, "channel_id": CHANNEL, "role_id": ROLE})

    await cog.on_presence_update(_Member(guild), member)

    effects = observable_effects(guild, member)
    assert [kind for kind, _payload in effects] == ["sent", "role_added"]
    assert len(effects) == 2


async def test_the_effect_detector_clears_a_run_that_did_nothing():
    """...and stays empty when the listener genuinely had nothing to do."""
    guild = _Guild(GUILD, channel=_Channel(CHANNEL))
    member = _Member(guild)

    assert observable_effects(guild, member) == []


# ---------------------------------------------------------------------------
# The gate's promise: a False verdict changes cost, never outcome.
# ---------------------------------------------------------------------------


async def test_a_guild_with_no_config_and_no_legacy_role_is_gated_out():
    cog = _cog()
    guild = _Guild(GUILD, roles=[_Role(ROLE, "Moderator")])
    _seed_no_blob()

    await cog.on_presence_update(_Member(guild), _Member(guild))

    assert cog._acts_in == {GUILD: False}


async def test_the_gated_out_guild_would_have_done_nothing_anyway():
    """THE EXACTNESS CLAIM, measured rather than argued.

    Same guild, same real go-live edge, run with the gate FORCED OPEN so the
    whole original body executes. If the gate were hiding real work, the
    detector would report it here - it reported two effects for a configured
    guild three tests above.
    """
    guild = _Guild(GUILD, roles=[_Role(ROLE, "Moderator")], channel=_Channel(CHANNEL))
    before = _Member(guild)
    after = _Member(guild, activities=[_streaming()], roles=list(guild.roles))
    cog = _cog(watch_row={"channel_id": CHANNEL})
    _seed_no_blob()
    cog._acts_in[GUILD] = True  # the gate is not allowed to decide this run

    await cog.on_presence_update(before, after)

    assert observable_effects(guild, after) == []


async def test_a_live_ended_edge_in_a_gated_out_guild_is_also_a_no_op():
    """The other branch, which does NOT check ``enabled`` and so needs proving."""
    guild = _Guild(GUILD, roles=[_Role(ROLE, "Moderator")], channel=_Channel(CHANNEL))
    before = _Member(guild, activities=[_streaming()])
    after = _Member(guild, roles=list(guild.roles))
    cog = _cog()
    _seed_no_blob()
    cog._acts_in[GUILD] = True

    await cog.on_presence_update(before, after)

    assert observable_effects(guild, after) == []


async def test_the_legacy_live_role_keeps_a_config_less_guild_in_the_gate():
    """The case a blob-only gate would have broken, and the reason for the scan.

    ``_resolve_role`` falls back to a role NAMED "Live ..." when no role_id is
    configured, and the live-ended branch never looks at ``enabled``. So a guild
    with no twitch blob at all still strips that role today - and must keep
    doing so.
    """
    legacy = _Role(ROLE, LEGACY_ROLE_NAME)
    guild = _Guild(GUILD, roles=[legacy])
    before = _Member(guild, activities=[_streaming()])
    after = _Member(guild, roles=[legacy])
    cog = _cog()
    _seed_no_blob()

    await cog.on_presence_update(before, after)

    assert cog._acts_in == {GUILD: True}
    assert [role.id for role, _reason in after.removed] == [ROLE]


# ---------------------------------------------------------------------------
# The cost the gate buys.
# ---------------------------------------------------------------------------


async def test_a_gated_out_guild_never_touches_the_activity_lists_again():
    """One probe, then a dict lookup - the activities are not even read."""
    cog = _cog()
    guild = _Guild(GUILD)
    _seed_no_blob()

    first_before, first_after = _Member(guild), _Member(guild)
    await cog.on_presence_update(first_before, first_after)

    later_before = _Member(guild, activities=[_streaming()])
    later_after = _Member(guild, activities=[_streaming()])
    for _ in range(5):
        await cog.on_presence_update(later_before, later_after)

    assert later_before.activity_reads == 0
    assert later_after.activity_reads == 0
    assert cog.bot.db_pool.calls == []  # the blob was warm; nothing re-probed


async def test_the_probe_runs_once_per_guild_not_once_per_event():
    """A cold blob costs ONE read, and only for the first event in that guild."""
    cog = _cog()
    guild = _Guild(GUILD)
    # No seeding at all: the LRU is cold, so the probe reads through.

    for _ in range(4):
        await cog.on_presence_update(_Member(guild), _Member(guild))

    assert len(cog.bot.db_pool.calls) == 1
    assert cog._acts_in == {GUILD: False}


async def test_an_ungated_guild_still_reads_its_activities():
    """The control for the cost claim: a configured guild is NOT short-circuited."""
    cog = _cog()
    guild = _Guild(GUILD)
    _seed_blob({"enabled": False})
    before, after = _Member(guild), _Member(guild)

    await cog.on_presence_update(before, after)

    assert cog._acts_in == {GUILD: True}
    assert before.activity_reads == 1
    assert after.activity_reads == 1


# ---------------------------------------------------------------------------
# Staying correct: every way the verdict can change.
# ---------------------------------------------------------------------------


async def test_saving_a_config_opens_the_gate_for_that_guild():
    cog = _cog()
    cog._acts_in[GUILD] = False

    await cog.save(GUILD, {"enabled": True})

    assert cog._acts_in[GUILD] is True


async def test_creating_or_deleting_a_role_drops_only_that_guilds_verdict():
    cog = _cog()
    cog._acts_in.update({GUILD: False, OTHER_GUILD: False})
    role = _Role(ROLE, LEGACY_ROLE_NAME)
    role.guild = _Guild(GUILD)

    await cog.on_guild_role_create(role)
    assert cog._acts_in == {OTHER_GUILD: False}

    cog._acts_in[GUILD] = True
    await cog.on_guild_role_delete(role)
    assert cog._acts_in == {OTHER_GUILD: False}


async def test_a_rename_drops_the_verdict_and_a_recolour_does_not():
    """The legacy fallback matches on NAME, so only a rename can move a guild."""
    cog = _cog()
    guild = _Guild(GUILD)
    before = _Role(ROLE, "Streamers")
    after = _Role(ROLE, LEGACY_ROLE_NAME)
    after.guild = guild

    cog._acts_in[GUILD] = False
    await cog.on_guild_role_update(before, after)
    assert GUILD not in cog._acts_in

    same_name = _Role(ROLE, LEGACY_ROLE_NAME)
    same_name.guild = guild
    cog._acts_in[GUILD] = True
    await cog.on_guild_role_update(after, same_name)
    assert cog._acts_in == {GUILD: True}


async def test_a_gate_reopened_by_a_rename_acts_again():
    """End to end: gated out, the role appears, and the strip works once more."""
    legacy = _Role(ROLE, LEGACY_ROLE_NAME)
    guild = _Guild(GUILD, roles=[_Role(ROLE, "Streamers")])
    cog = _cog()
    _seed_no_blob()

    await cog.on_presence_update(_Member(guild), _Member(guild))
    assert cog._acts_in == {GUILD: False}

    guild.roles = [legacy]
    renamed = _Role(ROLE, LEGACY_ROLE_NAME)
    renamed.guild = guild
    await cog.on_guild_role_update(_Role(ROLE, "Streamers"), renamed)

    after = _Member(guild, roles=[legacy])
    await cog.on_presence_update(_Member(guild, activities=[_streaming()]), after)

    assert [role.id for role, _reason in after.removed] == [ROLE]


async def test_a_probe_that_blows_up_fails_open_and_is_not_remembered(
    monkeypatch, caplog
):
    """A pool blip must cost a wasted scan, never a missed go-live."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("pool is down")

    monkeypatch.setattr(twitch.settings, "get_guild", _boom)
    cog = _cog()
    guild = _Guild(GUILD)
    before, after = _Member(guild), _Member(guild)

    await cog.on_presence_update(before, after)

    assert cog._acts_in == {}  # nothing memoised from a failure
    assert after.activity_reads == 1  # ...and the body still ran


# ---------------------------------------------------------------------------
# The new cache is real per-guild state, so the three consumers must know it.
# (The purge and the reconnect resync are pinned behaviourally against the real
# cog in tests/test_cache_mirror_registry.py; this is the per-kind notification
# that file does not run.)
# ---------------------------------------------------------------------------


class _SyncBot:
    def __init__(self, cog):
        self.db_pool = _Pool()
        self._cogs = {"Twitch": cog} if cog is not None else {}

    def get_cog(self, name):
        return self._cogs.get(name)


async def test_a_dashboard_twitch_write_reopens_the_gate_for_that_guild():
    """Otherwise enabling alerts from the dashboard would be INERT until a restart.

    The gate's verdict is derived from the very blob the dashboard just wrote,
    so a guild the gate had written off would keep rejecting every presence
    update - alerts silent, dashboard showing them on.
    """
    import json

    from cogs.system import dashboard_sync

    cog = _cog()
    cog._acts_in.update({GUILD: False, OTHER_GUILD: False})

    handled = await dashboard_sync.dispatch(
        _SyncBot(cog), json.dumps({"kind": "twitch", "guildId": str(GUILD)})
    )

    assert handled == "twitch"
    assert cog._acts_in == {OTHER_GUILD: False}


async def test_a_dashboard_twitch_write_without_the_cog_is_a_clean_no_op():
    import json

    from cogs.system import dashboard_sync

    handled = await dashboard_sync.dispatch(
        _SyncBot(None), json.dumps({"kind": "twitch", "guildId": str(GUILD)})
    )

    assert handled == "twitch"
