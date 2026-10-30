"""Temporary roles: `/temprole add|remove|list`.

A moderator hands a member a role for a set time; Yasuho takes it back on time.

STORAGE. One ``temp_roles`` row per (server, member, role) with ``expires_at``,
plus one ``timers`` row (event ``temprole_grant``) that only wakes the removal.
Both are written in ONE transaction under a per-guild advisory lock, which also
enforces the per-server limit, so there is nothing to reconcile at startup: a
grant either has both rows or neither. (The event is NOT ``temprole``: that is
the role menus' own timer, with its own handler.) Granting the same role again
replaces the timer and moves the expiry.

DELIVERY. The Reminder cog's dispatcher deletes the timer row before delivering
(at-most-once), then calls :meth:`TempRole.on_temprole_grant_timer_complete`. The
handler skips when the grant was extended meanwhile, drops silently when the
member left or the role is gone, and otherwise removes the role. Any failure
other than Forbidden (the hierarchy changed: logged and dropped) or NotFound
re-schedules the removal a few minutes later, up to 5 attempts; when the grant
row was erased meanwhile only the timer is re-written, never the row. Erasing the
user's data deletes the row but not the timer (which keeps the guild, user, role
and end time until it runs), so the role still comes off on time.

STARTUP SWEEP. ``cog_load`` re-arms, idempotently and in bounded batches, a timer
for every ``temp_roles`` row that has none (a dispatch that reached nobody
because the cog was unloaded, or a crash): rows already past their end fire
immediately.

TIERS. Free servers hold 5 active grants, Yasuho+ servers 100
(``GuildLimits.max_temp_roles``). A lapse never removes anything: the rows keep
expiring on schedule, only a NEW grant past the free limit is refused.

SCALE. A grant is one transaction (3 statements); an expiry is one claim, one
member lookup (cache, else one REST call) and one role removal. Nothing runs on a
timer of its own and no listener touches the hot path: at 1000+ guilds the load
is the number of grants actually made, each served by the single shared
dispatcher.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import json
import logging

import discord
from discord.ext import commands

from tools import modchecks, premium, premium_upsell, role_audit
from tools.formats import random_colour
from tools.i18n import _
from tools.paginator import Paginator, paginate_lines
from tools.time import ShortTime

log = logging.getLogger(__name__)

TIMER_EVENT = "temprole_grant"
MIN_SECONDS = 60
MAX_DAYS = 90
MAX_LIST = 200
_LOCK_NS = 0x54454D50  # 'TEMP'
_INT4_FOLD = 2147483647
_RETRY = datetime.timedelta(minutes=5)
_MAX_ATTEMPTS = 5
# The dispatcher fires a hair after expires_at; the claim tolerates that skew.
_SLACK = datetime.timedelta(seconds=5)

_KEY_SQL = "guild_id = $1 AND user_id = $2 AND role_id = $3"
_TIMER_KEY_SQL = (
    "event = '" + TIMER_EVENT + "' AND claimed_at IS NULL "
    "AND extra->>'guild_id' = $1 AND extra->>'user_id' = $2 AND extra->>'role_id' = $3"
)


async def grant(pool, guild_id, user_id, role_id, expires_at, limit, attempt=0):
    """Record a grant and its wake-up timer atomically.

    Returns ``(status, previous_expiry)``: status is ``"new"``, ``"extended"``
    (the grant existed; ``previous_expiry`` is its old end) or ``"limit"``.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1, $2)", _LOCK_NS, guild_id % _INT4_FOLD
            )
            previous = await conn.fetchval(
                f"SELECT expires_at FROM temp_roles WHERE {_KEY_SQL}",
                guild_id, user_id, role_id,
            )
            if previous is None:
                count = await conn.fetchval(
                    "SELECT count(*) FROM temp_roles WHERE guild_id = $1", guild_id
                )
                if count >= limit:
                    return "limit", None
            await conn.execute(
                "INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (guild_id, user_id, role_id) DO UPDATE SET expires_at = $4",
                guild_id, user_id, role_id, expires_at,
            )
            await conn.execute(
                f"DELETE FROM timers WHERE {_TIMER_KEY_SQL}",
                str(guild_id), str(user_id), str(role_id),
            )
            extra = {"guild_id": guild_id, "user_id": user_id, "role_id": role_id}
            if attempt:
                extra["attempt"] = attempt
            await conn.execute(
                "INSERT INTO timers(event, expires, extra) VALUES ($1, $2, $3::jsonb)",
                TIMER_EVENT, expires_at, json.dumps(extra),
            )
    return ("new" if previous is None else "extended"), previous


