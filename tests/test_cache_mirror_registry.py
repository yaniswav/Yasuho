"""One registry binding the three lists that drop per-guild cache mirrors.

THE DEFECT THIS GUARD EXISTS FOR. The bot mirrors dashboard-written Postgres
state in cog-private attributes, and THREE modules each keep their own
hand-maintained list of which ones to touch, with nothing connecting them:

* ``cogs/system/dashboard_sync.py`` - per-kind invalidation plus ``resync_all``;
* ``tools/retention.py`` - ``invalidate_guild_caches`` (guild purge/departure);
* ``cogs/system/events.py`` - the ``on_guild_join`` refill.

dashboard_sync reaches in duck-typed (``getattr(cog, "_settings", None)`` then
``isinstance(cache, dict)`` / ``getattr(cache, "clear", None)``), so RENAMING a
cog attribute does not raise, does not log, and did not fail a test. It silently
stopped invalidating, and the guild served pre-dashboard config until the next
restart. The bug was invisible from every side. The existing stand-ins made it
worse rather than better: ``tests/cogs/test_dashboard_resync.py`` hand-writes
``FakeLeveling._rank_cards``, so a rename on the REAL cog left every assertion
there green.

WHAT THIS FILE IS. The registry below is the single place where each mirror's
name, its key shape, and what EACH of the three consumers does with it are
written down once. The consumers keep their own code - their per-cache work is
genuinely different (an eager re-read, a negative-cache Record write, a
composite-key filter), and turning that into a table would have been a bigger
change than the bug - so this binds them by BEHAVIOUR instead:

* every attribute named here is resolved on a REAL owner object - the six
  production cog instances, and ``core.Yasuho`` itself for the bot's own eager
  maps - so a rename is a red test naming the attribute, not a silent no-op in
  production. The stand-ins below (``MirrorBot``, and the ``Fake*`` cogs in
  tests/cogs/test_dashboard_resync.py) exist to run the consumers offline, and
  every private name they hand-write is pinned against its real owner, so a
  stand-in can never keep spelling a name production has stopped using;
* every recorded decision is checked by seeding the real cache and running the
  real consumer, so a consumer that quietly stops dropping a cache is red;
* a DELIBERATE omission is a ``KEEP`` (or ``HEALS``) with a written ``why``, and
  it is pinned too: an entry recorded as untouched that a consumer starts
  touching is just as red as one it stops touching.

Nothing here reads source text or docstrings. Every claim is made by building an
input and looking at what the production function did to it - the repo has been
burnt by a guard that asserted ``"rglob" in getsource(f)`` while the code used a
flat glob, because the word was in the prose.

The suite never touches the network, a database, Discord, or Lavalink: it builds
the real cog objects (all six ``__init__`` are pure) against an in-memory pool.
"""

from __future__ import annotations

import asyncio
import logging
import types

import discord
import pytest
from discord.ext import commands

import core

from cogs.community.leveling.leveling import Leveling
from cogs.config.customcommands import CustomCommands
from cogs.config.rooms import TemporaryRooms
from cogs.config.starboard import Starboard
from cogs.moderation.automod import AutoMod
from cogs.moderation.modlog import ModLog
from cogs.system import dashboard_sync, events
from tools import retention, settings
from tools.lru_cache import BoundedLRU

# ---------------------------------------------------------------------------
# The vocabulary the registry is written in.
# ---------------------------------------------------------------------------

# Where the mirror lives. "bot" is the Yasuho object itself; anything else is a
# bot.get_cog() name.
BOT = "bot"

# KEY SHAPES - how a scope id appears in the structure, which is what decides
# how a consumer can drop one scope's entries and how this file seeds/probes.
GUILD_MAP = "guild-keyed mapping"  # {guild_id: value}, dict or BoundedLRU
GUILD_TUPLE_MAP = "guild-first tuple-keyed mapping"  # {(guild_id, ...): value}
GUILD_TUPLE_SET = "guild-first tuple-keyed set"  # {(guild_id, ...)}
USER_SET = "user-keyed set"  # {user_id}

# WHAT A CONSUMER DOES. The first three are observable outcomes of a run; KEEP
# is the written-down omission and demands a reason like every other row.
DROP = "drops this scope's entry"
EMPTY = "empties the whole structure"
RELOAD = "rebuilds it from the database"
KEEP = "deliberately leaves it alone"

# The join refill is a different verb, so it gets its own two values.
REBUILD = "replaces this guild's entry"
HEALS = "left to its own read-through"


class Mirror(types.SimpleNamespace):
    """One mirrored structure and the decision each consumer made about it."""

    @property
    def name(self):
        return "{}.{}".format(self.owner, self.attr)


def _mirror(owner, attr, key, on_purge, on_resync, on_join, why):
    return Mirror(
        owner=owner,
        attr=attr,
        key=key,
        on_purge=on_purge,
        on_resync=on_resync,
        on_join=on_join,
        why=why,
    )


# ---------------------------------------------------------------------------
# THE REGISTRY. One row per structure that mirrors per-guild database state, or
# that names a guild and therefore must not outlive one.
#
# Read the three decision columns as: what a guild purge/departure does
# (tools/retention.invalidate_guild_caches), what a dashboard reconnect does
# (dashboard_sync.resync_all), and what a guild rejoin does
# (events.Events.on_guild_join).
# ---------------------------------------------------------------------------

