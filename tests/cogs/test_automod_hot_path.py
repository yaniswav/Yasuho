"""``AutoMod.on_message`` must not do permission work for a guild with automod off.

``on_message`` runs for EVERY message in EVERY guild, and the overwhelming
majority of guilds never turn a single automod feature on. It used to read
``Member.guild_permissions`` first - a property that folds the permission bits of
every one of the author's roles on each access - and only then look at whether
antilink / antispam / antiinvite were enabled at all. That fold was pure waste on
the busiest listener in the bot.

The order is now: enabled gate first (two in-process cache reads), permission
check second. These tests pin BOTH halves - that the fold is skipped when the
feature set is empty, and that it still happens (and still lets moderators
through) the moment anything is on - because the swap is only safe if the
outcome is unchanged.

AND THE GATE ITSELF NOW COSTS NOTHING. Both of its halves were in-process
caches, but both READERS were coroutines - so an all-off guild still paid two
awaits per message, while the three other ``on_message`` listeners in this bot
(leveling, afk, serverstats) each decide on a synchronous dict first.
``AutoMod._enabled_cached`` reads the same two structures without building a
coroutine; the second half of this file pins that it enters ZERO coroutines on
the warm all-off path and that it returns the SAME verdict as the async reader
on every combination of the three toggles.

No database, no Discord: the settings reads are stubbed (or answered from the
real ``tools.settings`` LRU, seeded in memory) and the author counts its own
``guild_permissions`` accesses.
"""

import sys
import types

import pytest

from cogs.moderation import automod
from tools import settings

GUILD = 42
BLOB_KEY = (settings._GUILD[0], GUILD)


class _Author:
    """Message author that COUNTS how often its permissions are folded."""

    def __init__(self, *, manage_messages=False):
        self.bot = False
        self.id = 7
        self.mention = "<@7>"
        self.roles = []
        self._manage_messages = manage_messages
        self.permission_reads = 0

    @property
    def guild_permissions(self):
        self.permission_reads += 1
        return types.SimpleNamespace(manage_messages=self._manage_messages)


def _message(author, content="hello"):
    return types.SimpleNamespace(
        author=author,
        guild=types.SimpleNamespace(id=GUILD),
        channel=types.SimpleNamespace(id=1, parent_id=None),
        content=content,
    )


@pytest.fixture(autouse=True)
def _clean_settings_lru():
    """The tools.settings LRU is process-global; own it for the whole test.

    ``_enabled_cached`` peeks that LRU, so a blob for :data:`GUILD` left behind
    by another module would decide this file's verdicts. Emptied on the way in
    AND on the way out.
    """
    settings._cache.clear()
    yield
    settings._cache.clear()


def _cog(monkeypatch, *, antilink=False, antispam=False, antiinvite=False):
    """An AutoMod whose settings answer from memory, counting nothing else.

    The JSONB half is stubbed at ``settings.get_guild`` AND seeded into the real
    LRU, so the cog behaves the same whether the hot path peeks the cache or
    falls back to the coroutine - which is what lets the tests below assert
    about cost without changing the verdict they are asserting on.
    """
    cog = automod.AutoMod.__new__(automod.AutoMod)
    cog.bot = types.SimpleNamespace(db_pool=object())
    cog._settings = automod._SettingsCache()
    cog._settings[GUILD] = {"antilink": antilink, "antispam": antispam}
    cog._spam = {}
    settings._cache[BLOB_KEY] = {"antiinvite": antiinvite}

    async def _get_guild(_pool, _guild_id, key, default=None):
        assert key == "antiinvite"
        return antiinvite

    monkeypatch.setattr(automod.settings, "get_guild", _get_guild)
    return cog


# ---------------------------------------------------------------------------
# THE AWAIT COUNTER. Generic, and aimed at constructed input below before it is
# pointed at the cog: `await f()` cannot run f's body without ENTERING f's
# frame, so a path that awaits nothing enters no coroutine frame but its own.
# ---------------------------------------------------------------------------

_CO_COROUTINE = 0x0080


class CoroutineCounter:
    """Records the name of every coroutine frame entered while installed."""

    def __init__(self):
        self.names = []

    def _profile(self, frame, event, _arg):
        if event == "call" and frame.f_code.co_flags & _CO_COROUTINE:
            self.names.append(frame.f_code.co_name)

    def __enter__(self):
        sys.setprofile(self._profile)
        return self

    def __exit__(self, *_exc):
        sys.setprofile(None)
        return False

    @property
    def count(self):
        return len(self.names)


