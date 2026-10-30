"""Role pinged on a title's airing post: ``/anilist pingrole set|remove|list``.

A server attaches ONE role to a title it already tracks in an AniList feed
(``anilist_channel_subs``); when the airing poller posts that title's new episode in
a feed channel of the server, the post mentions the role - and ONLY that role.

PING RULE (mirrors ``cogs/config/event_reminders.py``). The role must not be
``@everyone`` and must be mentionable, or both the manager and the bot hold
Mention Everyone. It is checked when the mapping is set (the manager) and again at
post time (the bot, since a role can change under a stored mapping).

TIERS. Free servers keep 1 mapping, Yasuho+ servers up to 100
(``GuildLimits.max_airing_roles``). A lapse never deletes a row: the mappings past
the current limit (oldest ``created_at`` kept, ties by media id - the order of
:mod:`tools.premium_archive`) stay stored but are never pinged; the dashboard shows
them as "(archived)" with the same order.

SCALE. Zero AniList requests: a mapping only decorates a post the airing poller
already makes. The poller reads the mappings of the guilds it is about to post in
with ONE query per tick that has channel posts, and a post costs the same single
send with or without a role.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .account import AccountMixin
from tools import premium, premium_upsell
from tools.i18n import _, ngettext
from tools.premium_archive import classify

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()

LOCK_NS = 0x414E5252  # 'ANRR': per-guild advisory lock for the mapping count guard

# --- Pure helpers -------------------------------------------------------------


def role_pingable_by_manager(role, manager_can_mention_everyone, bot_can_mention_everyone):
    """The ``event_reminders.pick_role`` rule: never @everyone, mentionable or both
    sides hold Mention Everyone."""

    if role.is_default():
        return False
    return bool(
        role.mentionable or (manager_can_mention_everyone and bot_can_mention_everyone)
    )


def role_pingable_by_bot(role, can_mention_everyone):
    """Post-time check: the bot can make this role ping. ``can_mention_everyone`` is
    the bot's Mention Everyone IN THE DESTINATION (channel overwrites included)."""

    if role is None or role.is_default():
        return False
    return bool(role.mentionable or can_mention_everyone)


def ping_mentions(role):
    """``AllowedMentions`` allowing exactly ``role`` (and nothing else)."""

    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=role.id)],
        replied_user=False,
    )


def active_ids(rows, limit):
    """Media ids of one guild's rows that fit under ``limit`` (oldest kept)."""

    resources = [{"id": r["media_id"], "created_at": r["created_at"]} for r in rows]
    return classify(resources, limit).active_ids


def usable_roles(bot, rows):
    """``{(guild_id, media_id): role_id}`` for the rows under each guild's CURRENT
    ``max_airing_roles`` - the lazy archival the poller applies at post time."""

    by_guild = {}
    for row in rows:
        by_guild.setdefault(row["guild_id"], []).append(row)
    out = {}
    for guild_id, guild_rows in by_guild.items():
        limit = premium.resolve_guild_limits(bot, guild_id).max_airing_roles
        for media_id in active_ids(guild_rows, limit):
            row = next(r for r in guild_rows if r["media_id"] == media_id)
            out[(guild_id, media_id)] = row["role_id"]
    return out


