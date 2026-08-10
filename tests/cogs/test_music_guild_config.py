"""Unit tests for the per-guild music configuration (lot D of dashboard-executors).

Five keys the dashboard writes into the ``guild_settings`` JSONB blob, read by
the bot at command / player-birth frequency through the ``tools.settings`` LRU:
``music_default_volume``, ``music_autoplay``, ``music_voteskip``,
``music_dj_role``, ``music_sponsorblock``.

The lot's acceptance bar - and the bulk of what is pinned here - is that a guild
the dashboard has NEVER touched behaves exactly as it did before this feature
existed: no volume call at player birth, autoplay ON, vote-skip on, SponsorBlock
armed, and a privilege rule that is still exactly "the DJ or Manage Server". Each
key then gets a test at its real read point, plus the untrusted-payload cases
(absent vs explicit false, out-of-bounds volumes clamping on READ, snowflakes
arriving as strings) and the dashboard invalidation that makes a write visible.

Everything runs against the in-memory ``fake_pool`` and small stand-ins for the
player / cog, so no database, no Lavalink node and no Discord is involved.
"""

import types

import pytest

from cogs.music import guild_config, music, sponsorblock, voteskip
from cogs.system import dashboard_music_actions, dashboard_sync
from tools import settings, snowflake

GUILD_ID = 4242
MEMBER_ID = 77


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    settings._cache.clear()
    yield
    settings._cache.clear()


# ---------------------------------------------------------------------------
# Pure coercion: coerce_bool (the absent-vs-false distinction)
# ---------------------------------------------------------------------------


def test_coerce_bool_absent_returns_the_default():
    # Absent (never written) is NOT false: it resolves to the bot's own default.
    assert guild_config.coerce_bool(None, True) is True
    assert guild_config.coerce_bool(None, False) is False


def test_coerce_bool_explicit_false_is_false_even_when_the_default_is_true():
    # THE distinction the whole lot rests on: a guild that turned something off
    # must not be served the default just because the default is True.
    assert guild_config.coerce_bool(False, True) is False


def test_coerce_bool_explicit_true_is_true():
    assert guild_config.coerce_bool(True, False) is True


def test_coerce_bool_accepts_json_string_spellings():
    for raw in ("true", "TRUE", " True ", "1", "yes", "on"):
        assert guild_config.coerce_bool(raw, False) is True
    for raw in ("false", "FALSE", " false ", "0", "no", "off"):
        assert guild_config.coerce_bool(raw, True) is False


def test_coerce_bool_accepts_numbers():
    assert guild_config.coerce_bool(1, False) is True
    assert guild_config.coerce_bool(0, True) is False


def test_coerce_bool_junk_falls_back_to_the_default():
    # An unusable payload must never flip behaviour; it reads as unconfigured.
    for raw in ("maybe", [], {}, object()):
        assert guild_config.coerce_bool(raw, True) is True
        assert guild_config.coerce_bool(raw, False) is False


def test_coerce_bool_rejects_non_finite_numbers_like_coerce_volume():
    # NaN is truthy in Python, so a bare bool() would read it as an explicit ON.
    # Both coercers must return the same verdict on the same garbage payload.
    for raw in (float("nan"), float("inf"), float("-inf")):
        assert guild_config.coerce_bool(raw, True) is True
        assert guild_config.coerce_bool(raw, False) is False
        assert guild_config.coerce_bool(raw, None) is None
        assert guild_config.coerce_volume(raw) is None


def test_coerce_bool_tri_state_with_a_none_default():
    # The autoplay reader needs absent / on / off to stay three distinct answers.
    assert guild_config.coerce_bool(None, None) is None
    assert guild_config.coerce_bool(False, None) is False
    assert guild_config.coerce_bool(True, None) is True


# ---------------------------------------------------------------------------
# Pure coercion: coerce_volume (CLAMPS, never rejects, an out-of-bounds number)
# ---------------------------------------------------------------------------


