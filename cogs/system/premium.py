"""Owner-only ``?premium`` controls: gift Yasuho+/Pack Confort by hand.

M3a+ of the monetisation plan (.claude/plans/monetisation/4-plan-retenu.md).
The owner asked, in as many words, to be able to grant premium to a friend's
server or to a user themself - before any store is open and whether or not a
SKU is ever configured. This is that surface: it writes ``premium_grants``
(tools/premium.py) and then the bot's live cache, in that order, so a grant
or a revoke is never visible in memory before it is durable.

Prefix-only by design, with no app_command anywhere: an owner-only control
surface has no business in the public slash picker, and a hybrid command's
subcommands would need their OWN checks on both invocation paths (see
tests/test_hybrid_gating_hygiene.py's docstring for why a group's check does
not protect them) for no benefit here. ``@commands.is_owner()`` on every
leaf is the real gate - ``cog_check`` is a second, cog-wide layer on top, the
same belt-and-suspenders cogs/system/admin.py and cogs/system/retention.py
already use.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from tools import premium
from tools.formats import format_dt, random_colour
from tools.i18n import _
from tools.time import ShortTime

NO_MENTIONS = discord.AllowedMentions.none()

# The two scope keywords ?premium list/check accept, mapped to
# tools.premium's scope_type strings.
_SCOPE_TYPE = {"server": "guild", "user": "user"}


def _parse_duration_and_reason(rest):
    """Split "[duration] [reason...]" into ``(expires_at_or_None, reason_or_None)``.

    ``rest`` is the raw text after the id argument. The FIRST whitespace-
    separated token is tried against :class:`tools.time.ShortTime` (the same
    parser ``?remind`` uses for "2d"/"12h"/...); if it parses, it is consumed
    as the duration and everything after it is the reason. If it does not
    parse (or ``rest`` is empty), there is no duration - the grant is
    permanent - and the WHOLE of ``rest`` is the reason. This mirrors
    reminders.py's own ShortTime-first, text-second split.
    """
    rest = (rest or "").strip()
    if not rest:
        return None, None
    token, _sep, remainder = rest.partition(" ")
    match = ShortTime.compiled.fullmatch(token)
    if match is not None and match.group(0):
        try:
            expires_at = ShortTime(token).dt
        except commands.BadArgument:
            return None, rest
        return expires_at, (remainder.strip() or None)
    return None, rest


def _embed(title):
    return discord.Embed(title=title, colour=random_colour())


# Discord's embed size limits this cog must respect BY CONSTRUCTION, not by
# luck: a description over 4096 characters, or one field value over 1024,
# is rejected by the API (discord.HTTPException on send), not by
# discord.Embed itself at construction time. `reason` is free text the owner
# types (bounded only by a Discord message's own ~2000-char limit) and
# `?premium list`/`?premium check` otherwise join an unbounded NUMBER of rows
# into one string, so either a long reason or enough active rows can cross
# either budget on their own. Both constants below leave headroom under the
# real ceiling rather than target it exactly.
_REASON_CLIP = 120
_DESCRIPTION_BUDGET = 4000
_FIELD_BUDGET = 1000


def _clip_reason(reason):
    """Shorten a free-text reason so one row can never dominate a line budget."""
    if not reason or len(reason) <= _REASON_CLIP:
        return reason
    return reason[:_REASON_CLIP] + "..."


def _join_within_budget(lines, budget):
    """Join ``lines`` with newlines, stopping before the result would cross
    ``budget`` characters and naming how many were left out instead - the
    same "+N more" shape ``?premium list``'s row-count cap already uses, so
    an overlong field value never reaches discord.py's HTTP call at all."""
    kept = []
    total = 0
    for index, line in enumerate(lines):
        total += len(line) + 1  # +1 for the joining newline
        if total > budget:
            remaining = len(lines) - index
            kept.append(_("{count} more not shown.").format(count=remaining))
            break
        kept.append(line)
    return "\n".join(kept)


