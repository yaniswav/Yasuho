import asyncio
import io
import logging
import math

import discord
from discord.ext import commands
from PIL import Image

from tools import privacy, rendering, retention, settings
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorView

log = logging.getLogger(__name__)
TRACKING_PREF_KEY = privacy.AVATAR_TRACKING_KEY
HISTORY_LIMIT = retention.AVATAR_MAX_PER_SERIES
STORAGE_MAX_SIZE = 192
STORAGE_WEBP_QUALITY = 76
COMPRESSION_BATCH_SIZE = 25

# ONE LENGTH for the ONE expensive thing this cog does - fetch a history and
# paint a collage through the bot-wide Pillow semaphore (tools.rendering, 2
# slots for the whole fleet). There are TWO doors into that work and they are
# throttled by two DIFFERENT mechanisms (a discord.py command cooldown keyed on
# the invoking message, an in-memory debounce keyed on the user id), so what is
# shared here is the number, not the window: the two windows run independently,
# and a member who both types the command and clicks a button can get 2 renders
# per 10s rather than 1. That is the bound this cog wants - the leak it closes
# is the button that had NO throttle at all, and one number in one place is how
# the two doors stay comparable.
HISTORY_COOLDOWN_SECONDS = 10

# The buttons' half of it. A per-user in-memory debounce (tools.cooldowns.
# Cooldowns, the same shape cogs/music/views.py's station select and the AniList
# feed's action buttons use) rather than the command's discord.py cooldown,
# because that one is keyed on a Message whose author is the invoker and a
# component interaction has no such message - its message is the BOT's. Bounded
# and self-pruning, so a raid on the buttons cannot grow it without limit.
_RENDER_DEBOUNCE = Cooldowns(HISTORY_COOLDOWN_SECONDS)

# Human-readable titles and nouns per tracked image kind. Marked with N_ so
# pybabel extracts them; each is translated at the use site via _(...).
KIND_TITLES = {
    "global": N_("Global avatar history"),
    "guild": N_("Server avatar history"),
    "banner": N_("Banner history"),
}
KIND_NOUNS = {
    "global": N_("global"),
    "guild": N_("server"),
    "banner": N_("banner"),
}