def test_coerce_volume_passes_an_in_range_int():
    assert guild_config.coerce_volume(60) == 60


def test_coerce_volume_keeps_both_bounds():
    assert guild_config.coerce_volume(0) == 0
    assert guild_config.coerce_volume(200) == 200


def test_coerce_volume_clamps_above_the_maximum():
    # Payloads are untrusted even from our own dashboard: a level the /volume
    # command would refuse comes back as the nearest legal one, not as 1000.
    assert guild_config.coerce_volume(1000) == guild_config.MAX_VOLUME


def test_coerce_volume_clamps_below_the_minimum():
    assert guild_config.coerce_volume(-30) == guild_config.MIN_VOLUME


def test_coerce_volume_accepts_a_numeric_string():
    assert guild_config.coerce_volume("75") == 75
    assert guild_config.coerce_volume(" 75 ") == 75


def test_coerce_volume_accepts_a_float_and_truncates():
    assert guild_config.coerce_volume(80.9) == 80


def test_coerce_volume_rejects_bools():
    # bool is an int subclass in Python; True is not a volume.
    assert guild_config.coerce_volume(True) is None
    assert guild_config.coerce_volume(False) is None


def test_coerce_volume_rejects_junk_and_non_finite_numbers():
    for raw in (None, "loud", "", [], {}, object(), float("nan"), float("inf")):
        assert guild_config.coerce_volume(raw) is None


def test_coerce_volume_bounds_match_the_live_dashboard_executor():
    # Two independent surfaces write a volume (the per-guild default here, the
    # live remote control in dashboard_music_actions) and both are held to the
    # /volume command's own Range. Pinned so they can never drift apart.
    assert guild_config.MIN_VOLUME == dashboard_music_actions.MIN_VOLUME
    assert guild_config.MAX_VOLUME == dashboard_music_actions.MAX_VOLUME


# ---------------------------------------------------------------------------
# Pure coercion: coerce_role_id / member_has_role
# ---------------------------------------------------------------------------


def test_coerce_role_id_accepts_an_int_and_a_string_snowflake():
    assert guild_config.coerce_role_id(123456789012345678) == 123456789012345678
    # JS serialises snowflakes as strings; that must be the normal case, not a bug.
    assert guild_config.coerce_role_id("123456789012345678") == 123456789012345678


def test_coerce_role_id_rejects_non_snowflakes():
    for raw in (None, 0, -5, True, False, "", "abc", [], {}):
        assert guild_config.coerce_role_id(raw) is None


def test_coerce_role_id_is_the_shared_snowflake_reader():
    """One reader for ids out of a JSONB blob, not a private copy per module.

    ``coerce_role_id`` had grown its own int/str coercion beside
    ``tools.snowflake.coerce_id``, which exists for exactly this job and is what
    every other blob reader in the bot uses. It now delegates, so the two can
    never drift into disagreeing about what a stored id is.
    """
    for raw in (
        None,
        True,
        False,
        0,
        -5,
        1,
        123456789012345678,
        "123456789012345678",
        "  123456789012345678  ",
        "",
        "abc",
        "12.5",
        1.0,
        [],
        {},
        # The shapes a bare int() used to swallow and turn into a confident,
        # wrong id: a signed literal, a PEP 515 underscore separator, and
        # non-ASCII digits. The shared reader says "no role" to all three, which
        # is the only honest answer - the dashboard writes none of them.
        "+12",
        "1_2",
        "\u0661\u0662",  # Arabic-Indic digits: int() reads them, coerce_id does not
    ):
        assert guild_config.coerce_role_id(raw) == snowflake.coerce_id(raw)


def test_member_has_role_uses_get_role_when_available():
    member = types.SimpleNamespace(get_role=lambda rid: object() if rid == 9 else None)
    assert guild_config.member_has_role(member, 9) is True
    assert guild_config.member_has_role(member, 8) is False


