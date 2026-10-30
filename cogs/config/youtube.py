"""YouTube upload alerts: `/youtube add|remove|list|settings`.

A server follows YouTube channels; when one uploads a video the bot posts the link
in the chosen text channel (the player embeds by itself), optionally pinging ONE
configured role.

SOURCE. YouTube's public per-channel Atom feed (no API key, see tools/youtube.py:
the 15 latest uploads, no ETag so no conditional GET). Input is a channel id, an
``@handle`` or a youtube.com URL; a handle costs ONE page fetch at add time.

DEDUP. ``youtube_follows.seen`` holds the newest video ids already handled per
(server, channel), persisted BEFORE the post (at-most-once: a crash between the two
loses one post, never repeats it), so a restart re-posts nothing. Adding a channel
reads its feed at once and records what exists (the BASELINE): nothing is posted
for videos that predate the follow. If that first read fails the row stays
``baselined = false`` and the first successful poll baselines it silently.
Videos older than 3 days are never posted (a private video turned public, a long
outage), at most 3 per follow per cycle, and Shorts (feed link ``/shorts/``) are
skipped unless the server turns them on.

TIERS. Free servers follow 3 channels, Yasuho+ servers 25
(``GuildLimits.max_youtube_follows``). A lapse never deletes a row: follows beyond
the current limit (oldest first are kept) stay stored but are neither polled nor
posted; renewing brings them back, baseline unchanged.

SCALE. One feed fetch per DISTINCT followed channel per cycle (10 min), shared by
every server following it, with a global concurrency of 4, a per-channel
exponential backoff (up to 6 h; 24 h for a deleted channel) and a response size
cap. At 1000 servers x 3 follows with heavy overlap that is at most 3000
fetches / 10 min = 5 / s worst case, usually far fewer; the dedup cost is one
indexed UPDATE per follow only when something changed (a new video or a first
baseline), and a tick otherwise costs one SELECT.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import datetime
import logging
import re
import time

import discord
from discord.ext import commands, tasks

from tools import premium, premium_upsell, settings
from tools.cooldowns import Cooldowns
from tools.http import get_session
from tools.i18n import _, resolve_guild_locale
from tools.i18n import locale as use_locale
from tools.paginator import Paginator, paginate_lines
from tools.snowflake import coerce_id
from tools.youtube import (
    FeedError,
    FeedGone,
    fetch_feed,
    parse_channel_input,
    resolve_path,
)

log = logging.getLogger(__name__)

YOUTUBE_RED = 0xFF0000
POLL_SECONDS = 600
CONCURRENCY = 4
MAX_BACKOFF = 6 * 3600
GONE_BACKOFF = 24 * 3600
GONE_AFTER = 3  # consecutive 404s before a feed counts as deleted
ADD_COOLDOWN_SECONDS = 20
SEEN_CAP = 50
MAX_POSTS_PER_CYCLE = 3
MAX_AGE = datetime.timedelta(days=3)
MAX_MESSAGE = 500
_LOCK_NS = 0x5954  # 'YT'
_INT4_FOLD = 2147483647
SETTINGS_KEY = "youtube"
PLACEHOLDERS = "{channel} {title} {url}"


def _config_from(blob):
    """A fresh config dict merged over the defaults (never aliases the cache)."""

    blob = blob if isinstance(blob, dict) else {}
    message = blob.get("message")
    return {
        "role_id": coerce_id(blob.get("role_id")),
        "message": message.strip()[:MAX_MESSAGE] if isinstance(message, str) else "",
        "shorts": bool(blob.get("shorts")),
    }


_PLACEHOLDER_RE = re.compile(r"\{(channel|title|url)\}")


def render_template(template, channel_title, entry):
    """Fill {channel} {title} {url} in ONE pass, so a title that itself contains
    a placeholder is never expanded again. Names are markdown-escaped."""

    values = {
        "channel": discord.utils.escape_markdown(channel_title or ""),
        "title": discord.utils.escape_markdown(entry.title[:200]),
        "url": entry.url,
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def plan_follow(entries, seen, baselined, now, include_shorts):
    """What one follow does with a fetched feed: ``(new_seen, to_post)``.

    ``new_seen`` is the cursor to persist (current feed ids first, then older
    ones, capped); ``to_post`` is oldest-first and empty on a baseline run.
    """

    ids = [e.video_id for e in entries]
    new_seen = (ids + [s for s in seen if s not in ids])[:SEEN_CAP]
    if not baselined:
        return new_seen, []
    known = set(seen)
    eligible = [
        e
        for e in entries
        if e.video_id not in known
        and now - e.published <= MAX_AGE
        and (include_shorts or not e.is_short)
    ]
    eligible.sort(key=lambda e: e.published)
    return new_seen, eligible[-MAX_POSTS_PER_CYCLE:]


def active_rows(rows, limit):
    """The rows a guild may poll: its ``limit`` oldest follows (rows pre-sorted)."""

    return rows[:limit]


def backoff_delay(fails, gone=False):
    """Seconds to wait before asking a failing channel again."""

    if gone:
        return GONE_BACKOFF
    return min(POLL_SECONDS * (2 ** max(fails - 1, 0)), MAX_BACKOFF)


class YouTubeAlerts(commands.Cog):
    """Post when a followed YouTube channel uploads a new video."""

    def __init__(self, bot):
        self.bot = bot
        # yt channel id -> (consecutive failures, monotonic time it may be asked
        # again, consecutive 404s). Keyed by channel, never by guild; pruned every
        # tick. A 404 is often transient, so only GONE_AFTER in a row means gone.
        self._backoff = {}
        self._add_cooldown = Cooldowns(ADD_COOLDOWN_SECONDS)

    async def cog_load(self):
        self._poll.start()

    def cog_unload(self):
        self._poll.cancel()

    # -- config ---------------------------------------------------------
    async def get_config(self, guild_id):
        return _config_from(await settings.get_guild(self.bot.db_pool, guild_id, SETTINGS_KEY, None))

    async def save_config(self, guild_id, config):
        await settings.set_guild(self.bot.db_pool, guild_id, SETTINGS_KEY, config)

    # -- poller ---------------------------------------------------------
    @tasks.loop(seconds=POLL_SECONDS)
    async def _poll(self):
        try:
            await self._tick()
        except Exception:
            log.exception("YouTube alerts: poll tick failed")

    @_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @_poll.error
    async def _poll_error(self, error):
        log.exception("YouTube alerts: poll loop crashed; restarting", exc_info=error)
        self._poll.restart()

    async def _tick(self, now=None, mono=None):
        now = now or discord.utils.utcnow()
        mono = time.monotonic() if mono is None else mono
        rows = await self.bot.db_pool.fetch(
            "SELECT guild_id, yt_channel_id, post_channel, baselined, seen, title "
            "FROM youtube_follows ORDER BY guild_id, added_at, yt_channel_id"
        )
        by_guild = {}
        for row in rows:
            by_guild.setdefault(row["guild_id"], []).append(row)
        jobs = {}  # yt channel id -> [(guild, row)]
        for guild_id, guild_rows in by_guild.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            limit = premium.resolve_guild_limits(self.bot, guild_id).max_youtube_follows
            for row in active_rows(guild_rows, limit):
                jobs.setdefault(row["yt_channel_id"], []).append((guild, row))
        self._backoff = {k: v for k, v in self._backoff.items() if k in jobs}
        due = [c for c in jobs if self._backoff.get(c, (0, 0, 0))[1] <= mono]
        if not due:
            return

        session = get_session(self.bot)
        gate = asyncio.Semaphore(CONCURRENCY)

        async def fetch(channel_id):
            async with gate:
                try:
                    return channel_id, await fetch_feed(session, channel_id), None
                except (FeedGone, FeedError) as exc:
                    return channel_id, None, exc

        # ONE fetch per distinct channel, however many servers follow it; each
        # result is handled as soon as it lands so a slow feed delays nobody.
        for finished in asyncio.as_completed([fetch(c) for c in due]):
            channel_id, feed, error = await finished
            if error is not None:
                fails, _until, gones = self._backoff.get(channel_id, (0, 0, 0))
                fails += 1
                gones = gones + 1 if isinstance(error, FeedGone) else 0
                delay = backoff_delay(fails, gone=gones >= GONE_AFTER)
                self._backoff[channel_id] = (fails, mono + delay, gones)
                log.warning("YouTube alerts: %s unavailable (%s)", channel_id, error)
                continue
            self._backoff.pop(channel_id, None)
            for guild, row in jobs[channel_id]:
                try:
                    await self._handle(guild, row, feed, now)
                except Exception:
                    log.exception("YouTube alerts: follow %s/%s failed", guild.id, channel_id)

    async def _handle(self, guild, row, feed, now):
        config = await self.get_config(guild.id)
        new_seen, to_post = plan_follow(
            feed.entries, list(row["seen"]), row["baselined"], now, config["shorts"]
        )
        if row["baselined"] and new_seen == list(row["seen"]) and feed.title == row["title"]:
            return
        # Persist BEFORE posting: a crash loses a post, never repeats one. A row
        # removed meanwhile (UPDATE 0) posts nothing.
        result = await self.bot.db_pool.execute(
            "UPDATE youtube_follows SET seen = $3, baselined = TRUE, title = $4 "
            "WHERE guild_id = $1 AND yt_channel_id = $2",
            guild.id,
            row["yt_channel_id"],
            new_seen,
            feed.title or row["title"],
        )
        if not to_post or result.endswith(" 0"):
            return
        await self._post(guild, row["post_channel"], config, feed.title, to_post)

    async def _post(self, guild, channel_id, config, channel_title, entries):
        channel = guild.get_channel(channel_id)
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return
        role = guild.get_role(config["role_id"]) if config["role_id"] else None
        mentions = discord.AllowedMentions(
            everyone=False, users=False, roles=[role] if role is not None else False
        )
        loc = await resolve_guild_locale(self.bot, guild)
        with use_locale(loc):
            template = config["message"] or _(
                "{channel} just uploaded a new video: {title}\n{url}"
            )
        for entry in entries:
            text = render_template(template, channel_title, entry)
            if role is not None:
                text = role.mention + " " + text
            try:
                await channel.send(text[:2000], allowed_mentions=mentions)
            except discord.HTTPException:
                log.warning("YouTube alerts: post failed in guild %s", guild.id)
                return

    # -- commands -------------------------------------------------------
    @commands.hybrid_group(name="youtube", aliases=["yt"])
    @commands.guild_only()
    async def youtube(self, ctx):
        """Get a post when a YouTube channel uploads: add, remove, list, settings."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @youtube.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        channel="A YouTube channel link, @handle or channel id.",
        post_in="Where to post the new videos.",
    )
    async def youtube_add(
        self, ctx, channel: str, post_in: discord.TextChannel
    ):
        """Follow a YouTube channel and post its new videos here."""

        await ctx.defer()
        none = discord.AllowedMentions.none()
        parsed = parse_channel_input(channel)
        if parsed is None:
            return await ctx.send(
                _("I need a YouTube channel link, an @handle or a channel id."),
                allowed_mentions=none,
            )
        if self._add_cooldown.is_active(ctx.guild.id):
            return await ctx.send(
                _("Slow down a little, then try again in a few seconds."), allowed_mentions=none
            )
        self._add_cooldown.touch(ctx.guild.id)
        try:
            channel_id, feed = await resolve_follow(get_session(self.bot), parsed)
        except FeedError:
            return await ctx.send(
                _("I could not reach YouTube right now. Try again in a moment."),
                allowed_mentions=none,
            )
        if channel_id is None:
            return await ctx.send(_("I could not find that YouTube channel."), allowed_mentions=none)
        if not post_in.permissions_for(ctx.guild.me).send_messages:
            return await ctx.send(
                _("I cannot send messages in {channel}.").format(channel=post_in.mention),
                allowed_mentions=none,
            )
        limit = premium.resolve_guild_limits(self.bot, ctx.guild.id).max_youtube_follows
        status = await add_follow(
            self.bot.db_pool, ctx.guild.id, channel_id, post_in.id, limit, feed
        )
        if status == "limit":
            return await premium_upsell.refuse_in_context(
                self.bot,
                ctx,
                _("This server already follows the maximum of {n} YouTube channels.").format(n=limit),
                limit_key="max_youtube_follows",
                benefit=str(premium.GUILD_PREMIUM.max_youtube_follows),
            )
        name = feed.title if feed is not None and feed.title else channel_id
        await ctx.send(
            _("Following {name}. New videos will be posted in {channel}.").format(
                name=discord.utils.escape_markdown(name), channel=post_in.mention
            ),
            allowed_mentions=none,
        )

    @youtube.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(channel="The channel link, @handle, id or name you followed.")
    async def youtube_remove(self, ctx, *, channel: str):
        """Stop following a YouTube channel."""

        await ctx.defer()
        none = discord.AllowedMentions.none()
        parsed = parse_channel_input(channel)
        channel_id = parsed[1] if parsed is not None and parsed[0] == "id" else None
        if channel_id is None:
            # A handle or a name: the stored title first (no network)...
            needle = channel.strip().lstrip("@").casefold()
            channel_id = await self.bot.db_pool.fetchval(
                "SELECT yt_channel_id FROM youtube_follows "
                "WHERE guild_id = $1 AND lower(title) = $2 LIMIT 1",
                ctx.guild.id,
                needle,
            )
        if channel_id is None and parsed is not None and parsed[0] == "path":
            # ...then the same resolution `add` uses.
            try:
                channel_id = await resolve_path(get_session(self.bot), parsed[1])
            except FeedError:
                channel_id = None
        row = None
        if channel_id is not None:
            row = await self.bot.db_pool.fetchrow(
                "DELETE FROM youtube_follows WHERE guild_id = $1 AND yt_channel_id = $2 RETURNING 1",
                ctx.guild.id,
                channel_id,
            )
        if row is None:
            return await ctx.send(_("This server does not follow that channel."), allowed_mentions=none)
        await ctx.send(_("Unfollowed."), allowed_mentions=none)

    @youtube.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def youtube_list(self, ctx):
        """Show the followed YouTube channels."""

        await ctx.defer()
        rows = await self.bot.db_pool.fetch(
            "SELECT yt_channel_id, post_channel, title FROM youtube_follows "
            "WHERE guild_id = $1 ORDER BY added_at, yt_channel_id",
            ctx.guild.id,
        )
        limit = premium.resolve_guild_limits(self.bot, ctx.guild.id).max_youtube_follows
        lines = []
        for index, row in enumerate(rows):
            line = "**{name}** (`{id}`) -> <#{channel}>".format(
                name=discord.utils.escape_markdown(row["title"] or row["yt_channel_id"]),
                id=row["yt_channel_id"],
                channel=row["post_channel"],
            )
            if index >= limit:
                line += " - " + _("paused (over the limit)")
            lines.append(line)
        if not lines:
            return await ctx.send(_("This server follows no YouTube channel yet."))
        embeds = paginate_lines(
            lines, title=_("Followed YouTube channels"), colour=YOUTUBE_RED, per_page=10
        )
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    @youtube.command(name="settings")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        role="A role to ping on every new video.",
        message="Custom text: {channel} {title} {url}.",
        shorts="Also post YouTube Shorts.",
        reset="Clear the role and the custom text.",
    )
    async def youtube_settings(
        self,
        ctx,
        role: discord.Role = None,
        message: str = None,
        shorts: bool = None,
        reset: bool = False,
    ):
        """Set the ping role, the message and whether Shorts are posted."""

        await ctx.defer()
        none = discord.AllowedMentions.none()
        config = await self.get_config(ctx.guild.id)
        if role is not None and role.is_default():
            return await ctx.send(_("I will not ping everyone."), allowed_mentions=none)
        # Same rule as the event reminders: a role the manager could ping by hand.
        if role is not None and not role.mentionable and not (
            ctx.author.guild_permissions.mention_everyone
            and ctx.guild.me.guild_permissions.mention_everyone
        ):
            return await ctx.send(_("You can't ping that role."), allowed_mentions=none)
        if reset:
            config["role_id"], config["message"] = None, ""
        if role is not None:
            config["role_id"] = role.id
        if message is not None:
            config["message"] = message.strip()[:MAX_MESSAGE]
        if shorts is not None:
            config["shorts"] = shorts
        if reset or role is not None or message is not None or shorts is not None:
            await self.save_config(ctx.guild.id, config)
        embed = discord.Embed(title=_("YouTube alerts"), colour=YOUTUBE_RED)
        embed.add_field(
            name=_("Ping role"),
            value=f"<@&{config['role_id']}>" if config["role_id"] else _("None"),
            inline=True,
        )
        embed.add_field(
            name=_("Shorts"), value=_("Posted") if config["shorts"] else _("Skipped"), inline=True
        )
        embed.add_field(
            name=_("Message"),
            value=discord.utils.escape_markdown(config["message"]) or _("Default"),
            inline=False,
        )
        embed.set_footer(text=_("Placeholders: {placeholders}").format(placeholders=PLACEHOLDERS))
        await ctx.send(embed=embed, allowed_mentions=none)


