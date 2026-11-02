"""The ``?botstats`` Summary page: a few plain-language insights.

Built ONLY from figures the other pages already read, so there is nothing new to
query here. Every input may be ``None`` (unknown or failed read) and then its
insight is skipped: a missing figure is never guessed and never turned into a
zero. Pure Python, no Discord, no pool.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

from tools import botstats_health, botstats_premium
from tools.botstats_features import FEATURES, LOW_ADOPTION
from tools.i18n import _

# Longest feature-name list quoted in an insight sentence.
NAMES_LIMIT = 4
# Fewer days of join / leave history than this is not enough to call a trend.
GROWTH_MIN_DAYS = 30


def _percent(count, total):
    return "{:.0f}%".format(min(count, total) * 100 / total)


def adoption_insight(counts, guild_total):
    if not counts or not guild_total:
        return None
    known = [(f, counts[f.key]) for f in FEATURES if f.scope == "guild" and f.key in counts]
    if not known:
        return None
    top, top_count = max(known, key=lambda pair: (pair[1], _(pair[0].label)))
    low = [f for f, count in known if count / guild_total < LOW_ADOPTION]
    label, percent = _(top.label), _percent(top_count, guild_total)
    if not low:
        return _(
            "{label} is the most used feature ({percent} of servers); "
            "no feature is under 5%."
        ).format(label=label, percent=percent)
    names = ", ".join(_(f.label) for f in sorted(low, key=lambda f: _(f.label))[:NAMES_LIMIT])
    if len(low) > NAMES_LIMIT:
        names += ", ..."
    return _(
        "{label} is the most used feature ({percent} of servers); "
        "{count} feature(s) are under 5%: {names}."
    ).format(label=label, percent=percent, count=len(low), names=names)


def upsell_insight(wall):
    if wall is None:
        return None
    rows = botstats_premium.wall_rows(wall)
    if not rows:
        return _("No free limit has anyone at 80% or more, so there is no upsell argument yet.")
    spec, row = rows[0]
    return _(
        "Best upsell argument: {label}, with {full} at the free limit and "
        "{near} at 80% or more."
    ).format(label=spec.label, full=row.get("at_100") or 0, near=row["at_80"])


def health_insights(snapshot):
    """The verdict line, then the top error pair when something failed today."""
    if snapshot is None:
        return []
    lines = [_("Health: {verdict}").format(verdict=botstats_health.render_verdict(snapshot)[0])]
    errors = snapshot.errors
    if errors is not None and errors.today and errors.top:
        command, error, total = errors.top[0]
        lines.append(
            _("Most frequent error this week: {command} / {error} ({count}).").format(
                command=command, error=error, count=total
            )
        )
    return lines


def growth_insight(growth):
    if growth is None:
        return None
    if not growth.covers(GROWTH_MIN_DAYS):
        return _("Growth: not enough history yet ({days} day(s) recorded).").format(
            days=growth.covered_days
        )
    joins, leaves = growth.windows[30]
    net = "{:+d}".format(joins - leaves)
    if not growth.stay_known:
        return _(
            "Over 30 days the bot gained {joins} and lost {leaves} server(s), net {net}."
        ).format(joins=joins, leaves=leaves, net=net)
    return _(
        "Over 30 days the bot gained {joins} and lost {leaves} server(s), net {net}; "
        "{percent} of the {known} servers that left in the last {days} day(s) "
        "stayed under 7 days."
    ).format(
        joins=joins,
        leaves=leaves,
        net=net,
        percent=_percent(growth.early[1], growth.stay_known),
        known=growth.stay_known,
        days=min(90, growth.covered_days),
    )


def usage_insights(persisted, busiest, quietest):
    if persisted is None or persisted.since is None:
        return []
    lines = []
    if persisted.top:
        name, count = persisted.top[0]
        lines.append(
            # The recorded days, when fewer than the window: a partial window
            # must not be quoted as the whole week.
            _("Most used command over {days} day(s): {name} ({count}).").format(
                days=min(persisted.week_days, persisted.covered_days),
                name=name,
                count="{:,}".format(count),
            )
        )
    if busiest and quietest:
        lines.append(
            _("Busiest hour (UTC): {busiest}; quietest: {quietest}.").format(
                busiest=busiest, quietest=quietest
            )
        )
    elif busiest:
        lines.append(_("Busiest hour (UTC): {busiest}.").format(busiest=busiest))
    return lines


def build_insights(
    *,
    adoption=None,
    guild_total=None,
    wall=None,
    health=None,
    growth=None,
    persisted=None,
    busiest=None,
    quietest=None,
):
    """3 to 7 one-sentence insights, or one "not enough data" line. Each input
    may be ``None`` (unknown) and then simply contributes nothing."""
    lines = [
        adoption_insight(adoption, guild_total),
        upsell_insight(wall),
        *health_insights(health),
        growth_insight(growth),
        *usage_insights(persisted, busiest, quietest),
    ]
    lines = [line for line in lines if line]
    return lines or [_("Not enough data yet to say anything useful.")]


def render_summary(**figures):
    return [
        (_("What stands out"), build_insights(**figures)),
        (
            _("About this page"),
            [
                "-# "
                + _(
                    "Built only from the figures of the other pages; an insight "
                    "is left out when its figure could not be read."
                )
            ],
        ),
    ]
