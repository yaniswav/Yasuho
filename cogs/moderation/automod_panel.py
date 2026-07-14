"""AutoMod control panel: the Components V2 surface for the AutoMod cog.

House concern-split (mirrors ``cogs/music/views.py`` and the presentation half of
``cogs/community/level_config_ui.py``): this module owns the panel View, its
interactive components, and the display CATALOG the panel renders - the custom +
native filters and the action taken when a custom filter trips.

Import direction is one-way: ``cogs/moderation/automod.py`` (the engine) imports
this module's catalog and the panel; this module imports NOTHING from the engine.
There is therefore no import cycle - the panel is handed the AutoMod cog instance
at construction time and calls back into it (``cog.set_custom_rule``,
``cog.set_native_rule``, ``cog.bot.db_pool``). The action catalog lives here (with
its labels) as the single source of truth, so the engine imports
``VALID_ACTIONS`` / ``DEFAULT_ACTION`` from here rather than duplicating them.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the fancy
ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import logging

import discord

from tools import interactions, settings
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Display catalog: the filters the panel shows and the engine enforces.
# ----------------------------------------------------------------------
# Each entry is ``(state_key, emoji, N_ label, N_ one-line description)``. The
# ``state_key`` is the shared vocabulary the engine keys its DB column / native
# rule map on (see cogs/moderation/automod.py); everything else is presentation.
# Labels/descriptions are N_-marked here (extracted, stored in English) and
# translated at render time with ``_(...)`` (the mark-then-translate-at-use
# pattern - see tools.i18n.mark and cogs/system/help.py's CATEGORIES).
CONTENT_FILTERS = [
    ("link", "🔗", N_("Links"), N_("Deletes any link posted by a non-moderator.")),
    (
        "invite",
        "📨",
        N_("Invites"),
        N_("Deletes Discord invite links (discord.gg/...)."),
    ),
    (
        "spam",
        "💬",
        N_("Spam"),
        N_("Trips on a burst of messages sent in just a few seconds."),
    ),
]

NATIVE_FILTERS = [
    (
        "kw",
        "🚫",
        N_("Keywords"),
        N_("Discord's built-in profanity and slur presets."),
    ),
    (
        "nspam",
        "🌊",
        N_("Spam"),
        N_("Discord's own detection of repeated, spammy content."),
    ),
    (
        "nmention",
        "📣",
        N_("Mentions"),
        N_("Blocks messages that ping five or more people."),
    ),
]

# What happens to a member who trips a CUSTOM filter. ``(value, N_ label, emoji,
# N_ description)``. ``value`` is the stored/enforced action string (also the case
# action) - never change it, only its presentation. This list is the single
# source of truth for "which actions exist": the engine imports the derived
# VALID_ACTIONS / DEFAULT_ACTION below.
ACTION_CHOICES = [
    ("delete", N_("Delete only"), "🗑️", N_("Just remove the offending message.")),
    (
        "warn",
        N_("Warn"),
        "⚠️",
        N_("Remove it and add a warning (escalates per your warn rules)."),
    ),
    (
        "mute",
        N_("Timeout"),
        "🔇",
        N_("Remove it and time the member out for 10 minutes."),
    ),
    ("kick", N_("Kick"), "👢", N_("Remove it and kick the member.")),
]

VALID_ACTIONS = {value for value, *_rest in ACTION_CHOICES}
DEFAULT_ACTION = "delete"
_ACTION_LABELS = {value: label for value, label, _emoji, _desc in ACTION_CHOICES}


def _mark(value):
    """Render a filter's tri-state (on / off / unreadable) as a labelled dot."""

    if value is None:
        return _("⚪ Unavailable")
    return _("🟢 Enabled") if value else _("🔴 Disabled")


def _action_options(current):
    """Options for the panel's action <Select>, current value pre-selected."""

    return [
        discord.SelectOption(
            label=_(label),
            value=value,
            emoji=emoji,
            description=_(desc)[:100],
            default=value == current,
        )
        for value, label, emoji, desc in ACTION_CHOICES
    ]


