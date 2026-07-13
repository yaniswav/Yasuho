import logging

import discord
from discord.ext import commands

from tools import interactions, settings
from tools.formats import random_colour
from tools.i18n import N_, _, ngettext
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

NO_CATEGORY = "No Category"

# Components V2 caps the combined text of a message's TextDisplay blocks at 4000
# characters (the classic embed description limit was 4096). A category page now
# renders three text blocks - the ``###`` heading, the command body and the
# ``-#`` footer - into a single Container, so they share this one budget instead
# of the embed's separate description / footer allowances. The body is fitted
# against what remains once the heading, footer, truncation notice and a fixed
# control reserve are subtracted (see :meth:`HelpView._build_category`), which
# ports the old 4096-budget, stop-cleanly-before-the-limit line truncation.
CV2_TEXT_BUDGET = 4000

# Slack held back from the text budget for the non-TextDisplay chrome that shares
# the message (the category select's placeholder + option labels, the nav
# buttons). Discord budgets TextDisplay content, but reserving a fixed margin
# keeps a maximal page clear of the 4000 ceiling even if some control text counts.
CV2_CONTROL_RESERVE = 400

# Once this many messages have been posted after the help menu, navigating to a
# new category re-posts the menu at the bottom of the channel instead of editing
# it in place (otherwise it would update far up where the user can no longer see
# it). Self-correcting: right after a re-post nothing is below it, so subsequent
# navigation edits in place again until the channel buries it once more.
REPOST_AFTER_MESSAGES = 4

# Curated, top-level help taxonomy: one entry per line the home page shows.
# Each entry is ``(emoji, display name, home-page description, member cogs)``:
#
#   * ``emoji`` / ``name`` - the dropdown option and page heading (the names are
#     bare literals, rendered as-is; they are intentionally NOT translated, which
#     keeps the taxonomy a stable, language-neutral spine for both users and the
#     guard test in tests/cogs/test_help_taxonomy.py).
#   * ``description`` - a one-line blurb under the entry on the home page. It IS
#     user-facing prose, so it is marked with ``N_`` here (extracted, stored in
#     English) and translated at render time with ``_(...)`` in ``_build_home``,
#     the module-constant-then-translate-at-use pattern (see tools.i18n.mark).
#   * the trailing list holds one or more cog *class names* (what ``bot.get_cog``
#     expects) grouped under this single, human-friendly category.
#
# The order is member-relevance first (what a fresh member opens help for -
# music, games, anime, levels) and the admin-facing categories last. Every cog
# with visible commands MUST live in exactly one category (or in EXCLUDED_COGS);
# anything unclaimed is swept into the "Other" catch-all (see
# ``build_categories``) and, redundantly, flagged by the guard test - so "Other"
# should now always be empty.
CATEGORIES = [
    (
        "🎵",
        "Music",
        N_("Play music, queue tracks, tune filters and see lyrics."),
        ["Music"],
    ),
    (
        "🎮",
        "Fun & Games",
        N_("Memes, dice, minigames and quick laughs."),
        ["Fun", "Games"],
    ),
    (
        "🎬",
        "Anime & Manga",
        N_("Search AniList, track your list and get airing alerts."),
        ["AniList", "AniListFeed"],
    ),
    (
        "✨",
        "Levels & XP",
        N_("Earn XP, climb the leaderboard and set up rewards."),
        ["Leveling", "LevelRewards", "LevelConfigUI", "LevelAdmin"],
    ),
    (
        "👤",
        "Profile & Personal",
        N_("Your profile, reminders, AFK and personal preferences."),
        [
            "Profiles",
            "AFK",
            "Reminder",
            "AvatarHistory",
            "UserSettings",
            "Language",
        ],
    ),
    (
        "🧰",
        "Tools & Info",
        N_("Server info, polls, translations and handy utilities."),
        ["Info", "Meta", "Utility", "Extras", "SearchWeb"],
    ),
    (
        "🛡️",
        "Moderation",
        N_("Kicks, bans, warnings, automod and mod logs."),
        ["Moderation", "AutoMod", "ModLog", "Blacklist"],
    ),
    (
        "⚙️",
        "Server Setup",
        N_("Welcome messages, starboard, custom commands and more."),
        [
            "Settings",
            "Welcome",
            "Announcements",
            "CustomCommands",
            "Starboard",
            "TemporaryRooms",
            "Twitch",
        ],
    ),
    (
        "🎭",
        "Roles & Access",
        N_("Reaction roles, role menus, button roles and verification."),
        ["ReactionRoles", "RoleMenus", "ButtonRoles", "Verification"],
    ),
]
OTHER_EMOJI = "🧩"
OTHER_NAME = "Other"
# The catch-all's blurb. It should never render (nothing is unclaimed), but the
# code path stays as a safety net, so it carries a description like any category.
OTHER_DESCRIPTION = N_("Anything not yet sorted into a category.")

