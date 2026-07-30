"""Purpose: the Discord surface of the profile - the commands the old profiles
cog already had, re-homed onto the new socle, plus the two Components V2
surfaces this lot adds (the ``/profile view`` card and the ``/profile panel``
visibility panel), whose actual layout lives in the sibling ``views.py``
(mirrors the music.py -> views.py / seasons.py -> seasons_views.py split; see
that module's docstring for the one-way import direction).

Deliberately thin otherwise. It keeps every existing command NAME and
behaviour (``profile`` / ``view`` / ``set`` / ``edit`` / ``clear``) while
routing them through registry -> storage -> visibility, and extends ``set`` to
the socle fields the registry now knows (bio, pronouns, accent) because it is
the same command, not a new surface.

Two commands are new and do need a re-sync: ``profile visibility`` (the text
twin of the panel - without it a field written today could never be published,
every field is born private, so ``set`` would be a write into a void; ``set``
also says so in a footer when the section is still private) and ``profile
panel`` (its graphical twin, added by this lot).

Reads apply the visibility rules for real: a field the owner has not published is
not rendered for anyone else. Gamer IDs migrated from the legacy table are seeded
at 'server' by the boot fixup, so nothing that was visible yesterday goes dark
today.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import datetime
import logging
import sys
import time
from typing import Literal

import discord
from discord.ext import commands

from . import presence, registry, storage, views, visibility
from .connectors import base as connectors_base
from .connectors import sessions as connectors_sessions
from .connectors import storage as connectors_storage
from .views import (
    ProfileEditModal,
    ProfileEditView,
    ProfileVisibilityPanel,
    build_profile_card,
    format_value,
    invalid_value_message,
    section_for,
)
from .visibility import ViewerContext
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Socle fields a text command can set. custom_fields is deliberately absent: a
# list of label/value pairs belongs in the P2 panel, not in a chat argument.
TEXT_SETTABLE = ("bio", "pronouns", "accent")

# Everything `profile set` accepts, in the order the help string lists them.
SET_CHOICES = registry.GAMING_ID_KEYS + TEXT_SETTABLE

# What `profile visibility` (the text command) can publish: the sections this
# version actually stores. The graphical panel offers the connector sections
# too (see views.ProfileVisibilityPanel); offering them here, before P3/P4
# fill them, would be a toggle with nothing behind it in a plain-text answer.
VISIBILITY_CHOICES = registry.STORED_NAMES

# How stale a connector's cached payload may get before a `/profile view`
# quietly kicks off a background refresh, when the connector itself declares
# no preference. Most P4 modules DO declare their own ``REFRESH_TTL_SECONDS``
# module constant (lastfm.py's 15 minutes for a "now scrobbling" signal,
# backloggd.py's 12-hour scraping courtesy, ...) - see _connector_ttl, which
# reads that constant off whichever module actually defines the connector
# class. This is only the fallback for one that does not.
DEFAULT_CONNECTOR_REFRESH_TTL = 3600.0

# The floor between two refresh ATTEMPTS of the same (owner, connector),
# whatever the outcome. The TTL above is computed from ``last_refresh``, which
# only a SUCCESSFUL refresh stamps - so without this floor a connector whose
# remote is down (or whose account was renamed) would be re-attempted on EVERY
# `/profile view` of that member, forever: a popular profile would turn a dead
# third party into a request storm. Kept well under the shortest TTL any
# connector declares (Last.fm's 15 minutes) so it never delays a healthy
# refresh, and held in a self-pruning tools.cooldowns map rather than a plain
# dict so the memory cannot grow with the number of members ever viewed.
CONNECTOR_REFRESH_MIN_INTERVAL = 300.0

# ... and that floor DOUBLES per consecutive failure, up to the connector's own
# TTL (see _retry_interval). A flat 300s floor is the right answer for a remote
# having a bad minute and the wrong one for an account that is simply gone: a
# permanently-404 Backloggd handle on a viewed profile is 288 scrapes a day
# against a site whose declared courtesy window is 2 a day. Doubling turns a
# dead handle into a handful of attempts before it settles at that window, and
# costs a healthy connector nothing at all - one success resets the count.
CONNECTOR_FAILURE_BACKOFF_BASE = CONNECTOR_REFRESH_MIN_INTERVAL

# The exponent is capped before it is used: 2 ** 4000 is a number Python will
# happily build (and multiply by a float, and raise OverflowError on). The cap
# is far past the point where min() with the TTL has taken over anyway.
CONNECTOR_FAILURE_BACKOFF_MAX_SHIFT = 32

# Ceiling on lazy refreshes in flight at once, process-wide. The per-pair
# guard already stops one popular member from being refreshed N times over,
# but nothing stops a busy minute across MANY members (or the first views
# after a restart, when every cached payload is cold) from queueing one task
# per linked account. Past this many, the rest simply wait for a later view -
# a warm cache is best-effort by construction, and dropping the work is always
# better than holding hundreds of sockets open for a card nobody is waiting on.
MAX_CONNECTOR_REFRESHES_IN_FLIGHT = 8

# How long cog_unload waits for the refreshes it just cancelled to actually
# unwind before it closes the sessions under them. Short on purpose: an unload
# must never hang on a third party, and a request cancelled mid-flight costs
# nothing but a log line either way.
CONNECTOR_UNLOAD_TIMEOUT = 2.0


class _ConnectorFailures:
    """Consecutive-failure counts per (owner, connector), bounded in memory.

    What this is for is the account that will NEVER answer again - renamed,
    deleted, or a handle that was a typo the remote 404s forever. The flat
    CONNECTOR_REFRESH_MIN_INTERVAL floor treats that exactly like a remote
    having a bad minute, which on a profile people actually look at means
    hundreds of requests a day at a third party for data that is not coming.

    IN MEMORY on purpose, and not a column: a restart resets every counter,
    which costs at most one extra attempt per dead account and buys neither a
    schema change nor a write on every failure. The information is a hint
    about the near future, not a fact about the user, so losing it is free.

    Self-pruning past ``sweep_at`` keys, the same posture as tools.cooldowns:
    an entry older than ``horizon`` can no longer lengthen anybody's window
    (the interval is capped at the connector's TTL, and the longest TTL any
    connector declares is Backloggd's 12 hours), so it is already forgotten -
    dropping it just makes that official. Viewing a million profiles therefore
    cannot turn this into a leak.
    """

    def __init__(self, horizon, *, sweep_at=2000):
        self.horizon = horizon
        self._sweep_at = sweep_at
        self._counts = {}

    def count(self, key):
        entry = self._counts.get(key)
        return entry[0] if entry is not None else 0

    def record(self, key, *, now=None):
        """One more consecutive failure for ``key``; returns the new count."""
        now = time.monotonic() if now is None else now
        count = self.count(key) + 1
        self._counts[key] = (count, now)
        if len(self._counts) > self._sweep_at:
            cutoff = now - self.horizon
            self._counts = {
                k: v for k, v in self._counts.items() if v[1] >= cutoff
            }
        return count

    def clear(self, key):
        """A success: forget the whole history for ``key``."""
        self._counts.pop(key, None)

    def __len__(self):
        return len(self._counts)


def _connector_ttl(implementation):
    """The refresh TTL a connector module declares for itself, or the default.

    Read generically off ``sys.modules[type(implementation).__module__]``
    rather than requiring every P4 module to also expose it as a class
    attribute: lastfm.py and backloggd.py already ship a bare module-level
    ``REFRESH_TTL_SECONDS`` (this lot cannot edit either file), and this is
    the one place that turns "whichever lot adds the scheduling hook" - their
    own words for what P4A is - into an actual read of that constant.
    """
    module = sys.modules.get(type(implementation).__module__)
    return getattr(module, "REFRESH_TTL_SECONDS", DEFAULT_CONNECTOR_REFRESH_TTL)


def _payload_age(now, connection):
    """Seconds since this connection's cached payload was last filled, or None
    when there is no usable stamp (which reads as "stale").

    ``last_refresh`` is NULL until the first background refresh lands, but a
    row is not cold then: ``link`` fetched and stored a payload the moment the
    user linked, and stamped ``linked_at``. Falling back to it is what stops
    every fresh link from paying for a second, pointless round trip on the
    very next `/profile view`.

    A naive datetime cannot come out of asyncpg for a TIMESTAMPTZ column, but
    subtracting one from an aware ``now`` raises TypeError, and this runs
    inside `/profile view` - so a hand-written or hand-edited row is read as
    UTC rather than allowed to take the card down.
    """
    stamp = connection.get("last_refresh") or connection.get("linked_at")
    if not isinstance(stamp, datetime.datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return (now - stamp).total_seconds()


class Profiles(commands.Cog):
    """Your global profile: bio, pronouns, accent colour and gaming IDs."""

    def __init__(self, bot):
        self.bot = bot
        # Every real connector owns its OWN lazily-created aiohttp session
        # (see connectors/__init__.py's module docstring) rather than reaching
        # for the bot - the Connector interface carries no bot reference, by
        # construction. A connector that additionally wants opportunistic
        # access to the running bot (the AniList one, to share that cog's
        # interactive throttle) exposes its own optional ``bind_bot(bot)``
        # method; every other connector simply does not define one, and
        # `getattr(..., None)` skips it without ceremony. Done here,
        # synchronously, before `setup()` can finish adding either profile
        # cog - which is always before any command or view can reach one.
        for implementation in connectors_base.CONNECTORS.values():
            bind = getattr(implementation, "bind_bot", None)
            if bind is not None:
                try:
                    bind(bot)
                except Exception:
                    log.exception(
                        "Connector %s failed to bind the bot",
                        getattr(implementation, "name", "?"),
                    )
        # Fire-and-forget lazy refreshes kicked off by `profile view` (see
        # _schedule_stale_refreshes). Held so the loop cannot garbage-collect
        # a running task, and discarded on completion - the same pattern as
        # cogs/community/leveling.py's `_season_tasks`.
        self._connector_tasks: set[asyncio.Task] = set()
        # (owner_id, connector) pairs currently being refreshed, so a burst of
        # `/profile view` calls for the same popular member cannot spawn a
        # second in-flight fetch of the same account.
        self._connector_inflight: set[tuple[int, str]] = set()
        # When each (owner, connector) pair was last ATTEMPTED, successfully or
        # not - see CONNECTOR_REFRESH_MIN_INTERVAL. Self-pruning, so viewing a
        # million profiles cannot turn this into a leak.
        self._connector_attempts = Cooldowns(CONNECTOR_REFRESH_MIN_INTERVAL)
        # How many times in a row each pair has FAILED, which is what turns
        # that flat floor into an exponential backoff (see _retry_interval).
        # The horizon is the longest window the backoff can ever produce - the
        # largest TTL any connector declares, Backloggd's 12h scraping
        # courtesy - past which a failure record can no longer lengthen
        # anything.
        self._connector_failures = _ConnectorFailures(12 * 60 * 60)

    async def cog_unload(self):
        """Cancel every in-flight lazy refresh, then close what they used.

        Both halves matter on a reload. A fire-and-forget refresh that outlives
        its cog keeps a stale bot and a stale connector reference alive and can
        still write a payload after a reload replaced this cog; and the
        connectors' aiohttp sessions (connectors/sessions.py) belong to no bot,
        so if nothing closes them here they leak a connector pool and its open
        sockets for the life of the process - which is what aiohttp's "Unclosed
        client session" on shutdown was saying.

        Order: cancel, WAIT briefly for the cancellations to land, then close.
        Closing a session under a request that is still unwinding is how a
        clean teardown turns into a noisy one. The wait is bounded because an
        unload must never hang on a third party (same posture as the serverstats
        flush), and nothing here may raise - a failed teardown must not leave
        the extension half-unloaded.
        """
        tasks = [task for task in self._connector_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        self._connector_tasks.clear()
        try:
            if tasks:
                await asyncio.wait(tasks, timeout=CONNECTOR_UNLOAD_TIMEOUT)
        except Exception:
            log.exception("Failed to await the cancelled connector refreshes")
        try:
            await connectors_sessions.close_all()
        except Exception:
            log.exception("Failed to close the connector HTTP sessions")

    def _retry_interval(self, implementation, key):
        """How long this (owner, connector) pair must wait before the next
        ATTEMPT - the flat floor, doubled per consecutive failure.

        Zero failures is the healthy case and reads exactly as it did before:
        CONNECTOR_REFRESH_MIN_INTERVAL, well under the shortest TTL any
        connector declares, so a working connector is never delayed by this.
        Past that the window doubles until it reaches the connector's OWN TTL,
        which is the rate that connector already declared is polite for it (12
        hours of scraping courtesy for Backloggd, an hour for the JSON APIs) -
        a dead handle therefore settles at the same rate a live one is
        refreshed at, instead of at 288 attempts a day.

        The cap is never allowed BELOW the flat floor: a hypothetical connector
        declaring a two-minute TTL must not be able to shorten the guard that
        protects it.
        """
        failures = self._connector_failures.count(key)
        if failures <= 0:
            return CONNECTOR_REFRESH_MIN_INTERVAL
        ceiling = max(_connector_ttl(implementation), CONNECTOR_REFRESH_MIN_INTERVAL)
        shift = min(failures, CONNECTOR_FAILURE_BACKOFF_MAX_SHIFT)
        return min(CONNECTOR_FAILURE_BACKOFF_BASE * (2**shift), ceiling)

    # -- helpers ------------------------------------------------------------

    async def apply_field(self, user_id, field, value):
        """Validate and store one field; return ``(label, shown_value)`` or None.

        None means "no such settable field" (the caller prints the choices), and
        a None ``shown_value`` means the field was cleared. A bad value raises
        :class:`registry.InvalidValue`, which carries the cap that was exceeded,
        so the message can name it.
        """
        key = (field or "").strip().lower()
        if key in registry.GAMING_ID_KEYS:
            stored = await storage.set_gaming_id(self.bot.db_pool, user_id, key, value)
            return _(registry.GAMING_ID_LABELS[key]), format_value(key, stored)
        if key in TEXT_SETTABLE:
            stored = await storage.set_field(self.bot.db_pool, user_id, key, value)
            return _(registry.get(key).label), format_value(key, stored)
        return None

    async def visibility_note(self, user_id, section, prefix):
        """Warn the owner when what they just wrote is visible to nobody.

        Every field is born private (an ABSENT row IS private), so without this
        line `profile set` would look like a write into a void until the P2
        panel ships. Naming the command that publishes the section keeps the
        write path from being a dead end.
        """
        if section not in VISIBILITY_CHOICES:
            return None
        try:
            levels = await storage.get_visibility(self.bot.db_pool, user_id)
        except Exception:
            log.exception("Failed to read profile visibility for %s", user_id)
            return None
        if visibility.level_for(levels, section) != visibility.PRIVATE:
            return None
        return _(
            "Only you can see this for now. Use "
            "`{prefix}profile visibility {section} server` to show it to the "
            "servers you share."
        ).format(prefix=prefix, section=section)

    def _schedule_stale_refreshes(self, owner_id, connections):
        """Kick off a bounded, fire-and-forget refresh for every connection
        whose cached payload is older than its connector's own TTL (or has
        never been filled at all) - see :func:`_connector_ttl`.

        Four bounds, each closing a different way this could turn one card
        into third-party load:

        * the TTL, measured from ``last_refresh`` and falling back to
          ``linked_at`` (see :func:`_payload_age`), so a fresh link is not
          re-fetched moments later;
        * ``_connector_inflight``, so a burst of views of the same popular
          member spawns ONE fetch of their account, not one per viewer;
        * ``_connector_attempts``, so a connector that keeps FAILING (which
          never stamps ``last_refresh``) is not re-attempted on every single
          view - see CONNECTOR_REFRESH_MIN_INTERVAL, and _retry_interval for
          the doubling that takes a permanently-dead account from that floor
          up to the connector's own declared rate;
        * MAX_CONNECTOR_REFRESHES_IN_FLIGHT, the process-wide ceiling that
          covers the one case the per-pair guards cannot: many DIFFERENT
          members being viewed at once, the first minutes after a restart
          above all, when every cached payload is cold.

        Best-effort in every direction, and deliberately unable to fail the
        command: nothing here can affect the card that was just built (it
        rendered from what was on file before this runs), so a surprise in
        this bookkeeping must cost a log line, never a `/profile view`.
        """
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            for connection in connections:
                if len(self._connector_tasks) >= MAX_CONNECTOR_REFRESHES_IN_FLIGHT:
                    return
                if not isinstance(connection, dict):
                    continue
                name = connection.get("connector")
                implementation = connectors_base.CONNECTORS.get(name)
                if implementation is None:
                    continue
                age = _payload_age(now, connection)
                if age is not None and age < _connector_ttl(implementation):
                    continue
                key = (owner_id, name)
                if key in self._connector_inflight:
                    continue
                if self._connector_attempts.is_active(
                    key, seconds=self._retry_interval(implementation, key)
                ):
                    continue
                self._connector_attempts.touch(key)
                self._connector_inflight.add(key)
                task = asyncio.ensure_future(
                    self._refresh_connection(owner_id, name, implementation, connection)
                )
                self._connector_tasks.add(task)
                task.add_done_callback(self._connector_tasks.discard)
                task.add_done_callback(
                    lambda _task, key=key: self._connector_inflight.discard(key)
                )
        except Exception:
            log.exception(
                "Failed to schedule the lazy connector refreshes for %s", owner_id
            )

    async def _refresh_connection(self, owner_id, name, implementation, connection):
        """One connector's lazy refresh: fetch, then store. Never raises.

        Every exit through a FAILURE lengthens this pair's next wait
        (_retry_interval); the one exit through a successful fetch forgets the
        history. The count is bumped on the fetch, not on the store: a
        database that is unhappy says nothing about the third party this
        backoff exists to be polite to.
        """
        key = (owner_id, name)
        try:
            payload = await implementation.refresh(owner_id, connection)
        except connectors_base.ConnectorError as error:
            # An EXPECTED outcome, not a bug: the remote is down, the key was
            # never provisioned, the account was renamed. One warning line, no
            # traceback - a third party having a bad hour must not bury the
            # log under one stack trace per viewer.
            self._connector_failures.record(key)
            log.warning(
                "Connector %s could not refresh %s: %s", name, owner_id, error
            )
            return
        except Exception:
            self._connector_failures.record(key)
            log.exception(
                "Connector %s failed its lazy refresh for %s", name, owner_id
            )
            return
        self._connector_failures.clear(key)
        try:
            await connectors_storage.set_payload(self.bot.db_pool, owner_id, name, payload)
        except connectors_base.NotLinked:
            # The user unlinked while the fetch was in flight - nothing left
            # to write, and resurrecting the row would be the exact hazard
            # connectors.storage.set_payload's UPDATE-not-upsert avoids.
            pass
        except Exception:
            log.exception(
                "Failed to store the refreshed %s payload for %s", name, owner_id
            )

    # -- commands -----------------------------------------------------------

    @commands.hybrid_group(name="profile")
    @commands.guild_only()
    async def profile(self, ctx):
        """Manage your profile: view, set, edit, or clear."""

        if ctx.invoked_subcommand is None:
            await self.profile_view(ctx, ctx.author)

    @profile.command(name="view")
    @commands.guild_only()
    @discord.app_commands.describe(member="Whose profile to view (defaults to you).")
    async def profile_view(self, ctx, member: discord.Member = None):
        """View a member's profile."""

        member = member or ctx.author

        async with ctx.typing():
            pool = self.bot.db_pool
            profile = await storage.get_profile(pool, member.id)
            visibility_map = await storage.get_visibility(pool, member.id)
            # The third read of the card, and the one that makes a "Linked"
            # badge true: a section is drawn only if the owner really has a row
            # here, never because a visibility line exists. One indexed read
            # bounded to seven rows by the table's own primary key (user_id,
            # connector), so it costs the same as the two above.
            connections = await connectors_storage.get_connections(pool, member.id)
            # Best-effort cache warming, independent of who is looking or
            # what they may see: see _schedule_stale_refreshes. Never awaited
            # - it must not add a third-party round trip to this response.
            self._schedule_stale_refreshes(member.id, connections)
            # The LIVE half of the two presence sections, attached to the rows
            # that were just read: what the member is playing right now, and
            # what they are listening to on Spotify. Pure memory - it reads
            # ``member.activities`` out of the gateway cache, which this
            # command already holds - so it costs no await, no query and no
            # network, and nothing about Spotify is ever persisted (see
            # presence.py). Only sections with a marker row are touched, and
            # the card's own visibility check still decides what is drawn.
            presence.enrich_live(member, connections)
            # guild_only + a Member converter: both parties are in THIS guild, so
            # they share one. No global mutual-guild scan (that is O(guilds)).
            viewer = ViewerContext(
                owner_id=member.id,
                viewer_id=ctx.author.id,
                shares_guild=True,
            )
            card = await build_profile_card(
                member, profile, visibility_map, viewer, connections
            )

            if card is None:
                # A nickname is user-controlled too, and this branch is plain
                # message CONTENT (where "@everyone" from a nickname would
                # really ping), so it carries the same suppression as the card.
                await ctx.send(
                    _("{name} has no profile set.").format(name=member.display_name),
                    allowed_mentions=views.NO_PINGS,
                )
                return

            # The card's free-form fields (bio, custom values, gaming IDs) can
            # contain mention syntax the owner typed - unlike an embed, a
            # Components V2 card's TextDisplay text DOES get parsed for
            # mentions (see views.NO_PINGS).
            await ctx.send(view=card, allowed_mentions=views.NO_PINGS)

    @profile.command(name="set")
    @commands.guild_only()
    @discord.app_commands.describe(
        field="bio, pronouns, accent, switch, 3ds, battletag, riot, or steam_id.",
        value="The value to store; leave it out to clear the field.",
    )
    async def profile_set(self, ctx, field: str, *, value: str | None = None):
        """Set one of your profile fields (bio, pronouns, accent or a gaming ID)."""

        async with ctx.typing():
            try:
                applied = await self.apply_field(ctx.author.id, field, value)
            except registry.InvalidValue as error:
                await ctx.send(invalid_value_message(error))
                return
            except Exception:
                log.exception("Failed to set profile field %s", field)
                await ctx.send(
                    _("Failed to update your profile, please try again later.")
                )
                return

            if applied is None:
                await ctx.send(
                    _("Unknown field. Choose: {options}").format(
                        options=", ".join(SET_CHOICES)
                    )
                )
                return

            label, shown = applied
            if shown is None:
                # Omitting the value (or emptying it) is how a text command
                # clears a field: `?profile set bio` with nothing after it.
                await ctx.send(_("Cleared {field}.").format(field=label))
                return

            embed = discord.Embed(title=_("Profile updated"), colour=random_colour())
            embed.add_field(name=label, value=shown)
            note = await self.visibility_note(
                ctx.author.id, section_for(field), ctx.clean_prefix
            )
            if note:
                embed.set_footer(text=note)
            await ctx.send(embed=embed)

    # Both parameters are closed sets, so they are typed as Literals: discord.py
    # turns each one into real slash CHOICES (string values, unchanged), which
    # beats making the user guess the spelling of `custom_fields`. The literals
    # are spelled out rather than unpacked from the registry because an
    # annotation must be static; a test pins them to registry.STORED_NAMES and
    # visibility.LEVELS so the two can never drift.
    # The prefix path is unchanged in shape: the body still normalises and still
    # answers with the list of valid names, so a direct call (and the P2 panel)
    # keeps working with any casing.
    @profile.command(name="visibility")
    @commands.guild_only()
    @discord.app_commands.describe(
        section="Which part of your profile to publish.",
        level="public, server or private.",
    )
    async def profile_visibility(
        self,
        ctx,
        section: Literal["bio", "pronouns", "accent", "custom_fields", "gaming_ids"],
        level: Literal["public", "server", "private"],
    ):
        """Choose who can see one section of your profile."""

        async with ctx.typing():
            key = (section or "").strip().lower()
            if key not in VISIBILITY_CHOICES:
                await ctx.send(
                    _("Unknown section. Choose: {options}").format(
                        options=", ".join(VISIBILITY_CHOICES)
                    )
                )
                return
            try:
                chosen = visibility.normalise_level(level)
            except visibility.InvalidLevel:
                await ctx.send(
                    _("Unknown visibility. Choose: {options}").format(
                        options=", ".join(visibility.LEVELS)
                    )
                )
                return
            try:
                await storage.set_visibility(
                    self.bot.db_pool, ctx.author.id, key, chosen
                )
            except Exception:
                log.exception("Failed to set profile visibility for %s", key)
                await ctx.send(
                    _("Failed to update your profile, please try again later.")
                )
                return

            label = _(registry.get(key).label)
            if chosen == visibility.PUBLIC:
                await ctx.send(
                    _("{field} is now visible to anyone.").format(field=label)
                )
            elif chosen == visibility.SERVER:
                await ctx.send(
                    _("{field} is now visible to the servers you share.").format(
                        field=label
                    )
                )
            else:
                await ctx.send(
                    _("{field} is now visible to you only.").format(field=label)
                )

    # Declared HERE and implemented in presence.py. A hybrid subcommand must
    # live in the same cog as the group that owns it (the house lesson from the
    # /levelconfig fold), and `profile` belongs to this cog - but the presence
    # collector owns a hot gateway listener and a flush loop that have no
    # business in the command surface, so it is a cog of its own and this body
    # delegates to it through get_cog, exactly like that fold does.
    #
    # Both parameters are optional: `?profile presence` with neither reports
    # what is on and what is off, which is the answer to "wait, is the bot
    # recording my games" - a question an opt-in feature has to be able to
    # answer without changing anything.
    @profile.command(name="presence")
    @commands.guild_only()
    @discord.app_commands.describe(
        gaming="on to let your profile show the games you play, off to stop and forget them.",
        spotify="on to let your profile show what you are listening to, off to stop.",
    )
    async def profile_presence(
        self,
        ctx,
        gaming: Literal["on", "off"] | None = None,
        spotify: Literal["on", "off"] | None = None,
    ):
        """Choose whether your profile shares your Discord presence."""

        cog = self.bot.get_cog("ProfilePresence")
        if cog is None:
            # The extension adds all three cogs together, so this only happens
            # if presence.py failed to load - which is a log line for an
            # operator, not a traceback for the member who asked.
            log.error("The ProfilePresence cog is not loaded")
            await ctx.send(
                _("Failed to update your profile, please try again later."),
                ephemeral=True,
            )
            return
        await cog.cmd_presence(ctx, gaming=gaming, spotify=spotify)

    @profile.command(name="edit")
    @commands.guild_only()
    async def profile_edit(self, ctx):
        """Edit a gaming ID through a guided form (radio picker + value)."""

        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(ProfileEditModal(self))
        else:
            view = ProfileEditView(self, ctx.author.id)
            view.message = await ctx.send(
                _("Click below to edit a profile field:"), view=view
            )

    @profile.command(name="panel")
    @commands.guild_only()
    async def profile_panel(self, ctx):
        """Open a graphical panel to manage your profile's visibility."""

        # Same discipline as every sibling here: `ctx.typing()` defers the slash
        # interaction, so the database round-trip cannot eat the 3-second
        # response window and lose the panel entirely.
        async with ctx.typing():
            visibility_map = await storage.get_visibility(
                self.bot.db_pool, ctx.author.id
            )
            view = ProfileVisibilityPanel(self, ctx.author.id, visibility_map)
            view.message = await ctx.send(view=view, allowed_mentions=views.NO_PINGS)

    @profile.command(name="clear")
    @commands.guild_only()
    async def profile_clear(self, ctx):
        """Clear your entire profile, including who could see what."""

        async with ctx.typing():
            try:
                await storage.delete_profile(self.bot.db_pool, ctx.author.id)
            except Exception:
                log.exception("Failed to clear profile")
                await ctx.send(
                    _("Failed to clear your profile, please try again later.")
                )
                return
            # The rows are gone; the presence collector still holds this user in
            # memory (it is armed by an in-process set, not by a query). Told
            # here rather than left to the next flush so the very next event of
            # theirs is already rejected - see presence.forget_collected_presence
            # for why this is best-effort and what backs it up.
            presence.forget_collected_presence(self.bot, ctx.author.id)

            embed = discord.Embed(title=_("Profile cleared"), colour=random_colour())
            embed.add_field(name=_("Your profile has been cleared."), value="​")
            await ctx.send(embed=embed)
