"""Watch-along threads: ``/anilist watchalong start|stop|list`` (Yasuho+).

A server starts a watch-along for an anime it tracks; the bot keeps a thread for it
and, on each new episode of that title, posts "Episode N is out" there (pinging the
optional role under the same rule as :mod:`cogs.anilist.airing_roles`) plus a
discussion prompt that asks for spoiler tags. The post carries NO synopsis or any
text about the new episode: only the title and the episode number AniList's airing
schedule already gave the poller.

RIDES THE AIRING POLLER. There is no poller here. ``AniListAiring._tick`` adds the
watch-along titles to its tracked-media union (so the global cursor cannot pass
their airings, the C4 invariant) and calls :func:`deliver_watchalong` per aired
row. The same ONE ``airingSchedules`` query serves them: zero new AniList requests
per cycle.

THREAD LIFECYCLE. An archived thread is unarchived before posting (a locked one
needs Manage Threads, otherwise the post is skipped and logged). A thread Discord
reports as gone (404) ends the watch-along lazily: the row is deleted at that
moment, nothing sweeps. A thread we merely cannot reach (403, outage) is kept.

TIERS. Free servers have 0, Yasuho+ servers up to 5 (``GuildLimits.max_watchalongs``).
A lapse never deletes a row: the rows past the current limit (oldest ``created_at``
kept, ties by media id) stay stored but are never posted to.

SCALE. One send per active watch-along per airing of its title, plus at most one
unarchive edit and, only when the thread is not cached, one channel fetch. At 1000+
guilds that is bounded by (active watch-alongs of the aired titles), each guarded
and sequential like the existing channel fan-out; the table holds at most 5 active
rows per guild.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import typing

import discord
from discord import app_commands
from discord.ext import commands

from .account import AccountMixin
from .airing_roles import (
    NO_PINGS,
    find_tracked_title,
    ping_mentions,
    role_pingable_by_bot,
    role_pingable_by_manager,
    tracked_title_choices,
)
from tools import i18n, premium, premium_upsell
from tools.i18n import _, ngettext
from tools.premium_archive import classify

log = logging.getLogger(__name__)

THREAD_ARCHIVE_MINUTES = 10080  # 7 days: Discord's longest auto-archive
LOCK_NS = 0x414E5741  # 'ANWA': per-guild advisory lock for the watch-along count guard


# --- Pure helpers -------------------------------------------------------------


def active_watchalongs(bot, rows):
    """The rows under each guild's CURRENT ``max_watchalongs`` (lazy archival)."""

    by_guild = {}
    for row in rows:
        by_guild.setdefault(row["guild_id"], []).append(row)
    out = []
    for guild_id, guild_rows in by_guild.items():
        limit = premium.resolve_guild_limits(bot, guild_id).max_watchalongs
        resources = [
            {"id": r["media_id"], "created_at": r["created_at"]} for r in guild_rows
        ]
        keep = classify(resources, limit).active_ids
        out.extend(r for r in guild_rows if r["media_id"] in keep)
    return out


def plan_watchalong_posts(aired, watchalongs):
    """``[(watchalong_row, aired_row)]``: one post per aired row per watch-along of
    that title, in aired-row order then ``guild_id``. Rows without media or episode
    are skipped. Pure and total."""

    by_media = {}
    for wa in watchalongs:
        by_media.setdefault(wa["media_id"], []).append(wa)
    posts = []
    for row in aired:
        if row.get("media_id") is None or row.get("episode") is None:
            continue
        for wa in sorted(by_media.get(row["media_id"], ()), key=lambda w: w["guild_id"]):
            posts.append((wa, row))
    return posts


async def slot_state(db, guild_id, media_id, limit):
    """``(state, thread_id)``: ``"exists"`` (this title already has one, with its
    thread), ``"limit"`` (the guild is at ``limit``) or ``"ok"``. ``db`` is a pool
    (an advisory pre-check) or a connection (the locked re-check)."""

    row = await db.fetchrow(
        "SELECT thread_id FROM anilist_watchalongs WHERE guild_id = $1 AND media_id = $2;",
        guild_id,
        media_id,
    )
    if row is not None:
        return "exists", row["thread_id"]
    count = await db.fetchval(
        "SELECT COUNT(*) FROM anilist_watchalongs WHERE guild_id = $1;", guild_id
    )
    return ("limit" if count >= limit else "ok"), None