# Cogs never surfaced as a category: the help cog wires itself in separately, and
# Admin is owner-only (all its commands are hidden, so it never surfaces anyway -
# listing it here states the intent and lets the guard test treat it as claimed).
EXCLUDED_COGS = {"Help", "Admin"}


def _visible_commands(cmds):
    """Non-hidden commands from ``cmds``, sorted by name."""
    return sorted((c for c in cmds if not c.hidden), key=lambda c: c.name)


def build_categories(bot):
    """Resolve :data:`CATEGORIES` against the bot's currently loaded cogs.

    Returns an ordered list of dicts::

        {"emoji": str, "name": str, "description": str,
         "groups": [(cog_label, [commands]), ...], "total": int}

    Only the visible (non-hidden) commands of cogs that actually exist are
    included; a member cog that is unloaded or has no visible commands is
    skipped, and a category with zero visible commands is omitted entirely.

    Any cog with visible commands that is not listed in the taxonomy (and is
    not excluded, e.g. the help cog) is collected into a final "Other"
    category so nothing silently disappears.
    """
    resolved = []
    claimed = set(EXCLUDED_COGS)

    for emoji, name, description, cog_names in CATEGORIES:
        groups = []
        total = 0
        for cog_name in cog_names:
            claimed.add(cog_name)
            cog = bot.get_cog(cog_name)
            if cog is None:
                continue
            cmds = _visible_commands(cog.get_commands())
            if not cmds:
                continue
            groups.append((cog_name, cmds))
            total += len(cmds)
        if total:
            resolved.append(
                {
                    "emoji": emoji,
                    "name": name,
                    "description": description,
                    "groups": groups,
                    "total": total,
                }
            )

    # Catch-all: cogs (and cog-less commands) not claimed by the taxonomy.
    other_groups = []
    other_total = 0
    for cog_name, cog in bot.cogs.items():
        if cog_name in claimed:
            continue
        cmds = _visible_commands(cog.get_commands())
        if not cmds:
            continue
        other_groups.append((cog_name, cmds))
        other_total += len(cmds)

    cogless = _visible_commands([c for c in bot.commands if c.cog is None])
    if cogless:
        other_groups.append((NO_CATEGORY, cogless))
        other_total += len(cogless)

    if other_total:
        resolved.append(
            {
                "emoji": OTHER_EMOJI,
                "name": OTHER_NAME,
                "description": OTHER_DESCRIPTION,
                "groups": other_groups,
                "total": other_total,
            }
        )

    return resolved


def _category_lines(groups, prefix):
    """Render a category's cogs to display lines (pure, no truncation).

    Mirrors the old ``category_embed`` body build: a blank spacer between cogs, a
    bold sub-header per cog, then one terse ``prefix + qualified_name - short_doc``
    line per command. Kept side-effect-free so the fitting maths can be tested in
    isolation.
    """
    rendered = []
    for position, (label, commands_list) in enumerate(groups):
        if position:
            rendered.append("")
        rendered.append(f"**{label}**")
        for command in commands_list:
            doc = command.short_doc or _("No description provided.")
            rendered.append(f"`{prefix}{command.qualified_name}` - {doc}")
    return rendered


