"""Level-up role rewards (leveling L2) - MEE6's number-one paywalled feature.

An admin sets up rules via the ``/levelrewards`` group: "reach level N, get
role R". A member who levels up (dispatched from the Leveling cog's on_message
grant path, tools/leveling.level_up_between) is reconciled against those rules
here: :meth:`LevelRewards.grant_for_levelup` computes which roles to add and
(in 'replace' mode) remove, applies what it actually can, and hands the
Leveling cog back the list of roles it granted for the announce suffix.

Cross-cog seam, matching the existing house pattern (see rolemenus.py's
``interaction.client.get_cog("Reminder")``): the Leveling cog looks up this cog
by name (``bot.get_cog("LevelRewards")``) and calls it directly, guarded by a
try/except so a rewards failure never breaks the level-up itself. There is no
event dispatch and no persistent view to re-register on restart - this cog
owns exactly one table and reacts only when called.

Scale: role grants happen on level-up only, never per message (leveling.py's
on_message hot path never imports or touches this module). A guild's rule set
is capped at 25 rows (tools.level_rewards.MAX_REWARDS_PER_GUILD) and read
fresh from the DB on each level-up rather than cached - level-ups are already
the rare branch of an already-gated hot path, so a bounded, tiny read there
costs less than the cache invalidation it would take to avoid it (YAGNI).

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord.ext import commands

from tools import level_rewards
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)


def _assignable(role, guild):
    """Whether Yasuho could actually add/remove this role right now.

    Mirrors the hierarchy check every other role-granting cog repeats
    (buttonroles.BuilderView._can_assign, rolemenus's inline ``role >= bot_top``):
    not @everyone, not managed by an integration, and strictly below the bot's
    top role. Used both as a non-blocking ADD-time warning and to skip a role
    at grant time (see :meth:`LevelRewards.grant_for_levelup`).
    """
    me = guild.me
    return (
        me is not None
        and not role.is_default()
        and not role.managed
        and role < me.top_role
    )


# ----------------------------------------------------------------------
# Admin UX: "remove a rule" picker
# ----------------------------------------------------------------------
class _RemoveRewardSelect(discord.ui.Select):
    """Lists every configured rule so the admin can pick one to delete.

    One option per rule; the cap (MAX_REWARDS_PER_GUILD == 25) keeps this
    within Discord's 25-option select limit with no truncation needed.
    """

    def __init__(self, cog, guild, rules):
        self.cog = cog
        self.guild = guild
        options = []
        for lvl, role_id in sorted(rules):
            role = guild.get_role(role_id)
            desc = role.name if role is not None else _("Unknown role (deleted)")
            options.append(
                discord.SelectOption(
                    label=_("Level {level}").format(level=lvl)[:100],
                    value=f"{lvl}:{role_id}",
                    description=desc[:100],
                )
            )
        super().__init__(
            placeholder=_("Pick a reward rule to remove..."),
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            lvl_str, role_id_str = self.values[0].split(":")
            level, role_id = int(lvl_str), int(role_id_str)
            await self.cog.bot.db_pool.execute(
                "DELETE FROM level_rewards WHERE guild_id = $1 AND level = $2 "
                "AND role_id = $3;",
                self.guild.id,
                level,
                role_id,
            )
            role = self.guild.get_role(role_id)
            role_text = role.mention if role is not None else f"`{role_id}`"
            await interaction.response.edit_message(
                content=_("Removed the level {level} reward ({role}).").format(
                    level=level, role=role_text
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Level-reward remove select failed")
            await interaction.response.edit_message(
                content=_("Something went wrong."), view=None
            )
        finally:
            # Terminal action: the message no longer carries a view, so stop the
            # timer too. Otherwise AuthorView.on_timeout would fire later and
            # re-attach the (disabled) select to the confirmation message.
            self._owner.stop()


class _RemoveRewardView(AuthorView):
    def __init__(self, cog, guild, author_id, rules, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        select = _RemoveRewardSelect(cog, guild, rules)
        # The select stops this view once a rule is picked (terminal action); it
        # holds the owning view as ``_owner`` per the house convention (never the
        # banned ``self.view``).
        select._owner = self
        self.add_item(select)


# ----------------------------------------------------------------------
# CV2 "list" card, grouped by level
# ----------------------------------------------------------------------
class LevelRewardsListView(discord.ui.LayoutView):
    """Single-page Components V2 card: rules grouped by level, plus the mode."""

    def __init__(self, guild, rules, mode, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, rules, mode)

    def _build(self, guild, rules, mode):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("Level rewards | {guild}").format(guild=guild.name)
            )
        )
        mode_text = (
            _("Stack - members keep every role they've earned")
            if mode != level_rewards.REPLACE
            else _("Replace - members only keep the highest tier they've earned")
        )
        container.add_item(
            discord.ui.TextDisplay(_("Mode: **{mode}**").format(mode=mode_text))
        )
        container.add_item(discord.ui.Separator())

        if not rules:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "No level rewards configured yet. Use `/levelrewards "
                        "add` to create one."
                    )
                )
            )
        else:
            grouped = level_rewards.group_by_level(rules)
            lines = [
                _("**Level {level}** -> {roles}").format(
                    level=lvl,
                    roles=" ".join(f"<@&{rid}>" for rid in grouped[lvl]),
                )
                for lvl in sorted(grouped)
            ]
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        self.add_item(container)


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class LevelRewards(commands.Cog):
    """Automatic role grants on level-up, plus the /levelrewards admin group."""

    def __init__(self, bot):
        self.bot = bot

    # -- grant engine (called by the Leveling cog on every level-up) -------
    async def grant_for_levelup(self, guild, member, old_level, new_level):
        """Reconcile ``member``'s reward roles for a level-up. Returns the roles
        actually added (``list[discord.Role]``), for the Leveling cog's announce
        suffix. Never raises - any DB/HTTP hiccup is logged and swallowed so a
        rewards failure can never break the level-up itself (the Leveling cog
        also wraps this call, but the guarantee holds here independently).
        """
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT level, role_id FROM level_rewards WHERE guild_id = $1;",
                guild.id,
            )
            if not rows:
                return []

            mode = (
                await self.bot.db_pool.fetchval(
                    "SELECT rewards_mode FROM level_config WHERE guild_id = $1;",
                    guild.id,
                )
                or level_rewards.DEFAULT_MODE
            )
            rules = [(row["level"], row["role_id"]) for row in rows]
            held = {r.id for r in member.roles}
            to_add, to_remove = level_rewards.decide_role_changes(
                rules, mode, old_level, new_level, held
            )
            if not to_add and not to_remove:
                return []

            granted, _removed = await self._apply_role_changes(
                guild, member, to_add, to_remove
            )
            return granted
        except Exception:
            log.exception(
                "Level-reward grant failed for %s in guild %s", member.id, guild.id
            )
            return []

    async def reconcile_for_level(self, guild, member, level):
        """Recompute ``member``'s reward roles after an admin XP edit dropped
        them to ``level`` (leveling L5 - the level-DOWN case). Returns
        ``(added, removed)`` role lists.

        In stack mode this is a no-op in BOTH lists: earned roles are KEPT even
        when an admin removes XP (the documented convention - see
        tools.level_rewards.reconcile_to_level). In replace mode the tier is
        recomputed: reward roles above the new level are removed and the new
        tier's role(s) (re)added. Never raises - a missing/failing lookup is
        logged and swallowed so an admin XP edit is never broken by a reward
        hiccup (the Leveling cog also wraps this call). The UP case still routes
        through :meth:`grant_for_levelup`, never here.
        """
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT level, role_id FROM level_rewards WHERE guild_id = $1;",
                guild.id,
            )
            if not rows:
                return [], []
            mode = await self._fetch_mode(guild.id)
            rules = [(row["level"], row["role_id"]) for row in rows]
            held = {r.id for r in member.roles}
            to_add, to_remove = level_rewards.reconcile_to_level(
                rules, mode, level, held
            )
            if not to_add and not to_remove:
                return [], []
            return await self._apply_role_changes(guild, member, to_add, to_remove)
        except Exception:
            log.exception(
                "Level-reward reconcile failed for %s in guild %s",
                member.id,
                guild.id,
            )
            return [], []

    async def _apply_role_changes(self, guild, member, to_add, to_remove):
        """Apply a computed ``(to_add, to_remove)`` reward-role diff to a member.

        Shared by :meth:`grant_for_levelup` (level UP) and
        :meth:`reconcile_for_level` (level DOWN after an admin XP edit): resolves
        each role id, skips one the bot cannot manage (above its top role, or
        managed) with a debug log, swallows a per-role ``HTTPException`` so one
        bad role never blocks the rest, and lazily prunes any rule row pointing
        at a since-deleted role. Returns ``(added, removed)`` role lists (the
        roles actually applied), so the caller can build an announce suffix.
        """
        added = []
        removed = []
        stale_role_ids = set()
        for role_id in to_add:
            role = guild.get_role(role_id)
            if role is None:
                stale_role_ids.add(role_id)
                continue
            if not _assignable(role, guild):
                log.debug(
                    "Cannot assign level-reward role %s in guild %s "
                    "(above my top role or managed)",
                    role_id,
                    guild.id,
                )
                continue
            try:
                await member.add_roles(role, reason="Level reward")
                added.append(role)
            except discord.HTTPException:
                log.debug(
                    "Failed to add level-reward role %s to %s",
                    role_id,
                    member.id,
                )

        for role_id in to_remove:
            role = guild.get_role(role_id)
            if role is None:
                stale_role_ids.add(role_id)
                continue
            if not _assignable(role, guild):
                log.debug(
                    "Cannot remove level-reward role %s in guild %s "
                    "(above my top role or managed)",
                    role_id,
                    guild.id,
                )
                continue
            try:
                await member.remove_roles(role, reason="Level reward (replace mode)")
                removed.append(role)
            except discord.HTTPException:
                log.debug(
                    "Failed to remove level-reward role %s from %s",
                    role_id,
                    member.id,
                )

        if stale_role_ids:
            await self._prune_stale_rules(guild.id, stale_role_ids)

        return added, removed

    async def _prune_stale_rules(self, guild_id, role_ids):
        """Drop rule rows for roles that no longer exist (lazy prune, INFO log)."""
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM level_rewards WHERE guild_id = $1 "
                "AND role_id = ANY($2::bigint[]);",
                guild_id,
                list(role_ids),
            )
            log.info(
                "Pruned level_rewards rule(s) for deleted role(s) %s in guild %s",
                sorted(role_ids),
                guild_id,
            )
        except Exception:
            log.exception("Failed to prune stale level_rewards rules")

    # -- shared reads --------------------------------------------------
    async def _fetch_rules(self, guild_id):
        rows = await self.bot.db_pool.fetch(
            "SELECT level, role_id FROM level_rewards WHERE guild_id = $1;",
            guild_id,
        )
        return [(row["level"], row["role_id"]) for row in rows]

    async def _fetch_mode(self, guild_id):
        mode = await self.bot.db_pool.fetchval(
            "SELECT rewards_mode FROM level_config WHERE guild_id = $1;", guild_id
        )
        return mode or level_rewards.DEFAULT_MODE

    async def _send_list(self, ctx):
        rules = await self._fetch_rules(ctx.guild.id)
        mode = await self._fetch_mode(ctx.guild.id)
        view = LevelRewardsListView(ctx.guild, rules, mode)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    # -- command group ---------------------------------------------------
    @commands.hybrid_group(name="levelrewards", aliases=["lvlrewards"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelrewards(self, ctx):
        """Manage level-up role rewards."""
        if ctx.invoked_subcommand is None:
            await self._send_list(ctx)

    @levelrewards.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        level="The level that grants the role.", role="The role to grant."
    )
    async def levelrewards_add(self, ctx, level: int, role: discord.Role):
        """Grant a role automatically when a member reaches a level."""
        if role.guild.id != ctx.guild.id:
            await ctx.send(_("That role isn't from this server."))
            return
        if role.is_default():
            await ctx.send(_("You can't use @everyone as a level reward."))
            return
        if level < 1:
            await ctx.send(_("The level must be 1 or higher."))
            return

        # Friendly fast-path refusal when the guild is already at the cap. The
        # count here is advisory only: the WHERE guard inside the INSERT below is
        # what enforces the cap RACE-SAFELY (mirrors playlists_shared._save_guild_
        # playlist), so two admins adding the 25th rule at once can never both win.
        count = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM level_rewards WHERE guild_id = $1;", ctx.guild.id
        )
        if not level_rewards.can_add_rule(count or 0):
            await ctx.send(
                _(
                    "This server already has the maximum of {max} level "
                    "rewards."
                ).format(max=level_rewards.MAX_REWARDS_PER_GUILD)
            )
            return

        inserted = await self.bot.db_pool.fetchval(
            """
            INSERT INTO level_rewards (guild_id, level, role_id)
            SELECT $1, $2, $3
            WHERE (SELECT COUNT(*) FROM level_rewards WHERE guild_id = $1) < $4
            ON CONFLICT (guild_id, level, role_id) DO NOTHING
            RETURNING level;
            """,
            ctx.guild.id,
            level,
            role.id,
            level_rewards.MAX_REWARDS_PER_GUILD,
        )
        if inserted is None:
            # Nothing was inserted: either this exact rule already exists (the
            # common case) or a concurrent add just filled the last slot and the
            # WHERE guard refused (the race). Distinguish so the admin sees the
            # right reason instead of a misleading "already a reward".
            exists = await self.bot.db_pool.fetchval(
                "SELECT 1 FROM level_rewards "
                "WHERE guild_id = $1 AND level = $2 AND role_id = $3;",
                ctx.guild.id,
                level,
                role.id,
            )
            if exists:
                await ctx.send(
                    _("{role} is already a level {level} reward.").format(
                        role=role.mention, level=level
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.send(
                    _(
                        "This server already has the maximum of {max} level "
                        "rewards."
                    ).format(max=level_rewards.MAX_REWARDS_PER_GUILD)
                )
            return

        lines = [
            _("Added a level reward: reach level **{level}** to receive "
              "{role}.").format(level=level, role=role.mention)
        ]
        if not _assignable(role, ctx.guild):
            lines.append(
                _(
                    "I can add it, but that role is above me - move my role "
                    "up so I can actually assign it."
                )
            )
        embed = discord.Embed(
            title=_("Level reward added"),
            description="\n".join(lines),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelrewards.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelrewards_remove(self, ctx):
        """Pick a level reward to remove from a list of every rule set up."""
        rules = await self._fetch_rules(ctx.guild.id)
        if not rules:
            await ctx.send(
                _("This server has no level rewards configured yet.")
            )
            return
        view = _RemoveRewardView(self, ctx.guild, ctx.author.id, rules)
        view.message = await ctx.send(
            _("Pick a reward rule to remove:"), view=view
        )

    @levelrewards.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelrewards_list(self, ctx):
        """Show every level reward configured for this server."""
        await self._send_list(ctx)

    @levelrewards.command(name="mode")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(mode="stack (keep every reward) or replace (latest only).")
    async def levelrewards_mode(
        self, ctx, mode: Literal["stack", "replace"]
    ):
        """Set whether members keep every earned reward, or only the latest."""
        # rewards_mode shares level_config with the leveling on/off flag. Creating
        # that row here for a guild that enabled leveling ONLY through the legacy
        # guild_settings.leveling_enabled JSONB (no level_config row yet) would
        # otherwise mask that flag: a fresh row defaults enabled=FALSE and cog_load
        # treats ANY level_config row as authoritative, so on the next restart
        # leveling would silently switch off. Seed `enabled` from the legacy flag on
        # INSERT to preserve the read-through migration; DO UPDATE never touches
        # `enabled`, so set_enabled stays the sole writer of an existing row's flag.
        await self.bot.db_pool.execute(
            """
            INSERT INTO level_config (guild_id, enabled, rewards_mode)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2
            )
            ON CONFLICT (guild_id) DO UPDATE SET rewards_mode = $2;
            """,
            ctx.guild.id,
            mode,
        )
        if mode == level_rewards.REPLACE:
            desc = _(
                "Members now keep only the roles from the highest level "
                "reward they've reached."
            )
        else:
            desc = _(
                "Members now keep every level reward role they've ever "
                "earned."
            )
        embed = discord.Embed(
            title=_("Reward mode updated"), description=desc, colour=random_colour()
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(LevelRewards(bot))
