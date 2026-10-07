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
(tools.premium.GUILD_PREMIUM/USER_PREMIUM) actually carries - see
:func:`_catalog_text`, and its own docstring for why no number here is ever a
literal in a format string:

1. Yasuho+'s THREE headline benefits (24/7 music first, per the plan, then
   the server playlists and the year of statistics), each a bold title plus
   one short line, then ONE secondary line naming everything else it
   includes. Tickets - per-member OR the internal per-server cap - are
   deliberately NOT listed: the member cap is an admin setting, not a sales
   point, and the server cap is an anti-abuse backstop nobody is meant to
   see here;
2. Pack Confort's own one-line pitch (favourites, reminders, recurring);
3. one commitments line (nothing free becomes paid, nothing is deleted when
   a perk ends, Discord handles the purchase) plus a link button to
   TERMS.md;
4. this server's Yasuho+ status (not active / active-and-why: a Discord
   purchase keeps its existing "(purchased), active until/with no end date"
   wording; an owner gift reads "offered to this server" instead, either
   "- permanent access" or "until {date}") and the invoker's own Pack
   Confort status, worded the same way - :func:`tools.premium.guild_status`/
   :func:`tools.premium.user_status` (M3c, tools/premium.py) say which;
5. a purchase button for whichever product has a SKU configured. The
   Yasuho+ one only renders for a member with Manage Server in THIS guild;
   anyone else sees a line pointing them at a server admin instead. A
   product with no SKU configured yet shows a neutral "not on sale" line
   and no button, for that product only - but that line is de-duplicated
   (:func:`PremiumPanelView._build`'s ``_add_not_on_sale``): when BOTH
   products lack a SKU (today's posture, no ``[Premium]`` section at all),
   the panel says it ONCE, not twice.

In a DM (``ctx.guild is None``), the Yasuho+ pitch text still shows (it is
the same catalog text for everyone), but its status line and its purchase
button both drop - there is no guild to show a status for or to buy for. The
Pack Confort block (pitch, status, button) always shows: it is a personal
purchase, not guild-scoped.

Ephemeral by design (one more line in the plan: ``/premium`` must never spam
a channel), via the same ``ephemeral=ctx.interaction is not None`` pattern
every other hybrid command in this tree uses for a slash-vs-prefix send.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from tools import premium, premium_upsell
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


def _catalog_text(guild_plus, user_plus):
    """The Yasuho+ / Pack Confort pitch text.

    Every NUMBER in here is read off ``guild_plus``
    (:class:`tools.premium.GuildLimits`) and ``user_plus``
    (:class:`tools.premium.UserLimits`) - never a literal - so a catalog
    change in tools/premium.py (a new tier, a raised ceiling after measuring
    load, ...) shows up here with no edit to this function at all. Kept pure
    and parameterised (rather than reaching for the module-level
    ``premium.GUILD_PREMIUM`` etc. directly) so a test can hand it synthetic,
    distinctive values and assert they land in the output verbatim - see
    tests/cogs/test_premium_panel.py's catalog test and its negative control
    (a hardcoded number in this function fails that control, since the
    synthetic value would then never appear).

    THREE headline benefits, per the plan's own "argument principal: musique
    24/7" plus the two next most tangible ones (server playlists, a year of
    history on /serverstats), each a bold title and one short, concrete line
    - no "Free: x | Yasuho+: y" table. Everything else Yasuho+ adds (Previous
    history, AniList feeds, role menus, voice hubs, the badge) is ONE
    secondary line. Tickets - per-member and the internal per-server cap -
    are deliberately absent from both: the member cap is an admin setting,
    not a sales point, and the server cap must never be advertised at all
    (see cogs/config/tickets/open.py's own docstring).
    """
    lines = [
        _("## Yasuho+"),
        "",
        _("**24/7 music**"),
        _(
            "Yasuho stays in your voice channel, even when the queue is "
            "empty."
        ),
        "",
        _("**Extended server playlists**"),
        _("Up to {count} playlists of {tracks} tracks.").format(
            count=guild_plus.max_guild_playlists,
            tracks=guild_plus.max_playlist_tracks,
        ),
        "",
        _("**A year of statistics**"),
        _("See your server's activity over the last {days} days.").format(
            days=guild_plus.serverstats_retention_days
        ),
        "",
        _(
            "Also includes: a {history}-track Previous history, {feeds} "
            "AniList feeds, {menus} role menus, {hubs} voice hubs and the "
            "Yasuho+ badge."
        ).format(
            history=guild_plus.history_max_items,
            feeds=guild_plus.max_feeds_per_guild,
            menus=guild_plus.max_menus_per_guild,
            hubs=guild_plus.max_hubs,
        ),
        "",
        _("## Pack Confort"),
        "",
        _(
            "A one-time purchase, tied to your Discord account and valid "
            "for the lifetime of the Yasuho service: {favourites} "
            "favourites, {reminders} reminders including {recurring} "
            "recurring."
        ).format(
            favourites=user_plus.max_favourites,
            reminders=user_plus.max_pending_reminders,
            recurring=user_plus.max_recurring_reminders,
        ),
    ]
    return "\n".join(lines)


def _commitments_text():
    return _(
        "Nothing free becomes paid, nothing is deleted when a perk ends, "
        "and purchases are handled by Discord."
    )


def _guild_status_text(status):
    """Render :func:`tools.premium.guild_status`'s result as one sentence.

    A GIFT (owner grant, ``source == "gift"``) gets its OWN wording - "offered
    to this server", permanent or until a date - rather than the purchase
    phrasing with "(gifted)" bolted on: a gift is not a sale, so it should
    not read like one. A PURCHASE keeps today's exact wording.
    """
    if not status["active"]:
        return _("This server does not have **Yasuho+**.")
    if status["source"] == "gift":
        if status["ends_at"] is not None:
            return _("Yasuho+ is offered to this server until {until}.").format(
                until=format_dt(status["ends_at"])
            )
        return _("Yasuho+ is offered to this server - permanent access.")
    if status["ends_at"] is not None:
        return _(
            "This server has **Yasuho+** (purchased), active until {until}."
        ).format(until=format_dt(status["ends_at"]))
    return _("This server has **Yasuho+** (purchased), with no end date.")


def _user_status_text(status):
    """Render :func:`tools.premium.user_status`'s result as one sentence.

    Same gift-vs-purchase split as :func:`_guild_status_text`."""
    if not status["active"]:
        return _("You do not have the **Pack Confort**.")
    if status["source"] == "gift":
        if status["ends_at"] is not None:
            return _("The Pack Confort is offered to you until {until}.").format(
                until=format_dt(status["ends_at"])
            )
        return _("The Pack Confort is offered to you - permanent access.")
    if status["ends_at"] is not None:
        return _(
            "You have the **Pack Confort** (purchased), active until {until}."
        ).format(until=format_dt(status["ends_at"]))
    return _("You have the **Pack Confort** (purchased), with no end date.")


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
                _catalog_text(premium.GUILD_PREMIUM, premium.USER_PREMIUM)
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

        # "Premium is not on sale yet" appears AT MOST ONCE, even on today's
        # posture where NEITHER SKU is configured and both product blocks
        # below would otherwise each add their own copy of the same line.
        not_on_sale_shown = False

        def _add_not_on_sale():
            nonlocal not_on_sale_shown
            if not not_on_sale_shown:
                container.add_item(discord.ui.TextDisplay(_not_on_sale_text()))
                not_on_sale_shown = True

        if guild is not None:
            if premium.YASUHO_PLUS_SKU is None:
                _add_not_on_sale()
            elif can_manage_guild:
                container.add_item(
                    discord.ui.ActionRow(
                        discord.ui.Button(sku_id=premium.YASUHO_PLUS_SKU)
                    )
                )
            else:
                container.add_item(discord.ui.TextDisplay(_admin_only_text()))

        if premium.COMFORT_PACK_SKU is None:
            _add_not_on_sale()
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
        # Sollicitation rule (the plan's "reset seulement si la personne ouvre
        # /premium"): opening this panel resets every limit key's 7-day
        # upsell throttle for this person - see tools/premium_upsell.py's
        # '*' sentinel. Best effort: a write failure here must never block
        # the panel itself from rendering.
        await premium_upsell.mark_premium_opened(self.bot.db_pool, ctx.author.id)
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