def _fit_lines(rendered, budget, notice, empty_text):
    """Join ``rendered`` lines that fit in ``budget``, returning ``(text, truncated)``.

    ``budget`` is the room for the body BEFORE the truncation ``notice`` (the
    caller reserves ``len(notice) + 1`` when computing it). Accumulates lines
    (each costs its length plus one newline once the block is non-empty) until the
    next line would overflow, then stops cleanly. When nothing was rendered the
    body is ``empty_text``; when truncation happened the ``notice`` is appended on
    its own line. This is the verbatim port of the old 4096-budget loop, so the
    biggest category still stops before the limit exactly as before.
    """
    lines = []
    length = 0
    truncated = False
    for line in rendered:
        extra = len(line) + (1 if lines else 0)
        if length + extra > budget:
            truncated = True
            break
        lines.append(line)
        length += extra

    text = "\n".join(lines) if lines else empty_text
    if truncated:
        text = text + "\n" + notice
    return text, truncated


def _group_blocks(help_command, group, expand):
    """Text blocks for a group's help (heading, description, aliases, subcommands).

    Shared by the interactive :class:`GroupHelpView` and the one-shot
    :class:`_HelpCard` used when a group has no visible subcommands, so both
    render byte-identical content. Mirrors the old ``group_embed``: the signature
    as a ``###`` heading, the group help (or the no-description fallback), an
    optional bold Aliases block, and - only when there are visible subcommands -
    a Subcommands block that is the full list when ``expand`` else the collapsed
    notice.
    """
    prefix = help_command.context.clean_prefix
    blocks = [
        "### " + help_command.get_command_signature(group),
        group.help or _("No description provided."),
    ]

    if group.aliases:
        aliases = ", ".join(f"`{alias}`" for alias in group.aliases)
        blocks.append("**" + _("Aliases") + "**\n" + aliases)

    subcommands = sorted(
        (c for c in group.commands if not c.hidden), key=lambda c: c.name
    )
    if subcommands:
        if expand:
            value = "\n".join(
                f"`{c.name}` - {c.short_doc or _('No description provided.')}"
                for c in subcommands
            )
        else:
            value = _(
                "This command has {count}. "
                "Toggle expansion in /settings, or use "
                "`{prefix}help {group} <subcommand>`."
            ).format(
                count=ngettext(
                    "{n} subcommand", "{n} subcommands", len(subcommands)
                ).format(n=len(subcommands)),
                prefix=prefix,
                group=group.qualified_name,
            )
        blocks.append("**" + _("Subcommands") + "**\n" + value)

    return blocks


class _HelpCard(discord.ui.LayoutView):
    """A one-shot Components V2 card: a coloured Container of text blocks.

    Replaces the plain one-shot help embeds - command detail, the error notice,
    the no-categories fallback and group help with no subcommands. Purely
    presentational (no interactive components), so it is a plain LayoutView with
    no author gate, and it is a fresh send that is never edited afterwards (no CV2
    transition trap).
    """

    def __init__(self, blocks, *, timeout=180):
        super().__init__(timeout=timeout)
        container = discord.ui.Container(accent_colour=random_colour())
        for block in blocks:
            container.add_item(discord.ui.TextDisplay(block))
        self.add_item(container)


