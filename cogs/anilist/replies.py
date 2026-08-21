"""One rule for every reply this package sends: it can never ping.

THE HAZARD. Almost everything the AniList cogs say back carries text somebody
else wrote - a media title fetched from AniList, the search term the member
typed, an AniList username - quoted into the message CONTENT ("Found {count}
results for **{search}** - pick one:", "No result for **{title}**."). The bot's
client default is NOT silent about that: ``core.Yasuho`` builds itself with
``AllowedMentions(roles=False, everyone=False, users=True)``, so ``@everyone``
and role pings are already dead but a raw ``<@id>`` in that echoed text still
resolves and notifies its target. Anyone who can type a slash command (or
retitle a media entry upstream) can therefore make the bot ping people, in a
channel of their choosing, under the bot's name.

WHY A COG MIXIN AND NOT ~50 EDITED CALL SITES. There is no single "reply seam"
in this package to fix once: the four cogs (``AniList`` and the three pollers)
send through about fifty separate ``ctx.send`` calls, and the next feature adds
the fifty-first. What IS shared is the invocation: discord.py runs
``Cog.cog_before_invoke`` before every command body, prefix and hybrid-app alike
(``HybridAppCommand._invoke_with_namespace`` goes through ``command.prepare``,
which calls the same hook), and the ``Context`` it hands the body is a fresh
object per invocation. Binding a mention-free default onto that object's
``send`` is therefore ONE rule that covers every current and future reply,
without a call site being able to forget it.

``functools.partial`` and not a wrapper function on purpose: a call that passes
its own ``allowed_mentions`` still wins (partial merges the call's keywords over
its own), so the rare reply that MEANS to notify someone keeps saying so.

WHAT THIS DOES NOT COVER. Component callbacks never run through a command
invocation, so a view/button/modal that posts through ``interaction.followup``
is outside this hook. The ones that re-post a shared command payload use
:func:`no_ping` on the way out (see hub.py); the rest are author-scoped
ephemerals.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import functools

import discord

# Nothing this package echoes is allowed to notify anyone.
NO_PINGS = discord.AllowedMentions.none()


def no_ping(kwargs):
    """Apply the package rule to a ``send`` kwargs dict, in place.

    For the payload builders' ``(kwargs, view)`` dicts when they are posted
    through an INTERACTION rather than a ``Context`` - the one path
    :class:`NoPingReplies` cannot reach. ``setdefault`` so a caller that already
    said what it wants keeps saying it, and so applying it twice is harmless.
    """

    kwargs.setdefault("allowed_mentions", NO_PINGS)
    return kwargs


class NoPingReplies:
    """Cog mixin: every ``ctx.send`` of this cog defaults to no mentions at all.

    Mixed into all four cogs of the package (see ``cogs/anilist/__init__.py``,
    feed.py, airing.py, chapters.py). A cog that needs its own
    ``cog_before_invoke`` must call this one via ``super()``, or it silently
    takes the rule away from every command it owns.
    """

    async def cog_before_invoke(self, ctx):
        ctx.send = functools.partial(ctx.send, allowed_mentions=NO_PINGS)
