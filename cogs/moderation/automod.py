"""Hybrid auto-moderation engine + command group.

This module is the ENGINE half of the AutoMod feature: message scanning (links /
invites / spam), Discord native-rule management, the settings cache, and the
``/automod`` command group. The Components V2 control panel and its display
catalog live in the sibling ``automod_panel.py`` (the presentation concern,
mirroring the music.py -> views.py split); this module imports the panel and the
action catalog from there, and the panel calls back into this cog - a one-way
import with no cycle.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the fancy
ellipsis anywhere in this file.
"""

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
from tools.formats import random_colour
from tools.i18n import _
from tools.lru_cache import BoundedLRU
from tools.snowflake import coerce_ids

log = logging.getLogger(__name__)

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

    # Generic links (kept for backward compatibility) and Discord invites.
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

    def __init__(self, bot):
        self.bot = bot
        self._spam = {}
        self._settings = _SettingsCache()
        self._scanned = BoundedLRU(_SCANNED_CAP)

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

    def _toggle_embed(self, feature, on):
        """A consistent one-shot confirmation for the single-filter commands."""

        return discord.Embed(
            title=_("AutoMod"),
            description=_("{feature} is now {state}.").format(
                feature=feature, state=_("enabled") if on else _("disabled")
            ),
            colour=random_colour(),
        )

    @automod.command(name="links", aliases=["antilink"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(state="Turn link filtering on or off.")
    async def automod_links(self, ctx, state: Literal["on", "off"]):
        """Turn link filtering on or off for this server."""

        on = state == "on"
        await self.set_custom_rule(ctx.guild.id, "link", on)
        await ctx.send(embed=self._toggle_embed(_("Link filtering"), on))

    @automod.command(name="invites", aliases=["antiinvite"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(state="Turn invite filtering on or off.")
    async def automod_invites(self, ctx, state: Literal["on", "off"]):
        """Turn Discord-invite filtering on or off for this server."""

        on = state == "on"
        await self.set_custom_rule(ctx.guild.id, "invite", on)
        await ctx.send(embed=self._toggle_embed(_("Invite filtering"), on))

    @automod.command(name="spam", aliases=["antispam"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(state="Turn spam filtering on or off.")
    async def automod_spam(self, ctx, state: Literal["on", "off"]):
        """Turn spam filtering on or off for this server."""

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
        """Persist a custom-rule toggle (link / invite / spam filtering)."""

        if key == "invite":
            await settings.set_guild(
                self.bot.db_pool, guild_id, "antiinvite", value
            )
            return

        column = "antilink" if key == "link" else "antispam"
        await db.upsert_guild_value(
            self.bot.db_pool, "automod", column, guild_id, value
        )
        self._update_cache(guild_id, **{column: value})

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

    # ------------------------------------------------------------------
    # Custom message scanning
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
        """Delete the message, apply the configured action, and log a case."""

        # Marked BEFORE the first await, so a second MESSAGE_UPDATE for this same
        # message cannot land a second warn/kick/timeout while this one is still
        # deleting and writing its case.
        self._scanned[message.id] = True

        guild = message.guild
        member = message.author
        action = await settings.get_guild(
            self.bot.db_pool, guild.id, "automod_action", DEFAULT_ACTION
        )
        if action not in VALID_ACTIONS:
            action = DEFAULT_ACTION
        warn_case_number = None

        # The offending message always goes, whatever the escalation level.
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            log.exception("AutoMod failed to delete %s message", kind)

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

        try:
            await message.channel.send(notice, delete_after=5)
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

    async def _enabled(self, guild_id):
        """The hot-path gate: ``(antilink, antispam, antiinvite)`` for a guild.

        Both reads are in-process caches (the automod row cache and
        tools.settings' bounded LRU), i.e. a dict lookup each on a warm cache -
        which is why every listener may call this before doing anything else.
        """
        s = await self.get_settings(guild_id)
        antilink = bool(s["antilink"]) if s else False
        antispam = bool(s["antispam"]) if s else False
        antiinvite = bool(
            await settings.get_guild(self.bot.db_pool, guild_id, "antiinvite", False)
        )
        return antilink, antispam, antiinvite

    async def _scan_content(self, message, *, antilink, antiinvite):
        """Run the content filters on ``message``; True if one of them acted.

        The invite/link half of the pipeline, shared verbatim by the send and the
        edit path - editing a message must be judged by exactly the rules that
        would have judged it on send, or the edit is just a slower way to post
        the same invite.
        """
        if antiinvite and self.invite_re.search(message.content):
            await self._handle_violation(
                message,
                kind="invite",
                notice=_(
                    "{user} Discord invite links aren't allowed here."
                ).format(user=message.author.mention),
                reason="Posted a Discord invite link",
            )
            return True

        if antilink and self.url_re.search(message.content):
            await self._handle_violation(
                message,
                kind="link",
                notice=_("{user} links aren't allowed here.").format(
                    user=message.author.mention
                ),
                reason="Posted a disallowed link",
            )
            return True

        return False

    def _claim_scan(self, message_id):
        """Claim a message for scanning; False if it is already claimed/actioned.

        Check-and-set with NO await in between, so it is atomic against every
        other listener callback: two MESSAGE_UPDATEs for the same message cannot
        both win it, and an id already marked by :meth:`_handle_violation` is
        never re-judged. The claim is released again by the caller when the scan
        finds nothing, so a member's later edits are still scanned.
        """
        if message_id in self._scanned:
            return False
        self._scanned[message_id] = True
        return True

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        # The enabled gate comes FIRST, before any permission work. The two reads
        # behind _enabled are in-process caches on the hot path, i.e. a dict
        # lookup each, whereas Member.guild_permissions FOLDS every one of the
        # author's role permission sets on every call. Computing that for every
        # message in every guild - the overwhelming majority of which have
        # automod off - bought nothing: a member with manage_messages is let
        # through either way, so the order is pure cost, not behaviour.
        antilink, antispam, antiinvite = await self._enabled(message.guild.id)

        if not (antilink or antispam or antiinvite):
            return

        # Moderators are never auto-moderated; checked here, once we know at
        # least one feature is actually on.
        if message.author.guild_permissions.manage_messages:
            return

        if await self._is_exempt(message):
            return

        if await self._scan_content(
            message, antilink=antilink, antiinvite=antiinvite
        ):
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

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        """Re-scan an edited message: posting clean and editing the invite in.

        AutoMod used to see NEW messages only, which made every content filter
        opt-out: post "hello", edit it into a discord.gg link, and nothing ever
        looked at it again. The filters run here on exactly the same content
        pipeline as on send (:meth:`_scan_content`) - a message must not become
        legal by arriving in two steps.

        WHY RAW AND NOT ``on_message_edit``. discord.py dispatches
        ``message_edit`` only when the message is still in ``ConnectionState``'s
        message cache, and this bot never passes ``max_messages``, so that cache
        is the default 1000 entries BOT-WIDE, across every guild. At the scale
        this project designs for (1000+ guilds) that deque turns over in seconds,
        so the "edit it in later" bypass survived the fix for anything but the
        last few hundred messages the bot happened to see - which is to say, for
        the common case. ``raw_message_edit`` is dispatched on EVERY
        MESSAGE_UPDATE, cached or not, and since discord.py 2.5 it carries a
        fully built ``payload.message``, so nothing has to be fetched.

        ORDERING (this is a hot listener - MESSAGE_UPDATE also fires when
        Discord attaches a link preview to somebody else's message, on its own,
        seconds after the fact, and now we see those for every guild):

        1. bot/DM guard, free;
        2. the content-changed gate, free and SYNCHRONOUS. With the message
           cached it is the exact comparison it always was
           (``payload.cached_message.content``). Without it, an unfurl is told
           apart by ``edited_timestamp``: Discord sets that only when the AUTHOR
           edits, never when it attaches an embed or flips a flag, so an
           unfurl on an uncached message still costs nothing;
        3. only then the enabled gate, keeping the house rule that no permission
           folding happens for a guild with automod off;
        4. the permission fold, the scan claim, the exemption check, the scan.

        Spam is deliberately NOT re-evaluated here: the sliding window counts
        messages SENT, and feeding edits into it would let an edit burst trip a
        spam action on a single message (and double-count the one it belongs to).
        """
        after = getattr(payload, "message", None)
        if after is None:
            return
        if after.author.bot or after.guild is None:
            return

        before = getattr(payload, "cached_message", None)
        if before is not None:
            if before.content == after.content:
                return
        elif getattr(after, "edited_timestamp", None) is None:
            return

        antilink, _antispam, antiinvite = await self._enabled(after.guild.id)

        # antispam alone does not open this gate: an edit is not a send.
        if not (antilink or antiinvite):
            return

        # The author of a RAW payload is a Member whenever the gateway sent the
        # `member` field (it does for a guild message) or the member is cached;
        # if neither held, permissions cannot be folded off a bare User, so the
        # guild cache gets one more chance and then the check is skipped rather
        # than crashed. Skipping means the message IS scanned, which is the safe
        # direction: the alternative would let anyone who can suppress that field
        # opt out of automod entirely.
        author = after.author
        if not isinstance(author, discord.Member):
            author = after.guild.get_member(author.id) or author
        permissions = getattr(author, "guild_permissions", None)
        if permissions is not None and permissions.manage_messages:
            return

        if not self._claim_scan(after.id):
            return

        acted = False
        try:
            if await self._is_exempt(after):
                return
            acted = await self._scan_content(
                after, antilink=antilink, antiinvite=antiinvite
            )
        finally:
            # A clean (or exempt, or failed) scan releases the claim, so the next
            # edit of this message is judged on its own merits. Only a message
            # that was actually actioned stays marked.
            if not acted:
                self._scanned.discard(after.id)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
