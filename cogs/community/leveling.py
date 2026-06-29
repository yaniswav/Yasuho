import io
import logging
import os
import random
import time

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from tools import settings
from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)

# Bundled TTF used for the rank card; falls back to Pillow's default if missing.
_FONT_PATH = os.path.join("ressources", "fonts", "impact.ttf")


class Leveling(commands.Cog):
    """XP and leveling commands."""

    COOLDOWN = 60

    def __init__(self, bot):
        self.bot = bot
        self._cooldowns = {}

    @staticmethod
    def level_for_xp(xp):
        return int((xp / 100) ** 0.5)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        if not await settings.get_guild(
            self.bot.db_pool, message.guild.id, "leveling_enabled", False
        ):
            return

        key = (message.guild.id, message.author.id)
        now = time.time()

        if now - self._cooldowns.get(key, 0) < self.COOLDOWN:
            return

        self._cooldowns[key] = now
        gain = random.randint(15, 25)

        try:
            query = """
                INSERT INTO levels (guild_id, user_id, xp)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET xp = levels.xp + $3
                RETURNING xp;
                """

            new_xp = await self.bot.db_pool.fetchval(
                query, message.guild.id, message.author.id, gain
            )
            old_level = self.level_for_xp(new_xp - gain)
            new_level = self.level_for_xp(new_xp)

            if new_level > old_level and await settings.get_user(
                self.bot.db_pool, message.author.id, "levelup_announce", True
            ):
                await message.channel.send(
                    f"{message.author.mention} reached level **{new_level}**!"
                )

        except Exception:
            log.exception("Failed to update XP")

    @staticmethod
    def _load_font(size):
        """Load the bundled TTF at a size, falling back to Pillow's default."""
        try:
            return ImageFont.truetype(_FONT_PATH, size=size)
        except Exception:
            return ImageFont.load_default()

    @classmethod
    def _render_rank_card(
        cls,
        avatar_bytes,
        name,
        level,
        rank_pos,
        xp,
        cur_threshold,
        next_threshold,
        accent,
    ):
        """Blocking Pillow render of a member's rank card. Returns a BytesIO PNG."""
        width, height = 880, 240
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)

        # Rounded dark panel.
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=30, fill=(28, 30, 38, 255)
        )

        # Circular avatar with an accent ring on the left.
        av_size = 150
        av_x, av_y = 45, 45
        avatar = (
            Image.open(io.BytesIO(avatar_bytes))
            .convert("RGBA")
            .resize((av_size, av_size))
        )
        mask = Image.new("L", (av_size, av_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
        card.paste(avatar, (av_x, av_y), mask)
        draw.ellipse(
            (av_x - 4, av_y - 4, av_x + av_size + 4, av_y + av_size + 4),
            outline=accent,
            width=6,
        )

        text_x = av_x + av_size + 40

        # Member name, truncated to fit the available width.
        name_font = cls._load_font(40)
        name_max = width - text_x - 45
        display = name
        if draw.textlength(display, font=name_font) > name_max:
            while display and draw.textlength(
                display + "...", font=name_font
            ) > name_max:
                display = display[:-1]
            display = display + "..."
        draw.text((text_x, 48), display, font=name_font, fill=(240, 242, 248))

        # Rank + level, right-aligned on their own row.
        stat_font = cls._load_font(30)
        level_text = f"LEVEL {level}"
        rank_text = f"RANK #{rank_pos}"
        level_w = draw.textlength(level_text, font=stat_font)
        draw.text(
            (width - 45 - level_w, 108), level_text, font=stat_font, fill=accent
        )
        rank_w = draw.textlength(rank_text, font=stat_font)
        draw.text(
            (width - 45 - level_w - 28 - rank_w, 108),
            rank_text,
            font=stat_font,
            fill=(176, 182, 200),
        )

        # XP progress toward the next level.
        span = max(next_threshold - cur_threshold, 1)
        into_level = max(min(xp - cur_threshold, span), 0)
        pct = into_level / span

        bar_x, bar_y = text_x, 185
        bar_w, bar_h = width - bar_x - 45, 30
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=bar_h // 2,
            fill=(58, 61, 74, 255),
        )
        fill_w = int(bar_w * pct)
        if fill_w > 0:
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + max(fill_w, bar_h), bar_y + bar_h),
                radius=bar_h // 2,
                fill=accent,
            )

        # XP figures above the bar's right edge.
        xp_font = cls._load_font(22)
        xp_text = f"{into_level} / {span} XP"
        xp_w = draw.textlength(xp_text, font=xp_font)
        draw.text(
            (bar_x + bar_w - xp_w, bar_y - 30),
            xp_text,
            font=xp_font,
            fill=(176, 182, 200),
        )

        buf = io.BytesIO()
        card.save(buf, "PNG")
        buf.seek(0)
        return buf

    @commands.hybrid_command()
    @commands.guild_only()
    async def rank(self, ctx, member: discord.Member = None):
        """Shows your level and XP rank card, or another member's."""

        member = member or ctx.author

        xp = (
            await self.bot.db_pool.fetchval(
                "SELECT xp FROM levels WHERE guild_id = $1 AND user_id = $2;",
                ctx.guild.id,
                member.id,
            )
            or 0
        )
        level = self.level_for_xp(xp)
        cur_threshold = level**2 * 100
        next_threshold = (level + 1) ** 2 * 100
        needed = next_threshold - xp

        # Rank position within the guild (uses levels_guild_xp_idx).
        rank_pos = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) + 1 FROM levels WHERE guild_id = $1 AND xp > $2;",
            ctx.guild.id,
            xp,
        )

        async with ctx.typing():
            try:
                avatar_bytes = await member.display_avatar.replace(size=128).read()
                name = member.display_name
                accent = (
                    member.colour.to_rgb()
                    if member.colour.value
                    else (88, 101, 242)
                )

                def _render():
                    return self._render_rank_card(
                        avatar_bytes,
                        name,
                        level,
                        rank_pos,
                        xp,
                        cur_threshold,
                        next_threshold,
                        accent,
                    )

                buf = await self.bot.loop.run_in_executor(None, _render)
                await ctx.send(file=discord.File(buf, filename="rank.png"))
            except Exception:
                log.exception("Failed to render rank card")
                embed = discord.Embed(
                    title=f"Rank | {member.display_name}",
                    colour=random_colour(),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name="Rank", value=f"**#{rank_pos}**")
                embed.add_field(name="Level", value=f"**{level}**")
                embed.add_field(name="XP", value=f"**{xp}**")
                embed.add_field(
                    name="XP for next level",
                    value=f"**{needed}** ({xp}/{next_threshold})",
                    inline=False,
                )
                await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["leaderboard", "top"])
    @commands.guild_only()
    async def levels(self, ctx):
        """Shows the ranked members of the guild."""

        query = """
            SELECT user_id, xp FROM levels
            WHERE guild_id = $1
            ORDER BY xp DESC
            LIMIT 50;
            """

        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title=f"Leaderboard | {ctx.guild.name}",
                description="No one has earned any XP yet!",
                colour=random_colour(),
            )
            return await ctx.send(embed=embed)

        lines = []
        for index, row in enumerate(rows, start=1):
            uid = row["user_id"]
            xp = row["xp"]
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            level = self.level_for_xp(xp)
            lines.append(f"**{index}.** {name} - level **{level}** ({xp} XP)")

        embeds = paginate_lines(lines, title=f"Leaderboard | {ctx.guild.name}")
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