class AvatarHistoryView(AuthorView):
    """Lets the requester switch between global / server / banner history."""

    def __init__(self, cog, ctx, member, *, timeout=180):
        super().__init__(ctx.author.id, timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.member = member
        self.guild = ctx.guild
        # Per-guild avatars only make sense inside a guild.
        if self.guild is None:
            self.server_button.disabled = True
        self._set_active("global")

    def _set_active(self, kind):
        self.global_button.style = (
            discord.ButtonStyle.success
            if kind == "global"
            else discord.ButtonStyle.secondary
        )
        self.server_button.style = (
            discord.ButtonStyle.success
            if kind == "guild"
            else discord.ButtonStyle.secondary
        )
        self.banner_button.style = (
            discord.ButtonStyle.success
            if kind == "banner"
            else discord.ButtonStyle.secondary
        )

    async def _show(self, interaction, kind):
        # THROTTLE FIRST, before anything expensive is started. A click is not
        # a cheap redraw: it can run an uncached fetch_user (the banner tab) and
        # always repaints a collage through the bot-wide image semaphore, which
        # is exactly what the command's own cooldown rations. Only view CREATION
        # was rationed, so one member holding a card open could hammer three
        # buttons and monopolise the fleet's two Pillow slots for free.
        # Rejected clicks never touch the window (see Cooldowns.touch below), so
        # hammering cannot extend anyone's own wait either.
        if _RENDER_DEBOUNCE.is_active(interaction.user.id):
            return await interaction.response.send_message(
                _("You are flipping through this too fast - give it a moment."),
                ephemeral=True,
            )
        _RENDER_DEBOUNCE.touch(interaction.user.id)
        await interaction.response.defer()
        try:
            # Banners are not pushed by Discord, so grab one at view time too.
            if kind == "banner":
                await self.cog.capture_banner(self.member)
            guild_id = self.guild.id if (kind == "guild" and self.guild) else None
            embed, buf = await self.cog.build_payload(self.member, kind, guild_id)
            self._set_active(kind)
            if buf is None:
                await self.message.edit(embed=embed, attachments=[], view=self)
            else:
                await self.message.edit(
                    embed=embed,
                    attachments=[discord.File(buf, "history.png")],
                    view=self,
                )
        except Exception:
            log.exception("failed to render avatar history (%s)", kind)
            await interaction.followup.send(
                _("Something went wrong loading that history."), ephemeral=True
            )

    @discord.ui.button(label="Global")
    async def global_button(self, interaction, button):
        await self._show(interaction, "global")

    @discord.ui.button(label="Server")
    async def server_button(self, interaction, button):
        await self._show(interaction, "guild")

    @discord.ui.button(label="Banner")
    async def banner_button(self, interaction, button):
        await self._show(interaction, "banner")



class AvatarHistory(commands.Cog):
    """Records users' avatar/banner changes and builds history collages."""

    def __init__(self, bot):
        self.bot = bot
        self._compression_task = self.bot.loop.create_task(
            self._compress_existing_history()
        )

    def cog_unload(self):
        self._compression_task.cancel()

    @staticmethod
    def compress_for_storage(raw):
        """Return a bounded WebP representation suitable for collage storage."""
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            image = source.convert(
                "RGBA" if source.mode in {"RGBA", "LA"} else "RGB"
            )
        image.thumbnail(
            (STORAGE_MAX_SIZE, STORAGE_MAX_SIZE), Image.Resampling.LANCZOS
        )
        output = io.BytesIO()
        image.save(
            output,
            "WEBP",
            quality=STORAGE_WEBP_QUALITY,
            method=6,
        )
        return output.getvalue()

    async def _compress_existing_history(self):
        """Gradually recompress legacy PNG rows without delaying bot startup."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                rows = await self.bot.db_pool.fetch(
                    "SELECT id, avatar FROM avatar_history "
                    "WHERE image_format = 'png' ORDER BY id LIMIT $1",
                    COMPRESSION_BATCH_SIZE,
                )
                if not rows:
                    return
                for row in rows:
                    raw = bytes(row["avatar"])
                    try:
                        compressed = await rendering.run_image_job(
                            self.bot, self.compress_for_storage, raw
                        )
                    except Exception:
                        log.exception(
                            "failed to recompress avatar history row %s",
                            row["id"],
                        )
                        compressed = raw
                    if len(compressed) < len(raw):
                        await self.bot.db_pool.execute(
                            "UPDATE avatar_history "
                            "SET avatar = $2, image_format = 'webp' "
                            "WHERE id = $1 AND image_format = 'png'",
                            row["id"],
                            compressed,
                        )
                    else:
                        await self.bot.db_pool.execute(
                            "UPDATE avatar_history SET image_format = 'original' "
                            "WHERE id = $1 AND image_format = 'png'",
                            row["id"],
                        )
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("avatar history compression worker failed")
                await asyncio.sleep(60)

    async def _record(self, user_id, guild_id, kind, asset):
        """Single recording path for every tracked image kind."""
        if asset is None:
            return
        try:
            tracking_enabled = await settings.get_user(
                self.bot.db_pool, user_id, TRACKING_PREF_KEY, True
            )
            if not tracking_enabled:
                return
            ref = asset.key
            last = await self.bot.db_pool.fetchval(
                "SELECT ref FROM avatar_history "
                "WHERE user_id = $1 AND kind = $2 AND guild_id IS NOT DISTINCT FROM $3 "
                "ORDER BY changed_at DESC LIMIT 1",
                user_id,
                kind,
                guild_id,
            )
            if last == ref:
                return

            try:
                data = await asset.replace(size=256, format="png").read()
            except discord.NotFound:
                return  # the asset already vanished (avatar changed again); skip
            except discord.HTTPException:
                log.warning(
                    "could not download %s image for user %s", kind, user_id
                )
                return
            # A 256px PNG is small; anything large is unexpected (a malformed or
            # pathological image), so skip it rather than store/parse it.
            if len(data) > 2 * 1024 * 1024:
                log.warning(
                    "skipping oversized %s image for user %s (%d bytes)",
                    kind,
                    user_id,
                    len(data),
                )
                return
            data = await rendering.run_image_job(
                self.bot, self.compress_for_storage, data
            )
            await privacy.store_avatar_if_tracking(
                self.bot.db_pool,
                user_id=user_id,
                guild_id=guild_id,
                kind=kind,
                ref=ref,
                avatar=data,
                history_limit=HISTORY_LIMIT,
            )
        except Exception:
            log.exception("failed to record %s image for user %s", kind, user_id)

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        # on_user_update also fires for username/discriminator edits, so only act
        # on an actual avatar change (and skip default avatars). This avoids a DB
        # round-trip on every unrelated profile update.
        if after.avatar is None:
            return
        if before.avatar is not None and before.avatar.key == after.avatar.key:
            return
        await self._record(after.id, None, "global", after.avatar)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if after.guild_avatar is not None and (
            before.guild_avatar is None
            or before.guild_avatar.key != after.guild_avatar.key
        ):
            await self._record(
                after.id, after.guild.id, "guild", after.guild_avatar
            )

    async def capture_banner(self, user, *, fetched=None):
        """Best-effort banner capture (Discord never pushes banner changes).

        LAZY ON PURPOSE: it runs only when somebody actually asks to see a
        banner - the Banner button on a history card (AvatarHistoryView._show)
        or `?userinfo`, which shows one - never on an ambient gateway event.
        There used to be an on_member_join listener here that fetched a banner
        for EVERY join, bots included, ungated - one uncached REST call per join
        across every guild the bot is in. At 1000+ guilds that is a fleet-wide
        cost paid on raids, bot invites and every member who will never open the
        command, to pre-warm a history nobody may ever look at. The demand-side
        capture covers the same ground at the moment it is worth anything: the
        first person to look captures the banner then and there, and every later
        look adds a new one whenever it changed.

        ``fetched`` is the already-fetched `discord.User` when the CALLER had to
        fetch it anyway. `?userinfo` does, to render the banner it is about to
        show; without this it fetched the very same uncached user twice per
        invocation, and the archiving half was pure waste. Pass it whenever one
        is in hand; leave it off and this does its own fetch.

        Avatars are unaffected - Discord DOES push those, so on_user_update and
        on_member_update still record them for free, with no REST call at all.

        The opt-out check is a warm cached read and runs first, so an opted-out
        user costs zero round-trips on the path that still has to fetch (and
        nothing is recorded on the path that already fetched).
        """
        try:
            tracking_enabled = await settings.get_user(
                self.bot.db_pool, user.id, TRACKING_PREF_KEY, True
            )
            if not tracking_enabled:
                return
            if fetched is None:
                fetched = await self.bot.fetch_user(user.id)
            if fetched.banner:
                await self._record(user.id, None, "banner", fetched.banner)
        except Exception:
            log.exception("failed to capture banner for user %s", user.id)

    @staticmethod
    def build_collage(images):
        cell = 96
        n = len(images)
        cols = min(8, max(1, math.ceil(math.sqrt(n))))
        rows = math.ceil(n / cols)
        canvas = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
        for i, raw in enumerate(images):
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGBA").resize(
                    (cell, cell)
                )
                canvas.paste(im, ((i % cols) * cell, (i // cols) * cell))
            except Exception:
                continue
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        buf.seek(0)
        return buf

    async def _collage_for(self, member, kind, guild_id):
        """Fetch the retained rows for a kind and render them into a collage."""
        rows = await self.bot.db_pool.fetch(
            "SELECT avatar FROM avatar_history "
            "WHERE user_id = $1 AND kind = $2 AND guild_id IS NOT DISTINCT FROM $3 "
            "ORDER BY changed_at DESC LIMIT $4",
            member.id,
            kind,
            guild_id,
            HISTORY_LIMIT,
        )
        if not rows:
            return None
        images = [bytes(r["avatar"]) for r in rows]
        buf = await rendering.run_image_job(
            self.bot, self.build_collage, images
        )
        return buf, len(images)

    async def build_payload(self, member, kind, guild_id):
        """Build the (embed, buffer) pair for a kind; buffer is None if empty."""
        embed = discord.Embed(title=_(KIND_TITLES[kind]), colour=random_colour())
        embed.set_author(
            name=f"{member} ({member.id})",
            icon_url=member.display_avatar.url,
        )
        result = await self._collage_for(member, kind, guild_id)
        if result is None:
            embed.description = _("No {kind} history recorded yet.").format(
                kind=KIND_NOUNS[kind]
            )
            return embed, None
        buf, count = result
        embed.description = _(
            "Showing `{count}` of up to `{limit}` changes"
        ).format(count=count, limit=HISTORY_LIMIT)
        embed.set_image(url="attachment://history.png")
        return embed, buf

    async def _is_member_here(self, ctx, user):
        """Is ``user`` a member of the guild the command was run in?

        True / False / None, where None means UNKNOWN and the caller must
        refuse: this is a privacy gate, so the only answer that opens the door
        is a positive one.

        The history is a record of somebody's past faces, keyed by a raw user
        id. Without this the command answered for ANY id on Discord, so anyone
        could read the stored history of a person they share no server with -
        someone who has no way of even knowing this bot exists. The audience is
        therefore cut to the room the question is asked from: a server the asker
        is in, since running the command there proves it.

        THE MEMBER CACHE IS SPARSE (core.py sets chunk_guilds_at_startup=False),
        so ``get_member`` answering None is not evidence of absence - only of
        ignorance. It is confirmed with a REST ``fetch_member``, the same
        discipline cogs/community/reminders.py's audience check follows, and
        every outcome that is not "yes, they are here" refuses:
        ``NotFound`` because the API said so, any other HTTP failure because a
        blip must not be a way to read a stranger's history.

        Scale story (1000+ guilds): zero REST calls for a cached member (the
        members intent keeps the people who talk in cache) and at most ONE per
        invocation otherwise, behind the command's 1-per-10s-per-user cooldown.
        No fleet-wide loop over bot.guilds, at any size.
        """
        guild = ctx.guild
        if guild is None:
            return False
        if guild.get_member(user.id) is not None:
            return True
        fetch = getattr(guild, "fetch_member", None)
        if fetch is None:  # pragma: no cover - a stand-in guild without REST
            return None
        try:
            await fetch(user.id)
        except discord.NotFound:
            return False
        except discord.HTTPException:
            log.debug("could not confirm membership for %s", user.id, exc_info=True)
            return None
        return True

    @commands.hybrid_command(aliases=["avh"])
    @commands.cooldown(1, HISTORY_COOLDOWN_SECONDS, commands.BucketType.user)
    @discord.app_commands.describe(member="Whose history to show (defaults to you).")
    async def avatarhistory(self, ctx, member: discord.User = None):
        """Show a collage of a user's avatar / server avatar / banner history."""

        member = member or ctx.author
        if member.id != ctx.author.id:
            # Your own history is always yours to look at, anywhere, DMs
            # included. Anyone else's is readable only from a server you share
            # with them - see _is_member_here.
            here = await self._is_member_here(ctx, member)
            if here is None:
                return await ctx.send(
                    _("I could not check that right now - try again in a moment."),
                    ephemeral=True,
                )
            if not here:
                return await ctx.send(
                    _(
                        "I only show someone else's history in a server you "
                        "share with them - ask again there."
                    ),
                    ephemeral=True,
                )
        async with ctx.typing():
            view = AvatarHistoryView(self, ctx, member)
            embed, buf = await self.build_payload(member, "global", None)
            if buf is None:
                view.message = await ctx.send(embed=embed, view=view)
            else:
                view.message = await ctx.send(
                    embed=embed,
                    file=discord.File(buf, "history.png"),
                    view=view,
                )


async def setup(bot):
    await bot.add_cog(AvatarHistory(bot))