def test_member_has_role_falls_back_to_scanning_roles():
    member = types.SimpleNamespace(roles=[types.SimpleNamespace(id=9)])
    assert guild_config.member_has_role(member, 9) is True
    assert guild_config.member_has_role(member, 8) is False


def test_member_has_role_is_false_without_a_configured_role():
    # No DJ role configured -> nobody holds it, which is what keeps an untouched
    # guild's privilege rule exactly "the DJ or Manage Server".
    member = types.SimpleNamespace(roles=[types.SimpleNamespace(id=9)])
    assert guild_config.member_has_role(member, None) is False
    assert guild_config.member_has_role(None, 9) is False


def test_member_has_role_survives_odd_member_shapes():
    assert guild_config.member_has_role(object(), 9) is False
    assert guild_config.member_has_role(types.SimpleNamespace(roles=None), 9) is False


# ---------------------------------------------------------------------------
# Readers: an untouched guild resolves to the bot defaults, with ONE row read
# ---------------------------------------------------------------------------


async def test_untouched_guild_resolves_to_the_bot_defaults(fake_pool):
    fake_pool.fetchval_return = None
    assert await guild_config.default_volume(fake_pool, GUILD_ID) is None
    assert await guild_config.autoplay_default(fake_pool, GUILD_ID) is None
    assert await guild_config.voteskip_enabled(fake_pool, GUILD_ID) is True
    assert await guild_config.sponsorblock_enabled(fake_pool, GUILD_ID) is True
    assert await guild_config.dj_role_id(fake_pool, GUILD_ID) is None


async def test_all_five_keys_share_one_row_read(fake_pool):
    # The five keys ride ONE guild_settings blob behind the tools.settings LRU, so
    # reading all of them costs a single fetch - the scale story of this lot.
    fake_pool.fetchval_return = None
    await guild_config.default_volume(fake_pool, GUILD_ID)
    await guild_config.autoplay_default(fake_pool, GUILD_ID)
    await guild_config.voteskip_enabled(fake_pool, GUILD_ID)
    await guild_config.sponsorblock_enabled(fake_pool, GUILD_ID)
    await guild_config.dj_role_id(fake_pool, GUILD_ID)
    assert sum(1 for call in fake_pool.calls if call[0] == "fetchval") == 1


async def test_readers_return_configured_values(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DEFAULT_VOLUME, 55)
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_AUTOPLAY, False)
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_VOTESKIP, False)
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_SPONSORBLOCK, False)
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DJ_ROLE, "999")

    assert await guild_config.default_volume(fake_pool, GUILD_ID) == 55
    assert await guild_config.autoplay_default(fake_pool, GUILD_ID) is False
    assert await guild_config.voteskip_enabled(fake_pool, GUILD_ID) is False
    assert await guild_config.sponsorblock_enabled(fake_pool, GUILD_ID) is False
    assert await guild_config.dj_role_id(fake_pool, GUILD_ID) == 999


async def test_reader_clamps_an_out_of_bounds_stored_volume(fake_pool):
    # Bounds are enforced on READ, not only on write: a row that already holds an
    # illegal level (a dashboard bug, a hand-edited row) can never reach Lavalink.
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DEFAULT_VOLUME, 9000)
    assert await guild_config.default_volume(fake_pool, GUILD_ID) == 200


async def test_readers_degrade_to_defaults_without_a_pool_or_guild():
    # No pool / no guild id (a stand-in cog, a bot whose pool is not up yet) reads
    # as unconfigured rather than raising: configuration cannot break music.
    assert await guild_config.voteskip_enabled(None, GUILD_ID) is True
    assert await guild_config.sponsorblock_enabled(None, GUILD_ID) is True
    assert await guild_config.dj_role_id(None, GUILD_ID) is None
    assert await guild_config.default_volume(object(), None) is None


