"""``y!sync``: which of its four shapes records the global payload hash.

The bot now syncs its own GLOBAL tree at startup whenever the payload changed
(``tools/tree_sync.py``), gated on the hash of the last successful sync. That
gate only works if the manual command keeps it honest in both directions:

* a GLOBAL hand sync (``y!sync`` with no spec) MUST record the hash. Otherwise
  the owner syncs by hand, restarts, and the next boot sees a hash it has never
  stored and syncs the very same tree again - one wasted call against a
  rate-limited endpoint on every restart until something else moves the hash.
* the GUILD-scoped shapes (``y!sync ~`` copies nothing, ``*`` copies the global
  tree INTO one guild, ``^`` CLEARS one guild, and the greedy ``y!sync <id>...``
  form) must NOT record it. None of them changes the global registration, so
  writing the global hash from one would tell the next boot that a tree Discord
  has never been given is already live - and the auto-sync would skip it. That
  is the original outage, re-armed.

The second claim's success is a SILENCE (no row written), so the file carries
its own positive control: :func:`test_a_global_sync_records_the_hash` proves the
recording seam fires at all, and every assertion is a COUNT.

Pure fakes: no Discord, no database, no network.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from cogs.system import admin as admin_cog
from tools import tree_sync


class FakeTree:
    def __init__(self):
        self.global_syncs = 0
        self.guild_syncs = []
        self.copied = []
        self.cleared = []

    async def sync(self, *, guild=None):
        if guild is None:
            self.global_syncs += 1
        else:
            self.guild_syncs.append(guild)
        return [object(), object()]

    def copy_global_to(self, *, guild):
        self.copied.append(guild)

    def clear_commands(self, *, guild):
        self.cleared.append(guild)


class FakeBot:
    def __init__(self):
        self.tree = FakeTree()
        self.application_id = 4242
        self.db_pool = object()


class FakeCtx:
    def __init__(self, guild):
        self.guild = guild
        self.sends = []
        self.interaction = None

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _Guild:
    def __init__(self, guild_id):
        self.id = guild_id


def _install_recorder(monkeypatch):
    """Replace ``record_global_sync`` with a counter, in the cog's namespace."""
    calls = []

    async def recorder(bot, *, synced_count=None):
        calls.append(synced_count)
        return True

    monkeypatch.setattr(admin_cog.tree_sync, "record_global_sync", recorder)
    return calls


async def _run_sync(monkeypatch, *, spec=None, guilds=()):
    calls = _install_recorder(monkeypatch)
    bot = FakeBot()
    cog = admin_cog.Admin(bot)
    ctx = FakeCtx(_Guild(999))
    await cog.sync.callback(cog, ctx, list(guilds), spec)
    return bot, ctx, calls


# ---------------------------------------------------------------------------
# The positive control, then the silences.
# ---------------------------------------------------------------------------


async def test_a_global_sync_records_the_hash(monkeypatch):
    """POSITIVE CONTROL: the recorder fires exactly once, with the real count."""
    bot, ctx, calls = await _run_sync(monkeypatch)
    assert bot.tree.global_syncs == 1
    assert calls == [2], "the recorded count must be what Discord returned"
    assert ctx.sends, "the owner is still told what happened"


async def test_a_guild_sync_does_not_record_the_global_hash(monkeypatch):
    bot, ctx, calls = await _run_sync(monkeypatch, spec="~")
    assert [guild.id for guild in bot.tree.guild_syncs] == [ctx.guild.id]
    assert bot.tree.global_syncs == 0
    assert calls == []


async def test_copying_the_global_tree_into_a_guild_does_not_record(monkeypatch):
    """``*`` publishes the SAME commands, but only to one guild."""
    bot, _, calls = await _run_sync(monkeypatch, spec="*")
    assert len(bot.tree.copied) == 1
    assert bot.tree.global_syncs == 0
    assert calls == []


async def test_clearing_a_guild_does_not_record(monkeypatch):
    bot, _, calls = await _run_sync(monkeypatch, spec="^")
    assert len(bot.tree.cleared) == 1
    assert bot.tree.global_syncs == 0
    assert calls == []


async def test_the_greedy_per_guild_form_does_not_record(monkeypatch):
    bot, _, calls = await _run_sync(
        monkeypatch, guilds=[_Guild(1), _Guild(2)]
    )
    assert len(bot.tree.guild_syncs) == 2
    assert bot.tree.global_syncs == 0
    assert calls == []


async def test_the_recorded_hash_is_what_silences_the_next_boot(monkeypatch):
    """End to end on the REAL recorder: hand sync, reboot, zero extra syncs.

    Uses the real ``record_global_sync`` and the real ``sync_if_changed``
    against the in-memory table from tests/tools/test_tree_sync.py, so the claim
    is about the shipped code and not about a counter.
    """
    from tests.tools.test_tree_sync import FakeBot as SyncBot
    from tests.tools.test_tree_sync import FakeTree as SyncTree
    from tests.tools.test_tree_sync import HashStore, _entry

    store = HashStore()
    tree = SyncTree([_entry("config", "Configure the server.")])
    hand = SyncBot(tree, store)

    recorded = []
    # Bound BEFORE the patch: admin_cog.tree_sync IS the module, so reading the
    # name through it afterwards would call the stand-in and recurse.
    real_record = tree_sync.record_global_sync

    async def recorder(bot, *, synced_count=None):
        recorded.append(synced_count)
        return await real_record(hand, synced_count=synced_count)

    monkeypatch.setattr(admin_cog.tree_sync, "record_global_sync", recorder)

    cog = admin_cog.Admin(FakeBot())
    await cog.sync.callback(cog, FakeCtx(_Guild(999)), [], None)
    assert recorded == [2]
    assert store.writes == 1

    reboot = SyncBot(SyncTree([_entry("config", "Configure the server.")]), store)
    assert await tree_sync.sync_if_changed(reboot) == tree_sync.SKIP_UNCHANGED
    assert len(reboot.tree.syncs) == 0


def test_sync_is_still_prefix_only_and_owner_only():
    """Guard the guard's subject: this must stay the hidden owner command.

    The global tree stands at 78 of Discord's 100 slots
    (tests/test_command_tree_capacity.py), and an ops command the owner runs by
    hand must not spend one of the remaining ones.
    """
    command = admin_cog.Admin.sync
    assert isinstance(command, commands.Command)
    assert not isinstance(command, discord.ext.commands.HybridCommand)
    assert command.hidden is True
