import logging
import types
import typing

import discord
from discord.ext import commands

from tools import settings
from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)

# Twitch brand purple, used as the default embed colour.
TWITCH_PURPLE = 0x9146FF

# Legacy "Live" role name (invisible-emote style) kept for backward compat when
# no role_id is configured. The trailing char is a red circle (U+1F534).
LEGACY_ROLE_NAME = "Live \U0001F534"

# Hint shown in modals so admins know what they can interpolate.
PLACEHOLDER_HINT = "{streamer} {mention} {url} {game} {title} {server}"
ASSET_HINT = "https://... or {avatar}"

# Common colour names accepted by the colour modal (alongside #rrggbb).
COLOUR_NAMES = {
    "blurple": 0x5865F2,
    "twitch": 0x9146FF,
    "purple": 0x9146FF,
    "green": 0x2ECC71,
    "red": 0xE74C3C,
    "blue": 0x3498DB,
    "yellow": 0xF1C40F,
    "gold": 0xF1C40F,
    "orange": 0xE67E22,
    "pink": 0xE91E63,
    "magenta": 0xE91E63,
    "teal": 0x1ABC9C,
    "cyan": 0x1ABC9C,
    "white": 0xFFFFFF,
    "black": 0x000000,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
}

# Edit-menu options for the embed style: (value, label, emoji).
EMBED_EDIT_OPTIONS = [
    ("title", "Title", "\U0001F4DD"),
    ("description", "Description", "\U0001F4C4"),
    ("color", "Colour", "\U0001F3A8"),
    ("author", "Author", "\U0001F464"),
    ("footer", "Footer", "\U0001F516"),
    ("thumbnail", "Thumbnail", "\U0001F5BC"),
    ("image", "Image", "\U0001F305"),
    ("addfield", "Add field", "\U00002795"),
    ("clearfields", "Clear fields", "\U0001F9F9"),
]

# Edit-menu option for the classic-text style.
TEXT_EDIT_OPTIONS = [
    ("message", "Message", "\U0001F4AC"),
]


def _default_config():
    """A fresh, fully-populated Twitch alert config blob (no shared refs)."""

    return {
        "enabled": False,
        "channel_id": None,
        "role_id": None,
        "style": "embed",
        "text": "{mention} just went live on Twitch! Come watch: {url}",
        "embed": {
            "title": "{streamer} is now live on Twitch!",
            "description": (
                "{mention} just went live - come hang out!\n\n"
                "[Watch the stream]({url})"
            ),
            "color": TWITCH_PURPLE,
            "author": {"name": "", "icon": ""},
            "footer": {"text": "Streaming in {server}", "icon": ""},
            "thumbnail": "{avatar}",
            "image": "",
            "fields": [
                {"name": "Playing", "value": "{game}", "inline": True},
                {"name": "Title", "value": "{title}", "inline": True},
            ],
        },
    }


def _merge_defaults(blob):
    """Return a fresh config merged over the defaults (fills missing keys).

    Every nested container is rebuilt so the result never aliases the settings
    cache; the panel can mutate it freely and persist with one set_guild call.
    """

    config = _default_config()
    if not isinstance(blob, dict):
        return config

    for key in ("enabled", "channel_id", "role_id", "style", "text"):
        if key in blob:
            config[key] = blob[key]
    if config["style"] not in ("embed", "text"):
        config["style"] = "embed"

    embed = config["embed"]
    raw = blob.get("embed") or {}
    for key in ("title", "description", "color", "thumbnail", "image"):
        if key in raw:
            embed[key] = raw[key]
    embed["author"] = {
        "name": (raw.get("author") or {}).get("name", ""),
        "icon": (raw.get("author") or {}).get("icon", ""),
    }
    embed["footer"] = {
        "text": (raw.get("footer") or {}).get("text", ""),
        "icon": (raw.get("footer") or {}).get("icon", ""),
    }
    embed["fields"] = [
        {
            "name": f.get("name", ""),
            "value": f.get("value", ""),
            "inline": bool(f.get("inline")),
        }
        for f in (raw.get("fields") or [])
        if isinstance(f, dict)
    ]
    return config


def _parse_colour(text):
    """Parse '#rrggbb', 'rrggbb', or a common colour name. None if invalid."""

    if not text:
        return None
    text = text.strip().lower()
    if text in COLOUR_NAMES:
        return COLOUR_NAMES[text]
    text = text.lstrip("#")
    try:
        value = int(text, 16)
    except ValueError:
        return None
    if 0 <= value <= 0xFFFFFF:
        return value
    return None