async def test_readers_degrade_to_defaults_when_the_settings_read_raises():
    class _BoomPool:
        async def fetchval(self, *args):
            raise RuntimeError("db down")

    pool = _BoomPool()
    assert await guild_config.voteskip_enabled(pool, GUILD_ID) is True
    assert await guild_config.sponsorblock_enabled(pool, GUILD_ID) is True
    assert await guild_config.autoplay_default(pool, GUILD_ID) is None
    assert await guild_config.default_volume(pool, GUILD_ID) is None
    assert await guild_config.dj_role_id(pool, GUILD_ID) is None


# ---------------------------------------------------------------------------
# resolve_session_autoplay: the seed precedence (personal > guild > ON)
# ---------------------------------------------------------------------------


def test_autoplay_precedence_personal_preference_wins_over_the_guild_default():
    assert music.resolve_session_autoplay(True, False) is True
    assert music.resolve_session_autoplay(False, True) is False


def test_autoplay_precedence_guild_default_fills_in_for_an_unset_member():
    assert music.resolve_session_autoplay(None, False) is False
    assert music.resolve_session_autoplay(None, True) is True


def test_autoplay_precedence_falls_back_to_on_when_nothing_is_configured():
    # The untouched-guild case: same answer the one-argument version always gave.
    assert music.resolve_session_autoplay(None) is True
    assert music.resolve_session_autoplay(None, None) is True


# ---------------------------------------------------------------------------
# Player birth (_init_session): volume, autoplay seed and SponsorBlock
# ---------------------------------------------------------------------------


class _BirthPlayer:
    """Minimal player stand-in for the birth seam: records every volume call."""

    def __init__(self, guild_id=GUILD_ID):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.autoplay = None
        self.volumes = []

    async def set_volume(self, value):
        self.volumes.append(value)


class _BirthCog:
    """A Music stand-in wired with the REAL player-birth methods."""

    def __init__(self, pool):
        self.bot = types.SimpleNamespace(db_pool=pool)

    _settings_pool = music.Music._settings_pool
    _init_autoplay = music.Music._init_autoplay
    _apply_default_volume = music.Music._apply_default_volume
    _init_session = music.Music._init_session


@pytest.fixture
def sponsorblock_calls(monkeypatch):
    """Record every ``sponsorblock.schedule_apply`` the birth seam fires."""
    calls = []
    monkeypatch.setattr(sponsorblock, "schedule_apply", lambda player: calls.append(player))
    return calls


def _member(member_id=MEMBER_ID, **extra):
    return types.SimpleNamespace(id=member_id, **extra)


async def test_untouched_guild_birth_changes_nothing(fake_pool, sponsorblock_calls):
    # THE acceptance bar: a guild the dashboard never wrote gets no volume call at
    # all (sonolink's own default stands), autoplay ON, SponsorBlock armed.
    fake_pool.fetchval_return = None
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert player.volumes == []
    assert music._autoplay_on(player) is True
    assert sponsorblock_calls == [player]


async def test_birth_applies_the_configured_default_volume(
    fake_pool, sponsorblock_calls
):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DEFAULT_VOLUME, 35)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert player.volumes == [35]


async def test_birth_applies_a_configured_volume_of_zero(
    fake_pool, sponsorblock_calls
):
    # 0 is a legitimate level (a muted default), NOT "unconfigured".
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DEFAULT_VOLUME, 0)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert player.volumes == [0]


async def test_birth_clamps_an_out_of_bounds_stored_volume(
    fake_pool, sponsorblock_calls
):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DEFAULT_VOLUME, 5000)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert player.volumes == [guild_config.MAX_VOLUME]


async def test_birth_survives_a_failing_volume_call(fake_pool, sponsorblock_calls):
    # The player was connected microseconds ago; a volume PATCH can lose the race
    # with its node-side registration. That costs a default, never the session.
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DEFAULT_VOLUME, 35)

    class _AngryPlayer(_BirthPlayer):
        async def set_volume(self, value):
            raise RuntimeError("player not on the node yet")

    player = _AngryPlayer()
    await _BirthCog(fake_pool)._init_session(player, _member())

    # Birth completed: autoplay armed and SponsorBlock still scheduled.
    assert music._autoplay_on(player) is True
    assert sponsorblock_calls == [player]


