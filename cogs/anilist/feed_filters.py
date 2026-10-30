"""Per-feed activity filters: ``/anilistfeed filter`` (Yasuho+).

A feed keeps posting every activity type it is configured for; a filter narrows the
LIST activities further, by the ``status`` AniList already returned with each
activity (see :func:`cogs.anilist.feed_policy.passes_filters`). Score is not part of
a list activity, so there is no score filter: it would cost one request per activity.

Applied in ``route_activities``, i.e. at delivery and before any rendering, and never
on the fetch or the cursor, so a filter can neither skip an activity for another feed
nor cost a request. Stored in ``anilist_feeds.filters``. A server without Yasuho+ has
its stored filters ignored (everything is posted); nothing is deleted.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import discord

from . import feed_policy as af
from tools import premium, premium_upsell
from tools.i18n import N_, _

_LABELS = {
    "completed_only": N_("only completed titles"),
    "hide_planning": N_("no plans to watch or read"),
    "hide_progress": N_("no episode or chapter progress"),
    "hide_dropped": N_("no dropped or paused titles"),
}


def describe(filters):
    """Localised comma list of the active filters, or a 'none' phrase."""

    names = [_(_LABELS[f]) for f in af.FEED_FILTERS if f in filters]
    return ", ".join(names) if names else _("none (every activity is posted)")


def merge(current, changes):
    """Apply ``changes`` ({name: bool or None}) to ``current``; stored order."""

    wanted = set(current)
    for name, value in changes.items():
        if value is None:
            continue
        (wanted.add if value else wanted.discard)(name)
    return [f for f in af.FEED_FILTERS if f in wanted]


async def run_filter_command(cog, ctx, changes):
    """Body of ``/anilistfeed filter``; ``cog`` is the feed cog."""

    if not premium.resolve_guild_limits(cog.bot, ctx.guild.id).anilist_feed_filters:
        return await premium_upsell.refuse_in_context(
            cog.bot,
            ctx,
            _("Feed filters are a Yasuho+ feature."),
            limit_key="anilist_feed_filters",
            kind="unlock",
        )
    channel_id, error = await cog._resolve_target(ctx)
    if error:
        return await ctx.send(error)
    row = await cog.bot.db_pool.fetchrow(
        "SELECT filters FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
        ctx.guild.id,
        channel_id,
    )
    current = list((row["filters"] if row else None) or ())
    if any(value is not None for value in changes.values()):
        error = await cog._refuse_if_feed_archived(
            ctx.guild.id, channel_id, person_id=None
        )
        if error:
            return await ctx.send(error)
        current = merge(current, changes)
        await cog.bot.db_pool.execute(
            "UPDATE anilist_feeds SET filters = $3::text[] "
            "WHERE guild_id = $1 AND channel_id = $2;",
            ctx.guild.id,
            channel_id,
            current,
        )
    await ctx.send(
        _("Filters for <#{channel}>: {filters}").format(
            channel=channel_id, filters=describe(current)
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )
