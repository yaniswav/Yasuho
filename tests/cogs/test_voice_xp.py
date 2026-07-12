"""Unit tests for cogs.community.voice_xp.VoiceXP (leveling L7).

The pure decisions (is_voice_xp_eligible, voice_credit, build_voice_grant_payload)
are covered in tests/tools/test_leveling_service.py; these drive the COG against
fakes for the four things it owns:

* the listener's session bookkeeping (join creates, move repoints the channel
  while keeping the running marker, leave ends) and its zero-work non-matching
  path (bots, guilds without voice XP create no session);
* the sweep's eligibility gate, credit maths and marker advance, feeding ONE
  batched write (build_voice_grant_payload -> a single db_pool.fetch);
* the sweep's eviction of dead sessions (member left voice/guild without an
  event, or the guild toggled voice XP off);
* the level-up routing (only actual threshold crossings await the Leveling
  cog's credit_voice_levelup seam) and the on_ready one-shot seeding.
"""

import types

from cogs.community import voice_xp
from cogs.community.voice_xp import VoiceXP, _VoiceSession
from tools import leveling


# ---------------------------------------------------------------------------
# Fakes: just enough of discord's voice surface + the Leveling cross-cog seam.
# ---------------------------------------------------------------------------
class _Chan:
    def __init__(self, cid, members=(), category_id=None):
        self.id = cid
        self.members = list(members)
        self.category_id = category_id


class _VS:
    """A discord.VoiceState stand-in."""

    def __init__(self, channel=None, self_deaf=False, self_mute=False):
        self.channel = channel
        self.self_deaf = self_deaf
        self.self_mute = self_mute


class _Member:
    def __init__(self, uid, guild=None, bot=False, voice=None, roles=()):
        self.id = uid
        self.guild = guild
        self.bot = bot
        self.voice = voice
        self.roles = [types.SimpleNamespace(id=r) for r in roles]


class _Guild:
    def __init__(self, gid=1, members=None, afk_channel=None, voice_channels=()):
        self.id = gid
        self._members = {m.id: m for m in (members or [])}
        self.afk_channel = afk_channel
        self.voice_channels = list(voice_channels)

    def get_member(self, uid):
        return self._members.get(uid)


class _FakeLeveling:
    """Stand-in for the Leveling cog's cross-cog surface the VoiceXP cog reads."""

    def __init__(self, configs=None, snapshots=None, multiplier_snapshots=None):
        self._configs = configs or {}
        self._snapshots = snapshots or {}
        self._multiplier_snapshots = multiplier_snapshots or {}
        self.levelup_calls = []
        self.prune_calls = []  # L6: (guild_id, now) per maybe_prune_expired_periods call

    def get_config(self, guild_id):
        return self._configs.get(guild_id)

    async def ensure_no_xp_snapshot(self, guild_id):
        return self._snapshots.get(guild_id, leveling.EMPTY_NO_XP_SNAPSHOT)

    async def ensure_multiplier_snapshot(self, guild_id):
        return self._multiplier_snapshots.get(
            guild_id, leveling.EMPTY_MULTIPLIER_SNAPSHOT
        )

    async def credit_voice_levelup(self, **kwargs):
        self.levelup_calls.append(kwargs)

    async def maybe_prune_expired_periods(self, guild_id, now=None):
        self.prune_calls.append((guild_id, now))


class _FakeBot:
    def __init__(self, leveling_cog=None, pool=None, guilds=()):
        self._leveling = leveling_cog
        self.db_pool = pool
        self.guilds = list(guilds)
        self._by_id = {g.id: g for g in guilds}

    def get_cog(self, name):
        return self._leveling if name == "Leveling" else None

    def get_guild(self, gid):
        return self._by_id.get(gid)

    async def wait_until_ready(self):
        pass


def _cfg(*, voice_on=True, rate=5, enabled=True):
    return leveling.LevelConfig(
        enabled=enabled, voice_xp_enabled=voice_on, voice_xp_per_minute=rate
    )


# ---------------------------------------------------------------------------
# Listener: the non-matching path creates nothing (zero work).
# ---------------------------------------------------------------------------
async def test_listener_ignores_bots(fake_pool):
    lvl = _FakeLeveling(configs={1: _cfg()})
    cog = VoiceXP(_FakeBot(lvl, fake_pool))
    guild = _Guild(1)
    member = _Member(2, guild=guild, bot=True)
    await cog.on_voice_state_update(member, _VS(None), _VS(_Chan(10)))
    assert cog._sessions == {}


async def test_listener_ignores_guilds_without_voice_xp(fake_pool):
    # Guild present in the config map (leveling on) but voice XP OFF.
    lvl = _FakeLeveling(configs={1: _cfg(voice_on=False)})
    cog = VoiceXP(_FakeBot(lvl, fake_pool))
    guild = _Guild(1)
    member = _Member(2, guild=guild)
    await cog.on_voice_state_update(member, _VS(None), _VS(_Chan(10)))
    assert cog._sessions == {}


async def test_listener_ignores_guilds_with_leveling_off(fake_pool):
    # No config at all == leveling off == absent from the map.
    lvl = _FakeLeveling(configs={})
    cog = VoiceXP(_FakeBot(lvl, fake_pool))
    member = _Member(2, guild=_Guild(1))
    await cog.on_voice_state_update(member, _VS(None), _VS(_Chan(10)))
    assert cog._sessions == {}


async def test_listener_join_creates_a_session(fake_pool):
    lvl = _FakeLeveling(configs={1: _cfg()})
    cog = VoiceXP(_FakeBot(lvl, fake_pool))
    member = _Member(2, guild=_Guild(1))
    await cog.on_voice_state_update(member, _VS(None), _VS(_Chan(10)))
    assert (1, 2) in cog._sessions
    assert cog._sessions[(1, 2)].channel_id == 10


# ---------------------------------------------------------------------------
# _apply_transition: deterministic bookkeeping with an injected clock.
# ---------------------------------------------------------------------------
def test_apply_transition_join_then_move_keeps_the_marker(fake_pool):
    cog = VoiceXP(_FakeBot(_FakeLeveling(), fake_pool))
    member = _Member(2, guild=_Guild(1))

    cog._apply_transition(member, _VS(None), _VS(_Chan(10)), now=100.0)
    assert cog._sessions[(1, 2)] == _VoiceSession(channel_id=10, last_credit=100.0)

    # A MOVE repoints the channel but keeps the running marker (they keep
    # accruing across rooms) - last_credit is NOT reset to the new `now`.
    cog._apply_transition(member, _VS(_Chan(10)), _VS(_Chan(20)), now=200.0)
    assert cog._sessions[(1, 2)] == _VoiceSession(channel_id=20, last_credit=100.0)


def test_apply_transition_leave_ends_the_session(fake_pool):
    cog = VoiceXP(_FakeBot(_FakeLeveling(), fake_pool))
    member = _Member(2, guild=_Guild(1))
    cog._apply_transition(member, _VS(None), _VS(_Chan(10)), now=100.0)
    cog._apply_transition(member, _VS(_Chan(10)), _VS(None), now=150.0)
    assert (1, 2) not in cog._sessions


def test_apply_transition_same_channel_is_a_no_op(fake_pool):
    cog = VoiceXP(_FakeBot(_FakeLeveling(), fake_pool))
    member = _Member(2, guild=_Guild(1))
    cog._apply_transition(member, _VS(None), _VS(_Chan(10)), now=100.0)
    # A mute/deaf toggle: same channel id before and after - nothing changes.
    cog._apply_transition(
        member, _VS(_Chan(10)), _VS(_Chan(10), self_mute=True), now=250.0
    )
    assert cog._sessions[(1, 2)] == _VoiceSession(channel_id=10, last_credit=100.0)


