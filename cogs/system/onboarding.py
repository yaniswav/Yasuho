"""Posts a one-time onboarding card when Yasuho joins a new guild.

This is deliberately a cog of its own rather than more code inside
cogs/system/events.py: the join listener there restores retention state (cancel
a scheduled purge, refill the startup caches) and must never be delayed or
broken by a cosmetic greeting. discord.py dispatches every registered listener
for an event, so the two live side by side and fail independently.

The "already greeted" marker is a single key in the per-guild settings blob
(``tools.settings``, table ``guild_settings``), so it costs one cached read per
join and is purged with the rest of the guild's data when retention collects a
departed guild.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from tools import i18n, interactions, settings
from tools.i18n import N_, _
from tools.views import LocaleDynamicItem, LocaleLayoutView

log = logging.getLogger(__name__)

PANEL_ACCENT_DEFAULT = 0x5865F2

# Re-post the card if the last one is older than this. A guild that kicked the
# bot and invited it back months later gets a fresh first impression, while the
# repeated GUILD_CREATE a gateway outage can produce (or a kick/re-invite the
# same day) never double-posts.
REPOST_AFTER = timedelta(days=30)

# Fallback channel names, in PRIORITY order, tried when the guild has no usable
# system channel and before the "first writable text channel" last resort.
# Ported from the retired plain-text welcome message, which searched them as an
# unordered set (first match by channel position); here "general" really does
# beat "bots" whatever the sidebar order. Non-ASCII entries are real Discord
# channel names (Discord allows accents), not prose.
_CANDIDATE_NAMES = (
    "general",
    "général",
    "lobby",
    "chat",
    "welcome",
    "bienvenue",
    "commands",
    "cmds",
    "hub",
    "arrival",
    "command",
    "bots-commands",
    "bots",
)
_CANDIDATE_SET = frozenset(_CANDIDATE_NAMES)

_MARKER_KEY = "onboarding_card_posted_at"


def _is_writable(channel, me):
    """True if we may both SEE and post in ``channel``.

    ``send_messages`` alone is not enough: a channel hidden from the bot still
    reports it, and posting there would be a 403 (or worse, a card nobody can
    read), so ``view_channel`` is checked too.
    """

    perms = channel.permissions_for(me)
    return perms.send_messages and perms.view_channel


def _pick_channel(guild):
    """Pick the best channel to post the card in, or None if none qualifies.

    Order: the system channel, then :data:`_CANDIDATE_NAMES` by priority, then
    the first writable text channel. Every candidate is permission-checked, so
    an unwritable "general" falls through to the next name instead of aborting
    the search. One pass over ``guild.text_channels`` collects both the
    per-name winners (a dict bounded by the candidate list, 13 entries) and the
    overall first writable channel, so this stays O(channels) with a single
    ``permissions_for`` per channel rather than one per candidate name.
    """

    me = guild.me
    if me is None:
        # Only possible mid-outage / before the member cache has us; the next
        # join event (or nothing at all) is better than a crash here.
        return None

    system = guild.system_channel
    if system is not None and _is_writable(system, me):
        return system

    by_name = {}
    first_writable = None
    for channel in guild.text_channels:
        if not _is_writable(channel, me):
            continue
        if first_writable is None:
            first_writable = channel
        name = channel.name
        if name in _CANDIDATE_SET and name not in by_name:
            by_name[name] = channel

    for name in _CANDIDATE_NAMES:
        channel = by_name.get(name)
        if channel is not None:
            return channel

    return first_writable


def _marker_is_recent(marker, *, now=None):
    """True if ``marker`` (an ISO timestamp) is inside the no-repost window.

    An unreadable or missing marker is treated as "not recent" so a new guild
    still gets greeted rather than being silently skipped forever by one bad
    value. A marker in the FUTURE (clock skew, a hand-edited blob) counts as
    recent: when in doubt, staying quiet beats double-posting. Naive values are
    read as UTC, so a legacy or hand-written marker can never raise the
    naive-vs-aware TypeError.
    """

    if not marker:
        return False
    try:
        posted = datetime.fromisoformat(marker)
    except (TypeError, ValueError):
        return False
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - posted) < REPOST_AFTER


# custom_id namespace of the card's buttons: ``onb:<kind>``. The kind is the only
# thing the id carries - the guild always comes from the click itself
# (``interaction.guild``), never from a value a client could have edited.
CUSTOM_ID_PREFIX = "onb:"
_KIND_PATTERN = r"alerts|settings|help"
BUTTON_TEMPLATE = r"onb:(?P<kind>" + _KIND_PATTERN + r")"

# kind -> (label msgid, button style, needs Manage Server).
_BUTTONS = {
    "alerts": (N_("Set up anime alerts"), discord.ButtonStyle.primary, True),
    "settings": (N_("Server settings"), discord.ButtonStyle.secondary, True),
    "help": (N_("Help"), discord.ButtonStyle.secondary, False),
}


async def _open_alerts(interaction):
    from cogs.anilist.alerts_wizard import open_anime_alerts_wizard

    await open_anime_alerts_wizard(interaction)


async def _open_settings(interaction):
    from cogs.config.settings import open_config_panel

    await open_config_panel(interaction)


async def _open_help(interaction):
    from cogs.system.help import open_help_home

    await open_help_home(interaction)


_OPENERS = {"alerts": _open_alerts, "settings": _open_settings, "help": _open_help}


class OnboardingButton(LocaleDynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """One of the card's persistent buttons.

    A DynamicItem, so a click is matched on its custom_id against the template
    registered at load: it works on a card posted before a restart and long after
    the card's own view is gone (the view has no timeout and retains nothing).
    The clicker's locale is applied by :class:`~tools.views.LocaleDynamicItem`
    before the callback, so every response (all ephemeral) speaks the clicker's
    language; the card itself was rendered once in the guild's language.
    """

    def __init__(self, kind):
        self.kind = kind
        label, style, _manager = _BUTTONS[kind]
        super().__init__(
            discord.ui.Button(
                label=_(label), style=style, custom_id=CUSTOM_ID_PREFIX + kind
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"])

    async def callback(self, interaction):
        if interaction.guild is None:
            return await interactions.reply(
                interaction, _("This only works in a server.")
            )
        _label, _style, needs_manager = _BUTTONS[self.kind]
        # Checked here, on the live click: the card is public and anyone can
        # press it, so the button must not trust where it was posted.
        if needs_manager and not interaction.user.guild_permissions.manage_guild:
            return await interactions.reply(
                interaction,
                _("You need the Manage Server permission to use this button."),
            )
        await _OPENERS[self.kind](interaction)


class OnboardingCardView(LocaleLayoutView):
    """Single-page Components V2 card: a short pitch and three persistent buttons.

    ``timeout=None``: the buttons are DynamicItems (see :class:`OnboardingButton`),
    so discord.py retains no per-message entry and starts no timeout task, and
    there is no expiry to disable them. ``with_buttons=False`` is the owner-DM
    variant: the buttons act on a server, which a DM does not have.
    """

    def __init__(self, guild, *, with_buttons=True, timeout=None):
        super().__init__(timeout=timeout)
        self._build(guild, with_buttons)

    def _build(self, guild, with_buttons):
        container = discord.ui.Container(accent_colour=PANEL_ACCENT_DEFAULT)

        container.add_item(
            discord.ui.TextDisplay(
                "## \U0001F33A "
                + _("Hello, {guild}! I'm Yasuho.").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        lines = [
            _("I'm the bot for anime and manga servers."),
            "",
            _(
                "- **Anime & manga**: AniList feed, episode and chapter "
                "alerts, watch-alongs."
            ),
            _("- **Music**: queues, effects and synced lyrics."),
            _("- **Plus** levels, moderation, tickets and more."),
        ]
        if with_buttons:
            lines += ["", _("Use the buttons below to get started.")]
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        if with_buttons:
            container.add_item(
                discord.ui.ActionRow(*(OnboardingButton(kind) for kind in _BUTTONS))
            )
        container.add_item(
            discord.ui.TextDisplay(
                "-# " + _("Use `/language` to pick the language I speak in this server.")
            )
        )

        self.add_item(container)


class Onboarding(commands.Cog):
    """Greets a newly-joined guild with a one-time Components V2 promo card."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Process-wide, so the buttons of every card ever posted dispatch,
        # including the ones from before this start.
        self.bot.add_dynamic_items(OnboardingButton)

    def cog_unload(self):
        self.bot.remove_dynamic_items(OnboardingButton)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        try:
            await self._maybe_post_card(guild)
        except Exception:
            # A cosmetic card must never look like a failed join; the retention
            # restore in cogs/system/events.py runs as its own listener.
            log.exception("Onboarding card failed for guild %s", guild.id)

    async def _maybe_post_card(self, guild):
        """Send the card once per guild per :data:`REPOST_AFTER` window.

        The marker is written only AFTER a send that actually succeeded: a
        failed send (missing permission, closed DMs, a 5xx) must not burn the
        one greeting the guild gets, so the next join simply tries again. One
        target, one attempt - no cascading retry, because a send that failed
        after Discord accepted it would otherwise post the card twice.
        """

        pool = self.bot.db_pool
        marker = await settings.get_guild(pool, guild.id, _MARKER_KEY, None)
        if _marker_is_recent(marker):
            log.debug(
                "Onboarding card already posted for guild %s within %s days; "
                "skipping",
                guild.id,
                REPOST_AFTER.days,
            )
            return

        target = _pick_channel(guild)
        in_server = target is not None
        if target is None:
            # Nothing writable: fall back to the owner's DMs. ``guild.owner`` is
            # a cache read and may be None (uncached owner, outage), in which
            # case there is simply nobody to greet.
            target = guild.owner
        if target is None:
            log.info(
                "No writable channel and no reachable owner for guild %s; "
                "skipping onboarding card",
                guild.id,
            )
            return

        loc = await i18n.resolve_guild_locale(self.bot, guild)
        try:
            # Both the render and the send sit inside the locale block; the
            # context manager resets in a finally, so a raised send can never
            # leak this guild's locale into the next event handled by this task.
            with i18n.locale(loc):
                view = OnboardingCardView(guild, with_buttons=in_server)
                await target.send(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
        except discord.HTTPException:
            # Covers Forbidden (closed DMs, a permission that looked fine in
            # cache) and transient 5xx alike. No marker is written, so a later
            # join retries.
            log.warning(
                "Failed to send the onboarding card for guild %s",
                guild.id,
                exc_info=True,
            )
            return

        await settings.set_guild(
            pool, guild.id, _MARKER_KEY, datetime.now(timezone.utc).isoformat()
        )


async def setup(bot):
    await bot.add_cog(Onboarding(bot))
