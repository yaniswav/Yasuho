"""Music request panels: a persistent "Request a song" button, no message reading.

A manager posts a panel in a channel with ``/music requests post``. Its button
opens a one-field modal; submitting runs exactly what ``/play`` does for that
member (same SSRF guard, same voice requirement, same queue cap, same fair-queue
rules), through ``Music._play_query``. Nothing here ever reads message text: the
bot only reacts to a button press and a modal submit.

Persistence. The button is a :class:`discord.ui.DynamicItem` with a constant
custom id, registered process-wide at cog load, so every posted panel keeps
working across restarts with no stored view. The panel LOCATIONS
(``channel_id`` + ``message_id``) live in the guild settings blob under
``guild_config.KEY_REQUEST_PANELS``.

Limits and lapse. A free server has one panel, Yasuho+ up to
``GuildLimits.max_request_panels``. Panels are ordered by creation. Whatever goes
beyond the CURRENT limit (a lapsed subscription) stays posted and visible but is
read-only: its button answers that the panel is paused, and the first panels keep
working. Nothing is deleted on a lapse.

Cleanup is lazy: a deleted message or channel is pruned from the stored list the
next time a manager posts or removes a panel. A press on a panel that is not in
the list (removed, or its record was lost) is answered "no longer active".
"""

from __future__ import annotations

import asyncio
import logging
import typing

import discord

from cogs.music import guild_config
from tools import interactions, premium_upsell, settings
from tools.cooldowns import Cooldowns
from tools.i18n import _, ngettext
from tools.premium import GUILD_PREMIUM, resolve_guild_limits
from tools.views import LocaleDynamicItem, LocaleModal

log = logging.getLogger(__name__)

TEMPLATE = r"music:request"

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_MISSING = "missing"

# One request every few seconds per member: the modal is cheap to reopen, the
# search behind it is not.
_REQUEST_DEBOUNCE = Cooldowns(5.0)
# Opening the modal is debounced too, on its own window (the submit must not be
# blocked by the press that opened it).
_PRESS_DEBOUNCE = Cooldowns(2.0)

# A dismissed modal sends Discord nothing, so it must expire on its own.
MODAL_TIMEOUT = 300

QUERY_MAX_LENGTH = 400


def panel_status(
    panels: typing.Sequence[typing.Tuple[int, int]],
    message_id: int,
    limit: int,
    channel_exists: typing.Optional[typing.Callable[[int], bool]] = None,
) -> str:
    """Whether the panel on ``message_id`` works, is paused, or is unknown. Pure.

    ``panels`` is the stored list in creation order; the first ``limit`` entries
    are active, the rest are the read-only overflow of a lapsed plan.
    ``channel_exists`` (a cache lookup, no network) lets entries whose channel is
    gone stop taking a slot, so a deleted first panel does not keep the next one
    paused.
    """
    position = 0
    for channel_id, mid in panels:
        if mid != message_id:
            if channel_exists is not None and not channel_exists(channel_id):
                continue
            position += 1
            continue
        return STATUS_ACTIVE if position < limit else STATUS_PAUSED
    return STATUS_MISSING


def panel_content() -> str:
    return _(
        "**Song requests**\nPress the button, type a song name or a link, and I "
        "will add it to the queue. You need to be in a voice channel."
    )


