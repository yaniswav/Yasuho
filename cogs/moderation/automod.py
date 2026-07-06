import datetime
import logging
import re
import time

import discord
from discord.ext import commands

from tools import db, interactions, modactions, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Friendly names used both in the action <Select> and the case action string.
ACTION_CHOICES = [
    ("delete", "Delete message", "🗑️"),
    ("warn", "Warn", "⚠️"),
    ("mute", "Mute (10m timeout)", "🔇"),
    ("kick", "Kick", "👢"),
]
VALID_ACTIONS = {value for value, _label, _emoji in ACTION_CHOICES}

# Anti-spam sliding window: keep the last _SPAM_WINDOW seconds of a member's
# message timestamps and trip when more than _SPAM_THRESHOLD land inside it.
# _SPAM_SWEEP_AT bounds the tracking map: once it holds more keys than this, the
# next hit drops every entry that has gone quiet past the window (so a one-off
# talker's key cannot linger forever).
_SPAM_WINDOW = 5
_SPAM_THRESHOLD = 5
_SPAM_SWEEP_AT = 1000


def _action_options(current):
    """Options for the panel's action <Select>, current value pre-selected."""

    return [
        discord.SelectOption(
            label=label, value=value, emoji=emoji, default=value == current
        )
        for value, label, emoji in ACTION_CHOICES
    ]


# ----------------------------------------------------------------------
# Interactive panel components (discord.ui)
# ----------------------------------------------------------------------
class _AutoModToggle(discord.ui.Button):
    """A single on/off rule button; green when on, greyed-out when unavailable."""

    def __init__(self, panel, key, label, *, native, row):
        self.panel = panel
        self.key = key
        self.native = native
        self.base_label = label
        super().__init__(label=label, row=row)
        self.refresh()

    def refresh(self):
        state = self.panel.state.get(self.key)
        if state is None:
            # Native rule we can't read/manage (missing permission) -> disabled.
            self.disabled = True
            self.label = _("{label} (N/A)").format(label=self.base_label)
            self.style = discord.ButtonStyle.secondary
        else:
            self.disabled = False
            self.label = self.base_label
            self.style = (
                discord.ButtonStyle.success
                if state
                else discord.ButtonStyle.secondary
            )

    async def callback(self, interaction):
        await self.panel.toggle(interaction, self.key, self.native)


class _ActionSelect(discord.ui.Select):
    """Choose what happens to a member who trips a custom filter."""

    def __init__(self, panel, row):
        self.panel = panel
        super().__init__(
            placeholder=_("Action on a custom violation..."),
            min_values=1,
            max_values=1,
            options=_action_options(panel.state["action"]),
            row=row,
        )

    def refresh(self):
        self.options = _action_options(self.panel.state["action"])

    async def callback(self, interaction):
        await self.panel.set_action(interaction, self.values[0])


class _ExemptRoleSelect(discord.ui.RoleSelect):
    """Roles whose members bypass the custom filters."""

    def __init__(self, panel, row, defaults):
        self.panel = panel
        super().__init__(
            placeholder=_("Exempt roles (select to replace)..."),
            min_values=0,
            max_values=25,
            default_values=defaults,
            row=row,
        )

    async def callback(self, interaction):
        await self.panel.set_exempt(
            interaction, "roles", [r.id for r in self.values]
        )


class _ExemptChannelSelect(discord.ui.ChannelSelect):
    """Channels in which the custom filters are not enforced."""

    def __init__(self, panel, row, defaults):
        self.panel = panel
        super().__init__(
            placeholder=_("Exempt channels (select to replace)..."),
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.forum,
            ],
            min_values=0,
            max_values=25,
            default_values=defaults,
            row=row,
        )

    async def callback(self, interaction):
        await self.panel.set_exempt(
            interaction, "channels", [c.id for c in self.values]
        )


