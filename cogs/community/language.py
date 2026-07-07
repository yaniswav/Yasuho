"""Per-user and per-guild language selection for Yasuho's replies.

This is the user-facing half of the i18n system (see tools/i18n.py). The locale
is stored as a "locale" key in the JSONB user/guild settings and read back by
i18n.resolve_locale on every command.
"""

import logging

import discord
from discord.ext import commands

from tools import i18n, interactions, settings
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Friendly names for the locale codes we may ship. Only codes with a compiled
# catalog (plus English) are actually offered.
LANGUAGE_NAMES = {
    "en": "English",
    "es": "Español",
    "pt_BR": "Português (BR)",
    "fr": "Français",
    "de": "Deutsch",
    "ru": "Русский",
    "ja": "日本語",
    "ko": "한국어",
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
    "tr": "Türkçe",
    "bg": "Български",
    "cs": "Čeština",
    "da": "Dansk",
    "el": "Ελληνικά",
    "fi": "Suomi",
    "hi": "हिन्दी",
    "hr": "Hrvatski",
    "hu": "Magyar",
    "id": "Bahasa Indonesia",
    "it": "Italiano",
    "lt": "Lietuvių",
    "nl": "Nederlands",
    "no": "Norsk",
    "pl": "Polski",
    "ro": "Română",
    "sv": "Svenska",
    "th": "ไทย",
    "uk": "Українська",
    "vi": "Tiếng Việt",
}


def _offered():
    """The (code, name) pairs we can actually serve, default first."""
    codes = sorted(i18n.LOCALES, key=lambda c: (c != i18n.DEFAULT_LOCALE, c))
    return [(code, LANGUAGE_NAMES.get(code, code)) for code in codes]


class LanguageSelect(discord.ui.Select):
    def __init__(self, current):
        options = [
            discord.SelectOption(label=name, value=code, default=(code == current))
            for code, name in _offered()
        ]
        super().__init__(placeholder="Choose your language...", options=options)

    async def callback(self, interaction):
        try:
            code = self.values[0]
            await settings.set_user(
                interaction.client.db_pool, interaction.user.id, "locale", code
            )
            # Switch this very task so the confirmation is already localized.
            i18n.current_locale.set(code)
            await interaction.response.edit_message(
                content=_("Your language is now **{lang}**.").format(
                    lang=LANGUAGE_NAMES.get(code, code)
                ),
                view=None,
            )
        except Exception:
            log.exception("Language select failed")
            await interactions.notify_failure(
                interaction, _("Sorry, I couldn't change your language.")
            )


class LanguageView(AuthorView):
    def __init__(self, author_id, current):
        super().__init__(author_id, timeout=120, deny_message="This menu isn't for you.")
        self.add_item(LanguageSelect(current))


class Language(commands.Cog):
    """Choose the language Yasuho replies to you in."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="language", aliases=["lang", "locale"])
    async def language(self, ctx):
        """Pick the language for the bot's replies to you."""
        if ctx.invoked_subcommand is not None:
            return
        current = await settings.get_user(
            self.bot.db_pool, ctx.author.id, "locale", i18n.DEFAULT_LOCALE
        )
        view = LanguageView(ctx.author.id, current)
        view.message = await ctx.send(_("Pick your language:"), view=view)

    @language.command(name="server", aliases=["guild", "default"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def language_server(self, ctx, code):
        """Set the server's default language (used when a member has not picked one)."""
        resolved = i18n.normalize(code)
        if resolved is None:
            available = ", ".join(f"`{c}`" for c, _name in _offered())
            return await ctx.send(
                _("Unknown language. Available: {options}").format(options=available)
            )
        await settings.set_guild(self.bot.db_pool, ctx.guild.id, "locale", resolved)
        await ctx.send(
            _("Server default language set to **{lang}**.").format(
                lang=LANGUAGE_NAMES.get(resolved, resolved)
            )
        )


async def setup(bot):
    await bot.add_cog(Language(bot))
