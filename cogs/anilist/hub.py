"""The /anilist discoverability hub: one author-restricted Components V2 panel.

The bare ``anilist`` group used to print help; it now opens this hub, a single
LayoutView that routes into the EXISTING lookup / browse / account flows. Nothing
here re-implements those flows: each button re-enters a shared seam on the cog
(``_lookup_payload`` / ``_browse_payload`` / ``_seasonal_payload`` for Discover;
``_collection_payload`` / ``_profile_payload`` / the login view for You) and posts the
result as a fresh interaction followup, so the hub message itself is only ever
edited ``view=``-only. It is gated exactly like ``AniListFeedPanel`` (locale
first, author-only, finite timeout that disables every control).
"""

import logging

import discord

from .components import LoginView
from .helpers import _current_season
from .queries import VIEWER_QUERY
from tools import i18n, interactions
from tools.i18n import N_, _
from tools.views import LocaleModal

log = logging.getLogger(__name__)

# AniList brand blue, the hub container's accent (matches the feed activity card).
HUB_ACCENT = 0x3DB4F2


class HubSearchModal(LocaleModal):
    """Search box (title + an anime/manga radio) that routes into the lookup flow.

    On submit it defers, then re-enters :meth:`AniListBase._lookup_payload` for
    the invoker - the very same ResultSelect / MediaView experience the
    ``/anilist anime|manga`` commands produce - and posts it as a public followup.
    """

    def __init__(self, cog, author_id):
        super().__init__(title=_("Search AniList"))
        self.cog = cog
        self.author_id = author_id

        self.query_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
            placeholder=_("Title to search for..."),
        )
        self.add_item(
            discord.ui.Label(text=_("Search"), component=self.query_input)
        )

        self.kind = discord.ui.RadioGroup(required=True)
        self.kind.add_option(label=_("Anime"), value="ANIME", default=True)
        self.kind.add_option(label=_("Manga"), value="MANGA")
        self.add_item(discord.ui.Label(text=_("Type"), component=self.kind))

    async def on_submit(self, interaction):
        await interactions.defer(interaction, thinking=True, surface="anilist hub search modal")
        try:
            query = (self.query_input.value or "").strip()
            media_type = self.kind.value or "ANIME"
            if not query:
                return await interaction.followup.send(_("No result."))
            kwargs, view = await self.cog._lookup_payload(
                interaction.user.id, query, media_type
            )
            message = await interaction.followup.send(**kwargs)
            if view is not None:
                view.message = message
        except Exception:
            log.exception("AniList hub search failed")
            await interactions.notify_failure(interaction)


class _HubSearchButton(discord.ui.Button):
    """Open the search modal (title + anime/manga), then route into lookup."""

    def __init__(self, hub):
        self._hub = hub
        super().__init__(
            label=_("Search"), style=discord.ButtonStyle.primary, emoji="🔍"
        )

    async def callback(self, interaction):
        try:
            await interaction.response.send_modal(
                HubSearchModal(self._hub.cog, interaction.user.id)
            )
        except Exception:
            log.exception("AniList hub search launch failed")
            await interactions.notify_failure(interaction)