REGISTRY = (
    # -- the bot's own eagerly-primed maps ---------------------------------
    _mirror(
        BOT,
        "prefixes",
        GUILD_MAP,
        DROP,
        RELOAD,
        REBUILD,
        "get_prefix reads it synchronously with no read-through, so an absent "
        "key is the ANSWER 'no custom prefix' rather than a miss. It can only "
        "ever be reloaded, never emptied, and a rejoin has to refill it by hand "
        "because nothing else would.",
    ),
    _mirror(
        BOT,
        "autoroles",
        GUILD_MAP,
        DROP,
        RELOAD,
        REBUILD,
        "Same family as prefixes: on_member_join reads it synchronously and an "
        "absent key means 'this guild has no autorole'.",
    ),
    _mirror(
        BOT,
        "muteroles",
        GUILD_MAP,
        DROP,
        RELOAD,
        REBUILD,
        "Same family as prefixes: on_member_join and on_guild_channel_create "
        "read it synchronously and an absent key means 'no mute role here'.",
    ),
    _mirror(
        BOT,
        "blacklist",
        USER_SET,
        KEEP,
        RELOAD,
        HEALS,
        "USER-scoped, which is exactly why the guild purge must not touch it: a "
        "server leaving is not a reason to un-blacklist the people in it, and "
        "the ids are not guild ids at all. A reconnect still reloads it with "
        "the other three eager maps.",
    ),
    # -- ModLog -------------------------------------------------------------
    _mirror(
        "ModLog",
        "_channels",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Negative-cached log channel (None means 'looked up, not configured'). "
        "Read-through in get_log_channel, so emptying it is enough and a rejoin "
        "needs no refill.",
    ),
    _mirror(
        "ModLog",
        "_recent_bans",
        GUILD_TUPLE_SET,
        DROP,
        KEEP,
        HEALS,
        "(guild_id, user_id) dedup keys for bans this process just saw, with a "
        "5s self-expiry. Not dashboard state, so a reconnect has no reason to "
        "clear them; but they name a guild, so a purge drops them.",
    ),
    _mirror(
        "ModLog",
        "_suppressed",
        GUILD_TUPLE_SET,
        DROP,
        KEEP,
        HEALS,
        "(guild_id, user_id, kind) markers for bot-initiated actions whose case "
        "embed is already posted, with a 10s self-expiry. Same call as "
        "_recent_bans: guild-named runtime state, not dashboard state.",
    ),
    # -- Starboard ----------------------------------------------------------
    _mirror(
        "Starboard",
        "_config",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Negative-cached (channel_id, threshold). Read-through in get_config.",
    ),
    # -- AutoMod ------------------------------------------------------------
    _mirror(
        "AutoMod",
        "_settings",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Negative cache over the automod TABLE booleans. Read-through in "
        "get_settings; the JSONB half of AutoMod's config rides the "
        "tools.settings LRU, which both consumers evict separately.",
    ),
    _mirror(
        "AutoMod",
        "_spam",
        GUILD_TUPLE_MAP,
        DROP,
        KEEP,
        HEALS,
        "(guild_id, user_id) -> recent message timestamps. A debounce window, "
        "not configuration: a reconnect says nothing about who is spamming, and "
        "clearing it fleet-wide would hand every member a fresh burst budget.",
    ),
    # -- Leveling -----------------------------------------------------------
    _mirror(
        "Leveling",
        "_configs",
        GUILD_MAP,
        DROP,
        RELOAD,
        REBUILD,
        "The on_message hot-path config map. get_config is a plain synchronous "
        "dict read where a missing guild MEANS 'leveling is off', so clearing it "
        "would silently stop XP bot-wide with no read-through to heal it: "
        "reload_configs rebuilds, refresh_guild_config refills one guild.",
    ),
    _mirror(
        "Leveling",
        "_no_xp",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Read-through BoundedLRU of the guild's level_no_xp snapshot "
        "(ensure_no_xp_snapshot reloads on a miss).",
    ),
    _mirror(
        "Leveling",
        "_multipliers",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Read-through BoundedLRU of xp_multipliers plus the level_config event "
        "columns (ensure_multiplier_snapshot reloads on a miss).",
    ),
    _mirror(
        "Leveling",
        "_rank_cards",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Read-through BoundedLRU of the guild's rank-card style. The purge "
        "column is NOT symmetry for its own sake: rank_cards is one of the "
        "tables GUILD_DELETE_QUERIES deletes, so an entry left behind keeps the "
        "accent of a guild whose row was just erased, and a re-invite would "
        "render the purged style until the LRU happened to evict it.",
    ),
    _mirror(
        "Leveling",
        "_period_markers",
        GUILD_MAP,
        DROP,
        KEEP,
        HEALS,
        "Season-rollover bookkeeping (the last (week, month) a grant observed), "
        "never written by the dashboard, and cold-miss-safe by design - a miss "
        "just re-probes. A reconnect therefore has nothing to fix here; a purge "
        "still drops it, because the marker names a guild whose xp_period rows "
        "are gone.",
    ),
    # -- CustomCommands -----------------------------------------------------
    _mirror(
        "CustomCommands",
        "_cache",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "Lazily-loaded {name: response} map; get_custom_commands re-fetches on "
        "the next miss.",
    ),
    _mirror(
        "CustomCommands",
        "_uses",
        GUILD_MAP,
        DROP,
        EMPTY,
        HEALS,
        "The parallel {name: uses} map the panel displays, reloaded with "
        "_cache.",
    ),
    _mirror(
        "CustomCommands",
        "_cd",
        GUILD_TUPLE_MAP,
        DROP,
        KEEP,
        HEALS,
        "(guild_id, name, user_id) -> expiry cooldown CLOCKS, not "
        "configuration. The per-guild paths drop a guild's clocks because that "
        "guild's commands just changed; a reconnect says nothing of the sort, "
        "and wiping every clock would hand every member of every guild one free "
        "re-use of every command.",
    ),
    # -- TemporaryRooms -----------------------------------------------------
    _mirror(
        "TemporaryRooms",
        "_hub_index",
        GUILD_MAP,
        DROP,
        RELOAD,
        REBUILD,
        "A DERIVED {guild_id: {hub_channel_id: hub}} index that "
        "on_voice_state_update reads on every voice event and that nothing ever "
        "re-derives from settings. Dropping it would leave every hub dead until "
        "a restart, so both the reconnect and the rejoin rebuild it.",
    ),
)


