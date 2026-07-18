import logging

import discord
from discord.ext import commands

from tools import i18n, privacy, rendering, settings
from tools.i18n import N_, _
from tools.interactions import notify_failure
from tools.views import AuthorView

log = logging.getLogger(__name__)

PANEL_COLOUR = 0x5865F2
ON_BADGE = "🟢"
OFF_BADGE = "⚪"

# Keep the layout within the component budget: one Section (plus a Separator)
# per preference means the panel stays comfortably under the Container limit.
MAX_PREFS = 10


class Preference:
    """A single boolean per-user preference rendered in the settings panel."""

    __slots__ = ("key", "label", "emoji", "description", "default")

    def __init__(self, key, label, emoji, description, default):
        self.key = key
        self.label = label
        self.emoji = emoji
        self.description = description
        self.default = default


# Ordered list of preferences. Drop a new ``Preference`` in here and it gets its
# own Section (TextDisplay + toggle button accessory) automatically. The label
# and description are N_-marked for extraction and translated at the use site via
# _(pref.label) / _(pref.description).
PREFS = [
    Preference(
        key="levelup_announce",
        label=N_("Level-up announcements"),
        emoji="🔔",
        description=N_("Get pinged in chat when you reach a new level."),
        default=True,
    ),
    # Only affects HOW you're referenced in a level-up announce (a mention vs
    # your plain name) - it never silences the announce itself, that's the
    # preference above. The key MUST match cogs/community/leveling.py's
    # _announce_levelup read (by literal, like every other preference here).
    Preference(
        key="levelup_ping",
        label=N_("Level-up ping"),
        emoji="📣",
        description=N_(
            "Ping you by mention in level-up announcements. Turn off to be "
            "named without a ping."
        ),
        default=True,
    ),
    Preference(
        key="help_expand",
        label=N_("Expanded help"),
        emoji="📖",
        description=N_("Show every subcommand inline when you browse help."),
        default=False,
    ),
    # Seeds a new music session's autoplay mode. The key MUST match
    # cogs/music/music.py's AUTOPLAY_PREF_KEY; the music cog reads it (by literal,
    # like leveling/help read their keys) when a session starts, and only then -
    # changing it here never flips a session that is already playing.
    Preference(
        key="music_autoplay",
        label=N_("Music autoplay"),
        emoji="✨",
        description=N_(
            "Keep playing recommended tracks when your music queue runs out."
        ),
        default=True,
    ),
    # Opt-in /play picker. The key MUST match cogs/music/search.py's
    # SEARCH_PICKER_PREF_KEY; the music cog reads it (by literal, like the keys
    # above) when /play gets a plain text query. Default OFF so /play is
    # unchanged until a member turns this on.
    Preference(
        key="music_search_picker",
        label=N_("Play search picker"),
        emoji="🔎",
        description=N_(
            "On /play, choose from the top matches instead of instantly queuing "
            "the first."
        ),
        default=False,
    ),
    Preference(
        key="avatar_history_tracking",
        label=N_("Avatar history tracking"),
        emoji="🖼️",
        description=N_(
            "Let Yasuho save future avatar and banner changes for public history."
        ),
        default=True,
    ),
]


def _style(value):
    """Green when a preference is on, grey when off."""
    return discord.ButtonStyle.success if value else discord.ButtonStyle.secondary


def _human_bytes(value):
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return (
                f"{int(amount)} {unit}"
                if unit == "B"
                else f"{amount:.1f} {unit}"
            )
        amount /= 1024
    return f"{value} B"


