"""Yasuho+ numbers for the ``?botstats`` Premium page (cogs/system/botstats.py).

Three questions, none of them recomputed here: how many servers have Yasuho+
(the in-memory ``bot.premium`` cache, the same resolver every gate uses), where
servers press against the FREE ceiling (tools/premium_usage.py's own fleet
aggregates, reused as-is) and how many people were shown a limit note recently
(``premium_upsells``, the table tools/premium_upsell.py throttles with).

Honest stats: an unreadable figure is unknown, never zero. Pure Python plus one
SQL constant; no Discord and no pool reference beyond :func:`fetch_upsell_counts`.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

from tools import premium_usage
from tools.i18n import _
from tools.premium_upsell import ALL_KEYS_SENTINEL
from tools.retention import PREMIUM_UPSELL_MAX_AGE_DAYS

# How many limits each list shows before saying how many were left out.
WALL_LIMIT = 8
UPSELL_LIMIT = 10

# The table keeps ONE row per (person, limit): the latest time that note was
# shown. Rows are pruned after PREMIUM_UPSELL_MAX_AGE_DAYS, so this counts
# PEOPLE who saw each note in that window, not how many times it was shown.
UPSELL_COUNTS = """
    SELECT limit_key, COUNT(*)::bigint AS people
    FROM premium_upsells
    WHERE limit_key <> $1 AND shown_at >= now() - $2 * INTERVAL '1 day'
    GROUP BY limit_key
    ORDER BY people DESC, limit_key
    LIMIT $3::int
    """


async def fetch_upsell_counts(pool, *, timeout, limit=UPSELL_LIMIT):
    rows = await pool.fetch(
        UPSELL_COUNTS,
        ALL_KEYS_SENTINEL,
        PREMIUM_UPSELL_MAX_AGE_DAYS,
        limit + 1,
        timeout=timeout,
    )
    return [(row["limit_key"], row["people"]) for row in rows]


def count_premium_guilds(cache, guilds):
    """Guilds with Yasuho+ right now, or ``None`` when there is no cache."""
    if cache is None:
        return None
    return sum(1 for guild in guilds if cache.is_guild_premium(guild.id))


def wall_rows(rows_by_key, *, specs=None):
    """``[(spec, row)]`` for the limits where somebody is at 80% or more of the
    FREE value, the most-pressed first. A limit with no numeric FREE value
    (a capability gate: nothing to be 80% of) is left out by design."""
    specs = premium_usage.RESOURCE_SPECS if specs is None else specs
    picked = []
    for spec in specs:
        row = rows_by_key.get(spec.key)
        free = premium_usage.free_value(spec)
        if not row or not free or isinstance(free, bool):
            continue
        if (row.get("at_80") or 0) > 0:
            picked.append((spec, row))
    picked.sort(
        key=lambda pair: (-(pair[1].get("at_100") or 0), -(pair[1]["at_80"]), pair[0].key)
    )
    return picked


def render_premium(plus_guilds, guild_total, wall, upsells, *, limit=WALL_LIMIT):
    """The Premium page: ``[(heading, lines)]`` from plain data only.

    ``wall`` is ``premium_usage.fetch_all``'s dict and ``upsells`` the
    ``[(limit_key, people)]`` list; either is ``None`` when its read failed.
    """
    if plus_guilds is None:
        plus = [_("Yasuho+ servers are unknown right now.")]
    else:
        percent = (
            "{:.1f}%".format(plus_guilds * 100 / guild_total) if guild_total else "-"
        )
        plus = [
            _("{count} of {total} servers have Yasuho+ ({percent})").format(
                count=plus_guilds, total=guild_total, percent=percent
            )
        ]

    if wall is None:
        pressed = [_("Limit usage is unavailable right now.")]
    else:
        rows = wall_rows(wall)
        if not rows:
            pressed = [_("No free limit has anyone at 80% or more.")]
        else:
            pressed = [
                _(
                    "{label}: {full} at the free limit ({free}), "
                    "{near} at 80% or more, of {scopes}"
                ).format(
                    label=spec.label,
                    full=row.get("at_100") or 0,
                    free=premium_usage.free_value(spec),
                    near=row["at_80"],
                    scopes=row.get("scopes") or 0,
                )
                for spec, row in rows[:limit]
            ]
            if len(rows) > limit:
                pressed.append(_("... and {count} more").format(count=len(rows) - limit))
        pressed.append(
            "-# "
            + _(
                "Measured against the FREE limit across every server and person, "
                "Yasuho+ ones included."
            )
        )

    if upsells is None:
        notes = [_("Limit notes are unavailable right now.")]
    elif not upsells:
        notes = [
            _("No limit note was shown in the last {days} days.").format(
                days=PREMIUM_UPSELL_MAX_AGE_DAYS
            )
        ]
    else:
        notes = [
            _("{key} - {people} people").format(key=key, people=people)
            for key, people in upsells[:UPSELL_LIMIT]
        ]
        if len(upsells) > UPSELL_LIMIT:
            notes.append(_("... and more"))
        notes.append(
            "-# "
            + _(
                "People who saw each note in the last {days} days "
                "(older records are deleted)."
            ).format(days=PREMIUM_UPSELL_MAX_AGE_DAYS)
        )

    return [
        (_("Yasuho+"), plus),
        (_("Where free limits are hit"), pressed),
        (_("Limit notes shown"), notes),
    ]