def test_start_session_evicts_oldest_at_the_cap(fake_pool, monkeypatch):
    """The hard backstop: once the map is at SESSION_CAP, a new session evicts
    the oldest-inserted entry so the map can never grow without bound."""
    monkeypatch.setattr(voice_xp, "SESSION_CAP", 3)
    cog = VoiceXP(_FakeBot(_FakeLeveling(), fake_pool))
    for uid in (1, 2, 3):
        cog._start_session(1, uid, 10, now=float(uid))
    assert len(cog._sessions) == 3
    cog._start_session(1, 4, 10, now=4.0)  # over cap -> evict oldest (uid 1)
    assert len(cog._sessions) == 3
    assert (1, 1) not in cog._sessions
    assert (1, 4) in cog._sessions


# ---------------------------------------------------------------------------
# The sweep: eligibility, credit maths, the single batched write.
# ---------------------------------------------------------------------------
def _wire_sweep(fake_pool, *, rate=5, humans=2, channel_id=10, category_id=None,
                self_deaf=False, self_mute=False, afk=False, snapshot=None,
                multiplier_snapshot=None, returned_xp=11000, roles=()):
    """Build a one-guild, one-eligible-member scenario. Returns (cog, lvl, member,
    session). The member shares channel `channel_id` with `humans` non-bot
    people. `returned_xp` is the batch RETURNING total for that member."""
    others = [_Member(900 + i, bot=False) for i in range(max(humans - 1, 0))]
    bot_padding = [_Member(800, bot=True)]  # a bot never counts toward humans
    channel = _Chan(channel_id, category_id=category_id)
    afk_channel = _Chan(999) if afk else _Chan(555)
    guild = _Guild(1, afk_channel=afk_channel)
    member = _Member(
        2,
        guild=guild,
        voice=_VS(channel, self_deaf=self_deaf, self_mute=self_mute),
        roles=roles,
    )
    channel.members = [member, *others, *bot_padding]
    guild._members = {m.id: m for m in [member, *others]}
    snapshots = {1: snapshot} if snapshot is not None else None
    multiplier_snapshots = (
        {1: multiplier_snapshot} if multiplier_snapshot is not None else None
    )
    lvl = _FakeLeveling(
        configs={1: _cfg(rate=rate)},
        snapshots=snapshots,
        multiplier_snapshots=multiplier_snapshots,
    )
    bot = _FakeBot(lvl, fake_pool, guilds=[guild])
    fake_pool.fetch_return = [
        {"guild_id": 1, "user_id": 2, "xp": returned_xp}
    ]
    cog = VoiceXP(bot)
    return cog, lvl, member, guild


def _fetch_calls(fake_pool):
    return [c for c in fake_pool.calls if c[0] == "fetch"]


async def test_sweep_credits_a_full_eligible_window_one_batch_write(fake_pool):
    cog, lvl, member, _guild = _wire_sweep(fake_pool, rate=5)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)  # 300s elapsed == a full window

    writes = _fetch_calls(fake_pool)
    assert len(writes) == 1  # exactly ONE round-trip for the whole sweep
    _method, query, args = writes[0]
    assert "INSERT INTO levels" in query and "unnest" in query
    assert args[:3] == ([1], [2], [25])  # 5 minutes x 5 XP
    # L6: the SAME round trip also carries both period keys (scalar, shared
    # by every row in the batch).
    assert "xp_period" in query
    assert args[3].startswith("W")
    assert args[4].startswith("M")
    assert cog._sessions[(1, 2)].last_credit == 1000.0  # marker advanced
    assert lvl.levelup_calls == []  # mid-band total -> no level-up routing
    # The prune-decision hook fired exactly once, for the one credited guild.
    assert len(lvl.prune_calls) == 1
    assert lvl.prune_calls[0][0] == 1


async def test_sweep_partial_minute_carries_the_remainder(fake_pool):
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, rate=5)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=850.0)

    await cog._run_sweep(now=1000.0)  # 150s -> 2 whole minutes, 30s carries

    _method, _query, args = _fetch_calls(fake_pool)[0]
    assert args[:3] == ([1], [2], [10])  # 2 minutes x 5 XP
    assert cog._sessions[(1, 2)].last_credit == 970.0  # advanced 120s, not 150s