async def add_watchalong(pool, guild_id, media_id, channel_id, thread_id, role_id, title, limit):
    """Insert under a per-guild advisory lock (READ COMMITTED alone lets concurrent
    starts all pass the count). Returns ``("added"|"exists"|"limit", thread_id)``."""

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1, $2)", LOCK_NS, guild_id % 2147483647
            )
            state, existing = await slot_state(conn, guild_id, media_id, limit)
            if state != "ok":
                return state, existing
            await conn.execute(
                "INSERT INTO anilist_watchalongs "
                "(guild_id, media_id, channel_id, thread_id, role_id, title) "
                "VALUES ($1, $2, $3, $4, $5, $6);",
                guild_id,
                media_id,
                channel_id,
                thread_id,
                role_id,
                title,
            )
    return "added", thread_id


async def load_watchalongs(pool):
    """Every stored watch-along (archival needs the whole guild set)."""

    return await pool.fetch(
        "SELECT guild_id, media_id, channel_id, thread_id, role_id, title, created_at "
        "FROM anilist_watchalongs;"
    )


# --- Delivery (called by the airing poller) -----------------------------------


async def _end(bot, wa):
    try:
        await bot.db_pool.execute(
            "DELETE FROM anilist_watchalongs WHERE guild_id = $1 AND media_id = $2;",
            wa["guild_id"],
            wa["media_id"],
        )
    except Exception:
        log.exception("AniList watch-along: could not end %s", wa["thread_id"])


async def deliver_watchalong(bot, wa, row, pinged=None):
    """Post one episode in one watch-along thread. Never raises.

    Returns ``"sent"``, ``"ended"`` (thread gone, row deleted) or ``"skipped"``.
    ``pinged`` is the tick's set of ``(guild, title, role)`` already pinged: the role
    is pinged at most once per tick per guild and title.
    """

    try:
        thread = bot.get_channel(wa["thread_id"])
        if thread is None:
            # Archived threads are not cached: confirm with Discord before
            # concluding anything.
            try:
                thread = await bot.fetch_channel(wa["thread_id"])
            except discord.NotFound:
                await _end(bot, wa)
                return "ended"
        if not isinstance(thread, discord.Thread):
            await _end(bot, wa)
            return "ended"
        guild = thread.guild
        if guild.me is None:
            return "skipped"
        perms = thread.permissions_for(guild.me)
        if thread.archived:
            if thread.locked and not perms.manage_threads:
                log.warning(
                    "AniList watch-along: thread %s is archived and locked", thread.id
                )
                return "skipped"
            await thread.edit(archived=False)

        role = guild.get_role(wa["role_id"]) if wa["role_id"] else None
        ping = role if role_pingable_by_bot(role, perms.mention_everyone) else None
        key = (wa["guild_id"], wa["media_id"], wa["role_id"])
        if pinged is not None and key in pinged:
            ping = None
        media = row.get("media") or {}
        title = discord.utils.escape_markdown(
            discord.utils.escape_mentions(
                str(
                    (media.get("title") or {}).get("userPreferred")
                    or (media.get("title") or {}).get("romaji")
                    or (media.get("title") or {}).get("english")
                    or wa["title"]
                )
            )
        )
        loc = await i18n.resolve_guild_locale(bot, guild)
        with i18n.locale(loc):
            text = _("Episode {episode} of **{title}** is out!").format(
                episode=row["episode"], title=title
            )
            text += "\n" + _(
                "What did you think? Keep anything from later episodes or the "
                "source material behind spoiler tags (||like this||)."
            )
        if ping is not None:
            text = ping.mention + " " + text
        await thread.send(
            text, allowed_mentions=ping_mentions(ping) if ping else NO_PINGS
        )
        if ping is not None and pinged is not None:
            pinged.add(key)
        return "sent"
    except discord.NotFound:
        await _end(bot, wa)
        return "ended"
    except discord.HTTPException:
        log.warning("AniList watch-along: could not post in %s", wa["thread_id"])
    except Exception:
        log.exception("AniList watch-along: unexpected error in %s", wa["thread_id"])
    return "skipped"