async def _leaf():
    return 1


async def _awaits_two_coroutines():
    await _leaf()
    await _leaf()


async def _awaits_nothing():
    return 1


async def test_the_await_counter_reports_every_coroutine_a_path_enters():
    """NEGATIVE CONTROL: aim the counter at a path that DOES await, twice."""
    with CoroutineCounter() as counted:
        await _awaits_two_coroutines()

    assert counted.names == ["_awaits_two_coroutines", "_leaf", "_leaf"]
    assert counted.count == 3


async def test_the_await_counter_clears_a_path_that_awaits_nothing():
    """...and reports only the entry point when nothing else is awaited."""
    with CoroutineCounter() as counted:
        await _awaits_nothing()

    assert counted.names == ["_awaits_nothing"]


# ---------------------------------------------------------------------------
# The regression: no feature on -> no permission fold.
# ---------------------------------------------------------------------------


async def test_message_in_a_guild_with_automod_off_never_folds_permissions(
    monkeypatch,
):
    cog = _cog(monkeypatch)
    author = _Author()

    await cog.on_message(_message(author))

    assert author.permission_reads == 0


async def test_a_bot_or_dm_message_still_leaves_before_anything(monkeypatch):
    cog = _cog(monkeypatch, antilink=True)

    from_bot = _Author()
    from_bot.bot = True
    await cog.on_message(_message(from_bot))
    assert from_bot.permission_reads == 0

    in_dm = _Author()
    message = _message(in_dm)
    message.guild = None
    await cog.on_message(message)
    assert in_dm.permission_reads == 0


# ---------------------------------------------------------------------------
# ... and the outcome is unchanged once a feature IS on.
# ---------------------------------------------------------------------------


async def test_a_feature_being_on_does_check_permissions(monkeypatch):
    cog = _cog(monkeypatch, antilink=True)
    author = _Author()
    exempt_calls = []

    async def _is_exempt(_message):
        exempt_calls.append(_message)
        return True

    cog._is_exempt = _is_exempt

    await cog.on_message(_message(author))

    assert author.permission_reads == 1
    # The gate passed, so the message went on to the exemption check.
    assert len(exempt_calls) == 1


async def test_a_moderator_is_still_never_auto_moderated(monkeypatch):
    """The bypass moved AFTER the gate; it must still bypass."""
    cog = _cog(monkeypatch, antilink=True)
    author = _Author(manage_messages=True)
    violations = []

    async def _handle_violation(*_args, **_kwargs):
        violations.append(_kwargs)

    async def _is_exempt(_message):  # pragma: no cover - must never be reached
        raise AssertionError("a manage_messages author must return before this")

    cog._handle_violation = _handle_violation
    cog._is_exempt = _is_exempt

    await cog.on_message(_message(author, content="https://example.com"))

    assert author.permission_reads == 1
    assert violations == []


async def test_antiinvite_alone_is_enough_to_open_the_gate(monkeypatch):
    """The invite toggle lives in a DIFFERENT store than antilink/antispam."""
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()

    async def _is_exempt(_message):
        return True

    cog._is_exempt = _is_exempt

    await cog.on_message(_message(author))

    assert author.permission_reads == 1


# ---------------------------------------------------------------------------
# The cost the gate itself used to have: two awaits on every message.
# ---------------------------------------------------------------------------


class _Pool:
    """Records every query, so "no database" is a fact and not a hope."""

    def __init__(self, row=None):
        self.calls = []
        self.row = row

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return None


def _real_cog(*, antilink=False, antispam=False, antiinvite=False, row=True):
    """An AutoMod reading the REAL tools.settings, seeded in memory.

    Nothing is monkeypatched here: the JSONB half goes through the production
    ``settings.get_guild`` against a warm LRU, which is the only way the sync
    peek and the async reader can be compared on the same inputs.
    """
    cog = automod.AutoMod.__new__(automod.AutoMod)
    cog.bot = types.SimpleNamespace(db_pool=_Pool())
    cog._settings = automod._SettingsCache()
    if row:
        cog._settings[GUILD] = {"antilink": antilink, "antispam": antispam}
    settings._cache[BLOB_KEY] = {"antiinvite": antiinvite, "locale": "fr"}
    cog._spam = {}
    return cog


