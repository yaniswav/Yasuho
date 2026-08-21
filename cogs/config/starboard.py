import asyncio
import logging
from contextlib import asynccontextmanager

import discord
from discord.ext import commands

from tools import embed_creator
from tools.formats import random_colour
from tools.i18n import _
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

STAR = "⭐"

# How far a starred message may fall back BEFORE its starboard post is removed.
# Posting at ``threshold`` and removing only below ``threshold - _UNSTAR_MARGIN``
# means one member toggling their star on and off drives an EDIT each way
# instead of a delete/send pair: the star post keeps its id, its permalink and
# its own reactions, and the starboard channel stops flickering.
_UNSTAR_MARGIN = 1


def _keep_floor(threshold):
    """The star count an EXISTING starboard post survives down to.

    Never below 1: at threshold 1 there is no band to give away, and a message
    nobody stars any more must always leave the starboard.
    """
    return max(1, threshold - _UNSTAR_MARGIN)


def _star_count(message):
    """How many stars ``message`` carries right now (0 if none)."""
    for reaction in message.reactions:
        if str(reaction.emoji) == STAR:
            return reaction.count
    return 0


# ----------------------------------------------------------------------
# THE REPUBLISH RULE: never widen a message's audience
# ----------------------------------------------------------------------
# The starboard copies somebody's message into ANOTHER channel, so it may only
# ever show the starboard's audience something that audience could already read.
# Without this gate, one star on a staff-only channel published the staff
# conversation to the whole server. A message is republished only when:
#
#   * @everyone can view its source channel - the ordinary public case, one
#     permission fold and nothing more; OR
#   * every ROLE that can view the STARBOARD channel can also view the SOURCE
#     channel. That second clause is what keeps the feature working in a
#     members-only server where nothing at all is @everyone-visible: the
#     starboard may never WIDEN an audience, but it is allowed to keep one.
#
# and never when:
#
#   * the source is a PRIVATE thread - invite-only whatever its parent allows;
#   * the source is age-restricted while the starboard channel is not - that
#     would hand the whole server what Discord gates behind an age check.
#
# Anything that cannot be evaluated (a partial channel, no guild, a lookup that
# raises) answers NO. A starboard that quietly skips a message is a support
# question; a starboard that publishes the staff channel is an incident.
#
# A NO is not only "do not publish": the caller also RETRACTS any post that is
# already up for that message. The verdict is asked live, so it can flip long
# after a post was made, and a gate that merely stopped publishing would strand
# the old copy on a public board for good - unstarring it would hit the same
# gate and stop before the removal. Retraction always moves in the private
# direction, so an unevaluable channel losing its star post is the safe way to
# be wrong.
#
# Per-MEMBER overwrites are deliberately out of scope: the audience is compared
# role by role, which is how servers actually gate channels, and a lone member
# grant on the starboard channel cannot make the source readable to anyone else.


def _is_private_thread(channel):
    check = getattr(channel, "is_private", None)
    return bool(check()) if callable(check) else False


def _is_age_restricted(channel):
    check = getattr(channel, "is_nsfw", None)
    return bool(check()) if callable(check) else False


def _may_republish(src, star_ch):
    """Whether ``src``'s content may be posted into ``star_ch``. See the rule above."""

    guild = getattr(src, "guild", None)
    everyone = getattr(guild, "default_role", None)
    if src is None or star_ch is None or everyone is None:
        return False

    try:
        if _is_private_thread(src):
            return False
        if _is_age_restricted(src) and not _is_age_restricted(star_ch):
            return False
        if src.permissions_for(everyone).view_channel:
            return True

        roles = tuple(getattr(guild, "roles", ()) or ())
        audience = {
            role.id for role in roles if star_ch.permissions_for(role).view_channel
        }
        if not audience:
            # Nobody reaches the starboard through a role: unverifiable, refuse.
            return False
        readers = {
            role.id for role in roles if src.permissions_for(role).view_channel
        }
        return audience <= readers
    except Exception:
        log.debug("Starboard visibility check failed", exc_info=True)
        return False