def _is_url(value):
    return bool(value) and (
        value.startswith("http://") or value.startswith("https://")
    )


# ----------------------------------------------------------------------
# Modals (one per editable part)
# ----------------------------------------------------------------------
class _PanelModal(discord.ui.Modal):
    """Base modal: writes the config blob then re-renders the parent panel."""

    def __init__(self, panel, title):
        super().__init__(title=title)
        self.panel = panel

    async def _save_and_refresh(self, interaction):
        await self.panel.cog.save(self.panel.guild.id, self.panel.config)
        await self.panel._refresh(interaction)

    async def _fail(self, interaction):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class MessageModal(_PanelModal):
    """Edit the classic-text alert message (used when style == 'text')."""

    def __init__(self, panel):
        super().__init__(panel, "Edit message")
        self.field = discord.ui.TextInput(
            label="Alert message",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=2000,
            default=panel.config.get("text") or None,
            placeholder=PLACEHOLDER_HINT,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["text"] = self.field.value.strip()
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch message modal failed")
            await self._fail(interaction)


class TitleModal(_PanelModal):
    def __init__(self, panel):
        super().__init__(panel, "Edit title")
        self.field = discord.ui.TextInput(
            label="Title",
            style=discord.TextStyle.short,
            required=False,
            max_length=256,
            default=panel.config["embed"].get("title") or None,
            placeholder=PLACEHOLDER_HINT,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["embed"]["title"] = self.field.value.strip()
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch title modal failed")
            await self._fail(interaction)


class DescriptionModal(_PanelModal):
    def __init__(self, panel):
        super().__init__(panel, "Edit description")
        self.field = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
            default=panel.config["embed"].get("description") or None,
            placeholder=PLACEHOLDER_HINT,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["embed"]["description"] = self.field.value.strip()
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch description modal failed")
            await self._fail(interaction)


class ColorModal(_PanelModal):
    def __init__(self, panel):
        super().__init__(panel, "Edit colour")
        current = panel.config["embed"].get("color")
        self.field = discord.ui.TextInput(
            label="Colour (#hex or name)",
            style=discord.TextStyle.short,
            required=False,
            max_length=20,
            default=(f"#{current:06X}" if isinstance(current, int) else None),
            placeholder="#9146FF, twitch, blurple, red...",
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            raw = self.field.value.strip()
            if not raw:
                self.panel.config["embed"]["color"] = None
            else:
                parsed = _parse_colour(raw)
                if parsed is None:
                    return await interaction.response.send_message(
                        "That colour wasn't recognised. Use #rrggbb or a name "
                        "like 'twitch' or 'blurple'.",
                        ephemeral=True,
                    )
                self.panel.config["embed"]["color"] = parsed
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch colour modal failed")
            await self._fail(interaction)


class AuthorModal(_PanelModal):
    def __init__(self, panel):
        super().__init__(panel, "Edit author")
        author = panel.config["embed"].get("author") or {}
        self.name_field = discord.ui.TextInput(
            label="Author name",
            required=False,
            max_length=256,
            default=author.get("name") or None,
            placeholder=PLACEHOLDER_HINT,
        )
        self.icon_field = discord.ui.TextInput(
            label="Author icon URL",
            required=False,
            max_length=1024,
            default=author.get("icon") or None,
            placeholder=ASSET_HINT,
        )
        self.add_item(self.name_field)
        self.add_item(self.icon_field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["embed"]["author"] = {
                "name": self.name_field.value.strip(),
                "icon": self.icon_field.value.strip(),
            }
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch author modal failed")
            await self._fail(interaction)


class FooterModal(_PanelModal):
    def __init__(self, panel):
        super().__init__(panel, "Edit footer")
        footer = panel.config["embed"].get("footer") or {}
        self.text_field = discord.ui.TextInput(
            label="Footer text",
            required=False,
            max_length=2048,
            default=footer.get("text") or None,
            placeholder=PLACEHOLDER_HINT,
        )
        self.icon_field = discord.ui.TextInput(
            label="Footer icon URL",
            required=False,
            max_length=1024,
            default=footer.get("icon") or None,
            placeholder=ASSET_HINT,
        )
        self.add_item(self.text_field)
        self.add_item(self.icon_field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["embed"]["footer"] = {
                "text": self.text_field.value.strip(),
                "icon": self.icon_field.value.strip(),
            }
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch footer modal failed")
            await self._fail(interaction)


class AssetModal(_PanelModal):
    """Edit a single image URL field (thumbnail or image)."""

    def __init__(self, panel, key, label):
        super().__init__(panel, f"Edit {label.lower()}")
        self.key = key
        self.field = discord.ui.TextInput(
            label=f"{label} URL",
            required=False,
            max_length=1024,
            default=panel.config["embed"].get(key) or None,
            placeholder=ASSET_HINT,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["embed"][self.key] = self.field.value.strip()
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch asset modal failed")
            await self._fail(interaction)


class AddFieldModal(_PanelModal):
    def __init__(self, panel):
        super().__init__(panel, "Add a field")
        self.name_field = discord.ui.TextInput(
            label="Field name",
            required=True,
            max_length=256,
            placeholder=PLACEHOLDER_HINT,
        )
        self.value_field = discord.ui.TextInput(
            label="Field value",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1024,
            placeholder=PLACEHOLDER_HINT,
        )
        self.inline_field = discord.ui.TextInput(
            label="Inline? (yes/no)",
            required=False,
            max_length=5,
            default="no",
        )
        self.add_item(self.name_field)
        self.add_item(self.value_field)
        self.add_item(self.inline_field)

    async def on_submit(self, interaction):
        try:
            fields = self.panel.config["embed"].setdefault("fields", [])
            if len(fields) >= 25:
                return await interaction.response.send_message(
                    "An embed can have at most 25 fields.", ephemeral=True
                )
            inline = self.inline_field.value.strip().lower() in (
                "yes",
                "y",
                "true",
                "1",
                "on",
            )
            fields.append(
                {
                    "name": self.name_field.value.strip(),
                    "value": self.value_field.value.strip(),
                    "inline": inline,
                }
            )
            await self._save_and_refresh(interaction)
        except Exception:
            log.exception("Twitch add-field modal failed")
            await self._fail(interaction)


# ----------------------------------------------------------------------
# Panel components
# ----------------------------------------------------------------------
class TwitchChannelSelect(discord.ui.ChannelSelect):
    """Pick the channel live alerts are posted to."""

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
            placeholder="Select the alert channel...",
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
            log.exception("Twitch channel select failed")
            await self.panel._error(interaction)


class TwitchRoleSelect(discord.ui.RoleSelect):
    """Pick the role assigned to a member while they are live (optional)."""

    def __init__(self, panel):
        self.panel = panel
        defaults = []
        rid = panel.config.get("role_id")
        if rid:
            role = panel.guild.get_role(rid)
            if role is not None:
                defaults = [role]
        super().__init__(
            placeholder="Select the Live role (optional)...",
            min_values=0,
            max_values=1,
            default_values=defaults,
            row=1,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["role_id"] = (
                self.values[0].id if self.values else None
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch role select failed")
            await self.panel._error(interaction)


class EditSelect(discord.ui.Select):
    """Choose which part to edit; the options adapt to the current style."""

    def __init__(self, panel):
        self.panel = panel
        if panel.config.get("style") == "text":
            source = TEXT_EDIT_OPTIONS
            placeholder = "Edit the alert message..."
        else:
            source = EMBED_EDIT_OPTIONS
            placeholder = "Edit the alert embed..."
        options = [
            discord.SelectOption(label=label, value=value, emoji=emoji)
            for value, label, emoji in source
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction):
        try:
            choice = self.values[0]
            if choice == "message":
                return await interaction.response.send_modal(
                    MessageModal(self.panel)
                )
            modals = {
                "title": TitleModal,
                "description": DescriptionModal,
                "color": ColorModal,
                "author": AuthorModal,
                "footer": FooterModal,
                "addfield": AddFieldModal,
            }
            if choice in modals:
                return await interaction.response.send_modal(
                    modals[choice](self.panel)
                )
            if choice == "thumbnail":
                return await interaction.response.send_modal(
                    AssetModal(self.panel, "thumbnail", "Thumbnail")
                )
            if choice == "image":
                return await interaction.response.send_modal(
                    AssetModal(self.panel, "image", "Image")
                )
            if choice == "clearfields":
                self.panel.config["embed"]["fields"] = []
                await self.panel.cog.save(
                    self.panel.guild.id, self.panel.config
                )
                await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch edit select failed")
            await self.panel._error(interaction)


class _StyleButton(discord.ui.Button):
    """Switch the notification between an embed and a classic message."""

    def __init__(self, panel):
        self.panel = panel
        style = panel.config.get("style", "embed")
        label = "Style: Embed" if style == "embed" else "Style: Classic"
        super().__init__(
            label=label, style=discord.ButtonStyle.secondary, row=3
        )

    async def callback(self, interaction):
        try:
            current = self.panel.config.get("style", "embed")
            self.panel.config["style"] = (
                "text" if current == "embed" else "embed"
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch style button failed")
            await self.panel._error(interaction)


class _PlaceholdersButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label="Placeholders",
            style=discord.ButtonStyle.secondary,
            row=3,
        )

    async def callback(self, interaction):
        try:
            embed = discord.Embed(
                title="Twitch alert placeholders",
                description=(
                    "Drop any of these into your message, or into the embed's "
                    "title, description, fields, author, or footer. They are "
                    "filled in automatically the moment a watched member goes "
                    "live."
                ),
                colour=TWITCH_PURPLE,
            )
            embed.add_field(
                name="{streamer}",
                value="The streamer's display name.",
                inline=False,
            )
            embed.add_field(
                name="{mention}",
                value="Pings the streamer, e.g. @name.",
                inline=False,
            )
            embed.add_field(
                name="{url}",
                value="A clickable link to the Twitch stream.",
                inline=False,
            )
            embed.add_field(
                name="{game}",
                value="What they are playing (may be blank).",
                inline=False,
            )
            embed.add_field(
                name="{title}",
                value="The stream's title.",
                inline=False,
            )
            embed.add_field(
                name="{server}",
                value="Your server's name.",
                inline=False,
            )
            embed.add_field(
                name="{avatar}",
                value=(
                    "The streamer's avatar URL. Perfect for the Thumbnail or "
                    "Image field."
                ),
                inline=False,
            )
            embed.add_field(
                name="Example",
                value="`{mention} is now live playing {game}! Watch: {url}`",
                inline=False,
            )
            embed.set_footer(
                text="Tip: pop {avatar} into Thumbnail for a clean look."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Twitch placeholders button failed")
            await self.panel._error(interaction)


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label="Preview", style=discord.ButtonStyle.primary, row=3
        )

    async def callback(self, interaction):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.panel.cog.send_preview(interaction, self.panel.config)
        except Exception:
            log.exception("Twitch preview failed")
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
            row=4,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["enabled"] = not bool(
                self.panel.config.get("enabled")
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch enable button failed")
            await self.panel._error(interaction)


# ----------------------------------------------------------------------
# Main control panel
# ----------------------------------------------------------------------
class TwitchPanel(discord.ui.View):
    """Author-restricted Twitch live-alert builder (the single entry point)."""

    def __init__(self, cog, guild, author_id, config, timeout=180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.config = config
        self.message = None

        self.add_item(TwitchChannelSelect(self))
        self.add_item(TwitchRoleSelect(self))
        self.add_item(EditSelect(self))
        self.add_item(_StyleButton(self))
        self.add_item(_PlaceholdersButton(self))
        self.add_item(_PreviewButton(self))
        self.add_item(_EnableButton(self))

    def build_embed(self):
        config = self.config
        embed_cfg = config.get("embed") or {}
        enabled = bool(config.get("enabled"))
        style = config.get("style", "embed")
        colour = embed_cfg.get("color")

        embed = discord.Embed(
            title="Twitch live alerts",
            description=(
                "Design the alert that fires when a watched member goes live "
                "on Twitch. Every change saves instantly - hit **Preview** to "
                "see it, and add streamers with `/twitch watch`."
            ),
            colour=colour if isinstance(colour, int) else TWITCH_PURPLE,
        )

        cid = config.get("channel_id")
        channel_value = f"<#{cid}>" if cid else "*Not set.*"
        rid = config.get("role_id")
        role_value = f"<@&{rid}>" if rid else "*None (legacy lookup).*"

        embed.add_field(
            name="Status",
            value="\U0001F7E2 Enabled" if enabled else "\U0001F534 Disabled",
            inline=True,
        )
        embed.add_field(name="Channel", value=channel_value, inline=True)
        embed.add_field(
            name="Style",
            value="Embed" if style == "embed" else "Classic message",
            inline=True,
        )
        embed.add_field(name="Live role", value=role_value, inline=False)

        if style == "text":
            text = config.get("text") or "*none*"
            if len(text) > 200:
                text = text[:197] + "..."
            embed.add_field(name="Message", value=text, inline=False)
        else:
            title = embed_cfg.get("title") or "*none*"
            desc = embed_cfg.get("description") or "*none*"
            if len(desc) > 120:
                desc = desc[:117] + "..."
            colour_text = (
                f"#{colour:06X}" if isinstance(colour, int) else "default"
            )
            lines = [
                f"**Title:** {title[:120]}",
                f"**Description:** {desc}",
                f"**Colour:** {colour_text}",
                f"**Fields:** {len(embed_cfg.get('fields') or [])}",
            ]
            author_name = (embed_cfg.get("author") or {}).get("name")
            if author_name:
                lines.append(f"**Author:** {author_name[:60]}")
            footer_text = (embed_cfg.get("footer") or {}).get("text")
            if footer_text:
                lines.append(f"**Footer:** {footer_text[:60]}")
            if embed_cfg.get("thumbnail"):
                lines.append("**Thumbnail:** set")
            if embed_cfg.get("image"):
                lines.append("**Image:** set")
            content_line = config.get("text")
            if content_line:
                preview = content_line
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                lines.append(f"**Content line:** {preview}")
            embed.add_field(name="Embed", value="\n".join(lines), inline=False)

        embed.set_footer(
            text=(
                "Only you can use these controls. "
                f"Placeholders: {PLACEHOLDER_HINT}"
            )
        )
        return embed

    async def _refresh(self, interaction):
        """Rebuild a fresh panel from current config and show it in place."""

        new = TwitchPanel(self.cog, self.guild, self.author_id, self.config)
        new.message = self.message
        self.stop()
        embed = new.build_embed()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=new)
                return
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=new)
            except discord.HTTPException:
                pass

    async def _error(self, interaction):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class Twitch(commands.Cog):
    """Announce when watched members go live on Twitch, with a Live role."""

    def __init__(self, bot):
        self.bot = bot

    # -- config storage (single JSONB blob per guild) -------------------
    async def get_config(self, guild_id):
        """Load the Twitch alert blob, merged over the defaults."""

        blob = await settings.get_guild(self.bot.db_pool, guild_id, "twitch", None)
        return _merge_defaults(blob)

    async def save(self, guild_id, config):
        await settings.set_guild(self.bot.db_pool, guild_id, "twitch", config)

    # -- placeholder + embed building -----------------------------------
    def _apply(self, text, member, activity=None):
        if not text:
            return text
        guild = member.guild if member else None
        replacements = {
            "{streamer}": member.display_name if member else "",
            "{mention}": member.mention if member else "",
            "{url}": (getattr(activity, "url", "") or "") if activity else "",
            "{game}": (getattr(activity, "game", "") or "") if activity else "",
            "{title}": (getattr(activity, "name", "") or "") if activity else "",
            "{server}": guild.name if guild else "",
            "{avatar}": member.display_avatar.url if member else "",
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    def _resolve_asset(self, value, member):
        if not value:
            return None
        avatar = member.display_avatar.url if member else ""
        value = value.replace("{avatar}", avatar).strip()
        return value or None

    def _build_embed(self, config, member, activity=None):
        embed_cfg = config.get("embed") or {}
        colour = embed_cfg.get("color")
        embed = discord.Embed(
            colour=colour if isinstance(colour, int) else None
        )

        title = self._apply(embed_cfg.get("title"), member, activity)
        if title:
            embed.title = title[:256]
        description = self._apply(embed_cfg.get("description"), member, activity)
        if description:
            embed.description = description[:4096]

        author = embed_cfg.get("author") or {}
        author_name = self._apply(author.get("name"), member, activity)
        if author_name:
            icon = self._resolve_asset(author.get("icon"), member)
            embed.set_author(
                name=author_name[:256],
                icon_url=icon if _is_url(icon) else None,
            )

        footer = embed_cfg.get("footer") or {}
        footer_text = self._apply(footer.get("text"), member, activity)
        if footer_text:
            icon = self._resolve_asset(footer.get("icon"), member)
            embed.set_footer(
                text=footer_text[:2048],
                icon_url=icon if _is_url(icon) else None,
            )

        thumbnail = self._resolve_asset(embed_cfg.get("thumbnail"), member)
        if _is_url(thumbnail):
            embed.set_thumbnail(url=thumbnail)
        image = self._resolve_asset(embed_cfg.get("image"), member)
        if _is_url(image):
            embed.set_image(url=image)

        for field in (embed_cfg.get("fields") or [])[:25]:
            name = self._apply(field.get("name"), member, activity) or "​"
            value = self._apply(field.get("value"), member, activity) or "​"
            embed.add_field(
                name=name[:256],
                value=value[:1024],
                inline=bool(field.get("inline")),
            )
        return embed

    def _compose(self, config, member, activity=None):
        """Build (content, embed) exactly as a real go-live would render."""

        style = config.get("style", "embed")
        text = self._apply(config.get("text"), member, activity)
        if style == "text":
            return text or None, None

        embed = self._build_embed(config, member, activity)
        if not (
            embed.title
            or embed.description
            or embed.fields
            or embed.image.url
            or embed.thumbnail.url
            or embed.author.name
            or embed.footer.text
        ):
            embed.description = self._apply(
                "{mention} is now live! {url}", member, activity
            )
        return (text or None), embed

    async def send_preview(self, interaction, config):
        """Render the alert as a real go-live would, shown to the admin."""

        member = interaction.user
        activity = types.SimpleNamespace(
            url="https://twitch.tv/yourchannel",
            game="Just Chatting",
            name="Live now: come hang out!",
            platform="Twitch",
        )
        content, embed = self._compose(config, member, activity)
        kwargs = {"ephemeral": True}
        if content:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if not content and embed is None:
            kwargs["content"] = (
                "Nothing to preview yet - add a message or some embed content."
            )
        await interaction.followup.send(**kwargs)

    # -- live role helpers ----------------------------------------------
    def _resolve_role(self, guild, config):
        """The configured Live role, falling back to the legacy name."""

        rid = config.get("role_id")
        if rid:
            return guild.get_role(rid)
        return discord.utils.get(guild.roles, name=LEGACY_ROLE_NAME)

    async def _assign_role(self, member, config):
        try:
            role = self._resolve_role(member.guild, config)
            if role is not None and role not in member.roles:
                await member.add_roles(role, reason="Twitch live alert")
        except Exception:
            log.exception("Twitch role assign failed")

    async def _remove_role(self, member, config):
        try:
            role = self._resolve_role(member.guild, config)
            if role is not None and role in member.roles:
                await member.remove_roles(role, reason="Twitch live ended")
        except Exception:
            log.exception("Twitch role removal failed")

    # -- streaming listener ---------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ):
        try:
            was_live = any(
                isinstance(a, discord.Streaming) for a in before.activities
            )
            now_activity = next(
                (a for a in after.activities if isinstance(a, discord.Streaming)),
                None,
            )

            if now_activity is not None and not was_live:
                await self._on_go_live(after, now_activity)
            elif now_activity is None and was_live:
                config = await self.get_config(after.guild.id)
                await self._remove_role(after, config)
        except Exception:
            log.exception("Twitch on_member_update failed")

    async def _on_go_live(self, member, activity):
        """Post the alert and assign the Live role when a watched member goes live."""

        guild = member.guild
        try:
            row = await self.bot.db_pool.fetchrow(
                "SELECT channel_id FROM twitch_alert "
                "WHERE guild_id = $1 AND user_id = $2;",
                guild.id,
                member.id,
            )
        except Exception:
            log.exception("Twitch watchlist lookup failed")
            return
        if row is None:
            return

        config = await self.get_config(guild.id)
        if not config.get("enabled"):
            return

        # Per-member override (0 == no override) else the guild channel.
        channel_id = row["channel_id"] or config.get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            try:
                content, embed = self._compose(config, member, activity)
                kwargs = {}
                if content:
                    kwargs["content"] = content
                if embed is not None:
                    kwargs["embed"] = embed
                if kwargs:
                    await channel.send(**kwargs)
            except Exception:
                log.exception("Twitch alert send failed")

        await self._assign_role(member, config)

    # -- commands -------------------------------------------------------
    @commands.hybrid_group(name="twitch", aliases=["stream"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch(self, ctx: commands.Context):
        """Open the Twitch live-alert builder."""

        if ctx.invoked_subcommand is not None:
            return

        config = await self.get_config(ctx.guild.id)
        view = TwitchPanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @twitch.command(name="watch", aliases=["add"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch_watch(
        self,
        ctx: commands.Context,
        member: discord.Member,
        channel: typing.Optional[discord.TextChannel] = None,
    ):
        """Add a member to the live-alert watchlist (channel is an optional override)."""

        override = channel.id if channel else 0
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM twitch_alert WHERE guild_id = $1 AND user_id = $2;",
                ctx.guild.id,
                member.id,
            )
            await self.bot.db_pool.execute(
                "INSERT INTO twitch_alert(guild_id, user_id, channel_id, message) "
                "VALUES($1, $2, $3, NULL);",
                ctx.guild.id,
                member.id,
                override,
            )
        except Exception:
            log.exception("Twitch watch failed")
            return await ctx.send("Could not add that member to the watchlist.")

        where = channel.mention if channel else "the configured alert channel"
        embed = discord.Embed(title="Twitch watchlist", colour=random_colour())
        embed.add_field(name="Now watching", value=member.mention, inline=True)
        embed.add_field(name="Alerts in", value=where, inline=True)
        embed.set_footer(text="Use /twitch to open the builder.")
        await ctx.send(embed=embed)

    @twitch.command(name="unwatch", aliases=["remove", "del"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch_unwatch(
        self, ctx: commands.Context, member: discord.Member
    ):
        """Remove a member from the live-alert watchlist."""

        try:
            await self.bot.db_pool.execute(
                "DELETE FROM twitch_alert WHERE guild_id = $1 AND user_id = $2;",
                ctx.guild.id,
                member.id,
            )
        except Exception:
            log.exception("Twitch unwatch failed")
            return await ctx.send("Could not remove that member.")

        embed = discord.Embed(title="Twitch watchlist", colour=random_colour())
        embed.add_field(name="Removed", value=member.mention, inline=False)
        await ctx.send(embed=embed)

    @twitch.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch_list(self, ctx: commands.Context):
        """Show every member on the live-alert watchlist."""

        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT user_id, channel_id FROM twitch_alert "
                "WHERE guild_id = $1 ORDER BY user_id;",
                ctx.guild.id,
            )
        except Exception:
            log.exception("Twitch list failed")
            return await ctx.send("Could not load the watchlist.")

        lines = []
        for row in rows:
            cid = row["channel_id"]
            target = f"<#{cid}>" if cid else "default alert channel"
            lines.append(f"<@{row['user_id']}> -> {target}")

        embeds = paginate_lines(
            lines, title="Twitch watchlist", colour=TWITCH_PURPLE, per_page=10
        )
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    @twitch.command(name="createrole", aliases=["setup-role"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def twitch_createrole(self, ctx: commands.Context):
        """Create a Live streamer role and link it to the alert config."""

        existing = discord.utils.get(ctx.guild.roles, name=LEGACY_ROLE_NAME)
        if existing is not None:
            config = await self.get_config(ctx.guild.id)
            if not config.get("role_id"):
                config["role_id"] = existing.id
                await self.save(ctx.guild.id, config)
            return await ctx.send(
                "Your guild already has a Live streamer role - it is now linked."
            )

        try:
            role = await ctx.guild.create_role(
                name=LEGACY_ROLE_NAME, hoist=True, reason="Twitch live role"
            )
        except discord.HTTPException as e:
            return await ctx.send(
                f"Could not create the Live streamer role.\n\n{e}"
            )

        config = await self.get_config(ctx.guild.id)
        config["role_id"] = role.id
        await self.save(ctx.guild.id, config)
        await ctx.send(
            "Live streamer role created and linked. Move it to your preferred "
            "position in the role list."
        )

    @twitch.command(name="removerole", aliases=["delete-role", "del-role"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def twitch_removerole(self, ctx: commands.Context):
        """Delete the Live streamer role and unlink it from the config."""

        config = await self.get_config(ctx.guild.id)
        role = self._resolve_role(ctx.guild, config)
        if role is None:
            return await ctx.send("No Live streamer role is set up.")

        try:
            await role.delete(reason="Twitch live role removed")
        except discord.HTTPException as e:
            return await ctx.send(
                f"Could not delete the Live streamer role.\n\n{e}"
            )

        if config.get("role_id"):
            config["role_id"] = None
            await self.save(ctx.guild.id, config)
        await ctx.send("Live streamer role removed.")


async def setup(bot):
    await bot.add_cog(Twitch(bot))
