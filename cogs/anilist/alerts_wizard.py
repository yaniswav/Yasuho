"""Anime alerts quick setup: a channel, the shows you follow, one click.

Getting episode alerts used to take about five interactions PER TITLE (the
``/anilistfeed`` panel, a channel select, "Tracked releases", a search modal, a
pick) and the admin had to know the titles. This wizard is the short path: one
ephemeral Components V2 card with a channel select and the shows airing right
now (most popular first), then a single Create button.

PUBLIC ENTRY POINT: :func:`open_anime_alerts_wizard` (an interaction from a
slash command, a button or a select). Everything else is private to this module.

IT ADDS NO NEW WRITE PATH. Creation reuses the feed cog's own guarded helpers
(``_create_feed``, ``_set_enabled``, ``_add_channel_sub``), so every existing
rule keeps applying: the premium caps (feeds per guild, titles per feed), the
archived-feed freeze and the upsell refusal text. The wizard only sequences them
and reports the outcome (tracked / already tracked / skipped).

SCALE STORY. The airing list is ONE AniList request per hour for the whole
process (:data:`SEASON_TTL`), shared by every guild and held behind a lock so a
burst of wizards opened together still costs a single request. A failed fetch is
remembered for :data:`FAIL_TTL` so an outage is not retried per click. The
shared poller breaker is read (never fed) and the interactive ceiling is
honoured, like the other admin-triggered searches. Per wizard open: two DB reads
(the guild's tracked titles for the channel) and zero AniList calls on a cache
hit; Create is bounded by the 25 options.
"""

from __future__ import annotations

import asyncio
import logging
import time

import asyncpg
import discord

from . import feed_policy as af
from .breaker import breaker_for
from .feed_delivery import _FetchError, _RateLimited, _throttle_for
from .queries import AIRING_NOW_QUERY
from .replies import NO_PINGS
from tools import i18n, interactions
from tools.i18n import N_, _, ngettext
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# AniList brand blue, the accent of every feed surface.
ANILIST_BLUE = 0x02A9FF

# One request per hour for the whole process serves every wizard.
SEASON_TTL = 3600.0
# After a failed fetch, stay quiet this long before asking AniList again.
FAIL_TTL = 60.0

# A Discord select shows at most 25 options.
MAX_OPTIONS = 25

# What the feed needs in the target channel to post its cards, as
# (discord permission name, label msgid).
_REQUIRED_PERMISSIONS = (
    ("view_channel", N_("View Channel")),
    ("send_messages", N_("Send Messages")),
    ("embed_links", N_("Embed Links")),
)

_CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.news)


class SeasonUnavailable(Exception):
    """AniList cannot give us the airing list right now (down, throttled, empty)."""


