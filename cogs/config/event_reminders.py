"""Discord scheduled-event reminders.

When a server has a scheduled event (Discord's own Events tab), Yasuho posts a
reminder in the configured channel before it starts: 15 minutes before for
every server, and with Yasuho+ any mix of 24 h / 1 h / 15 min plus an optional
role ping. ``/eventreminders`` opens the manager panel.

STORAGE. The config (enabled, channel, offsets, role) is the ``event_reminders``
key of the guild_settings JSONB blob, so it rides the cached tools.settings path
the dashboard already syncs.

SCHEDULING. One row per (event, offset) in the shared ``timers`` table
(event ``event_reminder``, extra = guild_id / event_id / offset_minutes /
start_time). The Reminder cog's dispatcher claims a row with DELETE ... RETURNING
before delivering and hands it to :meth:`on_event_reminder_timer_complete`, so a
reminder is at-most-once and a crash never repeats it. Rows are replaced per
event inside one transaction (DELETE then INSERT under a per-guild advisory
lock), so reconciling twice yields the same rows. At most three rows per
upcoming event and Discord caps a guild at 100 events.

The listener re-reads the LIVE event at fire time and drops the reminder when
the event is gone, no longer scheduled, moved (``start_time`` differs from the
row's) or the offset is not in force any more.

TIERS. Rows are written for the configured offsets plus the free 15 minutes, and
the Yasuho+ test happens at fire time, so a lapse needs no clean-up and a
renewal needs no re-save: custom offsets and the role are simply ignored (15
minutes, no ping) while the guild is not Yasuho+. Nothing is ever deleted.

HOT PATH. The scheduled-event listeners read the cached guild blob and do no
query for a guild that did not enable the feature. Startup runs one query for
the enabled guilds, then reconciles only those.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import datetime
import json
import logging

import discord
from discord.ext import commands

from tools import embed_creator, i18n, premium, premium_upsell, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.modchecks import everyone_can_read
from tools.snowflake import coerce_id
from tools.views import AuthorView

log = logging.getLogger(__name__)

SETTINGS_KEY = "event_reminders"
TIMER_EVENT = "event_reminder"
OFFSETS = (1440, 60, 15)  # minutes, longest first
FREE_OFFSETS = (15,)
MAX_UPCOMING = 100  # Discord's own per-guild cap on scheduled events
_STARTUP_CONCURRENCY = 5
_LOCK_NS = 0x45565452

_ENABLED_SQL = (
    "SELECT guild_id FROM guild_settings "
    "WHERE settings @> '{\"event_reminders\": {\"enabled\": true}}'::jsonb"
)


def normalise(blob):
    """A fresh, fully-typed config from the stored (or dashboard-written) blob."""
    blob = blob if isinstance(blob, dict) else {}
    offsets = blob.get("offsets")
    kept = sorted(
        {v for v in (offsets if isinstance(offsets, list) else []) if type(v) is int and v in OFFSETS},
        reverse=True,
    )
    return {
        "enabled": blob.get("enabled") is True,
        "channel_id": coerce_id(blob.get("channel_id")),
        "offsets": kept or list(FREE_OFFSETS),
        "role_id": coerce_id(blob.get("role_id")),
    }


def effective_offsets(config, custom_allowed):
    """The offsets in force: the chosen ones only while the guild has Yasuho+."""
    return tuple(config["offsets"]) if custom_allowed else FREE_OFFSETS


def scheduled_offsets(config):
    """Offsets that get a timer row: the chosen ones plus the free fallback."""
    return sorted({*config["offsets"], *FREE_OFFSETS}, reverse=True)


def offset_label(offset):
    if offset == 1440:
        return _("24 hours before")
    if offset == 60:
        return _("1 hour before")
    return _("15 minutes before")


def timer_rows(guild_id, event, config, now):
    """``(fire_at, extra_json)`` for every offset of ``event`` still in the future."""
    if event.status is not discord.EventStatus.scheduled:
        return []
    start = event.start_time
    rows = []
    for offset in scheduled_offsets(config):
        fire_at = start - datetime.timedelta(minutes=offset)
        if fire_at > now:
            extra = {
                "guild_id": guild_id,
                "event_id": event.id,
                "offset_minutes": offset,
                "start_time": start.isoformat(),
            }
            rows.append((fire_at, json.dumps(extra)))
    return rows


def render_message(name, start, where, url, role_id=None):
    """The reminder text: name, start time, location and link."""
    stamp = int(start.timestamp())
    lines = [
        _("**{name}** starts <t:{stamp}:R> (<t:{stamp}:F>).").format(
            name=discord.utils.escape_markdown(discord.utils.escape_mentions(name)),
            stamp=stamp,
        )
    ]
    if where:
        lines.append(
            _("Where: {where}").format(
                where=discord.utils.escape_markdown(discord.utils.escape_mentions(where))
            )
        )
    lines.append(url)
    text = "\n".join(lines)
    return (f"<@&{role_id}> " + text if role_id else text)[:2000]


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Reminder channel..."),
            min_values=1,
            max_values=1,
            default_values=[current] if current is not None else [],
            row=0,
        )

    async def callback(self, interaction):
        await self._owner.pick_channel(interaction, self.values[0].id)


class _OffsetSelect(discord.ui.Select):
    def __init__(self, panel, chosen):
        self._owner = panel
        super().__init__(
            placeholder=_("When to remind..."),
            min_values=1,
            max_values=len(OFFSETS),
            options=[
                discord.SelectOption(
                    label=offset_label(o), value=str(o), default=o in chosen
                )
                for o in OFFSETS
            ],
            row=2,
        )

    async def callback(self, interaction):
        self._owner.config["offsets"] = sorted((int(v) for v in self.values), reverse=True)
        await self._owner.save_and_rerender(interaction)


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            placeholder=_("Role to ping (optional)..."),
            min_values=0,
            max_values=1,
            default_values=[current] if current is not None else [],
            row=3,
        )

    async def callback(self, interaction):
        await self._owner.pick_role(interaction, self.values[0] if self.values else None)


class EventReminderPanel(AuthorView):
    """The ephemeral manager: channel, on/off, preview, Yasuho+ offsets and role."""

    def __init__(self, cog, guild, author_id, config, *, custom_open=False):
        super().__init__(author_id, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.config = config
        self.custom_open = custom_open

        toggle = discord.ui.Button(
            label=_("Disable") if config["enabled"] else _("Enable"),
            style=discord.ButtonStyle.danger
            if config["enabled"]
            else discord.ButtonStyle.success,
            row=1,
        )
        toggle.callback = self._toggle
        preview = discord.ui.Button(
            label=_("Preview"), style=discord.ButtonStyle.secondary, row=1
        )
        preview.callback = self._preview
        custom = discord.ui.Button(
            label=_("Customize"), style=discord.ButtonStyle.primary, row=1
        )
        custom.callback = self._customize
        items = [
            _ChannelSelect(self, guild.get_channel(config["channel_id"] or 0)),
            toggle,
            preview,
            custom,
        ]
        if custom_open and self.custom_allowed:
            items.append(_OffsetSelect(self, config["offsets"]))
            items.append(_RoleSelect(self, guild.get_role(config["role_id"] or 0)))
        for item in items:
            self.add_item(item)

    @property
    def custom_allowed(self):
        return premium.resolve_guild_limits(self.cog.bot, self.guild.id).event_reminders_custom

    async def interaction_check(self, interaction):
        # Authority is re-checked on every click, not only when the command
        # opened the panel: a manager who lost Manage Server loses the panel.
        if not await super().interaction_check(interaction):
            return False
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            await interaction.response.send_message(
                _("You need the Manage Server permission for this."), ephemeral=True
            )
            return False
        return True

    def build_embed(self):
        config = self.config
        allowed = self.custom_allowed
        embed = discord.Embed(
            title=_("Event reminders"),
            description=_(
                "Yasuho reminds your members before a scheduled event starts."
            ),
            colour=random_colour(),
        )
        embed.add_field(
            name=_("Status"),
            value=_("Enabled") if config["enabled"] else _("Disabled"),
        )
        channel_id = config["channel_id"]
        embed.add_field(
            name=_("Channel"),
            value=f"<#{channel_id}>" if channel_id else _("*Not set.*"),
        )
        embed.add_field(
            name=_("Reminders"),
            value="\n".join(offset_label(o) for o in effective_offsets(config, allowed)),
            inline=False,
        )
        role_id = config["role_id"] if allowed else None
        embed.add_field(
            name=_("Role to ping"),
            value=f"<@&{role_id}>" if role_id else _("*None.*"),
        )
        if not allowed:
            embed.set_footer(
                text=_("Yasuho+ unlocks the reminder times and a role ping.")
            )
        return embed

    async def save_and_rerender(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()  # the writes below must not eat the 3 s
        await settings.set_guild(
            self.cog.bot.db_pool, self.guild.id, SETTINGS_KEY, dict(self.config)
        )
        await self.cog.reconcile_guild(self.guild)
        new = EventReminderPanel(
            self.cog, self.guild, self.author_id, self.config, custom_open=self.custom_open
        )
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def pick_channel(self, interaction, channel_id):
        channel = self.guild.get_channel(channel_id)
        perms = channel.permissions_for(self.guild.me) if channel and self.guild.me else None
        if perms is None or not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(
                _(
                    "I can't send messages in that channel. Give me View Channel "
                    "and Send Messages there first."
                ),
                ephemeral=True,
            )
        self.config["channel_id"] = channel.id
        await self.save_and_rerender(interaction)

    async def pick_role(self, interaction, role):
        if role is not None:
            # Only a role the manager could ping by hand, and never @everyone.
            # The bot must be able to ping it too, or the ping silently does nothing.
            if role.is_default() or not role.mentionable and not (
                interaction.user.guild_permissions.mention_everyone
                and self.guild.me.guild_permissions.mention_everyone
            ):
                return await interaction.response.send_message(
                    _("You can't ping that role."), ephemeral=True
                )
        self.config["role_id"] = role.id if role is not None else None
        await self.save_and_rerender(interaction)

    async def _toggle(self, interaction):
        if not self.config["enabled"] and not self.config["channel_id"]:
            return await interaction.response.send_message(
                _("Pick a reminder channel first."), ephemeral=True
            )
        self.config["enabled"] = not self.config["enabled"]
        await self.save_and_rerender(interaction)

    async def _preview(self, interaction):
        start = discord.utils.utcnow() + datetime.timedelta(minutes=15)
        await interaction.response.send_message(
            render_message(
                _("Movie night"), start, _("Voice channel"), "https://discord.com/events"
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _customize(self, interaction):
        if self.custom_allowed:
            new = EventReminderPanel(
                self.cog, self.guild, self.author_id, self.config, custom_open=True
            )
            new.message = self.message
            self.stop()
            return await interaction.response.edit_message(
                embed=new.build_embed(), view=new
            )
        upsell = await premium_upsell.for_guild_refusal(
            self.cog.bot,
            limit_key="event_reminders_custom",
            guild_id=self.guild.id,
            person_id=interaction.user.id,
            is_admin=premium_upsell.invoker_is_admin(interaction.user),
            already_top_tier=premium_upsell.is_guild_already_top_tier(
                self.cog.bot, self.guild.id
            ),
            kind="unlock",
        )
        text = _("Custom reminder times and a role ping are a Yasuho+ feature.")
        kwargs = {"ephemeral": True}
        if upsell is not None:
            text += "\n" + upsell.line
            view = upsell.view()
            if view is not None:
                kwargs["view"] = view
        await interaction.response.send_message(text, **kwargs)


class EventReminders(commands.Cog):
    """Remind members before a Discord scheduled event starts."""

    def __init__(self, bot):
        self.bot = bot

    # -- scheduling ---------------------------------------------------------

    def _wake(self):
        reminder = self.bot.get_cog("Reminder")
        if reminder is not None:
            reminder._have_data.set()

    async def _write(self, guild_id, event_id, build):
        """Replace the pending rows of one event (or of the whole guild when
        ``event_id`` is None) atomically; idempotent.

        ``build(config, now)`` returns the rows; the config is read under the
        lock, so a listener holding a stale config cannot undo a panel save.
        """
        now = discord.utils.utcnow()
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _LOCK_NS, guild_id % 2147483647)
                config = await self._enabled_config(guild_id)
                rows = build(config, now) if config is not None else []
                if event_id is None:
                    # Rows already due stay for the dispatcher (it re-checks the
                    # event at fire time); only future rows are rebuilt. Same
                    # Python `now` as build(), so a row is never kept AND re-made.
                    await conn.execute(
                        "DELETE FROM timers WHERE event = $1 AND claimed_at IS NULL "
                        "AND extra->>'guild_id' = $2 AND expires > $3",
                        TIMER_EVENT, str(guild_id), now,
                    )
                else:
                    await conn.execute(
                        "DELETE FROM timers WHERE event = $1 AND claimed_at IS NULL "
                        "AND extra->>'guild_id' = $2 AND extra->>'event_id' = $3",
                        TIMER_EVENT, str(guild_id), str(event_id),
                    )
                if rows:
                    await conn.executemany(
                        "INSERT INTO timers(event, expires, extra) "
                        f"VALUES ('{TIMER_EVENT}', $1, $2::jsonb)",
                        rows,
                    )
        self._wake()
        return len(rows)

    async def reconcile_guild(self, guild):
        """Make the guild's pending rows match its upcoming events and config.

        Returns how many future reminders are scheduled.
        """

        def build(config, now):
            upcoming = [
                e for e in guild.scheduled_events if e.status is discord.EventStatus.scheduled
            ][:MAX_UPCOMING]
            return [row for e in upcoming for row in timer_rows(guild.id, e, config, now)]

        return await self._write(guild.id, None, build)

    async def _enabled_config(self, guild_id):
        """The normalised config when the feature is on, else None (no query once cached)."""
        blob = await settings.get_guild(self.bot.db_pool, guild_id, SETTINGS_KEY, None)
        if not isinstance(blob, dict) or blob.get("enabled") is not True:
            return None
        config = normalise(blob)
        return config if config["channel_id"] else None

    async def _sync_event(self, event, *, clear=False):
        try:
            config = await self._enabled_config(event.guild_id)
            if config is None:
                return
            await self._write(
                event.guild_id, event.id,
                lambda config, now: [] if clear else timer_rows(event.guild_id, event, config, now),
            )
        except Exception:
            log.exception("EVENTREMIND-FAIL guild=%s event=%s reason=schedule", event.guild_id, event.id)

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event):
        await self._sync_event(event)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        scheduled = discord.EventStatus.scheduled
        if (
            before.start_time == after.start_time
            and before.status is scheduled
            and after.status is scheduled
        ):
            return  # a rename or description edit changes nothing we scheduled
        await self._sync_event(after, clear=after.status is not scheduled)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        await self._sync_event(event, clear=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        # A re-invite within the retention grace keeps the config.
        try:
            if await self._enabled_config(guild.id) is not None:
                await self.reconcile_guild(guild)
        except Exception:
            log.exception("EVENTREMIND-FAIL guild=%s event=- reason=reconcile", guild.id)

    @commands.Cog.listener()
    async def on_ready(self):
        """Pick up events created while the bot was offline or reconnecting
        (idempotent, so every READY reconciles)."""
        try:
            rows = await self.bot.db_pool.fetch(_ENABLED_SQL)
            gate = asyncio.Semaphore(_STARTUP_CONCURRENCY)

            async def one(guild):
                async with gate:
                    try:
                        await self.reconcile_guild(guild)
                    except Exception:
                        log.exception("EVENTREMIND-FAIL guild=%s event=- reason=reconcile", guild.id)

            guilds = (self.bot.get_guild(r["guild_id"]) for r in rows)
            await asyncio.gather(*(one(g) for g in guilds if g is not None))
        except Exception:
            log.exception("Event reminders startup reconcile failed")

    # -- delivery -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_event_reminder_timer_complete(self, extra):
        guild_id = extra.get("guild_id")
        event_id = extra.get("event_id")
        try:
            reason = await self._deliver(extra)
        except discord.HTTPException as exc:
            reason = "http_" + type(exc).__name__.lower()
        except Exception:
            log.exception("Event reminder delivery crashed")
            reason = "error"
        if reason == "sent":
            log.info("EVENTREMIND-SENT guild=%s event=%s", guild_id, event_id)
        elif reason.startswith("skip_"):
            log.info("EVENTREMIND-SKIP guild=%s event=%s reason=%s", guild_id, event_id, reason[5:])
        else:
            log.warning("EVENTREMIND-FAIL guild=%s event=%s reason=%s", guild_id, event_id, reason)

    async def _deliver(self, extra):
        """Return ``sent``, ``skip_<why>`` or a failure code. Never raises on bad data."""
        try:
            guild_id = int(extra["guild_id"])
            event_id = int(extra["event_id"])
            offset = int(extra["offset_minutes"])
            start_time = str(extra["start_time"])
        except (KeyError, TypeError, ValueError):
            return "bad_row"
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.me is None:
            return "guild_unavailable"
        config = await self._enabled_config(guild_id)
        if config is None:
            return "skip_disabled"
        event = guild.get_scheduled_event(event_id)
        if event is None:
            return "skip_event_gone"
        if event.status is not discord.EventStatus.scheduled:
            return "skip_not_scheduled"
        if event.start_time.isoformat() != start_time:
            return "skip_stale"
        if event.start_time <= discord.utils.utcnow():
            return "skip_started"
        custom = premium.resolve_guild_limits(self.bot, guild_id).event_reminders_custom
        if offset not in effective_offsets(config, custom):
            return "skip_offset_off"
        channel = guild.get_channel(config["channel_id"])
        if channel is None:
            return "channel_missing"
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return "missing_permissions"
        role = guild.get_role(config["role_id"] or 0) if custom else None
        if role is not None and role.is_default():
            role = None
        if (
            event.channel is not None
            and not everyone_can_read(event.channel, unknown=False)
            and everyone_can_read(channel, unknown=True)
        ):
            return "skip_private"  # a staff-only event must not be announced publicly
        where = event.channel.mention if event.channel is not None else event.location
        locale = await i18n.resolve_guild_locale(self.bot, guild)
        with i18n.locale(locale):
            text = render_message(
                event.name, event.start_time, where, event.url, role.id if role else None
            )
        mentions = discord.AllowedMentions(
            everyone=False, users=False,
            roles=[discord.Object(id=role.id)] if role else False,
            replied_user=False,
        )
        await channel.send(text, allowed_mentions=mentions)
        return "sent"

    # -- command ------------------------------------------------------------

    @commands.hybrid_command(name="eventreminders")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def eventreminders(self, ctx):
        """Remind members before a scheduled event starts."""
        config = normalise(
            await settings.get_guild(self.bot.db_pool, ctx.guild.id, SETTINGS_KEY, None)
        )
        view = EventReminderPanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(EventReminders(bot))