# --- Commands -----------------------------------------------------------------


class WatchalongMixin:
    """``/anilist watchalong``, a subgroup of the shared ``anilist`` group."""

    @AccountMixin.anilist.group(name="watchalong")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilist_watchalong(self, ctx):
        """Follow a new anime together in a thread (Yasuho+)."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @anilist_watchalong.command(name="start")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        title="A title this server tracks in an AniList feed.",
        channel="The channel for the thread (or an existing thread).",
        role="Optional role to ping on each new episode.",
    )
    async def anilist_watchalong_start(
        self,
        ctx,
        title: str,
        channel: typing.Union[discord.TextChannel, discord.Thread],
        role: typing.Optional[discord.Role] = None,
    ):
        """Start a watch-along thread for a tracked title."""

        await ctx.defer()  # creating a thread can outlast the 3 s window
        pool = self.bot.db_pool
        limit = premium.resolve_guild_limits(self.bot, ctx.guild.id).max_watchalongs
        if limit <= 0:
            return await premium_upsell.refuse_in_context(
                self.bot,
                ctx,
                _("Watch-alongs are a Yasuho+ feature."),
                limit_key="max_watchalongs",
                kind="unlock",
            )
        found = await find_tracked_title(pool, ctx.guild.id, title)
        if found is None:
            return await ctx.send(
                _(
                    "This server does not track that anime. Track it in the feed "
                    "first (`/anilistfeed`), then try again."
                ),
                allowed_mentions=NO_PINGS,
            )
        media_id, name = found
        state, existing_thread = await slot_state(pool, ctx.guild.id, media_id, limit)
        if state == "exists":
            return await self._watchalong_exists(ctx, name, existing_thread)
        if state == "limit":
            return await self._watchalong_limit(ctx, limit)
        if role is not None and not role_pingable_by_manager(
            role,
            ctx.author.guild_permissions.mention_everyone,
            ctx.guild.me.guild_permissions.mention_everyone,
        ):
            return await ctx.send(_("You can't ping that role."), allowed_mentions=NO_PINGS)

        is_thread = isinstance(channel, discord.Thread)
        if (is_thread and channel.type is not discord.ChannelType.public_thread) or (
            not is_thread and channel.is_news()
        ):
            return await ctx.send(
                _(
                    "Pick a regular text channel or a public thread, not an "
                    "announcement channel or a private thread."
                ),
                allowed_mentions=NO_PINGS,
            )
        for member, refusal in (
            (
                ctx.author,
                _(
                    "You need View Channel, Create Public Threads and Send "
                    "Messages in Threads in {channel} first."
                ),
            ),
            (
                ctx.guild.me,
                _(
                    "I need View Channel, Create Public Threads and Send "
                    "Messages in Threads in {channel} first."
                ),
            ),
        ):
            perms = channel.permissions_for(member)
            need = perms.view_channel and perms.send_messages_in_threads
            if not is_thread:
                need = need and perms.create_public_threads
            if not need:
                return await ctx.send(
                    refusal.format(channel=channel.mention), allowed_mentions=NO_PINGS
                )

        # The count and the title were checked above, BEFORE any thread exists: a
        # refused start never creates one. The locked insert below re-checks, so a
        # race can still lose; then only the thread created here is removed.
        created = None
        if is_thread:
            thread, parent_id = channel, channel.parent_id
        else:
            thread = created = await channel.create_thread(
                name=_("Watch-along: {title}").format(title=name)[:100],
                type=discord.ChannelType.public_thread,
                auto_archive_duration=THREAD_ARCHIVE_MINUTES,
            )
            parent_id = channel.id
        try:
            state, existing_thread = await add_watchalong(
                pool,
                ctx.guild.id,
                media_id,
                parent_id,
                thread.id,
                role.id if role else None,
                name,
                limit,
            )
        except BaseException:
            await self._discard_thread(created)
            raise
        if state != "added":
            await self._discard_thread(created)
            if state == "exists":
                return await self._watchalong_exists(ctx, name, existing_thread)
            return await self._watchalong_limit(ctx, limit)
        await ctx.send(
            _(
                "Watch-along started for **{title}** in {thread}. I will post there "
                "when each new episode airs."
            ).format(title=discord.utils.escape_markdown(name), thread=thread.mention),
            allowed_mentions=NO_PINGS,
        )

    @staticmethod
    async def _discard_thread(thread):
        """Delete a thread this command created. Needs Manage Threads: when the bot
        lacks it the thread stays (logged), an empty public thread and nothing more."""

        if thread is None:
            return
        try:
            await thread.delete()
        except discord.HTTPException:
            log.warning("AniList watch-along: could not delete unused thread %s", thread.id)

    async def _watchalong_exists(self, ctx, name, thread_id):
        await ctx.send(
            _("A watch-along for **{title}** already exists: <#{thread}>.").format(
                title=discord.utils.escape_markdown(name), thread=thread_id
            ),
            allowed_mentions=NO_PINGS,
        )

    async def _watchalong_limit(self, ctx, limit):
        await premium_upsell.refuse_in_context(
            self.bot,
            ctx,
            ngettext(
                "This server already has the maximum of {n} watch-along.",
                "This server already has the maximum of {n} watch-alongs.",
                limit,
            ).format(n=limit),
            limit_key="max_watchalongs",
            benefit=str(premium.GUILD_PREMIUM.max_watchalongs),
        )

    @anilist_watchalong.command(name="stop")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(title="The title whose watch-along to stop.")
    async def anilist_watchalong_stop(self, ctx, title: str):
        """Stop a watch-along (the thread stays)."""

        text = (title or "").strip()
        status = await self.bot.db_pool.execute(
            "DELETE FROM anilist_watchalongs WHERE guild_id = $1 "
            "AND (media_id = $2 OR lower(title) = lower($3));",
            ctx.guild.id,
            int(text) if text.isdigit() and len(text) < 10 else -1,
            text,
        )
        if str(status).endswith(" 0"):
            return await ctx.send(
                _("There is no watch-along for that title."), allowed_mentions=NO_PINGS
            )
        await ctx.send(
            _("Watch-along stopped. The thread stays as it is."),
            allowed_mentions=NO_PINGS,
        )

    @anilist_watchalong_start.autocomplete("title")
    async def _watchalong_start_autocomplete(self, interaction, current):
        if interaction.guild is None:
            return []
        try:
            return await tracked_title_choices(
                self.bot.db_pool, interaction.guild.id, current
            )
        except Exception:
            log.exception("AniList watchalong autocomplete failed")
            return []

    @anilist_watchalong_stop.autocomplete("title")
    async def _watchalong_stop_autocomplete(self, interaction, current):
        if interaction.guild is None:
            return []
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT media_id, title FROM anilist_watchalongs "
                "WHERE guild_id = $1 AND position(lower($2) in lower(title)) > 0 "
                "ORDER BY created_at, media_id LIMIT 25;",
                interaction.guild.id,
                (current or "").strip(),
            )
        except Exception:
            log.exception("AniList watchalong autocomplete failed")
            return []
        return [
            app_commands.Choice(name=r["title"][:100], value=str(r["media_id"]))
            for r in rows
        ]

    @anilist_watchalong.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilist_watchalong_list(self, ctx):
        """List this server's watch-alongs."""

        rows = await self.bot.db_pool.fetch(
            "SELECT guild_id, media_id, thread_id, role_id, title, created_at "
            "FROM anilist_watchalongs WHERE guild_id = $1 ORDER BY created_at, media_id;",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send(
                _("No watch-along yet. Use `/anilist watchalong start`."),
                allowed_mentions=NO_PINGS,
            )
        keep = {r["media_id"] for r in active_watchalongs(self.bot, rows)}
        lines = []
        for row in rows:
            line = "**{title}** - <#{thread}>".format(
                title=discord.utils.escape_markdown(row["title"]), thread=row["thread_id"]
            )
            if row["role_id"]:
                line += " <@&{}>".format(row["role_id"])
            if row["media_id"] not in keep:
                line += " " + _("(archived)")
            lines.append(line)
        await ctx.send("\n".join(lines)[:2000], allowed_mentions=NO_PINGS)
