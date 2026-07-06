import io
import logging
import os
import random

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from tools import settings
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Bundled TTF used for the rank card; falls back to Pillow's default if missing.
_FONT_PATH = os.path.join("ressources", "fonts", "impact.ttf")

# Neutral Discord avatar used when a top-ranked member has left the guild and no
# real avatar is available for the Section thumbnail accessory.
_DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"

# Components V2 budget: how many ranks get their own avatar Section (podium) and
# how many total ranks the single-page layout shows (the rest go in a text list).
_PODIUM_SLOTS = 5
_LEADERBOARD_CAP = 15

# Medal glyphs for the top three; lower ranks fall back to a plain number.
_MEDALS = {1: "\N{FIRST PLACE MEDAL}", 2: "\N{SECOND PLACE MEDAL}", 3: "\N{THIRD PLACE MEDAL}"}


class LeaderboardView(discord.ui.LayoutView):
    """Single-page Components V2 podium for the guild XP leaderboard.

    The top ranks each become a :class:`discord.ui.Section` with the member's
    avatar as a :class:`discord.ui.Thumbnail` accessory; the remaining ranks are
    collapsed into one :class:`discord.ui.TextDisplay` list to respect the V2
    component budget. It is purely presentational (no interactive components), so
    it carries no author gating.
    """

    def __init__(self, title, entries, *, timeout=180):
        # entries: list of dicts with rank, name, level, xp, avatar_url.
        super().__init__(timeout=timeout)
        self.message = None
        self._build(title, entries)

    def _build(self, title, entries):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay("## {title}".format(title=title)))
        container.add_item(discord.ui.Separator())

        podium = entries[:_PODIUM_SLOTS]
        remainder = entries[_PODIUM_SLOTS:]

        for entry in podium:
            marker = _MEDALS.get(entry["rank"], "**#{rank}**".format(rank=entry["rank"]))
            text = _("{marker} **{name}**\nLevel **{level}** - {xp} XP").format(
                marker=marker,
                name=entry["name"],
                level=entry["level"],
                xp=entry["xp"],
            )
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(text),
                    accessory=discord.ui.Thumbnail(entry["avatar_url"]),
                )
            )

        if remainder:
            container.add_item(discord.ui.Separator())
            lines = [
                _("**#{rank}** {name} - level **{level}** ({xp} XP)").format(
                    rank=entry["rank"],
                    name=entry["name"],
                    level=entry["level"],
                    xp=entry["xp"],
                )
                for entry in remainder
            ]
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        self.add_item(container)


class Leveling(commands.Cog):
    """XP and leveling commands."""

    COOLDOWN = 60

    def __init__(self, bot):
        self.bot = bot
        self._cooldowns = Cooldowns(self.COOLDOWN)

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
        if self._cooldowns.is_active(key):
            return

        self._cooldowns.touch(key)
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
                    _("{user} reached level **{level}**!").format(
                        user=message.author.mention, level=new_level
                    )
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
                    title=_("Rank | {name}").format(name=member.display_name),
                    colour=random_colour(),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name=_("Rank"), value=f"**#{rank_pos}**")
                embed.add_field(name=_("Level"), value=f"**{level}**")
                embed.add_field(name=_("XP"), value=f"**{xp}**")
                embed.add_field(
                    name=_("XP for next level"),
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
                title=_("Leaderboard | {guild}").format(guild=ctx.guild.name),
                description=_("No one has earned any XP yet!"),
                colour=random_colour(),
            )
            return await ctx.send(embed=embed)

        entries = []
        for index, row in enumerate(rows[:_LEADERBOARD_CAP], start=1):
            uid = row["user_id"]
            xp = row["xp"]
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else _("User {uid}").format(uid=uid)
            avatar_url = member.display_avatar.url if member else _DEFAULT_AVATAR_URL
            entries.append(
                {
                    "rank": index,
                    "name": name,
                    "level": self.level_for_xp(xp),
                    "xp": xp,
                    "avatar_url": avatar_url,
                }
            )

        # A LayoutView carries its own content: send it with no embed/content, and
        # suppress mentions since TextDisplay resolves them (unlike an embed).
        view = LeaderboardView(
            _("Leaderboard | {guild}").format(guild=ctx.guild.name), entries
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(Leveling(bot))
