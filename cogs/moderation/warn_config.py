"""Warn-escalation control panel: the Components V2 surface for `/warnings config`.

House concern-split, mirroring ``cogs/moderation/automod_panel.py`` (the AutoMod
panel) and ``cogs/community/level_config_ui.py``: this module owns the panel
View, its interactive components, the action CATALOG the panel renders, and the
presentation helpers (localized action labels, duration formatting, the case-embed
"Auto-action" line, the failure notice, the DM). The Moderation cog
(``cogs/moderation/moderation.py``) owns the ``/warnings config`` command that
opens the panel and the warn command that fires escalation; it imports this
module. Import direction is one-way: this module imports NOTHING from the
moderation cog (the panel is handed the cog instance at construction time and
calls back into it via ``cog.bot.db_pool``), so there is no cycle.

The escalation vocabulary matches AutoMod's on purpose (an action catalog of
``(value, N_ label, emoji, N_ description)``, the same CV2 container/section
layout, the same ``_rerender`` + ``interactions.refresh_layout`` plumbing) so
the two moderation surfaces read as one product.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the fancy
ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import logging

import discord

from tools import interactions, settings, warn_escalation
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.time import ShortTime
from tools.views import AuthorLayoutView, LocaleModal

log = logging.getLogger(__name__)

# Re-exported bounds so the modal and its callers read one source of truth.
MIN_THRESHOLD = warn_escalation.MIN_THRESHOLD
MAX_THRESHOLD = warn_escalation.MAX_THRESHOLD
MAX_RULES = warn_escalation.MAX_RULES

# ----------------------------------------------------------------------
# Action catalog: what an escalation rule can DO. ``(value, N_ label, emoji,
# N_ description)`` - ``value`` is the stored/enforced action string
# (tools.warn_escalation.VALID_ACTIONS); everything else is presentation. Marked
# with N_ (extracted, stored in English) and translated at render with _(...),
# the same mark-then-translate-at-use pattern as automod_panel.ACTION_CHOICES.
# ----------------------------------------------------------------------
ACTION_CHOICES = [
    (
        warn_escalation.TIMEOUT,
        N_("Timeout"),
        "🔇",
        N_("Time the member out (default 10 minutes, up to 28 days)."),
    ),
    (
        warn_escalation.KICK,
        N_("Kick"),
        "👢",
        N_("Kick the member from the server."),
    ),
    (
        warn_escalation.BAN,
        N_("Ban"),
        "🔨",
        N_("Ban the member from the server."),
    ),
]

_ACTION_LABELS = {value: label for value, label, _emoji, _desc in ACTION_CHOICES}
_ACTION_EMOJI = {value: emoji for value, _label, emoji, _desc in ACTION_CHOICES}


# ----------------------------------------------------------------------
# Presentation helpers (shared with the Moderation cog's warn command)
# ----------------------------------------------------------------------
def format_duration(seconds):
    """A localized, human timeout duration like '10 minute(s)' / '2 hour(s)'.

    Uses the codebase's '(s)' plural convention (as in 'warn(s)') rather than
    ngettext, keeping the msgid surface to three strings. Picks the coarsest
    exact unit (whole days, then whole hours, else minutes).
    """
    if seconds % 86400 == 0:
        return _("{n} day(s)").format(n=seconds // 86400)
    if seconds % 3600 == 0:
        return _("{n} hour(s)").format(n=seconds // 3600)
    return _("{n} minute(s)").format(n=max(1, seconds // 60))


def action_label(action):
    """The localized display label for an action value (timeout/kick/ban)."""
    return _(_ACTION_LABELS.get(action, action))


def escalation_summary(count, rule):
    """The case-embed 'Auto-action' line for a fired rule (mirrors the historical
    'Reached 3 warns - kicked')."""
    action = rule["action"]
    if action == warn_escalation.TIMEOUT:
        return _("Reached {count} warns - timed out for {duration}").format(
            count=count, duration=format_duration(rule["duration"])
        )
    if action == warn_escalation.BAN:
        return _("Reached {count} warns - banned").format(count=count)
    return _("Reached {count} warns - kicked").format(count=count)


def escalation_failure_notice(member_mention, count, rule):
    """A clear notice when a fired rule's action could not be applied (hierarchy
    or missing permissions); the warn itself is still recorded."""
    action = rule["action"]
    if action == warn_escalation.TIMEOUT:
        return _(
            "{member} reached {count} warns, but I couldn't time them out "
            "(check my permissions and role position)."
        ).format(member=member_mention, count=count)
    if action == warn_escalation.BAN:
        return _(
            "{member} reached {count} warns, but I couldn't ban them (check my "
            "permissions and role position)."
        ).format(member=member_mention, count=count)
    return _(
        "{member} reached {count} warns, but I couldn't kick them (check my "
        "permissions and role position)."
    ).format(member=member_mention, count=count)


def escalation_dm(guild_name, count, rule):
    """The best-effort DM sent to a member an escalation action was applied to."""
    action = rule["action"]
    if action == warn_escalation.TIMEOUT:
        return _(
            "You reached {count} warns in {guild} and have been timed out for "
            "{duration}."
        ).format(count=count, guild=guild_name, duration=format_duration(rule["duration"]))
    if action == warn_escalation.BAN:
        return _("You reached {count} warns in {guild} and have been banned.").format(
            count=count, guild=guild_name
        )
    return _("You reached {count} warns in {guild} and have been kicked.").format(
        count=count, guild=guild_name
    )


def _rule_detail(rule):
    """The emoji + action (+ duration for timeout) fragment for one rule."""
    action = rule["action"]
    fragment = f"{_ACTION_EMOJI.get(action, '')} {action_label(action)}".strip()
    if action == warn_escalation.TIMEOUT:
        return _("{action} for {duration}").format(
            action=fragment, duration=format_duration(rule["duration"])
        )
    return fragment


def _parse_duration_seconds(text):
    """Parse a duration string ('10m', '1h', '1d', ...) into clamped seconds.

    Returns ``None`` when the text is unparseable or non-positive, so the caller
    can show a friendly error. On success the value is clamped into Discord's
    allowed timeout range (tools.warn_escalation.clamp_timeout).
    """
    now = discord.utils.utcnow()
    try:
        dt = ShortTime(text, now=now).dt
    except Exception:
        return None
    seconds = int((dt - now).total_seconds())
    if seconds <= 0:
        return None
    return warn_escalation.clamp_timeout(seconds)


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
# Interactive components (discord.ui)
# ----------------------------------------------------------------------
class _ActionSelect(discord.ui.Select):
    """Pick the action the next added/updated rule will take."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            placeholder=_("Action for the next rule..."),
            min_values=1,
            max_values=1,
            options=_action_options(panel.state["pending_action"]),
        )

    async def callback(self, interaction):
        await self.panel.set_pending_action(interaction, self.values[0])