async def test_birth_seeds_autoplay_off_from_the_guild_default(
    fake_pool, sponsorblock_calls
):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_AUTOPLAY, False)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert music._autoplay_on(player) is False


async def test_birth_personal_preference_beats_the_guild_default(
    fake_pool, sponsorblock_calls
):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_AUTOPLAY, False)
    await settings.set_user(fake_pool, MEMBER_ID, music.AUTOPLAY_PREF_KEY, True)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert music._autoplay_on(player) is True


async def test_birth_personal_opt_out_still_wins_when_the_guild_says_on(
    fake_pool, sponsorblock_calls
):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_AUTOPLAY, True)
    await settings.set_user(fake_pool, MEMBER_ID, music.AUTOPLAY_PREF_KEY, False)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    assert music._autoplay_on(player) is False


async def test_birth_skips_sponsorblock_when_the_guild_turned_it_off(
    fake_pool, sponsorblock_calls
):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_SPONSORBLOCK, False)
    player = _BirthPlayer()

    await _BirthCog(fake_pool)._init_session(player, _member())

    # No categories PUT is even scheduled: the plugin never acts on this player.
    assert sponsorblock_calls == []


# ---------------------------------------------------------------------------
# The DJ role: one privilege rule, three gates
# ---------------------------------------------------------------------------


class _GateCog:
    """A Music stand-in wired with the REAL privilege methods."""

    def __init__(self, pool, *, manager=False):
        self.bot = types.SimpleNamespace(db_pool=pool)
        self._has_manage_guild = lambda actor: manager

    _settings_pool = music.Music._settings_pool
    _is_music_manager = music.Music._is_music_manager
    _privileged = music.Music._privileged
    _can_control = music.Music._can_control
    _skip_exempt = music.Music._skip_exempt


def _gate_player(dj_id=None, guild_id=GUILD_ID):
    dj = None if dj_id is None else types.SimpleNamespace(id=dj_id, mention="<@dj>")
    return types.SimpleNamespace(dj=dj, guild=types.SimpleNamespace(id=guild_id))


def _role_member(member_id, *role_ids):
    return types.SimpleNamespace(
        id=member_id, roles=[types.SimpleNamespace(id=r) for r in role_ids]
    )


async def test_manager_check_is_plain_manage_guild_without_a_dj_role(fake_pool):
    fake_pool.fetchval_return = None
    cog = _GateCog(fake_pool, manager=False)
    player = _gate_player(dj_id=5)

    assert await cog._is_music_manager(player, _role_member(9, 111)) is False
    assert await _GateCog(fake_pool, manager=True)._is_music_manager(
        player, _role_member(9, 111)
    ) is True


async def test_manage_guild_short_circuits_before_any_settings_read(fake_pool):
    # The common privileged path must not even touch the settings store.
    fake_pool.fetchval_return = None
    cog = _GateCog(fake_pool, manager=True)

    assert await cog._is_music_manager(_gate_player(dj_id=5), _role_member(9)) is True
    assert fake_pool.calls == []


async def test_an_already_decided_gate_reads_no_settings_at_all(fake_pool):
    # The two commonest gated paths: a session with no DJ (every restored one)
    # opens the control lock outright, and the session DJ is exempt everywhere.
    # Neither answer can depend on the DJ role, so neither may pay a settings
    # read - the guild blob is cold here, so a read would be a real round trip
    # inside a button callback.
    # A COLD blob the pool would have to serve: nothing is cached, so any read
    # this gate performs shows up as a fetchval in fake_pool.calls.
    fake_pool.fetchval_return = '{"music_dj_role": 111}'
    cog = _GateCog(fake_pool, manager=False)

    assert await cog._can_control(_gate_player(dj_id=None), _role_member(9)) is True
    assert await cog._can_control(_gate_player(dj_id=9), _role_member(9)) is True
    assert await cog._skip_exempt(_gate_player(dj_id=9), _role_member(9)) is True
    assert fake_pool.calls == []

    # ... and the read DOES happen the moment it can change the answer.
    assert await cog._can_control(_gate_player(dj_id=5), _role_member(9, 111)) is True
    assert sum(1 for call in fake_pool.calls if call[0] == "fetchval") == 1


