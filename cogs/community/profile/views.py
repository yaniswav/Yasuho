"""Purpose: the PRESENTATION half of the social profile - the public read-only
card (``/profile view``) and the owner-only visibility panel (``/profile
panel``), plus the gaming-ID edit modal both surfaces use.

Mirrors the house split (music.py -> views.py, seasons.py -> seasons_views.py,
automod.py -> automod_panel.py): this module owns every Components V2 surface
and imports NOTHING from ``.cog`` - a view is handed the owning ``Profiles``
cog instance at construction time and calls back into its methods
(``cog.apply_field``, ``cog.visibility_note``, ...), never the other way
round. ``cog.py`` is the one that imports from here.

Two surfaces:

* :class:`ProfileCard` - the public card ``/profile view`` sends, rendered
  straight from :func:`visibility.resolve_visible_fields` (a plain
  ``LayoutView``, not author-gated - a profile is a card anyone who can already
  see it may look at, like :class:`~cogs.config.welcome.WelcomeStatusView`).
  Its connector-SECTION rows are data-driven: :func:`render_sections` walks
  the registry names that have no storage of their own (AniList, Steam, ...)
  and calls whatever P3/P4 registered for that name via
  :func:`register_section_renderer`, falling back to a sober "Linked" badge
  for the ones nobody has wired up yet.

  A connector section is shown only when BOTH halves are true: the viewer may
  see it (visibility) AND the owner really has a row in ``profile_connections``
  for it (linkage). A visibility line alone is a choice about an audience, not
  a statement that an account exists - and "Linked" is an assertion, so it is
  never printed over nothing. The card is handed the connection rows its
  caller already read; it opens no database connection of its own.
* :class:`ProfileVisibilityPanel` - the ``/profile panel`` admin twin of the
  text ``profile visibility`` command: one visibility select per section
  (STORED_NAMES and every connector name alike - :func:`storage.set_visibility`
  already accepts both) plus a button that reuses the existing
  :class:`ProfileEditModal` gaming-ID editor.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from . import registry, storage, visibility
from .visibility import ViewerContext, resolve_visible_fields, visible_field_names
from tools import interactions
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorLayoutView, AuthorView, LocaleModal

log = logging.getLogger(__name__)

# A profile's free-form fields (bio, custom values, gaming IDs) can contain
# anything the owner typed, including "@everyone" or a raw <@id> - and unlike
# an embed, Components V2 TextDisplay text DOES get parsed for mentions (see
# cogs/community/leveling/seasons_views.py's HallOfFameCard for the verified precedent).
# Every send/edit of a card or panel built here passes this.
NO_PINGS = discord.AllowedMentions.none()


def invalid_value_message(error):
    """Map a typed registry rejection to the message the user should read."""
    if error.reason == "too_long":
        return _("That value is too long (max {limit} characters).").format(
            limit=error.limit
        )
    if error.reason == "colour":
        return _("That is not a valid colour. Use a hex colour like #5865F2.")
    return _("That value is not valid for this field.")


def section_for(field):
    """The visibility SECTION a settable field belongs to.

    The five gamer IDs are keys inside one ``gaming_ids`` section, so publishing
    a Switch code publishes them all: the visibility choice is made once, for
    the section, not per key.
    """
    key = (field or "").strip().lower()
    return "gaming_ids" if key in registry.GAMING_ID_KEYS else key


def format_value(name, stored):
    """Render what was actually STORED, not what was typed.

    The registry trims text and packs a colour into an int, so echoing the raw
    input back would confirm something the profile does not contain.
    """
    if stored is None:
        return None
    if name == "accent":
        return "#%06X" % stored
    return str(stored)


# ---------------------------------------------------------------------------
# The gaming-ID edit modal (moved verbatim from cog.py - the "existing modal"
# both the prefix editor and the new panel button reuse).
# ---------------------------------------------------------------------------


class ProfileEditModal(LocaleModal):
    """Pick a gamer ID from a radio and type its value (Components V2 modal)."""

    def __init__(self, cog):
        super().__init__(title=_("Edit your profile"))
        self.cog = cog
        self.field = discord.ui.RadioGroup(required=True)
        for key in registry.GAMING_ID_KEYS:
            self.field.add_option(label=_(registry.GAMING_ID_LABELS[key]), value=key)
        self.add_item(discord.ui.Label(text=_("Field"), component=self.field))
        self.value_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=True,
            max_length=registry.GAMING_ID_MAX,
        )
        self.add_item(discord.ui.Label(text=_("Value"), component=self.value_input))

    async def on_submit(self, interaction):
        try:
            field = self.field.value
            value = (self.value_input.value or "").strip()
            if not field or not value:
                return await interaction.response.send_message(
                    _("Pick a field and enter a value."), ephemeral=True
                )
            try:
                applied = await self.cog.apply_field(interaction.user.id, field, value)
            except registry.InvalidValue as error:
                return await interaction.response.send_message(
                    invalid_value_message(error), ephemeral=True
                )
            if applied is None:
                return await interaction.response.send_message(
                    _("Unknown field."), ephemeral=True
                )
            label, shown = applied
            embed = discord.Embed(title=_("Profile updated"), colour=random_colour())
            embed.add_field(name=label, value=shown or value)
            # The modal is interaction-only, so the prefix that publishes the
            # section is always the slash one.
            note = await self.cog.visibility_note(
                interaction.user.id, section_for(field), "/"
            )
            if note:
                embed.set_footer(text=note)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Profile edit modal failed")
            await interaction.response.send_message(
                _("Failed to update your profile, please try again later."),
                ephemeral=True,
            )


class ProfileEditView(AuthorView):
    """One-button launcher for the profile edit modal (the prefix entry point)."""

    def __init__(self, cog, author_id):
        super().__init__(
            author_id, timeout=120, deny_message="This profile editor isn't for you."
        )
        self.cog = cog

    @discord.ui.button(
        label="Edit a field", emoji="\U0000270F", style=discord.ButtonStyle.primary
    )
    async def edit(self, interaction, button):
        try:
            await interaction.response.send_modal(ProfileEditModal(self.cog))
        except Exception:
            log.exception("Profile edit button failed")


# ---------------------------------------------------------------------------
# The public card: /profile view
# ---------------------------------------------------------------------------

# Components V2 caps a message at 4000 characters of combined TextDisplay text
# AND at 40 components, nested ones included (see cogs/system/help.py's
# CV2_TEXT_BUDGET and cogs/community/usersettings.py's COMPONENT_CAP). Going
# over either one is a 400 from Discord, i.e. no card at all. The header
# (display name + pronouns) and the truncation footer are small and fixed, so
# they are RESERVED outside the tracked budget rather than measured - the same
# margin discipline as help.py's CV2_CONTROL_RESERVE.
CARD_TEXT_BUDGET = 4000
CARD_COMPONENT_CAP = 40
_HEADER_RESERVE = 300
_FOOTER_RESERVE = 100
_CONTENT_BUDGET = CARD_TEXT_BUDGET - _HEADER_RESERVE - _FOOTER_RESERVE

# Components reserved for the fixed furniture: the Container itself, the header
# Section with its TextDisplay and Thumbnail accessory, and the truncation
# footer's Separator + TextDisplay. Everything else (content blocks AND the
# connector sections, whose renderers are written by OTHER lots) is charged
# against what is left.
_FIXED_COMPONENTS = 6
_CONTENT_COMPONENTS = CARD_COMPONENT_CAP - _FIXED_COMPONENTS

# A single free-form line (one gaming ID) kept to one sane on-screen line. Only
# the gaming IDs need this: GAMING_ID_MAX is 1000 chars (inherited verbatim
# from the legacy cog, see registry.py) and five of them would alone exceed
# the card's whole budget, where bio/custom-field caps are already an order of
# magnitude smaller.
_GAMING_ID_LINE_CLIP = 300


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _one_line(text):
    """Flatten a value that shares a line with its label.

    ``_clean_text`` in registry.py only strips the ENDS, so a custom-field
    label or a gaming ID may still carry interior newlines - and a newline
    inside a ``**label:** value`` line lets its author forge a convincing fake
    section header ("## Gaming IDs") inside somebody else's card. The bio keeps
    its newlines (see :func:`defuse_lines`): it is a block of its own, exactly
    like the embed description it replaces, so its SHAPE is free - only its
    line-leading markdown is not.
    """
    return " ".join(str(text).split())


# Markdown structure is only structure at the START of a line, so one
# zero-width space in front of it is enough to make it inert while leaving the
# text visually identical - no visible backslash, no character removed.
_ZERO_WIDTH = "\N{ZERO WIDTH SPACE}"

# The line-leading prefixes that MEAN something on this card: headings ('#',
# '##', '###'), subtext ('-#', used by the header and the truncation footer)
# and quotes ('>', '>>>'). Bold/italics are inline decoration, not structure,
# and are deliberately left alone.
_STRUCTURAL_PREFIXES = ("#", "-#", ">")


def defuse_lines(text):
    """Keep a multi-line block's newlines, neutralise its line-leading markdown.

    The bio is the one owner-typed value rendered as a block of its own, so
    unlike a label/value row it must NOT be flattened - people write bios on
    several lines on purpose. What it must not be able to do is grow a heading:
    once P4 draws verified connector data underneath, a bio line reading
    "## Gaming IDs" would sit above real sections and pass for one of them.
    Every offending line keeps its exact characters and simply stops being at
    the start of its line.

    PUBLIC on purpose (like :func:`register_section_renderer`): a section
    renderer written in another lot faces the identical hazard the moment it
    draws third-party text at the START of a line - a game name, a persona
    name, a title - and flattening (:func:`_one_line`) is not enough, because
    it removes the newline and leaves the "## " exactly where markdown wants
    it. One implementation, used by every writer of a card block.
    """
    lines = []
    for line in str(text).split("\n"):
        if line.lstrip().startswith(_STRUCTURAL_PREFIXES):
            line = _ZERO_WIDTH + line
        lines.append(line)
    return "\n".join(lines)


def _flatten(item):
    """``item`` and every component nested inside it (Sections hold children)."""
    yield item
    walk = getattr(item, "walk_children", None)
    if walk is not None:
        yield from walk()


def _component_cost(item):
    """How many components ``item`` is worth, nested children included."""
    return sum(1 for _child in _flatten(item))


def _text_cost(item):
    """How many characters of TextDisplay text ``item`` contributes."""
    return sum(
        len(child.content)
        for child in _flatten(item)
        if isinstance(child, discord.ui.TextDisplay)
    )


@dataclass(frozen=True)
class SectionBudget:
    """What is still free on the card when a section renderer runs.

    Handed to a renderer so it can SIZE its own output (how many list rows to
    draw, how long a title may be) instead of being rolled back whole for
    overflowing. Frozen and detached on purpose: the framework, not the
    renderer, is what measures and charges a block, so there is nothing here
    to mutate and no way for one connector to spend another's room.

    ``components`` counts the section's own leading Separator too, so a
    renderer that means to fill the card should leave one spare.
    """

    text: int
    components: int


class _CardBudget:
    """Adds separator-led blocks to a card, dropping whichever ones would push
    it past :data:`_CONTENT_BUDGET` characters or :data:`_CONTENT_COMPONENTS`
    components instead of letting the whole card fail to send.

    Drops are whole blocks, never a mid-line cut - the same "clean truncation"
    discipline as the old embed's ``add_field_within_budget`` this replaces -
    and :attr:`truncated` drives the one footer note the card shows when it
    happens. Blocks are MEASURED after the fact rather than predicted, because
    a connector section's content comes from a renderer another lot wrote
    (:func:`register_section_renderer`): what it costs is not knowable here,
    only observable once it has been added, and rolled back if it does not fit.
    """

    __slots__ = ("container", "remaining", "components", "truncated")

    def __init__(self, container, budget=None, components=None):
        self.container = container
        # A module GLOBAL lookup at call time (never a default-argument value,
        # which Python would bind once at function-definition time) - so a
        # test can monkeypatch `_CONTENT_BUDGET` and see it take effect.
        self.remaining = _CONTENT_BUDGET if budget is None else budget
        self.components = _CONTENT_COMPONENTS if components is None else components
        self.truncated = False

    def snapshot(self):
        """The room left, as the frozen value a section renderer is handed."""
        return SectionBudget(text=self.remaining, components=self.components)

    def _rollback(self, mark):
        for item in list(self.container.children[mark:]):
            self.container.remove_item(item)

    def _open(self):
        """Start a block: remember where it begins and lay its Separator."""
        mark = len(self.container.children)
        self.container.add_item(discord.ui.Separator())
        return mark

    def _commit(self, mark):
        """Measure what was added since ``mark`` and keep it only if it fits.

        ``build`` adding nothing is a legitimate answer (a connector renderer
        may decide it has nothing to show): the leading Separator is rolled
        back too, so the card never grows a divider with no section under it.
        """
        added = list(self.container.children[mark:])
        if len(added) <= 1:
            self._rollback(mark)
            return False
        cost = sum(_text_cost(item) for item in added)
        components = sum(_component_cost(item) for item in added)
        if cost > self.remaining or components > self.components:
            self._rollback(mark)
            self.truncated = True
            return False
        self.remaining -= cost
        self.components -= components
        return True

    def add_items(self, build):
        """Run ``build(container)`` behind a Separator, keeping what it added
        only if it fits. Returns True when the block survived.

        An exception from ``build`` rolls back first and then propagates -
        deciding what to do with a broken renderer is :func:`render_sections`'
        job, not the budget's.
        """
        mark = self._open()
        try:
            build(self.container)
        except Exception:
            self._rollback(mark)
            raise
        return self._commit(mark)

    async def add_items_async(self, build):
        """:meth:`add_items` for an AWAITABLE builder - the connector seam.

        Same three guarantees (separator rolled back with an empty block,
        exception rolled back then re-raised, cost charged after the fact), so
        a section renderer written in another lot gains nothing and loses
        nothing by being async. The measuring half is shared with the sync
        path rather than copied, so the two can never drift.
        """
        mark = self._open()
        try:
            await build(self.container)
        except Exception:
            self._rollback(mark)
            raise
        return self._commit(mark)

    def add_block(self, text):
        if not text:
            return False
        return self.add_items(
            lambda container: container.add_item(discord.ui.TextDisplay(text))
        )


def _header_section(member, visible):
    # Both halves carry a structural markdown prefix ("## " / "-# ") that a
    # newline in the value would escape from, so both are flattened for the
    # same reason the label/value rows are (see _one_line).
    lines = ["## " + _one_line(member.display_name)]
    pronouns = visible.get("pronouns")
    if pronouns:
        lines.append("-# " + _one_line(pronouns))
    return discord.ui.Section(
        discord.ui.TextDisplay("\n".join(lines)),
        accessory=discord.ui.Thumbnail(member.display_avatar.url),
    )


def _labelled_line(label, value):
    """One ``**label:** value`` row, flattened so it stays exactly one row."""
    return "**{label}:** {value}".format(label=_one_line(label), value=_one_line(value))


def _gaming_id_lines(gaming_ids):
    lines = []
    for key in registry.GAMING_ID_KEYS:
        value = gaming_ids.get(key)
        if value:
            lines.append(
                _labelled_line(
                    _(registry.GAMING_ID_LABELS[key]),
                    _clip(value, _GAMING_ID_LINE_CLIP),
                )
            )
    return lines


def _custom_field_lines(pairs):
    lines = []
    for pair in pairs:
        label = pair.get("label") if isinstance(pair, dict) else None
        value = pair.get("value") if isinstance(pair, dict) else None
        if label and value:
            lines.append(_labelled_line(label, value))
    return lines


# The FINAL renderer contract P4 implements:
#
#     async def render(container, field, viewer, connection, budget) -> None
#
# ``connection`` is the ``profile_connections`` row for this exact section,
# payload already decoded (see connectors/storage._row_to_connection), and
# ``budget`` is the frozen :class:`SectionBudget` of what is still free. The
# renderer appends whatever Components V2 items it wants straight onto
# ``container`` and returns nothing.
SectionRenderer = Callable[
    [
        discord.ui.Container,
        registry.Field,
        ViewerContext,
        dict,
        SectionBudget,
    ],
    Awaitable[None],
]

# section name -> renderer. Empty until a P3/P4 lot calls
# register_section_renderer; every LINKED connector section renders the
# fallback "Linked" badge until then. Module-level and mutable ON PURPOSE (like
# tools.cooldowns' bounded maps) - this IS the seam other lots hang off of.
SECTION_RENDERERS: dict[str, SectionRenderer] = {}


def register_section_renderer(section, renderer):
    """P3/P4 entry point: wire a connector SECTION's real rendering in.

    ``await renderer(container, field, viewer, connection, budget)`` is called
    only when BOTH preconditions the card enforces already hold:

    * the CURRENT viewer may see ``field.name`` (visibility is resolved before
      this seam is ever reached, so a renderer never re-checks it);
    * the owner really has a ``profile_connections`` row for it, which is what
      ``connection`` carries - so it is never None and a renderer never has to
      ask the database whether the account it is drawing exists.

    The renderer is AWAITED: fetching (a cache, a bounded API call) is allowed
    here. It must still be quick - it runs inside the ``/profile view``
    response - and it must not write to ``container`` after returning.
    ``budget`` is a frozen snapshot, for sizing its own output; the framework
    charges the block afterwards either way.
    """
    if not registry.is_known(section):
        raise registry.UnknownField(section)
    SECTION_RENDERERS[section] = renderer


def _linked_badge(container, field):
    """The fallback for a section nobody has written a renderer for yet.

    It only ever runs over a section that HAS a connection row, so the word is
    a fact about this profile, not a placeholder.
    """
    container.add_item(
        discord.ui.TextDisplay("**" + _(field.label) + "**\n" + _("Linked"))
    )


async def render_sections(container, sections, viewer, connections, budget):
    """Append one block per connector SECTION in ``sections`` onto ``container``.

    ``sections`` is the ordered list of registry :class:`~registry.Field`
    objects the viewer may see that have no storage in this lot AND that the
    owner has actually linked (see :func:`_connector_sections`); ``connections``
    maps a section name to its ``profile_connections`` row. Each section gets
    the renderer P3/P4 registered for its name, or a sober "Linked" badge when
    none is registered yet - which is a TRUE statement by construction, because
    an unlinked section never reaches this function.

    ``budget`` is required: minting a fresh full-size one here would let a
    caller that already spent most of the card hand its connectors a budget
    they do not have, and the Components V2 ceiling is per MESSAGE.

    Three things a renderer written in ANOTHER lot cannot be trusted with are
    handled here rather than in that lot:

    * it may raise - the section falls back to the badge and the rest of the
      card still renders, because one broken connector must not take
      ``/profile view`` down for a whole profile;
    * it may add nothing - the leading Separator is rolled back with it, so no
      divider is left hanging over an empty section;
    * it may be enormous - its output is charged to ``budget`` after the fact
      and dropped whole if it would blow the 4000-character / 40-component
      Components V2 ceiling.
    """
    for field in sections:
        connection = connections.get(field.name) or {}
        renderer = SECTION_RENDERERS.get(field.name)
        if renderer is not None:
            room = budget.snapshot()
            try:
                await budget.add_items_async(
                    lambda box, f=field, row=connection, left=room: renderer(
                        box, f, viewer, row, left
                    )
                )
                continue
            except Exception:
                log.exception(
                    "Profile section renderer for %r failed, showing the badge",
                    field.name,
                )
        budget.add_items(lambda box, f=field: _linked_badge(box, f))


class ProfileCard(discord.ui.LayoutView):
    """Public, read-only Components V2 profile card (``/profile view``).

    A plain :class:`~discord.ui.LayoutView`, not author-gated: there is
    nothing to interact WITH (no button, no select), exactly like
    :class:`~cogs.config.welcome.WelcomeStatusView`. The command that sends it
    stays ``guild_only`` (a shared server is what makes ``shares_guild`` true
    for the ``server``-level fields) - this class does not enforce that
    itself, matching its purely presentational role.

    ``timeout=None`` for the same reason: with no dispatchable item,
    discord.py neither stores the view for a sent message nor starts a timeout
    task (``BaseView.is_dispatchable`` is what decides both), so a timeout
    would only be a timer over nothing. Same shape as the static cards in
    cogs/anilist/feed_views.py. Nothing binds a ``message`` to it either -
    there is no control to disable when it expires.

    Built through :meth:`create` rather than ``__init__`` because a connector
    section renderer is awaited (see :func:`render_sections`).
    """

    def __init__(self):
        super().__init__(timeout=None)

    @classmethod
    async def create(cls, member, visible, connector_sections, viewer, connections):
        card = cls()
        await card._build(member, visible, connector_sections, viewer, connections)
        return card

    async def _build(self, member, visible, connector_sections, viewer, connections):
        accent = visible.get("accent")
        colour = discord.Colour(accent) if accent is not None else random_colour()
        container = discord.ui.Container(accent_colour=colour)
        container.add_item(_header_section(member, visible))

        budget = _CardBudget(container)
        bio = visible.get("bio")
        if bio:
            # Keeps its newlines - it is the one block value - but not the
            # power to open a section of its own (see defuse_lines).
            budget.add_block(defuse_lines(bio))

        custom_lines = _custom_field_lines(visible.get("custom_fields") or [])
        if custom_lines:
            budget.add_block(
                "**" + _("Custom fields") + "**\n" + "\n".join(custom_lines)
            )

        gaming_lines = _gaming_id_lines(visible.get("gaming_ids") or {})
        if gaming_lines:
            budget.add_block(
                "**" + _("Gaming IDs") + "**\n" + "\n".join(gaming_lines)
            )

        if connector_sections:
            await render_sections(
                container, connector_sections, viewer, connections, budget
            )

        if budget.truncated:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "-# " + _("This profile is too long to show in full.")
                )
            )

        self.add_item(container)


def _by_connector(connections):
    """Index ``profile_connections`` rows by section name.

    Tolerates anything row-shaped (a dict from
    ``connectors.storage.get_connections``, an asyncpg Record) and silently
    drops what is not - a malformed row must cost one section, never the card.
    """
    linked = {}
    for row in connections or ():
        try:
            name = row["connector"]
        except (TypeError, KeyError, IndexError):
            continue
        if name:
            linked[name] = row
    return linked


def _connector_sections(visibility_map, viewer, linked):
    """Connector SECTIONS (registry fields with no storage of their own) the
    card may show for ``viewer``.

    TWO conditions, both necessary. Visibility says who is allowed to look;
    ``linked`` (the names present in ``profile_connections``) says whether
    there is anything to look AT. A visibility row on its own is not evidence
    of an account - a user can publish ``steam`` from the panel and never link
    it, and the two presence sections cannot be linked by anyone until P5
    writes their marker rows - so a section without a connection is DROPPED
    rather than badged. "Linked" is an assertion about someone else's
    accounts; the card does not make it on faith.

    Nothing is said in the dropped case either (no "coming soon" on a stranger's
    card): what a viewer sees is what exists, and an owner who wants to know
    where their sections stand has ``connections list``.
    """
    return [
        registry.get(name)
        for name in visible_field_names(visibility_map, viewer)
        if not registry.get(name).stored and name in linked
    ]


async def build_profile_card(member, profile, visibility_map, viewer, connections):
    """Assemble the card ``/profile view`` sends, or ``None`` when the viewer
    may see nothing at all (the caller falls back to a plain "no profile"
    message rather than sending an empty card).

    Computes the exact same thing the old embed path did -
    :func:`visibility.resolve_visible_fields` - plus the connector sections the
    viewer may see AND the owner has really linked
    (:func:`_connector_sections`), so switching the presentation from an embed
    to a card changes nothing about WHAT is visible to whom.

    ``connections`` is the caller's already-read list of
    ``profile_connections`` rows (one indexed read, at most seven rows by the
    table's own primary key). It is REQUIRED rather than optional: a caller who
    forgets it would silently render a profile with no connector section at
    all, which is a lie in the other direction.
    """
    visible = resolve_visible_fields(profile, visibility_map, viewer)
    linked = _by_connector(connections)
    connector_sections = _connector_sections(visibility_map, viewer, linked)
    if not visible and not connector_sections:
        return None
    return await ProfileCard.create(
        member, visible, connector_sections, viewer, linked
    )


# ---------------------------------------------------------------------------
# The visibility panel: /profile panel
# ---------------------------------------------------------------------------

_LEVEL_LABELS = {
    visibility.PUBLIC: N_("Public"),
    visibility.SERVER: N_("Server"),
    visibility.PRIVATE: N_("Private"),
}

# Components the panel emits whatever the section count: the Container, its
# header / overview / footer TextDisplays, the four Separators between the
# blocks, and the edit button's own ActionRow plus the button itself. Each
# section then costs 2 (its ActionRow and the Select inside it). A test pins
# this arithmetic to the real rendered payload so it cannot drift.
_PANEL_FIXED_COMPONENTS = 10


def _panel_component_count(sections):
    return _PANEL_FIXED_COMPONENTS + 2 * sections


def _level_line(field, level):
    """The one wording used for BOTH the overview line and a select option.

    The select option carries the section name on purpose: an option marked
    ``default`` is what Discord renders on the COLLAPSED select, and the
    placeholder is then never shown - so a bare "Private" would leave twelve
    identical-looking pickers with nothing saying which section each drives.
    """
    return "{label}: {level}".format(
        label=_(field.label), level=_(_LEVEL_LABELS[level])
    )


class _SectionVisibilitySelect(discord.ui.Select):
    """One section's public/server/private picker, defaulted to its current
    level. Lives in its own :class:`~discord.ui.ActionRow` (a Section
    accessory can only hold a button, never a select - see
    cogs/community/usersettings.py's ``ChoiceSelect`` for the same shape).
    """

    def __init__(self, panel, field, current_level):
        super().__init__(
            placeholder=_(field.label),
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_level_line(field, level),
                    value=level,
                    default=(level == current_level),
                )
                for level in visibility.LEVELS
            ],
        )
        self._panel = panel
        self.field = field

    async def callback(self, interaction):
        # A malformed (empty) payload must not raise an IndexError out here,
        # where nothing would answer the interaction: it is passed on as None
        # and rejected inside the panel's own handled path, which answers the
        # clicker (same discipline as usersettings.ChoiceSelect).
        values = self.values
        await self._panel.set_level(
            interaction, self.field, values[0] if values else None
        )


class _EditGamingIdButton(discord.ui.Button):
    """Opens the existing gaming-ID modal - the panel adds no second editor."""

    def __init__(self, panel):
        super().__init__(
            label=_("Edit a gaming ID..."),
            emoji="\U0000270F",
            style=discord.ButtonStyle.primary,
        )
        self._panel = panel

    async def callback(self, interaction):
        try:
            await interaction.response.send_modal(ProfileEditModal(self._panel.cog))
        except Exception:
            log.exception("Profile panel edit button failed")
            await interactions.notify_failure(interaction)


class ProfileVisibilityPanel(AuthorLayoutView):
    """Author-restricted Components V2 panel: the graphical twin of the text
    ``profile visibility`` command, always over the OWNER's own profile (no
    target member - the invoking user's own visibility, like every other
    "your preferences" panel in this codebase).

    Single :class:`~discord.ui.Container` in the house style: a header, a
    "current visibility" overview line per section, one select per section
    (every :data:`registry.FIELD_NAMES` entry - the five stored fields AND
    every connector name, since :func:`storage.set_visibility` already accepts
    both), then the gaming-ID edit button. Publishing a connector section the
    user has not linked is allowed and simply shows nothing on the card: the
    two are independent questions (who may look / is there anything to look
    at), and the card refuses to imply the second from the first - see
    :func:`_connector_sections`. Every write goes through
    ``storage.set_visibility`` via ``self.cog.bot.db_pool`` - this module owns
    no query of its own, only the layout and the callbacks (same posture as
    :class:`~cogs.community.leveling.seasons_views.SeasonsPanel`).
    """

    def __init__(self, cog, author_id, visibility_map, *, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.state = {}
        self._load_state(visibility_map)
        self._build()

    def _overview_text(self):
        return "\n".join(
            _level_line(field, self.state[field.name]) for field in registry.FIELDS
        )

    def _build(self):
        self.clear_items()

        # Fail fast and say why, rather than shipping a payload Discord answers
        # with an opaque 400 (and that discord.py refuses outright past 40). The
        # section list is module-level registry data, so this trips for whoever
        # adds the connector, in tests, never for a member at runtime.
        total = _panel_component_count(len(registry.FIELDS))
        if total > CARD_COMPONENT_CAP:
            raise RuntimeError(
                f"profile visibility panel would emit {total} components, over "
                f"Discord's cap of {CARD_COMPONENT_CAP}: paginate the panel "
                f"instead of adding sections to it"
            )

        container = discord.ui.Container(accent_colour=random_colour())

        container.add_item(
            discord.ui.TextDisplay(
                "### \N{BUST IN SILHOUETTE} "
                + _("Profile visibility")
                + "\n-# "
                + _(
                    "Choose who can see each part of your profile: public "
                    "(anyone), server (members of servers you share), or "
                    "private (only you)."
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Current visibility") + "**\n" + self._overview_text()
            )
        )
        container.add_item(discord.ui.Separator())

        for field in registry.FIELDS:
            container.add_item(
                discord.ui.ActionRow(
                    _SectionVisibilitySelect(self, field, self.state[field.name])
                )
            )

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_EditGamingIdButton(self)))

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " - "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    async def _rerender(self, interaction):
        await interactions.refresh_layout(
            interaction, self.message, self, surface="profile visibility panel"
        )

    def _load_state(self, visibility_map):
        self.state = {
            field.name: visibility.level_for(visibility_map, field.name)
            for field in registry.FIELDS
        }

    async def set_level(self, interaction, field, level):
        try:
            stored = await storage.set_visibility(
                self.cog.bot.db_pool, self.author_id, field.name, level
            )
            self.state[field.name] = stored
            # Then RE-READ the whole map rather than trusting that echo alone:
            # the overview block claims to show every section, and the text
            # `profile visibility` command, a second open panel or the
            # dashboard may have moved another one since this panel was built.
            # One indexed point read per click on a per-user singleton - the
            # same query the panel opened with, no new hot path. A read that
            # fails must NOT undo the write that just succeeded, so the echoed
            # level stands and only the refresh is degraded.
            try:
                self._load_state(
                    await storage.get_visibility(self.cog.bot.db_pool, self.author_id)
                )
            except Exception:
                log.exception(
                    "Profile visibility panel could not re-read %s", self.author_id
                )
            self._build()
            await self._rerender(interaction)
        except Exception:
            log.exception(
                "Profile visibility panel update failed for %s", field.name
            )
            await interactions.notify_failure(
                interaction,
                _("Something went wrong updating your profile visibility."),
            )
