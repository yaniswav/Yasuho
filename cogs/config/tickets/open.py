"""The public side of tickets: the panel button, the subject modal, and the one
flow that turns a click into a private thread.

ONE global persistent view. The button carries a STATIC ``custom_id``
(:data:`OPEN_CUSTOM_ID`), registered once in ``Tickets.cog_load``, so every
panel in every guild - including panels posted months ago, and panels the bot
has never seen since its last restart - dispatches here with nothing stored per
message. The guild's configuration is read AT CLICK TIME, never captured when
the panel was posted, which is what lets ``/ticket disable`` turn old buttons
into a clean "not set up here" instead of leaving live buttons behind. This is
the ``cogs/config/verification.py`` pattern, and the reasoning is the same.

Where the SUBJECT goes. Into the thread's opening message and NOWHERE else. It
is never passed to :mod:`storage`, and there is no column that could hold it
(schema.sql). A ticket's content is readable exactly where its participants can
read it, and it dies with the thread.

What makes the room private. ``TextChannel.create_thread(message=None)`` creates
a PRIVATE thread (discord.py defaults ``type`` to ``private_thread`` in that
case), which is only visible to its members and to staff holding
``manage_threads`` on the parent. ``invitable`` is passed as ``False``
EXPLICITLY - discord.py defaults it to ``True`` - so a member cannot pull
bystanders into somebody else's ticket. The opener is added by MENTIONING them in
the opening message: Discord adds a mentioned member to a private thread, so this
costs no extra API call.

Ordering, and why. The thread is created BEFORE the row, because ``thread_id`` is
the row's identity. A cap race therefore has to be compensated: when the guarded
INSERT declines (:func:`storage.open_ticket` returns ``None``), the thread that
was just created is DELETED. The other order - reserve the row, then create the
thread - was rejected on purpose: a crash between the two would leave an 'open'
row pointing at no thread, which would hold a cap slot against a member with no
way to release it, whereas an orphaned thread is invisible and auto-archives on
its own.

That is a comparison, not a proof: this order narrows the orphan-row window, it
does not close it. If the INSERT COMMITS and the connection then drops, asyncpg
still raises, the compensation deletes the thread, and a committed 'open' row is
left holding a cap slot with nothing here to release it. Nothing transactional
spans Discord and Postgres, so the window is inherent - what retires it is the
SWEEP (lifecycle.py), which closes rows whose thread is GONE rather than merely
idle. Recorded here because that is the only thing standing behind this order.

How long a ticket lives. The thread is created with the guild's own inactivity
window as its ``auto_archive_duration``, so Discord archives a silent ticket
exactly when the server said to and lifecycle.py turns that archive into a
close. The window is therefore enforced by Discord, not by a timer of ours, and
it is read HERE - at open time - like every other configuration value in this
flow.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import discord

from . import guild_config, lifecycle, preflight, storage
from tools import i18n, interactions
from tools.formats import random_colour
from tools.i18n import _, ngettext
from tools.views import LocaleModal

log = logging.getLogger(__name__)

# The panel button's custom_id. STATIC and global: one registered view serves
# every guild. Never put a guild or channel id in here - that is exactly the
# captured-at-post-time state this design avoids.
OPEN_CUSTOM_ID = "ticket_open"

# The namespace the IN-THREAD controls own (close / claim), which are per-thread
# and therefore ``discord.ui.DynamicItem`` templates rather than one static id.
# They live in lifecycle.py; this declaration is what a test pins, so no other
# feature can take the prefix out from under them.
DYNAMIC_NAMESPACE = "tk:"

# The name a thread is created with, before the authoritative number is known.
# Deliberately a valid, readable name rather than a placeholder nobody could
# interpret: if the rename below ever fails, the thread reads as an unnumbered
# ticket instead of as the WRONG ticket. (Naming it from a pre-read MAX + 1 would
# be one API call cheaper and is the shape that fails badly - a lost race would
# leave a thread confidently labelled with another ticket's number.)
PROVISIONAL_THREAD_NAME = "ticket"

# Members with an open flow in progress RIGHT NOW, as (guild_id, user_id).
#
# The database guard in storage.open_ticket is the authority on the cap and
# needs no help from this - but it cannot see intent. A member at 0 of 2 who
# submits the modal twice while the first thread is still being created passes
# the cap on both, and ends up with two identical tickets from one accident.
# This set is what makes the second submit a no-op. Process-wide and bounded by
# the number of opens IN FLIGHT (every add has a matching discard in a finally),
# so it is a handful of tuples at any instant even at 1000+ guilds.
_IN_FLIGHT = set()


class TicketSubjectModal(LocaleModal):
    """Asks for one line: what the ticket is about.

    Subclasses ``LocaleModal`` so the submit callback - which runs in its own
    task, where the command-path locale was never set - answers in the
    submitter's language.
    """

    def __init__(self):
        super().__init__(title=_("Open a ticket"))
        self.subject = discord.ui.TextInput(
            label=_("What do you need help with?"),
            placeholder=_("A short summary - staff will read it first."),
            style=discord.TextStyle.short,
            required=True,
            max_length=guild_config.MAX_SUBJECT_LENGTH,
        )
        self.add_item(self.subject)

    async def on_submit(self, interaction):
        await _create_ticket(interaction, str(self.subject.value or "").strip())


class TicketOpenButton(discord.ui.Button):
    """The public, persistent "Open a ticket" button on the panel."""

    def __init__(self):
        super().__init__(
            # Rendered in whatever locale is active when the button is BUILT.
            # The panel is built per guild inside `/ticket setup`, so a panel is
            # posted in that server's language; the copy registered at cog_load
            # (no locale context) falls back to English and is never rendered -
            # it exists only so clicks dispatch by custom_id.
            label=_("Open a ticket"),
            style=discord.ButtonStyle.primary,
            emoji="\N{ADMISSION TICKETS}",
            custom_id=OPEN_CUSTOM_ID,
        )

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interactions.reply(
                interaction, _("Tickets only work inside a server.")
            )

        # The bot-wide blacklist is a synchronous in-memory set primed at boot
        # (core.Yasuho.load_eager_caches). Absence is an ANSWER here, not a cache
        # miss, so this needs no await and cannot fail.
        blacklist = getattr(interaction.client, "blacklist", None) or ()
        if member.id in blacklist:
            return await interactions.reply(
                interaction, _("You cannot use this.")
            )

        pool = getattr(interaction.client, "db_pool", None)
        channel_id = await guild_config.panel_channel_id(pool, guild.id)
        if channel_id is None:
            return await interactions.reply(
                interaction, _("Tickets are not set up here.")
            )

        # Courtesy pre-check: refuse a capped member before making them type a
        # subject. NOT the guard - that is the INSERT in storage.open_ticket,
        # which is what two simultaneous clicks actually run into.
        cap = await guild_config.max_open_per_user(pool, guild.id)
        try:
            already = await storage.count_open_for_user(pool, guild.id, member.id)
        except Exception:
            log.exception("tickets: open-count check failed")
            return await interactions.reply(
                interaction, _("Something went wrong, please try again.")
            )
        if already >= cap:
            return await interactions.reply(interaction, _cap_message(cap))

        if (guild.id, member.id) in _IN_FLIGHT:
            return await interactions.reply(
                interaction, _("I am already opening a ticket for you - one moment.")
            )

        await interaction.response.send_modal(TicketSubjectModal())


class TicketPanelView(discord.ui.View):
    """Persistent (timeout=None) wrapper around the single Open button."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketOpenButton())