class RequestButton(LocaleDynamicItem[discord.ui.Button], template=TEMPLATE):
    """The persistent "Request a song" button of a panel."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=_("Request a song"),
                emoji="\N{MUSICAL NOTE}",
                custom_id="music:request",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Music")
        if cog is None:
            await interaction.response.send_message(
                _("Music is unavailable right now."), ephemeral=True
            )
            return
        await cog.open_request_modal(interaction)


class RequestModal(LocaleModal):
    """One text field; the submit is a /play for the member who pressed."""

    def __init__(self, cog: typing.Any) -> None:
        super().__init__(title=_("Request a song"), timeout=MODAL_TIMEOUT)
        self.cog = cog
        self.query_field = discord.ui.TextInput(
            label=_("Song or URL"),
            placeholder=_("A song name to search, or a full URL"),
            style=discord.TextStyle.short,
            required=True,
            max_length=QUERY_MAX_LENGTH,
        )
        self.add_item(self.query_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.cog.submit_request(interaction, self.query_field.value)
        except Exception:
            log.exception("Music request modal submit failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong with that request.")
            )


def panel_view() -> discord.ui.View:
    """The view carried by a freshly posted panel (the item is dynamic)."""
    view = discord.ui.View(timeout=None)
    view.add_item(RequestButton())
    return view


class RequestPanelMixin:
    """The panel logic of the Music cog (commands are declared on the cog)."""

    def _panel_lock(self, guild_id: int) -> asyncio.Lock:
        locks = self.__dict__.setdefault("_panel_locks", {})
        return locks.setdefault(guild_id, asyncio.Lock())

    async def _panel_limit_upsell(self, guild: discord.Guild, actor: typing.Any):
        upsell = premium_upsell
        return await upsell.for_guild_refusal(
            self.bot,
            limit_key="max_request_panels",
            guild_id=guild.id,
            person_id=actor.id,
            is_admin=upsell.invoker_is_admin(actor),
            already_top_tier=upsell.is_guild_already_top_tier(self.bot, guild.id),
            benefit=str(GUILD_PREMIUM.max_request_panels),
        )

    async def _prune_panels(
        self, guild: discord.Guild, panels: typing.List[typing.Tuple[int, int]]
    ) -> typing.List[typing.Tuple[int, int]]:
        """Drop entries whose channel or message is gone (best-effort, bounded)."""
        kept = []
        for channel_id, message_id in panels:
            channel = guild.get_channel_or_thread(channel_id)
            if channel is None:
                continue
            try:
                await channel.fetch_message(message_id)
            except discord.NotFound:
                continue
            except discord.HTTPException:
                pass  # Forbidden / 5xx: unknown, so keep it
            kept.append((channel_id, message_id))
        return kept

    async def _read_panels_strict(self, guild_id: int):
        """The stored panels, RAISING on a settings failure.

        ``guild_config.request_panels`` swallows errors into "no panels", which
        is fine for a press but would let ``post`` overwrite a good list.
        """
        raw = await settings.get_guild(
            self._settings_pool(), guild_id, guild_config.KEY_REQUEST_PANELS
        )
        return guild_config.coerce_panels(raw)

    async def _read_or_fail(self, ctx, guild_id: int):
        try:
            return await self._read_panels_strict(guild_id)
        except Exception:
            log.exception("Request panel list read failed for guild %s", guild_id)
            await ctx.send(
                _("Something went wrong with that request."), ephemeral=True
            )
            return None

    async def _save_panels(self, guild_id: int, panels) -> None:
        await settings.set_guild(
            self.bot.db_pool,
            guild_id,
            guild_config.KEY_REQUEST_PANELS,
            [{"c": c, "m": m} for c, m in panels],
        )

    async def requests_post(self, ctx, channel: typing.Optional[discord.TextChannel]):
        """Post a request panel (Manage Server is checked by the command)."""
        guild = ctx.guild
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(
                _("Pick a text channel for the request panel."), ephemeral=True
            )
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            await ctx.send(
                _("I cannot send messages in {channel}.").format(
                    channel=channel.mention
                ),
                ephemeral=True,
            )
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        async with self._panel_lock(guild.id):
            panels = await self._read_or_fail(ctx, guild.id)
            if panels is None:
                return
            pruned = await self._prune_panels(guild, panels)
            if pruned != panels:
                await self._save_panels(guild.id, pruned)
            panels = pruned
            if any(c == channel.id for c, _m in panels):
                await ctx.send(
                    _("{channel} already has a request panel.").format(
                        channel=channel.mention
                    ),
                    ephemeral=True,
                )
                return
            limit = resolve_guild_limits(self.bot, guild.id).max_request_panels
            if len(panels) >= limit:
                text = ngettext(
                    "This server already has {count} request panel, the most "
                    "its plan allows. Remove one with `/music requests remove`.",
                    "This server already has {count} request panels, the most "
                    "its plan allows. Remove one with `/music requests remove`.",
                    len(panels),
                ).format(count=len(panels))
                upsell = await self._panel_limit_upsell(guild, ctx.author)
                view = None
                if upsell is not None:
                    text += "\n" + upsell.line
                    view = upsell.view()
                await ctx.send(text, ephemeral=True, view=view)
                return
            message = await channel.send(
                panel_content(),
                view=panel_view(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                await self._save_panels(guild.id, panels + [(channel.id, message.id)])
            except Exception:
                # No record means no working panel: do not leave a dead one up.
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                raise
        await ctx.send(
            _("Request panel posted in {channel}.").format(channel=channel.mention),
            ephemeral=True,
        )

    async def requests_remove(self, ctx, channel: typing.Optional[discord.TextChannel]):
        """Remove the request panel(s) of a channel (default: this channel)."""
        guild = ctx.guild
        channel = channel or ctx.channel
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        async with self._panel_lock(guild.id):
            panels = await self._read_or_fail(ctx, guild.id)
            if panels is None:
                return
            gone = [p for p in panels if p[0] == getattr(channel, "id", None)]
            if not gone:
                await ctx.send(
                    _("There is no request panel in {channel}.").format(
                        channel=getattr(channel, "mention", "#?")
                    ),
                    ephemeral=True,
                )
                return
            await self._save_panels(guild.id, [p for p in panels if p not in gone])
        for channel_id, message_id in gone:
            target = guild.get_channel_or_thread(channel_id)
            if target is None:
                continue
            try:
                await target.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass
        await ctx.send(
            _("Request panel removed from {channel}.").format(
                channel=getattr(channel, "mention", "#?")
            ),
            ephemeral=True,
        )

    async def open_request_modal(self, interaction: discord.Interaction) -> None:
        """The button press: check the panel is live, then open the modal."""
        guild = interaction.guild
        message = interaction.message
        if guild is None or message is None:
            await interaction.response.send_message(
                _("This panel is no longer active."), ephemeral=True
            )
            return
        panels = await guild_config.request_panels(self._settings_pool(), guild.id)
        limit = resolve_guild_limits(self.bot, guild.id).max_request_panels
        status = panel_status(
            panels,
            message.id,
            limit,
            lambda cid: guild.get_channel_or_thread(cid) is not None,
        )
        if status == STATUS_MISSING:
            await interaction.response.send_message(
                _("This panel is no longer active."), ephemeral=True
            )
            return
        if status == STATUS_PAUSED:
            # Answer first, for everyone; the upsell (a DB write + LIMIT-REACHED
            # log) is only for a manager, who is the person who can act on it.
            await interaction.response.send_message(
                _(
                    "This request panel is paused: it is beyond what this "
                    "server's plan includes (Yasuho+ allows more panels)."
                ),
                ephemeral=True,
            )
            if premium_upsell.invoker_is_admin(interaction.user):
                upsell = await self._panel_limit_upsell(guild, interaction.user)
                if upsell is not None:
                    await interaction.followup.send(
                        upsell.line, view=upsell.view(), ephemeral=True
                    )
            return
        if _PRESS_DEBOUNCE.is_active(interaction.user.id):
            await interaction.response.send_message(
                _("You are requesting too fast - give it a moment."), ephemeral=True
            )
            return
        _PRESS_DEBOUNCE.touch(interaction.user.id)
        await interaction.response.send_modal(RequestModal(self))

    async def submit_request(self, interaction: discord.Interaction, raw: str) -> None:
        """The modal submit: defer ephemeral, then run the /play core."""
        query = (raw or "").strip()
        if not query:
            await interaction.response.send_message(
                _("Give me a song name or URL to add."), ephemeral=True
            )
            return
        if _REQUEST_DEBOUNCE.is_active(interaction.user.id):
            await interaction.response.send_message(
                _("You are requesting too fast - give it a moment."), ephemeral=True
            )
            return
        _REQUEST_DEBOUNCE.touch(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        from cogs.music.views import _ModalPlayContext

        ctx = _ModalPlayContext(interaction)
        # A panel can live in any channel: a live session keeps its own home
        # channel (the /play home check then passes, and nothing is moved).
        home = getattr(ctx.voice_client, "home", None)
        if home is not None:
            ctx.channel = home
        await self._play_query(ctx, query)