class _AiringCache:
    """The airing list, fetched at most once per :data:`SEASON_TTL`.

    Times are :func:`time.monotonic` deltas (a wall-clock step must not skew the
    window); the clock is injectable for tests.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = asyncio.Lock()
        self._titles = None
        self._at = 0.0
        self._failed_at = None

    def clear(self):
        self._titles = None
        self._failed_at = None

    def _fresh(self, now):
        if self._titles is not None and now - self._at < SEASON_TTL:
            return self._titles
        return None

    async def get(self, fetch):
        """The cached list, else ``await fetch()`` once under the lock.

        Raises :class:`SeasonUnavailable` (also straight away while a recent
        failure is still being waited out).
        """

        titles = self._fresh(self._clock())
        if titles is not None:
            return titles
        async with self._lock:
            now = self._clock()
            titles = self._fresh(now)  # someone else filled it while we waited
            if titles is not None:
                return titles
            if self._failed_at is not None and now - self._failed_at < FAIL_TTL:
                raise SeasonUnavailable("recent failure")
            try:
                titles = await fetch()
            except SeasonUnavailable:
                self._failed_at = self._clock()
                raise
            self._titles, self._at, self._failed_at = titles, self._clock(), None
            return titles


_airing_cache = _AiringCache()


def _parse_titles(data):
    """``[{"id", "title", "format"}]`` from an AIRING_NOW_QUERY response."""

    page = ((data or {}).get("data") or {}).get("Page") or {}
    titles = []
    for media in af.drop_adult(page.get("media"), False):
        mid = media.get("id")
        names = media.get("title") or {}
        name = (names.get("romaji") or names.get("english") or "").strip()
        if isinstance(mid, int) and name:
            titles.append(
                {"id": mid, "title": name, "format": media.get("format") or ""}
            )
    return titles[:MAX_OPTIONS]


async def fetch_airing_titles(cog):
    """The airing anime, from the cache or ONE AniList request.

    Raises :class:`SeasonUnavailable` when AniList is tripped on the shared
    breaker, over the interactive ceiling, failing, or has nothing to list.
    """

    async def fetch():
        # Read, never fed: the breaker belongs to the pollers (breaker.py), but a
        # wizard has no reason to hit a service they already know is down.
        if breaker_for(cog.bot).is_open():
            raise SeasonUnavailable("breaker open")
        throttle = _throttle_for(cog.bot)
        if throttle is not None and not throttle.allow_global():
            raise SeasonUnavailable("interactive ceiling")
        try:
            data = await cog._graphql(AIRING_NOW_QUERY, {})
        except (_RateLimited, _FetchError) as exc:
            raise SeasonUnavailable(str(exc)) from exc
        titles = _parse_titles(data)
        if not titles:
            raise SeasonUnavailable("empty list")
        return titles

    return await _airing_cache.get(fetch)


def missing_bot_permissions(channel, me):
    """Labels of the permissions Yasuho lacks in ``channel`` (empty when fine)."""

    perms = channel.permissions_for(me)
    return [_(label) for name, label in _REQUIRED_PERMISSIONS if not getattr(perms, name)]


class AlertsResult:
    """What :func:`apply_alerts` did."""

    def __init__(self):
        self.feed_created = False
        self.feed_enabled = False
        self.added = 0
        self.already = 0
        self.skipped = 0
        # The refusal text of the first guarded helper that said no, else None.
        self.error = None


async def apply_alerts(cog, guild_id, channel_id, picks, person_id):
    """Ensure a feed on the channel and track ``picks`` ``[(media_id, title)]``.

    Only the feed cog's guarded helpers write. The first refusal (feed cap,
    archived feed, titles cap) is kept and ends the run: every later title would
    be refused for the same reason, and retrying would only spend more upsell
    slots. Already-tracked titles are counted, never rewritten.
    """

    result = AlertsResult()
    feed = await cog.bot.db_pool.fetchrow(
        "SELECT enabled FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
        guild_id,
        channel_id,
    )
    if feed is None:
        try:
            error = await cog._create_feed(guild_id, channel_id, person_id)
        except asyncpg.UniqueViolationError:
            error = None  # raced another admin: the feed exists now, which is all we need
        else:
            result.feed_created = error is None
        if error:
            result.error = error
            result.skipped = len(picks)
            return result
    elif not feed["enabled"]:
        error = await cog._set_enabled(guild_id, channel_id, True, person_id)
        if error:
            result.error = error
            result.skipped = len(picks)
            return result
        result.feed_enabled = True

    tracked = {
        row["media_id"] for row in await cog._channel_subs_for_feed(guild_id, channel_id)
    }
    for index, (media_id, title) in enumerate(picks):
        if media_id in tracked:
            result.already += 1
            continue
        error = await cog._add_channel_sub(
            guild_id, channel_id, media_id, "ANIME", title, person_id
        )
        if error:
            result.error = error
            result.skipped = len(picks) - index
            break
        result.added += 1
    return result


class _ChannelSelect(discord.ui.ChannelSelect):
    """Where the alerts are posted."""

    def __init__(self, wizard):
        self._owner = wizard
        # An Object default needs no resolved channel (discord.py accepts it for
        # a channel select); the id was validated when the wizard was built.
        channel = (
            discord.Object(id=wizard.channel_id, type=discord.TextChannel)
            if wizard.channel_id
            else None
        )
        super().__init__(
            channel_types=list(_CHANNEL_TYPES),
            placeholder=_("Pick the channel for the alerts..."),
            min_values=1,
            max_values=1,
            default_values=[channel] if channel is not None else [],
        )

    async def callback(self, interaction):
        try:
            await self._owner.set_channel(interaction, self.values[0].id)
        except Exception:
            log.exception("Anime alerts wizard channel select failed")
            await interactions.notify_failure(interaction)


class _TitleSelect(discord.ui.Select):
    """The airing shows to follow (the already tracked ones are marked)."""

    def __init__(self, wizard):
        self._owner = wizard
        options = []
        for item in wizard.titles:
            is_tracked = item["id"] in wizard.tracked
            options.append(
                discord.SelectOption(
                    label=item["title"][:100],
                    value=str(item["id"]),
                    description=(
                        _("Already tracked here") if is_tracked else item["format"].replace("_", " ")
                    )[:100]
                    or None,
                    emoji="\N{WHITE HEAVY CHECK MARK}" if is_tracked else None,
                    default=item["id"] in wizard.selected,
                )
            )
        super().__init__(
            placeholder=_("Pick the anime you follow..."),
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction):
        try:
            await self._owner.set_selected(interaction, {int(v) for v in self.values})
        except Exception:
            log.exception("Anime alerts wizard title select failed")
            await interactions.notify_failure(interaction)


class _CreateButton(discord.ui.Button):
    def __init__(self, wizard):
        self._owner = wizard
        super().__init__(
            label=_("Create alerts"),
            style=discord.ButtonStyle.success,
            disabled=not (wizard.channel_id and wizard.selected),
        )

    async def callback(self, interaction):
        try:
            await self._owner.create(interaction)
        except Exception:
            log.exception("Anime alerts wizard create failed")
            await interactions.notify_failure(interaction)


class AnimeAlertsWizard(AuthorLayoutView):
    """The ephemeral setup card: channel select, title select, Create."""

    def __init__(self, cog, guild, author_id, titles, channel_id, tracked, *, timeout=300):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.titles = list(titles)[:MAX_OPTIONS]
        self.channel_id = channel_id
        self.tracked = set(tracked)
        self.selected = set()
        self.note = None
        self.result = None
        self._build()

    def _channel_mention(self):
        return "<#{}>".format(self.channel_id)

    def _build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        if self.result is not None:
            container.add_item(discord.ui.TextDisplay(self._result_text()))
            self.add_item(container)
            return

        header = [
            "### " + _("Anime alerts"),
            _(
                "Pick a channel and the shows you follow. Yasuho posts every new "
                "episode of them there."
            ),
        ]
        if self.note:
            header.append(self.note)
        container.add_item(discord.ui.TextDisplay("\n\n".join(header)))
        container.add_item(discord.ui.ActionRow(_ChannelSelect(self)))
        container.add_item(discord.ui.ActionRow(_TitleSelect(self)))
        if self.tracked:
            container.add_item(
                discord.ui.TextDisplay(
                    "-# "
                    + ngettext(
                        "{count} title is already tracked in this channel.",
                        "{count} titles are already tracked in this channel.",
                        len(self.tracked),
                    ).format(count=len(self.tracked))
                )
            )
        container.add_item(discord.ui.ActionRow(_CreateButton(self)))
        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    def _result_text(self):
        res = self.result
        done = res.added or res.already or res.feed_created or res.feed_enabled
        lines = [
            "### "
            + (_("Anime alerts are ready") if done else _("Anime alerts were not set up"))
        ]
        if res.feed_created:
            lines.append(_("Created the AniList feed in {channel}.").format(channel=self._channel_mention()))
        elif res.feed_enabled:
            lines.append(_("Turned the AniList feed in {channel} back on.").format(channel=self._channel_mention()))
        if res.added:
            lines.append(
                ngettext(
                    "{count} title is now tracked.",
                    "{count} titles are now tracked.",
                    res.added,
                ).format(count=res.added)
            )
        if res.already:
            lines.append(
                ngettext(
                    "{count} title was already tracked.",
                    "{count} titles were already tracked.",
                    res.already,
                ).format(count=res.already)
            )
        if res.skipped:
            lines.append(
                ngettext(
                    "{count} title was skipped:",
                    "{count} titles were skipped:",
                    res.skipped,
                ).format(count=res.skipped)
                + "\n"
                + (res.error or "")
            )
        if res.added or res.already:
            lines.append(
                _("New episodes of these titles will be posted in {channel}.").format(
                    channel=self._channel_mention()
                )
            )
        lines.append(
            "-# "
            + _(
                "Want to follow AniList users too? Use `/anilistfeed follow`. "
                "Manage the tracked titles from `/anilistfeed`."
            )
        )
        return "\n\n".join(lines)

    async def _rerender(self, interaction):
        self._build()
        await interactions.refresh_layout(
            interaction, self.message, self, surface="anime alerts wizard",
            allowed_mentions=NO_PINGS,
        )

    async def set_channel(self, interaction, channel_id):
        self.channel_id = channel_id
        rows = await self.cog._channel_subs_for_feed(self.guild.id, channel_id)
        self.tracked = {row["media_id"] for row in rows}
        self.note = None
        await self._rerender(interaction)

    async def set_selected(self, interaction, selected):
        self.selected = selected
        self.note = None
        await self._rerender(interaction)

    async def create(self, interaction):
        await interactions.defer(interaction, surface="anime alerts wizard")
        # Fresh from the live interaction, not from when the card was opened.
        if not interaction.user.guild_permissions.manage_guild:
            self.note = _("You need the Manage Server permission to do that.")
            return await self._finish(interaction)
        channel = self.guild.get_channel(self.channel_id)
        if channel is None:
            self.note = _("I can't see that channel any more - pick another one.")
            return await self._finish(interaction)
        missing = missing_bot_permissions(channel, self.guild.me)
        if missing:
            self.note = _(
                "I'm missing permissions in {channel}: {permissions}. Grant them "
                "and press Create again."
            ).format(channel=channel.mention, permissions=", ".join(missing))
            return await self._finish(interaction)

        picks = [(t["id"], t["title"]) for t in self.titles if t["id"] in self.selected]
        self.result = await apply_alerts(
            self.cog, self.guild.id, channel.id, picks, interaction.user.id
        )
        self.stop()  # the result card is final
        await self._finish(interaction)

    async def _finish(self, interaction):
        """Render after a deferred click (an edit of the message the click came from)."""

        self._build()
        try:
            await interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("Anime alerts wizard: could not render the result", exc_info=True)


def _default_channel(guild, channel):
    """The invoking channel as a starting point, when a feed may live there."""

    if channel is None or getattr(channel, "type", None) not in _CHANNEL_TYPES:
        return None
    return channel.id if guild.get_channel(channel.id) is not None else None


async def build_alerts_wizard(cog, guild, member, channel):
    """The wizard view for ``member`` in ``guild``, or a refusal string.

    ``channel`` is where the command ran: it becomes the default target when a
    feed may live there. The refusals are plain text so each caller can send
    them its own way (ephemeral on an interaction, a reply on a prefix command).
    """

    if not member.guild_permissions.manage_guild:
        return _("You need the Manage Server permission to set up anime alerts.")
    try:
        titles = await fetch_airing_titles(cog)
    except SeasonUnavailable:
        return _("AniList isn't answering right now. Try again in a few minutes.")
    channel_id = _default_channel(guild, channel)
    tracked = set()
    if channel_id is not None:
        rows = await cog._channel_subs_for_feed(guild.id, channel_id)
        tracked = {row["media_id"] for row in rows}
    return AnimeAlertsWizard(cog, guild, member.id, titles, channel_id, tracked)


async def open_anime_alerts_wizard(interaction):
    """Open the anime alerts quick setup for ``interaction``'s user, ephemerally.

    The one entry point (slash subcommand, onboarding card, config panel, empty
    states). Answers by itself in every case: a refusal for a non-manager, a
    friendly line when AniList is unavailable, else the wizard card. Safe to call
    on a fresh or an already deferred interaction.
    """

    await i18n.apply_interaction_locale(interaction)
    guild = interaction.guild
    cog = interaction.client.get_cog("AniListFeed")
    if guild is None or cog is None:
        return await interactions.reply(
            interaction, _("Anime alerts are unavailable here right now.")
        )
    if not interaction.response.is_done():
        await interactions.defer(
            interaction, ephemeral=True, thinking=True, surface="anime alerts wizard"
        )
    view = await build_alerts_wizard(cog, guild, interaction.user, interaction.channel)
    if isinstance(view, str):
        return await interactions.reply(interaction, view)
    view.message = await interaction.followup.send(
        view=view, ephemeral=True, wait=True, allowed_mentions=NO_PINGS
    )
