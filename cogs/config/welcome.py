import io
import logging

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from tools.formats import random_colour

log = logging.getLogger(__name__)

# Reuse a TTF already shipped with the bot (see cogs/fun/fun.py); fall back to
# PIL's bitmap default if the file is missing so a render never hard-fails.
_FONT_PATH = "ressources/fonts/impact.ttf"


class Welcome(commands.Cog):
    """Greet new members with a configurable welcome message."""

    def __init__(self, bot):
        self.bot = bot
        # Per-guild welcome config cache: {guild_id: (channel_id, message) | None}.
        # None is cached for unconfigured guilds so they cost zero queries.
        self._welcome = {}

    async def get_welcome(self, guild_id):
        if guild_id in self._welcome:
            return self._welcome[guild_id]

        query = """SELECT channel_id, message FROM welcome WHERE guild_id = $1;"""
        row = await self.bot.db_pool.fetchrow(query, guild_id)
        value = (row["channel_id"], row["message"]) if row else None
        self._welcome[guild_id] = value
        return value

    def format_msg(self, template, member):
        return (
            template.replace("{user}", member.mention)
            .replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count))
        )

    async def render_welcome_card(self, member):
        """Render a welcome card for a joining member.

        Returns a BytesIO PNG. All Pillow work runs in an executor so the join
        event loop is never blocked. The caller wraps this in try/except and
        falls back to a text-only welcome on any failure.
        """

        # Pull the avatar bytes off the loop before handing PIL the raw data.
        avatar_bytes = await member.display_avatar.replace(size=128).read()
        display_name = member.display_name
        member_count = member.guild.member_count or 0
        colour = random_colour()
        bg_rgb = ((colour >> 16) & 0xFF, (colour >> 8) & 0xFF, colour & 0xFF)

        def _render():
            width, height = 640, 200
            size = 128
            ring = 6
            card = Image.new("RGBA", (width, height), bg_rgb + (255,))
            draw = ImageDraw.Draw(card)

            # Avatar drawn in a circle, with a white ring behind it. The mask is
            # built at 4x then downscaled so the circle edge stays smooth.
            avatar = (
                Image.open(io.BytesIO(avatar_bytes))
                .convert("RGBA")
                .resize((size, size), Image.LANCZOS)
            )
            mask = Image.new("L", (size * 4, size * 4), 0)
            ImageDraw.Draw(mask).ellipse(
                (0, 0, size * 4, size * 4), fill=255
            )
            mask = mask.resize((size, size), Image.LANCZOS)

            avatar_x = 36
            avatar_y = (height - size) // 2
            draw.ellipse(
                (
                    avatar_x - ring,
                    avatar_y - ring,
                    avatar_x + size + ring,
                    avatar_y + size + ring,
                ),
                fill=(255, 255, 255, 255),
            )
            card.paste(avatar, (avatar_x, avatar_y), mask)

            try:
                title_font = ImageFont.truetype(_FONT_PATH, size=38)
                sub_font = ImageFont.truetype(_FONT_PATH, size=24)
            except Exception:
                title_font = ImageFont.load_default()
                sub_font = ImageFont.load_default()

            text_x = avatar_x + size + ring + 28
            available = width - text_x - 24

            # Shrink the greeting until it fits the remaining width so long
            # display names never overflow the card.
            name = display_name
            welcome_text = f"Welcome {name}!"
            while (
                name
                and draw.textlength(welcome_text, font=title_font) > available
            ):
                name = name[:-1]
                welcome_text = f"Welcome {name.rstrip()}...!"

            draw.text(
                (text_x, 60),
                welcome_text,
                font=title_font,
                fill=(255, 255, 255, 255),
                stroke_width=2,
                stroke_fill=(0, 0, 0, 160),
            )
            draw.text(
                (text_x, 112),
                f"Member #{member_count}",
                font=sub_font,
                fill=(255, 255, 255, 255),
                stroke_width=1,
                stroke_fill=(0, 0, 0, 160),
            )

            buf = io.BytesIO()
            card.convert("RGB").save(buf, "PNG")
            buf.seek(0)
            return buf

        return await self.bot.loop.run_in_executor(None, _render)

    @commands.hybrid_group()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome(self, ctx):
        """Welcome message related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @welcome.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_set(self, ctx, channel: discord.TextChannel, *, message: str):
        """Set the welcome channel and message.

        Placeholders: {user}, {server}, {count}.
        """

        query = """
            INSERT INTO welcome
            (guild_id, channel_id, message)
            VALUES
            ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message = $3;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, channel.id, message)
        self._welcome[ctx.guild.id] = (channel.id, message)
        embed = discord.Embed(
            title="Welcome message", colour=random_colour()
        )
        embed.add_field(name="Channel", value=channel.mention, inline=False)
        embed.add_field(name="Message", value=(message if len(message) <= 1024 else message[:1021] + "..."), inline=False)
        await ctx.send(embed=embed)

    @welcome.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_disable(self, ctx):
        """Disable the welcome message for your guild."""

        query = """DELETE FROM welcome WHERE guild_id = $1;"""

        await self.bot.db_pool.execute(query, ctx.guild.id)
        self._welcome[ctx.guild.id] = None
        embed = discord.Embed(
            title="Welcome message", colour=random_colour()
        )
        embed.add_field(
            name="Disabled", value="Welcome messages have been turned off.", inline=False
        )
        await ctx.send(embed=embed)

    @welcome.command(name="test")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx):
        """Preview the configured welcome message."""

        query = """SELECT channel_id, message FROM welcome WHERE guild_id = $1;"""

        row = await self.bot.db_pool.fetchrow(query, ctx.guild.id)
        if not row:
            await ctx.send("Welcome messages are not configured for this guild.")
            return

        await ctx.send(self.format_msg(row["message"], ctx.author))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.id in self.bot.blacklist:
            return

        config = await self.get_welcome(member.guild.id)
        if config is None:
            return

        channel_id, message = config
        channel = member.guild.get_channel(channel_id)
        if channel is None:
            return

        content = self.format_msg(message, member)

        # Render a welcome card; on any failure fall back to text only so a join
        # is never broken by image work.
        card = None
        try:
            buf = await self.render_welcome_card(member)
            card = discord.File(buf, filename="welcome.png")
        except Exception:
            log.exception("Failed to render welcome card")
            card = None

        try:
            if card is not None:
                await channel.send(content, file=card)
            else:
                await channel.send(content)
        except Exception:
            log.exception("Failed to send welcome message")


async def setup(bot):
    await bot.add_cog(Welcome(bot))
