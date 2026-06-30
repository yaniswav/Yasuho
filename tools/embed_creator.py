"""Reusable embed-creator toolkit for cogs that build configurable embeds.

This module is the shared spine for cogs that build configurable embeds - the
button-role, welcome, and Twitch builders all consume it. It owns ONLY the
"embed" sub-blob of a cog's config: title/description/colour/author/footer/thumbnail/
image/fields. The cog keeps its own top-level keys (channel_id, enabled,
role_id, style, ...) and hands the toolkit ``config["embed"]`` by reference.

Design notes:
- ``render(config, substitute=None)`` is the SINGLE rendering path for both the
  live send and any preview, so the two can never drift. It is placeholder
  agnostic: the cog passes a ``substitute`` callable that resolves whatever
  tokens it supports ({user}, {avatar}, {streamer}, ...).
- The edit components (the EditSelect + the seven modals) drop straight into the
  cog's own ``discord.ui.View``. The View just has to satisfy the
  ``EmbedEditorHost`` protocol: expose ``embed_config`` and an async
  ``on_embed_changed``. The cog keeps its own interaction_check, on_timeout, and
  status embed.
- Everything pure (render / parse_colour / merge_embed / summarise / hint_line /
  placeholder_guide / embed_has_content) is independently unit-testable with no
  live Discord objects.

Self-contained: depends only on ``discord``, the stdlib, and ``tools.formats``.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import logging
from typing import Callable, Optional, Protocol

import discord

from tools import i18n
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Discord per-part hard limits, centralised so every cog caps each embed part the
# same way. (Discord also rejects an embed whose summed text exceeds 6000 chars;
# cogs that build very large embeds should keep that aggregate budget in mind.)
LIMIT_TITLE = 256
LIMIT_DESC = 4096
LIMIT_AUTHOR = 256
LIMIT_FOOTER = 2048
LIMIT_FIELD_NAME = 256
LIMIT_FIELD_VALUE = 1024
LIMIT_FIELDS = 25

# Discord rejects truly empty field parts; this zero-width space stands in.
_ZERO_WIDTH = "​"

# Common colour names accepted by the colour modal (alongside #rrggbb). This is
# the shared copy a cog imports instead of carrying its own.
COLOUR_NAMES = {
    "blurple": 0x5865F2,
    "green": 0x2ECC71,
    "red": 0xE74C3C,
    "blue": 0x3498DB,
    "yellow": 0xF1C40F,
    "gold": 0xF1C40F,
    "orange": 0xE67E22,
    "purple": 0x9B59B6,
    "pink": 0xE91E63,
    "magenta": 0xE91E63,
    "teal": 0x1ABC9C,
    "cyan": 0x1ABC9C,
    "white": 0xFFFFFF,
    "black": 0x000000,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
}

# Default edit-dropdown order: (value, label, emoji). Cogs may pass a subset or a
# reordering to make_edit_select (twitch varies it by style).
DEFAULT_EDIT_OPTIONS = [
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


# ----------------------------------------------------------------------
# Pure helpers (no live Discord objects required)
# ----------------------------------------------------------------------
def default_embed() -> dict:
    """A fresh canonical embed-config dict with no shared nested references."""

    return {
        "title": "",
        "description": "",
        "color": None,
        "author": {"name": "", "icon": ""},
        "footer": {"text": "", "icon": ""},
        "thumbnail": "",
        "image": "",
        "fields": [],
    }


def merge_embed(blob) -> dict:
    """Merge a raw stored embed dict over default_embed().

    Every nested container is rebuilt so the result never aliases the settings
    cache; the panel can mutate it freely and persist with one set_guild call.
    This replaces the embed half of each cog's old _merge_defaults.
    """

    config = default_embed()
    if not isinstance(blob, dict):
        return config

    for key in ("title", "description", "color", "thumbnail", "image"):
        if key in blob:
            config[key] = blob[key]
    config["author"] = {
        "name": (blob.get("author") or {}).get("name", ""),
        "icon": (blob.get("author") or {}).get("icon", ""),
    }
    config["footer"] = {
        "text": (blob.get("footer") or {}).get("text", ""),
        "icon": (blob.get("footer") or {}).get("icon", ""),
    }
    config["fields"] = [
        {
            "name": f.get("name", ""),
            "value": f.get("value", ""),
            "inline": bool(f.get("inline")),
        }
        for f in (blob.get("fields") or [])
        if isinstance(f, dict)
    ]
    return config


def parse_colour(text, names=None) -> Optional[int]:
    """Parse '#rrggbb', 'rrggbb', a colour name, or 'random'.

    ``names`` is the name->int palette to accept (defaults to COLOUR_NAMES). A
    cog can pass its own palette so its colour vocabulary stays its own concern
    instead of mutating the shared global. Returns an int in 0..0xFFFFFF, a fresh
    random_colour() for 'random', or None when empty or unrecognised.
    """

    if not text:
        return None
    palette = names if names is not None else COLOUR_NAMES
    text = text.strip().lower()
    if text == "random":
        return random_colour()
    if text in palette:
        return palette[text]
    text = text.lstrip("#")
    try:
        value = int(text, 16)
    except ValueError:
        return None
    if 0 <= value <= 0xFFFFFF:
        return value
    return None


def is_url(value) -> bool:
    """Scheme-only http(s) check used by render to drop broken/empty assets."""

    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


def _substitute(substitute, text) -> str:
    """Apply the cog's token resolver to one string (identity if not callable)."""

    if text is None:
        return ""
    text = str(text)
    if text and callable(substitute):
        return substitute(text)
    return text