class _HubBrowseButton(discord.ui.Button):
    """Route the invoker into a browse flow (trending / seasonal / popular).

    Reuses the same ``_browse_payload`` / ``_seasonal_payload`` seams the
    ``trending`` / ``popular`` / ``seasonal`` commands use, so the resulting
    ResultView / SeasonView behave identically.
    """

    # kind -> (emoji, label msgid). N_ marks the labels for extraction; _()
    # resolves them at build time under the invoker's locale.
    _CONFIG = {
        "trending": ("📈", N_("Trending")),
        "seasonal": ("🗓️", N_("Seasonal")),
        "popular": ("🔥", N_("Popular")),
    }

    def __init__(self, hub, kind):
        self._hub = hub
        self.kind = kind
        emoji, label = self._CONFIG[kind]
        super().__init__(
            label=_(label), style=discord.ButtonStyle.secondary, emoji=emoji
        )

    async def callback(self, interaction):
        try:
            await interaction.response.defer()
            cog = self._hub.cog
            if self.kind == "trending":
                kwargs, view = await cog._browse_payload(
                    interaction.user.id,
                    {"sort": ["TRENDING_DESC"], "type": "ANIME"},
                    "ANIME",
                    _("Trending anime"),
                )
            elif self.kind == "popular":
                kwargs, view = await cog._browse_payload(
                    interaction.user.id,
                    {"sort": ["POPULARITY_DESC"], "type": "ANIME"},
                    "ANIME",
                    _("Popular anime"),
                )
            else:  # seasonal - default to the current season, like the command
                season, year = _current_season()
                kwargs, view = await cog._seasonal_payload(
                    interaction.user.id, season, year
                )
            message = await interaction.followup.send(**kwargs)
            if view is not None:
                view.message = message
        except Exception:
            log.exception("AniList hub browse failed")
            await interactions.notify_failure(interaction)


class _HubListButton(discord.ui.Button):
    """Show the invoker's own list (their current anime, like ``/anilist list``)."""

    def __init__(self, hub):
        self._hub = hub
        super().__init__(
            label=_("My list"), style=discord.ButtonStyle.secondary, emoji="📋"
        )

    async def callback(self, interaction):
        try:
            await interaction.response.defer()
            error, view = await self._hub.cog._collection_payload(
                interaction.user.id, "anime", "CURRENT"
            )
            if error:
                return await interactions.reply(interaction, error)
            view.message = await interaction.followup.send(view=view)
        except Exception:
            log.exception("AniList hub list failed")
            await interactions.notify_failure(interaction)


class _HubStatsButton(discord.ui.Button):
    """Show the invoker's own AniList profile/stats (like ``/anilist profile``)."""

    def __init__(self, hub):
        self._hub = hub
        super().__init__(
            label=_("My stats"), style=discord.ButtonStyle.secondary, emoji="📊"
        )

    async def callback(self, interaction):
        try:
            await interaction.response.defer()
            error, kwargs = await self._hub.cog._profile_payload(
                interaction.user.id
            )
            if error:
                return await interactions.reply(interaction, error)
            await interaction.followup.send(**kwargs)
        except Exception:
            log.exception("AniList hub stats failed")
            await interactions.notify_failure(interaction)


class _HubLinkButton(discord.ui.Button):
    """Open the OAuth login flow for a not-yet-linked invoker (ephemeral)."""

    def __init__(self, hub):
        self._hub = hub
        super().__init__(
            label=_("Link my account"),
            style=discord.ButtonStyle.primary,
            emoji="🔗",
        )

    async def callback(self, interaction):
        try:
            cog = self._hub.cog
            if not cog._login_available():
                return await interactions.reply(
                    interaction, _("AniList account linking is not configured.")
                )
            # Same LoginView the /anilist login command uses; sent ephemerally so
            # only the invoker sees their authorize link and enters the code.
            view = LoginView(cog, interaction.user.id)
            await interaction.response.send_message(
                cog._login_instructions(), view=view, ephemeral=True
            )
            try:
                view.message = await interaction.original_response()
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("AniList hub link launch failed")
            await interactions.notify_failure(interaction)