# ----------------------------------------------------------------------
# Interactive configuration form (discord.ui)
# ----------------------------------------------------------------------
class StarboardSetModal(LocaleModal):
    """One form combining the channel picker and the star threshold.

    A Label-wrapped :class:`discord.ui.ChannelSelect` (text channels only) sits
    above a short "Star threshold" text field. On submit the threshold is
    validated as a positive whole number and persisted through the cog's
    existing ``_apply_set``.
    """

    def __init__(self, cog, *, threshold=3, panel=None):
        super().__init__(title=_("Configure the starboard"))
        self.cog = cog
        self.panel = panel

        self.channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder=_("Pick the starboard channel"),
            min_values=1,
            max_values=1,
        )
        self.add_item(
            discord.ui.Label(
                text=_("Starboard channel"), component=self.channel_select
            )
        )

        self.threshold_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            placeholder=_("How many stars a message needs"),
            default=str(threshold),
            max_length=6,
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text=_("Star threshold"), component=self.threshold_input
            )
        )

    async def on_submit(self, interaction):
        try:
            raw = (self.threshold_input.value or "").strip()
            try:
                threshold = int(raw)
            except ValueError:
                threshold = 0
            if threshold < 1:
                return await embed_creator.notify_failure(
                    interaction,
                    _("The star threshold must be a positive whole number."),
                )

            values = self.channel_select.values
            if not values:
                return await embed_creator.notify_failure(
                    interaction, _("Please pick a starboard channel.")
                )
            channel = values[0]

            await self.cog._apply_set(interaction.guild.id, channel.id, threshold)
            embed = self.cog._set_embed(channel, threshold)

            # Prefix path: refresh the button panel in place and acknowledge
            # the modal quietly. Slash path (no panel): post the result.
            if self.panel is not None:
                self.panel.stop()
                for child in self.panel.children:
                    child.disabled = True
                if self.panel.message is not None:
                    try:
                        await self.panel.message.edit(embed=embed, view=self.panel)
                    except discord.HTTPException:
                        pass
                await interaction.response.send_message(
                    _("Starboard configured!"), ephemeral=True
                )
            else:
                await interaction.response.send_message(embed=embed)
        except Exception:
            log.exception("Starboard set modal failed")
            await embed_creator.notify_failure(interaction)


