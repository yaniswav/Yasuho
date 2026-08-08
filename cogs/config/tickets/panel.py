"""The ``Tickets`` cog: the ``/ticket`` admin group and the panel it posts.

Three verbs, the shape ``cogs/config/verification.py`` established for a
button-backed feature:

* ``/ticket setup <channel> [support_role] [log_channel]`` - preflight the bot's
  permissions on that channel, store the options that were NAMED (an omitted one
  keeps its stored value: this command is also how a panel is moved, and moving
  it must not wipe the support role), post the panel, and warn if the support
  role cannot reach the threads it will be pinged in;
* ``/ticket status`` - a read-only Components V2 card of what is configured;
* ``/ticket disable`` - clear the panel channel, which is the feature switch;
  panels already posted stay in place and answer "not set up here" on click,
  because the button reads configuration at click time and never trusts itself.

Nothing here is per-message state: :class:`open.TicketPanelView` is registered
ONCE in :meth:`Tickets.cog_load` and serves every panel in every guild.

Scale story. Opens are user-initiated and rare, and this cog adds no listener, no
timer and no per-message work of any kind: the steady-state cost of the whole
feature at 1000+ guilds is exactly zero until a member clicks. A click costs one
settings read (a shared, LRU-cached JSONB blob - the same one welcome/automod
already pull, so usually free), one indexed COUNT, and one guarded INSERT. The
recurring sweep that lot T2 adds is the first thing here that will run on a
clock, and it is deliberately not in this lot.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import typing

import discord
from discord.ext import commands

from . import guild_config, preflight
from .open import TicketPanelView
from tools import i18n, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.snowflake import coerce_id

log = logging.getLogger(__name__)


class TicketStatusView(discord.ui.LayoutView):
    """Single-page Components V2 card: this guild's ticket configuration.

    Read-only, no controls - the same role ``VerifyStatusView`` plays for
    verification. It renders IDS it can no longer resolve as ``(deleted)``
    rather than as "not set", because "you configured a channel that no longer
    exists" and "you configured nothing" need different fixes.
    """

    def __init__(self, guild, config, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, config)

    def _build(self, guild, config):
        channel = _resolve(guild.get_channel, config["panel_channel"])
        role = _resolve(guild.get_role, config["support_role"])
        log_channel = _resolve(guild.get_channel, config["log_channel"])
        enabled = channel is not None

        status_value = (
            ("\U0001F7E2 " + _("Enabled"))
            if enabled
            else ("\U0001F534 " + _("Disabled"))
        )

        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("Support tickets | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "**Status:** {status}\n"
                    "**Panel channel:** {channel}\n"
                    "**Support role:** {role}\n"
                    "**Log channel:** {log}\n"
                    "**Open tickets per member:** {cap}\n"
                    "**Inactivity window:** {hours}h"
                ).format(
                    status=status_value,
                    channel=_render(channel, config["panel_channel"]),
                    role=_render(role, config["support_role"]),
                    log=_render(log_channel, config["log_channel"]),
                    cap=config["max_open"],
                    hours=config["inactivity_hours"],
                )
            )
        )
        if not enabled:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "-# " + _("Use `/ticket setup` to turn tickets on.")
                )
            )
        self.add_item(container)


def _resolve(getter, raw_id):
    """Look an id up, tolerating the string spellings the dashboard writes."""
    ident = coerce_id(raw_id)
    return getter(ident) if ident else None


def _render(obj, raw_id):
    """Mention it, say it is deleted, or say it was never set."""
    if obj is not None:
        return obj.mention
    ident = coerce_id(raw_id)
    if ident:
        return f"`{ident}` " + _("(deleted)")
    return _("*Not set.*")


class Tickets(commands.Cog):
    """Private support tickets: one button, one thread per request."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # ONE global persistent view handles every guild's panel button.
        try:
            self.bot.add_view(TicketPanelView())
        except Exception:
            log.exception("Failed to register the ticket panel view")

    async def _read_config(self, guild_id):
        """Everything ``/ticket status`` shows, in one settings blob read."""
        pool = self.bot.db_pool
        return {
            "panel_channel": await guild_config.panel_channel_id(pool, guild_id),
            "support_role": await guild_config.support_role_id(pool, guild_id),
            "log_channel": await guild_config.log_channel_id(pool, guild_id),
            "max_open": await guild_config.max_open_per_user(pool, guild_id),
            "inactivity_hours": await guild_config.inactivity_hours(pool, guild_id),
        }

    @commands.hybrid_group(name="ticket")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticket(self, ctx):
        """Manage support tickets: set up the panel, check it or turn it off."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ticket.command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        channel="Where the ticket panel is posted (tickets become threads here).",
        support_role="Role pinged inside every new ticket.",
        log_channel="Where ticket activity is logged.",
        message="A custom message shown on the panel.",
    )
    async def ticket_setup(
        self,
        ctx,
        channel: discord.TextChannel,
        support_role: typing.Optional[discord.Role] = None,
        log_channel: typing.Optional[discord.TextChannel] = None,
        *,
        message: typing.Optional[str] = None,
    ):
        """Post the ticket panel and turn support tickets on."""
        # Preflight BEFORE writing anything: a configuration the bot cannot act
        # on is worse than no configuration, because the panel looks fine and
        # only fails in front of the member who needed help.
        missing = preflight.missing_permissions(
            channel.permissions_for(ctx.guild.me), preflight.SETUP_PERMISSIONS
        )
        if missing:
            return await ctx.send(
                _(
                    "I need these permissions in {channel} first: {permissions}."
                ).format(
                    channel=channel.mention,
                    permissions=preflight.describe(missing),
                )
            )

        pool = self.bot.db_pool

        # ONLY the options the manager actually named are written. `/ticket
        # setup #new-channel` is how a panel is MOVED, and it must not silently
        # wipe the support role, the log channel and the blurb on the way - the
        # same reasoning `/ticket disable` states below: those are the server's
        # choices, not this command's to reset. An omitted option therefore
        # keeps its stored value, which is also why the blurb has to be READ
        # here to render the panel.
        #
        # Clearing one is still possible: the blurb by passing a blank message
        # (coerce_text reads whitespace as "nothing to show"), the role and the
        # log channel from the dashboard, which writes these same keys.
        patches = []
        if support_role is not None:
            patches.append((guild_config.KEY_SUPPORT_ROLE, support_role.id))
        if log_channel is not None:
            patches.append((guild_config.KEY_LOG_CHANNEL, log_channel.id))
        if message is None:
            blurb = await guild_config.panel_message(pool, ctx.guild.id)
        else:
            blurb = guild_config.coerce_text(
                message, limit=guild_config.MAX_PANEL_MESSAGE_LENGTH
            )
            patches.append((guild_config.KEY_PANEL_MESSAGE, blurb))

        # Per-key patches, not one blob write (tools.settings.set_guild
        # jsonb_sets a single key, so a sibling feature's key in the same blob
        # cannot be clobbered). They are NOT one transaction, so the order is
        # the safety: the panel channel - which IS the on switch - is written
        # LAST, so a failure part-way through leaves the feature off with a
        # half-written configuration rather than on with one.
        patches.append((guild_config.KEY_PANEL_CHANNEL, channel.id))
        for key, value in patches:
            await settings.set_guild(pool, ctx.guild.id, key, value)

        # The panel is a PUBLIC, per-guild artifact: everybody in the server
        # reads it, and it outlives this conversation. So its embed and its
        # button label are rendered under the GUILD's locale, not under the
        # locale of whoever happened to type the command - a manager who reads
        # Japanese must not leave a Japanese button on a French server. What
        # this command says BACK to that manager stays in their own language,
        # which is why the block is scoped to exactly these two objects.
        panel_locale = await i18n.resolve_guild_locale(self.bot, ctx.guild)
        with i18n.locale(panel_locale):
            embed = discord.Embed(
                title=_("Support tickets"),
                description=(
                    blurb
                    or _(
                        "Need a hand? Click below and I will open a private thread "
                        "for you and the staff team."
                    )
                ),
                colour=random_colour(),
            )
            view = TicketPanelView()
        try:
            posted = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("tickets: could not post the panel in %s", channel.id)
            return await ctx.send(
                _("I could not post the panel in {channel}.").format(
                    channel=channel.mention
                )
            )

        lines = [
            _("Tickets are on - the panel is up in {channel}. {jump}").format(
                channel=channel.mention, jump=posted.jump_url
            )
        ]
        lines += await self._setup_notes(ctx.guild, channel)
        await ctx.send(
            "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
        )

    async def _setup_notes(self, guild, channel):
        """What setup did NOT change, and the one thing it cannot fix itself.

        Two lines, both about the support role and both earned by a real failure
        mode. The first states the options that were kept, because a command that
        preserves what you did not name has to SAY so or it looks like it forgot
        them. The second is the trap Discord builds into private threads: a role
        mention adds nobody, so staff see a ticket only through ``manage_threads``
        on the parent channel, and a support role without it gets pinged into
        rooms it cannot open. Warned, never refused - the server may add staff
        some other way, and refusing a working setup over a guess would be worse.
        """
        pool = self.bot.db_pool
        role_id = await guild_config.support_role_id(pool, guild.id)
        log_id = await guild_config.log_channel_id(pool, guild.id)
        role = _resolve(guild.get_role, role_id)
        log_channel = _resolve(guild.get_channel, log_id)

        notes = [
            "-# "
            + _(
                "Support role: {role} | Log channel: {log} | setup only changes "
                "the options you name."
            ).format(role=_render(role, role_id), log=_render(log_channel, log_id))
        ]
        if role is None:
            return notes

        missing = preflight.missing_permissions(
            channel.permissions_for(role), preflight.SUPPORT_ROLE_PERMISSIONS
        )
        if missing:
            notes.append(
                _(
                    "Heads up: {role} is missing {permissions} in {channel}. A "
                    "ticket is a private thread, so staff can only see it with "
                    "Manage Threads on the channel - without it they will be "
                    "pinged into tickets they cannot open."
                ).format(
                    role=role.mention,
                    permissions=preflight.describe(missing),
                    channel=channel.mention,
                )
            )
        return notes

    @ticket.command(name="status")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticket_status(self, ctx):
        """Show the current ticket configuration."""
        config = await self._read_config(ctx.guild.id)
        view = TicketStatusView(ctx.guild, config)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @ticket.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticket_disable(self, ctx):
        """Turn tickets off (panels already posted will say so when clicked)."""
        # ONLY the panel channel is cleared. The support role, log channel and
        # blurb are the server's choices, and wiping them would punish a manager
        # who turns the feature off for an afternoon.
        await settings.set_guild(
            self.bot.db_pool, ctx.guild.id, guild_config.KEY_PANEL_CHANNEL, None
        )
        await ctx.send(
            _(
                "Tickets are off. Threads that are already open stay where they "
                "are, and the panel button will say tickets are not set up."
            )
        )
