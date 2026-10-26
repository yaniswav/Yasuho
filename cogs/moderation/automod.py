"""Hybrid auto-moderation engine + command group.

This module is the ENGINE half of the AutoMod feature: Discord native-rule
management (link / invite filters included - the bot never reads message text
for them), the spam rate check, the settings cache, and the ``/automod`` command
group. The Components V2 control panel and its display
catalog live in the sibling ``automod_panel.py`` (the presentation concern,
mirroring the music.py -> views.py split); this module imports the panel and the
action catalog from there, and the panel calls back into this cog - a one-way
import with no cycle.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the fancy
ellipsis anywhere in this file.
"""

import asyncio
import datetime
import logging
import re
import time
from typing import Literal

import discord
from discord.ext import commands

from . import modactions
from cogs.moderation.automod_panel import (
    DEFAULT_ACTION,
    VALID_ACTIONS,
    AutoModPanel,
)
from tools import db, settings, warn_escalation
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _
from tools.lru_cache import BoundedLRU
from tools.snowflake import coerce_ids

log = logging.getLogger(__name__)

# THE HOT-PATH PEEK INTO tools.settings.
#
# The antiinvite toggle does not live in the ``automod`` table: it is a key in
# the guild's ``guild_settings`` JSONB blob, served from tools.settings' own
# process-wide LRU. That LRU is already in memory on the hot path - the only
# thing standing between ``on_message`` and it is that its reader is a
# COROUTINE, so asking it cost an await (and a coroutine object) for every
# message in every guild, including the overwhelming majority where all three
# AutoMod features are off. :meth:`AutoMod._enabled_cached` reads the SAME
# structure the coroutine would have read, synchronously.
#
# This is a deliberate reach into a module private, and it is a reach rather
# than a mirror ON PURPOSE: a second copy of the flag would be a third cache to
# invalidate, and ``settings.invalidate_guild`` already has a dozen callers
# across the bot (dashboard_sync, dashboard_actions, retention, tickets,
# serverstats). Reading the authoritative structure cannot go stale; a mirror
# of it would, silently, the first time somebody added a thirteenth caller.
#
# Both names are resolved HERE, at import, so a rename inside tools.settings is
# a loud failure when the extension loads (and in tests/test_import_smoke.py)
# instead of an AttributeError raised inside a listener.
_GUILD_BLOB_TABLE = settings._GUILD[0]
_GUILD_BLOBS = settings._cache

# Tells "no entry in the cache" apart from a seated ``None`` - which is a real,
# meaningful value here (the negative cache for a guild with no automod row).
_COLD = object()

# Anti-spam sliding window: keep the last _SPAM_WINDOW seconds of a member's
# message timestamps and trip when more than _SPAM_THRESHOLD land inside it.
# _SPAM_SWEEP_AT bounds the tracking map: once it holds more keys than this, the
# next hit drops every entry that has gone quiet past the window (so a one-off
# talker's key cannot linger forever).
_SPAM_WINDOW = 5
_SPAM_THRESHOLD = 5
_SPAM_SWEEP_AT = 1000

# Message ids that must not be scanned (again): claimed by an in-flight edit scan
# or already actioned. Bounded LRU, not a growing set - see AutoMod._claim_scan.
# Only a violation or an edit that got past the enabled gate ever inserts, so at
# 1000+ guilds this holds the last few thousand INTERESTING messages, not traffic.
_SCANNED_CAP = 2048

# How long one member's violations are coalesced into a single record and a
# single announcement. A member pasting 200 invites used to open 200 permanent
# case rows and post 200 mod-log embeds: the table grew without bound and the
# channel moderators actually read became unusable exactly when it mattered.
# One entry per burst is all a moderator needs to act, and the map behind it
# (tools.cooldowns) prunes itself, so this costs no unbounded state either.
# NOTHING punitive hangs off this window - see AutoMod._handle_violation.
_VIOLATION_LOG_WINDOW = 60