class StarboardSetView(AuthorView):
    """Author-restricted prompt that opens the starboard configuration form."""

    def __init__(self, cog, author_id, *, threshold, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.threshold = threshold
        self.configure.label = _("Configure the starboard")

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def configure(self, interaction, button):
        try:
            await interaction.response.send_modal(
                StarboardSetModal(self.cog, threshold=self.threshold, panel=self)
            )
        except Exception:
            log.exception("Starboard set modal launch failed")
            await embed_creator.notify_failure(interaction)


class Starboard(commands.Cog):
    """Highlight the messages your community loves the most."""

    def __init__(self, bot):
        self.bot = bot
        self._config = {}
        # message_id -> [lock, waiter_count]; serializes concurrent star
        # reactions on the same message and is pruned once nobody holds it, so
        # it cannot grow without bound.
        self._locks = {}

    @asynccontextmanager
    async def _message_lock(self, message_id):
        entry = self._locks.get(message_id)
        if entry is None:
            entry = self._locks[message_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(message_id, None)

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
        """Manage the starboard: set up, adjust, disable, or view the top messages."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @starboard.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        channel="The channel where starred messages are posted.",
        threshold="How many stars a message needs (default 3).",
    )
    async def starboard_set(
        self, ctx, channel: discord.TextChannel = None, threshold: int = 3
    ):
        """Set the starboard channel and the star threshold."""

        if channel is None:
            # Slash invocation can pop the modal straight away; a prefix
            # invocation has no interaction, so offer a button that opens it.
            if ctx.interaction is not None:
                await ctx.interaction.response.send_modal(
                    StarboardSetModal(self, threshold=threshold)
                )
                return

            view = StarboardSetView(self, ctx.author.id, threshold=threshold)
            embed = discord.Embed(
                title=_("Starboard"),
                description=_(
                    "Use the button below to pick the channel where starred "
                    "messages should be posted and set the star threshold."
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
    @discord.app_commands.describe(value="How many stars a message needs to reach the starboard.")
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
    @discord.app_commands.describe(limit="How many messages to show (max 25, default 10).")
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

    def _cached_message(self, message_id):
        """The message out of discord.py's own cache, or ``None`` on a miss.

        ``Client.cached_messages`` is the bot-wide 1000-entry deque; scanning it
        NEWEST FIRST finds a message somebody just starred within the first few
        entries. Its reaction counts are maintained by the gateway parser, and
        that parser finishes its synchronous pass before the task it dispatched
        for this listener gets to run - so the count read here already includes
        the reaction that woke us.
        """
        cached = getattr(self.bot, "cached_messages", None)
        if not cached:
            return None
        try:
            for message in reversed(cached):
                if message.id == message_id:
                    return message
        except TypeError:  # pragma: no cover - a stand-in with no __reversed__
            return None
        return None

    async def handle(self, payload):
        """React to one star being added or removed.

        REQUEST BUDGET, for R star reactions per second across the whole fleet
        (every other emoji leaves on the first line, for free):

        * source message: 0 REST on a cache hit - which is the normal case,
          since a message being starred is a message people are looking at - and
          exactly 1 ``fetch_message`` on a miss. It used to be 1 EVERY time.
        * starboard post: 0 REST always. It is addressed by id through
          ``get_partial_message``, so editing or deleting it no longer costs a
          ``fetch_message`` first.
        * the write itself: at most 1 REST (one edit, one send or one delete),
          and 0 when the displayed count did not change - a re-delivered event
          after a gateway resume, or a star from someone who already starred it.
        * a reaction that neither reaches the threshold nor touches an existing
          post: 0 REST at all.
        * a reaction in a channel the audience gate refuses: 0 REST, plus one
          indexed row read to see whether an old post has to be retracted.

        THE TRADE that "0 when the displayed count did not change" buys: a
        starboard post somebody DELETED BY HAND is no longer put back on the
        next identical reaction, because nothing is fetched to notice it is
        gone. It comes back as soon as the count moves (the edit 404s and the
        post is re-sent, once). Restoring it sooner meant one fetch_message per
        star, fleet-wide, forever.

        So the ceiling is R REST calls per second instead of the old 2R..3R, the
        common cases are free, and crossing back under the threshold edits the
        post rather than deleting and re-sending it (see ``_UNSTAR_MARGIN``).
        The listener itself stays O(1) per event: two cache dict lookups, one
        bounded scan of the message deque, one row read.
        """
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

        star_ch = guild.get_channel(channel_id)
        if star_ch is None:
            return

        # The audience gate runs BEFORE the message is read, so starring a
        # private channel costs no Discord request at all - and can never post
        # one. The verdict can also FLIP after a post exists (the source channel
        # is made private, or age-restricted, later on), and refusing to publish
        # is only half the rule then: a copy of what is now a private
        # conversation would sit on a public board with no way off it, since
        # every later add and remove would stop here too. So a NO also retracts.
        # That reads one row; it touches Discord only when there is really a
        # post to take down.
        if not _may_republish(src, star_ch):
            async with self._message_lock(payload.message_id):
                await self._retract(payload.message_id, star_ch)
            return

        msg = self._cached_message(payload.message_id)
        if msg is None:
            try:
                msg = await src.fetch_message(payload.message_id)
            except Exception:
                log.exception("Failed to fetch message %s", payload.message_id)
                return

        # Serialize per source message so two near-simultaneous star reactions
        # cannot both see "no entry yet" and each post a duplicate starboard
        # message (which the ON CONFLICT below would then orphan). The count is
        # read INSIDE the lock: on a cached message it is live, so the waiter
        # writes the count as of its turn rather than as of its arrival.
        async with self._message_lock(msg.id):
            await self._sync_star(
                msg, guild, star_ch, channel_id, _star_count(msg), threshold
            )

    def _star_embed(self, msg, count):
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
        return embed

    async def _sync_star(self, msg, guild, star_ch, channel_id, count, threshold):
        row = await self.bot.db_pool.fetchrow(
            "SELECT star_message_id, star_count FROM starboard_entries "
            "WHERE message_id = $1;",
            msg.id,
        )
        entry = row["star_message_id"] if row else None
        stored = row["star_count"] if row else None

        # A post appears at the threshold and survives down to the floor, so a
        # star toggled on and off around the threshold edits the post twice
        # instead of deleting and re-sending it.
        if count >= threshold or (
            entry is not None and count >= _keep_floor(threshold)
        ):
            if entry is not None and stored == count:
                # Nothing on screen would change: spend no request.
                return

            embed = self._star_embed(msg, count)

            if entry is not None:
                try:
                    await star_ch.get_partial_message(entry).edit(embed=embed)
                except discord.NotFound:
                    # Somebody deleted the star post: put it back, once.
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
                    await self.bot.db_pool.execute(
                        "UPDATE starboard_entries SET star_count = $2, "
                        "channel_id = $3 WHERE message_id = $1;",
                        msg.id,
                        count,
                        channel_id,
                    )
                return

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
            return

        if entry is not None:
            await self._drop_entry(star_ch, msg.id, entry)

    async def _retract(self, message_id, star_ch):
        """Take a starboard post down, if there is one. Costs one row read.

        The audience-gate path: it has no message and no count, only the id that
        was reacted to, so it asks the table whether anything was ever posted
        for it. Nothing there (the ordinary case for a private channel) means no
        Discord request at all.
        """
        row = await self.bot.db_pool.fetchrow(
            "SELECT star_message_id FROM starboard_entries WHERE message_id = $1;",
            message_id,
        )
        entry = row["star_message_id"] if row else None
        if entry is None:
            return
        await self._drop_entry(star_ch, message_id, entry)

    async def _drop_entry(self, star_ch, message_id, entry):
        """Delete the starboard post and forget the entry. One REST at most."""
        try:
            await star_ch.get_partial_message(entry).delete()
        except discord.NotFound:
            pass
        except Exception:
            log.exception("Failed to delete star message %s", entry)

        await self.bot.db_pool.execute(
            "DELETE FROM starboard_entries WHERE message_id = $1;", message_id
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self.handle(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.handle(payload)


async def setup(bot):
    await bot.add_cog(Starboard(bot))
