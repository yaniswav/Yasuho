"""The opener's satisfaction rating of a closed ticket (lot V5a).

ONE prompt per ticket, sent by DM on every close path. NEVER in the thread: the
close archives and locks it, and Discord refuses to edit a message of an
archived thread, so a click there would save the rating and then fail on the
reply. The thread only gets one plain line ("Check your DMs") when the DM went
through. Five star buttons, then - after a rating - an optional comment button
that opens a short modal.

Persistent: both controls are DynamicItems (``tkr:<ticket id>:<1-5>`` and
``tkc:<ticket id>``), so a prompt posted before a restart still works for seven
days. The ticket's database ID (not the thread) is the identity, because a DM
prompt has no thread to resolve.

Exactly-once and opener-only are decided by ONE UPDATE (storage.rate_ticket):
``WHERE opener_id = presser AND rating IS NULL AND status = 'closed' AND inside
the window``. Nothing is read first, so a double click or a click by somebody
else matches no row. The refusal reason is a second read on the failure path
only.

The comment is personal data written by a member: <= 300 characters, exported by
/mydata and erased by deleteprofile (tools/privacy.py). The bot shows it ONLY to
staff, in the ticket log channel, and only when that channel is not readable by
everyone - the same rule the transcript follows.

Scale story: a close adds one settings read and one send; a rating is one UPDATE
by primary key. No timer, no listener, no cache.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import discord

from . import guild_config, storage
from tools import i18n, interactions
from tools.i18n import _
from tools.modchecks import everyone_can_read
from tools.views import LocaleDynamicItem, LocaleModal, LocaleView

log = logging.getLogger(__name__)

RATE_TEMPLATE = r"tkr:(?P<tid>\d+):(?P<n>[1-5])"
COMMENT_TEMPLATE = r"tkc:(?P<tid>\d+)"

# How long after the close the opener may still rate / comment.
RATING_WINDOW_DAYS = 7
MAX_COMMENT_LENGTH = 300

_STAR = "\N{WHITE MEDIUM STAR}"


def stars(rating):
    """``3`` -> three star glyphs. Pure."""
    return _STAR * int(rating)


class RatingStarButton(LocaleDynamicItem[discord.ui.Button], template=RATE_TEMPLATE):
    """One of the five star buttons: opener only, once."""

    def __init__(self, ticket_id, rating):
        self.ticket_id = int(ticket_id)
        self.rating = int(rating)
        super().__init__(
            discord.ui.Button(
                label=str(self.rating),
                emoji=_STAR,
                style=discord.ButtonStyle.secondary,
                custom_id="tkr:{tid}:{n}".format(tid=self.ticket_id, n=self.rating),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]), int(match["n"]))

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        await _run_rate(interaction, self.ticket_id, self.rating)


class RatingCommentButton(
    LocaleDynamicItem[discord.ui.Button], template=COMMENT_TEMPLATE
):
    """Opens the comment modal: opener only (re-checked by the UPDATE itself)."""

    def __init__(self, ticket_id):
        self.ticket_id = int(ticket_id)
        super().__init__(
            discord.ui.Button(
                label=_("Add a comment"),
                style=discord.ButtonStyle.primary,
                custom_id="tkc:{tid}".format(tid=self.ticket_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        await interaction.response.send_modal(RatingCommentModal(self.ticket_id))


class RatingPromptView(LocaleView):
    """The five stars. ``timeout=None``: dispatch goes through the DynamicItems."""

    def __init__(self, ticket_id):
        super().__init__(timeout=None)
        for n in range(1, 6):
            self.add_item(RatingStarButton(ticket_id, n))


class RatingCommentView(LocaleView):
    """What the prompt turns into after a rating: just the comment button."""

    def __init__(self, ticket_id):
        super().__init__(timeout=None)
        self.add_item(RatingCommentButton(ticket_id))


class RatingCommentModal(LocaleModal):
    """One optional paragraph, 300 characters at most."""

    def __init__(self, ticket_id):
        super().__init__(title=_("Rate your ticket"))
        self.ticket_id = int(ticket_id)
        self.comment = discord.ui.TextInput(
            label=_("Comment (optional)"),
            placeholder=_("What went well, or what could be better?"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_COMMENT_LENGTH,
        )
        self.add_item(self.comment)

    async def on_submit(self, interaction):
        await _run_comment(
            interaction, self.ticket_id, clean_comment(self.comment.value)
        )


def clean_comment(raw):
    """Trim and bound a comment; ``None`` when nothing is left. Pure."""
    text = str(raw or "").strip()[:MAX_COMMENT_LENGTH]
    return text or None


async def _refusal(state, user_id):
    """The sentence that says WHY a rating or comment was refused."""
    if state is None or state["opener_id"] != user_id:
        return _("Only the member who opened this ticket can rate it.")
    if state["rating"] is not None:
        return _("This ticket is already rated. Thank you!")
    return _("The time to rate this ticket has passed.")


async def _state(pool, ticket_id):
    try:
        return await storage.rating_state(pool, ticket_id), True
    except Exception:
        log.exception("tickets: reading the rating state failed")
        return None, False


async def _run_rate(interaction, ticket_id, rating):
    pool = getattr(interaction.client, "db_pool", None)
    # One indexed read first: the ticket's guild decides whether ratings are
    # still on (the prompt is a DM, so the click carries no guild).
    state, ok = await _state(pool, ticket_id)
    if not ok:
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )
    if state is not None and not await guild_config.rating_enabled(
        pool, state["guild_id"]
    ):
        return await interactions.reply(
            interaction, _("Ratings are turned off on this server.")
        )
    try:
        row = await storage.rate_ticket(
            pool, ticket_id, interaction.user.id, rating, window_days=RATING_WINDOW_DAYS
        )
    except Exception:
        log.exception("tickets: rating ticket %s failed", ticket_id)
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )
    if row is None:
        return await interactions.reply(
            interaction, await _refusal(state, interaction.user.id)
        )
    # The single response: swap the stars for the thank-you and the comment button.
    try:
        await interaction.response.edit_message(
            content=_("Thank you! You rated ticket #{number}: {stars}").format(
                number=row["ticket_number"], stars=stars(rating)
            ),
            view=RatingCommentView(ticket_id),
        )
    except discord.HTTPException:
        log.debug("tickets: could not update the rating prompt", exc_info=True)
    await _tell_staff(
        interaction.client,
        row["guild_id"],
        lambda: _("Ticket #{number} was rated {stars}").format(
            number=row["ticket_number"], stars=stars(rating)
        ),
    )


async def _run_comment(interaction, ticket_id, comment):
    # Deferred first: the UPDATE and the staff post are not worth the 3 s window.
    await interaction.response.defer(ephemeral=True)
    pool = getattr(interaction.client, "db_pool", None)
    if comment is None:
        return await interactions.reply(
            interaction, _("The comment was empty, so nothing was saved.")
        )
    try:
        row = await storage.comment_ticket(
            pool, ticket_id, interaction.user.id, comment, window_days=RATING_WINDOW_DAYS
        )
    except Exception:
        log.exception("tickets: commenting ticket %s failed", ticket_id)
        return await interactions.reply(
            interaction, _("Something went wrong, please try again.")
        )
    if row is None:
        state, ok = await _state(pool, ticket_id)
        if ok and (state is None or state["opener_id"] != interaction.user.id):
            return await interactions.reply(
                interaction,
                _("Only the member who opened this ticket can rate it."),
            )
        return await interactions.reply(
            interaction,
            _("Your comment could not be saved: it is already there, or the time has passed."),
        )
    await interactions.reply(interaction, _("Thank you, your comment was saved."))
    await _tell_staff(
        interaction.client,
        row["guild_id"],
        lambda: _("Comment on ticket #{number}: {comment}").format(
            number=row["ticket_number"], comment=comment
        ),
    )


async def _tell_staff(bot, guild_id, build):
    """Post ``build()`` to the ticket log channel, only if staff-only. Best effort.

    ``build`` is called INSIDE the guild's locale: the log channel is read by the
    server's staff, not by the member who clicked.
    """
    from .lifecycle import _log_channel  # lazy: lifecycle imports this module

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return
    try:
        channel = await _log_channel(guild, getattr(bot, "db_pool", None))
        if channel is None or everyone_can_read(channel, unknown=True):
            return
        with i18n.locale(await i18n.resolve_guild_locale(bot, guild)):
            text = build()
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.debug("tickets: could not tell staff about a rating", exc_info=True)


async def offer_rating(bot, guild, closed):
    """DM the opener ONE rating prompt for a just-closed ticket; ``True`` if sent.

    DM on every close path, never a thread (see the module docstring). The opener
    is resolved with ``get_user``/``fetch_user``, not ``guild.get_member``, which
    is blind when members are not chunked. Never raises: a rating is a courtesy
    and must not be able to break a close. Default OFF per guild.
    """
    try:
        pool = getattr(bot, "db_pool", None)
        if not await guild_config.rating_enabled(pool, guild.id):
            return False
        opener_id = int(closed["opener_id"])
        user = bot.get_user(opener_id)
        if user is None:
            user = await bot.fetch_user(opener_id)
        await user.send(
            _("How was the support you got in {server} on ticket #{number}?").format(
                server=guild.name, number=closed["ticket_number"]
            )
            + "\n-# "
            + _("Staff can see your rating."),
            view=RatingPromptView(closed["id"]),
        )
        return True
    except discord.HTTPException as exc:
        log.info(
            "tickets: rating prompt for ticket %s not delivered: %s",
            closed["id"],
            exc,
        )
    except Exception:
        log.exception("tickets: offering the rating prompt failed")
    return False
