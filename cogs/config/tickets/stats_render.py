"""The ``/ticket stats`` embed, built from :func:`stats.summarize`'s dict.

Split from stats.py so that module stays free of Discord and i18n. "No data" is
always a sentence, never a zero.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime

import discord

from . import stats
from tools.formats import random_colour
from tools.i18n import _, ngettext
from tools.time import human_timedelta


def duration(seconds):
    """``3725`` -> ``1h 2m``; under a minute reads as a sentence. Pure."""
    if seconds < 60:
        return _("under a minute")
    now = datetime.datetime.now(datetime.timezone.utc)
    return human_timedelta(
        now - datetime.timedelta(seconds=int(seconds)),
        source=now,
        accuracy=2,
        brief=True,
        suffix=False,
    )


def _timing(median, mean, empty):
    if median is None:
        return empty
    return _("median {median}, average {average}").format(
        median=duration(median), average=duration(mean)
    )


def build_embed(summary, days, *, truncated):
    embed = discord.Embed(
        title=ngettext(
            "Ticket statistics: the last day",
            "Ticket statistics: the last {days} days",
            days,
        ).format(days=days),
        colour=random_colour(),
        description=_("{opened} opened, {closed} closed.").format(
            opened=summary["opened"], closed=summary["closed"]
        ),
    )
    embed.add_field(
        name=_("Time to first claim"),
        value=_timing(
            summary["claim_median"], summary["claim_mean"], _("No claimed tickets yet.")
        ),
        inline=False,
    )
    embed.add_field(
        name=_("Time to close"),
        value=_timing(
            summary["close_median"], summary["close_mean"], _("No closed tickets yet.")
        ),
        inline=False,
    )
    if summary["rating_mean"] is None:
        satisfaction = _("No ratings yet.")
    else:
        dist = summary["distribution"]
        satisfaction = ngettext(
            "{average} out of 5 from {count} rating",
            "{average} out of 5 from {count} ratings",
            summary["rated"],
        ).format(
            average="{0:.1f}".format(summary["rating_mean"]), count=summary["rated"]
        ) + "\n" + " ".join(
            "{0}\N{WHITE MEDIUM STAR} x{1}".format(n, dist[n]) for n in range(5, 0, -1)
        )
    embed.add_field(name=_("Satisfaction"), value=satisfaction, inline=False)
    if summary["staff"]:
        embed.add_field(
            name=_("Most claims"),
            value="\n".join(
                "<@{0}>: {1}".format(user_id, count) for user_id, count in summary["staff"]
            ),
            inline=False,
        )
    if truncated:
        embed.set_footer(
            text=_("Only the most recent {n} tickets are counted.").format(
                n=stats.ROW_CAP
            )
        )
    return embed
