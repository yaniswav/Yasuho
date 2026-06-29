import logging

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)


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

    @profile.command(name="set")
    @commands.guild_only()
    async def profile_set(self, ctx, field: str, *, value: str):
        """Set one of your profile fields (switch, 3ds, battletag, riot, steam)."""

        field = field.lower()

        if field not in self.FIELDS:
            await ctx.send(f"Unknown field. Choose: {', '.join(self.FIELDS)}")
            return

        if len(value) > 1000:
            await ctx.send("That value is too long (max 1000 characters).")
            return

        # col comes ONLY from the whitelist dict, so this f-string is safe;
        # the user supplied value stays a $2 parameter.
        col = self.FIELDS[field]
        query = f"INSERT INTO profiles(user_id, {col}) VALUES($1, $2) ON CONFLICT (user_id) DO UPDATE SET {col} = $2"

        try:
            await self.bot.db_pool.execute(query, ctx.author.id, value)
        except Exception:
            log.exception("Failed to set field %s", field)
            await ctx.send("Failed to update your profile, please try again later.")
            return

        embed = discord.Embed(
            title="Profile updated", colour=random_colour()
        )
        embed.add_field(name=self.LABELS[col], value=value)
        await ctx.send(embed=embed)

    @profile.command(name="clear")
    @commands.guild_only()
    async def profile_clear(self, ctx):
        """Clear your entire gaming profile."""

        query = """DELETE FROM profiles WHERE user_id = $1;"""

        try:
            await self.bot.db_pool.execute(query, ctx.author.id)
        except Exception:
            log.exception("Failed to clear profile")
            await ctx.send("Failed to clear your profile, please try again later.")
            return

        embed = discord.Embed(
            title="Profile cleared", colour=random_colour()
        )
        embed.add_field(name="Your profile has been cleared.", value="​")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Profiles(bot))
