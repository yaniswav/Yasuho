import io
import logging
import random

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from tools import embed_creator, settings
from tools.formats import random_colour
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Reuse a TTF already shipped with the bot (see cogs/fun/fun.py); fall back to
# PIL's bitmap default if the file is missing so a render never hard-fails.
_FONT_PATH = "ressources/fonts/impact.ttf"

# Hint shown in modals so admins know what they can interpolate. The embed
# editing (modals, edit select, render, colour parsing, summary) all comes from
# tools.embed_creator now; welcome only owns its token vocabulary and the
# non-embed controls below.
PLACEHOLDER_HINT = "{mention} {user} {server} {count} {membercount}"
ASSET_HINT = "https://... or {avatar}"


def _default_config():
    """A fresh, fully-populated welcome config blob (no shared references)."""

    return {
        "channel_id": None,
        "enabled": False,
        "ping": True,
        "card": True,
        "gifs": [],
        "random_gif": False,
        "embed": {
            "title": "Welcome {user}!",
            "description": "Welcome to {server}, {mention}! You are member #{count}.",
            "color": 0x5865F2,
            "author": {"name": "", "icon": ""},
            "footer": {"text": "", "icon": ""},
            "thumbnail": "{avatar}",
            "image": "",
            "fields": [],
        },
    }


def _merge_defaults(blob):
    """Return a fresh config merged over the defaults (fills missing keys).

    Every nested container is rebuilt so the result never aliases the settings
    cache; the panel can mutate it freely and persist with one set_guild call.
    The "embed" sub-blob is normalised by embed_creator.merge_embed, the shared
    spine for every embed-builder cog.
    """

    config = _default_config()
    if not isinstance(blob, dict):
        return config

    for key in ("channel_id", "enabled", "ping", "card", "random_gif"):
        if key in blob:
            config[key] = blob[key]
    config["gifs"] = list(blob.get("gifs") or [])
    config["embed"] = embed_creator.merge_embed(blob.get("embed"))
    return config


# ----------------------------------------------------------------------
# GIF pool modal (welcome-specific; the embed modals come from embed_creator)
# ----------------------------------------------------------------------
class AddGifModal(discord.ui.Modal):
    """Add a single GIF/image URL to the random pool."""

    def __init__(self, manage_view):
        super().__init__(title="Add a GIF")
        self.manage_view = manage_view
        self.url_field = discord.ui.TextInput(
            label="GIF or image URL",
            required=True,
            max_length=1024,
            placeholder="https://...",
        )
        self.add_item(self.url_field)

    async def on_submit(self, interaction):
        try:
            url = self.url_field.value.strip()
            if not embed_creator.is_url(url):
                return await interaction.response.send_message(
                    "That doesn't look like a valid URL.", ephemeral=True
                )
            gifs = self.manage_view.config.setdefault("gifs", [])
            gifs.append(url)
            await self.manage_view.cog.save(
                self.manage_view.guild.id, self.manage_view.config
            )
            await self.manage_view.refresh(interaction)
        except Exception:
            log.exception("Welcome add-GIF modal failed")
            try:
                await interaction.response.send_message(
                    "Something went wrong.", ephemeral=True
                )
            except discord.HTTPException:
                pass


# ----------------------------------------------------------------------
# Panel components
# ----------------------------------------------------------------------
class WelcomeChannelSelect(discord.ui.ChannelSelect):
    """Pick the channel welcome messages are sent to."""

    def __init__(self, panel):
        self.panel = panel
        defaults = []
        cid = panel.config.get("channel_id")
        if cid:
            channel = panel.guild.get_channel(cid)
            if channel is not None:
                defaults = [channel]
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Select the welcome channel...",
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["channel_id"] = self.values[0].id
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Welcome channel select failed")
            await self.panel._error(interaction)