class _AddRuleButton(discord.ui.Button):
    """Open the modal that adds (or updates) a rule at a chosen threshold."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label=_("Add / update rule"),
            emoji="\N{HEAVY PLUS SIGN}",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction):
        await interaction.response.send_modal(_AddRuleModal(self.panel))


class _ResetButton(discord.ui.Button):
    """Restore the built-in default policy (kick at 3 warns)."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label=_("Reset to default"),
            emoji="\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction):
        await self.panel.reset_default(interaction)


class _RemoveRuleSelect(discord.ui.Select):
    """List every configured rule so the admin can pick one to delete.

    One option per rule; the policy is capped at MAX_RULES (10), well inside
    Discord's 25-option select limit, so no truncation is needed.
    """

    def __init__(self, panel):
        self.panel = panel
        options = [
            discord.SelectOption(
                label=_("At {n} warns").format(n=rule["threshold"])[:100],
                value=str(rule["threshold"]),
                description=_rule_detail(rule)[:100],
                emoji=_ACTION_EMOJI.get(rule["action"]),
            )
            for rule in panel.state["policy"]
        ]
        super().__init__(
            placeholder=_("Remove a rule..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await self.panel.remove_rule(interaction, int(self.values[0]))


class _AddRuleModal(LocaleModal):
    """Collect the threshold (and, for a timeout, the duration) for a new rule.

    The action itself is chosen on the panel's action select before this modal
    opens (Discord modals take text inputs, not selects), so the modal only
    needs the number of warns and an optional timeout duration.
    """

    def __init__(self, panel):
        super().__init__(title=_("Add or update a warn rule")[:45])
        self.panel = panel

        self.threshold_input = discord.ui.TextInput(
            placeholder=_("Number of warns, e.g. 3"),
            required=True,
            max_length=2,
        )
        self.add_item(
            discord.ui.Label(
                text=_("Number of warns ({min}-{max})").format(
                    min=MIN_THRESHOLD, max=MAX_THRESHOLD
                ),
                component=self.threshold_input,
            )
        )

        self.duration_input = discord.ui.TextInput(
            placeholder=_("e.g. 10m, 1h, 1d (timeout only; blank = 10 minutes)"),
            required=False,
            max_length=16,
        )
        self.add_item(
            discord.ui.Label(
                text=_("Timeout duration"), component=self.duration_input
            )
        )

    async def on_submit(self, interaction):
        raw_threshold = (self.threshold_input.value or "").strip()
        try:
            threshold = int(raw_threshold)
        except ValueError:
            return await interactions.notify_failure(
                interaction, _("The number of warns must be a whole number.")
            )
        if not (MIN_THRESHOLD <= threshold <= MAX_THRESHOLD):
            return await interactions.notify_failure(
                interaction,
                _("The number of warns must be between {min} and {max}.").format(
                    min=MIN_THRESHOLD, max=MAX_THRESHOLD
                ),
            )

        action = self.panel.state["pending_action"]
        duration = None
        if action == warn_escalation.TIMEOUT:
            raw_duration = (self.duration_input.value or "").strip()
            if raw_duration:
                duration = _parse_duration_seconds(raw_duration)
                if duration is None:
                    return await interactions.notify_failure(
                        interaction,
                        _(
                            "I couldn't read that timeout duration - try "
                            "something like \"10m\", \"1h\", or \"1d\"."
                        ),
                    )
            else:
                duration = warn_escalation.DEFAULT_TIMEOUT_SECONDS

        await self.panel.add_rule(interaction, threshold, action, duration)


# ----------------------------------------------------------------------
# The panel
# ----------------------------------------------------------------------
class WarnConfigPanel(AuthorLayoutView):
    """Author-restricted Components V2 panel for a guild's warn-escalation rules.

    A single :class:`~discord.ui.Container` in the house style (AutoMod / level
    config panels): a header, the current rules listed as ``threshold -> action``,
    a one-line note on the equals-threshold firing semantics, then one control
    row each for the action select, the add/reset buttons and the remove select.

    Text-budget math (Components V2 caps combined TextDisplay text at 4000
    characters). The fixed chrome - header, semantics note, footer - is roughly
    600 characters. The only variable content is the rules list, hard-capped at
    MAX_RULES (10) rules of at most ~60 characters each (~600). Worst case is
    therefore ~1200, a wide ~2800 under the ceiling, so this panel needs no
    truncation logic (like the AutoMod panel, unlike the help menu).

    The panel is born CV2 and stays ``view=``-only for its whole life (a
    Components V2 message carries its content inside the view and Discord rejects
    an ``embed=`` on such an edit).
    """

    def __init__(self, cog, guild, author_id, state, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.state = state
        self._build()

    # -- rendering ------------------------------------------------------
    def _rules_text(self):
        policy = self.state["policy"]
        if not policy:
            return _(
                "**No escalation rules.** Warns are recorded, but no automatic "
                "action is taken. Add a rule below, or reset to the default."
            )
        lines = []
        for rule in policy:
            lines.append(
                _("**At {n} warns** -> {detail}").format(
                    n=rule["threshold"], detail=_rule_detail(rule)
                )
            )
        text = "\n".join(lines)
        if policy == warn_escalation.default_policy():
            text += "\n-# " + _("This is the default (kick at 3 warns).")
        return text

    def _build(self):
        """(Re)assemble the layout from the current state."""
        container = discord.ui.Container(accent_colour=random_colour())

        container.add_item(
            discord.ui.TextDisplay(
                "### \N{WARNING SIGN} "
                + _("Warn escalation - {guild}").format(guild=self.guild.name)
                + "\n-# "
                + _(
                    "Decide what happens as a member piles up warns: at each "
                    "threshold Yasuho can time them out, kick, or ban them."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Current rules") + "**\n" + self._rules_text()
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _(
                    "A rule fires the moment a member's warn count lands exactly "
                    "on its threshold, so editing rules never re-punishes past "
                    "warns. Thresholds are unique; up to {max} rules."
                ).format(max=MAX_RULES)
            )
        )
        container.add_item(discord.ui.Separator())

        # Control rows.
        container.add_item(discord.ui.ActionRow(_ActionSelect(self)))
        container.add_item(
            discord.ui.ActionRow(_AddRuleButton(self), _ResetButton(self))
        )
        if self.state["policy"]:
            container.add_item(discord.ui.ActionRow(_RemoveRuleSelect(self)))

        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " \N{MIDDLE DOT} "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    # -- refresh --------------------------------------------------------
    async def _rerender(self, interaction):
        """Rebuild a fresh panel from current state and show it in place."""
        new = WarnConfigPanel(self.cog, self.guild, self.author_id, self.state)
        new.message = self.message
        self.stop()
        await interactions.refresh_layout(
            interaction, self.message, new, surface="warn config panel"
        )

    async def _safe_fail(self, interaction):
        await interactions.notify_failure(
            interaction, _("Something went wrong updating the panel.")
        )

    async def _persist(self, policy):
        """Write the policy to guild settings and update in-memory state."""
        await settings.set_guild(
            self.cog.bot.db_pool,
            self.guild.id,
            warn_escalation.SETTINGS_KEY,
            policy,
        )
        self.state["policy"] = policy

    # -- callbacks ------------------------------------------------------
    async def set_pending_action(self, interaction, value):
        try:
            if value not in warn_escalation.VALID_ACTIONS:
                value = warn_escalation.TIMEOUT
            self.state["pending_action"] = value
            await self._rerender(interaction)
        except Exception:
            log.exception("Warn config panel action select failed")
            await self._safe_fail(interaction)

    async def add_rule(self, interaction, threshold, action, duration):
        try:
            try:
                new_policy = warn_escalation.upsert_rule(
                    self.state["policy"], threshold, action, duration
                )
            except ValueError:
                # The threshold range is pre-validated in the modal and the
                # action comes from the select, so the only reachable ValueError
                # here is the rule cap (adding a genuinely new threshold at MAX).
                return await interactions.notify_failure(
                    interaction,
                    _(
                        "This server already has the maximum of {max} "
                        "escalation rules."
                    ).format(max=MAX_RULES),
                )
            await self._persist(new_policy)
            await self._rerender(interaction)
        except Exception:
            log.exception("Warn config panel add-rule failed")
            await self._safe_fail(interaction)

    async def remove_rule(self, interaction, threshold):
        try:
            new_policy = warn_escalation.remove_threshold(
                self.state["policy"], threshold
            )
            await self._persist(new_policy)
            await self._rerender(interaction)
        except Exception:
            log.exception("Warn config panel remove-rule failed")
            await self._safe_fail(interaction)

    async def reset_default(self, interaction):
        try:
            await self._persist(warn_escalation.default_policy())
            await self._rerender(interaction)
        except Exception:
            log.exception("Warn config panel reset failed")
            await self._safe_fail(interaction)