def render(
    config: dict, substitute: Optional[Callable[[str], str]] = None
) -> discord.Embed:
    """Build a discord.Embed from an embed-config dict (the single render path).

    ``substitute`` is applied to every string including asset URLs, so a token
    like {avatar} in the thumbnail resolves through the same path and is then
    validated by is_url(). Empty author/footer/thumbnail/image drop out cleanly,
    every part is capped to its Discord limit, and fields are capped at 25.

    render() never raises on an all-empty config: it returns a bare coloured
    embed. Empty-embed policy (a default greeting, gating a preview) is left to
    the cog via embed_has_content().
    """

    config = config or {}
    color = config.get("color")
    embed = discord.Embed(colour=color if isinstance(color, int) else None)

    title = _substitute(substitute, config.get("title"))
    if title:
        embed.title = title[:LIMIT_TITLE]
    description = _substitute(substitute, config.get("description"))
    if description:
        embed.description = description[:LIMIT_DESC]

    author = config.get("author") or {}
    author_name = _substitute(substitute, author.get("name"))
    if author_name:
        icon = _substitute(substitute, author.get("icon"))
        embed.set_author(
            name=author_name[:LIMIT_AUTHOR],
            icon_url=icon if is_url(icon) else None,
        )

    footer = config.get("footer") or {}
    footer_text = _substitute(substitute, footer.get("text"))
    if footer_text:
        icon = _substitute(substitute, footer.get("icon"))
        embed.set_footer(
            text=footer_text[:LIMIT_FOOTER],
            icon_url=icon if is_url(icon) else None,
        )

    thumbnail = _substitute(substitute, config.get("thumbnail"))
    if is_url(thumbnail):
        embed.set_thumbnail(url=thumbnail)
    image = _substitute(substitute, config.get("image"))
    if is_url(image):
        embed.set_image(url=image)

    for field in (config.get("fields") or [])[:LIMIT_FIELDS]:
        if not isinstance(field, dict):
            continue
        name = _substitute(substitute, field.get("name")) or _ZERO_WIDTH
        value = _substitute(substitute, field.get("value")) or _ZERO_WIDTH
        embed.add_field(
            name=name[:LIMIT_FIELD_NAME],
            value=value[:LIMIT_FIELD_VALUE],
            inline=bool(field.get("inline")),
        )
    return embed


def embed_has_content(embed: discord.Embed) -> bool:
    """True if the embed has any visible content.

    Lets a cog apply its own empty-embed fallback or gate a Preview button.
    """

    return bool(
        embed.title
        or embed.description
        or embed.fields
        or embed.image.url
        or embed.thumbnail.url
        or embed.author.name
        or embed.footer.text
    )


def summarise(config: dict, *, empty: str = "*none*") -> str:
    """Compact multi-line summary for a cog panel's 'Embed' status field."""

    config = config or {}
    colour = config.get("color")
    title = config.get("title") or empty
    desc = config.get("description") or empty
    if len(desc) > 120:
        desc = desc[:117] + "..."
    colour_text = f"#{colour:06X}" if isinstance(colour, int) else _("default")
    lines = [
        _("**Title:** {title}").format(title=title[:120]),
        _("**Description:** {description}").format(description=desc),
        _("**Colour:** {colour}").format(colour=colour_text),
        _("**Fields:** {count}").format(count=len(config.get('fields') or [])),
    ]
    author_name = (config.get("author") or {}).get("name")
    if author_name:
        lines.append(_("**Author:** {name}").format(name=author_name[:60]))
    footer_text = (config.get("footer") or {}).get("text")
    if footer_text:
        lines.append(_("**Footer:** {text}").format(text=footer_text[:60]))
    if config.get("thumbnail"):
        lines.append(_("**Thumbnail:** set"))
    if config.get("image"):
        lines.append(_("**Image:** set"))
    return "\n".join(lines)


