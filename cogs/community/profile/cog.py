"""Purpose: the Discord surface of the profile - the commands the old profiles
cog already had, re-homed onto the new socle.

Deliberately thin. The card and the visibility PANEL are the next lot's job; this
one keeps every existing command NAME and behaviour (``profile`` / ``view`` /
``set`` / ``edit`` / ``clear``) while routing them through
registry -> storage -> visibility, and extends ``set`` to the socle fields the
registry now knows (bio, pronouns, accent) because it is the same command, not a
new surface.

One command IS new and does need a re-sync: ``profile visibility``, the text
twin of the future panel. Without it a field written today could never be
published (every field is born private), so ``set`` would be a write into a
void; ``set`` also says so in a footer when the section is still private.

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

from . import registry, storage, visibility
from .visibility import ViewerContext, resolve_visible_fields
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Socle fields a text command can set. custom_fields is deliberately absent: a
# list of label/value pairs belongs in the P2 panel, not in a chat argument.
TEXT_SETTABLE = ("bio", "pronouns", "accent")

# Everything `profile set` accepts, in the order the help string lists them.
SET_CHOICES = registry.GAMING_ID_KEYS + TEXT_SETTABLE

# What `profile visibility` can publish: the sections this version actually
# stores. The connector sections (anilist, lastfm, ...) are addressable in
# storage already, but offering them before P3/P4 fill them would be a toggle
# with nothing behind it.
VISIBILITY_CHOICES = registry.STORED_NAMES

# Discord rejects an embed whose total length exceeds 6000 characters (50035),
# and a single field value over 1024. Both are TOTALS across the card, so a
# profile made only of legal fields can still be illegal as a whole; we stop
# adding fields a little early and say so rather than raise on `profile view`.
EMBED_TOTAL_BUDGET = 5800
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FIELD_LIMIT = 25


def _invalid_value_message(error):
    """Map a typed registry rejection to the message the user should read."""
    if error.reason == "too_long":
        return _("That value is too long (max {limit} characters).").format(
            limit=error.limit
        )
    if error.reason == "colour":
        return _("That is not a valid colour. Use a hex colour like #5865F2.")
    return _("That value is not valid for this field.")


def section_for(field):
    """The visibility SECTION a settable field belongs to.

    The five gamer IDs are keys inside one ``gaming_ids`` section, so publishing
    a Switch code publishes them all: the visibility choice is made once, for
    the section, not per key.
    """
    key = (field or "").strip().lower()
    return "gaming_ids" if key in registry.GAMING_ID_KEYS else key


def add_field_within_budget(embed, name, value, inline=False):
    """Add one field unless it would push the embed past Discord's limits.

    Returns False when the field was dropped. Five 1000-char gamer IDs plus a
    full bio and five custom pairs are each individually legal but total more
    than 6000 characters, and Discord refuses the whole embed at that point -
    which would make a profile unviewable even by its own owner.
    """
    text = str(value)
    if len(text) > EMBED_FIELD_VALUE_MAX:
        text = text[: EMBED_FIELD_VALUE_MAX - 3] + "..."
    if len(embed.fields) >= EMBED_FIELD_LIMIT:
        return False
    if len(embed) + len(str(name)) + len(text) > EMBED_TOTAL_BUDGET:
        return False
    embed.add_field(name=name, value=text, inline=inline)
    return True


def format_value(name, stored):
    """Render what was actually STORED, not what was typed.

    The registry trims text and packs a colour into an int, so echoing the raw
    input back would confirm something the profile does not contain.
    """
    if stored is None:
        return None
    if name == "accent":
        return "#%06X" % stored
    return str(stored)


class ProfileEditModal(LocaleModal):
    """Pick a gamer ID from a radio and type its value (Components V2 modal)."""

    def __init__(self, cog):
        super().__init__(title=_("Edit your profile"))
        self.cog = cog
        self.field = discord.ui.RadioGroup(required=True)
        for key in registry.GAMING_ID_KEYS:
            self.field.add_option(label=_(registry.GAMING_ID_LABELS[key]), value=key)
        self.add_item(discord.ui.Label(text=_("Field"), component=self.field))
        self.value_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=True,
            max_length=registry.GAMING_ID_MAX,
        )
        self.add_item(discord.ui.Label(text=_("Value"), component=self.value_input))

    async def on_submit(self, interaction):
        try:
            field = self.field.value
            value = (self.value_input.value or "").strip()
            if not field or not value:
                return await interaction.response.send_message(
                    _("Pick a field and enter a value."), ephemeral=True
                )
            try:
                applied = await self.cog.apply_field(interaction.user.id, field, value)
            except registry.InvalidValue as error:
                return await interaction.response.send_message(
                    _invalid_value_message(error), ephemeral=True
                )
            if applied is None:
                return await interaction.response.send_message(
                    _("Unknown field."), ephemeral=True
                )
            label, shown = applied
            embed = discord.Embed(title=_("Profile updated"), colour=random_colour())
            embed.add_field(name=label, value=shown or value)
            # The modal is interaction-only, so the prefix that publishes the
            # section is always the slash one.
            note = await self.cog.visibility_note(
                interaction.user.id, section_for(field), "/"
            )
            if note:
                embed.set_footer(text=note)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Profile edit modal failed")
            await interaction.response.send_message(
                _("Failed to update your profile, please try again later."),
                ephemeral=True,
            )


class ProfileEditView(AuthorView):
    """One-button launcher for the profile edit modal (the prefix entry point)."""

    def __init__(self, cog, author_id):
        super().__init__(
            author_id, timeout=120, deny_message="This profile editor isn't for you."
        )
        self.cog = cog

    @discord.ui.button(
        label="Edit a field", emoji="\U0000270F", style=discord.ButtonStyle.primary
    )
    async def edit(self, interaction, button):
        try:
            await interaction.response.send_modal(ProfileEditModal(self.cog))
        except Exception:
            log.exception("Profile edit button failed")


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

    def _build_embed(self, member, visible):
        """Render the fields the viewer is allowed to see, in registry order."""
        accent = visible.get("accent")
        embed = discord.Embed(
            title=_("{name}'s profile").format(name=member.display_name),
            description=visible.get("bio"),
            colour=discord.Colour(accent) if accent is not None else random_colour(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        dropped = False
        pronouns = visible.get("pronouns")
        if pronouns:
            dropped |= not add_field_within_budget(
                embed, _(registry.get("pronouns").label), pronouns, inline=True
            )
        gaming_ids = visible.get("gaming_ids") or {}
        for key in registry.GAMING_ID_KEYS:
            value = gaming_ids.get(key)
            if value:
                dropped |= not add_field_within_budget(
                    embed, _(registry.GAMING_ID_LABELS[key]), value
                )
        for pair in visible.get("custom_fields", []):
            label = pair.get("label") if isinstance(pair, dict) else None
            value = pair.get("value") if isinstance(pair, dict) else None
            if not label or not value:
                continue
            dropped |= not add_field_within_budget(embed, label, value)
        if dropped:
            embed.set_footer(
                text=_("This profile is too long to show in full.")
            )
        return embed

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
            # guild_only + a Member converter: both parties are in THIS guild, so
            # they share one. No global mutual-guild scan (that is O(guilds)).
            viewer = ViewerContext(
                owner_id=member.id,
                viewer_id=ctx.author.id,
                shares_guild=True,
            )
            visible = resolve_visible_fields(profile, visibility_map, viewer)

            if not visible:
                await ctx.send(
                    _("{name} has no profile set.").format(name=member.display_name)
                )
                return

            await ctx.send(embed=self._build_embed(member, visible))

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
                await ctx.send(_invalid_value_message(error))
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