class _ToggleButton(discord.ui.Button):
    """A boolean config toggle, green when on and grey when off."""

    def __init__(self, panel, key, label):
        self.panel = panel
        self.key = key
        on = bool(panel.config.get(key))
        super().__init__(
            label=f"{label}: {'On' if on else 'Off'}",
            style=(
                discord.ButtonStyle.success
                if on
                else discord.ButtonStyle.secondary
            ),
            row=2,
        )

    async def callback(self, interaction):
        try:
            self.panel.config[self.key] = not bool(
                self.panel.config.get(self.key)
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Welcome toggle button failed")
            await self.panel._error(interaction)


class _ManageGifsButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label="Manage GIFs", style=discord.ButtonStyle.primary, row=2
        )

    async def callback(self, interaction):
        try:
            view = ManageGifsView(self.panel)
            await interaction.response.send_message(
                embed=view.build_embed(), view=view, ephemeral=True
            )
        except Exception:
            log.exception("Welcome manage-GIFs launch failed")
            await self.panel._error(interaction)


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label="Preview", style=discord.ButtonStyle.primary, row=2
        )

    async def callback(self, interaction):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.panel.cog.send_preview(interaction, self.panel.config)
        except Exception:
            log.exception("Welcome preview failed")
            try:
                await interaction.followup.send(
                    "Could not render the preview.", ephemeral=True
                )
            except discord.HTTPException:
                pass


