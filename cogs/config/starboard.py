import logging

import discord
from discord.ext import commands

from tools import embed_creator
from tools.formats import random_colour
from tools.i18n import _
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorView

log = logging.getLogger(__name__)

STAR = "⭐"


# ----------------------------------------------------------------------
# Interactive channel picker (discord.ui)
# ----------------------------------------------------------------------
class _StarboardChannelSelect(discord.ui.ChannelSelect):
    """Pick the text channel that starred messages are posted to."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder=_("Pick the starboard channel"),
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        try:
            channel = self.values[0]
            await self.panel.cog._apply_set(
                interaction.guild.id, channel.id, self.panel.threshold
            )
            self.panel.stop()
            for child in self.panel.children:
                child.disabled = True
            embed = self.panel.cog._set_embed(channel, self.panel.threshold)
            await interaction.response.edit_message(
                embed=embed, view=self.panel
            )
        except Exception:
            log.exception("Starboard channel select failed")
            await embed_creator.notify_failure(interaction)


class StarboardSetView(AuthorView):
    """Author-restricted prompt to choose the starboard channel."""

    def __init__(self, cog, author_id, *, threshold, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.threshold = threshold
        self.add_item(_StarboardChannelSelect(self))


class Starboard(commands.Cog):
    """Highlight the messages your community loves the most."""

    def __init__(self, bot):
        self.bot = bot
        self._config = {}

    async def get_config(self, guild_id):
        if guild_id in self._config:
            return self._config[guild_id]

        query = """
            SELECT channel_id, threshold FROM starboard
            WHERE guild_id = $1;
            """
        row = await self.bot.db_pool.fetchrow(query, guild_id)
        config = (row["channel_id"], row["threshold"]) if row else None
        self._config[guild_id] = config
        return config

    async def _apply_set(self, guild_id, channel_id, threshold):
        """Upsert the starboard config and keep the negative-cache coherent."""

        query = """
            INSERT INTO starboard
            (guild_id, channel_id, threshold)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE
            SET channel_id = $2, threshold = $3;
            """
        await self.bot.db_pool.execute(query, guild_id, channel_id, threshold)
        self._config[guild_id] = (channel_id, threshold)

    def _set_embed(self, channel, threshold):
        embed = discord.Embed(title=_("Starboard"), colour=random_colour())
        embed.add_field(name=_("Channel"), value=channel.mention)
        embed.add_field(name=_("Threshold"), value=f"`{threshold}` {STAR}")
        return embed

    @commands.hybrid_group(name="starboard")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def starboard(self, ctx):
        """Starboard related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @starboard.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def starboard_set(
        self, ctx, channel: discord.TextChannel = None, threshold: int = 3
    ):
        """Set the starboard channel and the star threshold."""

        if channel is None:
            view = StarboardSetView(self, ctx.author.id, threshold=threshold)
            embed = discord.Embed(
                title=_("Starboard"),
                description=_(
                    "Pick the channel where starred messages should be "
                    "posted using the menu below."
                ),
                colour=random_colour(),
            )
            embed.set_footer(text=_("Only you can use this menu."))
            view.message = await ctx.send(embed=embed, view=view)
            return

        await self._apply_set(ctx.guild.id, channel.id, threshold)
        await ctx.send(embed=self._set_embed(channel, threshold))

    @starboard.command(name="threshold")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def starboard_threshold(self, ctx, value: int):
        """Update the amount of stars needed to reach the starboard."""

        query = """
            UPDATE starboard SET threshold = $2
            WHERE guild_id = $1;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, value)
        current = self._config.get(ctx.guild.id)
        if current is not None:
            self._config[ctx.guild.id] = (current[0], value)
        embed = discord.Embed(
            title=_("Starboard"), colour=random_colour()
        )
        embed.add_field(
            name=_("Threshold has been set to:"), value=f"`{value}` {STAR}"
        )
        await ctx.send(embed=embed)

    @starboard.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def starboard_disable(self, ctx):
        """Disable the starboard and wipe its configuration for this guild."""

        await self.bot.db_pool.execute(
            "DELETE FROM starboard_entries WHERE guild_id = $1;", ctx.guild.id
        )
        await self.bot.db_pool.execute(
            "DELETE FROM starboard WHERE guild_id = $1;", ctx.guild.id
        )
        self._config[ctx.guild.id] = None
        embed = discord.Embed(
            title=_("Starboard"), colour=random_colour()
        )
        embed.add_field(
            name=_("Starboard has been disabled for this guild"), value="​"
        )
        await ctx.send(embed=embed)

    @starboard.command(name="top", aliases=["leaderboard"])
    @commands.guild_only()
    async def starboard_top(self, ctx, limit: int = 10):
        """Show the most-starred messages in this guild."""

        limit = max(1, min(limit, 25))

        query = """
            SELECT message_id, star_message_id, channel_id, star_count
            FROM starboard_entries
            WHERE guild_id = $1 AND star_count > 0
            ORDER BY star_count DESC
            LIMIT $2;
            """
        rows = await self.bot.db_pool.fetch(query, ctx.guild.id, limit)

        if not rows:
            embed = discord.Embed(
                title=_("Starboard top | {guild}").format(guild=ctx.guild.name),
                description=_("No starred messages yet."),
                colour=random_colour(),
            )
            return await ctx.send(embed=embed)

        cfg = await self.get_config(ctx.guild.id)
        star_channel_id = cfg[0] if cfg else None

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for index, row in enumerate(rows, start=1):
            rank = medals.get(index, f"`#{index}`")
            count = row["star_count"]
            star_message_id = row["star_message_id"]
            target_id = star_message_id or row["message_id"]
            # Prefer the channel stored with the entry; fall back to the current
            # starboard channel for entries written before channel_id existed.
            channel_id = (
                (row["channel_id"] or star_channel_id) if star_message_id else None
            )
            if channel_id:
                url = (
                    "https://discord.com/channels/"
                    f"{ctx.guild.id}/{channel_id}/{target_id}"
                )
                link = " - " + _("[Jump]({url})").format(url=url)
            else:
                link = ""
            lines.append(f"{rank} **{count}** {STAR}{link}")

        embeds = paginate_lines(
            lines, title=_("Starboard top | {guild}").format(guild=ctx.guild.name)
        )
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    async def handle(self, payload):
        if str(payload.emoji) != STAR or payload.guild_id is None:
            return

        cfg = await self.get_config(payload.guild_id)
        if not cfg:
            return

        channel_id, threshold = cfg

        if payload.channel_id == channel_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        src = guild.get_channel_or_thread(payload.channel_id)
        if src is None:
            return

        try:
            msg = await src.fetch_message(payload.message_id)
        except Exception:
            log.exception("Failed to fetch message %s", payload.message_id)
            return

        count = 0
        for r in msg.reactions:
            if str(r.emoji) == STAR:
                count = r.count
                break

        star_ch = guild.get_channel(channel_id)
        if star_ch is None:
            return

        entry = await self.bot.db_pool.fetchval(
            "SELECT star_message_id FROM starboard_entries WHERE message_id = $1;",
            msg.id,
        )

        if count >= threshold:
            embed = discord.Embed(
                description=msg.content,
                colour=0xFFAC33,  # fixed star-gold so the colour doesn't change on every edit
                timestamp=msg.created_at,
            )
            embed.set_author(
                name=msg.author.display_name, icon_url=msg.author.display_avatar.url
            )
            embed.add_field(
                name=_("Source"),
                value=_("[Jump]({url})").format(url=msg.jump_url),
            )

            for attachment in msg.attachments:
                if attachment.content_type and attachment.content_type.startswith(
                    "image/"
                ):
                    embed.set_image(url=attachment.url)
                    break

            embed.set_footer(text=f"{count} {STAR}")

            if entry:
                try:
                    star_message = await star_ch.fetch_message(entry)
                    await star_message.edit(embed=embed)
                    await self.bot.db_pool.execute(
                        "UPDATE starboard_entries SET star_count = $2, "
                        "channel_id = $3 WHERE message_id = $1;",
                        msg.id,
                        count,
                        channel_id,
                    )
                except discord.NotFound:
                    star_message = await star_ch.send(embed=embed)
                    await self.bot.db_pool.execute(
                        "UPDATE starboard_entries SET star_message_id = $2, "
                        "star_count = $3, channel_id = $4 WHERE message_id = $1;",
                        msg.id,
                        star_message.id,
                        count,
                        channel_id,
                    )
                except Exception:
                    log.exception("Failed to edit star message %s", entry)
            else:
                star_message = await star_ch.send(embed=embed)
                try:
                    query = """
                        INSERT INTO starboard_entries
                        (message_id, guild_id, star_message_id, channel_id, star_count)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (message_id) DO UPDATE
                        SET star_message_id = $3, channel_id = $4, star_count = $5;
                        """
                    await self.bot.db_pool.execute(
                        query, msg.id, guild.id, star_message.id, channel_id, count
                    )
                except Exception:
                    log.exception("Failed to record entry, rolling back")
                    await star_message.delete()

        elif entry:
            try:
                star_message = await star_ch.fetch_message(entry)
                await star_message.delete()
            except Exception:
                log.exception("Failed to delete star message %s", entry)

            await self.bot.db_pool.execute(
                "DELETE FROM starboard_entries WHERE message_id = $1;", msg.id
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self.handle(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.handle(payload)


async def setup(bot):
    await bot.add_cog(Starboard(bot))