class AutoModPanel(AuthorView):
    """Author-restricted control panel for every custom + native AutoMod rule."""

    def __init__(self, cog, guild, author_id, state, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.state = state

        self._toggles = [
            _AutoModToggle(self, "link", _("Anti-link"), native=False, row=0),
            _AutoModToggle(self, "invite", _("Anti-invite"), native=False, row=0),
            _AutoModToggle(self, "spam", _("Anti-spam"), native=False, row=0),
            _AutoModToggle(self, "kw", _("Native: Keyword"), native=True, row=1),
            _AutoModToggle(self, "nspam", _("Native: Spam"), native=True, row=1),
            _AutoModToggle(self, "nmention", _("Native: Mentions"), native=True, row=1),
        ]
        for toggle in self._toggles:
            self.add_item(toggle)

        self._action_select = _ActionSelect(self, row=2)
        self.add_item(self._action_select)

        role_defaults = [
            r
            for r in (guild.get_role(i) for i in state["exempt_roles"])
            if r is not None
        ]
        channel_defaults = [
            c
            for c in (guild.get_channel(i) for i in state["exempt_channels"])
            if c is not None
        ]
        self.add_item(_ExemptRoleSelect(self, row=3, defaults=role_defaults))
        self.add_item(
            _ExemptChannelSelect(self, row=4, defaults=channel_defaults)
        )

    # -- rendering ------------------------------------------------------
    def _refresh_components(self):
        for toggle in self._toggles:
            toggle.refresh()
        self._action_select.refresh()

    def embed(self):
        def mark(value):
            if value is None:
                return _("⚪ Unavailable")
            return _("🟢 Enabled") if value else _("🔴 Disabled")

        embed = discord.Embed(
            title=_("AutoMod control panel"),
            description=_(
                "Custom filters are enforced by Yasuho on every message. "
                "Native filters are Discord's own AutoMod, blocked before a "
                "message is ever posted."
            ),
            colour=random_colour(),
        )
        embed.add_field(
            name=_("Custom filters (Yasuho)"),
            value=_(
                "Anti-link: {link}\n"
                "Anti-invite: {invite}\n"
                "Anti-spam: {spam}"
            ).format(
                link=mark(self.state["link"]),
                invite=mark(self.state["invite"]),
                spam=mark(self.state["spam"]),
            ),
            inline=True,
        )
        embed.add_field(
            name=_("Native filters (Discord)"),
            value=_(
                "Keyword preset: {kw}\n"
                "Spam: {nspam}\n"
                "Mention spam: {nmention}"
            ).format(
                kw=mark(self.state["kw"]),
                nspam=mark(self.state["nspam"]),
                nmention=mark(self.state["nmention"]),
            ),
            inline=True,
        )
        embed.add_field(
            name=_("Action on custom violation"),
            value=self.state["action"].title(),
            inline=False,
        )

        roles = self.state["exempt_roles"]
        channels = self.state["exempt_channels"]
        embed.add_field(
            name=_("Exempt roles"),
            value=(
                ", ".join(f"<@&{r}>" for r in roles) if roles else _("None")
            ),
            inline=False,
        )
        embed.add_field(
            name=_("Exempt channels"),
            value=(
                ", ".join(f"<#{c}>" for c in channels) if channels else _("None")
            ),
            inline=False,
        )

        if any(self.state[k] is None for k in ("kw", "nspam", "nmention")):
            embed.set_footer(
                text=_("Native rules need the bot to have Manage Server.")
            )
        return embed

    async def _refresh_message(self, interaction):
        self._refresh_components()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _safe_fail(self, interaction):
        await interactions.notify_failure(
            interaction, _("Something went wrong updating the panel.")
        )

    # -- callbacks ------------------------------------------------------
    async def toggle(self, interaction, key, native):
        try:
            target = not bool(self.state.get(key))
            if native:
                ok, new_state = await self.cog.set_native_rule(
                    self.guild, key, target
                )
                if not ok:
                    return await interaction.response.send_message(
                        _(
                            "I couldn't change that rule. Discord's built-in "
                            "AutoMod needs the bot to have the **Manage Server** "
                            "permission."
                        ),
                        ephemeral=True,
                    )
                self.state[key] = new_state
            else:
                await self.cog.set_custom_rule(self.guild.id, key, target)
                self.state[key] = target
            await self._refresh_message(interaction)
        except Exception:
            log.exception("AutoMod panel toggle failed")
            await self._safe_fail(interaction)

    async def set_action(self, interaction, value):
        try:
            if value not in VALID_ACTIONS:
                value = "delete"
            await settings.set_guild(
                self.cog.bot.db_pool, self.guild.id, "automod_action", value
            )
            self.state["action"] = value
            await self._refresh_message(interaction)
        except Exception:
            log.exception("AutoMod panel action update failed")
            await self._safe_fail(interaction)

    async def set_exempt(self, interaction, kind, ids):
        try:
            if kind == "roles":
                key, state_key = "automod_exempt_roles", "exempt_roles"
            else:
                key, state_key = "automod_exempt_channels", "exempt_channels"
            await settings.set_guild(
                self.cog.bot.db_pool, self.guild.id, key, ids
            )
            self.state[state_key] = ids
            await self._refresh_message(interaction)
        except Exception:
            log.exception("AutoMod panel exemption update failed")
            await self._safe_fail(interaction)


class AutoMod(commands.Cog):
    """Hybrid auto-moderation: custom message scanning + Discord's native AutoMod."""

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

    @commands.hybrid_group(name="automod")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod(self, ctx):
        """Automatic moderation related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @automod.command(name="antilink")
    async def automod_antilink(self, ctx, mode: bool):
        """Enable or disable link filtering for this guild."""

        await self.set_custom_rule(ctx.guild.id, "link", mode)
        embed = discord.Embed(title=_("Auto-mod"), colour=random_colour())
        embed.add_field(
            name=_("Anti-link"), value=_("Enabled") if mode else _("Disabled")
        )
        await ctx.send(embed=embed)

    @automod.command(name="antiinvite")
    async def automod_antiinvite(self, ctx, mode: bool):
        """Enable or disable Discord-invite filtering for this guild."""

        await self.set_custom_rule(ctx.guild.id, "invite", mode)
        embed = discord.Embed(title=_("Auto-mod"), colour=random_colour())
        embed.add_field(
            name=_("Anti-invite"), value=_("Enabled") if mode else _("Disabled")
        )
        await ctx.send(embed=embed)

    @automod.command(name="antispam")
    async def automod_antispam(self, ctx, mode: bool):
        """Enable or disable spam filtering for this guild."""

        await self.set_custom_rule(ctx.guild.id, "spam", mode)
        embed = discord.Embed(title=_("Auto-mod"), colour=random_colour())
        embed.add_field(
            name=_("Anti-spam"), value=_("Enabled") if mode else _("Disabled")
        )
        await ctx.send(embed=embed)

    @automod.command(name="status")
    async def automod_status(self, ctx):
        """Show the current auto-mod settings for this guild."""

        s = await self.get_settings(ctx.guild.id)
        antilink = bool(s["antilink"]) if s else False
        antispam = bool(s["antispam"]) if s else False

        embed = discord.Embed(
            title=_("Auto-mod status"), colour=random_colour()
        )
        embed.add_field(
            name=_("Anti-link"), value=_("Enabled") if antilink else _("Disabled")
        )
        embed.add_field(
            name=_("Anti-spam"), value=_("Enabled") if antispam else _("Disabled")
        )
        await ctx.send(embed=embed)

    @automod.command(name="panel")
    async def automod_panel(self, ctx):
        """Open the interactive AutoMod control panel."""

        state = await self._panel_state(ctx.guild)
        view = AutoModPanel(self, ctx.guild, ctx.author.id, state)
        view.message = await ctx.send(embed=view.embed(), view=view)

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
        """Persist a custom-rule toggle (anti-link / anti-invite / anti-spam)."""

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
        action = await settings.get_guild(pool, guild.id, "automod_action", "delete")
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
            "action": action if action in VALID_ACTIONS else "delete",
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
            self.bot.db_pool, guild.id, "automod_action", "delete"
        )
        if action not in VALID_ACTIONS:
            action = "delete"

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
                    "{user} Discord invite links are not allowed here."
                ).format(user=message.author.mention),
                reason="Posted a Discord invite link",
            )
            return

        if antilink and self.url_re.search(message.content):
            await self._handle_violation(
                message,
                kind="link",
                notice=_("{user} links are not allowed here.").format(
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
                    notice=_("{user} stop spamming.").format(
                        user=message.author.mention
                    ),
                    reason="Spamming messages",
                )


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
