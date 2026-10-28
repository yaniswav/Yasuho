"""Suggestion box rendering: status labels and colours, and the posted embed.

Everything here is called under the GUILD's locale (a public message is re-rendered
in the background by many clickers, so it speaks one pinned language).

Typography rule: ASCII '-' and '...' only.
"""

import discord

from . import store
from tools.i18n import _

UP = "\N{THUMBS UP SIGN}"
DOWN = "\N{THUMBS DOWN SIGN}"

_COLOURS = {
    "open": 0x5865F2,
    "approved": 0x2ECC71,
    "rejected": 0xE74C3C,
    "implemented": 0x1ABC9C,
}


def status_label(key):
    if store.is_custom(key):
        return key[len(store.CUSTOM_PREFIX):]
    if key == "approved":
        return _("Approved")
    if key == "rejected":
        return _("Rejected")
    if key == "implemented":
        return _("Implemented")
    return _("Open")


def status_colour(key, config):
    if store.is_custom(key):
        label = key[len(store.CUSTOM_PREFIX):]
        for custom in config["custom_statuses"]:
            if custom["label"] == label:
                return custom["colour"]
        return store.DEFAULT_COLOUR
    return _COLOURS.get(key, store.DEFAULT_COLOUR)


def build_embed(row, up, down, author_name, author_icon, config):
    embed = discord.Embed(
        title="#{n} {title}".format(n=row["number"], title=row["title"])[:256],
        description=row["details"],
        colour=status_colour(row["status"], config),
        timestamp=row["created_at"],
    )
    embed.set_author(name=author_name[:256], icon_url=author_icon)
    embed.add_field(name=_("Status"), value=status_label(row["status"]))
    embed.add_field(name=_("Votes"), value="{up} {u}   {down} {d}".format(
        up=UP, u=up, down=DOWN, d=down))
    if row["status_reason"]:
        embed.add_field(name=_("Reason"), value=row["status_reason"], inline=False)
    return embed


def embed_author(message):
    """``(name, icon_url)`` of the posted embed, so a refresh keeps the author."""
    if message is not None and message.embeds and message.embeds[0].author:
        author = message.embeds[0].author
        return author.name or "?", author.icon_url
    return "?", None
