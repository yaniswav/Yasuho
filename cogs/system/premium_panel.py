"""The public ``/premium`` panel: what Yasuho+ and the Pack Confort add,
this server's and this member's live status, and - once a SKU is configured
in ``[Premium]`` - the Discord purchase button for each.

M3c of the monetisation plan (.claude/plans/monetisation/4-plan-retenu.md).

NAMED ``/premium``, PER THE PLAN. The plan's own prose and TERMS.md both cite
this command as "/premium" - the owner's hidden grant / test-purchase /
reconciliation group (cogs/system/premium.py) held that exact token first,
so THIS lot renames that group to ``?premiumadmin`` (see its own module
docstring for the full reasoning) rather than carve an exemption into
tests/test_command_tree_hygiene.py's ROOT-namespace guard for one command
name. Registered as a hybrid command (``?premium`` works too, for anyone,
not just the owner) - plain text, no leftover collision: the owner's group
no longer answers to "premium" at all.

WHY A SEPARATE COG. ``Premium`` (cogs/system/premium.py) gates its ENTIRE
surface behind ``cog_check`` (owner-only) - a command living in that cog
would inherit that gate and become unreachable for anyone else. This cog has
no gate of its own: ``/premium`` is for every member, everywhere.

WHAT IT SHOWS, built ONLY from the numbers the catalog
(tools.premium.GUILD_FREE/GUILD_PREMIUM/USER_FREE/USER_PREMIUM) actually
carries - see :func:`_catalog_text`, and its own docstring for why no number
here is ever a literal in a format string:

1. a FREE vs Yasuho+ comparison (24/7 music first, per the plan), then a
   FREE vs Pack Confort one (favourites, reminders);
2. one commitments line (nothing free becomes paid, nothing is deleted when
   a perk ends, Discord handles the purchase) plus a link button to
   TERMS.md;
3. this server's Yasuho+ status (not active / active-and-why: a Discord
   purchase or an owner gift, with the end date if any) and the invoker's
   own Pack Confort status - :func:`tools.premium.guild_status`/
   :func:`tools.premium.user_status` (M3c, tools/premium.py) say which;
4. a purchase button for whichever product has a SKU configured. The
   Yasuho+ one only renders for a member with Manage Server in THIS guild;
   anyone else sees a line pointing them at a server admin instead. A
   product with no SKU configured yet shows a neutral "not on sale" line
   and no button, for that product only (the two SKUs are configured
   independently per the plan's own rollout order - Yasuho+ before Pack
   Confort).

In a DM (``ctx.guild is None``), the entire Yasuho+ block (comparison stays,
status and button both drop) and the whole server-purchase block are
skipped - there is no guild to show a status for or to buy for. The Pack
Confort block (comparison, status, button) always shows: it is a personal
purchase, not guild-scoped.

Ephemeral by design (one more line in the plan: ``/premium`` must never spam
a channel), via the same ``ephemeral=ctx.interaction is not None`` pattern
every other hybrid command in this tree uses for a slash-vs-prefix send.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from tools import premium
from tools.formats import format_dt
from tools.i18n import _
from tools.views import AuthorLayoutView

COMMAND_NAME = "premium"

# A warm amber, distinct from the plain Discord blurple most info panels in
# this tree use (PANEL_COLOUR in cogs/community/usersettings.py, etc.) - this
# one is specifically a SALES surface, so it gets its own accent.
PANEL_COLOUR = 0xF5A623

# TERMS.md is not (yet) hosted on a dashboard page (M5a of the plan puts that
# page online BEFORE any SKU) - this mirrors the exact citation style
# TERMS.md's own section 2 already uses for PRIVACY.md (a raw GitHub blob
# link into this same public repo), so it is a real, working URL today.
TERMS_URL = "https://github.com/yaniswav/Yasuho/blob/main/TERMS.md"


def _yesno(value):
    return _("yes") if value else _("no")


def _catalog_text(guild_free, guild_plus, user_free, user_plus):
    """The FREE vs Yasuho+ / Pack Confort comparison text.

    Every NUMBER in here is read off ``guild_free``/``guild_plus``
    (:class:`tools.premium.GuildLimits`) and ``user_free``/``user_plus``
    (:class:`tools.premium.UserLimits`) - never a literal - so a catalog
    change in tools/premium.py (a new tier, a raised ceiling after measuring
    load, ...) shows up here with no edit to this function at all. Kept pure
    and parameterised (rather than reaching for the module-level
    ``premium.GUILD_FREE`` etc. directly) so a test can hand it synthetic,
    distinctive values and assert they land in the output verbatim - see
    tests/cogs/test_premium_panel.py's catalog test and its negative control
    (a hardcoded number in this function fails that control, since the
    synthetic value would then never appear).

    24/7 music is listed FIRST per the plan's own "argument principal:
    musique 24/7"; AniList is worded "per feed" (follows and titles are a
    PER-FEED cap, not a total) per the plan's own table.
    """
    lines = [
        _("## Yasuho+"),
        _(
            "A paid upgrade for a whole server. Nothing free is removed - "
            "here is what Yasuho+ adds:"
        ),
        "",
        _("**24/7 music** - Free: {free} | Yasuho+: {plus}").format(
            free=_yesno(guild_free.music_247), plus=_yesno(guild_plus.music_247)
        ),
        _(
            "**Server playlists** - Free: {free_count} x {free_tracks} "
            "tracks | Yasuho+: {plus_count} x {plus_tracks} tracks"
        ).format(
            free_count=guild_free.max_guild_playlists,
            free_tracks=guild_free.max_playlist_tracks,
            plus_count=guild_plus.max_guild_playlists,
            plus_tracks=guild_plus.max_playlist_tracks,
        ),
        _(
            "**Previous history** - Free: {free} tracks | Yasuho+: {plus} "
            "tracks"
        ).format(
            free=guild_free.history_max_items, plus=guild_plus.history_max_items
        ),
        _(
            "**AniList feeds** - Free: {free_feeds} feeds, {free_accounts} "
            "accounts and {free_titles} titles per feed | Yasuho+: "
            "{plus_feeds} feeds, {plus_accounts} accounts and {plus_titles} "
            "titles per feed"
        ).format(
            free_feeds=guild_free.max_feeds_per_guild,
            free_accounts=guild_free.max_follows_per_feed,
            free_titles=guild_free.max_subs_per_feed,
            plus_feeds=guild_plus.max_feeds_per_guild,
            plus_accounts=guild_plus.max_follows_per_feed,
            plus_titles=guild_plus.max_subs_per_feed,
        ),
        _(
            "**Server stats** - Free: {free} days | Yasuho+: {plus} days"
        ).format(
            free=guild_free.serverstats_retention_days,
            plus=guild_plus.serverstats_retention_days,
        ),
        _("**Role menus** - Free: {free} | Yasuho+: {plus}").format(
            free=guild_free.max_menus_per_guild, plus=guild_plus.max_menus_per_guild
        ),
        _("**Auto voice hubs** - Free: {free} | Yasuho+: {plus}").format(
            free=guild_free.max_hubs, plus=guild_plus.max_hubs
        ),
        _(
            "**Open tickets per member** - Free: {free} | Yasuho+: {plus}"
        ).format(
            free=guild_free.max_tickets_open_per_user,
            plus=guild_plus.max_tickets_open_per_user,
        ),
        _("**Yasuho+ badge** - Free: {free} | Yasuho+: {plus}").format(
            free=_yesno(guild_free.premium_badge),
            plus=_yesno(guild_plus.premium_badge),
        ),
        "",
        _("## Pack Confort"),
        _(
            "A one-time purchase for you personally. No "
            "subscription, no expiry while the service and your account "
            "exist."
        ),
        "",
        _("**Favourites** - Free: {free} | Pack Confort: {plus}").format(
            free=user_free.max_favourites, plus=user_plus.max_favourites
        ),
        _(
            "**Reminders** - Free: {free} ({free_recurring} recurring) | "
            "Pack Confort: {plus} ({plus_recurring} recurring)"
        ).format(
            free=user_free.max_pending_reminders,
            free_recurring=user_free.max_recurring_reminders,
            plus=user_plus.max_pending_reminders,
            plus_recurring=user_plus.max_recurring_reminders,
        ),
    ]
    return "\n".join(lines)


def _commitments_text():
    return _(
        "Nothing free becomes paid, nothing is deleted when a perk ends, "
        "and purchases are handled by Discord."
    )


def _source_label(source):
    return _("purchased") if source == "purchase" else _("gifted")


def _guild_status_text(status):
    """Render :func:`tools.premium.guild_status`'s result as one sentence."""
    if not status["active"]:
        return _("This server does not have **Yasuho+**.")
    source = _source_label(status["source"])
    if status["ends_at"] is not None:
        return _(
            "This server has **Yasuho+** ({source}), active until {until}."
        ).format(source=source, until=format_dt(status["ends_at"]))
    return _("This server has **Yasuho+** ({source}), with no end date.").format(
        source=source
    )