class CategorySelect(discord.ui.Select):
    """Dropdown of help categories; selecting one jumps to that category."""

    def __init__(self, owner, options):
        self._owner = owner
        super().__init__(
            placeholder=_("Jump to a category..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            await self._owner.show_category(interaction, self.values[0])
        except Exception:
            log.exception("Failed to render help category")
            await self._owner.report_error(interaction)


class _NavButton(discord.ui.Button):
    """A help navigation button (previous / home / forward).

    Holds the bound view coroutine to run on click; the view methods keep their
    own try/except + logging so each navigation path fails independently exactly
    as the old button callbacks did.
    """

    def __init__(self, on_click, **kwargs):
        self._on_click = on_click
        super().__init__(**kwargs)

    async def callback(self, interaction):
        await self._on_click(interaction)


class HelpView(AuthorLayoutView):
    """Author-restricted, navigable overview of every command category.

    A single Components V2 :class:`~discord.ui.Container` renders the current page
    - the Home welcome page (bot intro + category overview) or one category page
    (bold cog sub-headers + terse command lines) - as TextDisplay blocks, with the
    category dropdown (jump anywhere) and the previous / home / forward buttons
    (step through categories, return home) in trailing ActionRows. State is a
    single index: ``None`` means the Home page, otherwise the position within
    ``self.categories``. Every re-render rebuilds the container in place and edits
    the message ``view=``-only (a CV2 message rejects ``embed=``/``content=``).
    """

    def __init__(self, help_command, categories, timeout=180):
        # Keep the classic help menu's exact deny wording (this surface used
        # AuthorView's default before the Components V2 migration).
        super().__init__(
            help_command.context.author.id,
            timeout=timeout,
            deny_message="This menu isn't for you.",
        )
        self.help_command = help_command
        self.bot = help_command.context.bot
        self.prefix = help_command.context.clean_prefix
        # ``categories`` is a list of resolved category dicts (see
        # ``build_categories``); the index into it is the only navigation state.
        self.categories = categories
        self.index = None

    # -- data helpers -----------------------------------------------------

    def _select_options(self):
        options = []
        for position, category in enumerate(self.categories):
            options.append(
                discord.SelectOption(
                    label=category["name"],
                    value=str(position),
                    description=ngettext(
                        "{n} command", "{n} commands", category["total"]
                    ).format(n=category["total"]),
                    emoji=category["emoji"],
                )
            )
        return options

    # -- layout -----------------------------------------------------------

    async def rebuild(self):
        """(Re)assemble the layout for the current page, freshly each render.

        Fresh component instances every time (mirroring the house CV2 panels), so
        the same view object can be re-serialised on the next ``view=``-only edit
        or re-post without any stale-item reuse.
        """
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        if self.index is None:
            await self._build_home(container)
        else:
            self._build_category(container, self.index)

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.ActionRow(CategorySelect(self, self._select_options()))
        )

        active = self.index is not None
        container.add_item(
            discord.ui.ActionRow(
                _NavButton(
                    self.previous,
                    emoji="◀",
                    style=discord.ButtonStyle.secondary,
                    disabled=not active or self.index == 0,
                ),
                _NavButton(
                    self.home,
                    emoji="🏠",
                    label="Home",
                    style=discord.ButtonStyle.primary,
                ),
                _NavButton(
                    self.forward,
                    emoji="▶",
                    style=discord.ButtonStyle.secondary,
                    disabled=not active or self.index == len(self.categories) - 1,
                ),
            )
        )
        self.add_item(container)

    async def _build_home(self, container):
        """The friendly welcome page shown by the bot-wide help menu."""
        bot = self.bot
        prefix = self.prefix
        guild = self.help_command.context.guild

        # Title + intro beside the bot avatar (the old embed thumbnail, now a
        # Section accessory).
        title = _("{bot} • Help").format(bot=bot.user.name)
        intro = _(
            "Hey there! I'm **{bot}**, glad to help. 💫\n\n"
            "Use the menu to jump to a category, the arrows to browse, "
            "or `{prefix}help <command>` for a specific command."
        ).format(bot=bot.user.name, prefix=prefix)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay("### " + title),
                discord.ui.TextDisplay(intro),
                accessory=discord.ui.Thumbnail(bot.user.display_avatar.url),
            )
        )
        container.add_item(discord.ui.Separator())

        # Server basics (or DM fallback).
        if guild is not None:
            try:
                # Prefer the Leveling cog's in-memory read-through (level_config
                # with the legacy JSONB fallback resolved at load); fall back to the
                # raw JSONB bool only if that cog is not loaded.
                leveling_cog = bot.get_cog("Leveling")
                if leveling_cog is not None:
                    enabled = leveling_cog.is_enabled(guild.id)
                else:
                    enabled = await settings.get_guild(
                        bot.db_pool, guild.id, "leveling_enabled", False
                    )
                leveling = _("Enabled ✅") if enabled else _("Disabled ❌")
            except Exception:
                log.exception(
                    "Failed to read leveling state for guild %s", guild.id
                )
                leveling = _("Unknown")
            server_block = (
                "**"
                + _("🏠 Server")
                + "**\n"
                + _(
                    "Prefix: `{prefix}`\n"
                    "Members: **{members}**\n"
                    "Leveling: {leveling}"
                ).format(
                    prefix=prefix,
                    members=f"{guild.member_count:,}",
                    leveling=leveling,
                )
            )
        else:
            server_block = (
                "**"
                + _("🏠 Direct Messages")
                + "**\n"
                + _("Default prefix: `{prefix}`").format(prefix=prefix)
            )
        container.add_item(discord.ui.TextDisplay(server_block))

        # Curated categories. A CV2 TextDisplay has no per-field 1024-char cap
        # (unlike the old embed fields), so every line goes in one block: per
        # category the emoji + name + visible-command count, then a one-line
        # ``-#`` subtext blurb (translated here from the ``N_`` source stored in
        # CATEGORIES) so the home reads like a phone home screen, not a list.
        categories = self.categories
        lines = []
        total = 0
        for category in categories:
            total += category["total"]
            count = ngettext(
                "{n} command", "{n} commands", category["total"]
            ).format(n=category["total"])
            lines.append(
                f"{category['emoji']} **{category['name']}** - {count}"
            )
            # Bind before translating: babel's token-based extractor would
            # otherwise capture the literal "description" as a bogus msgid.
            blurb = category["description"]
            lines.append("-# " + _(blurb))
        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("📚 Categories") + "**\n" + "\n".join(lines)
            )
        )

        container.add_item(discord.ui.Separator())
        footer = _(
            "{commands} across {categories} • Use the menu or arrows to explore"
        ).format(
            commands=ngettext("{n} command", "{n} commands", total).format(
                n=total
            ),
            categories=ngettext(
                "{n} category", "{n} categories", len(categories)
            ).format(n=len(categories)),
        )
        container.add_item(discord.ui.TextDisplay("-# " + footer))

    def _build_category(self, container, index):
        category = self.categories[index]
        heading = f"### {category['emoji']} {category['name']}"
        footer = "-# " + _(
            "Category {current}/{total} • "
            "{prefix}help <command> for details"
        ).format(
            current=index + 1, total=len(self.categories), prefix=self.prefix
        )
        notice = _(
            "...more commands available. Use "
            "`{prefix}help <command>` to see them."
        ).format(prefix=self.prefix)

        # The heading, body and footer share one CV2 text budget, so fit the body
        # against what remains once the heading, footer, control reserve and the
        # truncation notice are subtracted - the old stop-cleanly-before-the-limit
        # behaviour, ported from the 4096 embed-description budget.
        budget = (
            CV2_TEXT_BUDGET
            - len(heading)
            - len(footer)
            - CV2_CONTROL_RESERVE
            - (len(notice) + 1)
        )
        rendered = _category_lines(category["groups"], self.prefix)
        body, _truncated = _fit_lines(
            rendered, budget, notice, _("No commands available.")
        )

        container.add_item(discord.ui.TextDisplay(heading))
        container.add_item(discord.ui.TextDisplay(body))
        container.add_item(discord.ui.TextDisplay(footer))

    # -- navigation -------------------------------------------------------

    async def _is_buried(self, interaction):
        """True when enough messages piled up after the menu to re-post it."""
        channel = interaction.channel
        if channel is None or self.message is None:
            return False
        try:
            count = 0
            async for _msg in channel.history(
                limit=REPOST_AFTER_MESSAGES, after=self.message
            ):
                count += 1
            return count >= REPOST_AFTER_MESSAGES
        except discord.HTTPException:
            return False

    async def _repost(self, interaction):
        """Re-post the menu at the bottom of the channel, dropping the old one.

        Mirrors the old embed path exactly: send a fresh message carrying the SAME
        view (already rebuilt for the target page), rebind ``self.message`` to it,
        then delete the previous message. The view object lives on - it is not
        stopped - now bound to the new message. A CV2 message carries its content
        inside the view, so this sends ``view=`` only (no embed).
        """
        await interaction.response.defer()
        old = self.message
        self.message = await interaction.channel.send(view=self)
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def _render(self, interaction):
        await self.rebuild()
        # If later messages have buried the menu, re-post it at the bottom rather
        # than editing in place (which would strand the user scrolled up).
        if await self._is_buried(interaction):
            await self._repost(interaction)
        else:
            await interaction.response.edit_message(view=self)

    async def show_category(self, interaction, value):
        try:
            index = int(value)
        except (TypeError, ValueError):
            index = None
        if index is not None and 0 <= index < len(self.categories):
            self.index = index
        else:
            self.index = None
        await self._render(interaction)

    async def report_error(self, interaction):
        """Best-effort, ephemeral error notice that never raises."""
        await interactions.notify_failure(
            interaction, _("Something went wrong opening the help menu.")
        )

    async def previous(self, interaction):
        try:
            self.index = 0 if self.index is None else max(0, self.index - 1)
            await self._render(interaction)
        except Exception:
            log.exception("Failed to page to the previous help category")
            await self.report_error(interaction)

    async def home(self, interaction):
        try:
            self.index = None
            await self._render(interaction)
        except Exception:
            log.exception("Failed to return to help home")
            await self.report_error(interaction)

    async def forward(self, interaction):
        try:
            last = len(self.categories) - 1
            self.index = 0 if self.index is None else min(last, self.index + 1)
            await self._render(interaction)
        except Exception:
            log.exception("Failed to page to the next help category")
            await self.report_error(interaction)