async def test_an_all_off_guild_now_decides_without_entering_one_coroutine(
    monkeypatch,
):
    """THE REGRESSION THIS LOT CLOSES: zero awaits on the all-off hot path.

    Not "one fewer await": ``on_message`` itself is the only coroutine frame the
    message enters, which is the same shape leveling / afk / serverstats have.
    """
    cog = _cog(monkeypatch)
    author = _Author()

    with CoroutineCounter() as counted:
        await cog.on_message(_message(author))

    assert counted.names == ["on_message"]
    assert author.permission_reads == 0


async def test_the_edit_listener_gate_is_free_on_an_all_off_guild(monkeypatch):
    """on_raw_message_edit is the other hot listener behind the same gate."""
    cog = _cog(monkeypatch)
    after = _message(_Author(), content="edited")
    after.edited_timestamp = object()
    payload = types.SimpleNamespace(message=after, cached_message=None)

    with CoroutineCounter() as counted:
        await cog.on_raw_message_edit(payload)

    assert counted.names == ["on_raw_message_edit"]


async def test_a_cold_cache_still_reads_through_and_the_counter_sees_it():
    """NEGATIVE CONTROL for the cost claim, on the real cog.

    The counter has to be able to SEE the reads it says are gone, or "zero
    coroutines entered" only proves the counter is asleep. With the automod row
    cache cold, the same message must enter ``_enabled`` and ``get_settings``
    and hit Postgres exactly once.
    """
    cog = _real_cog(row=False)
    author = _Author()

    with CoroutineCounter() as counted:
        await cog.on_message(_message(author))

    assert "_enabled" in counted.names
    assert "get_settings" in counted.names
    assert counted.count > 1
    assert len(cog.bot.db_pool.calls) == 1  # the automod row, read through


async def test_a_cold_settings_blob_also_falls_back_to_the_async_reader():
    """The OTHER half going cold must fall back too, not answer from half a gate."""
    cog = _real_cog(antiinvite=True)
    settings._cache.clear()  # the JSONB half is now cold; the row half is warm

    assert cog._enabled_cached(GUILD) is None


# ---------------------------------------------------------------------------
# Same verdicts: the sync gate and the async gate cannot disagree.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("antilink", [False, True])
@pytest.mark.parametrize("antispam", [False, True])
@pytest.mark.parametrize("antiinvite", [False, True])
async def test_the_sync_gate_returns_what_the_async_gate_returns(
    antilink, antispam, antiinvite
):
    """All eight combinations, both readers, one assertion."""
    cog = _real_cog(
        antilink=antilink, antispam=antispam, antiinvite=antiinvite
    )

    assert cog._enabled_cached(GUILD) == await cog._enabled(GUILD)
    assert cog._enabled_cached(GUILD) == (antilink, antispam, antiinvite)
    assert cog.bot.db_pool.calls == []  # both readers stayed in memory


async def test_a_guild_with_no_automod_row_still_reads_its_invite_toggle():
    """The negative cache is a VALUE, not a miss: None row plus antiinvite on.

    A seated ``None`` is what ``get_settings`` stores for a guild with no
    ``automod`` row, and antiinvite lives in the other store entirely - so this
    is the combination a sentinel-free "if not row: cold" would get wrong.
    """
    cog = _real_cog(antiinvite=True, row=False)
    cog._settings[GUILD] = None

    assert cog._enabled_cached(GUILD) == (False, False, True)
    assert cog._enabled_cached(GUILD) == await cog._enabled(GUILD)


async def test_an_unseen_guild_is_cold_rather_than_all_off():
    """A miss must return None, never a fabricated (False, False, False).

    Answering "everything is off" from an empty cache would turn a guild's first
    message into a permanent bypass of its own automod config.
    """
    cog = _real_cog(antilink=True, antiinvite=True)

    assert cog._enabled_cached(GUILD + 1) is None


async def test_the_sync_gate_follows_an_invalidation_of_either_half():
    """It reads the authoritative structures, so an eviction is visible at once.

    This is the reason the hot path peeks tools.settings instead of mirroring
    the flag: there is no third copy to forget. Dropping either half must turn
    the gate cold, and re-seating it must be seen immediately.
    """
    cog = _real_cog(antiinvite=True)
    assert cog._enabled_cached(GUILD) == (False, False, True)

    settings.invalidate_guild(GUILD)  # what the dashboard / retention call
    assert cog._enabled_cached(GUILD) is None

    settings._cache[BLOB_KEY] = {"antiinvite": False}
    assert cog._enabled_cached(GUILD) == (False, False, False)

    cog._settings.pop(GUILD, None)
    assert cog._enabled_cached(GUILD) is None