async def resolve_follow(session, parsed):
    """``(channel_id, feed)`` for a parsed input; ``(None, None)`` when not found.

    A 404 is often transient on YouTube's side, so "not found" is only answered
    after one retry. ``feed`` is None when the feed itself could not be read (the
    first poll baselines the follow instead). Raises FeedError when YouTube is
    unreachable.
    """

    for attempt in range(2):
        channel_id = parsed[1]
        if parsed[0] == "path":
            channel_id = await resolve_path(session, parsed[1])
        if channel_id is None:
            continue
        try:
            return channel_id, await fetch_feed(session, channel_id)
        except FeedGone:
            continue
        except FeedError:
            return channel_id, None
    return None, None


async def add_follow(pool, guild_id, channel_id, post_channel, limit, feed):
    """Insert a follow under a per-guild lock; returns ``new``, ``updated`` or ``limit``.

    With a fetched ``feed`` the follow is baselined at once (its existing videos
    are recorded as seen); without one it stays un-baselined for the first poll.
    Following a channel already followed only moves its post channel.
    """

    seen = [e.video_id for e in feed.entries][:SEEN_CAP] if feed is not None else []
    title = feed.title if feed is not None else ""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1, $2)", _LOCK_NS, guild_id % _INT4_FOLD
            )
            exists = await conn.fetchval(
                "SELECT 1 FROM youtube_follows WHERE guild_id = $1 AND yt_channel_id = $2",
                guild_id,
                channel_id,
            )
            if exists:
                await conn.execute(
                    "UPDATE youtube_follows SET post_channel = $3 "
                    "WHERE guild_id = $1 AND yt_channel_id = $2",
                    guild_id,
                    channel_id,
                    post_channel,
                )
                return "updated"
            count = await conn.fetchval(
                "SELECT count(*) FROM youtube_follows WHERE guild_id = $1", guild_id
            )
            if count >= limit:
                return "limit"
            await conn.execute(
                "INSERT INTO youtube_follows "
                "(guild_id, yt_channel_id, post_channel, title, baselined, seen) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                guild_id,
                channel_id,
                post_channel,
                title,
                feed is not None,
                seen,
            )
    return "new"


async def setup(bot):
    await bot.add_cog(YouTubeAlerts(bot))
