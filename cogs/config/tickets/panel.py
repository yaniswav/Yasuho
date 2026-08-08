"""The ``Tickets`` cog: the ``/ticket`` admin group and the panel it posts.

Four verbs, the shape ``cogs/config/verification.py`` established for a
button-backed feature:

* ``/ticket setup <channel> [support_role] [log_channel]`` - preflight the bot's
  permissions on that channel, store the options that were NAMED (an omitted one
  keeps its stored value: this command is also how a panel is moved, and moving
  it must not wipe the support role), post the panel, and warn if the support
  role cannot reach the threads it will be pinged in;
* ``/ticket status`` - a read-only Components V2 card of what is configured;
* ``/ticket config`` - the same six keys, EDITABLE: an author-gated Components V2
  panel (:class:`TicketConfigPanel`) with a picker per key and a reset per key.
  It is the graphical twin of the dashboard's tickets screen and writes exactly
  what the dashboard writes, key by key, resets included (a reset DELETES the
  key - see ``guild_config.set_key``);
* ``/ticket disable`` - clear the panel channel, which is the feature switch;
  panels already posted stay in place and answer "not set up here" on click,
  because the button reads configuration at click time and never trusts itself.

What the LOG CHANNEL really is, and why both surfaces say "transcript". It is
where a CLOSED ticket is logged, and that log line carries the thread's
conversation as an uploaded text file (lifecycle.py). "Ticket activity" would be
a comfortable label for a control that copies a private conversation into a
channel of the manager's choosing, so the copy names the file - and both
surfaces WARN when the chosen channel is readable by everyone, because that
choice publishes the transcript to the whole server. Warned, never refused: a
server is allowed to keep a public ticket log, it is just not allowed to do so
without being told.

Division of labour between ``setup`` and ``config``: ``setup`` is the one that
POSTS a panel message in a channel, ``config`` only edits stored keys. Pointing
the panel channel somewhere else from ``config`` therefore moves where tickets
are allowed to open WITHOUT posting a new panel there, and the panel says so
rather than leaving a manager hunting for a message that was never sent.

Nothing here is per-message state: :class:`open.TicketPanelView` is registered
ONCE in :meth:`Tickets.cog_load` and serves every panel in every guild.

Scale story. Opens are user-initiated and rare, and this cog adds no listener, no
timer and no per-message work of any kind: the steady-state cost of the whole
feature at 1000+ guilds is exactly zero until a member clicks. A click costs one
settings read (a shared, LRU-cached JSONB blob - the same one welcome/automod
already pull, so usually free), one indexed COUNT, and one guarded INSERT. The
one clock in the whole feature is the hourly backstop sweep, and it lives in the
OTHER cog (lifecycle.py) precisely so this one stays free until somebody clicks.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import typing

import discord
from discord.ext import commands

from . import guild_config, preflight
from .open import TicketPanelView
from tools import i18n, interactions
from tools.formats import random_colour
from tools.i18n import _
from tools.snowflake import coerce_id
from tools.views import AuthorLayoutView, LocaleModal

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
        notes = []
        if log_channel is not None:
            notes.append(
                _("Closed tickets are logged in {channel}, transcript attached.")
                .format(channel=log_channel.mention)
            )
            caution = _log_caution(log_channel)
            if caution:
                notes.append(caution)
        if not enabled:
            notes.append(_("Use `/ticket setup` to turn tickets on."))
        if notes:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "\n".join("-# " + note for note in notes)
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


def _is_public(channel):
    """Can @everyone read this channel? Defensive - unknown answers ``False``.

    Asked of the LOG channel, because a ticket transcript posted there is the
    conversation of a private thread: in a public channel that publishes it to
    the whole server. Anything this cannot evaluate (a partial channel, no
    guild, a permissions lookup that raises) answers "not public": the caution
    it feeds is advice, and advice nobody can verify is noise.
    """
    guild = getattr(channel, "guild", None)
    everyone = getattr(guild, "default_role", None)
    if channel is None or everyone is None:
        return False
    try:
        return bool(channel.permissions_for(everyone).view_channel)
    except Exception:  # pragma: no cover - a fake or a partial channel
        return False


def _log_caution(channel):
    """The one warning a log channel can earn, or ``None``.

    Never a refusal. A server is allowed to log its tickets in public; it is not
    allowed to do so without being told, because the transcript is somebody
    else's conversation.
    """
    if not _is_public(channel):
        return None
    return _(
        "Heads up: everyone can read {channel}, and a closed ticket is logged "
        "there with the full transcript of its thread. Pick a staff-only "
        "channel if that is not what you want."
    ).format(channel=channel.mention)


# ---------------------------------------------------------------------------
# /ticket config: the editable twin of the status card.
# ---------------------------------------------------------------------------

# The select value that means "reset this key". A sentinel string, never a
# number, so it can never collide with a real choice.
RESET_VALUE = "default"

# How much of a stored blurb the panel previews. The blurb itself may be up to
# guild_config.MAX_PANEL_MESSAGE_LENGTH, which would dominate the card.
BLURB_PREVIEW_LENGTH = 100

# The channel types a ticket panel can live on. Threads are created ON this
# channel, so it has to be a real text channel - offering a category or a voice
# channel would only let a manager pick something the preflight then refuses.
_PANEL_CHANNEL_TYPES = [discord.ChannelType.text]


def _preview_blurb(text):
    """One-line, length-bounded preview of a stored panel blurb.

    Newlines are folded to spaces because the preview sits inside a line of the
    card, and a blurb with a line break would otherwise reflow the layout around
    it. NOT escaped: the card is always edited with mentions suppressed (see
    :meth:`TicketConfigPanel._rerender`), so the worst a blurb can do here is
    look bold.
    """
    flat = " ".join(text.split())
    if len(flat) <= BLURB_PREVIEW_LENGTH:
        return flat
    return flat[:BLURB_PREVIEW_LENGTH] + "..."


class _PanelChannelSelect(discord.ui.ChannelSelect):
    """Where tickets may be opened - and the feature switch.

    ``min_values=0``: deselecting is how the key is RESET, which for this key
    means turning tickets off, exactly as ``/ticket disable`` does.
    """

    def __init__(self, panel, defaults):
        self.panel = panel
        super().__init__(
            channel_types=_PANEL_CHANNEL_TYPES,
            placeholder=_("Panel channel (clear to turn tickets off)..."),
            min_values=0,
            max_values=1,
            default_values=defaults,
        )

    async def callback(self, interaction):
        await self.panel.set_panel_channel(
            interaction, self.values[0] if self.values else None
        )


class _SupportRoleSelect(discord.ui.RoleSelect):
    """The role pinged inside a new ticket. Optional: clearing resets the key."""

    def __init__(self, panel, defaults):
        self.panel = panel
        super().__init__(
            placeholder=_("Support role (clear for none)..."),
            min_values=0,
            max_values=1,
            default_values=defaults,
        )

    async def callback(self, interaction):
        await self.panel.set_support_role(
            interaction, self.values[0] if self.values else None
        )


class _LogChannelSelect(discord.ui.ChannelSelect):
    """Where closed tickets are logged, WITH their transcript attached.

    Optional: clearing resets the key, and a server with no log channel gets no
    transcript at all. The placeholder says "transcripts" rather than "activity"
    because that is what this control really decides - a private conversation
    gets copied into the channel picked here, so picking a public one publishes
    it. The card above the picker warns when the chosen channel is one everybody
    can read; it never refuses, because a server may well want its ticket log
    where its members can see it.
    """

    def __init__(self, panel, defaults):
        self.panel = panel
        super().__init__(
            channel_types=_PANEL_CHANNEL_TYPES,
            placeholder=_("Log channel: closed tickets + transcripts..."),
            min_values=0,
            max_values=1,
            default_values=defaults,
        )

    async def callback(self, interaction):
        await self.panel.set_log_channel(
            interaction, self.values[0] if self.values else None
        )


def _reset_option(label):
    """The leading "back to the bot default" option every count select carries."""
    return discord.SelectOption(
        label=label,
        value=RESET_VALUE,
        description=_("Clears the setting and follows the bot default."),
    )


class _MaxOpenSelect(discord.ui.Select):
    """How many tickets one member may have open at once (1..5, or default)."""

    def __init__(self, panel, current, configured):
        self.panel = panel
        options = [
            _reset_option(
                _("Default ({count})").format(
                    count=guild_config.DEFAULT_MAX_OPEN_PER_USER
                )
            )
        ]
        options[0].default = not configured
        for count in range(
            guild_config.MIN_OPEN_PER_USER, guild_config.MAX_OPEN_PER_USER + 1
        ):
            options.append(
                discord.SelectOption(
                    label=_("{count} at a time").format(count=count),
                    value=str(count),
                    default=configured and count == current,
                )
            )
        super().__init__(
            placeholder=_("Open tickets per member..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await self.panel.set_count(
            interaction, guild_config.KEY_MAX_OPEN_PER_USER, self.values[0]
        )


class _InactivitySelect(discord.ui.Select):
    """How long a ticket may sit idle before it is swept closed."""

    def __init__(self, panel, current, configured):
        self.panel = panel
        options = [
            _reset_option(
                _("Default ({hours}h)").format(
                    hours=guild_config.DEFAULT_INACTIVITY_HOURS
                )
            )
        ]
        options[0].default = not configured
        for hours in guild_config.INACTIVITY_PRESET_HOURS:
            options.append(
                discord.SelectOption(
                    label=_("{hours}h").format(hours=hours),
                    value=str(hours),
                    default=configured and hours == current,
                )
            )
        super().__init__(
            placeholder=_("Close a ticket after this much silence..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await self.panel.set_count(
            interaction, guild_config.KEY_INACTIVITY_HOURS, self.values[0]
        )


class _PanelMessageModal(LocaleModal):
    """Edit the blurb shown above the panel button. Blank submit = reset."""

    def __init__(self, panel):
        super().__init__(title=_("Panel message"))
        self.panel = panel
        self.field = discord.ui.TextInput(
            label=_("Shown above the ticket button"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=guild_config.MAX_PANEL_MESSAGE_LENGTH,
            default=panel.state["panel_message"] or None,
            placeholder=_("Leave empty to use the default wording."),
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        await self.panel.set_blurb(interaction, self.field.value)


class _EditMessageButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(
            label=_("Edit panel message"), style=discord.ButtonStyle.secondary
        )
        self.panel = panel

    async def callback(self, interaction):
        await interaction.response.send_modal(_PanelMessageModal(self.panel))


class _ResetMessageButton(discord.ui.Button):
    def __init__(self, panel, *, disabled):
        super().__init__(
            label=_("Reset message"),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.panel = panel

    async def callback(self, interaction):
        await self.panel.set_blurb(interaction, None)


class TicketConfigPanel(AuthorLayoutView):
    """Author-restricted Components V2 panel over the six ``tickets_*`` keys.

    The house admin-panel shape (``AutoModPanel``, ``SeasonsPanel``,
    ``ProfileVisibilityPanel``): a single :class:`~discord.ui.Container` with a
    header, an overview block, then one control row per key. Every write goes
    through ``guild_config.set_key`` - this class owns no query of its own, only
    the layout and the callbacks.

    Two rules it exists to make visible, both borrowed from the dashboard
    contract this panel is the twin of:

    * **absent means default.** The panel is built from the RAW key map, not from
      the coerced values, so it can mark a value the server actually chose apart
      from one it is merely inheriting - and so the "Default" option of each
      select can start selected on a guild that configured nothing.
    * **a reset DELETES the key.** Clearing a picker, or choosing "Default", does
      not store a neutral value: it removes the key, leaving the guild
      indistinguishable from one that never opened this panel.

    The panel channel is the one control that can REFUSE: it runs the same
    ``preflight`` ``/ticket setup`` runs, because a channel the bot cannot create
    private threads in is a configuration that only fails later, in front of the
    member who needed help.

    Scale story: admin-frequency, no listener, no timer, no per-message work. One
    settings read to open (a cached blob), one statement per edit plus a cache
    eviction, and one cached re-read to redraw. Nothing here scales with guild
    count beyond the panels a human has open right now.

    The deny wording matches AuthorLayoutView's default ("This panel isn't for
    you."), so it is left unset here (same note as AutoModPanel's).
    """

    def __init__(self, cog, guild, author_id, raw, *, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self._load_state(raw)
        self._build()

    # -- state ----------------------------------------------------------
    def _load_state(self, raw):
        """Adopt a raw key map: keep it AND its coerced view side by side."""
        self.raw = dict(raw) if isinstance(raw, dict) else {}
        self.state = guild_config.resolve(self.raw)

    def _is_set(self, key):
        """Did this guild actually write ``key``? (absent -> the bot default)"""
        return self.raw.get(key) is not None

    # -- rendering ------------------------------------------------------
    def _overview(self):
        state = self.state
        channel = _resolve(self.guild.get_channel, state["panel_channel"])
        role = _resolve(self.guild.get_role, state["support_role"])
        log_channel = _resolve(self.guild.get_channel, state["log_channel"])
        blurb = state["panel_message"]
        return _(
            "**Status:** {status}\n"
            "**Panel channel:** {channel}\n"
            "**Support role:** {role}\n"
            "**Log channel:** {log}\n"
            "**Open tickets per member:** {cap}\n"
            "**Inactivity window:** {hours}\n"
            "**Panel message:** {message}"
        ).format(
            status=(
                ("\U0001F7E2 " + _("Enabled"))
                if channel is not None
                else ("\U0001F534 " + _("Disabled"))
            ),
            channel=_render(channel, state["panel_channel"]),
            role=_render(role, state["support_role"]),
            log=_render(log_channel, state["log_channel"]),
            cap=self._value_note(
                str(state["max_open"]), guild_config.KEY_MAX_OPEN_PER_USER
            ),
            hours=self._value_note(
                _("{hours}h").format(hours=state["inactivity_hours"]),
                guild_config.KEY_INACTIVITY_HOURS,
            ),
            message=(
                _preview_blurb(blurb) if blurb else _("*Default wording.*")
            ),
        )

    def _value_note(self, rendered, key):
        """Tag a value the guild is INHERITING rather than one it chose."""
        if self._is_set(key):
            return rendered
        return _("{value} (default)").format(value=rendered)

    def _build(self):
        self.clear_items()
        state = self.state
        container = discord.ui.Container(accent_colour=random_colour())

        container.add_item(
            discord.ui.TextDisplay(
                "### \U0001F3AB "
                + _("Support tickets - {guild}").format(guild=self.guild.name)
                + "\n-# "
                + _(
                    "Every setting below is optional: clear a picker, or choose "
                    "Default, and Yasuho goes back to her own default for it."
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._overview()))
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.ActionRow(
                _PanelChannelSelect(
                    self,
                    _defaults(self.guild.get_channel, state["panel_channel"]),
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _SupportRoleSelect(
                    self, _defaults(self.guild.get_role, state["support_role"])
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _LogChannelSelect(
                    self,
                    _defaults(self.guild.get_channel, state["log_channel"]),
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _MaxOpenSelect(
                    self,
                    state["max_open"],
                    self._is_set(guild_config.KEY_MAX_OPEN_PER_USER),
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _InactivitySelect(
                    self,
                    state["inactivity_hours"],
                    self._is_set(guild_config.KEY_INACTIVITY_HOURS),
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                _EditMessageButton(self),
                _ResetMessageButton(
                    self, disabled=state["panel_message"] is None
                ),
            )
        )

        container.add_item(discord.ui.Separator())
        footer = [
            _(
                "Changing the panel channel here does not post a panel there "
                "- run `/ticket setup` for that."
            ),
            _(
                "A closed ticket is logged with the full transcript of its "
                "thread; with no log channel, no transcript is kept at all."
            ),
        ]
        caution = _log_caution(_resolve(self.guild.get_channel, state["log_channel"]))
        if caution:
            footer.append(caution)
        container.add_item(
            discord.ui.TextDisplay(
                "\n".join("-# " + line for line in footer)
                + "\n-# "
                + _("Only you can use these controls")
                + " - "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    # -- plumbing -------------------------------------------------------
    async def _rerender(self, interaction):
        """Redraw in place, NEVER notifying: the card is full of mentions.

        A Components V2 edit resends every TextDisplay, and this one holds the
        panel channel, the support role and the log channel as live mentions - so
        an unsuppressed refresh would ping the support role on every click. Same
        reasoning as ``AuthorLayoutView.on_timeout``.
        """
        await interactions.refresh_layout(
            interaction,
            self.message,
            self,
            surface="ticket config panel",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _write(self, interaction, key, value):
        """Persist ONE key (``None`` deletes it), then redraw from the truth."""
        pool = self.cog.bot.db_pool
        try:
            await guild_config.set_key(pool, self.guild.id, key, value)
        except Exception:
            log.exception("Ticket config panel could not write %s", key)
            return await interactions.notify_failure(
                interaction, _("Something went wrong saving that setting.")
            )
        # Echo the write first so the redraw is right even if the read below is
        # not, then RE-READ: a second panel, `/ticket setup` or the dashboard may
        # have moved another key since this one was opened. A read that fails
        # must not undo the write that just succeeded, so a `None` (failed) read
        # is dropped and the echo stands - the same posture the profile
        # visibility panel takes.
        self.raw[key] = value
        self.state = guild_config.resolve(self.raw)
        fresh = await guild_config.read_raw(pool, self.guild.id)
        if fresh is not None:
            self._load_state(fresh)
        self._build()
        await self._rerender(interaction)

    # -- callbacks ------------------------------------------------------
    async def set_panel_channel(self, interaction, channel):
        # The one refusing control: same preflight `/ticket setup` runs, for the
        # same reason (a configuration the bot cannot act on is worse than none).
        if channel is not None:
            # A ChannelSelect hands back a PARTIAL channel (an
            # ``app_commands.AppCommandChannel``), which carries no
            # ``permissions_for``; resolve it against the guild cache first. An
            # id the cache cannot resolve yields ``None`` permissions, which
            # ``missing_permissions`` reads as "everything is missing" - the safe
            # direction, and the same one it takes for a DM context.
            resolved = self.guild.get_channel(channel.id)
            missing = preflight.missing_permissions(
                resolved.permissions_for(self.guild.me) if resolved else None,
                preflight.SETUP_PERMISSIONS,
            )
            if missing:
                await interaction.response.send_message(
                    _(
                        "I need these permissions in {channel} first: "
                        "{permissions}."
                    ).format(
                        channel=channel.mention,
                        permissions=preflight.describe(missing),
                    ),
                    ephemeral=True,
                )
                # Nothing was written, so the select is now showing a channel the
                # configuration does not have: redraw to put it back.
                return await self._rerender(interaction)
        await self._write(
            interaction,
            guild_config.KEY_PANEL_CHANNEL,
            channel.id if channel is not None else None,
        )

    async def set_support_role(self, interaction, role):
        await self._write(
            interaction,
            guild_config.KEY_SUPPORT_ROLE,
            role.id if role is not None else None,
        )

    async def set_log_channel(self, interaction, channel):
        await self._write(
            interaction,
            guild_config.KEY_LOG_CHANNEL,
            channel.id if channel is not None else None,
        )

    async def set_count(self, interaction, key, value):
        """Store a whole number from a select, or delete the key on RESET_VALUE.

        The value still goes through ``coerce_count`` even though it came from
        our own options list: the bounds are the storage layer's to enforce, and
        a preset list that drifted out of them must be clamped, not written.
        """
        if value == RESET_VALUE:
            return await self._write(interaction, key, None)
        if key == guild_config.KEY_MAX_OPEN_PER_USER:
            bounds = (
                guild_config.MIN_OPEN_PER_USER,
                guild_config.MAX_OPEN_PER_USER,
                guild_config.DEFAULT_MAX_OPEN_PER_USER,
            )
        else:
            bounds = (
                guild_config.MIN_INACTIVITY_HOURS,
                guild_config.MAX_INACTIVITY_HOURS,
                guild_config.DEFAULT_INACTIVITY_HOURS,
            )
        await self._write(
            interaction,
            key,
            guild_config.coerce_count(
                value, minimum=bounds[0], maximum=bounds[1], default=bounds[2]
            ),
        )

    async def set_blurb(self, interaction, text):
        """Store the panel blurb; blank (or the reset button) DELETES the key."""
        await self._write(
            interaction,
            guild_config.KEY_PANEL_MESSAGE,
            guild_config.coerce_text(
                text, limit=guild_config.MAX_PANEL_MESSAGE_LENGTH
            ),
        )


def _defaults(getter, raw_id):
    """Pre-selection list for a picker: the resolved object, or nothing.

    An id that no longer resolves (deleted channel/role) yields an EMPTY list
    rather than a fabricated default: Discord rejects a default value it cannot
    resolve, and the overview line above already says "(deleted)".
    """
    obj = _resolve(getter, raw_id)
    return [obj] if obj is not None else []


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
        return guild_config.resolve(
            await guild_config.read_raw(self.bot.db_pool, guild_id)
        )

    @commands.hybrid_group(name="ticket")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticket(self, ctx):
        """Manage support tickets: set up the panel, configure it or turn it off."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ticket.command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        channel="Where the ticket panel is posted (tickets become threads here).",
        support_role="Role pinged inside every new ticket.",
        log_channel="Where closed tickets are logged, transcript attached.",
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

        # Per-key patches, not one blob write (guild_config.set_key jsonb_sets a
        # single key, so a sibling feature's key in the same blob cannot be
        # clobbered) and a patch worth None DELETES its key rather than storing a
        # null - the reset rule, so a blurb cleared here leaves the guild exactly
        # as unconfigured as one that never set a blurb. They are NOT one
        # transaction, so the order is the safety: the panel channel - which IS
        # the on switch - is written LAST, so a failure part-way through leaves
        # the feature off with a half-written configuration rather than on with
        # one.
        patches.append((guild_config.KEY_PANEL_CHANNEL, channel.id))
        for key, value in patches:
            await guild_config.set_key(pool, ctx.guild.id, key, value)

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
        if log_channel is not None:
            notes.append(
                "-# "
                + _("Closed tickets are logged in {channel}, transcript attached.")
                .format(channel=log_channel.mention)
            )
            caution = _log_caution(log_channel)
            if caution:
                notes.append(caution)
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

    @ticket.command(name="config")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticket_config(self, ctx):
        """Open the ticket settings panel (edit every option in place)."""
        # The panel is a PRIVATE surface for one manager, unlike the panel
        # `/ticket setup` posts, so it renders in the INVOKER's locale (the
        # default) - there is nothing here the rest of the server reads.
        raw = await guild_config.read_raw(self.bot.db_pool, ctx.guild.id)
        view = TicketConfigPanel(self, ctx.guild, ctx.author.id, raw)
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
        # who turns the feature off for an afternoon. Cleared by DELETING the
        # key, not by storing a null: a guild that disables tickets goes back to
        # "never configured the switch", which is what the dashboard reads too.
        await guild_config.set_key(
            self.bot.db_pool, ctx.guild.id, guild_config.KEY_PANEL_CHANNEL, None
        )
        await ctx.send(
            _(
                "Tickets are off. Threads that are already open stay where they "
                "are, and the panel button will say tickets are not set up."
            )
        )