def _cap_message(cap):
    """The one refusal a member can act on: 'close one and come back'."""
    return ngettext(
        "You already have {count} ticket open here. Close it before opening another.",
        "You already have {count} tickets open here. Close one before opening another.",
        cap,
    ).format(count=cap)


async def _create_ticket(interaction, subject):
    """Modal-submit body: create the private thread, record it, invite everyone.

    Every configuration value is re-read here rather than carried from the click:
    the modal was open for as long as the member took to type, and a server can
    be reconfigured (or the feature turned off) in that window.
    """
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return await interactions.reply(
            interaction, _("Tickets only work inside a server.")
        )

    # Re-checked here, not only at the click. A blacklisting lands in the
    # in-memory set the instant it happens, and the modal may have been open for
    # minutes - checking only at the click would let somebody who was banned
    # WHILE typing still open a ticket. Synchronous set lookup, so this is free.
    blacklist = getattr(interaction.client, "blacklist", None) or ()
    if member.id in blacklist:
        return await interactions.reply(interaction, _("You cannot use this."))

    await interactions.defer(
        interaction, ephemeral=True, thinking=True, surface="ticket-open"
    )

    pool = getattr(interaction.client, "db_pool", None)
    channel_id = await guild_config.panel_channel_id(pool, guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return await interactions.reply(
            interaction, _("Tickets are not set up here.")
        )

    missing = preflight.missing_permissions(
        channel.permissions_for(guild.me), preflight.OPEN_PERMISSIONS
    )
    if missing:
        log.warning(
            "tickets: guild %s channel %s is missing %s",
            guild.id,
            channel.id,
            ",".join(missing),
        )
        return await interactions.reply(
            interaction,
            _(
                "I am missing permissions in {channel} ({permissions}), so I "
                "cannot open a ticket. Please tell a server admin."
            ).format(channel=channel.mention, permissions=preflight.describe(missing)),
        )

    key = (guild.id, member.id)
    if key in _IN_FLIGHT:
        return await interactions.reply(
            interaction, _("I am already opening a ticket for you - one moment.")
        )
    _IN_FLIGHT.add(key)
    try:
        await _open_thread(interaction, guild, member, channel, subject, pool)
    finally:
        _IN_FLIGHT.discard(key)


async def _open_thread(interaction, guild, member, channel, subject, pool):
    """Create the thread, take the row, and compensate if the row is refused."""
    cap = await guild_config.max_open_per_user(pool, guild.id)
    # The guild's inactivity window IS the thread's auto-archive duration: that
    # is what makes the setting real. Discord then enforces it for free, the
    # archive it fires is what lifecycle.py turns into a close, and no ticket
    # needs a timer. Read here, at open time, from the same cached blob as the
    # cap - a window changed later applies to the tickets opened after it, which
    # is what the dashboard contract promises.
    hours = await guild_config.inactivity_hours(pool, guild.id)

    try:
        thread = await channel.create_thread(
            name=PROVISIONAL_THREAD_NAME,
            message=None,  # explicit: no starter message => a PRIVATE thread
            auto_archive_duration=guild_config.auto_archive_minutes(hours),
            invitable=False,  # discord.py defaults this to True
            reason=f"Support ticket opened by {member} ({member.id})",
        )
    except discord.HTTPException:
        log.exception("tickets: thread creation failed in guild %s", guild.id)
        return await interactions.reply(
            interaction, _("I could not create the ticket, please try again.")
        )

    try:
        number = await storage.open_ticket(pool, guild.id, thread.id, member.id, cap)
    except Exception:
        log.exception("tickets: recording the ticket failed in guild %s", guild.id)
        await _discard_thread(thread)
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )

    if number is None:
        # Lost the cap race against another click of our own. The thread is not
        # a ticket (no row), so it must not survive.
        await _discard_thread(thread)
        return await interactions.reply(interaction, _cap_message(cap))

    try:
        await thread.edit(name=f"{PROVISIONAL_THREAD_NAME}-{number}")
    except discord.HTTPException:
        # Cosmetic only: the number is stated in the opening message and in the
        # confirmation below, so a failed rename never hides which ticket this is.
        log.warning(
            "tickets: could not rename thread %s to ticket-%s", thread.id, number
        )

    await _post_opening_message(guild, thread, member, subject, number, pool)

    await interactions.reply(
        interaction,
        _("Ticket #{number} opened: {thread}").format(
            number=number, thread=thread.mention
        ),
    )


