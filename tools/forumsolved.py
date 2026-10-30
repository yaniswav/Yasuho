"""Forum "solved": `/ticket solved` closes a forum post, `/ticket forum` tunes it.

The two commands live on the existing `/ticket` group (cogs/config/tickets/panel.py),
which costs no extra top-level slot (the command tree is capped); this module holds
their logic.

`/ticket solved` works inside a forum post (a thread whose parent is a forum channel) and
is usable by the post's author or by members who can manage threads. It applies the
forum's "Solved" tag, optionally prefixes the title with "[Solved]", then archives
the post and optionally locks it.

TAG. Per forum, the configured tag; otherwise the forum's tag named "solved" or
"resolved" (case-insensitive). No such tag: the tag step is simply skipped.
Discord limits are kept: at most 5 applied tags (a full post is closed without the
tag and says so), the tag must exist, a moderated tag is applied only when the
invoker could apply it themselves, a name is cut to 100 characters.

ORDER. Discord refuses every edit but "unarchive" on an archived thread, so an
archived post is unarchived first. The optional [Solved] rename is its own edit with
a short deadline (two renames per ten minutes: a rate-limited one is skipped, never
awaited); the tag + archive (+ lock, kept if a moderator already set it) go in ONE
final edit. The reply is sent BEFORE that edit: posting into an archived
thread would re-open it.

STORAGE. One ``guild_settings`` JSONB blob (key ``forum_solved``), written by this
module only: ``{"tags": {"<forum id>": tag_id}, "prefix": bool, "lock": bool}``.
Message content is never read.

SCALE. No listener, no timer: one command = one defer, up to two thread edits.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import logging

import discord

from tools import settings
from tools.i18n import _
from tools.snowflake import coerce_id

log = logging.getLogger(__name__)

SETTINGS_KEY = "forum_solved"
SOLVED_NAMES = frozenset({"solved", "resolved"})
PREFIX = "[Solved] "
MAX_APPLIED_TAGS = 5
MAX_NAME = 100
# Discord allows two renames per ten minutes per channel and discord.py sleeps out
# the 429 inside the call: the [Solved] rename gets this long, then is skipped.
RENAME_TIMEOUT = 5


def _config_from(blob):
    blob = blob if isinstance(blob, dict) else {}
    tags = {}
    raw = blob.get("tags")
    for forum_id, tag_id in (raw.items() if isinstance(raw, dict) else ()):
        forum_id, tag_id = coerce_id(forum_id), coerce_id(tag_id)
        if forum_id and tag_id:
            tags[forum_id] = tag_id
    return {"tags": tags, "prefix": bool(blob.get("prefix")), "lock": bool(blob.get("lock"))}


def pick_tag(available_tags, configured_id):
    """The configured tag (if it still exists), else the first one named solved / resolved, else None."""

    for tag in available_tags:
        if configured_id and tag.id == configured_id:
            return tag
    for tag in available_tags:
        if tag.name.strip().casefold() in SOLVED_NAMES:
            return tag
    return None


def plan_edit(name, applied_tags, tag, prefix, lock, can_manage):
    """Build the thread edit: ``(kwargs, tag_skipped)``.

    ``tag_skipped`` is True when a tag was meant to be applied but Discord's rules
    (5 tags max, moderated tag) forbid it for this invoker.
    """

    kwargs = {"archived": True, "locked": lock}
    skipped = False
    if tag is not None and tag.id not in {t.id for t in applied_tags}:
        if len(applied_tags) >= MAX_APPLIED_TAGS or (tag.moderated and not can_manage):
            skipped = True
        else:
            kwargs["applied_tags"] = [*applied_tags, tag]
    if prefix and not name.casefold().startswith(PREFIX.strip().casefold()):
        kwargs["name"] = (PREFIX + name)[:MAX_NAME]
    return kwargs, skipped


async def get_config(bot, guild_id):
    return _config_from(await settings.get_guild(bot.db_pool, guild_id, SETTINGS_KEY, None))


async def close_post(bot, ctx):
    """Body of `/ticket solved`: tag, rename, reply, then archive (and lock)."""

    thread = ctx.channel
    forum = getattr(thread, "parent", None)
    if not isinstance(thread, discord.Thread) or not isinstance(forum, discord.ForumChannel):
        return await ctx.send(_("Use this inside a forum post."), ephemeral=True)
    can_manage = thread.permissions_for(ctx.author).manage_threads
    if not (can_manage or thread.owner_id == ctx.author.id):
        return await ctx.send(
            _("Only the post's author or someone who can manage threads can do that."),
            ephemeral=True,
        )
    if not thread.permissions_for(ctx.guild.me).manage_threads:
        return await ctx.send(
            _("I need the Manage Threads permission to close forum posts."), ephemeral=True
        )

    await ctx.defer()
    config = await get_config(bot, ctx.guild.id)
    tag = pick_tag(forum.available_tags, config["tags"].get(forum.id))
    kwargs, skipped = plan_edit(
        thread.name,
        thread.applied_tags,
        tag,
        config["prefix"],
        config["lock"] or thread.locked,  # a moderator's lock is never undone
        can_manage,
    )
    text = _("Marked as solved by {user}.").format(user=ctx.author.mention)
    if skipped:
        text += "\n" + _("The post could not take the {tag} tag, so I left the tags as they were.").format(
            tag=tag.name
        )
    # Reply first: a message in an archived thread would re-open it.
    await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
    try:
        if thread.archived:
            await thread.edit(archived=False, reason="Forum post marked solved")
        new_name = kwargs.pop("name", None)
        if new_name is not None:
            # Renamed on its own, BEFORE the archive (an archived post accepts no
            # other edit) and with a deadline: a rate-limited rename is skipped
            # silently so it can never stall the close itself.
            try:
                await asyncio.wait_for(
                    thread.edit(name=new_name, reason="Forum post marked solved"),
                    RENAME_TIMEOUT,
                )
            except (asyncio.TimeoutError, discord.HTTPException):
                log.info("Forum solved: rename skipped in guild %s", ctx.guild.id)
        await thread.edit(**kwargs, reason="Forum post marked solved by %s" % ctx.author)
    except discord.HTTPException:
        log.warning("Forum solved: edit failed in guild %s", ctx.guild.id)
        await ctx.send(_("I could not update that post."), ephemeral=True)


async def configure(bot, ctx, forum, tag, prefix, lock):
    """Body of `/ticket forum`: choose the tag per forum, the prefix and the lock."""

    await ctx.defer()
    none = discord.AllowedMentions.none()
    config = await get_config(bot, ctx.guild.id)
    if forum is not None:
        if tag:
            wanted = tag.strip().casefold()
            match = next((t for t in forum.available_tags if t.name.strip().casefold() == wanted), None)
            if match is None:
                return await ctx.send(_("That forum has no tag with that name."), allowed_mentions=none)
            config["tags"][forum.id] = match.id
        else:
            config["tags"].pop(forum.id, None)
    if prefix is not None:
        config["prefix"] = prefix
    if lock is not None:
        config["lock"] = lock
    if forum is not None or prefix is not None or lock is not None:
        await settings.set_guild(
            bot.db_pool,
            ctx.guild.id,
            SETTINGS_KEY,
            {"tags": {str(k): v for k, v in config["tags"].items()}, "prefix": config["prefix"], "lock": config["lock"]},
        )
    embed = discord.Embed(title=_("Forum solved settings"), colour=discord.Colour.green())
    embed.add_field(name=_("Title prefix"), value=_("On") if config["prefix"] else _("Off"), inline=True)
    embed.add_field(name=_("Lock when closed"), value=_("On") if config["lock"] else _("Off"), inline=True)
    lines = [f"<#{fid}>" for fid in config["tags"]]
    embed.add_field(name=_("Forums with a chosen tag"), value="\n".join(lines) or _("None"), inline=False)
    await ctx.send(embed=embed, allowed_mentions=none)