# ----------------------------------------------------------------------
# Interactive panel components (discord.ui)
# ----------------------------------------------------------------------
class _FilterToggle(discord.ui.Button):
    """A single on/off filter button; green when on, grey when off, and disabled
    with an ``(N/A)`` label when a native rule can't be read (missing the
    Manage Server permission)."""

    def __init__(self, panel, key, emoji, label, *, native):
        self.panel = panel
        self.key = key
        self.native = native
        self._label = label  # already translated by the caller
        super().__init__(emoji=emoji, label=label)
        self._apply_state()

    def _apply_state(self):
        state = self.panel.state.get(self.key)
        if state is None:
            # Native rule we can't read/manage (missing permission) -> disabled.
            self.disabled = True
            self.label = _("{label} (N/A)").format(label=self._label)
            self.style = discord.ButtonStyle.secondary
        else:
            self.disabled = False
            self.label = self._label
            self.style = (
                discord.ButtonStyle.success
                if state
                else discord.ButtonStyle.secondary
            )

    async def callback(self, interaction):
        await self.panel.toggle(interaction, self.key, self.native)


class _ActionSelect(discord.ui.Select):
    """Choose what happens to a member who trips a custom filter."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            placeholder=_("Action when a filter trips..."),
            min_values=1,
            max_values=1,
            options=_action_options(panel.state["action"]),
        )

    async def callback(self, interaction):
        await self.panel.set_action(interaction, self.values[0])


class _ExemptRoleSelect(discord.ui.RoleSelect):
    """Roles whose members bypass the custom filters."""

    def __init__(self, panel, defaults):
        self.panel = panel
        super().__init__(
            placeholder=_("Exempt roles (select to replace)..."),
            min_values=0,
            max_values=25,
            default_values=defaults,
        )

    async def callback(self, interaction):
        await self.panel.set_exempt(
            interaction, "roles", [r.id for r in self.values]
        )


class _ExemptChannelSelect(discord.ui.ChannelSelect):
    """Channels in which the custom filters are not enforced."""

    def __init__(self, panel, defaults):
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
        )

    async def callback(self, interaction):
        await self.panel.set_exempt(
            interaction, "channels", [c.id for c in self.values]
        )


async def _refresh_layout(interaction, message, view):
    """Edit a LayoutView panel in place with ``view=`` only (no embed/content).

    A Components V2 message carries its content inside the view and Discord
    rejects an ``embed=`` on such an edit, so the panel is born CV2 and stays
    ``view=``-only for its whole life.
    """

    await interactions.refresh_layout(
        interaction, message, view, surface="automod panel"
    )


class AutoModPanel(AuthorLayoutView):
    """Author-restricted control panel for every custom + native AutoMod filter.

    A single Components V2 :class:`~discord.ui.Container` in the house style
    (settings / level-config panels): a header, then a section per filter family
    with a status dot and a ``-#`` explanation line, then the enforcement summary,
    then one control row each for the toggles, the action select and the two
    exemption selects.

    Text-budget math (Components V2 caps the combined TextDisplay text at 4000
    characters). The fixed chrome - header, both filter sections with their six
    ``-#`` lines, the enforcement labels and the footer - is roughly 1200
    characters. The only variable content is the two exempt lists, and each select
    is capped at ``max_values=25``, so each list is at most ~25 mentions of ~24
    characters (~625). Worst case is therefore ~1200 + 625 + 625 = ~2450, a clear
    ~1500 under the 4000 ceiling - so, unlike the help menu, this panel needs no
    truncation logic.

    The deny wording matches AuthorLayoutView's default ("This panel isn't for
    you.", the wording the pre-CV2 panel used), so it is left unset here.
    """

    def __init__(self, cog, guild, author_id, state, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.state = state
        self._build()

    # -- rendering ------------------------------------------------------
    def _filter_section(self, title, subtitle, specs):
        """One filter family as a TextDisplay: bold title, ``-#`` subtitle, then a
        status dot + ``-#`` description line per filter."""

        lines = ["**" + title + "**", "-# " + subtitle]
        for key, emoji, label, desc in specs:
            lines.append(f"{emoji} **{_(label)}** - {_mark(self.state.get(key))}")
            lines.append("-# " + _(desc))
        return discord.ui.TextDisplay("\n".join(lines))

    def _build(self):
        """(Re)assemble the layout from the current state."""

        state = self.state
        container = discord.ui.Container(accent_colour=random_colour())

        # Header.
        container.add_item(
            discord.ui.TextDisplay(
                "### 🛡️ "
                + _("AutoMod - {guild}").format(guild=self.guild.name)
                + "\n-# "
                + _(
                    "Keeping the server tidy: Yasuho scans each message for the "
                    "content filters, while Discord's native filters block matches "
                    "before they are ever posted."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        # Custom (Yasuho) filters.
        container.add_item(
            self._filter_section(
                _("Content filters"),
                _("Checked by Yasuho on every message."),
                CONTENT_FILTERS,
            )
        )
        container.add_item(discord.ui.Separator())

        # Native (Discord) filters.
        container.add_item(
            self._filter_section(
                _("Native filters (Discord)"),
                _("Enforced by Discord before a message is posted."),
                NATIVE_FILTERS,
            )
        )
        container.add_item(discord.ui.Separator())

        # Enforcement summary: action + exemptions.
        roles = state["exempt_roles"]
        channels = state["exempt_channels"]
        roles_value = ", ".join(f"<@&{r}>" for r in roles) if roles else _("None")
        channels_value = (
            ", ".join(f"<#{c}>" for c in channels) if channels else _("None")
        )
        action_label = _(
            _ACTION_LABELS.get(state["action"], _ACTION_LABELS[DEFAULT_ACTION])
        )
        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("When a content filter trips")
                + "**\n"
                + _("Action")
                + f": **{action_label}**\n"
                + _("Exempt roles")
                + f": {roles_value}\n"
                + _("Exempt channels")
                + f": {channels_value}\n"
                + "-# "
                + _(
                    "Members with an exempt role, and messages in an exempt "
                    "channel, skip the content filters entirely."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        # Control rows.
        container.add_item(
            discord.ui.ActionRow(
                *[
                    _FilterToggle(self, key, emoji, _(label), native=False)
                    for key, emoji, label, _desc in CONTENT_FILTERS
                ]
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                *[
                    _FilterToggle(self, key, emoji, _(label), native=True)
                    for key, emoji, label, _desc in NATIVE_FILTERS
                ]
            )
        )
        container.add_item(discord.ui.ActionRow(_ActionSelect(self)))

        role_defaults = [
            r
            for r in (self.guild.get_role(i) for i in state["exempt_roles"])
            if r is not None
        ]
        channel_defaults = [
            c
            for c in (self.guild.get_channel(i) for i in state["exempt_channels"])
            if c is not None
        ]
        container.add_item(
            discord.ui.ActionRow(_ExemptRoleSelect(self, defaults=role_defaults))
        )
        container.add_item(
            discord.ui.ActionRow(
                _ExemptChannelSelect(self, defaults=channel_defaults)
            )
        )

        if any(state[k] is None for k in ("kw", "nspam", "nmention")):
            container.add_item(
                discord.ui.TextDisplay(
                    "-# "
                    + _(
                        "Native filters need Yasuho to have the Manage Server "
                        "permission."
                    )
                )
            )

        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " · "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    async def _rerender(self, interaction):
        """Rebuild a fresh panel from current state and show it in place."""

        new = AutoModPanel(self.cog, self.guild, self.author_id, dict(self.state))
        new.message = self.message
        self.stop()
        await _refresh_layout(interaction, self.message, new)

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
                            "Yasuho needs the **Manage Server** permission to "
                            "change Discord's native filters."
                        ),
                        ephemeral=True,
                    )
                self.state[key] = new_state
            else:
                await self.cog.set_custom_rule(self.guild.id, key, target)
                self.state[key] = target
            await self._rerender(interaction)
        except Exception:
            log.exception("AutoMod panel toggle failed")
            await self._safe_fail(interaction)

    async def set_action(self, interaction, value):
        try:
            if value not in VALID_ACTIONS:
                value = DEFAULT_ACTION
            await settings.set_guild(
                self.cog.bot.db_pool, self.guild.id, "automod_action", value
            )
            self.state["action"] = value
            await self._rerender(interaction)
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
            await self._rerender(interaction)
        except Exception:
            log.exception("AutoMod panel exemption update failed")
            await self._safe_fail(interaction)
