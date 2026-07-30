"""Purpose: the Discord surface of the profile - the commands the old profiles
cog already had, re-homed onto the new socle, plus the two Components V2
surfaces this lot adds (the ``/profile view`` card and the ``/profile panel``
visibility panel), whose actual layout lives in the sibling ``views.py``
(mirrors the music.py -> views.py / seasons.py -> seasons_views.py split; see
that module's docstring for the one-way import direction).

Deliberately thin otherwise. It keeps every existing command NAME and
behaviour (``profile`` / ``view`` / ``set`` / ``edit`` / ``clear``) while
routing them through registry -> storage -> visibility, and extends ``set`` to
the socle fields the registry now knows (bio, pronouns, accent) because it is
the same command, not a new surface.

Two commands are new and do need a re-sync: ``profile visibility`` (the text
twin of the panel - without it a field written today could never be published,
every field is born private, so ``set`` would be a write into a void; ``set``
also says so in a footer when the section is still private) and ``profile
panel`` (its graphical twin, added by this lot).

Reads apply the visibility rules for real: a field the owner has not published is
not rendered for anyone else. Gamer IDs migrated from the legacy table are seeded
at 'server' by the boot fixup, so nothing that was visible yesterday goes dark
today.

Typography rule: ASCII '-' and '...' only.
"""

import logging
from typing import Literal

import discord
from discord.ext import commands

from . import registry, storage, views, visibility
from .connectors import storage as connectors_storage
from .views import (
    ProfileEditModal,
    ProfileEditView,
    ProfileVisibilityPanel,
    build_profile_card,
    format_value,
    invalid_value_message,
    section_for,
)
from .visibility import ViewerContext
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Socle fields a text command can set. custom_fields is deliberately absent: a
# list of label/value pairs belongs in the P2 panel, not in a chat argument.
TEXT_SETTABLE = ("bio", "pronouns", "accent")

# Everything `profile set` accepts, in the order the help string lists them.
SET_CHOICES = registry.GAMING_ID_KEYS + TEXT_SETTABLE

# What `profile visibility` (the text command) can publish: the sections this
# version actually stores. The graphical panel offers the connector sections
# too (see views.ProfileVisibilityPanel); offering them here, before P3/P4
# fill them, would be a toggle with nothing behind it in a plain-text answer.
VISIBILITY_CHOICES = registry.STORED_NAMES


