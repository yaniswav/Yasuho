import io
import logging
import math
import time

import discord
from discord.ext import commands
from PIL import Image

from tools.formats import random_colour

log = logging.getLogger(__name__)


class AvatarHistory(commands.Cog):
    """Records users' avatar changes and builds a collage of their history."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        if before.display_avatar.key == after.display_avatar.key:
            return

        try:
            data = await after.display_avatar.replace(
                size=128, format="png"
            ).read()
            await self.bot.db_pool.execute(
                "INSERT INTO avatar_history(user_id, avatar) VALUES($1, $2)",
                after.id,
                data,
            )
            await self.bot.db_pool.execute(
                "DELETE FROM avatar_history WHERE user_id = $1 AND id NOT IN "
                "(SELECT id FROM avatar_history WHERE user_id = $1 ORDER BY changed_at DESC LIMIT 50)",
                after.id,
            )
        except Exception:
            log.exception("failed to record avatar change")

    @staticmethod
    def build_collage(images):
        cell = 96
        n = len(images)
        cols = min(8, max(1, math.ceil(math.sqrt(n))))
        rows = math.ceil(n / cols)
        canvas = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
        for i, raw in enumerate(images):
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGBA").resize(
                    (cell, cell)
                )
                canvas.paste(im, ((i % cols) * cell, (i // cols) * cell))
            except Exception:
                continue
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        buf.seek(0)
        return buf

    @commands.hybrid_command(aliases=["avh"])
    async def avatarhistory(self, ctx, member: discord.User = None):
        """Build a collage of a user's past avatars."""

        member = member or ctx.author
        async with ctx.typing():
            start = time.perf_counter()
            rows = await self.bot.db_pool.fetch(
                "SELECT avatar FROM avatar_history WHERE user_id = $1 ORDER BY changed_at DESC LIMIT 50",
                member.id,
            )
            if not rows:
                return await ctx.send(
                    f"No avatar history recorded for {member} yet."
                )
            images = [bytes(r["avatar"]) for r in rows]
            buf = await self.bot.loop.run_in_executor(
                None, self.build_collage, images
            )
            elapsed = time.perf_counter() - start
            embed = discord.Embed(
                title="Avatar History",
                colour=random_colour(),
            )
            embed.set_author(
                name=f"{member} ({member.id})",
                icon_url=member.display_avatar.url,
            )
            embed.description = (
                f"Generating took `{elapsed:.2f}s`\n"
                f"Showing `{len(images)}` of up to `50` changes"
            )
            embed.set_image(url="attachment://avatars.png")
            await ctx.send(embed=embed, file=discord.File(buf, "avatars.png"))


async def setup(bot):
    await bot.add_cog(AvatarHistory(bot))
