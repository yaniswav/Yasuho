import logging

import discord
from discord.ext import commands

from tools import settings
from tools.formats import random_colour

log = logging.getLogger(__name__)

# (key, label, default) — boolean per-user preferences. Add more entries here.
PREFS = [
    ("help_expand", "Help: expand subcommands", False),
]


def _style(value):
    """Green when a preference is on, grey when off."""
    return discord.ButtonStyle.success if value else discord.ButtonStyle.secondary


def _build_embed(author, states):
    """Build the settings embed from a {key: bool} state map."""
    embed = discord.Embed(
        title="Your settings",
        description="Click a button to toggle a preference.",
        colour=random_colour(),
    )
    embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    for key, label, _default in PREFS:
        embed.add_field(
            name=label, value="On" if states.get(key) else "Off", inline=False
        )
    return embed


class PrefButton(discord.ui.Button):
    """Toggle button bound to a single boolean preference."""

    def __init__(self, key, label, value):
        super().__init__(label=label, style=_style(value))
        self.key = key

    async def callback(self, interaction):
        view = self.view
        try:
            new_value = not view.states.get(self.key, False)
            await settings.set_user(
                view.bot.db_pool, view.author_id, self.key, new_value
            )
            view.states[self.key] = new_value
            self.style = _style(new_value)
            embed = _build_embed(view.author, view.states)
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception:
            log.exception("Failed to toggle user setting %s", self.key)
            try:
                await interaction.response.send_message(
                    "Something went wrong updating that setting.", ephemeral=True
                )
            except Exception:
                pass


class SettingsView(discord.ui.View):
    """Author-restricted panel of per-user preference toggles."""

    def __init__(self, bot, author, states, timeout=120):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.author = author
        self.author_id = author.id
        self.states = states
        self.message = None
        for key, label, _default in PREFS:
            self.add_item(PrefButton(key, label, states.get(key, False)))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
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


class UserSettings(commands.Cog):
    """Per-user preference panel that works in guilds and DMs."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="settings")
    async def settings_cmd(self, ctx):
        """Open your personal settings panel."""
        states = {}
        for key, _label, default in PREFS:
            states[key] = await settings.get_user(
                self.bot.db_pool, ctx.author.id, key, default
            )
        embed = _build_embed(ctx.author, states)
        view = SettingsView(self.bot, ctx.author, states)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(UserSettings(bot))