class Premium(commands.Cog):
    """Owner-only gifting of Yasuho+ / Pack Confort, outside any store."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        # Second layer on top of @commands.is_owner() on every leaf below -
        # see the module docstring for why both exist.
        return await self.bot.is_owner(ctx.author)

    # -- group ---------------------------------------------------------

    @commands.group(name="premium", hidden=True, invoke_without_command=True)
    @commands.is_owner()
    async def premium_group(self, ctx):
        await ctx.send_help(ctx.command)

    # -- grant -----------------------------------------------------------

    @premium_group.group(name="grant", invoke_without_command=True)
    @commands.is_owner()
    async def premium_grant(self, ctx):
        await ctx.send_help(ctx.command)

    @premium_grant.command(name="server")
    @commands.is_owner()
    async def premium_grant_server(self, ctx, guild_id: int, *, rest: str = ""):
        """Gift Yasuho+ to a server: ?premium grant server <guild_id> [duration] [reason...]

        DELIBERATELY allows a guild_id the bot is not currently a member of
        (no ``self.bot.get_guild`` check): the plan's own "gift a friend's
        server... before any store is open" scenario includes pre-granting
        before the bot has even been invited there (a pre-launch/preorder
        gift) - the row simply sits unused until the guild exists from the
        bot's point of view, exactly like a grant surviving the bot leaving
        and later rejoining a guild already does. What IS rejected is a
        value that cannot be a real Discord id at all (zero or negative) -
        see the check right below.
        """
        if guild_id <= 0:
            await ctx.send(
                _("Server id must be a positive number, not `{value}`.").format(
                    value=guild_id
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        expires_at, reason = _parse_duration_and_reason(rest)
        grant_id = await premium.create_grant(
            self.bot.db_pool,
            product=premium.PRODUCT_YASUHO_PLUS,
            scope_type="guild",
            guild_id=guild_id,
            reason=reason,
            granted_by=ctx.author.id,
            expires_at=expires_at,
        )
        await self.bot.premium.refresh_grant_scope(
            self.bot.db_pool, "guild", guild_id=guild_id
        )
        until = format_dt(expires_at) if expires_at else _("permanent")
        await ctx.send(
            _(
                "Granted **Yasuho+** to server `{guild_id}` (grant #{grant_id}, "
                "{until})."
            ).format(guild_id=guild_id, grant_id=grant_id, until=until),
            allowed_mentions=NO_MENTIONS,
        )

    @premium_grant.command(name="user")
    @commands.is_owner()
    async def premium_grant_user(self, ctx, user_id: int, *, rest: str = ""):
        """Gift the Pack Confort to a user: ?premium grant user <user_id> [duration] [reason...]"""
        if user_id <= 0:
            await ctx.send(
                _("User id must be a positive number, not `{value}`.").format(
                    value=user_id
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        expires_at, reason = _parse_duration_and_reason(rest)
        grant_id = await premium.create_grant(
            self.bot.db_pool,
            product=premium.PRODUCT_COMFORT_PACK,
            scope_type="user",
            user_id=user_id,
            reason=reason,
            granted_by=ctx.author.id,
            expires_at=expires_at,
        )
        await self.bot.premium.refresh_grant_scope(
            self.bot.db_pool, "user", user_id=user_id
        )
        until = format_dt(expires_at) if expires_at else _("permanent")
        await ctx.send(
            _(
                "Granted the **Pack Confort** to user `{user_id}` (grant "
                "#{grant_id}, {until})."
            ).format(user_id=user_id, grant_id=grant_id, until=until),
            allowed_mentions=NO_MENTIONS,
        )

    # -- revoke ------------------------------------------------------------

    @premium_group.command(name="revoke")
    @commands.is_owner()
    async def premium_revoke(self, ctx, grant_id: int):
        """Revoke one grant by id: ?premium revoke <grant_id>"""
        # Looked up BEFORE the revoke, by its own id only - one row, not a
        # filtered list - purely to learn which scope's cache entry needs a
        # refresh after a successful revoke; the revoke itself does not need
        # it (revoke_grant takes the id alone).
        row = await self.bot.db_pool.fetchrow(
            "SELECT scope_type, guild_id, user_id FROM premium_grants "
            "WHERE id = $1",
            grant_id,
        )
        revoked = await premium.revoke_grant(
            self.bot.db_pool, grant_id, revoked_by=ctx.author.id
        )
        if not revoked:
            await ctx.send(
                _(
                    "No active grant #{grant_id} found (already revoked, "
                    "expired, or it never existed)."
                ).format(grant_id=grant_id),
                allowed_mentions=NO_MENTIONS,
            )
            return
        if row is not None:
            if row["scope_type"] == "guild":
                await self.bot.premium.refresh_grant_scope(
                    self.bot.db_pool, "guild", guild_id=row["guild_id"]
                )
            else:
                await self.bot.premium.refresh_grant_scope(
                    self.bot.db_pool, "user", user_id=row["user_id"]
                )
        await ctx.send(
            _("Revoked grant #{grant_id}.").format(grant_id=grant_id),
            allowed_mentions=NO_MENTIONS,
        )

    # -- list ----------------------------------------------------------

    @premium_group.command(name="list")
    @commands.is_owner()
    async def premium_list(self, ctx, scope: str = None, target_id: int = None):
        """List active grants: ?premium list [server|user <id>]"""
        if scope is not None and scope not in _SCOPE_TYPE:
            await ctx.send(
                _("Scope must be `server` or `user`, not `{scope}`.").format(
                    scope=scope
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        kwargs = {}
        if scope == "server":
            kwargs["scope_type"] = "guild"
            kwargs["guild_id"] = target_id
        elif scope == "user":
            kwargs["scope_type"] = "user"
            kwargs["user_id"] = target_id
        rows = await premium.list_grants(self.bot.db_pool, **kwargs)
        embed = _embed(_("Active premium grants"))
        if not rows:
            embed.description = _("No active grant matches.")
        else:
            lines = []
            for row in rows[:25]:
                scope_label = "server" if row["scope_type"] == "guild" else "user"
                scope_id = row["guild_id"] if row["scope_type"] == "guild" else row["user_id"]
                until = format_dt(row["expires_at"]) if row["expires_at"] else _("permanent")
                lines.append(
                    _("#{id} - {product} - {scope} `{scope_id}` - {until}").format(
                        id=row["id"],
                        product=row["product"],
                        scope=scope_label,
                        scope_id=scope_id,
                        until=until,
                    )
                )
            # Two independent caps, never traded against each other: at most
            # 25 ROWS shown (the footer below counts whatever that excludes),
            # and within those 25 the DESCRIPTION's own character budget
            # (_join_within_budget) - a `reason` is not shown here, so a long
            # one cannot inflate a list row, but 25 ids/dates alone are still
            # worth guarding on principle.
            embed.description = _join_within_budget(lines, _DESCRIPTION_BUDGET)
            if len(rows) > 25:
                embed.set_footer(
                    text=_("{count} more not shown.").format(count=len(rows) - 25)
                )
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # -- check -----------------------------------------------------------

    @premium_group.command(name="check")
    @commands.is_owner()
    async def premium_check(self, ctx, scope: str, target_id: int):
        """Show effective premium status and why: ?premium check server|user <id>"""
        if scope not in _SCOPE_TYPE:
            await ctx.send(
                _("Scope must be `server` or `user`, not `{scope}`.").format(
                    scope=scope
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        scope_type = _SCOPE_TYPE[scope]
        embed = _embed(_("Premium status"))

        if scope_type == "guild":
            is_premium = self.bot.premium.is_guild_premium(target_id)
            entitlement_rows = await self.bot.db_pool.fetch(
                "SELECT sku_id, ends_at FROM premium_entitlements "
                "WHERE guild_id = $1 AND deleted = FALSE",
                target_id,
            )
            sku = premium.YASUHO_PLUS_SKU
            grants = await premium.list_grants(
                self.bot.db_pool, scope_type="guild", guild_id=target_id
            )
        else:
            is_premium = self.bot.premium.has_comfort_pack(target_id)
            entitlement_rows = await self.bot.db_pool.fetch(
                "SELECT sku_id, ends_at FROM premium_entitlements "
                "WHERE user_id = $1 AND deleted = FALSE",
                target_id,
            )
            sku = premium.COMFORT_PACK_SKU
            grants = await premium.list_grants(
                self.bot.db_pool, scope_type="user", user_id=target_id
            )

        active_entitlements = [
            row
            for row in entitlement_rows
            if sku is not None
            and int(row["sku_id"]) == sku
            and premium.is_active(row)
        ]

        embed.add_field(
            name=_("Effective"),
            value=_("Premium") if is_premium else _("Free"),
            inline=False,
        )
        if active_entitlements:
            lines = []
            for row in active_entitlements:
                until = format_dt(row["ends_at"]) if row["ends_at"] else _("permanent")
                lines.append(_("Discord entitlement, {until}").format(until=until))
            embed.add_field(
                name=_("Entitlement"),
                value=_join_within_budget(lines, _FIELD_BUDGET),
                inline=False,
            )
        if grants:
            lines = []
            for row in grants:
                until = format_dt(row["expires_at"]) if row["expires_at"] else _("permanent")
                reason = _clip_reason(row["reason"]) or _("(no reason given)")
                lines.append(
                    _("#{id} - {until} - {reason}").format(
                        id=row["id"], until=until, reason=reason
                    )
                )
            embed.add_field(
                name=_("Owner grant(s)"),
                value=_join_within_budget(lines, _FIELD_BUDGET),
                inline=False,
            )
        if not active_entitlements and not grants:
            embed.add_field(name=_("Why"), value=_("Nothing active."), inline=False)

        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)


async def setup(bot):
    await bot.add_cog(Premium(bot))
