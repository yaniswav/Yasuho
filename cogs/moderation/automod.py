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

from cogs.moderation.automod_panel import (
    DEFAULT_ACTION,
    VALID_ACTIONS,
    AutoModPanel,
)
from tools import db, modactions, settings, warn_escalation
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Anti-spam sliding window: keep the last _SPAM_WINDOW seconds of a member's
# message timestamps and trip when more than _SPAM_THRESHOLD land inside it.
# _SPAM_SWEEP_AT bounds the tracking map: once it holds more keys than this, the
# next hit drops every entry that has gone quiet past the window (so a one-off
# talker's key cannot linger forever).
_SPAM_WINDOW = 5
_SPAM_THRESHOLD = 5
_SPAM_SWEEP_AT = 1000


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
        self._settings = {}

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------
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
    @discord.app_commands.describe(state="Turn link filtering on or off.")
    async def automod_links(self, ctx, state: Literal["on", "off"]):
        """Turn link filtering on or off for this server."""

        on = state == "on"
        await self.set_custom_rule(ctx.guild.id, "link", on)
        await ctx.send(embed=self._toggle_embed(_("Link filtering"), on))

    @automod.command(name="invites", aliases=["antiinvite"])
    @discord.app_commands.describe(state="Turn invite filtering on or off.")
    async def automod_invites(self, ctx, state: Literal["on", "off"]):
        """Turn Discord-invite filtering on or off for this server."""

        on = state == "on"
        await self.set_custom_rule(ctx.guild.id, "invite", on)
        await ctx.send(embed=self._toggle_embed(_("Invite filtering"), on))

    @automod.command(name="spam", aliases=["antispam"])
    @discord.app_commands.describe(state="Turn spam filtering on or off.")
    async def automod_spam(self, ctx, state: Literal["on", "off"]):
        """Turn spam filtering on or off for this server."""

        on = state == "on"
        await self.set_custom_rule(ctx.guild.id, "spam", on)
        await ctx.send(embed=self._toggle_embed(_("Spam filtering"), on))

    @automod.command(name="panel")
    async def automod_panel(self, ctx):
        """Open the interactive AutoMod control panel."""

        await self._open_panel(ctx)

    # ------------------------------------------------------------------
    # Custom-rule settings (cached, mirrors the original pattern)
    # ------------------------------------------------------------------
    async def get_settings(self, guild_id):
        if guild_id in self._settings:
            return self._settings[guild_id]

        query = """SELECT antilink, antispam FROM automod WHERE guild_id = $1;"""
        row = await self.bot.db_pool.fetchrow(query, guild_id)
        self._settings[guild_id] = row
        return row

    def _update_cache(self, guild_id, **changes):
        current = self._settings.get(guild_id)
        data = {
            "antilink": bool(current["antilink"]) if current else False,
            "antispam": bool(current["antispam"]) if current else False,
        }
        data.update(changes)
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
        exempt_roles = (
            await settings.get_guild(pool, guild.id, "automod_exempt_roles", [])
            or []
        )
        exempt_channels = (
            await settings.get_guild(
                pool, guild.id, "automod_exempt_channels", []
            )
            or []
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
            "exempt_roles": list(exempt_roles),
            "exempt_channels": list(exempt_channels),
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

        exempt_channels = await settings.get_guild(
            pool, guild_id, "automod_exempt_channels", []
        )
        if exempt_channels:
            if message.channel.id in exempt_channels:
                return True
            parent_id = getattr(message.channel, "parent_id", None)
            if parent_id is not None and parent_id in exempt_channels:
                return True

        exempt_roles = await settings.get_guild(
            pool, guild_id, "automod_exempt_roles", []
        )
        if exempt_roles:
            role_ids = {role.id for role in message.author.roles}
            if role_ids.intersection(exempt_roles):
                return True
        return False

    async def _log_case(self, guild, target, action, reason):
        """Open a moderation case and funnel the embed to the mod-log."""

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

        guild = message.guild
        member = message.author
        action = await settings.get_guild(
            self.bot.db_pool, guild.id, "automod_action", DEFAULT_ACTION
        )
        if action not in VALID_ACTIONS:
            action = DEFAULT_ACTION

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
            # A real warn: bump the shared (monotonic) counter and escalate per
            # the guild's configurable warn policy, exactly like the warn
            # command (bump_warn + load_escalation_policy + the shared action
            # applier). Both surfaces stay in lockstep this way.
            new_count = await modactions.bump_warn(
                self.bot.db_pool, guild.id, member.id
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

        await self._log_case(guild, member, action, reason)

    def _prune_spam(self, now):
        """Drop spam-tracking entries whose newest timestamp is past the window."""
        self._spam = {
            k: ts
            for k, ts in self._spam.items()
            if ts and now - ts[-1] <= _SPAM_WINDOW
        }

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        if message.author.guild_permissions.manage_messages:
            return

        s = await self.get_settings(message.guild.id)
        antilink = bool(s["antilink"]) if s else False
        antispam = bool(s["antispam"]) if s else False
        antiinvite = bool(
            await settings.get_guild(
                self.bot.db_pool, message.guild.id, "antiinvite", False
            )
        )

        if not (antilink or antispam or antiinvite):
            return

        if await self._is_exempt(message):
            return

        if antiinvite and self.invite_re.search(message.content):
            await self._handle_violation(
                message,
                kind="invite",
                notice=_(
                    "{user} Discord invite links aren't allowed here."
                ).format(user=message.author.mention),
                reason="Posted a Discord invite link",
            )
            return

        if antilink and self.url_re.search(message.content):
            await self._handle_violation(
                message,
                kind="link",
                notice=_("{user} links aren't allowed here.").format(
                    user=message.author.mention
                ),
                reason="Posted a disallowed link",
            )
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