class _SettingsCache(dict):
    """The ``automod``-table read-through cache, with a generation guard.

    Mirrors ``tools/settings.py``: every invalidation bumps a generation, and a
    cold read samples it BEFORE its fetch and refuses to seat the result if it
    moved. Without that, a read already in flight when the dashboard invalidator
    lands re-seats the row it fetched from BEFORE the dashboard's write, and
    nothing ever re-reads a cache hit - so the stale toggles drive
    ``on_message`` until the next write to that guild.

    A ``dict`` SUBCLASS on purpose. The invalidators are not in this cog: they
    reach in from ``cogs/system/dashboard_sync.py`` through
    ``getattr(cog, "_settings", None)`` and mutate the mapping directly
    (``cache[gid] = row`` in ``_invalidate_automod``, ``cache.clear()`` in
    ``_resync_automod``), both behind an ``isinstance(cache, dict)`` guard this
    class satisfies. Bumping inside the mutation methods therefore covers every
    invalidator that exists today and any future one automatically - there is no
    way to invalidate this cache that bypasses the guard, and no way to forget.
    Only :meth:`seat` writes without bumping, because seating a cold read is not
    an invalidation: if it bumped, two concurrent misses would cancel each
    other's caching and the hot path would never keep a value at all.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generation = 0

    def _bump(self):
        self.generation += 1

    def __setitem__(self, key, value):
        self._bump()
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._bump()
        super().__delitem__(key)

    def pop(self, *args, **kwargs):
        self._bump()
        return super().pop(*args, **kwargs)

    def popitem(self):
        self._bump()
        return super().popitem()

    def setdefault(self, key, default=None):
        self._bump()
        return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        self._bump()
        super().update(*args, **kwargs)

    def clear(self):
        self._bump()
        super().clear()

    def seat(self, key, value, generation):
        """Cache a cold read's result unless an invalidation raced it.

        Returns the value the caller should use either way: a read that lost its
        cache seat still came from the database and is still the right answer for
        THIS caller - it is only untrustworthy to seat for the NEXT one. And if
        another task populated the key while we awaited the fetch, that value
        wins (the same reason ``tools/settings.py`` seats with ``setdefault``):
        it cannot be older than ours.
        """
        if generation != self.generation:
            return value
        if key in self:
            return dict.__getitem__(self, key)
        # dict.__setitem__, not self[key]: seating must not bump the generation.
        dict.__setitem__(self, key, value)
        return value


class AutoMod(commands.Cog):
    """Hybrid auto-moderation: Yasuho's message scanning plus Discord's native AutoMod."""

    # Generic links and Discord invites. Enforcement uses the native patterns
    # in FILTER_PATTERNS; these Python twins stay the reference cogs/fun/fun.py
    # mirrors (and tests compare against).
    url_re = re.compile(r"https?://\S+|discord\.gg/\S+", re.IGNORECASE)
    invite_re = re.compile(
        r"(?:https?://)?(?:www\.)?"
        r"(?:discord(?:\.gg|app\.com/invite|\.com/invite)|discord\.me|discord\.io)"
        r"/[\w-]+",
        re.IGNORECASE,
    )

    # Our managed native rules: panel key -> the rule name we own in the guild.
    NATIVE_RULE_NAMES = {
        "kw": "Yasuho - Keyword Filter",
        "nspam": "Yasuho - Spam",
        "nmention": "Yasuho - Mention Spam",
    }

    # The link / invite filters are ONE native keyword rule: Discord blocks
    # the message before anyone sees it, and the bot never reads message text
    # for them (the Message Content intent is requested for moderation logging
    # only). One rule, not two, so a message matching both filters is one
    # execution and one punishment, and it costs one of the guild's six keyword
    # rule slots. The server's escalation (warn / mute / kick, cases) runs from
    # the AUTO_MODERATION_ACTION_EXECUTION event - see on_automod_action.
    # Patterns are Discord's Rust regex flavour.
    FILTER_RULE_NAME = "Yasuho - Links & Invites"
    FILTER_PATTERNS = {
        "invite": [
            r"(?i)(discord(\.gg|app\.com/invite|\.com/invite)|discord\.me|discord\.io)/[\w-]+",
        ],
        "link": [r"(?i)https?://\S+", r"(?i)discord\.gg/\S+"],
    }
    # guild_settings key holding the managed rule's id: ownership is by id, so
    # an admin renaming the rule neither orphans it nor makes us create a twin.
    FILTER_RULE_KEY = "automod_native_filter_rule"
    # Discord caps a rule's exemptions.
    _NATIVE_MAX_EXEMPT_ROLES = 20
    _NATIVE_MAX_EXEMPT_CHANNELS = 50

    def __init__(self, bot):
        self.bot = bot
        self._spam = {}
        self._settings = _SettingsCache()
        self._scanned = BoundedLRU(_SCANNED_CAP)
        # (guild_id, member_id) -> last time this member's violation was
        # RECORDED and ANNOUNCED. Never gates a deletion or a punishment.
        self._violation_log = Cooldowns(_VIOLATION_LOG_WINDOW)
        # rule_id -> custom key ("link" / "invite") or None for a rule that is
        # not one of our filters; filled when we create, edit or first see one.
        # guild_id -> asyncio.Lock: one native filter sync at a time per guild,
        # or two concurrent syncs could both create the rule.
        self._sync_locks = {}
        self._migrated = False
        self._reconcile_task = None
        self._reconcile_again = False

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------
    # SECURITY: every subcommand below repeats guild_only + has_permissions.
    # That is NOT redundant with the group's own checks. discord.py forces
    # ``HybridGroup.invoke_without_command = True``, so Group.invoke takes the
    # ``early_invoke = False`` branch and never calls the group's prepare() -
    # the group's checks run ONLY on a bare ``?automod``. The slash path is
    # worse still: HybridAppCommand._check_can_run consults
    # ``self.wrapped.checks``, i.e. the SUBCOMMAND's checks alone. A check that
    # lives only on the parent gates neither path for a subcommand.
    @commands.hybrid_group(name="automod")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod(self, ctx):
        """Open the AutoMod control panel, or manage a single filter."""

        # Bare prefix invoke opens the panel, matching the house config /
        # levelconfig panels. Slash users reach it via `/automod panel` (a group
        # itself is never directly invokable in Discord's UI).
        if ctx.invoked_subcommand is None:
            await self._open_panel(ctx)

    async def _open_panel(self, ctx):
        state = await self._panel_state(ctx.guild)
        view = AutoModPanel(self, ctx.guild, ctx.author.id, state)
        view.message = await ctx.send(view=view)

    def _toggle_embed(self, feature, on, *, synced=True):
        """A consistent one-shot confirmation for the single-filter commands."""

        description = _("{feature} is now {state}.").format(
            feature=feature, state=_("enabled") if on else _("disabled")
        )
        if not synced:
            description += "\n" + _(
                "Saved, but Discord's rule could not be updated: Yasuho needs "
                "the **Manage Server** permission, and the server can have at "
                "most 6 keyword rules (or 20 exempt roles)."
            )
        return discord.Embed(
            title=_("AutoMod"), description=description, colour=random_colour()
        )

    @automod.command(name="links", aliases=["antilink"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(state="Turn link filtering on or off.")
    async def automod_links(self, ctx, state: Literal["on", "off"]):
        """Turn link filtering on or off for this server."""

        # Link / invite toggles sync a native rule over REST first.
        await ctx.defer()
        on = state == "on"
        ok = await self.set_custom_rule(ctx.guild.id, "link", on)
        await ctx.send(embed=self._toggle_embed(_("Link filtering"), on, synced=ok))

    @automod.command(name="invites", aliases=["antiinvite"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(state="Turn invite filtering on or off.")
    async def automod_invites(self, ctx, state: Literal["on", "off"]):
        """Turn Discord-invite filtering on or off for this server."""

        # Link / invite toggles sync a native rule over REST first.
        await ctx.defer()
        on = state == "on"
        ok = await self.set_custom_rule(ctx.guild.id, "invite", on)
        await ctx.send(embed=self._toggle_embed(_("Invite filtering"), on, synced=ok))

    @automod.command(name="spam", aliases=["antispam"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(state="Turn spam filtering on or off.")
    async def automod_spam(self, ctx, state: Literal["on", "off"]):
        """Turn spam filtering on or off for this server."""

        # Link / invite toggles sync a native rule over REST first.
        await ctx.defer()
        on = state == "on"
        await self.set_custom_rule(ctx.guild.id, "spam", on)
        await ctx.send(embed=self._toggle_embed(_("Spam filtering"), on))

    @automod.command(name="panel")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod_panel(self, ctx):
        """Open the interactive AutoMod control panel."""

        await self._open_panel(ctx)

    # ------------------------------------------------------------------
    # Custom-rule settings (cached, mirrors the original pattern)
    # ------------------------------------------------------------------
    async def get_settings(self, guild_id):
        """Read the guild's ``automod`` row, caching misses (negative cache too).

        The generation is sampled BEFORE the fetch and re-checked by
        :meth:`_SettingsCache.seat`, so a dashboard invalidation that lands while
        this fetch is in flight cannot be undone by seating our older snapshot -
        see :class:`_SettingsCache`.
        """
        if guild_id in self._settings:
            return self._settings[guild_id]

        generation = self._settings.generation
        query = """SELECT antilink, antispam FROM automod WHERE guild_id = $1;"""
        row = await self.bot.db_pool.fetchrow(query, guild_id)
        return self._settings.seat(guild_id, row, generation)

    def _update_cache(self, guild_id, **changes):
        current = self._settings.get(guild_id)
        data = {
            "antilink": bool(current["antilink"]) if current else False,
            "antispam": bool(current["antispam"]) if current else False,
        }
        data.update(changes)
        # Plain assignment, so the generation bumps: this IS a write, and a cold
        # read still in flight must not re-seat the pre-write row over it.
        self._settings[guild_id] = data

    async def set_custom_rule(self, guild_id, key, value):
        """Persist a custom-rule toggle (link / invite / spam filtering).

        Link and invite filtering are enforced by native rules, so their toggle
        also syncs those rules. Returns False when that sync could not reach
        Discord (the bot lacks Manage Server): the setting is saved either way
        and is applied by the next successful sync.
        """

        if key == "invite":
            await settings.set_guild(
                self.bot.db_pool, guild_id, "antiinvite", value
            )
        else:
            column = "antilink" if key == "link" else "antispam"
            await db.upsert_guild_value(
                self.bot.db_pool, "automod", column, guild_id, value
            )
            self._update_cache(guild_id, **{column: value})

        if key in self.FILTER_PATTERNS:
            guild = self.bot.get_guild(guild_id)
            return guild is not None and await self.sync_native_filters(guild)
        return True

    async def _panel_state(self, guild):
        pool = self.bot.db_pool
        s = await self.get_settings(guild.id)
        action = await settings.get_guild(
            pool, guild.id, "automod_action", DEFAULT_ACTION
        )
        # coerce_ids: the dashboard writes snowflakes as STRINGS, so an exempt
        # list can come back as ["123"] - which would render as a dead entry in
        # the panel (``guild.get_role("123")`` is None) and never match a live id.
        exempt_roles = coerce_ids(
            await settings.get_guild(pool, guild.id, "automod_exempt_roles", [])
        )
        exempt_channels = coerce_ids(
            await settings.get_guild(
                pool, guild.id, "automod_exempt_channels", []
            )
        )
        native = await self.native_state(guild)
        return {
            "link": bool(s["antilink"]) if s else False,
            "spam": bool(s["antispam"]) if s else False,
            "invite": bool(
                await settings.get_guild(pool, guild.id, "antiinvite", False)
            ),
            "kw": native["kw"],
            "nspam": native["nspam"],
            "nmention": native["nmention"],
            "action": action if action in VALID_ACTIONS else DEFAULT_ACTION,
            "exempt_roles": exempt_roles,
            "exempt_channels": exempt_channels,
        }

    # ------------------------------------------------------------------
    # Native Discord AutoMod
    # ------------------------------------------------------------------
    def _build_native_trigger(self, key):
        types = discord.AutoModRuleTriggerType
        if key == "kw":
            return discord.AutoModTrigger(
                type=types.keyword_preset, presets=discord.AutoModPresets.all()
            )
        if key == "nspam":
            return discord.AutoModTrigger(type=types.spam)
        if key == "nmention":
            return discord.AutoModTrigger(
                type=types.mention_spam, mention_limit=5
            )
        return None

    async def _fetch_native_rules(self, guild):
        """Map our managed rules; return None if the API is not accessible."""

        try:
            rules = await guild.fetch_automod_rules()
        except (discord.Forbidden, discord.HTTPException):
            return None
        by_name = {rule.name: rule for rule in rules}
        return {key: by_name.get(name) for key, name in self.NATIVE_RULE_NAMES.items()}

    async def native_state(self, guild):
        """Per-rule tri-state: True/False if known, None if unavailable."""

        rules = await self._fetch_native_rules(guild)
        if rules is None:
            return {key: None for key in self.NATIVE_RULE_NAMES}
        return {
            key: (rule.enabled if rule is not None else False)
            for key, rule in rules.items()
        }

    async def set_native_rule(self, guild, key, enabled):
        """Create or edit a managed native rule. Returns (ok, new_state)."""

        name = self.NATIVE_RULE_NAMES.get(key)
        if name is None:
            return False, None

        try:
            rules = await guild.fetch_automod_rules()
        except (discord.Forbidden, discord.HTTPException):
            return False, None

        existing = discord.utils.get(rules, name=name)
        try:
            if existing is None:
                if not enabled:
                    # Nothing to disable; treat as already off.
                    return True, False
                trigger = self._build_native_trigger(key)
                if trigger is None:
                    return False, None
                action = discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.block_message
                )
                await guild.create_automod_rule(
                    name=name,
                    event_type=discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=[action],
                    enabled=True,
                    reason="Yasuho AutoMod panel",
                )
                return True, True

            await existing.edit(enabled=enabled, reason="Yasuho AutoMod panel")
            return True, enabled
        except (discord.Forbidden, discord.HTTPException):
            log.exception("AutoMod native rule update failed")
            return False, None

    async def _exemptions(self, guild):
        """Native exemptions: the saved lists, plus every role that grants
        Manage Messages - moderators were never filtered before the move to
        native rules, and Discord only exempts Manage Server / Administrator.
        Returns (roles, channels, complete); complete is False when Discord's
        caps forced entries out."""
        pool = self.bot.db_pool
        saved_roles = coerce_ids(
            await settings.get_guild(pool, guild.id, "automod_exempt_roles", [])
        )
        channels = coerce_ids(
            await settings.get_guild(pool, guild.id, "automod_exempt_channels", [])
        )
        moderator_roles = [
            role.id
            for role in getattr(guild, "roles", ())
            if not role.is_default()
            and (role.permissions.manage_messages or role.permissions.administrator)
        ]
        roles = list(dict.fromkeys([*saved_roles, *moderator_roles]))
        complete = (
            len(roles) <= self._NATIVE_MAX_EXEMPT_ROLES
            and len(channels) <= self._NATIVE_MAX_EXEMPT_CHANNELS
        )
        return (
            roles[: self._NATIVE_MAX_EXEMPT_ROLES],
            channels[: self._NATIVE_MAX_EXEMPT_CHANNELS],
            complete,
        )

    async def sync_native_filters(self, guild):
        """Make the managed native rule match the saved settings.

        Serialised per guild. Finds the rule by its stored id (adopting one by
        name only when no id is stored), creates it only when a filter is on,
        and otherwise edits it back to the full intended shape: patterns, block
        action, enabled flag, exemptions. Returns False when Discord refused or
        failed (permission, the six-keyword-rule limit) or an exemption list
        had to be cut to Discord's caps; the settings stay saved either way.
        """
        lock = self._sync_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            return await self._sync_native_filters(guild)

    async def _sync_native_filters(self, guild):
        pool = self.bot.db_pool
        antilink, _antispam, antiinvite = await self._enabled(guild.id)
        patterns = []
        if antiinvite:
            patterns += self.FILTER_PATTERNS["invite"]
        if antilink:
            patterns += self.FILTER_PATTERNS["link"]
        roles, channels, complete = await self._exemptions(guild)
        stored = await settings.get_guild(pool, guild.id, self.FILTER_RULE_KEY)
        shape = dict(
            trigger=discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.keyword,
                # A disabled rule still needs a valid trigger.
                regex_patterns=patterns or self.FILTER_PATTERNS["invite"],
            ),
            actions=[
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.block_message
                )
            ],
            enabled=bool(patterns),
            exempt_roles=[discord.Object(id=i) for i in roles],
            exempt_channels=[discord.Object(id=i) for i in channels],
            reason="Yasuho AutoMod",
        )
        try:
            rules = await guild.fetch_automod_rules()
            if stored is not None:
                # Ours by id. If an admin deleted it, a rule that merely bears
                # the name is NOT adopted: a replacement is created instead.
                rule = discord.utils.get(rules, id=int(stored))
            else:
                rule = discord.utils.get(rules, name=self.FILTER_RULE_NAME)
            if rule is None:
                if not patterns:
                    return True
                rule = await guild.create_automod_rule(
                    name=self.FILTER_RULE_NAME,
                    event_type=discord.AutoModRuleEventType.message_send,
                    **shape,
                )
            else:
                await rule.edit(**shape)
        except discord.Forbidden:
            log.warning("AUTOMOD-NATIVE-REFUSED guild=%s (no Manage Server)", guild.id)
            return False
        except discord.HTTPException:
            log.exception("AUTOMOD-NATIVE-FAILED guild=%s", guild.id)
            return False
        if stored is None or int(stored) != rule.id:
            await settings.set_guild(pool, guild.id, self.FILTER_RULE_KEY, rule.id)
        if not complete:
            log.warning("AUTOMOD-EXEMPT-TRUNCATED guild=%s", guild.id)
        return complete

    @commands.Cog.listener()
    async def on_ready(self):
        """Once per process: reconcile every guild that has a managed rule or a
        filter on (servers set up before the filters went native, a sync that
        failed, a dashboard change missed while disconnected)."""

        if self._migrated:
            return
        self._migrated = True
        self.request_reconcile()

    def request_reconcile(self):
        """Run :meth:`reconcile_all` in this cog's ONE background task. A
        request while a pass is running queues exactly one more pass (so a
        change missed mid-pass is still picked up), never a parallel sweep."""
        if self._reconcile_task is not None and not self._reconcile_task.done():
            self._reconcile_again = True
            return
        self._reconcile_task = asyncio.get_running_loop().create_task(
            self._reconcile_loop()
        )

    async def _reconcile_loop(self):
        while True:
            self._reconcile_again = False
            try:
                await self.reconcile_all()
            except Exception:
                log.exception("AutoMod native reconciliation pass failed")
            if not self._reconcile_again:
                return

    async def cog_unload(self):
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()

    async def reconcile_all(self):
        """Sync every guild that has the filter on or owns a managed rule.
        Sequential and paced: a handful of REST calls per guild; one guild's
        failure is logged and the pass goes on."""
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT guild_id FROM automod WHERE antilink "
                "UNION SELECT guild_id FROM guild_settings "
                "WHERE (settings->>'antiinvite')::boolean "
                "OR settings ? $1",
                self.FILTER_RULE_KEY,
            )
        except Exception:
            log.exception("AutoMod native reconciliation query failed")
            return
        synced = failed = 0
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            try:
                ok = await self.sync_native_filters(guild)
            except Exception:
                log.exception("AUTOMOD-NATIVE-FAILED guild=%s", guild.id)
                ok = False
            if ok:
                synced += 1
            else:
                failed += 1
            await asyncio.sleep(1)
        log.info("AUTOMOD-NATIVE-SYNC synced=%s failed=%s", synced, failed)

    @staticmethod
    def _moderates(role):
        return role.permissions.manage_messages or role.permissions.administrator

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        """A role gaining or losing Manage Messages changes the exemptions."""
        if self._moderates(before) != self._moderates(after):
            await self._resync_if_managed(after.guild)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        """A role born with Manage Messages is a moderator role to exempt."""
        if self._moderates(role):
            await self._resync_if_managed(role.guild)

    async def _resync_if_managed(self, guild):
        stored = await settings.get_guild(
            self.bot.db_pool, guild.id, self.FILTER_RULE_KEY
        )
        if stored is not None:
            await self.sync_native_filters(guild)

    @commands.Cog.listener()
    async def on_automod_action(self, execution):
        """Apply the server's escalation when our native filter blocks a message.

        Discord already blocked the message; this only does what the native
        action cannot: warn / mute / kick, the case, the short notice. It reads
        the member, the rule and the matched PATTERN, never the message text.
        """
        if execution.action.type != discord.AutoModRuleActionType.block_message:
            return
        guild = execution.guild
        pool = self.bot.db_pool
        stored = await settings.get_guild(pool, guild.id, self.FILTER_RULE_KEY)
        if stored is None or int(stored) != execution.rule_id:
            return
        antilink, _antispam, antiinvite = await self._enabled(guild.id)
        if not (antilink or antiinvite):
            return
        member = execution.member
        if member is None:
            try:
                member = await guild.fetch_member(execution.user_id)
            except discord.HTTPException:
                return
        if member.bot or member.guild_permissions.manage_messages:
            return
        if execution.matched_keyword in self.FILTER_PATTERNS["invite"]:
            notice = _("{user} Discord invite links aren't allowed here.").format(
                user=member.mention
            )
            reason = "Posted a Discord invite link"
        else:
            notice = _("{user} links aren't allowed here.").format(user=member.mention)
            reason = "Posted a disallowed link"
        await self._punish(
            guild, member, execution.channel, notice=notice, reason=reason
        )

    # ------------------------------------------------------------------
    # Spam (message metadata only: who posted when, never what)
    # ------------------------------------------------------------------
    async def _is_exempt(self, message):
        pool = self.bot.db_pool
        guild_id = message.guild.id

        # coerce_ids on both lists: a dashboard-written ["123"] would never match
        # ``message.channel.id`` / a member's role ids, so the exemption would
        # silently stop protecting the people it names.
        exempt_channels = coerce_ids(
            await settings.get_guild(
                pool, guild_id, "automod_exempt_channels", []
            )
        )
        if exempt_channels:
            if message.channel.id in exempt_channels:
                return True
            parent_id = getattr(message.channel, "parent_id", None)
            if parent_id is not None and parent_id in exempt_channels:
                return True

        exempt_roles = coerce_ids(
            await settings.get_guild(pool, guild_id, "automod_exempt_roles", [])
        )
        if exempt_roles:
            role_ids = {role.id for role in message.author.roles}
            if role_ids.intersection(exempt_roles):
                return True
        return False

    async def _log_case(
        self, guild, target, action, reason, *, case_number=None
    ):
        """Open a moderation case and funnel the embed to the mod-log."""

        if case_number is None:
            try:
                case_number = await modactions.create_case(
                    self.bot.db_pool,
                    guild.id,
                    target.id,
                    self.bot.user.id,
                    action,
                    reason,
                )
            except Exception:
                log.exception("AutoMod failed to create case")
                return

        embed = modactions.case_embed(
            case_number, action, target, guild.me, reason
        )
        await modactions.funnel_action(self.bot, guild, embed)

    async def _handle_violation(self, message, *, kind, notice, reason):
        """Delete a spam message, then punish its author (see :meth:`_punish`)."""

        # Marked BEFORE the first await, so a second event for this same
        # message cannot land a second warn/kick/timeout while this one is
        # still deleting and writing its case.
        self._scanned[message.id] = True

        # The offending message always goes, whatever the escalation level.
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            log.exception("AutoMod failed to delete %s message", kind)

        await self._punish(
            message.guild, message.author, message.channel,
            notice=notice, reason=reason,
        )

    async def _punish(self, guild, member, channel, *, notice, reason):
        """Apply the configured action, and record and announce it.

        WHAT IS THROTTLED AND WHAT IS NOT. A member can trip this hundreds of
        times in seconds, so the RECORD and the ANNOUNCEMENT are coalesced to one
        per member per ``_VIOLATION_LOG_WINDOW``: the case AutoMod opens on its
        own, the mod-log post, and the short in-channel notice. Everything that
        actually moderates runs on EVERY violation and is gated by nothing - the
        timeout / kick is always applied, and a warn is always recorded and
        always feeds the escalation counter (that row is the punishment, not the
        paperwork). 200 invites therefore still cost the spammer 200 warns; they
        just stop costing the server 200 permanent rows and 200 mod-log embeds.
        """

        # Checked and armed with no await in between, so two violations racing
        # through this coroutine cannot both believe they are the first one.
        announce = not self._violation_log.is_active((guild.id, member.id))
        if announce:
            self._violation_log.touch((guild.id, member.id))

        action = await settings.get_guild(
            self.bot.db_pool, guild.id, "automod_action", DEFAULT_ACTION
        )
        if action not in VALID_ACTIONS:
            action = DEFAULT_ACTION
        warn_case_number = None

        if action == "mute":
            try:
                await member.timeout(
                    datetime.timedelta(minutes=10), reason=f"AutoMod: {reason}"
                )
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                log.exception("AutoMod failed to time out member")
        elif action == "kick":
            try:
                await guild.kick(member, reason=f"AutoMod: {reason}")
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                log.exception("AutoMod failed to kick member")
        elif action == "warn":
            # Persist the case and counter as one atomic operation, then apply
            # the same configurable escalation policy as the manual command.
            warn_case_number, new_count = await modactions.record_warn(
                self.bot.db_pool,
                guild.id,
                member.id,
                self.bot.user.id,
                reason,
            )
            policy, _default = await modactions.load_escalation_policy(
                self.bot.db_pool, guild.id
            )
            rule = warn_escalation.action_for_count(policy, new_count)
            if rule is not None:
                await modactions.apply_escalation_action(
                    self.bot, guild, member, rule
                )

        if not announce:
            return

        if channel is not None:
            try:
                await channel.send(notice, delete_after=5)
            except discord.HTTPException:
                pass

        await self._log_case(
            guild,
            member,
            action,
            reason,
            case_number=warn_case_number,
        )

    def _prune_spam(self, now):
        """Drop spam-tracking entries whose newest timestamp is past the window."""
        self._spam = {
            k: ts
            for k, ts in self._spam.items()
            if ts and now - ts[-1] <= _SPAM_WINDOW
        }

    def _enabled_cached(self, guild_id):
        """The gate from WARM caches only: the triple, or None if either is cold.

        SAME TWO STRUCTURES, SAME VERDICTS as :meth:`_enabled` - this just
        refuses to build a coroutine to read a dict. Both halves of the gate are
        already in memory in the common case:

        * ``self._settings`` is this cog's own read-through cache of the
          ``automod`` row (a seated ``None`` means "looked up, no row", which is
          why the miss is told apart by :data:`_COLD` and not by falsiness);
        * the ``antiinvite`` key rides tools.settings' guild LRU, peeked through
          :data:`_GUILD_BLOBS` exactly the way ``settings._load`` would read it
          on a hit (``key in cache`` then ``cache[key]``, same order, same LRU
          bookkeeping).

        Returning None on a cold half is what keeps this honest: the caller then
        pays for the real :meth:`_enabled`, which reads through to Postgres and
        seats both caches, and every later message in that guild is free. There
        is no third cache and therefore nothing new to invalidate - see the
        module header.
        """
        row = self._settings.get(guild_id, _COLD)
        if row is _COLD:
            return None
        blob_key = (_GUILD_BLOB_TABLE, guild_id)
        if blob_key not in _GUILD_BLOBS:
            return None
        blob = _GUILD_BLOBS[blob_key]
        return (
            bool(row["antilink"]) if row else False,
            bool(row["antispam"]) if row else False,
            bool(blob.get("antiinvite", False)),
        )

    async def _enabled(self, guild_id):
        """The hot-path gate: ``(antilink, antispam, antiinvite)`` for a guild.

        The COLD half of the gate. Callers on a hot listener try
        :meth:`_enabled_cached` first and only fall back here, so this runs on a
        guild's first scanned message (or right after an invalidation) and then
        never again until something evicts a cache.
        """
        s = await self.get_settings(guild_id)
        antilink = bool(s["antilink"]) if s else False
        antispam = bool(s["antispam"]) if s else False
        antiinvite = bool(
            await settings.get_guild(self.bot.db_pool, guild_id, "antiinvite", False)
        )
        return antilink, antispam, antiinvite

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        # The enabled gate comes FIRST, before any permission work, and in the
        # common case it comes without an AWAIT. Member.guild_permissions FOLDS
        # every one of the author's role permission sets on every call, so it
        # stays behind the gate: a member with manage_messages is let through
        # either way, which makes the order pure cost, not behaviour.
        #
        # Both halves of the gate are in-process caches, but both readers were
        # coroutines, so an all-off guild paid two awaits per message anyway -
        # while the three other on_message listeners in this bot (leveling, afk,
        # serverstats) all decide on a synchronous dict before anything.
        # _enabled_cached is that synchronous decision; None means a cache is
        # cold and the real reader has to run, which happens once per guild.
        gate = self._enabled_cached(message.guild.id)
        if gate is None:
            gate = await self._enabled(message.guild.id)
        _antilink, antispam, _antiinvite = gate

        # Links and invites are native rules now (sync_native_filters); the
        # only thing left on the message hot path is the spam rate, which needs
        # who-posted-when and never the text.
        if not antispam:
            return

        # Moderators are never auto-moderated; checked here, once we know at
        # least one feature is actually on.
        if message.author.guild_permissions.manage_messages:
            return

        if await self._is_exempt(message):
            return

        if antispam:
            key = (message.guild.id, message.author.id)
            now = time.time()
            timestamps = self._spam.setdefault(key, [])
            timestamps.append(now)
            recent = [t for t in timestamps if now - t <= _SPAM_WINDOW]
            if recent:
                self._spam[key] = recent
                if len(self._spam) > _SPAM_SWEEP_AT:
                    self._prune_spam(now)
            else:
                self._spam.pop(key, None)
                return

            if len(recent) > _SPAM_THRESHOLD:
                self._spam.pop(key, None)
                await self._handle_violation(
                    message,
                    kind="spam",
                    notice=_(
                        "{user} slow down - you're sending messages too fast."
                    ).format(user=message.author.mention),
                    reason="Spamming messages",
                )


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