async def test_sweep_alone_member_credits_nothing_but_advances_marker(fake_pool):
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, humans=1)  # alone in the channel
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    assert _fetch_calls(fake_pool) == []  # nothing credited -> no write at all
    assert cog._sessions[(1, 2)].last_credit == 1000.0  # marker still advanced


async def test_sweep_self_muted_member_is_ineligible(fake_pool):
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, self_mute=True)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


async def test_sweep_self_deaf_member_is_ineligible(fake_pool):
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, self_deaf=True)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


async def test_sweep_afk_channel_is_ineligible(fake_pool):
    # Put the member's channel id == the guild's AFK channel id.
    cog, _lvl, member, guild = _wire_sweep(fake_pool)
    guild.afk_channel = member.voice.channel  # sitting in the AFK channel
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


async def test_sweep_no_xp_channel_is_ineligible(fake_pool):
    snap = leveling.NoXpSnapshot(channels=frozenset({10}))  # the member's channel
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, snapshot=snap)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


async def test_sweep_no_xp_role_is_ineligible(fake_pool):
    snap = leveling.NoXpSnapshot(roles=frozenset({77}))
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, snapshot=snap, roles=[77, 88])
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


async def test_sweep_no_xp_category_is_ineligible(fake_pool):
    snap = leveling.NoXpSnapshot(channels=frozenset({50}))  # a category id
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, snapshot=snap, category_id=50)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


# ---------------------------------------------------------------------------
# The sweep: XP multipliers (L4) applied to the per-minute rate.
# ---------------------------------------------------------------------------
def test_wire_sweep_default_multiplier_is_trivial(fake_pool):
    """Sanity check on the fixture itself: the default scenario has no
    multiplier configured, so EMPTY_MULTIPLIER_SNAPSHOT applies."""
    _cog, lvl, _m, _g = _wire_sweep(fake_pool)
    assert lvl._multiplier_snapshots == {}


async def test_sweep_global_boost_scales_the_credited_rate(fake_pool):
    snap = leveling.MultiplierSnapshot(global_factor=2.0)
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, rate=5, multiplier_snapshot=snap)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)  # 300s == a full 5-minute window

    _method, _query, args = _fetch_calls(fake_pool)[-1]
    assert args[:3] == ([1], [2], [50])  # 5 minutes x (5 rate x 2.0) == 50


async def test_sweep_channel_boost_applies_to_the_members_channel(fake_pool):
    snap = leveling.MultiplierSnapshot(channels={10: 0.5})
    cog, _lvl, _m, _g = _wire_sweep(
        fake_pool, rate=10, channel_id=10, multiplier_snapshot=snap
    )
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    _method, _query, args = _fetch_calls(fake_pool)[-1]
    assert args[:3] == ([1], [2], [25])  # 5 minutes x (10 rate x 0.5) == 25


async def test_sweep_role_boost_uses_the_highest_matching_role(fake_pool):
    snap = leveling.MultiplierSnapshot(roles={77: 2.0, 88: 3.0})
    cog, _lvl, _m, _g = _wire_sweep(
        fake_pool, rate=5, roles=[77, 88], multiplier_snapshot=snap
    )
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    _method, _query, args = _fetch_calls(fake_pool)[-1]
    assert args[:3] == ([1], [2], [75])  # 5 minutes x (5 rate x 3.0 highest) == 75


async def test_sweep_zero_factor_credits_nothing_but_still_advances_the_marker(
    fake_pool,
):
    snap = leveling.MultiplierSnapshot(global_factor=0.0)
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, rate=5, multiplier_snapshot=snap)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    assert _fetch_calls(fake_pool) == []  # nothing credited -> no write
    assert cog._sessions[(1, 2)].last_credit == 1000.0  # marker still advanced