class AniListHub(discord.ui.LayoutView):
    """The author-restricted /anilist discoverability hub (bare-group panel).

    A single Components V2 container (AniList blue) with three parts: a header
    stating the invoker's link state, a Discover row (Search / Trending /
    Seasonal / Popular) that re-enters the existing lookup / browse flows, and a
    You row (My list / My stats when linked, else a single Link button). It is
    author-gated exactly like :class:`~cogs.anilist.feed.AniListFeedPanel`:
    locale is resolved first, other users are denied with the shared "This panel
    isn't for you." wording, and a finite timeout disables every control. The
    routed flows each post a fresh followup, so the hub is only ever edited
    ``view=``-only.
    """

    def __init__(
        self, cog, author_id, *, linked, viewer_name, feed_channels, timeout=180
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.linked = linked
        self.viewer_name = viewer_name
        self.feed_channels = list(feed_channels)
        self.message = None
        self._build()

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localise.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This panel isn't for you."), ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        """Disable every button in the layout (walks the nested ActionRows)."""

        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _header_text(self):
        if self.linked:
            if self.viewer_name:
                state = _("✅ Linked as **{name}**.").format(
                    name=self.viewer_name
                )
            else:
                state = _("Your AniList account is linked.")
        else:
            state = _("Your AniList account is not linked yet.")
        return (
            "## "
            + _("AniList")
            + "\n"
            + _("Discover anime and manga, and manage your own lists.")
            + "\n"
            + state
        )

    def _footer_text(self):
        lines = []
        if self.feed_channels:
            lines.append(
                "-# "
                + _("Activity feed: {channels}").format(
                    channels=", ".join(self.feed_channels)
                )
            )
        lines.append("-# " + _("Only you can use these controls."))
        return "\n".join(lines)

    def _build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=HUB_ACCENT)

        container.add_item(discord.ui.TextDisplay(self._header_text()))
        container.add_item(discord.ui.Separator())

        # Discover: four browse entry points on one ActionRow (4 of 5 slots).
        container.add_item(discord.ui.TextDisplay("**" + _("Discover") + "**"))
        discover = discord.ui.ActionRow()
        discover.add_item(_HubSearchButton(self))
        discover.add_item(_HubBrowseButton(self, "trending"))
        discover.add_item(_HubBrowseButton(self, "seasonal"))
        discover.add_item(_HubBrowseButton(self, "popular"))
        container.add_item(discover)

        # You: linked -> list + stats; not linked -> a single primary Link button.
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("**" + _("You") + "**"))
        you = discord.ui.ActionRow()
        if self.linked:
            you.add_item(_HubListButton(self))
            you.add_item(_HubStatsButton(self))
        else:
            you.add_item(_HubLinkButton(self))
        container.add_item(you)

        container.add_item(discord.ui.TextDisplay(self._footer_text()))
        self.add_item(container)


class HubMixin:
    """Cog mixin: opens the /anilist hub from the bare-group callback."""

    async def _hub_feed_channels(self, guild):
        """Mentions of this guild's ENABLED AniList feed channels (footer pointer).

        Reads through the sibling :class:`AniListFeed` cog's ``_feeds_for_guild``;
        returns an empty list off-guild, when the feed cog is absent, or on any
        error so a hub open never fails on the footer.
        """

        if guild is None:
            return []
        feed_cog = self.bot.get_cog("AniListFeed")
        if feed_cog is None:
            return []
        try:
            feeds = await feed_cog._feeds_for_guild(guild.id)
        except Exception:
            log.exception("AniList hub: could not load feed channels")
            return []
        mentions = []
        for feed in feeds:
            if not feed["enabled"]:
                continue
            channel = guild.get_channel_or_thread(feed["channel_id"])
            mentions.append(
                channel.mention
                if channel is not None
                else "<#{cid}>".format(cid=feed["channel_id"])
            )
        return mentions

    async def _open_hub(self, ctx):
        """Open the discoverability hub (the bare ``/anilist`` entry point)."""

        status, token = await self._token_status(ctx.author.id)
        linked = status == "ok"
        viewer_name = None
        async with ctx.typing():
            if linked and token:
                viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
                viewer_name = (
                    ((viewer or {}).get("data") or {}).get("Viewer") or {}
                ).get("name")
            feed_channels = await self._hub_feed_channels(ctx.guild)
        view = AniListHub(
            self,
            ctx.author.id,
            linked=linked,
            viewer_name=viewer_name,
            feed_channels=feed_channels,
        )
        view.message = await ctx.send(view=view)