async def revoke(pool, guild_id, user_id, role_id):
    """Forget a grant and its pending timer. True when a grant existed."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"DELETE FROM temp_roles WHERE {_KEY_SQL} RETURNING 1",
                guild_id, user_id, role_id,
            )
            await conn.execute(
                f"DELETE FROM timers WHERE {_TIMER_KEY_SQL}",
                str(guild_id), str(user_id), str(role_id),
            )
    return row is not None


_SWEEP_SQL = (
    "INSERT INTO timers(event, expires, extra) "
    "SELECT $1, t.expires_at, jsonb_build_object("
    "'guild_id', t.guild_id, 'user_id', t.user_id, 'role_id', t.role_id) "
    "FROM temp_roles t WHERE NOT EXISTS (SELECT 1 FROM timers x WHERE x.event = $1 "
    "AND x.extra->>'guild_id' = t.guild_id::text "
    "AND x.extra->>'user_id' = t.user_id::text "
    "AND x.extra->>'role_id' = t.role_id::text) "
    "ORDER BY t.expires_at LIMIT 500"
)
_SWEEP_MAX_BATCHES = 20


async def sweep(pool):
    """Re-arm a timer for every grant row that has none. Returns how many."""
    total = 0
    for _batch in range(_SWEEP_MAX_BATCHES):
        status = await pool.execute(_SWEEP_SQL, TIMER_EVENT)
        added = int(status.rsplit(" ", 1)[-1])
        total += added
        if added < 500:
            break
    return total


def duration_error(expires_at, now):
    """A user-facing reason when the duration is out of range, else None."""
    seconds = (expires_at - now).total_seconds()
    if seconds < MIN_SECONDS:
        return _("The duration must be at least one minute.")
    if seconds > MAX_DAYS * 86400:
        return _("The duration can't be longer than {days} days.").format(days=MAX_DAYS)
    return None


def role_error(role):
    """Why ``role`` is @everyone or integration-managed (never handed out), else None."""
    if role.is_default() or role.managed:
        return _("I can't hand out that role: it is @everyone or managed by an integration.")
    return None


class TempRole(commands.Cog):
    """Give a role for a set time, then take it back."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        try:
            if await sweep(self.bot.db_pool):
                self._wake()
        except Exception:
            log.exception("TEMPROLE-FAIL reason=sweep")

    async def _member(self, guild, user_id):
        """The member, or None when they are proven absent. Raises on other errors."""
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    def _wake(self):
        reminder = self.bot.get_cog("Reminder")
        if reminder is not None:
            reminder._have_data.set()

    @commands.hybrid_group(name="temprole")
    @commands.guild_only()
    async def temprole(self, ctx):
        """Give a role for a limited time: add, remove, list."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @temprole.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        member="Who receives the role.",
        role="The role to give.",
        duration="How long it lasts, e.g. 1d or 2h30m.",
        reason="Why (shown in the audit log).",
    )
    async def temprole_add(
        self, ctx, member: discord.Member, role: discord.Role, duration: ShortTime,
        *, reason: str = None,
    ):
        """Give a member a role that expires by itself."""
        await ctx.defer()
        none = discord.AllowedMentions.none()
        err = role_error(role)
        if err is None and role_audit.refuse_dangerous_role(
            role, surface="temprole", guild_id=ctx.guild.id
        ):
            err = _("That role carries powerful permissions, so I won't hand it out automatically.")
        err = (
            err
            or modchecks.role_hierarchy_error(ctx, role)
            or duration_error(duration.dt, discord.utils.utcnow())
        )
        if err:
            return await ctx.send(err, allowed_mentions=none)
        if role in member.roles and not await self.bot.db_pool.fetchval(
            f"SELECT 1 FROM temp_roles WHERE {_KEY_SQL}", ctx.guild.id, member.id, role.id
        ):
            # No grant row: the member holds it for another reason, and expiry
            # would take it away.
            return await ctx.send(
                _("That member already has that role, so I won't make it temporary."),
                allowed_mentions=none,
            )
        limit = premium.resolve_guild_limits(self.bot, ctx.guild.id).max_temp_roles
        result, previous = await grant(
            self.bot.db_pool, ctx.guild.id, member.id, role.id, duration.dt, limit
        )
        if result == "limit":
            return await premium_upsell.refuse_in_context(
                self.bot,
                ctx,
                _("This server already has the maximum of {n} active temporary roles.").format(n=limit),
                limit_key="max_temp_roles",
                benefit=str(premium.GUILD_PREMIUM.max_temp_roles),
            )
        self._wake()
        audit = (_("Temporary role by {mod}").format(mod=ctx.author) + (f": {reason}" if reason else ""))[:512]
        try:
            await member.add_roles(role, reason=audit)
        except discord.HTTPException:
            if previous is None:
                await revoke(self.bot.db_pool, ctx.guild.id, member.id, role.id)
            else:
                # A failed extension keeps the original grant (and its end) intact.
                await grant(
                    self.bot.db_pool, ctx.guild.id, member.id, role.id, previous, 10**9
                )
            return await ctx.send(
                _("I couldn't give that role. Check my permissions and role order."),
                allowed_mentions=none,
            )
        await ctx.send(
            _("{member} has {role} until {time}.").format(
                member=member.mention, role=role.mention,
                time=discord.utils.format_dt(duration.dt, "F"),
            ),
            allowed_mentions=none,
        )

    @temprole.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(member="Who has the role.", role="The temporary role.")
    async def temprole_remove(self, ctx, member: discord.Member, role: discord.Role):
        """Take a temporary role back now."""
        await ctx.defer()
        none = discord.AllowedMentions.none()
        err = modchecks.role_hierarchy_error(ctx, role)
        if err:
            return await ctx.send(err, allowed_mentions=none)
        if not await revoke(self.bot.db_pool, ctx.guild.id, member.id, role.id):
            return await ctx.send(
                _("That member has no temporary grant of that role."), allowed_mentions=none
            )
        if role in member.roles:
            try:
                await member.remove_roles(role, reason=_("Temporary role ended by {mod}").format(mod=ctx.author))
            except discord.HTTPException:
                return await ctx.send(
                    _("I forgot the grant but couldn't remove the role. Check my permissions."),
                    allowed_mentions=none,
                )
        await ctx.send(
            _("{role} was taken back from {member}.").format(role=role.mention, member=member.mention),
            allowed_mentions=none,
        )

    @temprole.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def temprole_list(self, ctx):
        """List this server's active temporary roles."""
        await ctx.defer()
        rows = await self.bot.db_pool.fetch(
            "SELECT user_id, role_id, expires_at FROM temp_roles WHERE guild_id = $1 "
            "ORDER BY expires_at LIMIT $2",
            ctx.guild.id, MAX_LIST,
        )
        title = _("Temporary roles | {guild}").format(guild=ctx.guild.name)
        if not rows:
            return await ctx.send(
                embed=discord.Embed(
                    title=title, description=_("No active temporary roles."), colour=random_colour()
                )
            )
        lines = [
            f"<@{r['user_id']}> - <@&{r['role_id']}> - {discord.utils.format_dt(r['expires_at'], 'R')}"
            for r in rows
        ]
        await Paginator(paginate_lines(lines, title=title), author_id=ctx.author.id).start(ctx)

    # -- expiry ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_temprole_grant_timer_complete(self, extra):
        try:
            await self._expire(extra)
        except Exception:
            log.exception("TEMPROLE-FAIL reason=crash")

    async def _expire(self, extra):
        try:
            guild_id, user_id, role_id = (int(extra[k]) for k in ("guild_id", "user_id", "role_id"))
            attempt = int(extra.get("attempt") or 0)
        except (KeyError, TypeError, ValueError):
            return
        pool = self.bot.db_pool
        now = discord.utils.utcnow()
        claimed = None
        try:
            # The dispatcher already claimed the timer row. The grant row says
            # whether the grant was EXTENDED meanwhile (then this wake-up is stale).
            claimed = await pool.fetchrow(
                f"DELETE FROM temp_roles WHERE {_KEY_SQL} AND expires_at <= $4 RETURNING 1",
                guild_id, user_id, role_id, now + _SLACK,
            )
            if claimed is None and await pool.fetchval(
                f"SELECT 1 FROM temp_roles WHERE {_KEY_SQL}", guild_id, user_id, role_id
            ):
                return
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            role = guild.get_role(role_id)
            if role is None:
                return
            member = await self._member(guild, user_id)
            if member is not None and role in member.roles:
                await member.remove_roles(role, reason=_("Temporary role expired"))
        except discord.NotFound:
            return
        except discord.Forbidden:
            log.warning("TEMPROLE-FAIL guild=%s role=%s reason=forbidden", guild_id, role_id)
        except Exception:
            if attempt + 1 >= _MAX_ATTEMPTS:
                log.warning("TEMPROLE-FAIL guild=%s role=%s reason=gave_up", guild_id, role_id)
                return
            try:
                await self._requeue(pool, guild_id, user_id, role_id, now + _RETRY, attempt + 1, claimed is not None)
            except Exception:
                log.exception("TEMPROLE-FAIL guild=%s role=%s reason=requeue", guild_id, role_id)

    async def _requeue(self, pool, guild_id, user_id, role_id, when, attempt, with_row):
        """Put the removal back. Without ``with_row`` ONLY the timer is written:
        a record erased meanwhile (/mydata) must not come back."""
        if with_row:
            # No limit applies: a re-queue of a grant that already counted.
            await grant(pool, guild_id, user_id, role_id, when, 10**9, attempt)
        else:
            extra = {"guild_id": guild_id, "user_id": user_id, "role_id": role_id, "attempt": attempt}
            await pool.execute(
                "INSERT INTO timers(event, expires, extra) VALUES ($1, $2, $3::jsonb)",
                TIMER_EVENT, when, json.dumps(extra),
            )
        self._wake()


async def setup(bot):
    await bot.add_cog(TempRole(bot))