async def set_role(pool, guild_id, media_id, role_id, title, limit):
    """Upsert a mapping; ``False`` when it is new and the guild is at ``limit``.

    The count guard and the write run in one transaction under a per-guild advisory
    lock: READ COMMITTED alone lets concurrent sets all pass the count. An existing
    mapping is always editable.
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1, $2)", LOCK_NS, guild_id % 2147483647
            )
            status = await conn.execute(
                "INSERT INTO anilist_airing_roles (guild_id, media_id, role_id, title) "
                "SELECT $1, $2, $3, $4 WHERE EXISTS ("
                "  SELECT 1 FROM anilist_airing_roles WHERE guild_id = $1 AND media_id = $2"
                ") OR (SELECT COUNT(*) FROM anilist_airing_roles WHERE guild_id = $1) < $5 "
                "ON CONFLICT (guild_id, media_id) DO UPDATE SET "
                "role_id = EXCLUDED.role_id, title = EXCLUDED.title;",
                guild_id,
                media_id,
                role_id,
                title,
                limit,
            )
    return str(status).endswith(" 1")


async def load_roles_for_guilds(pool, guild_ids):
    """All role rows of the given guilds (archival needs the whole guild set)."""

    if not guild_ids:
        return []
    return await pool.fetch(
        "SELECT guild_id, media_id, role_id, created_at FROM anilist_airing_roles "
        "WHERE guild_id = ANY($1::bigint[]);",
        sorted(guild_ids),
    )


# --- Tracked title lookup (shared with the watch-along commands) --------------


def _parse_media_id(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() and len(text) < 10 else -1


async def find_tracked_title(pool, guild_id, text):
    """``(media_id, title)`` of a title this server tracks as ANIME, else ``None``.

    ``text`` is a media id (what the autocomplete submits) or a title, matched
    case-insensitively and exactly. Local lookup only: no AniList request.
    """

    row = await pool.fetchrow(
        "SELECT media_id, title FROM anilist_channel_subs "
        "WHERE guild_id = $1 AND media_type = 'ANIME' "
        "AND (media_id = $2 OR lower(title) = lower($3)) "
        "ORDER BY media_id LIMIT 1;",
        guild_id,
        _parse_media_id(text),
        (text or "").strip(),
    )
    return None if row is None else (row["media_id"], row["title"])


async def tracked_title_choices(pool, guild_id, current):
    rows = await pool.fetch(
        "SELECT DISTINCT ON (media_id) media_id, title FROM anilist_channel_subs "
        "WHERE guild_id = $1 AND media_type = 'ANIME' "
        "AND position(lower($2) in lower(title)) > 0 "
        "ORDER BY media_id LIMIT 25;",
        guild_id,
        (current or "").strip(),
    )
    return [
        app_commands.Choice(name=r["title"][:100], value=str(r["media_id"]))
        for r in rows
    ]


# --- Commands -----------------------------------------------------------------


class AiringRolesMixin:
    """``/anilist pingrole``, a subgroup of the shared ``anilist`` group."""

    @AccountMixin.anilist.group(name="pingrole")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilist_pingrole(self, ctx):
        """Choose a role to ping when a tracked title's new episode airs."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @anilist_pingrole.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        title="A title this server tracks in an AniList feed.",
        role="The role to ping when a new episode airs.",
    )
    async def anilist_pingrole_set(self, ctx, title: str, role: discord.Role):
        """Ping a role on the airing post of a tracked title."""

        found = await find_tracked_title(self.bot.db_pool, ctx.guild.id, title)
        if found is None:
            return await ctx.send(
                _(
                    "This server does not track that anime. Track it in the feed "
                    "first (`/anilistfeed`), then try again."
                ),
                allowed_mentions=NO_PINGS,
            )
        media_id, name = found
        if not role_pingable_by_manager(
            role,
            ctx.author.guild_permissions.mention_everyone,
            ctx.guild.me.guild_permissions.mention_everyone,
        ):
            return await ctx.send(_("You can't ping that role."), allowed_mentions=NO_PINGS)
        limit = premium.resolve_guild_limits(self.bot, ctx.guild.id).max_airing_roles
        stored = await set_role(
            self.bot.db_pool, ctx.guild.id, media_id, role.id, name, limit
        )
        if not stored:
            return await premium_upsell.refuse_in_context(
                self.bot,
                ctx,
                ngettext(
                    "This server already has the maximum of {n} airing role.",
                    "This server already has the maximum of {n} airing roles.",
                    limit,
                ).format(n=limit),
                limit_key="max_airing_roles",
                benefit=str(premium.GUILD_PREMIUM.max_airing_roles),
            )
        await ctx.send(
            _("I will ping {role} when a new episode of **{title}** airs.").format(
                role=role.mention, title=discord.utils.escape_markdown(name)
            ),
            allowed_mentions=NO_PINGS,
        )

    @anilist_pingrole.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(title="The title whose ping role to remove.")
    async def anilist_pingrole_remove(self, ctx, title: str):
        """Stop pinging a role for a title."""

        text = (title or "").strip()
        status = await self.bot.db_pool.execute(
            "DELETE FROM anilist_airing_roles WHERE guild_id = $1 "
            "AND (media_id = $2 OR lower(title) = lower($3));",
            ctx.guild.id,
            _parse_media_id(text),
            text,
        )
        if str(status).endswith(" 0"):
            return await ctx.send(
                _("There is no ping role for that title."), allowed_mentions=NO_PINGS
            )
        await ctx.send(_("Ping role removed."), allowed_mentions=NO_PINGS)

    @anilist_pingrole_set.autocomplete("title")
    async def _pingrole_set_autocomplete(self, interaction, current):
        if interaction.guild is None:
            return []
        try:
            return await tracked_title_choices(
                self.bot.db_pool, interaction.guild.id, current
            )
        except Exception:
            log.exception("AniList pingrole autocomplete failed")
            return []

    @anilist_pingrole_remove.autocomplete("title")
    async def _pingrole_remove_autocomplete(self, interaction, current):
        if interaction.guild is None:
            return []
        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT media_id, title FROM anilist_airing_roles "
                "WHERE guild_id = $1 AND position(lower($2) in lower(title)) > 0 "
                "ORDER BY created_at, media_id LIMIT 25;",
                interaction.guild.id,
                (current or "").strip(),
            )
        except Exception:
            log.exception("AniList pingrole autocomplete failed")
            return []
        return [
            app_commands.Choice(name=r["title"][:100], value=str(r["media_id"]))
            for r in rows
        ]

    @anilist_pingrole.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilist_pingrole_list(self, ctx):
        """List the titles with a ping role."""

        rows = await self.bot.db_pool.fetch(
            "SELECT media_id, role_id, title, created_at FROM anilist_airing_roles "
            "WHERE guild_id = $1 ORDER BY created_at, media_id;",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send(
                _("No ping role is set. Use `/anilist pingrole set`."),
                allowed_mentions=NO_PINGS,
            )
        limit = premium.resolve_guild_limits(self.bot, ctx.guild.id).max_airing_roles
        keep = active_ids(rows, limit)
        lines = []
        for row in rows:
            line = "**{title}** - <@&{role}>".format(
                title=discord.utils.escape_markdown(row["title"]), role=row["role_id"]
            )
            if row["media_id"] not in keep:
                line += " " + _("(archived)")
            lines.append(line)
        await ctx.send("\n".join(lines)[:2000], allowed_mentions=NO_PINGS)