class Profiles(commands.Cog):
    """Your global profile: bio, pronouns, accent colour and gaming IDs."""

    def __init__(self, bot):
        self.bot = bot

    # -- helpers ------------------------------------------------------------

    async def apply_field(self, user_id, field, value):
        """Validate and store one field; return ``(label, shown_value)`` or None.

        None means "no such settable field" (the caller prints the choices), and
        a None ``shown_value`` means the field was cleared. A bad value raises
        :class:`registry.InvalidValue`, which carries the cap that was exceeded,
        so the message can name it.
        """
        key = (field or "").strip().lower()
        if key in registry.GAMING_ID_KEYS:
            stored = await storage.set_gaming_id(self.bot.db_pool, user_id, key, value)
            return _(registry.GAMING_ID_LABELS[key]), format_value(key, stored)
        if key in TEXT_SETTABLE:
            stored = await storage.set_field(self.bot.db_pool, user_id, key, value)
            return _(registry.get(key).label), format_value(key, stored)
        return None

    async def visibility_note(self, user_id, section, prefix):
        """Warn the owner when what they just wrote is visible to nobody.

        Every field is born private (an ABSENT row IS private), so without this
        line `profile set` would look like a write into a void until the P2
        panel ships. Naming the command that publishes the section keeps the
        write path from being a dead end.
        """
        if section not in VISIBILITY_CHOICES:
            return None
        try:
            levels = await storage.get_visibility(self.bot.db_pool, user_id)
        except Exception:
            log.exception("Failed to read profile visibility for %s", user_id)
            return None
        if visibility.level_for(levels, section) != visibility.PRIVATE:
            return None
        return _(
            "Only you can see this for now. Use "
            "`{prefix}profile visibility {section} server` to show it to the "
            "servers you share."
        ).format(prefix=prefix, section=section)

    # -- commands -----------------------------------------------------------

    @commands.hybrid_group(name="profile")
    @commands.guild_only()
    async def profile(self, ctx):
        """Manage your profile: view, set, edit, or clear."""

        if ctx.invoked_subcommand is None:
            await self.profile_view(ctx, ctx.author)

    @profile.command(name="view")
    @commands.guild_only()
    @discord.app_commands.describe(member="Whose profile to view (defaults to you).")
    async def profile_view(self, ctx, member: discord.Member = None):
        """View a member's profile."""

        member = member or ctx.author

        async with ctx.typing():
            pool = self.bot.db_pool
            profile = await storage.get_profile(pool, member.id)
            visibility_map = await storage.get_visibility(pool, member.id)
            # The third read of the card, and the one that makes a "Linked"
            # badge true: a section is drawn only if the owner really has a row
            # here, never because a visibility line exists. One indexed read
            # bounded to seven rows by the table's own primary key (user_id,
            # connector), so it costs the same as the two above.
            connections = await connectors_storage.get_connections(pool, member.id)
            # guild_only + a Member converter: both parties are in THIS guild, so
            # they share one. No global mutual-guild scan (that is O(guilds)).
            viewer = ViewerContext(
                owner_id=member.id,
                viewer_id=ctx.author.id,
                shares_guild=True,
            )
            card = await build_profile_card(
                member, profile, visibility_map, viewer, connections
            )

            if card is None:
                # A nickname is user-controlled too, and this branch is plain
                # message CONTENT (where "@everyone" from a nickname would
                # really ping), so it carries the same suppression as the card.
                await ctx.send(
                    _("{name} has no profile set.").format(name=member.display_name),
                    allowed_mentions=views.NO_PINGS,
                )
                return

            # The card's free-form fields (bio, custom values, gaming IDs) can
            # contain mention syntax the owner typed - unlike an embed, a
            # Components V2 card's TextDisplay text DOES get parsed for
            # mentions (see views.NO_PINGS).
            await ctx.send(view=card, allowed_mentions=views.NO_PINGS)

    @profile.command(name="set")
    @commands.guild_only()
    @discord.app_commands.describe(
        field="bio, pronouns, accent, switch, 3ds, battletag, riot, or steam_id.",
        value="The value to store; leave it out to clear the field.",
    )
    async def profile_set(self, ctx, field: str, *, value: str | None = None):
        """Set one of your profile fields (bio, pronouns, accent or a gaming ID)."""

        async with ctx.typing():
            try:
                applied = await self.apply_field(ctx.author.id, field, value)
            except registry.InvalidValue as error:
                await ctx.send(invalid_value_message(error))
                return
            except Exception:
                log.exception("Failed to set profile field %s", field)
                await ctx.send(
                    _("Failed to update your profile, please try again later.")
                )
                return

            if applied is None:
                await ctx.send(
                    _("Unknown field. Choose: {options}").format(
                        options=", ".join(SET_CHOICES)
                    )
                )
                return

            label, shown = applied
            if shown is None:
                # Omitting the value (or emptying it) is how a text command
                # clears a field: `?profile set bio` with nothing after it.
                await ctx.send(_("Cleared {field}.").format(field=label))
                return

            embed = discord.Embed(title=_("Profile updated"), colour=random_colour())
            embed.add_field(name=label, value=shown)
            note = await self.visibility_note(
                ctx.author.id, section_for(field), ctx.clean_prefix
            )
            if note:
                embed.set_footer(text=note)
            await ctx.send(embed=embed)

    # Both parameters are closed sets, so they are typed as Literals: discord.py
    # turns each one into real slash CHOICES (string values, unchanged), which
    # beats making the user guess the spelling of `custom_fields`. The literals
    # are spelled out rather than unpacked from the registry because an
    # annotation must be static; a test pins them to registry.STORED_NAMES and
    # visibility.LEVELS so the two can never drift.
    # The prefix path is unchanged in shape: the body still normalises and still
    # answers with the list of valid names, so a direct call (and the P2 panel)
    # keeps working with any casing.
    @profile.command(name="visibility")
    @commands.guild_only()
    @discord.app_commands.describe(
        section="Which part of your profile to publish.",
        level="public, server or private.",
    )
    async def profile_visibility(
        self,
        ctx,
        section: Literal["bio", "pronouns", "accent", "custom_fields", "gaming_ids"],
        level: Literal["public", "server", "private"],
    ):
        """Choose who can see one section of your profile."""

        async with ctx.typing():
            key = (section or "").strip().lower()
            if key not in VISIBILITY_CHOICES:
                await ctx.send(
                    _("Unknown section. Choose: {options}").format(
                        options=", ".join(VISIBILITY_CHOICES)
                    )
                )
                return
            try:
                chosen = visibility.normalise_level(level)
            except visibility.InvalidLevel:
                await ctx.send(
                    _("Unknown visibility. Choose: {options}").format(
                        options=", ".join(visibility.LEVELS)
                    )
                )
                return
            try:
                await storage.set_visibility(
                    self.bot.db_pool, ctx.author.id, key, chosen
                )
            except Exception:
                log.exception("Failed to set profile visibility for %s", key)
                await ctx.send(
                    _("Failed to update your profile, please try again later.")
                )
                return

            label = _(registry.get(key).label)
            if chosen == visibility.PUBLIC:
                await ctx.send(
                    _("{field} is now visible to anyone.").format(field=label)
                )
            elif chosen == visibility.SERVER:
                await ctx.send(
                    _("{field} is now visible to the servers you share.").format(
                        field=label
                    )
                )
            else:
                await ctx.send(
                    _("{field} is now visible to you only.").format(field=label)
                )

    @profile.command(name="edit")
    @commands.guild_only()
    async def profile_edit(self, ctx):
        """Edit a gaming ID through a guided form (radio picker + value)."""

        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(ProfileEditModal(self))
        else:
            view = ProfileEditView(self, ctx.author.id)
            view.message = await ctx.send(
                _("Click below to edit a profile field:"), view=view
            )

    @profile.command(name="panel")
    @commands.guild_only()
    async def profile_panel(self, ctx):
        """Open a graphical panel to manage your profile's visibility."""

        # Same discipline as every sibling here: `ctx.typing()` defers the slash
        # interaction, so the database round-trip cannot eat the 3-second
        # response window and lose the panel entirely.
        async with ctx.typing():
            visibility_map = await storage.get_visibility(
                self.bot.db_pool, ctx.author.id
            )
            view = ProfileVisibilityPanel(self, ctx.author.id, visibility_map)
            view.message = await ctx.send(view=view, allowed_mentions=views.NO_PINGS)

    @profile.command(name="clear")
    @commands.guild_only()
    async def profile_clear(self, ctx):
        """Clear your entire profile, including who could see what."""

        async with ctx.typing():
            try:
                await storage.delete_profile(self.bot.db_pool, ctx.author.id)
            except Exception:
                log.exception("Failed to clear profile")
                await ctx.send(
                    _("Failed to clear your profile, please try again later.")
                )
                return

            embed = discord.Embed(title=_("Profile cleared"), colour=random_colour())
            embed.add_field(name=_("Your profile has been cleared."), value="​")
            await ctx.send(embed=embed)
