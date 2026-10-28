"""The Birthdays cog: /birthday, the daily celebration and the member-leave cleanup.

PRIVACY. A birthday is stored per server (day and month, never a year) and only
announced where the member chose to share it. ``/birthday set`` refuses in a
server that did not enable the feature, so nothing is collected where it can never
be used, and a member who leaves loses their row there.

DAILY. One pending ``birthday_daily`` row per enabled guild in the shared timers
table. The dispatcher deletes it before dispatch (at-most-once) and the handler
schedules the next day BEFORE doing anything else, so a crash loses one day and
never the series. A fire later than LATE_WINDOW after its planned time only
reschedules. The Yasuho+ test happens at fire time (a lapse needs no clean-up).

HOT PATH. ``on_raw_member_remove`` costs one cached settings read for a guild that
never configured birthdays and one DELETE otherwise.

MENTIONS. Only today's members may be pinged: users=[...], never roles/everyone.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import datetime
import json
import logging

import discord
from discord.ext import commands

from . import store
from .panel import BirthdayPanel
from tools import i18n, premium, role_audit, settings
from tools.i18n import N_, _

log = logging.getLogger(__name__)

_STARTUP_CONCURRENCY = 5
MAX_MENTIONED = 40
LIST_CAP = 25
_ENABLED_SQL = (
    "SELECT guild_id FROM guild_settings WHERE settings @> "
    "'{\"birthdays\": {\"enabled\": true}}'::jsonb"
)
MONTHS = (
    N_("January"), N_("February"), N_("March"), N_("April"), N_("May"), N_("June"),
    N_("July"), N_("August"), N_("September"), N_("October"), N_("November"), N_("December"),
)


def month_name(month):
    return _(MONTHS[month - 1])


def render_message(guild, members, template=None):
    """The celebration text; ``template`` is the Yasuho+ custom message or None."""
    mentions = ", ".join(m.mention for m in members[:MAX_MENTIONED])
    if len(members) > MAX_MENTIONED:
        mentions += _(" and {count} more").format(count=len(members) - MAX_MENTIONED)
    text = template or _("Happy birthday, {users}! I hope your day is wonderful.")
    return text.replace("{users}", mentions).replace("{server}", guild.name)[:2000]


async def resolve_members(guild, user_ids):
    """id -> Member for the ids still in the guild: cache first, then gateway queries.

    The bot runs with chunk_guilds_at_startup=False, so get_member only sees cached
    members. ``limit`` must be passed with user_ids (the default of 5 would truncate
    a chunk). Members who left are simply not returned; a failed chunk is skipped.
    """
    found, missing = {}, []
    for uid in dict.fromkeys(user_ids):
        member = guild.get_member(uid)
        if member is not None:
            found[uid] = member
        else:
            missing.append(uid)
    for start in range(0, len(missing), store.MEMBER_CHUNK):
        chunk = missing[start : start + store.MEMBER_CHUNK]
        try:
            members = await guild.query_members(user_ids=chunk, limit=store.MEMBER_CHUNK, cache=True)
        except Exception:
            log.warning("BIRTHDAY-SKIP guild=%s reason=member_query", guild.id, exc_info=True)
            continue
        found.update({m.id: m for m in members})
    return found


class Birthdays(commands.Cog):
    """Members share their birthday, Yasuho celebrates it."""

    def __init__(self, bot):
        self.bot = bot

    async def config_for(self, guild_id):
        return store.normalise(
            await settings.get_guild(self.bot.db_pool, guild_id, store.SETTINGS_KEY, None)
        )

    # -- commands -----------------------------------------------------------

    @commands.hybrid_group(name="birthday", fallback="list")
    @commands.guild_only()
    async def birthday(self, ctx):
        """See the birthdays coming up in this server."""
        await ctx.defer(ephemeral=True)
        config = await self.config_for(ctx.guild.id)
        if not config["enabled"]:
            return await ctx.send(_("Birthdays are not set up in this server."), ephemeral=True)
        today = store.local_today(discord.utils.utcnow(), config["timezone"])
        months = {today.month, today.month % 12 + 1}
        upcoming = []
        for row in await store.in_months(self.bot.db_pool, ctx.guild.id, months):
            when = store.next_occurrence(today, row["month"], row["day"])
            if (when - today).days <= store.UPCOMING_DAYS:
                upcoming.append((when, row["user_id"]))
        upcoming.sort()
        coming = []  # absent members stay invisible: only resolved ones are listed
        for start in range(0, len(upcoming), store.MEMBER_CHUNK):
            batch = upcoming[start : start + store.MEMBER_CHUNK]
            members = await resolve_members(ctx.guild, [uid for _when, uid in batch])
            coming += [(when, members[uid]) for when, uid in batch if uid in members]
            if len(coming) >= LIST_CAP:
                break
        if not coming:
            return await ctx.send(
                _("No birthdays in the next {days} days.").format(days=store.UPCOMING_DAYS),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        lines = [
            _("**{day} {month}** - {name}").format(
                day=when.day, month=month_name(when.month),
                name=discord.utils.escape_markdown(discord.utils.escape_mentions(m.display_name)),
            )
            for when, m in coming[:LIST_CAP]
        ]
        embed = discord.Embed(
            title=_("Upcoming birthdays"), description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @birthday.command(name="set")
    @commands.guild_only()
    async def birthday_set(self, ctx, day: commands.Range[int, 1, 31], month: commands.Range[int, 1, 12]):
        """Share your birthday with this server (day and month only)."""
        await ctx.defer(ephemeral=True)
        if not store.valid_date(month, day):
            return await ctx.send(_("That date does not exist."), ephemeral=True)
        config = await self.config_for(ctx.guild.id)
        if not config["enabled"]:
            return await ctx.send(_("Birthdays are not set up in this server."), ephemeral=True)
        await store.upsert(self.bot.db_pool, ctx.guild.id, ctx.author.id, month, day)
        await ctx.send(
            _("Saved. I will celebrate your birthday on {day} {month} in this server.").format(
                day=day, month=month_name(month)
            ),
            ephemeral=True,
        )

    @birthday.command(name="remove")
    @commands.guild_only()
    async def birthday_remove(self, ctx):
        """Forget your birthday in this server."""
        await ctx.defer(ephemeral=True)
        removed = await store.remove(self.bot.db_pool, ctx.guild.id, ctx.author.id)
        await ctx.send(
            _("Your birthday is forgotten in this server.")
            if removed
            else _("You had not shared a birthday in this server."),
            ephemeral=True,
        )

    @commands.hybrid_command(name="birthdays")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def birthdays(self, ctx):
        """Set up birthday celebrations."""
        await ctx.defer(ephemeral=True)
        config = await self.config_for(ctx.guild.id)
        view = BirthdayPanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(embed=view.build_embed(), view=view, ephemeral=True)

    # -- leaving -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload):
        try:
            blob = await settings.get_guild(
                self.bot.db_pool, payload.guild_id, store.SETTINGS_KEY, None
            )
            if isinstance(blob, dict):  # never configured: zero queries
                await store.remove_member(self.bot.db_pool, payload.guild_id, payload.user.id)
        except Exception:
            log.exception("Birthdays member-remove cleanup failed")

    # -- timer -------------------------------------------------------------

    async def _write_timer(self, guild_id, config, *, replace, after=None):
        """Keep one pending daily row: ``replace`` moves it to the current config.

        The slot is the first one strictly after ``after``. A replacing write looks
        back LATE_WINDOW, so a slot that passed a moment ago still fires (the per-day
        claim in _daily prevents a second celebration).
        """
        now = discord.utils.utcnow()
        wanted = config["enabled"] and config["channel_id"]
        if after is None:
            after = now - store.LATE_WINDOW if replace else now
        planned = store.next_fire(after, config["hour"], config["timezone"])
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1, $2)", store.LOCK_NS, guild_id % 2147483647
                )
                if replace or not wanted:
                    # A row already due stays: the dispatcher still owes that day.
                    await conn.execute(
                        "DELETE FROM timers WHERE event = $1 AND claimed_at IS NULL "
                        "AND extra->>'guild_id' = $2 AND expires > $3",
                        store.TIMER_EVENT, str(guild_id), now,
                    )
                if wanted:
                    await conn.execute(
                        "INSERT INTO timers (event, expires, extra) "
                        "SELECT $1::text, $2, $3::jsonb WHERE NOT EXISTS ("
                        "SELECT 1 FROM timers WHERE event = $1 AND extra->>'guild_id' = $4)",
                        store.TIMER_EVENT, planned,
                        json.dumps({"guild_id": guild_id, "planned": planned.isoformat()}),
                        str(guild_id),
                    )
        reminder = self.bot.get_cog("Reminder")
        if reminder is not None:
            reminder._have_data.set()

    async def reconcile(self, guild_id):
        """After a panel save: move the pending row to the new config."""
        await self._write_timer(guild_id, await self.config_for(guild_id), replace=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Every READY: make sure each enabled guild has its pending row (idempotent)."""
        try:
            rows = await self.bot.db_pool.fetch(_ENABLED_SQL)
            gate = asyncio.Semaphore(_STARTUP_CONCURRENCY)

            async def one(guild_id):
                async with gate:
                    try:
                        await self._write_timer(
                            guild_id, await self.config_for(guild_id), replace=False
                        )
                    except Exception:
                        log.exception("BIRTHDAY-FAIL guild=%s reason=reconcile", guild_id)

            await asyncio.gather(
                *(one(r["guild_id"]) for r in rows if self.bot.get_guild(r["guild_id"]))
            )
        except Exception:
            log.exception("Birthdays startup reconcile failed")

    @commands.Cog.listener()
    async def on_birthday_daily_timer_complete(self, extra):
        try:
            reason = await self._daily(extra)
        except discord.HTTPException as exc:
            reason = "http_" + type(exc).__name__.lower()
        except Exception:
            log.exception("Birthday daily crashed")
            reason = "error"
        gid = extra.get("guild_id") if isinstance(extra, dict) else None
        if reason == "sent":
            log.info("BIRTHDAY-SENT guild=%s", gid)
        elif reason.startswith("skip_"):
            log.info("BIRTHDAY-SKIP guild=%s reason=%s", gid, reason[5:])
        else:
            log.warning("BIRTHDAY-FAIL guild=%s reason=%s", gid, reason)

    async def _daily(self, extra):
        """Return ``sent``, ``skip_<why>`` or a failure code."""
        try:
            guild_id = int(extra["guild_id"])
        except (KeyError, TypeError, ValueError):
            return "bad_row"
        now = discord.utils.utcnow()
        try:
            planned = discord.utils.parse_time(extra.get("planned")) or now
        except (TypeError, ValueError):
            planned = now
        late = now - planned > store.LATE_WINDOW
        config = None
        for attempt in (1, 2):
            try:
                config = await self.config_for(guild_id)
                if not (config["enabled"] and config["channel_id"]):
                    return "skip_disabled"
                # Next day first (strictly after this slot): a crash below costs this
                # day, never the series. A very late row restarts from now instead.
                await self._write_timer(
                    guild_id, config, replace=False, after=now if late else planned
                )
                break
            except Exception:
                log.warning("BIRTHDAY-FAIL guild=%s reason=reschedule attempt=%s", guild_id, attempt, exc_info=True)
                config = None
        if config is None:
            return "reschedule"
        if late:
            return "skip_late"
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.me is None:
            return "guild_unavailable"
        today = store.local_today(planned, config["timezone"])
        if not await store.claim_day(self.bot.db_pool, guild_id, today):
            return "skip_already"
        today_ids = await store.on_day(self.bot.db_pool, guild_id, *store.matches_today(today))
        yesterday_ids = await store.on_day(
            self.bot.db_pool, guild_id, *store.matches_today(today - datetime.timedelta(days=1))
        )
        role = guild.get_role(config["role_id"] or 0)
        holders = {m.id for m in role.members} if role is not None else set()
        members = await resolve_members(guild, [*today_ids, *yesterday_ids, *holders])
        celebrating = [members[i] for i in today_ids if i in members]
        await self._sync_role(guild, role, members, celebrating)
        if not celebrating:
            return "skip_nobody"
        channel = guild.get_channel(config["channel_id"])
        if channel is None:
            return "channel_missing"
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return "missing_permissions"
        template = config["message"] if premium.resolve_guild_limits(self.bot, guild_id).birthdays_custom else None
        locale = await i18n.resolve_guild_locale(self.bot, guild)
        with i18n.locale(locale):
            text = render_message(guild, celebrating, template)
        await channel.send(
            text, allowed_mentions=discord.AllowedMentions(
                users=celebrating[:MAX_MENTIONED], roles=False, everyone=False, replied_user=False
            ),
        )
        return "sent"

    async def _sync_role(self, guild, role, members, celebrating):
        """Take the role off everyone resolved who is not celebrating, give it to today's."""
        if store.role_problem(guild, role):
            return
        today_ids = {m.id for m in celebrating}
        for member in members.values():
            if member.id not in today_ids and role in member.roles:
                await self._set_role(member, role, give=False)
        for member in celebrating:
            if role not in member.roles:
                await self._set_role(member, role, give=True)

    async def strip_role(self, guild, role_id, config):
        """Panel: the role was disabled or changed, take it off today's holders."""
        role = guild.get_role(role_id or 0)
        if role is None or role.is_default() or role.managed:
            return
        today = store.local_today(discord.utils.utcnow(), config["timezone"])
        ids = await store.on_day(self.bot.db_pool, guild.id, *store.matches_today(today))
        members = await resolve_members(guild, [*ids, *(m.id for m in role.members)])
        for member in members.values():
            if role in member.roles:
                await self._set_role(member, role, give=False)

    @staticmethod
    async def _set_role(member, role, *, give):
        try:
            if give:
                # Defence in depth: a stored role is never handed out with powerful permissions.
                if role_audit.refuse_dangerous_role(role, surface="birthday", guild_id=member.guild.id):
                    return
                await member.add_roles(role, reason="Birthday")
            else:
                await member.remove_roles(role, reason="Birthday is over")
        except discord.HTTPException:
            log.info("BIRTHDAY-SKIP guild=%s reason=role_http", member.guild.id)