class _ExpandToggleButton(discord.ui.Button):
    """The expand/collapse subcommands toggle for :class:`GroupHelpView`."""

    def __init__(self, owner, label):
        self._owner = owner
        super().__init__(label=label, style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        await self._owner.on_toggle(interaction)


class GroupHelpView(AuthorLayoutView):
    """Author-restricted group help with a per-user expand/collapse toggle.

    A single Components V2 Container of the group's text blocks (see
    :func:`_group_blocks`) plus a trailing toggle button. Toggling flips the
    stored ``help_expand`` preference, rebuilds the container in place and edits
    the message ``view=``-only (the message is CV2 from its first send).
    """

    def __init__(self, help_command, group, expand, timeout=180):
        # Preserve the classic deny wording this surface had under AuthorView.
        super().__init__(
            help_command.context.author.id,
            timeout=timeout,
            deny_message="This menu isn't for you.",
        )
        self.help_command = help_command
        self.bot = help_command.context.bot
        self.group = group
        self.expand = expand
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        for block in _group_blocks(self.help_command, self.group, self.expand):
            container.add_item(discord.ui.TextDisplay(block))
        container.add_item(discord.ui.Separator())
        label = (
            _("Collapse subcommands") if self.expand else _("Expand subcommands")
        )
        container.add_item(discord.ui.ActionRow(_ExpandToggleButton(self, label)))
        self.add_item(container)

    async def on_toggle(self, interaction):
        try:
            self.expand = not self.expand
            await settings.set_user(
                self.bot.db_pool, self.author_id, "help_expand", self.expand
            )
            self.rebuild()
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Failed to toggle help_expand")
            try:
                await interaction.response.send_message(
                    _("Something went wrong updating that preference."), ephemeral=True
                )
            except Exception:
                pass


class YasuhoHelp(commands.HelpCommand):
    """Custom help command for Yasuho (replaces the default help_command)."""

    async def send_bot_help(self, mapping):
        prefix = self.context.clean_prefix
        categories = build_categories(self.context.bot)

        if not categories:
            view = _HelpCard(
                [
                    "### " + _("Help"),
                    _(
                        "Use `{prefix}help <command>` for more info on a command."
                    ).format(prefix=prefix),
                ]
            )
            await self.get_destination().send(view=view)
            return

        view = HelpView(self, categories)
        await view.rebuild()
        view.message = await self.get_destination().send(view=view)

    async def send_command_help(self, command):
        blocks = [
            "### " + self.get_command_signature(command),
            command.help or _("No description provided."),
        ]
        if command.aliases:
            aliases = ", ".join(f"`{alias}`" for alias in command.aliases)
            blocks.append("**" + _("Aliases") + "**\n" + aliases)

        await self.get_destination().send(view=_HelpCard(blocks))

    async def send_group_help(self, group):
        expand = await settings.get_user(
            self.context.bot.db_pool, self.context.author.id, "help_expand", False
        )

        if not any(not c.hidden for c in group.commands):
            await self.get_destination().send(
                view=_HelpCard(_group_blocks(self, group, expand))
            )
            return

        view = GroupHelpView(self, group, expand)
        view.message = await self.get_destination().send(view=view)

    async def send_error_message(self, error):
        await self.get_destination().send(
            view=_HelpCard(["### " + _("Help"), error])
        )


class Help(commands.Cog):
    """Installs the custom help command and restores the original on unload."""

    def __init__(self, bot):
        self.bot = bot
        self._original = bot.help_command
        bot.help_command = YasuhoHelp()
        bot.help_command.cog = self

    async def cog_unload(self):
        self.bot.help_command = self._original


async def setup(bot):
    await bot.add_cog(Help(bot))