def hint_line(entries) -> str:
    """Flatten [(name, desc), ...] to '{user} {server} ...' for modal hints."""

    return " ".join(name for name, _desc in entries)


def placeholder_guide(
    entries,
    *,
    title: str = "Placeholders",
    intro: Optional[str] = None,
    colour: Optional[int] = None,
) -> discord.Embed:
    """Build a guide embed from (name, description) pairs.

    Lines are formatted as "`{name}` - desc" and packed into successive fields
    whose value never exceeds 1024 chars, so an arbitrarily long token list can
    never trigger a 400. Colour defaults to random_colour() when not an int. The
    caller decides ephemeral=True when sending.
    """

    embed = discord.Embed(
        title=title,
        colour=colour if isinstance(colour, int) else random_colour(),
    )
    if intro:
        embed.description = intro[:LIMIT_DESC]

    chunks = []
    current = []
    length = 0
    for name, desc in entries:
        line = f"`{name}` - {desc}"[:LIMIT_FIELD_VALUE]
        addition = len(line) + (1 if current else 0)
        if current and length + addition > LIMIT_FIELD_VALUE:
            chunks.append("\n".join(current))
            current = [line]
            length = len(line)
        else:
            current.append(line)
            length += addition
    if current:
        chunks.append("\n".join(current))

    for index, value in enumerate(chunks[:LIMIT_FIELDS]):
        embed.add_field(
            name=_("Tokens") if index == 0 else _ZERO_WIDTH,
            value=value,
            inline=False,
        )
    return embed


async def refresh_in_place(interaction, message, *, embed, view) -> None:
    """Edit the panel in place, handling the response.is_done() fork.

    Factored out of every panel's _refresh: try the live interaction edit first,
    fall back to editing the stored message if the interaction is already done.
    """

    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=view)
            return
    except discord.HTTPException:
        pass
    if message is not None:
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass


async def notify_failure(interaction, message: str = "Something went wrong.") -> None:
    """Best-effort ephemeral error reply that respects the response state."""

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ----------------------------------------------------------------------
# Host contract
# ----------------------------------------------------------------------
class EmbedEditorHost(Protocol):
    """The duck-typed contract a cog's own View must satisfy.

    This documents and type-checks the integration; it is NOT an enforcing ABC,
    so there is no runtime ceremony. A host exposes a STABLE ``embed_config``
    reference (reused across panel rebuilds, never deep-copied per refresh) and
    an async ``on_embed_changed`` that persists the blob and refreshes in place.
    ``placeholder_hint`` and ``asset_hint`` are optional; modals read them with
    getattr and fall back to sensible defaults.
    """

    embed_config: dict
    placeholder_hint: str = ""
    asset_hint: str = "https://..."

    async def on_embed_changed(self, interaction: discord.Interaction) -> None:
        ...


# ----------------------------------------------------------------------
# Modals (one per editable embed part), public and independently reusable
# ----------------------------------------------------------------------
class _EmbedModal(discord.ui.Modal):
    """Base modal: mutates host.embed_config then fires host.on_embed_changed."""

    def __init__(self, host, title):
        super().__init__(title=title)
        self.host = host

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return True

    @property
    def embed_config(self) -> dict:
        return self.host.embed_config

    def _placeholder_hint(self):
        return getattr(self.host, "placeholder_hint", "") or None

    def _asset_hint(self):
        return getattr(self.host, "asset_hint", None) or "https://..."

    async def _commit(self, interaction):
        await self.host.on_embed_changed(interaction)

    async def _fail(self, interaction):
        await notify_failure(interaction)