# ---------------------------------------------------------------------------
# The duck-typed METHOD seams, which miss just as silently as the attributes:
# dashboard_sync and events.py look every one of these up by name and skip when
# it is not callable.
# ---------------------------------------------------------------------------

SEAMS = (
    (BOT, "load_eager_caches"),
    ("Leveling", "reload_configs"),
    ("Leveling", "refresh_guild_config"),
    ("Leveling", "refresh_no_xp_snapshot"),
    ("Leveling", "refresh_multiplier_snapshot"),
    ("Leveling", "invalidate_rank_card"),
    ("TemporaryRooms", "reload_hub_index"),
    ("TemporaryRooms", "_load_hubs"),
    ("TemporaryRooms", "_index_guild"),
)


# ---------------------------------------------------------------------------
# Structures on a registered owner that are deliberately NOT mirrors. Every one
# needs a reason, because "absent from the registry" and "forgotten" look
# identical otherwise - which is the whole defect this file closes.
# ---------------------------------------------------------------------------

NOT_MIRRORED = {
    ("Leveling", "_season_tasks"): "asyncio.Task handles, not cached state.",
    ("Leveling", "_user_rank_cards"): (
        "USER-keyed 'does this member have a row?' hint. No guild purge can "
        "reach a user row, and no dashboard kind writes user_rank_cards."
    ),
    ("Leveling", "_vote_boosts"): (
        "USER-keyed vote-boost expiries, reloaded by reload_vote_boosts from "
        "the top.gg ledger; not guild data."
    ),
    ("AutoMod", "_scanned"): (
        "BoundedLRU of message ids already scanned, to keep an edit from "
        "re-punishing. Message-keyed and self-bounding."
    ),
    ("Starboard", "_locks"): (
        "message_id -> [asyncio.Lock, waiters], popped when the last waiter "
        "leaves. Not guild-keyed and not state."
    ),
    ("TemporaryRooms", "_locks"): (
        "guild_id -> asyncio.Lock serialising room creation. A lock is not "
        "state: dropping one mid-creation would break the serialisation it "
        "exists for, and an idle Lock costs nothing."
    ),
    ("TemporaryRooms", "_active"): (
        "(guild_id, hub_id) -> live room channel ids. Bookkeeping for rooms "
        "that exist RIGHT NOW, maintained by the room lifecycle rather than by "
        "any config write. Left out of the purge deliberately and knowingly: "
        "the entries of a departed guild are a bounded leak, never a stale "
        "ANSWER, and dropping them is a room-lifecycle change rather than a "
        "cache-invalidation one."
    ),
    ("TemporaryRooms", "_cleanup_tasks"): "asyncio.Task handles.",
    ("TemporaryRooms", "_room_owners"): (
        "channel_id -> owner user id for live temp rooms; dies with the room."
    ),
    ("TemporaryRooms", "_room_views"): (
        "channel_id -> the room's control View; dies with the room."
    ),
}


# ---------------------------------------------------------------------------
# THE DETECTORS. Plain callables over data you construct, so each one can be
# aimed at a case it MUST report and a case it must clear.
# ---------------------------------------------------------------------------


def absent_attributes(entries, resolve):
    """Registry rows whose attribute is missing on the object ``resolve`` returns.

    ``resolve(owner)`` yields the live object that is supposed to carry the
    mirror. A row naming an attribute that object does not have is precisely the
    rename the duck-typed ``getattr`` swallows in production.
    """
    missing = []
    for entry in entries:
        target = resolve(entry.owner)
        if target is None or not hasattr(target, entry.attr):
            missing.append(entry.name)
    return missing


def broken_seams(seams, resolve_class):
    """Seam rows whose method is not callable on the class ``resolve_class`` gives."""
    broken = []
    for owner, method in seams:
        klass = resolve_class(owner)
        if klass is None or not callable(getattr(klass, method, None)):
            broken.append("{}.{}".format(owner, method))
    return broken


def decision_mismatches(entries, column, observed):
    """Rows where the written decision is not what the consumer actually did.

    ``observed`` maps a row name to the outcome a real run produced. A row the
    run never reached is a mismatch too: a decision nobody exercised is a
    decision nobody is checking.
    """
    mismatches = []
    for entry in entries:
        recorded = getattr(entry, column)
        actual = observed.get(entry.name, "not observed")
        if actual != recorded:
            mismatches.append((entry.name, recorded, actual))
    return mismatches