class AvatarDeletionView(AuthorView):
    """One-shot destructive confirmation for personal avatar history."""

    def __init__(self, cog, author_id):
        super().__init__(
            author_id,
            timeout=60,
            # A registered N_ literal (see tools.views._DENY_STRINGS); the base
            # AuthorView.interaction_check translates it at click time.
            deny_message="This prompt isn't for you.",
        )
        self.cog = cog
        self._running = False
        # Localize the labels at send time (the command context set the
        # invoker's locale), the way RemindLauncherView builds its labels in
        # __init__. The decorator labels below are construction-time
        # placeholders discord.py requires; these overrides are what render.
        self.confirm.label = _("Delete my avatar history")
        self.cancel.label = _("Cancel")

    @discord.ui.button(
        label="Delete my avatar history",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(self, interaction, button):
        if self._running:
            return
        self._running = True
        await interaction.response.defer()
        try:
            count, size = await privacy.delete_user_avatar_history(
                self.cog.bot.db_pool, self.author_id
            )
            self.stop()
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(
                content=_(
                    "Deleted {count} saved image(s) ({size}). Future avatar "
                    "tracking is now turned off."
                ).format(count=count, size=_human_bytes(size)),
                view=self,
            )
        except Exception:
            self._running = False
            log.exception(
                "Failed to delete avatar history for %s", self.author_id
            )
            await interaction.followup.send(
                _("Something went wrong deleting your avatar history."),
                ephemeral=True,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=_("Avatar history deletion cancelled."), view=self
        )


class PrefButton(discord.ui.Button):
    """Toggle button bound to a single boolean preference.

    Used as the ACCESSORY of a Components V2 Section. Layout views cannot use the
    ``@discord.ui.button`` decorator, so the button forwards its click to the
    owning panel, which flips the value, persists it and re-renders in place.
    """

    def __init__(self, panel, pref, value):
        # The label states the action the click performs, so the button reads
        # naturally next to the current ON/OFF state shown in the Section text.
        label = _("Turn off") if value else _("Turn on")
        super().__init__(label=label, emoji=pref.emoji, style=_style(value))
        self._panel = panel
        self.pref = pref

    async def callback(self, interaction):
        await self._panel.toggle(interaction, self.pref)


class SettingsView(discord.ui.LayoutView):
    """Author-restricted Components V2 panel of per-user preference toggles.

    Rendered as a Container holding one Section per preference: a TextDisplay
    with the label, current ON/OFF state and description, plus the toggle button
    as the Section accessory, with Separators between sections. A LayoutView is
    not a plain ``discord.ui.View``, so it cannot subclass ``AuthorView``; the
    author gating, locale resolution and timeout cleanup are mirrored here.
    """

    def __init__(self, bot, author, states, *, timeout=180):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.author = author
        self.author_id = author.id
        self.states = states
        self.message = None
        # Registered in tools.views._DENY_STRINGS; translated at check time.
        self._deny_message = N_("This panel isn't for you.")
        self._rerender()

    def _rerender(self):
        """(Re)assemble the layout from the current ``{key: bool}`` state map."""
        self.clear_items()

        container = discord.ui.Container(accent_colour=PANEL_COLOUR)
        container.add_item(discord.ui.TextDisplay(_("## Your preferences")))
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "These settings only affect **you**, everywhere I'm used.\n"
                    "Use the button beside a preference to toggle it on or off."
                )
            )
        )
        container.add_item(discord.ui.Separator())

        for pref in PREFS[:MAX_PREFS]:
            on = bool(self.states.get(pref.key, pref.default))
            badge = ON_BADGE if on else OFF_BADGE
            state = _("ON") if on else _("OFF")
            text = _("{emoji} **{label}** - {badge} {state}\n{description}").format(
                emoji=pref.emoji,
                label=_(pref.label),
                badge=badge,
                state=state,
                description=_(pref.description),
            )
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(text),
                    accessory=PrefButton(self, pref, on),
                )
            )
            container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(_("-# Only you can use these controls."))
        )
        self.add_item(container)

    async def toggle(self, interaction, pref):
        """Flip a preference, persist it and re-render the panel in place."""
        try:
            new_value = not self.states.get(pref.key, pref.default)
            if pref.key == privacy.AVATAR_TRACKING_KEY:
                await privacy.set_avatar_tracking(
                    self.bot.db_pool, self.author_id, new_value
                )
            else:
                await settings.set_user(
                    self.bot.db_pool, self.author_id, pref.key, new_value
                )
            self.states[pref.key] = new_value
            self._rerender()
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception(
                "Failed to toggle user setting %s for %s",
                pref.key,
                self.author_id,
            )
            await notify_failure(
                interaction, _("Something went wrong updating that setting.")
            )

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localize.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _(self._deny_message), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class UserSettings(commands.Cog):
    """Per-user preference panel that works in guilds and DMs."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="preferences", aliases=["settings"])
    async def preferences_cmd(self, ctx):
        """Open your personal preferences panel."""
        states = {}
        for pref in PREFS:
            states[pref.key] = await settings.get_user(
                self.bot.db_pool, ctx.author.id, pref.key, pref.default
            )
        view = SettingsView(self.bot, ctx.author, states)
        # A LayoutView carries its own content, so it is sent with view= only
        # (no embed, no content) and with mentions suppressed for safety.
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.hybrid_group(name="mydata", invoke_without_command=True)
    async def mydata(self, ctx):
        """Export your personal data or delete your avatar history."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                _(
                    "Use `{prefix}mydata export` to receive your data, or "
                    "`{prefix}mydata deleteavatars` to erase saved avatars."
                ).format(prefix=ctx.clean_prefix)
            )

    @mydata.command(name="export")
    @commands.cooldown(1, 3600, commands.BucketType.user)
    async def mydata_export(self, ctx):
        """Export your Yasuho personal data without OAuth secrets."""
        async def _build():
            data, avatar_rows = await privacy.collect_user_export(
                self.bot.db_pool, ctx.author.id
            )
            return await rendering.run_image_job(
                self.bot,
                privacy.build_export_archives,
                data,
                avatar_rows,
            )

        try:
            if ctx.interaction is not None:
                await ctx.defer(ephemeral=True)
                archives = await _build()
            else:
                async with ctx.typing():
                    archives = await _build()

            if ctx.interaction is not None:
                for filename, archive in archives:
                    await ctx.send(
                        file=discord.File(archive, filename=filename),
                        ephemeral=True,
                    )
                return

            for filename, archive in archives:
                await ctx.author.send(
                    file=discord.File(archive, filename=filename)
                )
            await ctx.send(
                _("I sent your data export to you by direct message.")
            )
        except discord.Forbidden:
            await ctx.send(
                _("I couldn't send you a direct message. Please enable DMs.")
            )
        except Exception:
            log.exception("Failed to export personal data for %s", ctx.author.id)
            await ctx.send(
                _("Something went wrong building your data export."),
                ephemeral=ctx.interaction is not None,
            )

    @mydata.command(name="deleteavatars")
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def mydata_deleteavatars(self, ctx):
        """Permanently delete your saved avatars and disable future tracking."""
        view = AvatarDeletionView(self, ctx.author.id)
        view.message = await ctx.send(
            _(
                "This permanently deletes every saved global avatar, server "
                "avatar and banner for your account. It also turns future "
                "tracking off. This cannot be undone."
            ),
            view=view,
            ephemeral=ctx.interaction is not None,
        )


async def setup(bot):
    await bot.add_cog(UserSettings(bot))
