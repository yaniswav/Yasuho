import logging

import discord
from discord.ext import commands

from tools import embed_creator, settings
from tools.views import AuthorView

log = logging.getLogger(__name__)

PANEL_COLOUR = 0x5865F2
ON_BADGE = "🟢"
OFF_BADGE = "⚪"


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
# own embed field + author-restricted toggle button automatically.
PREFS = [
    Preference(
        key="levelup_announce",
        label="Level-up announcements",
        emoji="🔔",
        description="Get pinged in chat when you reach a new level.",
        default=True,
    ),
    Preference(
        key="help_expand",
        label="Expanded help",
        emoji="📖",
        description="Show every subcommand inline when you browse help.",
        default=False,
    ),
]


def _style(value):
    """Green when a preference is on, grey when off."""
    return discord.ButtonStyle.success if value else discord.ButtonStyle.secondary


def build_embed(author, states):
    """Render the settings panel from a ``{key: bool}`` state map."""
    embed = discord.Embed(
        title="Your preferences",
        description=(
            "These settings only affect **you**, everywhere I'm used.\n"
            "Tap a button below to toggle a preference on or off."
        ),
        colour=PANEL_COLOUR,
    )
    embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.set_thumbnail(url=author.display_avatar.url)

    for pref in PREFS:
        on = bool(states.get(pref.key, pref.default))
        badge = ON_BADGE if on else OFF_BADGE
        state = "ON" if on else "OFF"
        embed.add_field(
            name=f"{pref.emoji} {pref.label} - {badge} {state}",
            value=pref.description,
            inline=False,
        )

    embed.set_footer(text="Only you can use these controls.")
    return embed


class PrefButton(discord.ui.Button):
    """Toggle button bound to a single boolean preference."""

    def __init__(self, pref, value):
        super().__init__(label=pref.label, emoji=pref.emoji, style=_style(value))
        self.pref = pref

    async def callback(self, interaction):
        view = self.view
        try:
            new_value = not view.states.get(self.pref.key, self.pref.default)
            await settings.set_user(
                view.bot.db_pool, view.author_id, self.pref.key, new_value
            )
            view.states[self.pref.key] = new_value
            self.style = _style(new_value)
            await interaction.response.edit_message(
                embed=build_embed(view.author, view.states), view=view
            )
        except Exception:
            log.exception(
                "Failed to toggle user setting %s for %s",
                self.pref.key,
                view.author_id,
            )
            await embed_creator.notify_failure(
                interaction, "Something went wrong updating that setting."
            )


class SettingsView(AuthorView):
    """Author-restricted panel of per-user preference toggles."""

    def __init__(self, bot, author, states, timeout=180):
        super().__init__(
            author.id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.bot = bot
        self.author = author
        self.states = states
        for pref in PREFS:
            value = bool(states.get(pref.key, pref.default))
            self.add_item(PrefButton(pref, value))


class UserSettings(commands.Cog):
    """Per-user preference panel that works in guilds and DMs."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="settings")
    async def settings_cmd(self, ctx):
        """Open your personal settings panel."""
        states = {}
        for pref in PREFS:
            states[pref.key] = await settings.get_user(
                self.bot.db_pool, ctx.author.id, pref.key, pref.default
            )
        view = SettingsView(self.bot, ctx.author, states)
        view.message = await ctx.send(
            embed=build_embed(ctx.author, states), view=view
        )


async def setup(bot):
    await bot.add_cog(UserSettings(bot))