def swept_attributes(instances, baselines=None):
    """Every ``(owner, attr)`` the completeness sweep below looks at.

    ``instances`` maps an owner name to a live object. Only dicts, sets and
    BoundedLRUs are swept: those are the three shapes the three consumers reach
    into, and therefore the shapes that can silently drift. A Cooldowns map or an
    asyncio.Lock cannot be dropped per guild by any of them without an API that
    does not exist, so they are out of this sweep by construction.

    Which NAMES are candidates depends on the owner. A cog's mirrors are private
    by convention, so an owner with no baseline is swept for private names only.
    ``baselines`` maps an owner to the attribute names its FRAMEWORK base already
    carries, and switches that owner to "everything the subclass added, public or
    private" - which is the only rule that works for the bot: its four mirrors
    are PUBLIC (``bot.prefixes``), so the private-name rule would sweep nothing
    there, while a raw sweep of a discord.py Bot would drown in the library's own
    dicts (``all_commands``, ``extra_events``, ``_BotBase__cogs``, ...).

    Split out from the classification below so the counter that proves the sweep
    examined SOMETHING counts what the sweep actually looked at, rather than a
    second hand-written approximation of it.
    """
    baselines = baselines or {}
    swept = []
    for owner, obj in instances.items():
        baseline = baselines.get(owner)
        for attr, value in sorted(vars(obj).items()):
            if attr.startswith("__"):
                continue
            if baseline is None:
                if not attr.startswith("_"):
                    continue
            elif attr in baseline:
                continue
            if not isinstance(value, (dict, set, BoundedLRU)):
                continue
            swept.append((owner, attr))
    return swept


def unclassified_caches(instances, registered, waived, baselines=None):
    """Swept attributes on a registered owner that no decision covers."""
    return [
        "{}.{}".format(owner, attr)
        for owner, attr in swept_attributes(instances, baselines)
        if (owner, attr) not in registered and (owner, attr) not in waived
    ]


# ---------------------------------------------------------------------------
# The in-memory boundary: one pool answering the exact queries the three
# consumers re-run, and a bench that seeds every registered structure.
# ---------------------------------------------------------------------------

# The guild each consumer is aimed at.
TARGET = 7001
# A second guild whose entries must SURVIVE a per-guild drop, so a .clear() can
# never be mistaken for one.
NEIGHBOUR = 7002
# A guild that exists only in the database. It can appear in a structure only if
# the consumer genuinely re-read Postgres, which is what tells RELOAD apart from
# DROP and EMPTY.
FRESH = 7003

TARGET_USER = 8001
NEIGHBOUR_USER = 8002
FRESH_USER = 8003

# A value no production code can produce, so "still holds the seed" is exact.
SEED = object()


class MirrorPool:
    """Answers the cold-start, read-through and rejoin queries. No I/O."""

    def __init__(self):
        self.prefixes = {NEIGHBOUR: "!", FRESH: "?"}
        self.blacklist = {FRESH_USER}
        self.autoroles = {NEIGHBOUR: 11, FRESH: 12}
        self.muteroles = {NEIGHBOUR: 21, FRESH: 22}
        self.guild_settings = {
            TARGET: {"leveling_enabled": True, "autorooms": [{"hub_channel_id": 41}]},
            NEIGHBOUR: {
                "leveling_enabled": True,
                "autorooms": [{"hub_channel_id": 42}],
            },
            FRESH: {"leveling_enabled": True, "autorooms": [{"hub_channel_id": 43}]},
        }

    async def fetch(self, query, *args):
        if "FROM prefixes" in query:
            return list(self.prefixes.items())
        if "FROM blbot" in query:
            return [{"member_id": uid} for uid in sorted(self.blacklist)]
        if "FROM autorole" in query:
            return list(self.autoroles.items())
        if "FROM muterole" in query:
            return list(self.muteroles.items())
        if "FROM level_config" in query:
            return []  # every enabled guild here rides the legacy JSONB flag
        if "FROM guild_settings" in query and "leveling_enabled" in query:
            return [
                {"guild_id": gid}
                for gid, blob in self.guild_settings.items()
                if blob.get("leveling_enabled") is True
            ]
        if "FROM guild_settings" in query:
            return [
                {"guild_id": gid, "settings": blob}
                for gid, blob in self.guild_settings.items()
            ]
        raise AssertionError("unexpected fetch: {!r}".format(query))

    async def fetchrow(self, query, *args):
        if "AS prefix" in query:  # the on_guild_join refill's one round trip
            gid = args[0]
            return {
                "prefix": self.prefixes.get(gid),
                "autorole": self.autoroles.get(gid),
                "muterole": self.muteroles.get(gid),
            }
        if "FROM level_config" in query:
            return None  # no row: refresh_guild_config falls back to the blob
        raise AssertionError("unexpected fetchrow: {!r}".format(query))

    async def fetchval(self, query, *args):
        if "FROM guild_settings" in query:  # settings.get_guild read-through
            return self.guild_settings.get(args[0])
        raise AssertionError("unexpected fetchval: {!r}".format(query))

    async def execute(self, query, *args):
        return "DELETE 1"