async def test_sweep_trivial_multiplier_snapshot_leaves_the_rate_unchanged(
    fake_pool,
):
    """The default (no boosts configured) scenario credits exactly the
    configured rate - proving the multiplier hook is a true no-op when
    nothing is configured."""
    cog, _lvl, _m, _g = _wire_sweep(fake_pool, rate=5)  # no multiplier_snapshot
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    _method, _query, args = _fetch_calls(fake_pool)[-1]
    assert args[:3] == ([1], [2], [25])  # unaffected: 5 minutes x 5 rate


# ---------------------------------------------------------------------------
# The sweep: level-up routing (only real threshold crossings await the seam).
# ---------------------------------------------------------------------------
async def test_sweep_routes_a_voice_levelup_through_the_leveling_seam(fake_pool):
    # gain = 5 rate x 5 min = 25; returned total 10000 -> old 9975 (lvl 9) crosses
    # into level 10.
    cog, lvl, member, _g = _wire_sweep(fake_pool, rate=5, returned_xp=10000)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    assert len(lvl.levelup_calls) == 1
    call = lvl.levelup_calls[0]
    assert call["old_xp"] == 9975 and call["new_xp"] == 10000
    assert call["member"] is member
    assert call["channel"] is member.voice.channel  # voice channel's own text chat


async def test_sweep_mid_band_gain_never_awaits_the_seam(fake_pool):
    """A gain that does not cross a threshold costs zero level-up awaits (the
    per-user await is reserved for the level-up handful)."""
    cog, lvl, _m, _g = _wire_sweep(fake_pool, rate=5, returned_xp=11000)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert lvl.levelup_calls == []


# ---------------------------------------------------------------------------
# The sweep: eviction of dead / disabled sessions.
# ---------------------------------------------------------------------------
async def test_sweep_evicts_a_missed_leave(fake_pool):
    """The member's voice is None (they left, but the event was missed): the
    session is evicted and nothing is credited."""
    cog, _lvl, member, _g = _wire_sweep(fake_pool)
    member.voice = None
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert (1, 2) not in cog._sessions
    assert _fetch_calls(fake_pool) == []


async def test_sweep_evicts_when_member_left_the_guild(fake_pool):
    cog, _lvl, _member, guild = _wire_sweep(fake_pool)
    guild._members = {}  # get_member returns None
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert (1, 2) not in cog._sessions
    assert _fetch_calls(fake_pool) == []


async def test_sweep_evicts_when_voice_xp_toggled_off(fake_pool):
    cog, lvl, _m, _g = _wire_sweep(fake_pool)
    lvl._configs[1] = _cfg(voice_on=False)  # admin turned voice XP off
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    await cog._run_sweep(now=1000.0)
    assert (1, 2) not in cog._sessions
    assert _fetch_calls(fake_pool) == []


async def test_sweep_no_sessions_is_a_no_op(fake_pool):
    lvl = _FakeLeveling(configs={1: _cfg()})
    cog = VoiceXP(_FakeBot(lvl, fake_pool))
    await cog._run_sweep(now=1000.0)
    assert _fetch_calls(fake_pool) == []