class _EnableButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        enabled = bool(panel.config.get("enabled"))
        super().__init__(
            label="Disable" if enabled else "Enable",
            style=(
                discord.ButtonStyle.danger
                if enabled
                else discord.ButtonStyle.success
            ),
            row=3,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["enabled"] = not bool(
                self.panel.config.get("enabled")
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Welcome enable button failed")
            await self.panel._error(interaction)


# ----------------------------------------------------------------------
# Manage-GIFs sub-panel (ephemeral)
# ----------------------------------------------------------------------
class RemoveGifSelect(discord.ui.Select):
    def __init__(self, manage_view):
        self.manage_view = manage_view
        gifs = manage_view.config.get("gifs") or []
        options = []
        for index, url in enumerate(gifs[:25]):
            name = url.rsplit("/", 1)[-1] or url
            options.append(
                discord.SelectOption(
                    label=name[:100] or "GIF",
                    value=str(index),
                    description=url[:100],
                )
            )
        super().__init__(
            placeholder="Remove a GIF...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction):
        try:
            index = int(self.values[0])
            gifs = self.manage_view.config.get("gifs") or []
            if 0 <= index < len(gifs):
                gifs.pop(index)
                self.manage_view.config["gifs"] = gifs
                await self.manage_view.cog.save(
                    self.manage_view.guild.id, self.manage_view.config
                )
            await self.manage_view.refresh(interaction)
        except Exception:
            log.exception("Welcome remove-GIF select failed")
            try:
                await interaction.response.send_message(
                    "Something went wrong.", ephemeral=True
                )
            except discord.HTTPException:
                pass


class ManageGifsView(AuthorView):
    """Small ephemeral flow to add/remove GIFs in the random pool."""

    def __init__(self, panel, timeout=180):
        super().__init__(
            panel.author_id,
            timeout=timeout,
            deny_message="This panel isn't for you.",
        )
        self.panel = panel
        self.cog = panel.cog
        self.guild = panel.guild
        self.config = panel.config
        if self.config.get("gifs"):
            self.add_item(RemoveGifSelect(self))

    def build_embed(self):
        gifs = self.config.get("gifs") or []
        embed = discord.Embed(
            title="Manage welcome GIFs",
            description=(
                "Add image/GIF URLs to the random pool, or remove one below. "
                "Turn on **Random GIF** on the main panel to use the pool."
            ),
            colour=0x5865F2,
        )
        if gifs:
            lines = [f"{i + 1}. {url}" for i, url in enumerate(gifs[:15])]
            if len(gifs) > 15:
                lines.append(f"...and {len(gifs) - 15} more")
            value = "\n".join(lines)
            embed.add_field(
                name=f"Pool ({len(gifs)})", value=value[:1024], inline=False
            )
        else:
            embed.add_field(
                name="Pool (0)",
                value="No GIFs yet. Add one to build the random pool.",
                inline=False,
            )
        return embed

    @discord.ui.button(
        label="Add GIF", style=discord.ButtonStyle.success, row=0
    )
    async def add_button(self, interaction, button):
        try:
            await interaction.response.send_modal(AddGifModal(self))
        except Exception:
            log.exception("Welcome add-GIF launch failed")
            try:
                await interaction.response.send_message(
                    "Could not open the form.", ephemeral=True
                )
            except discord.HTTPException:
                pass

    async def refresh(self, interaction):
        """Re-render this ephemeral view and sync the main panel summary."""

        new = ManageGifsView(self.panel)
        self.stop()
        embed = new.build_embed()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=new)
            else:
                await interaction.edit_original_response(
                    embed=embed, view=new
                )
        except discord.HTTPException:
            pass
        await self.panel.sync_message()


# ----------------------------------------------------------------------
# Main control panel
# ----------------------------------------------------------------------
class WelcomePanel(AuthorView):
    """Author-restricted welcome control panel (the single entry point).

    This View is the embed_creator.EmbedEditorHost for the welcome embed: it
    exposes ``embed_config`` (the config["embed"] sub-blob the shared modals
    mutate) and ``on_embed_changed`` (persist + refresh). ``placeholder_hint``
    and ``asset_hint`` feed the shared modals' input placeholders.
    """

    def __init__(self, cog, guild, author_id, config, timeout=180):
        super().__init__(
            author_id,
            timeout=timeout,
            deny_message="This panel isn't for you.",
        )
        self.cog = cog
        self.guild = guild
        self.config = config
        # Read by the embed_creator modals via getattr.
        self.placeholder_hint = PLACEHOLDER_HINT
        self.asset_hint = ASSET_HINT

        self.add_item(WelcomeChannelSelect(self))
        self.add_item(
            embed_creator.make_edit_select(
                self, placeholder="Edit the welcome embed...", row=1
            )
        )
        self.add_item(_ToggleButton(self, "card", "Card"))
        self.add_item(_ToggleButton(self, "random_gif", "Random GIF"))
        self.add_item(_ToggleButton(self, "ping", "Ping"))
        self.add_item(_ManageGifsButton(self))
        self.add_item(_PreviewButton(self))
        self.add_item(_EnableButton(self))

    # -- embed_creator.EmbedEditorHost contract -------------------------
    @property
    def embed_config(self):
        """The embed sub-blob the shared modals/edit select mutate in place."""

        return self.config["embed"]

    async def on_embed_changed(self, interaction):
        """Persist the config blob and re-render the panel in place.

        Called by the shared embed_creator modals and edit select after they
        mutate embed_config; mirrors the channel/toggle save + refresh flow.
        """

        await self.cog.save(self.guild.id, self.config)
        await self._refresh(interaction)

    def build_embed(self):
        config = self.config
        embed_cfg = config.get("embed") or {}
        enabled = bool(config.get("enabled"))
        colour = embed_cfg.get("color")

        embed = discord.Embed(
            title="Welcome system",
            description=(
                "Design the greeting new members receive. Use the menus below; "
                "every change saves instantly. Hit **Preview** to see it live."
            ),
            colour=colour if isinstance(colour, int) else 0x5865F2,
        )

        cid = config.get("channel_id")
        channel_value = f"<#{cid}>" if cid else "*Not set.*"
        embed.add_field(
            name="Status",
            value="\U0001F7E2 Enabled" if enabled else "\U0001F534 Disabled",
            inline=True,
        )
        embed.add_field(name="Channel", value=channel_value, inline=True)
        embed.add_field(
            name="GIF pool",
            value=f"{len(config.get('gifs') or [])} saved",
            inline=True,
        )

        embed.add_field(
            name="Card",
            value="On" if config.get("card") else "Off",
            inline=True,
        )
        embed.add_field(
            name="Random GIF",
            value="On" if config.get("random_gif") else "Off",
            inline=True,
        )
        embed.add_field(
            name="Ping",
            value="On" if config.get("ping") else "Off",
            inline=True,
        )

        embed.add_field(
            name="Embed",
            value=embed_creator.summarise(embed_cfg),
            inline=False,
        )

        embed.set_footer(
            text=(
                "Only you can use these controls. "
                f"Placeholders: {PLACEHOLDER_HINT}"
            )
        )
        return embed

    async def _refresh(self, interaction):
        """Rebuild a fresh panel from current config and show it in place."""

        new = WelcomePanel(self.cog, self.guild, self.author_id, self.config)
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def sync_message(self):
        """Re-render the stored panel message (used by the GIF sub-panel)."""

        if self.message is None:
            return
        new = WelcomePanel(self.cog, self.guild, self.author_id, self.config)
        new.message = self.message
        self.stop()
        try:
            await self.message.edit(embed=new.build_embed(), view=new)
        except discord.HTTPException:
            pass

    async def _error(self, interaction):
        await embed_creator.notify_failure(interaction, "Something went wrong.")


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class Welcome(commands.Cog):
    """Greet new members with a configurable embed, card, and GIF pool."""

    def __init__(self, bot):
        self.bot = bot

    # -- config storage (single JSONB blob per guild) -------------------
    async def get_config(self, guild_id):
        """Load the welcome blob, migrating the legacy table on first access."""

        pool = self.bot.db_pool
        blob = await settings.get_guild(pool, guild_id, "welcome", None)
        if blob is not None:
            return _merge_defaults(blob)

        # No blob yet: seed it from the legacy ``welcome`` table if present.
        config = _default_config()
        try:
            row = await pool.fetchrow(
                "SELECT channel_id, message FROM welcome WHERE guild_id = $1;",
                guild_id,
            )
        except Exception:
            log.exception("Welcome legacy lookup failed")
            row = None

        if row is not None:
            config["channel_id"] = row["channel_id"]
            config["enabled"] = True
            if row["message"]:
                config["embed"]["description"] = row["message"]
            await self.save(guild_id, config)
        return config

    async def save(self, guild_id, config):
        await settings.set_guild(self.bot.db_pool, guild_id, "welcome", config)

    # -- placeholder + embed building -----------------------------------
    def _substitution(self, member):
        """Build the token resolver passed to embed_creator.render.

        Folds every welcome placeholder ({mention}/{user}/{server}/{count}/
        {membercount}) and the asset token {avatar} into one callable, so the
        text parts and the thumbnail/image URLs all resolve through render's
        single substitute path.
        """

        guild = member.guild if member else None
        count = guild.member_count if guild else None
        count_text = str(count) if count is not None else ""
        avatar = member.display_avatar.url if member else ""
        replacements = {
            "{mention}": member.mention if member else "",
            "{user}": member.display_name if member else "",
            "{server}": guild.name if guild else "",
            "{count}": count_text,
            "{membercount}": count_text,
            "{avatar}": avatar,
        }

        def substitute(text):
            for key, value in replacements.items():
                text = text.replace(key, value)
            return text

        return substitute

    def _compose(self, config, member):
        """Build (content, embed) exactly as a real join would render."""

        substitute = self._substitution(member)
        embed = embed_creator.render(
            config.get("embed") or {}, substitute=substitute
        )
        gifs = config.get("gifs") or []
        if config.get("random_gif") and gifs:
            embed.set_image(url=random.choice(gifs))

        if not embed_creator.embed_has_content(embed):
            embed.description = substitute("Welcome {mention}!")

        content = member.mention if config.get("ping") else None
        return content, embed

    # -- welcome card ---------------------------------------------------
    async def render_welcome_card(self, member):
        """Render a welcome card for a joining member.

        Returns a BytesIO PNG. All Pillow work runs in an executor so the join
        event loop is never blocked. The caller wraps this in try/except and
        falls back to a text-only welcome on any failure.
        """

        # Pull the avatar bytes off the loop before handing PIL the raw data.
        avatar_bytes = await member.display_avatar.replace(size=128).read()
        display_name = member.display_name
        member_count = member.guild.member_count or 0
        colour = random_colour()
        bg_rgb = ((colour >> 16) & 0xFF, (colour >> 8) & 0xFF, colour & 0xFF)

        def _render():
            width, height = 640, 200
            size = 128
            ring = 6
            card = Image.new("RGBA", (width, height), bg_rgb + (255,))
            draw = ImageDraw.Draw(card)

            # Avatar drawn in a circle, with a white ring behind it. The mask is
            # built at 4x then downscaled so the circle edge stays smooth.
            avatar = (
                Image.open(io.BytesIO(avatar_bytes))
                .convert("RGBA")
                .resize((size, size), Image.LANCZOS)
            )
            mask = Image.new("L", (size * 4, size * 4), 0)
            ImageDraw.Draw(mask).ellipse(
                (0, 0, size * 4, size * 4), fill=255
            )
            mask = mask.resize((size, size), Image.LANCZOS)

            avatar_x = 36
            avatar_y = (height - size) // 2
            draw.ellipse(
                (
                    avatar_x - ring,
                    avatar_y - ring,
                    avatar_x + size + ring,
                    avatar_y + size + ring,
                ),
                fill=(255, 255, 255, 255),
            )
            card.paste(avatar, (avatar_x, avatar_y), mask)

            try:
                title_font = ImageFont.truetype(_FONT_PATH, size=38)
                sub_font = ImageFont.truetype(_FONT_PATH, size=24)
            except Exception:
                title_font = ImageFont.load_default()
                sub_font = ImageFont.load_default()

            text_x = avatar_x + size + ring + 28
            available = width - text_x - 24

            # Shrink the greeting until it fits the remaining width so long
            # display names never overflow the card.
            name = display_name
            welcome_text = f"Welcome {name}!"
            while (
                name
                and draw.textlength(welcome_text, font=title_font) > available
            ):
                name = name[:-1]
                welcome_text = f"Welcome {name.rstrip()}...!"

            draw.text(
                (text_x, 60),
                welcome_text,
                font=title_font,
                fill=(255, 255, 255, 255),
                stroke_width=2,
                stroke_fill=(0, 0, 0, 160),
            )
            draw.text(
                (text_x, 112),
                f"Member #{member_count}",
                font=sub_font,
                fill=(255, 255, 255, 255),
                stroke_width=1,
                stroke_fill=(0, 0, 0, 160),
            )

            buf = io.BytesIO()
            card.convert("RGB").save(buf, "PNG")
            buf.seek(0)
            return buf

        return await self.bot.loop.run_in_executor(None, _render)

    async def _render_card_file(self, member):
        try:
            buf = await self.render_welcome_card(member)
            return discord.File(buf, filename="welcome.png")
        except Exception:
            log.exception("Failed to render welcome card")
            return None

    async def send_preview(self, interaction, config):
        """Render the welcome exactly as a real join would, shown to the admin."""

        member = interaction.user
        content, embed = self._compose(config, member)
        file = await self._render_card_file(member) if config.get("card") else None
        kwargs = {"embed": embed, "ephemeral": True}
        if content:
            kwargs["content"] = content
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)

    # -- commands -------------------------------------------------------
    @commands.hybrid_group(name="welcome")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome(self, ctx):
        """Open the welcome control panel."""

        if ctx.invoked_subcommand is not None:
            return

        config = await self.get_config(ctx.guild.id)
        view = WelcomePanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @welcome.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_set(self, ctx, channel: discord.TextChannel, *, message: str):
        """Set the welcome channel and message (a simple fallback).

        Placeholders: {mention}, {user}, {server}, {count}, {membercount}.
        """

        config = await self.get_config(ctx.guild.id)
        config["channel_id"] = channel.id
        config["enabled"] = True
        config["embed"]["description"] = message
        await self.save(ctx.guild.id, config)

        embed = discord.Embed(title="Welcome message", colour=random_colour())
        embed.add_field(name="Channel", value=channel.mention, inline=False)
        embed.add_field(
            name="Message",
            value=(message if len(message) <= 1024 else message[:1021] + "..."),
            inline=False,
        )
        embed.set_footer(text="Use /welcome to open the full builder.")
        await ctx.send(embed=embed)

    @welcome.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_disable(self, ctx):
        """Disable the welcome message for your guild."""

        config = await self.get_config(ctx.guild.id)
        config["enabled"] = False
        await self.save(ctx.guild.id, config)

        embed = discord.Embed(title="Welcome message", colour=random_colour())
        embed.add_field(
            name="Disabled",
            value="Welcome messages have been turned off.",
            inline=False,
        )
        await ctx.send(embed=embed)

    @welcome.command(name="test")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx):
        """Simulate a join for yourself to preview the welcome."""

        config = await self.get_config(ctx.guild.id)
        content, embed = self._compose(config, ctx.author)
        file = (
            await self._render_card_file(ctx.author)
            if config.get("card")
            else None
        )
        kwargs = {"embed": embed}
        if content:
            kwargs["content"] = content
        if file is not None:
            kwargs["file"] = file
        await ctx.send(**kwargs)

    # -- join listener --------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            if member.bot:
                return
            if member.id in self.bot.blacklist:
                return

            config = await self.get_config(member.guild.id)
            if not config.get("enabled"):
                return
            channel_id = config.get("channel_id")
            if not channel_id:
                return
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                return

            content, embed = self._compose(config, member)
            file = (
                await self._render_card_file(member)
                if config.get("card")
                else None
            )
            kwargs = {"embed": embed}
            if content:
                kwargs["content"] = content
            if file is not None:
                kwargs["file"] = file
            await channel.send(**kwargs)
        except Exception:
            log.exception("Welcome on_member_join failed")
            # A join must never break: fall back to a minimal text welcome.
            try:
                config = await self.get_config(member.guild.id)
                channel = member.guild.get_channel(config.get("channel_id"))
                if channel is not None:
                    await channel.send(f"Welcome {member.mention}!")
            except Exception:
                log.exception("Welcome fallback send failed")


async def setup(bot):
    await bot.add_cog(Welcome(bot))