async def test_a_no_dj_session_still_consults_the_dj_role_for_the_effects_quota(
    fake_pool,
):
    # Counter-test to the short-circuit above: "no DJ" opens the CONTROL lock but
    # decides nothing about the effects/vote exemption, which still has to ask
    # who is a music manager. A short-circuit that returned True for a None DJ
    # everywhere would hand the quota exemption to the whole room.
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DJ_ROLE, 111)
    cog = _GateCog(fake_pool, manager=False)
    player = _gate_player(dj_id=None)

    assert await cog._skip_exempt(player, _role_member(9, 111)) is True
    assert await cog._skip_exempt(player, _role_member(9, 222)) is False


async def test_dj_role_holder_counts_as_a_music_manager(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DJ_ROLE, 111)
    cog = _GateCog(fake_pool, manager=False)
    player = _gate_player(dj_id=5)

    assert await cog._is_music_manager(player, _role_member(9, 111)) is True
    assert await cog._is_music_manager(player, _role_member(9, 222)) is False


async def test_dj_role_opens_the_playback_control_lock(fake_pool):
    # A listener holding the DJ role may drive a session someone else started.
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DJ_ROLE, 111)
    cog = _GateCog(fake_pool, manager=False)
    player = _gate_player(dj_id=5)

    assert await cog._can_control(player, _role_member(9, 111)) is True
    assert await cog._can_control(player, _role_member(9, 222)) is False


async def test_dj_role_exempts_from_the_skip_vote(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_DJ_ROLE, 111)
    cog = _GateCog(fake_pool, manager=False)
    player = _gate_player(dj_id=5)

    assert await cog._skip_exempt(player, _role_member(9, 111)) is True
    assert await cog._skip_exempt(player, _role_member(9, 222)) is False


async def test_dj_role_accepts_a_string_snowflake_from_the_dashboard(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(
        fake_pool, GUILD_ID, guild_config.KEY_DJ_ROLE, "123456789012345678"
    )
    cog = _GateCog(fake_pool, manager=False)

    assert (
        await cog._is_music_manager(
            _gate_player(dj_id=5), _role_member(9, 123456789012345678)
        )
        is True
    )


# ---------------------------------------------------------------------------
# The vote-skip toggle, read at the ONE skip routing point
# ---------------------------------------------------------------------------


class _SkipVotes:
    """Records every vote the routing point opens."""

    def __init__(self):
        self.opened = []

    async def open(self, cog, player, actor, channel):
        self.opened.append((player, actor))
        return voteskip.VOTE_OPENED


class _SkipCog(_GateCog):
    def __init__(self, pool, *, manager=False):
        super().__init__(pool, manager=manager)
        self.skip_votes = _SkipVotes()

    _request_skip = music.Music._request_skip


def _skip_player(humans, *, dj_id=5, guild_id=GUILD_ID):
    members = [types.SimpleNamespace(id=200 + i, bot=False) for i in range(humans)]
    return types.SimpleNamespace(
        dj=types.SimpleNamespace(id=dj_id, mention="<@dj>"),
        guild=types.SimpleNamespace(id=guild_id),
        current=object(),
        channel=types.SimpleNamespace(members=members),
    )


async def test_untouched_guild_still_opens_a_vote_for_a_plain_listener(fake_pool):
    fake_pool.fetchval_return = None
    cog = _SkipCog(fake_pool, manager=False)
    player = _skip_player(4)

    decision = await cog._request_skip(player, _role_member(9), None)

    assert decision == voteskip.VOTE_OPENED
    assert len(cog.skip_votes.opened) == 1


async def test_voteskip_off_skips_directly_and_posts_no_vote(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_VOTESKIP, False)
    cog = _SkipCog(fake_pool, manager=False)
    player = _skip_player(4)

    decision = await cog._request_skip(player, _role_member(9), None)

    assert decision == voteskip.SKIP_INSTANT
    assert cog.skip_votes.opened == []


async def test_voteskip_explicitly_on_is_not_read_as_absent(fake_pool):
    # A guild that wrote `true` must behave like the default, not like `false`.
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_VOTESKIP, True)
    cog = _SkipCog(fake_pool, manager=False)
    player = _skip_player(4)

    assert await cog._request_skip(player, _role_member(9), None) == voteskip.VOTE_OPENED


