"""Purpose: the Discord surface of the connector framework - link an external
account to your profile, unlink it, see what is linked and decide who sees it.

Why its own cog and its own command group, rather than more ``profile``
subcommands: a hybrid subcommand must live in the SAME cog as the group that
owns it (the house lesson from the /levelconfig fold), and the ``profile`` group
belongs to ``..cog``. ``connections`` is a sibling root group instead. The name
was picked against the live command tree: ``disconnect`` is already the music
cog's leave command at root, so a bare ``connect``/``disconnect`` pair would
shadow it for every prefix user (tests/test_command_tree_hygiene.py would fail).

``connections visibility`` looks like a duplicate of ``profile visibility`` and
is not: that TEXT command deliberately only offers the sections the profile
itself stores (``registry.STORED_NAMES``), so it answers "unknown section" for
``anilist`` and friends. Without the verb below, the "your section is still
private" hint printed after a successful link would point at a dead end for
prefix users. The graphical panel does cover connector sections, and both it and
this command write the same ``profile_visibility`` row through the same parent
seam, so the two surfaces cannot disagree.

Nothing here implements a connector: the registry is EMPTY until P4, so every
name answers "coming soon" and the link is refused rather than half-stored. The
command surface still ships now because storage, privacy and visibility are
what needed proving, and a framework nobody can call is a framework nobody has
tested.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord.ext import commands

from .. import storage as profile_storage
from .. import visibility
from . import base, storage
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Spelled out because an annotation must be static; a test pins these to
# base.LINKABLE and visibility.LEVELS so the two can never drift. discord.py
# turns each Literal into real slash CHOICES, which beats making the user guess
# the spelling of `spotify_presence`.
ConnectorName = Literal["anilist", "steam", "lastfm", "osu", "backloggd"]
VisibilityLevel = Literal["public", "server", "private"]

# One link is a third-party round trip in P4 (and a write here today). Three a
# minute per user is plenty for a human correcting a typo and stops a single
# account from being the reason a connector's API key gets rate-limited for
# everybody. The bot-wide budget lives with the P4 connectors themselves.
LINK_RATE = 3
LINK_PER_SECONDS = 60.0

# `connections list` opens a DM on every PREFIX invocation (the slash path
# answers ephemerally instead), and a DM is a per-bot rate-limited resource
# that a channel of users can exhaust for everybody. One a minute per user is
# generous for reading one's own list and turns the "spam ?connections" trick
# into a single DM.
LIST_RATE = 1
LIST_PER_SECONDS = 60.0


def _linked_line(name, level):
    """One whole sentence per audience, never an assembled one.

    Composing "Linked as {name} - visible to " + a translated fragment would
    hand translators a sentence they cannot reorder (and a gender they cannot
    agree with), so the three cases are three complete msgids.
    """
    if level == visibility.PUBLIC:
        return _("Linked as {name} - visible to anyone").format(name=name)
    if level == visibility.SERVER:
        return _("Linked as {name} - visible to the servers you share").format(
            name=name
        )
    return _("Linked as {name} - visible to you only").format(name=name)


class ProfileConnectors(commands.Cog):
    """Link external accounts (AniList, Steam, Last.fm, osu!, Backloggd) to your profile."""

    def __init__(self, bot):
        self.bot = bot

    # -- helpers ------------------------------------------------------------

    def _normalise(self, name):
        """Fold a user-typed connector name; never trust the Literal alone.

        The slash side is constrained to the choices, but the prefix side, a
        direct call and the future panel all reach this body with free text.
        """
        return (name or "").strip().lower()

    async def _levels(self, user_id):
        """Read the visibility rows; an unreadable map is treated as private."""
        try:
            return await profile_storage.get_visibility(self.bot.db_pool, user_id)
        except Exception:
            log.exception("Failed to read profile visibility for %s", user_id)
            return {}

    def _charge_list_cooldown(self, ctx):
        """Consume ``connections list``'s own bucket from the bare-group path.

        ``?connections`` runs the subcommand's BODY directly, and discord.py
        applies cooldowns in ``Command.invoke``, which a direct call skips - so
        without this the short spelling would be a free door to the very DM the
        long one rate-limits. Charging the SAME bucket (rather than a second
        one) is also what stops the two spellings from being alternated to
        double the allowance.
        """
        command = self.connections_list
        buckets = command._buckets
        if not buckets.valid:
            return
        bucket = buckets.get_bucket(ctx)
        if bucket is None:
            return
        retry_after = bucket.update_rate_limit()
        if retry_after:
            raise commands.CommandOnCooldown(bucket, retry_after, buckets.type)

    def _refund(self, ctx):
        """Give the rate-limit token back: this refusal cost nothing.

        ``connections link``'s bucket exists to protect a THIRD PARTY (and the
        write behind it). A name that was refused before any of that happened -
        a typo, or a connector P4 has not shipped - spent none of it, so making
        the user wait a minute to try the right one would be a penalty for
        being told "not yet".
        """
        command = ctx.command
        if command is not None:
            command.reset_cooldown(ctx)

    async def _fail(self, ctx):
        """The one answer to "something on our side broke", said once."""
        await ctx.send(
            _("Failed to update your profile, please try again later."),
            ephemeral=True,
        )

    async def _unknown(self, ctx):
        """The one answer to a name that is not a connector, said once."""
        await ctx.send(
            _("Unknown connection. Choose: {options}").format(
                options=", ".join(base.LINKABLE)
            ),
            ephemeral=True,
        )

    def _visibility_hint(self, levels, section, prefix):
        """Warn the owner when what they just linked is visible to nobody.

        Every section is born private (an ABSENT row IS private), so without
        this line a successful link would look like it did nothing. Naming the
        command that publishes the section keeps the write path from being a
        dead end.
        """
        if visibility.level_for(levels, section) != visibility.PRIVATE:
            return None
        command = "{prefix}connections visibility {section} server".format(
            prefix=prefix, section=section
        )
        return _(
            "Only you can see this for now. Use `{command}` to show it to the "
            "servers you share."
        ).format(command=command)

    def _unavailable_message(self, error):
        """Map a typed unavailability to the message the user should read."""
        if error.reason == "coming_soon":
            return _(
                "That connection is not available yet - it is coming soon."
            )
        if error.reason == "not_configured":
            return _(
                "That connection is not set up on this bot yet. Ask an admin."
            )
        return _(
            "That service is not answering right now, please try again later."
        )

    def _invalid_handle_message(self, error, connector):
        """Map a refused handle to a message that says what to type instead."""
        if error.reason == "too_long":
            return _("That value is too long (max {limit} characters).").format(
                limit=error.limit
            )
        if error.reason == "not_found":
            return _("No account was found with that name.")
        hint = getattr(connector, "handle_hint", "")
        if hint:
            return _("That does not look right. Enter {hint}.").format(
                hint=_(hint)
            )
        return _("That value is not valid for this field.")

    def _describe(self, connection, levels):
        """One line of status for a linked account."""
        name = connection.get("display_name") or connection["external_id"]
        level = visibility.level_for(levels, connection["connector"])
        return _linked_line(name, level)

    def _build_list_embed(self, connections, levels):
        """Every linkable section with its status, in registry order."""
        linked = {item["connector"]: item for item in connections}
        pending = set(base.coming_soon())
        embed = discord.Embed(
            title=_("Your connections"), colour=random_colour()
        )
        for name in base.LINKABLE:
            label = _(base.label_for(name))
            connection = linked.get(name)
            if connection is not None:
                value = self._describe(connection, levels)
            elif name in pending:
                value = _("Coming soon")
            else:
                value = _("Not linked")
            embed.add_field(name=label, value=value, inline=False)
        return embed

    # -- commands -----------------------------------------------------------

    @commands.hybrid_group(name="connections")
    @commands.guild_only()
    async def connections(self, ctx):
        """See and manage the external accounts linked to your profile."""

        if ctx.invoked_subcommand is None:
            self._charge_list_cooldown(ctx)
            await self.connections_list(ctx)

    @connections.command(name="list")
    @commands.guild_only()
    @commands.cooldown(LIST_RATE, LIST_PER_SECONDS, commands.BucketType.user)
    async def connections_list(self, ctx):
        """List the external accounts linked to your profile."""

        # This answer names handles the user never typed here, including the
        # ones they deliberately left private, so it must never land in a
        # channel. `ephemeral=True` alone does NOT guarantee that: verified in
        # discord.py 2.7.1, Context.send drops the flag entirely when there is
        # no interaction. So the prefix path gets the `?mydata export`
        # treatment instead - a DM, and an in-channel line that names nothing.
        ephemeral = ctx.interaction is not None
        async with ctx.typing(ephemeral=True):
            try:
                connections = await storage.get_connections(
                    self.bot.db_pool, ctx.author.id
                )
            except Exception:
                log.exception("Failed to read connections for %s", ctx.author.id)
                await self._fail(ctx)
                return
            levels = await self._levels(ctx.author.id)
            embed = self._build_list_embed(connections, levels)
            if ephemeral:
                await ctx.send(
                    embed=embed,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                await ctx.author.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                await ctx.send(
                    _("I couldn't send you a direct message. Please enable DMs.")
                )
                return
            except Exception:
                log.exception(
                    "Failed to DM the connections list to %s", ctx.author.id
                )
                await self._fail(ctx)
                return
            await ctx.send(
                _("I sent your connections to you by direct message.")
            )

    @connections.command(name="link")
    @commands.guild_only()
    @commands.cooldown(LINK_RATE, LINK_PER_SECONDS, commands.BucketType.user)
    @discord.app_commands.describe(
        connector="Which service to link (anilist, steam, lastfm, osu, backloggd).",
        handle="Your username or id on that service.",
    )
    async def connections_link(
        self, ctx, connector: ConnectorName, *, handle: str
    ):
        """Link one of your external accounts to your profile."""

        name = self._normalise(connector)
        async with ctx.typing(ephemeral=True):
            # Both of these are decided BEFORE anything is spent - no remote
            # call, no write - so the rate-limit token goes back (see _refund).
            # Everything past this point did touch the connector, and keeps it.
            try:
                implementation = base.get(name)
            except base.UnknownConnector:
                self._refund(ctx)
                await self._unknown(ctx)
                return
            except base.ConnectorUnavailable as error:
                self._refund(ctx)
                await ctx.send(
                    self._unavailable_message(error), ephemeral=True
                )
                return

            try:
                result = await implementation.link(ctx.author.id, handle)
            except base.InvalidHandle as error:
                await ctx.send(
                    self._invalid_handle_message(error, implementation),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except base.ConnectorUnavailable as error:
                # 'not_configured' joins the two refusals above: every
                # connector reads its API key as the FIRST statement of the
                # call path (steam.py and osu.py in `link` itself, lastfm.py
                # at the top of its first request), so a bot whose admin never
                # provisioned that key refuses before a single byte leaves
                # this process - the token bought nothing and goes back. A
                # 'remote' failure DID cost a round trip at the third party
                # this bucket exists to protect, and keeps it.
                if error.reason == "not_configured":
                    self._refund(ctx)
                await ctx.send(
                    self._unavailable_message(error), ephemeral=True
                )
                return
            except Exception:
                log.exception("Connector %s failed to link", name)
                await self._fail(ctx)
                return

            try:
                stored = await storage.link(
                    self.bot.db_pool, ctx.author.id, name, result
                )
            except base.InvalidHandle as error:
                await ctx.send(
                    self._invalid_handle_message(error, implementation),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except Exception:
                log.exception("Failed to store the %s connection", name)
                await self._fail(ctx)
                return

            shown = stored.get("display_name") or stored["external_id"]
            embed = discord.Embed(
                title=_("Connection linked")
                if stored.get("created")
                else _("Connection updated"),
                colour=random_colour(),
            )
            embed.add_field(name=_(base.label_for(name)), value=shown)
            levels = await self._levels(ctx.author.id)
            # The footer always states the audience. When the section is still
            # private the hint also names the command that publishes it; when
            # it is already published the plain line is what matters, because
            # re-linking over a section someone set to `public` months ago for
            # a DIFFERENT account puts a brand-new handle live immediately -
            # the exact hazard storage.unlink deletes the visibility row to
            # avoid. Saying it costs no new msgid.
            note = self._visibility_hint(
                levels, name, ctx.clean_prefix
            ) or _linked_line(shown, visibility.level_for(levels, name))
            embed.set_footer(text=note)
            await ctx.send(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @connections.command(name="unlink")
    @commands.guild_only()
    @discord.app_commands.describe(
        connector="Which service to unlink (anilist, steam, lastfm, osu, backloggd)."
    )
    async def connections_unlink(self, ctx, connector: ConnectorName):
        """Unlink one of your external accounts and hide its section."""

        name = self._normalise(connector)
        async with ctx.typing(ephemeral=True):
            if name not in base.LINKABLE:
                await self._unknown(ctx)
                return
            try:
                removed = await storage.unlink(
                    self.bot.db_pool, ctx.author.id, name
                )
            except Exception:
                log.exception("Failed to unlink the %s connection", name)
                await self._fail(ctx)
                return

            # Best effort, and deliberately after the row is gone: a connector
            # that fails to clean up its own side must not resurrect the link.
            implementation = base.CONNECTORS.get(name)
            if implementation is not None:
                try:
                    await implementation.unlink(ctx.author.id)
                except Exception:
                    log.exception("Connector %s failed its unlink hook", name)

            label = _(base.label_for(name))
            if not removed:
                await ctx.send(
                    _("{field} was not linked.").format(field=label),
                    ephemeral=True,
                )
                return
            await ctx.send(
                _("{field} is unlinked and hidden again.").format(field=label),
                ephemeral=True,
            )

    @connections.command(name="visibility")
    @commands.guild_only()
    @discord.app_commands.describe(
        connector="Which connection to publish.",
        level="public, server or private.",
    )
    async def connections_visibility(
        self, ctx, connector: ConnectorName, level: VisibilityLevel
    ):
        """Choose who can see one of your linked accounts."""

        name = self._normalise(connector)
        async with ctx.typing(ephemeral=True):
            if name not in base.LINKABLE:
                await self._unknown(ctx)
                return
            try:
                chosen = visibility.normalise_level(level)
            except visibility.InvalidLevel:
                await ctx.send(
                    _("Unknown visibility. Choose: {options}").format(
                        options=", ".join(visibility.LEVELS)
                    ),
                    ephemeral=True,
                )
                return
            try:
                await profile_storage.set_visibility(
                    self.bot.db_pool, ctx.author.id, name, chosen
                )
            except Exception:
                log.exception("Failed to set connection visibility for %s", name)
                await self._fail(ctx)
                return

            label = _(base.label_for(name))
            if chosen == visibility.PUBLIC:
                await ctx.send(
                    _("{field} is now visible to anyone.").format(field=label),
                    ephemeral=True,
                )
            elif chosen == visibility.SERVER:
                await ctx.send(
                    _("{field} is now visible to the servers you share.").format(
                        field=label
                    ),
                    ephemeral=True,
                )
            else:
                await ctx.send(
                    _("{field} is now visible to you only.").format(field=label),
                    ephemeral=True,
                )