class MirrorBot:
    """Bot stand-in whose eager reload is core's REAL seam."""

    def __init__(self, pool, cogs):
        self.db_pool = pool
        self.prefixes = {}
        self.blacklist = set()
        self.autoroles = {}
        self.muteroles = {}
        self.eager_cache_lock = asyncio.Lock()
        self._cogs = cogs

    def get_cog(self, name):
        return self._cogs.get(name)

    async def load_eager_caches(self):
        await core.Yasuho.load_eager_caches(self)


def real_bot():
    """A real ``core.Yasuho``, built exactly the way production builds it.

    The four ``bot.*`` rows are registry rows like any other, and resolving them
    on ``MirrorBot`` would only ever prove that the stand-in still spells them
    the way the registry does - which is the very defect this file closes for the
    six cogs. ``Yasuho.__init__`` is offline: it builds containers, one asyncio
    primitive and an UNSTARTED sonolink client, and touches no socket, no pool
    and no file, so it belongs in this suite like the pure cog ``__init__`` do.
    """
    return core.Yasuho(db_pool=None)


def bot_framework_attrs():
    """The attribute names a bare discord.py Bot already carries.

    Subtracting these from a real Yasuho leaves precisely what THIS bot's
    ``__init__`` added, which is what the completeness sweep has to classify. It
    is computed by BUILDING a plain Bot rather than by listing names, so a
    discord.py upgrade that adds an attribute cannot turn into a false alarm here
    (it lands on both sides and cancels out).
    """
    return frozenset(
        vars(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    )


COG_CLASSES = {
    "ModLog": ModLog,
    "Starboard": Starboard,
    "AutoMod": AutoMod,
    "Leveling": Leveling,
    "CustomCommands": CustomCommands,
    "TemporaryRooms": TemporaryRooms,
}


class Bench:
    """Real cogs, a real bot seam, every registered structure seeded."""

    def __init__(self):
        self.pool = MirrorPool()
        self.bot = MirrorBot(self.pool, {})
        # The REAL cog objects. Every one of these __init__ is pure (it only
        # builds containers), which is what lets the guard bind to production
        # attribute names instead of to hand-written stand-ins.
        self.bot._cogs = {
            name: klass(self.bot) for name, klass in COG_CLASSES.items()
        }
        self._real_bot = None

    def owner(self, name):
        """The object the CONSUMERS are run against: real cogs, the stand-in bot.

        The stand-in is here only because ``resync_all`` and ``on_guild_join``
        need a ``get_cog`` and a pool. Its four map names are pinned against the
        real bot by ``test_the_bot_stand_in_carries_only_names_the_real_bot_has``,
        so it cannot drift; nothing is ever CHECKED for existence against it.
        """
        return self.bot if name == BOT else self.bot.get_cog(name)

    def real_owner(self, name):
        """The PRODUCTION object for an owner: a real cog, or a real Yasuho.

        This is what the rename guard and the completeness sweep resolve against
        - never ``owner`` - because a stand-in can only ever confirm itself.
        """
        if name != BOT:
            return self.bot.get_cog(name)
        if self._real_bot is None:
            self._real_bot = real_bot()
        return self._real_bot

    def structure(self, entry):
        return getattr(self.owner(entry.owner), entry.attr)

    def seed(self):
        """Put the seed under TARGET/NEIGHBOUR in every registered structure."""
        for entry in REGISTRY:
            cache = self.structure(entry)
            for scope in self._scopes(entry)[:2]:
                self._seed_one(cache, entry.key, scope)

    @staticmethod
    def _scopes(entry):
        if entry.key is USER_SET:
            return (TARGET_USER, NEIGHBOUR_USER, FRESH_USER)
        return (TARGET, NEIGHBOUR, FRESH)

    @staticmethod
    def _seed_one(cache, key, scope):
        if key is GUILD_MAP:
            cache[scope] = SEED
        elif key is GUILD_TUPLE_MAP:
            cache[(scope, "seed")] = SEED
        elif key is GUILD_TUPLE_SET:
            cache.add((scope, "seed"))
        elif key is USER_SET:
            cache.add(scope)
        else:  # pragma: no cover - a new key shape must teach the bench first
            raise AssertionError("unknown key shape {!r}".format(key))

    @staticmethod
    def _holds_seed(cache, key, scope):
        if key is GUILD_MAP:
            return cache.get(scope) is SEED
        if key is GUILD_TUPLE_MAP:
            return cache.get((scope, "seed")) is SEED
        if key is GUILD_TUPLE_SET:
            return (scope, "seed") in cache
        return scope in cache

    @staticmethod
    def _has_any(cache, key, scope):
        if key is GUILD_MAP:
            return scope in cache
        if key is GUILD_TUPLE_MAP:
            return any(k[0] == scope for k in cache)
        if key is GUILD_TUPLE_SET:
            return any(k[0] == scope for k in cache)
        return scope in cache

    def classify(self):
        """What the run just done did to each structure, per registry row.

        RELOAD is checked first and is the only outcome that can produce the
        database-only scope, so it can never be confused with DROP or EMPTY.
        """
        observed = {}
        for entry in REGISTRY:
            cache = self.structure(entry)
            target, neighbour, fresh = self._scopes(entry)
            if self._has_any(cache, entry.key, fresh):
                observed[entry.name] = RELOAD
            elif self._holds_seed(cache, entry.key, target):
                observed[entry.name] = KEEP
            elif self._holds_seed(cache, entry.key, neighbour):
                observed[entry.name] = DROP
            else:
                observed[entry.name] = EMPTY
        return observed

    def classify_join(self):
        """Whether the rejoin refill replaced this guild's pre-departure entry."""
        return {
            entry.name: (
                HEALS
                if self._holds_seed(
                    self.structure(entry), entry.key, self._scopes(entry)[0]
                )
                else REBUILD
            )
            for entry in REGISTRY
        }


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The tools.settings LRU is process-global; keep it out of other tests."""
    settings._cache.clear()
    yield
    settings._cache.clear()


# ---------------------------------------------------------------------------
# The registry against reality: a rename is red, and it names the attribute.
# ---------------------------------------------------------------------------


def test_every_registered_attribute_exists_on_the_real_owner():
    """The rename guard. Every row resolves on production's own object.

    ``real_owner`` and not ``owner``: the four ``bot.*`` rows have to land on a
    real ``core.Yasuho``, because checking them against ``MirrorBot`` would only
    ever ask the stand-in whether it agrees with itself - the exact shape of the
    defect this file was written for, one level up from the cogs.
    """
    bench = Bench()

    missing = absent_attributes(REGISTRY, bench.real_owner)

    assert missing == []
    assert len(REGISTRY) >= 19  # a registry that shrank examined less than it did
    # ...and the bot half of it really was resolved on core.Yasuho, so a future
    # resolver that quietly fell back to the stand-in cannot pass here.
    assert sum(1 for entry in REGISTRY if entry.owner == BOT) >= 4
    assert isinstance(bench.real_owner(BOT), core.Yasuho)


def test_the_bot_stand_in_carries_only_names_the_real_bot_has():
    """MirrorBot cannot outlive a rename on core.Yasuho either.

    The behavioural tests run the consumers against ``MirrorBot``, which
    hand-writes the four eager maps. If ``core.Yasuho`` renamed one, the stand-in
    would keep the old name, ``resync_all`` and the purge would go on operating
    on a map production no longer has, and every classification here would stay
    green. So each cache the stand-in declares is resolved on the real bot - the
    same treatment tests/cogs/test_dashboard_resync.py gives its ``Fake*`` cogs.

    Only PUBLIC caches: ``_cogs`` is the stand-in's own ``get_cog`` plumbing, not
    a mirror of anything, and a leading underscore is how it says so.
    """
    real = real_bot()
    stand_in = MirrorBot(MirrorPool(), {})

    checked = 0
    for attr, value in vars(stand_in).items():
        if attr.startswith("_") or not isinstance(value, (dict, set, BoundedLRU)):
            continue
        assert hasattr(real, attr), "MirrorBot.{}".format(attr)
        checked += 1

    assert checked == 4  # a sweep that resolved nothing must not pass


def test_the_absence_detector_reports_a_renamed_attribute():
    """NEGATIVE CONTROL: aim the detector at the exact bug and it must fire.

    Built from a fabricated owner rather than a real cog on purpose. A control
    that hard-coded a production attribute name would go red on a LEGITIMATE
    rename that was carried through everywhere, which teaches the next reader to
    edit the guard rather than to trust it.
    """
    owner = types.SimpleNamespace(_kept={})
    kept = _mirror("Stub", "_kept", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic")
    renamed = _mirror("Stub", "_gone", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic")

    assert absent_attributes([kept, renamed], lambda _o: owner) == ["Stub._gone"]


def test_the_absence_detector_clears_an_owner_that_carries_every_attribute():
    """...and stays silent when nothing is missing, so it is not a blanket alarm."""
    owner = types.SimpleNamespace(_kept={}, _also={})
    entries = [
        _mirror("Stub", "_kept", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic"),
        _mirror("Stub", "_also", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic"),
    ]

    assert absent_attributes(entries, lambda _o: owner) == []


def test_the_absence_detector_reports_an_owner_that_is_not_loaded():
    """A cog nobody can resolve is a miss too, not a free pass."""
    renamed = _mirror("Nope", "_x", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic")

    assert absent_attributes([renamed], lambda _owner: None) == ["Nope._x"]


def test_every_duck_typed_seam_is_a_real_method():
    """The method half of the same defect: getattr(cog, name, None) + callable()."""

    def resolve(owner):
        return core.Yasuho if owner is BOT else COG_CLASSES.get(owner)

    assert broken_seams(SEAMS, resolve) == []
    assert len(SEAMS) >= 9


def test_the_seam_detector_reports_a_renamed_method():
    """NEGATIVE CONTROL for the seam half, on a fabricated class."""

    class Stub:
        def kept(self):
            pass

        not_a_method = "just an attribute"

    assert broken_seams(
        [("Stub", "kept"), ("Stub", "gone"), ("Stub", "not_a_method")],
        lambda _owner: Stub,
    ) == ["Stub.gone", "Stub.not_a_method"]


def test_the_seam_detector_clears_a_class_that_has_every_method():
    class Stub:
        def kept(self):
            pass

    assert broken_seams([("Stub", "kept")], lambda _owner: Stub) == []


# ---------------------------------------------------------------------------
# The three consumers against the registry's recorded decisions.
# ---------------------------------------------------------------------------


def test_the_guild_purge_matches_its_recorded_decisions():
    """tools.retention.invalidate_guild_caches, run for real over real cogs.

    A per-guild DROP is distinguished from a wholesale EMPTY by the neighbouring
    guild's seed, which a correct drop must leave untouched.
    """
    bench = Bench()
    bench.seed()

    retention.invalidate_guild_caches(bench.bot, TARGET)

    observed = bench.classify()
    assert decision_mismatches(REGISTRY, "on_purge", observed) == []
    assert len(observed) == len(REGISTRY)


def test_the_purge_raises_on_a_renamed_bot_map_instead_of_swallowing_it():
    """The bot half of the purge reaches in DIRECTLY, and that is the point.

    ``getattr(bot, attr, None)`` there would swallow exactly the rename
    tools/retention.py's docstring promises will raise, and the bot's four maps
    are where a swallowed miss hurts most: an absent key is an ANSWER ('this
    guild has no custom prefix'), not a miss that a read-through heals.
    """
    bench = Bench()
    bench.seed()
    del bench.bot.prefixes

    with pytest.raises(AttributeError):
        retention.invalidate_guild_caches(bench.bot, TARGET)


async def test_the_reconnect_resync_matches_its_recorded_decisions():
    """dashboard_sync.resync_all, run for real over real cogs.

    The database-only guild is what proves a RELOAD re-read Postgres rather than
    just emptying the map, which is the distinction the eager caches live or die
    on: an emptied bot.prefixes is not a miss, it is a wrong answer.
    """
    bench = Bench()
    bench.seed()

    done = await dashboard_sync.resync_all(bench.bot)

    assert done == [name for name, _step in dashboard_sync._RESYNC_STEPS]
    observed = bench.classify()
    assert decision_mismatches(REGISTRY, "on_resync", observed) == []
    assert len(observed) == len(REGISTRY)


async def test_the_rejoin_refill_matches_its_recorded_decisions(caplog):
    """events.Events.on_guild_join, run for real over real cogs.

    The listener swallows every exception into one log line, so the log is
    asserted too: without that, a refill that crashed on its first statement
    would read as 'this cache heals itself' for everything after it.
    """
    bench = Bench()
    bench.seed()
    cog = object.__new__(events.Events)
    cog.bot = bench.bot

    with caplog.at_level(logging.ERROR, logger=events.log.name):
        await cog.on_guild_join(types.SimpleNamespace(id=TARGET))

    assert caplog.records == []
    observed = bench.classify_join()
    assert decision_mismatches(REGISTRY, "on_join", observed) == []
    assert len(observed) == len(REGISTRY)


def test_the_decision_detector_reports_a_consumer_that_stopped_dropping():
    """NEGATIVE CONTROL: the silence this whole file exists to break.

    A consumer that quietly stops dropping a cache leaves the seed in place. The
    detector must name the row, what was written down, and what happened.
    """
    entry = _mirror("Stub", "_cache", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic")

    assert decision_mismatches([entry], "on_purge", {"Stub._cache": KEEP}) == [
        ("Stub._cache", DROP, KEEP)
    ]


def test_the_decision_detector_reports_a_row_no_run_reached():
    """A row the run never touched cannot pass by default."""
    entry = _mirror("Stub", "_cache", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic")

    assert decision_mismatches([entry], "on_purge", {}) == [
        ("Stub._cache", DROP, "not observed")
    ]


def test_the_decision_detector_clears_when_the_run_agrees():
    """...and says nothing when the outcome is the one that was written down."""
    entry = _mirror("Stub", "_cache", GUILD_MAP, DROP, EMPTY, HEALS, "synthetic")

    assert decision_mismatches([entry], "on_purge", {"Stub._cache": DROP}) == []


def test_a_deliberate_omission_is_pinned_in_both_directions():
    """A KEEP is a decision, not a hole: starting to touch it is red too.

    ``_period_markers`` (reconnect) and ``_cd`` (reconnect) are the two omissions
    that were already written down in prose. This states them as data, so the
    day somebody 'tidies up' the resync into a loop over every cache, the guard
    says which two rows that breaks and why.

    The literal below IS a second write site for names the registry already
    carries, and deliberately so: it is the one place in this file where adding a
    row has to be a conscious act rather than a silent one, because a new KEEP is
    a new hole in the invalidation and deserves a second signature. A RENAME
    stays a single edit - any sed over the attribute name hits the registry row
    and this set together.
    """
    keeps = {
        entry.name
        for entry in REGISTRY
        if entry.on_resync is KEEP or entry.on_purge is KEEP
    }

    assert keeps == {
        "Leveling._period_markers",
        "CustomCommands._cd",
        "AutoMod._spam",
        "ModLog._recent_bans",
        "ModLog._suppressed",
        "bot.blacklist",
    }
    for entry in REGISTRY:
        if entry.on_resync is KEEP or entry.on_purge is KEEP:
            assert len(entry.why) > 40, entry.name


# ---------------------------------------------------------------------------
# Completeness: a cache added later must be classified, not merely forgotten.
# ---------------------------------------------------------------------------


def test_every_cache_on_a_mirroring_owner_is_classified():
    """The way this defect was born: a new cache, and only one list updated.

    ``_rank_cards`` was the newest Leveling cache and reached the reconnect list
    without ever reaching the purge one. Every mapping or set on a registered
    owner must therefore be either a registry row or a written waiver.

    THE BOT IS ONE OF THOSE OWNERS. A cache added to ``core.Yasuho.__init__`` and
    wired into only one of the three lists is the same bug with a shorter name,
    so it is swept here too - against the real bot, minus what a bare discord.py
    Bot already carries.
    """
    bench = Bench()
    instances = {name: bench.real_owner(name) for name in COG_CLASSES}
    instances[BOT] = bench.real_owner(BOT)
    baselines = {BOT: bot_framework_attrs()}
    registered = {(entry.owner, entry.attr) for entry in REGISTRY}

    unclassified = unclassified_caches(
        instances, registered, NOT_MIRRORED, baselines
    )

    assert unclassified == []
    swept = swept_attributes(instances, baselines)
    assert len(swept) >= 29  # a sweep that examined nothing must not pass
    # ...and the bot's own half of it is not the part that went empty: without
    # this, a baseline that swallowed all four would read as "nothing to
    # classify" instead of "the bot is no longer being swept".
    assert len([owner for owner, _attr in swept if owner == BOT]) >= 4


def test_the_completeness_detector_reports_an_unclassified_cache():
    """NEGATIVE CONTROL: a cache in neither list must be named.

    ``_public`` and ``_lock`` are in the same object to pin the sweep's stated
    boundary: it reports private MAPPINGS AND SETS, and only those.
    """
    owner = types.SimpleNamespace(
        _listed={}, _waived=set(), _forgotten=BoundedLRU(4), _lock=asyncio.Lock()
    )
    owner.public = {}

    unclassified = unclassified_caches(
        {"Stub": owner}, {("Stub", "_listed")}, {("Stub", "_waived"): "synthetic"}
    )

    assert unclassified == ["Stub._forgotten"]


def test_the_completeness_detector_clears_when_every_cache_is_accounted_for():
    """...and a waiver silences it, which is what makes a waiver a decision."""
    owner = types.SimpleNamespace(_listed={}, _waived=set(), _forgotten=BoundedLRU(4))

    unclassified = unclassified_caches(
        {"Stub": owner},
        {("Stub", "_listed")},
        {("Stub", "_waived"): "synthetic", ("Stub", "_forgotten"): "synthetic"},
    )

    assert unclassified == []


def test_a_new_cache_on_a_real_cog_is_reported_by_the_live_sweep():
    """The same detector, aimed at a REAL cog that just grew a cache."""
    bench = Bench()
    leveling = bench.owner("Leveling")
    leveling._brand_new_cache = {}

    unclassified = unclassified_caches(
        {"Leveling": leveling},
        {(entry.owner, entry.attr) for entry in REGISTRY},
        NOT_MIRRORED,
    )

    assert "Leveling._brand_new_cache" in unclassified


def test_a_new_cache_on_the_real_bot_is_reported_by_the_live_sweep():
    """The same detector, aimed at a REAL Yasuho that just grew a cache.

    This is the bot-level twin of the ``_rank_cards`` story: somebody adds
    ``self.brand_new_bot_cache = {}`` to ``core.Yasuho.__init__``, wires it into
    the purge and stops there. Public, so only the framework-baseline rule can see
    it; the private-name rule the cogs use would sweep straight past it.
    """
    bot = real_bot()
    bot.brand_new_bot_cache = {}

    unclassified = unclassified_caches(
        {BOT: bot},
        {(entry.owner, entry.attr) for entry in REGISTRY},
        NOT_MIRRORED,
        {BOT: bot_framework_attrs()},
    )

    assert unclassified == ["bot.brand_new_bot_cache"]


def test_the_framework_baseline_hides_the_library_and_only_the_library():
    """The baseline is a subtraction, not a blanket mute.

    It must swallow discord.py's own dicts (``all_commands`` and friends, which
    are not this bot's state and which nothing here could classify) while leaving
    every container ``Yasuho.__init__`` added - otherwise the bot half of the
    sweep is green because it looks at nothing.
    """
    baseline = bot_framework_attrs()
    swept = {
        attr
        for _owner, attr in swept_attributes({BOT: real_bot()}, {BOT: baseline})
    }

    assert "all_commands" in baseline  # the library really does own that one
    assert "all_commands" not in swept  # ...and the baseline hides it
    # Named from the registry rather than re-listed, so this control does not
    # become a fourth place to edit when a bot map is added or renamed.
    assert swept >= {entry.attr for entry in REGISTRY if entry.owner == BOT}
    assert len(swept) >= 4


# ---------------------------------------------------------------------------
# The registry's own hygiene.
# ---------------------------------------------------------------------------


def test_every_row_states_its_reasoning():
    """A row with no ``why`` is a list entry again, not a decision."""
    for entry in REGISTRY:
        assert entry.why.strip(), entry.name
    for (owner, attr), why in NOT_MIRRORED.items():
        assert why.strip(), "{}.{}".format(owner, attr)


def test_the_registry_names_each_structure_once():
    names = [entry.name for entry in REGISTRY]

    assert len(names) == len(set(names))


def test_every_decision_uses_a_known_value():
    for entry in REGISTRY:
        assert entry.key in (GUILD_MAP, GUILD_TUPLE_MAP, GUILD_TUPLE_SET, USER_SET)
        assert entry.on_purge in (DROP, EMPTY, RELOAD, KEEP)
        assert entry.on_resync in (DROP, EMPTY, RELOAD, KEEP)
        assert entry.on_join in (REBUILD, HEALS)
