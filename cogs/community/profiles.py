import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Nice labels for the radio picker; each value is a FIELDS key (see Profiles).
_FIELD_CHOICES = [
    ("Switch friend code", "switch"),
    ("3DS friend code", "3ds"),
    ("BattleTag", "battletag"),
    ("Riot ID", "riot"),
    ("Steam ID", "steam"),
]


class ProfileEditModal(discord.ui.Modal, title="Edit your profile"):
    """Pick a field from a radio and type its value (Components V2 modal)."""

    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.field = discord.ui.RadioGroup(required=True)
        for label, value in _FIELD_CHOICES:
            self.field.add_option(label=label, value=value)
        self.add_item(discord.ui.Label(text="Field", component=self.field))
        self.value_input = discord.ui.TextInput(
            style=discord.TextStyle.short, required=True, max_length=1000
        )
        self.add_item(discord.ui.Label(text="Value", component=self.value_input))

    async def on_submit(self, interaction):
        try:
            field = self.field.value
            value = (self.value_input.value or "").strip()
            if not field or not value:
                return await interaction.response.send_message(
                    "Pick a field and enter a value.", ephemeral=True
                )
            label = await self.cog._apply_field(interaction.user.id, field, value)
            if label is None:
                return await interaction.response.send_message(
                    "Unknown field.", ephemeral=True
                )
            embed = discord.Embed(title="Profile updated", colour=random_colour())
            embed.add_field(name=label, value=value)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Profile edit modal failed")
            await interaction.response.send_message(
                "Failed to update your profile, please try again later.",
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
    """Store and display gaming profile IDs for members."""

    # Fixed whitelist mapping field-name -> column so the column is NEVER
    # user-controlled. Only values from this dict are ever interpolated into SQL.
    FIELDS = {
        "switch": "switch_fc",
        "3ds": "threeds_fc",
        "battletag": "battletag",
        "riot": "riotid",
        "steam": "steamid",
    }

    # Friendly labels for displaying each column in the profile embed.
    LABELS = {
        "switch_fc": "Switch Friend Code",
        "threeds_fc": "3DS Friend Code",
        "battletag": "BattleTag",
        "riotid": "Riot ID",
        "steamid": "Steam ID",
    }

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="profile")
    @commands.guild_only()
    async def profile(self, ctx):
        """Gaming profile related commands."""

        if ctx.invoked_subcommand is None:
            await self.profile_view(ctx, ctx.author)

    @profile.command(name="view")
    @commands.guild_only()
    async def profile_view(self, ctx, member: discord.Member = None):
        """View a member's gaming profile."""

        member = member or ctx.author

        query = """
            SELECT switch_fc, threeds_fc, battletag, riotid, steamid
            FROM profiles
            WHERE user_id = $1;
            """

        async with ctx.typing():
            row = await self.bot.db_pool.fetchrow(query, member.id)

            if row is None or all(row[col] is None for col in self.LABELS):
                await ctx.send(f"{member.display_name} has no profile set.")
                return

            embed = discord.Embed(
                title=f"{member.display_name}'s profile",
                colour=random_colour(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)

            for col, label in self.LABELS.items():
                value = row[col]
                if value is not None:
                    embed.add_field(name=label, value=value, inline=False)

            await ctx.send(embed=embed)

    async def _apply_field(self, user_id, field, value):
        """Validate and store one profile field.

        Returns the display label, or None for an unknown field; raises on a DB
        error. The column comes ONLY from the FIELDS whitelist, so the f-string
        is safe; the user value stays a $2 parameter.
        """
        field = (field or "").lower()
        if field not in self.FIELDS:
            return None
        col = self.FIELDS[field]
        query = (
            f"INSERT INTO profiles(user_id, {col}) VALUES($1, $2) "
            f"ON CONFLICT (user_id) DO UPDATE SET {col} = $2"
        )
        await self.bot.db_pool.execute(query, user_id, value)
        return self.LABELS[col]

    @profile.command(name="set")
    @commands.guild_only()
    async def profile_set(self, ctx, field: str, *, value: str):
        """Set one of your profile fields (switch, 3ds, battletag, riot, steam)."""

        if len(value) > 1000:
            await ctx.send("That value is too long (max 1000 characters).")
            return

        async with ctx.typing():
            try:
                label = await self._apply_field(ctx.author.id, field, value)
            except Exception:
                log.exception("Failed to set field %s", field)
                await ctx.send(
                    "Failed to update your profile, please try again later."
                )
                return

            if label is None:
                await ctx.send(f"Unknown field. Choose: {', '.join(self.FIELDS)}")
                return

            embed = discord.Embed(title="Profile updated", colour=random_colour())
            embed.add_field(name=label, value=value)
            await ctx.send(embed=embed)

    @profile.command(name="edit")
    @commands.guild_only()
    async def profile_edit(self, ctx):
        """Edit a profile field through a guided form (radio picker + value)."""

        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(ProfileEditModal(self))
        else:
            view = ProfileEditView(self, ctx.author.id)
            view.message = await ctx.send(
                "Click below to edit a profile field:", view=view
            )

    @profile.command(name="clear")
    @commands.guild_only()
    async def profile_clear(self, ctx):
        """Clear your entire gaming profile."""

        query = """DELETE FROM profiles WHERE user_id = $1;"""

        async with ctx.typing():
            try:
                await self.bot.db_pool.execute(query, ctx.author.id)
            except Exception:
                log.exception("Failed to clear profile")
                await ctx.send(
                    "Failed to clear your profile, please try again later."
                )
                return

            embed = discord.Embed(
                title="Profile cleared", colour=random_colour()
            )
            embed.add_field(name="Your profile has been cleared.", value="​")
            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Profiles(bot))