async def _post_opening_message(guild, thread, member, subject, number, pool):
    """The one message inside the ticket: it adds the opener and calls staff.

    The opener's MENTION is what pulls them into the private thread, so it has to
    be in the content (a mention inside an embed does not add anybody). Mentions
    are pinned to exactly the opener and the configured support role:
    ``everyone=False`` is what stops a subject typed as ``@everyone`` from
    becoming one, and naming the role explicitly means no other role in a subject
    can ping either.
    """
    role_id = await guild_config.support_role_id(pool, guild.id)
    role = guild.get_role(role_id) if role_id else None

    mentions = [member.mention]
    if role is not None:
        mentions.append(role.mention)
    content = " ".join(mentions)

    embed = discord.Embed(
        title=_("Ticket #{number}").format(number=number),
        description=subject or _("*No subject given.*"),
        colour=random_colour(),
    )
    embed.set_footer(
        text=_("Opened by {user}").format(user=str(member))
    )

    allowed = discord.AllowedMentions(
        everyone=False,
        users=[member],
        roles=[role] if role is not None else False,
        replied_user=False,
    )
    # --- lot T2 seam: the in-thread controls ride this message ---------------
    # The ONLY line open.py contributes to the lifecycle. The view is keyed on
    # the thread id (the ticket's identity) and its buttons are DynamicItems, so
    # nothing about it is stored per message and it keeps working across
    # restarts. Built here, in the submitter's locale, so its labels match the
    # embed printed right above them.
    controls = lifecycle.TicketControlsView(thread.id)
    # ------------------------------------------------------------------------
    try:
        await thread.send(
            content, embed=embed, view=controls, allowed_mentions=allowed
        )
    except discord.HTTPException:
        # The room exists and the row is written, so the ticket is real - but the
        # mention that failed to send is also what ADDS the opener to a private
        # thread, and a link to a private thread you are not a member of opens
        # nothing. So fall back to the explicit add (a different endpoint and a
        # different permission, so it can succeed where the send did not) rather
        # than leave somebody locked out of the ticket they just opened.
        log.warning("tickets: could not post the opening message in %s", thread.id)
        try:
            await thread.add_user(member)
        except discord.HTTPException:
            log.warning(
                "tickets: could not add the opener to thread %s either", thread.id
            )
        # add_user restores ACCESS, not the controls - and without them nobody
        # in the room can close the ticket, which leaves the sweep as the only
        # ending and the opener's cap slot held meanwhile. So retry the send
        # stripped down to the two things that matter: the number and the
        # buttons. A fresh view, because the one above was already handed to a
        # failed send.
        try:
            await thread.send(
                _("Ticket #{number} - use the buttons below to claim or close it.")
                .format(number=number),
                view=lifecycle.TicketControlsView(thread.id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning(
                "tickets: thread %s has no in-thread controls; only the sweep "
                "can end it",
                thread.id,
            )


async def _discard_thread(thread):
    """Delete a thread that never became a ticket. Best effort by design."""
    try:
        await thread.delete()
    except discord.HTTPException:
        log.warning("tickets: could not delete the orphaned thread %s", thread.id)
