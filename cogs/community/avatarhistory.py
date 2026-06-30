import io
import logging
import math

import discord
from discord.ext import commands
from PIL import Image

from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorView

log = logging.getLogger(__name__)

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

    async def _record(self, user_id, guild_id, kind, asset):
        """Single recording path for every tracked image kind."""
        if asset is None:
            return
        try:
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
            await self.bot.db_pool.execute(
                "INSERT INTO avatar_history(user_id, guild_id, kind, ref, avatar) "
                "VALUES($1, $2, $3, $4, $5)",
                user_id,
                guild_id,
                kind,
                ref,
                data,
            )
            await self.bot.db_pool.execute(
                "DELETE FROM avatar_history "
                "WHERE user_id = $1 AND kind = $2 AND guild_id IS NOT DISTINCT FROM $3 "
                "AND id NOT IN ("
                "SELECT id FROM avatar_history "
                "WHERE user_id = $1 AND kind = $2 AND guild_id IS NOT DISTINCT FROM $3 "
                "ORDER BY changed_at DESC LIMIT 50)",
                user_id,
                kind,
                guild_id,
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

    async def capture_banner(self, user):
        """Best-effort banner capture (Discord never pushes banner changes)."""
        try:
            fetched = await self.bot.fetch_user(user.id)
            if fetched.banner:
                await self._record(user.id, None, "banner", fetched.banner)
        except Exception:
            log.exception("failed to capture banner for user %s", user.id)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self.capture_banner(member)

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
        """Fetch up to 50 rows for a kind and render them into a collage."""
        rows = await self.bot.db_pool.fetch(
            "SELECT avatar FROM avatar_history "
            "WHERE user_id = $1 AND kind = $2 AND guild_id IS NOT DISTINCT FROM $3 "
            "ORDER BY changed_at DESC LIMIT 50",
            member.id,
            kind,
            guild_id,
        )
        if not rows:
            return None
        images = [bytes(r["avatar"]) for r in rows]
        buf = await self.bot.loop.run_in_executor(
            None, self.build_collage, images
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
        embed.description = _("Showing `{count}` of up to `50` changes").format(
            count=count
        )
        embed.set_image(url="attachment://history.png")
        return embed, buf

    @commands.hybrid_command(aliases=["avh"])
    async def avatarhistory(self, ctx, member: discord.User = None):
        """Show a collage of a user's avatar / server avatar / banner history."""

        member = member or ctx.author
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