async def test_sweep_batches_every_credited_member_into_one_write(fake_pool):
    """Two eligible members in the same channel -> a SINGLE db_pool.fetch whose
    three arrays carry both."""
    a = _Member(2, bot=False)
    b = _Member(3, bot=False)
    channel = _Chan(10)
    guild = _Guild(1, afk_channel=_Chan(555))
    a.guild = b.guild = guild
    a.voice = _VS(channel)
    b.voice = _VS(channel)
    channel.members = [a, b]
    guild._members = {2: a, 3: b}
    lvl = _FakeLeveling(configs={1: _cfg(rate=5)})
    bot = _FakeBot(lvl, fake_pool, guilds=[guild])
    fake_pool.fetch_return = [
        {"guild_id": 1, "user_id": 2, "xp": 11000},
        {"guild_id": 1, "user_id": 3, "xp": 11000},
    ]
    cog = VoiceXP(bot)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    cog._sessions[(1, 3)] = _VoiceSession(channel_id=10, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    writes = _fetch_calls(fake_pool)
    assert len(writes) == 1
    _method, _query, args = writes[0]
    assert args[0] == [1, 1]
    assert sorted(args[1]) == [2, 3]
    assert args[2] == [25, 25]
    # L6: two credited SESSIONS but ONE guild -> the prune-decision hook fires
    # once, not once per session (see the `{c[0] for c in credits}` dedup).
    assert lvl.prune_calls == [(1, lvl.prune_calls[0][1])]


# ---------------------------------------------------------------------------
# The sweep: L6 period rollups (xp_period) ride the SAME batched write.
# ---------------------------------------------------------------------------
async def test_sweep_credits_two_guilds_prunes_each_guild_once(fake_pool):
    """Credited members in TWO different guilds -> the prune hook fires once
    PER DISTINCT guild, still inside the SAME single batched write."""
    a = _Member(2, bot=False)
    b = _Member(3, bot=False)
    chan_a = _Chan(10)
    chan_b = _Chan(20)
    guild_a = _Guild(1, afk_channel=_Chan(555))
    guild_b = _Guild(2, afk_channel=_Chan(556))
    a.guild, b.guild = guild_a, guild_b
    a.voice, b.voice = _VS(chan_a), _VS(chan_b)
    chan_a.members = [a, _Member(900, bot=False)]  # a 2nd human so `a` is eligible
    chan_b.members = [b, _Member(901, bot=False)]
    guild_a._members = {2: a, 900: _Member(900)}
    guild_b._members = {3: b, 901: _Member(901)}
    lvl = _FakeLeveling(configs={1: _cfg(rate=5), 2: _cfg(rate=5)})
    bot = _FakeBot(lvl, fake_pool, guilds=[guild_a, guild_b])
    fake_pool.fetch_return = [
        {"guild_id": 1, "user_id": 2, "xp": 11000},
        {"guild_id": 2, "user_id": 3, "xp": 11000},
    ]
    cog = VoiceXP(bot)
    cog._sessions[(1, 2)] = _VoiceSession(channel_id=10, last_credit=700.0)
    cog._sessions[(2, 3)] = _VoiceSession(channel_id=20, last_credit=700.0)

    await cog._run_sweep(now=1000.0)

    assert len(_fetch_calls(fake_pool)) == 1  # still ONE round trip
    assert sorted(gid for gid, _now in lvl.prune_calls) == [1, 2]


# ---------------------------------------------------------------------------
# on_ready seeding (the DECIDED soften-the-restart-gap behaviour).
# ---------------------------------------------------------------------------
def _seed_scene(fake_pool):
    human = _Member(2, bot=False)
    a_bot = _Member(3, bot=True)  # bots never earn voice XP
    channel = _Chan(10, members=[human, a_bot])
    on_guild = _Guild(1, voice_channels=[channel])
    # A second guild WITHOUT voice XP - none of its members are seeded.
    off_channel = _Chan(20, members=[_Member(4, bot=False)])
    off_guild = _Guild(2, voice_channels=[off_channel])
    lvl = _FakeLeveling(configs={1: _cfg(), 2: _cfg(voice_on=False)})
    bot = _FakeBot(lvl, fake_pool, guilds=[on_guild, off_guild])
    return VoiceXP(bot)


def test_seed_sessions_opens_only_eligible_humans(fake_pool):
    cog = _seed_scene(fake_pool)
    cog._seed_sessions()
    assert set(cog._sessions) == {(1, 2)}  # the human in the voice-XP guild only
    assert cog._sessions[(1, 2)].channel_id == 10


async def test_on_ready_seeds_exactly_once(fake_pool):
    cog = _seed_scene(fake_pool)
    await cog.on_ready()
    first = dict(cog._sessions)
    # A reconnect fires on_ready again; the once-flag keeps it from re-seeding
    # (and from clobbering markers of sessions that have since advanced).
    cog._sessions[(1, 2)].last_credit = 12345.0
    await cog.on_ready()
    assert cog._sessions[(1, 2)].last_credit == 12345.0
    assert set(cog._sessions) == set(first)