class TitleModal(_EmbedModal):
    """Edit the embed title."""

    def __init__(self, host):
        super().__init__(host, _("Edit title"))
        self.field = discord.ui.TextInput(
            label=_("Title"),
            style=discord.TextStyle.short,
            required=False,
            max_length=LIMIT_TITLE,
            default=self.embed_config.get("title") or None,
            placeholder=self._placeholder_hint(),
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.embed_config["title"] = self.field.value.strip()
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator title modal failed")
            await self._fail(interaction)


class DescriptionModal(_EmbedModal):
    """Edit the embed description."""

    def __init__(self, host):
        super().__init__(host, _("Edit description"))
        self.field = discord.ui.TextInput(
            label=_("Description"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
            default=self.embed_config.get("description") or None,
            placeholder=self._placeholder_hint(),
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.embed_config["description"] = self.field.value.strip()
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator description modal failed")
            await self._fail(interaction)


class ColourModal(_EmbedModal):
    """Edit the embed colour: a quick-pick radio, or a typed hex / name.

    A selected radio option wins; otherwise the text box is parsed (through the
    host palette, so a cog override like Twitch's purple still applies). The box
    defaults to the current colour, so submitting unchanged keeps it; clearing
    the box and picking nothing clears the colour.
    """

    # Curated quick-pick names, resolved through the host palette on submit.
    _QUICK = (
        "blurple", "red", "green", "blue", "yellow",
        "orange", "purple", "pink", "white", "black",
    )

    def __init__(self, host):
        super().__init__(host, _("Edit colour"))
        current = self.embed_config.get("color")

        self.radio = discord.ui.RadioGroup(required=False)
        for name in self._QUICK:
            self.radio.add_option(label=name.capitalize(), value=name)
        self.add_item(
            discord.ui.Label(
                text=_("Quick colour (optional)"),
                component=self.radio,
                description=_("Pick one, or edit the box below. Empty both to clear."),
            )
        )

        self.field = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=False,
            max_length=20,
            default=(f"#{current:06X}" if isinstance(current, int) else None),
            placeholder="#5865F2, blurple, random...",
        )
        self.add_item(
            discord.ui.Label(text=_("Custom colour (hex or name)"), component=self.field)
        )

    async def on_submit(self, interaction):
        try:
            names = getattr(self.host, "colour_names", None)
            raw = (self.field.value or "").strip()
            chosen = self.radio.value
            if chosen:
                self.embed_config["color"] = parse_colour(chosen, names)
            elif raw:
                parsed = parse_colour(raw, names)
                if parsed is None:
                    await interaction.response.send_message(
                        _(
                            "That colour wasn't recognised. Use #rrggbb or a name "
                            "like 'blurple'."
                        ),
                        ephemeral=True,
                    )
                    return
                self.embed_config["color"] = parsed
            else:
                self.embed_config["color"] = None
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator colour modal failed")
            await self._fail(interaction)


class AuthorModal(_EmbedModal):
    """Edit the embed author (name + icon URL)."""

    def __init__(self, host):
        super().__init__(host, _("Edit author"))
        author = self.embed_config.get("author") or {}
        self.name_field = discord.ui.TextInput(
            label=_("Author name"),
            required=False,
            max_length=LIMIT_AUTHOR,
            default=author.get("name") or None,
            placeholder=self._placeholder_hint(),
        )
        self.icon_field = discord.ui.TextInput(
            label=_("Author icon URL"),
            required=False,
            max_length=1024,
            default=author.get("icon") or None,
            placeholder=self._asset_hint(),
        )
        self.add_item(self.name_field)
        self.add_item(self.icon_field)

    async def on_submit(self, interaction):
        try:
            self.embed_config["author"] = {
                "name": self.name_field.value.strip(),
                "icon": self.icon_field.value.strip(),
            }
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator author modal failed")
            await self._fail(interaction)


class FooterModal(_EmbedModal):
    """Edit the embed footer (text + icon URL)."""

    def __init__(self, host):
        super().__init__(host, _("Edit footer"))
        footer = self.embed_config.get("footer") or {}
        self.text_field = discord.ui.TextInput(
            label=_("Footer text"),
            required=False,
            max_length=LIMIT_FOOTER,
            default=footer.get("text") or None,
            placeholder=self._placeholder_hint(),
        )
        self.icon_field = discord.ui.TextInput(
            label=_("Footer icon URL"),
            required=False,
            max_length=1024,
            default=footer.get("icon") or None,
            placeholder=self._asset_hint(),
        )
        self.add_item(self.text_field)
        self.add_item(self.icon_field)

    async def on_submit(self, interaction):
        try:
            self.embed_config["footer"] = {
                "text": self.text_field.value.strip(),
                "icon": self.icon_field.value.strip(),
            }
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator footer modal failed")
            await self._fail(interaction)


class AssetModal(_EmbedModal):
    """Edit a single image URL field (key is 'thumbnail' or 'image')."""

    def __init__(self, host, key, label):
        super().__init__(host, _("Edit {label}").format(label=label.lower()))
        self.key = key
        self.field = discord.ui.TextInput(
            label=_("{label} URL").format(label=label),
            required=False,
            max_length=1024,
            default=self.embed_config.get(key) or None,
            placeholder=self._asset_hint(),
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.embed_config[self.key] = self.field.value.strip()
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator asset modal failed")
            await self._fail(interaction)


class AddFieldModal(_EmbedModal):
    """Append a field (name/value/inline), enforcing the 25-field cap."""

    def __init__(self, host):
        super().__init__(host, _("Add a field"))
        self.name_field = discord.ui.TextInput(
            label=_("Field name"),
            required=True,
            max_length=LIMIT_FIELD_NAME,
            placeholder=self._placeholder_hint(),
        )
        self.value_field = discord.ui.TextInput(
            label=_("Field value"),
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=LIMIT_FIELD_VALUE,
            placeholder=self._placeholder_hint(),
        )
        self.inline_field = discord.ui.TextInput(
            label=_("Inline? (yes/no)"),
            required=False,
            max_length=5,
            default="no",
        )
        self.add_item(self.name_field)
        self.add_item(self.value_field)
        self.add_item(self.inline_field)

    async def on_submit(self, interaction):
        try:
            fields = self.embed_config.setdefault("fields", [])
            if len(fields) >= LIMIT_FIELDS:
                await interaction.response.send_message(
                    _("An embed can have at most {limit} fields.").format(
                        limit=LIMIT_FIELDS
                    ),
                    ephemeral=True,
                )
                return
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
            await self._commit(interaction)
        except Exception:
            log.exception("embed_creator add-field modal failed")
            await self._fail(interaction)


# Maps an edit-dropdown value to the modal that edits it. thumbnail/image are
# special-cased in the select callback because AssetModal needs (key, label).
_MODAL_FACTORIES = {
    "title": TitleModal,
    "description": DescriptionModal,
    "color": ColourModal,
    "author": AuthorModal,
    "footer": FooterModal,
    "addfield": AddFieldModal,
}


# ----------------------------------------------------------------------
# Edit dropdown
# ----------------------------------------------------------------------
class _EditSelect(discord.ui.Select):
    """Choose which embed part to edit; opens the matching modal or clears."""

    def __init__(self, host, *, options=None, placeholder="Edit the embed...", row=None):
        self.host = host
        specs = options if options is not None else DEFAULT_EDIT_OPTIONS
        select_options = [
            discord.SelectOption(label=label, value=value, emoji=emoji)
            for value, label, emoji in specs
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=select_options,
            row=row,
        )

    async def callback(self, interaction):
        try:
            choice = self.values[0]
            factory = _MODAL_FACTORIES.get(choice)
            if factory is not None:
                await interaction.response.send_modal(factory(self.host))
                return
            if choice == "thumbnail":
                await interaction.response.send_modal(
                    AssetModal(self.host, "thumbnail", "Thumbnail")
                )
                return
            if choice == "image":
                await interaction.response.send_modal(
                    AssetModal(self.host, "image", "Image")
                )
                return
            if choice == "clearfields":
                self.host.embed_config["fields"] = []
                await self.host.on_embed_changed(interaction)
                return
            log.warning("embed_creator edit select got unknown option %r", choice)
        except Exception:
            log.exception("embed_creator edit select failed")
            await notify_failure(interaction)


def make_edit_select(
    host, *, options=None, placeholder: str = "Edit the embed...", row=None
) -> discord.ui.Select:
    """Return an embed-edit dropdown bound to ``host``.

    ``options`` is an optional list of (value, label, emoji) tuples (defaults to
    DEFAULT_EDIT_OPTIONS); pass a subset or reordering to vary the menu by style.
    """

    return _EditSelect(host, options=options, placeholder=placeholder, row=row)


# ----------------------------------------------------------------------
# Optional placeholder-guide button drop-in
# ----------------------------------------------------------------------
class PlaceholderGuideButton(discord.ui.Button):
    """Optional drop-in that sends placeholder_guide(entries) ephemerally.

    Replaces a cog's bespoke "Placeholders" button with one line. Cogs that want
    custom styling can call placeholder_guide() directly from their own button.
    """

    def __init__(self, entries, *, label="Placeholders", row=None, colour=None):
        super().__init__(
            label=label, style=discord.ButtonStyle.secondary, row=row
        )
        self._entries = entries
        self._colour = colour

    async def callback(self, interaction):
        try:
            await interaction.response.send_message(
                embed=placeholder_guide(self._entries, colour=self._colour),
                ephemeral=True,
            )
        except Exception:
            log.exception("embed_creator placeholder guide failed")
            try:
                await interaction.response.send_message(
                    _("Could not open the guide."), ephemeral=True
                )
            except discord.HTTPException:
                pass