def _user_status_text(status):
    """Render :func:`tools.premium.user_status`'s result as one sentence."""
    if not status["active"]:
        return _("You do not have the **Pack Confort**.")
    source = _source_label(status["source"])
    if status["ends_at"] is not None:
        return _(
            "You have the **Pack Confort** ({source}), active until {until}."
        ).format(source=source, until=format_dt(status["ends_at"]))
    return _(
        "You have the **Pack Confort** ({source}), with no end date."
    ).format(source=source)


def _not_on_sale_text():
    return _("Premium is not on sale yet.")


def _admin_only_text():
    return _("A server admin can manage this with `/{command}`.").format(
        command=COMMAND_NAME
    )


class PremiumPanelView(AuthorLayoutView):
    """The ephemeral Components V2 panel itself.

    No real dispatchable component exists here besides Discord's own
    premium-style buy buttons (``sku_id=...``, no ``custom_id`` -
    discord/ui/button.py's own ``requires_custom_id`` branch confirms Discord
    handles the whole purchase flow itself and never dispatches an
    interaction to this bot for one), so :class:`AuthorLayoutView`'s author
    gate and timeout cleanup cost nothing here. It is still the base used,
    per the house convention tests/test_view_authorization_census.py
    enforces: every dispatch root derives from one of the four locale/author
    bases, display-only cards included, so nobody has to re-decide "is this
    gated?" if an actionable control is ever added later.
    """

    def __init__(
        self,
        author,
        *,
        guild,
        guild_status,
        user_status,
        can_manage_guild,
        timeout=180,
    ):
        super().__init__(author.id, timeout=timeout)
        self.message = None
        self._build(guild, guild_status, user_status, can_manage_guild)

    def _build(self, guild, g_status, u_status, can_manage_guild):
        self.clear_items()
        container = discord.ui.Container(accent_colour=PANEL_COLOUR)

        container.add_item(
            discord.ui.TextDisplay(
                _catalog_text(
                    premium.GUILD_FREE,
                    premium.GUILD_PREMIUM,
                    premium.USER_FREE,
                    premium.USER_PREMIUM,
                )
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(_commitments_text()))
        container.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=_("Terms"), url=TERMS_URL
                )
            )
        )
        container.add_item(discord.ui.Separator())

        status_lines = []
        if guild is not None:
            status_lines.append(_guild_status_text(g_status))
        status_lines.append(_user_status_text(u_status))
        container.add_item(discord.ui.TextDisplay("\n".join(status_lines)))
        container.add_item(discord.ui.Separator())

        if guild is not None:
            if premium.YASUHO_PLUS_SKU is None:
                container.add_item(discord.ui.TextDisplay(_not_on_sale_text()))
            elif can_manage_guild:
                container.add_item(
                    discord.ui.ActionRow(
                        discord.ui.Button(sku_id=premium.YASUHO_PLUS_SKU)
                    )
                )
            else:
                container.add_item(discord.ui.TextDisplay(_admin_only_text()))

        if premium.COMFORT_PACK_SKU is None:
            container.add_item(discord.ui.TextDisplay(_not_on_sale_text()))
        else:
            container.add_item(
                discord.ui.ActionRow(
                    discord.ui.Button(sku_id=premium.COMFORT_PACK_SKU)
                )
            )

        self.add_item(container)


class PremiumInfo(commands.Cog):
    """Public ``/premium`` entry point - no gate of its own."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name=COMMAND_NAME)
    async def premium_info(self, ctx):
        """Show what Yasuho+ and the Pack Confort offer, your status, and buy them."""
        u_status = await premium.user_status(self.bot.db_pool, ctx.author.id)
        guild = ctx.guild
        g_status = None
        can_manage_guild = False
        if guild is not None:
            g_status = await premium.guild_status(self.bot.db_pool, guild.id)
            can_manage_guild = ctx.author.guild_permissions.manage_guild

        view = PremiumPanelView(
            ctx.author,
            guild=guild,
            guild_status=g_status,
            user_status=u_status,
            can_manage_guild=can_manage_guild,
        )
        # A LayoutView carries its own content, so it is sent with view= only
        # (no embed, no content) - same shape as /preferences
        # (cogs/community/usersettings.py).
        view.message = await ctx.send(
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(PremiumInfo(bot))
