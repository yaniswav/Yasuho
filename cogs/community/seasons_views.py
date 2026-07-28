"""Components V2 views for the leveling seasons feature (S2).

This module is the PRESENTATION half of the seasons feature (mirrors the
music.py -> views.py and automod.py -> automod_panel.py splits): it owns the
two interactive Components V2 surfaces and nothing else. The engine and the
command bodies live in the sibling ``cogs/community/seasons.py``; import
direction is one-way (this module imports NOTHING from there) - a view is
handed the ``Seasons`` cog instance at construction time and calls back into
its query/write methods (``cog.season_podium_rows``, ``cog.set_champion_role``,
...), exactly like AutoModPanel calls back into the AutoMod cog.

* :class:`HallOfFameCard` - the ``/halloffame`` browsable podium, one season
  per page, walked via indexed PK hops (never a full season list loaded into
  memory - see cogs/community/seasons.py's "hall of fame browsing queries").
* :class:`SeasonsPanel` - the ``/levelconfig seasons`` admin panel: the season
  champion RoleSelect (+ a clear button) and the season-announce toggle,
  refusing an inert toggle rather than silently accepting one (see
  :func:`_describe_season_announce`).

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import logging

import discord

from tools import interactions, leveling
from tools.formats import random_colour
from tools.i18n import _
from tools.modchecks import bot_can_assign_role
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# The card names its podium with raw <@id> mentions, and a page flip REPLACES
# them with a different season's members. The bot's client-wide default allows
# user mentions (core.Yasuho: users=True), and discord.py folds that default
# into every edit it sends (verified: InteractionResponse.edit_message passes
# previous_allowed_mentions=state.allowed_mentions), so an un-suppressed page
# flip would PING everyone newly shown - a stranger's browsing would notify a
# member for a season they won months ago. The initial send already suppresses
# this; every edit has to say it too.
_NO_PINGS = discord.AllowedMentions.none()

# Medal glyphs, taken from the ONE shared home (tools.leveling.PODIUM_MEDALS)
# rather than copied: importing them from cogs/community/seasons.py would break
# this module's strictly one-way import direction (see the module docstring),
# but tools.leveling is neutral ground both sides already import.
_MEDALS = leveling.PODIUM_MEDALS


class _SeasonPagerButton(discord.ui.Button):
    """A hall-of-fame pager button whose click delegates to a bound handler.

    Components V2 layouts cannot use the ``@discord.ui.button`` decorator
    (buttons live inside :class:`discord.ui.ActionRow` children), so Prev/Next
    are plain instances that forward their click to a coroutine on the owning
    card - the same shape as the leveling cog's ``_PagerButton`` and the
    reminders card's ``_RemPagerButton``.
    """

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction):
        await self._handler(interaction)


class HallOfFameCard(AuthorLayoutView):
    """Browsable Components V2 podium of a guild's closed leveling seasons.

    Opens on the MOST RECENT season (the guild's default "what happened
    lately" view) and lets the author walk older/newer ones with Prev/Next,
    bounded to seasons that actually exist. Each hop is one or two indexed
    ``season_podiums`` lookups (see cogs/community/seasons.py's
    ``older_season_key`` / ``newer_season_key`` / ``season_podium_rows``, all
    served by the ``(guild_id, period_key, rank)`` PK) - never a query that
    loads every season a guild ever had, so a guild with 2 closed seasons or
    200 costs the same per page flip.

    A neighbour's existence is cheap to reason about without an extra query in
    the common case: moving to an OLDER season means the one we just left is
    necessarily NEWER than the new position (so ``has_newer`` is always True
    there), and symmetrically for moving NEWER - only the far side ever needs
    a fresh lookup. Author-gated through
    :class:`~tools.views.AuthorLayoutView` so only the member who opened it
    can flip pages.
    """

    def __init__(
        self,
        cog,
        guild,
        author_id,
        period_key,
        podium,
        has_older,
        has_newer,
        *,
        timeout=180,
    ):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.period_key = period_key
        self.podium = podium
        self.has_older = has_older
        self.has_newer = has_newer
        self._build()

    def _line(self, rank, user_id, xp):
        marker = _MEDALS.get(rank, "#{rank}".format(rank=rank))
        return _("{marker} <@{user_id}> - {xp} XP").format(
            marker=marker, user_id=user_id, xp=xp
        )

    def _build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "### \N{TROPHY} "
                + _("Hall of fame - {guild}").format(guild=self.guild.name)
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Season {month}").format(
                    month=leveling.format_month_period_label(self.period_key)
                )
                + "**"
            )
        )
        if not self.podium:
            # Structurally unreachable today (a frozen season always has at
            # least one podium place, see the engine's snapshot contract), but
            # cheap to survive soberly rather than render an empty container.
            container.add_item(
                discord.ui.TextDisplay(_("No podium recorded for this season."))
            )
        else:
            container.add_item(
                discord.ui.TextDisplay(
                    "\n".join(
                        self._line(rank, user_id, xp)
                        for rank, user_id, xp in self.podium
                    )
                )
            )

        if self.has_older or self.has_newer:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.ActionRow(
                    _SeasonPagerButton(
                        self._older,
                        label=_("Prev"),
                        emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                        style=discord.ButtonStyle.secondary,
                        disabled=not self.has_older,
                    ),
                    _SeasonPagerButton(
                        self._newer,
                        label=_("Next"),
                        emoji="\N{BLACK RIGHT-POINTING TRIANGLE}",
                        style=discord.ButtonStyle.secondary,
                        disabled=not self.has_newer,
                    ),
                )
            )

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                # The house footer wording, reused verbatim (settings, automod,
                # warn-config panels all ship it) rather than a pager-specific
                # variant - one msgid, already translated in every locale.
                "-# "
                + _("Only you can use these controls")
                + " - "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    async def _older(self, interaction):
        try:
            key = await self.cog.older_season_key(self.guild.id, self.period_key)
            if key is None:
                return await interaction.response.defer()
            podium = await self.cog.season_podium_rows(self.guild.id, key)
            even_older = await self.cog.older_season_key(self.guild.id, key)
            self.period_key = key
            self.podium = podium
            self.has_older = even_older is not None
            self.has_newer = True  # we just came from a strictly newer season
            self._build()
            await interaction.response.edit_message(
                view=self, allowed_mentions=_NO_PINGS
            )
        except Exception:
            log.exception("Hall of fame prev failed")
            # A bare log leaves the clicker on Discord's own "This interaction
            # failed" with no idea whether the bot is broken or the button is:
            # always answer, with the house generic failure msgid (already
            # translated everywhere - no new string for a pager).
            await interactions.notify_failure(
                interaction, _("Something went wrong.")
            )

    async def _newer(self, interaction):
        try:
            key = await self.cog.newer_season_key(self.guild.id, self.period_key)
            if key is None:
                return await interaction.response.defer()
            podium = await self.cog.season_podium_rows(self.guild.id, key)
            even_newer = await self.cog.newer_season_key(self.guild.id, key)
            self.period_key = key
            self.podium = podium
            self.has_newer = even_newer is not None
            self.has_older = True  # we just came from a strictly older season
            self._build()
            await interaction.response.edit_message(
                view=self, allowed_mentions=_NO_PINGS
            )
        except Exception:
            log.exception("Hall of fame next failed")
            # Same as _older: never leave the click unanswered (see there).
            await interactions.notify_failure(
                interaction, _("Something went wrong.")
            )


# ---------------------------------------------------------------------------
# /levelconfig seasons: the champion-role + announce-toggle admin panel.
# ---------------------------------------------------------------------------
def season_panel_state(row):
    """The panel's initial state dict from a ``_SEASON_CONFIG_SQL`` row.

    ``row`` is ``None`` for a guild with no level_config row at all (never
    configured anything leveling-related yet) - every knob then falls back to
    its inert default, same shape as :meth:`tools.leveling.LevelConfig.from_row`
    handling an absent column.
    """
    if row is None:
        return {
            "champion_role_id": None,
            "season_announce": False,
            "announce_mode": leveling.DEFAULT_ANNOUNCE_MODE,
            "announce_channel_id": None,
        }
    return {
        "champion_role_id": row["season_champion_role_id"],
        "season_announce": bool(row["season_announce"]),
        "announce_mode": row["announce_mode"] or leveling.DEFAULT_ANNOUNCE_MODE,
        "announce_channel_id": row["announce_channel_id"],
    }


def _describe_champion_role(state):
    if state["champion_role_id"] is None:
        return _("None set - no role is granted for winning a season.")
    # A bare mention token, no natural language in it: never wrapped in _()
    # (mirrors cogs/community/seasons.py's own "<@{user_id}>".format(...)).
    return "<@&{role_id}>".format(role_id=state["champion_role_id"])


def _describe_season_announce(state):
    if not state["season_announce"]:
        return _("Off - the season podium is never announced.")
    if state["announce_mode"] == "fixed" and state["announce_channel_id"]:
        return _("On - posted in <#{channel_id}> when a season closes.").format(
            channel_id=state["announce_channel_id"]
        )
    # Representable (an admin changed the announce mode away from "fixed"
    # after turning this on) even though the panel's OWN toggle refuses to
    # ever create this state - see SeasonsPanel.toggle_announce.
    return _(
        "On, but no fixed announce channel is configured, so nothing will "
        "actually be posted. Set one with `/levelconfig announce mode`."
    )


class _ChampionRoleSelect(discord.ui.RoleSelect):
    """Picks the season champion role. A single choice, not a set (unlike
    AutoMod's exempt-role picker) - there is only ever one champion."""

    def __init__(self, panel, defaults):
        self.panel = panel
        super().__init__(
            placeholder=_("Pick the season champion role..."),
            min_values=1,
            max_values=1,
            default_values=defaults,
        )

    async def callback(self, interaction):
        await self.panel.set_champion_role(interaction, self.values[0])


class _ClearChampionRoleButton(discord.ui.Button):
    def __init__(self, panel, *, disabled):
        super().__init__(
            label=_("Clear role"),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.clear_champion_role(interaction)


class _AnnounceToggleButton(discord.ui.Button):
    def __init__(self, panel, enabled):
        super().__init__(
            label=_("Turn off") if enabled else _("Turn on"),
            style=discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.success,
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.toggle_announce(interaction)


class SeasonsPanel(AuthorLayoutView):
    """Author-restricted admin panel for the two S1 season knobs.

    A single Components V2 :class:`~discord.ui.Container` in the house style
    (settings / level-config / automod panels): a header, a section for the
    champion role (a :class:`_ChampionRoleSelect` plus a clear button) and one
    for the announce toggle. Both writes go through the owning
    :class:`~cogs.community.seasons.Seasons` cog (``self.cog``), never the DB
    directly - this module owns no query, only the layout and the callbacks.

    The champion-role write is guarded by :func:`tools.modchecks.
    bot_can_assign_role` (the same hierarchy check the engine itself applies
    at role-move time, see Seasons._apply_champion_role): picking a role the
    bot could never actually hand out is refused here, before it is ever
    written, rather than silently accepted and failing a month later.

    The announce toggle enforces the S1 rule: turning it ON while
    announce_mode is not "fixed" (or the fixed channel is unset) would create
    an inert toggle indistinguishable from a bug (the engine's own WARNING
    log is not something an admin is watching) - so that specific transition
    is refused with an explanation instead. Turning it OFF is always allowed.

    The deny wording matches AuthorLayoutView's default ("This panel isn't for
    you."), so it is left unset here (same note as AutoModPanel's).
    """

    def __init__(self, cog, guild, author_id, state, *, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.state = state
        self._build()

    def _build(self):
        self.clear_items()
        state = self.state
        container = discord.ui.Container(accent_colour=random_colour())

        container.add_item(
            discord.ui.TextDisplay(
                "### \N{TROPHY} "
                + _("Seasons - {guild}").format(guild=self.guild.name)
                + "\n-# "
                + _(
                    "Configure the role handed to each season's champion and "
                    "whether the podium is announced when a season closes."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Season champion role")
                + "**\n"
                + _describe_champion_role(state)
            )
        )
        role_defaults = []
        if state["champion_role_id"] is not None:
            role = self.guild.get_role(state["champion_role_id"])
            if role is not None:
                role_defaults = [role]
        container.add_item(
            discord.ui.ActionRow(_ChampionRoleSelect(self, role_defaults))
        )
        container.add_item(
            discord.ui.ActionRow(
                _ClearChampionRoleButton(
                    self, disabled=state["champion_role_id"] is None
                )
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Season announce")
                + "**\n"
                + _describe_season_announce(state)
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _AnnounceToggleButton(self, state["season_announce"])
            )
        )

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " - "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    async def _rerender(self, interaction):
        await interactions.refresh_layout(interaction, self.message, self, surface="seasons panel")

    async def set_champion_role(self, interaction, role):
        try:
            if not bot_can_assign_role(role, self.guild):
                return await interactions.notify_failure(
                    interaction,
                    _(
                        "I can't manage {role} (it's above my top role, "
                        "managed by an integration, or @everyone). Pick "
                        "another role."
                    ).format(role=role.mention),
                )
            await self.cog.set_champion_role(self.guild.id, role.id)
            self.state["champion_role_id"] = role.id
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("Seasons panel champion role update failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong updating the panel.")
            )

    async def clear_champion_role(self, interaction):
        try:
            await self.cog.set_champion_role(self.guild.id, None)
            self.state["champion_role_id"] = None
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("Seasons panel champion role clear failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong updating the panel.")
            )

    async def toggle_announce(self, interaction):
        try:
            target = not self.state["season_announce"]
            if target and not (
                self.state["announce_mode"] == "fixed"
                and self.state["announce_channel_id"]
            ):
                return await interactions.notify_failure(
                    interaction,
                    _(
                        "Set a fixed announce channel first with "
                        "`/levelconfig announce mode` before turning the "
                        "season announce on."
                    ),
                )
            await self.cog.set_season_announce(self.guild.id, target)
            self.state["season_announce"] = target
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception("Seasons panel announce toggle failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong updating the panel.")
            )
