"""app_commands.Translator that localizes slash command descriptions and choices.

This is the second i18n layer (the first, tools/i18n.py, localizes the bot's
reply text). It reuses the SAME compiled gettext catalogs, but is driven by the
explicit locale Discord passes at sync time - not the per-invocation ContextVar,
since Discord calls the translator in bulk outside any command task.

Command / group / parameter NAMES are deliberately left in English: Discord
enforces lowercase, no-space, 1-32-char names, and translating a name changes how
users type the command. Only descriptions and choice labels are translated.

For a description to translate it must exist as a msgid in the catalog. The exact
strings Discord uses come from the live command tree, so collect them once with
the owner command ?i18ndump (writes locales/command_strings.py for extraction),
then extract + translate + compile.
"""

from discord import app_commands

from tools import i18n

# Locations whose strings are names (untranslatable per Discord's rules).
_SKIP_LOCATIONS = {
    app_commands.TranslationContextLocation.command_name,
    app_commands.TranslationContextLocation.group_name,
    app_commands.TranslationContextLocation.parameter_name,
}


class YasuhoTranslator(app_commands.Translator):
    """Translate slash descriptions/choices against the gettext catalogs."""

    async def translate(self, string, locale, context):
        if context.location in _SKIP_LOCATIONS:
            return None
        code = i18n.normalize(str(locale))
        if not code or code == i18n.DEFAULT_LOCALE:
            return None
        catalog = i18n.translations.get(code)
        if catalog is None:
            return None
        translated = catalog.gettext(string.message)
        # None keeps Discord's source (English); only override on a real change.
        return translated if translated != string.message else None