async def test_voteskip_off_still_lets_a_privileged_actor_skip(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, GUILD_ID, guild_config.KEY_VOTESKIP, False)
    cog = _SkipCog(fake_pool, manager=True)
    player = _skip_player(4)

    assert await cog._request_skip(player, _role_member(9), None) == voteskip.SKIP_INSTANT


async def test_voteskip_toggle_never_resurrects_a_dead_player(fake_pool):
    # Nothing playing short-circuits BEFORE the settings read, exactly as before.
    fake_pool.fetchval_return = None
    cog = _SkipCog(fake_pool, manager=False)
    player = _skip_player(4)
    player.current = None

    assert await cog._request_skip(player, _role_member(9), None) == voteskip.SKIP_INSTANT
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# Dashboard wiring: the music_config kind evicts the guild's settings blob
# ---------------------------------------------------------------------------


class _KindBot:
    def __init__(self, pool):
        self.db_pool = pool

    def get_cog(self, name):
        return None


async def test_music_config_is_a_registered_guild_kind():
    assert "music_config" in dashboard_sync.VALID_KINDS
    assert "music_config" in dashboard_sync._INVALIDATORS
    # Guild-scoped: its payload carries guildId, not userId.
    assert "music_config" not in dashboard_sync.USER_KINDS


async def test_music_config_dispatch_evicts_the_guilds_settings_blob(fake_pool):
    key = (settings._GUILD[0], GUILD_ID)
    settings._cache[key] = {guild_config.KEY_VOTESKIP: False}
    assert key in settings._cache

    handled = await dashboard_sync.dispatch(
        _KindBot(fake_pool),
        '{"kind": "music_config", "guildId": "%d"}' % GUILD_ID,
    )

    assert handled == "music_config"
    assert key not in settings._cache


async def test_music_config_dispatch_leaves_other_guilds_alone(fake_pool):
    mine = (settings._GUILD[0], GUILD_ID)
    other = (settings._GUILD[0], GUILD_ID + 1)
    settings._cache[mine] = {guild_config.KEY_VOTESKIP: False}
    settings._cache[other] = {guild_config.KEY_VOTESKIP: False}

    await dashboard_sync.dispatch(
        _KindBot(fake_pool),
        '{"kind": "music_config", "guildId": "%d"}' % GUILD_ID,
    )

    assert mine not in settings._cache
    assert other in settings._cache


async def test_a_dashboard_write_takes_effect_on_the_next_read(fake_pool):
    # End to end through the LRU: read the default, another process writes the
    # row, the notification evicts, the next read sees the new value.
    fake_pool.fetchval_return = None
    assert await guild_config.voteskip_enabled(fake_pool, GUILD_ID) is True

    # The dashboard's write lands in the DB, invisible to the warm LRU...
    fake_pool.fetchval_return = {guild_config.KEY_VOTESKIP: False}
    assert await guild_config.voteskip_enabled(fake_pool, GUILD_ID) is True

    # ...until its NOTIFY evicts the blob.
    await dashboard_sync.dispatch(
        _KindBot(fake_pool),
        '{"kind": "music_config", "guildId": "%d"}' % GUILD_ID,
    )
    assert await guild_config.voteskip_enabled(fake_pool, GUILD_ID) is False
