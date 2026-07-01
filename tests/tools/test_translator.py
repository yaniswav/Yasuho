"""Tests for the slash-description Translator (tools/translator.py).

Regression guard for a production sync failure: a localized command / parameter
description longer than Discord's 100-char limit must fall back to English
rather than 400 the whole application-command tree sync.
"""

import types

from discord import app_commands

from tools import i18n
from tools.translator import YasuhoTranslator


class _Catalog:
    """Minimal gettext catalog stand-in."""

    def __init__(self, mapping):
        self._m = mapping

    def gettext(self, message):
        return self._m.get(message, message)


def _string(message):
    return types.SimpleNamespace(message=message)


def _context(location):
    return types.SimpleNamespace(location=location)


_DESC = app_commands.TranslationContextLocation.command_description


async def _run(monkeypatch, message, translated, location=_DESC, locale="fr"):
    monkeypatch.setitem(i18n.translations, locale, _Catalog({message: translated}))
    return await YasuhoTranslator().translate(
        _string(message), locale, _context(location)
    )


async def test_short_translation_is_used(monkeypatch):
    assert await _run(monkeypatch, "Ban a user.", "Bannir un utilisateur.") == (
        "Bannir un utilisateur."
    )


async def test_over_100_char_translation_falls_back_to_english(monkeypatch):
    # The exact class of string that 400'd the live sync (es/el descriptions).
    assert await _run(monkeypatch, "Ban a user.", "B" * 101) is None


async def test_unchanged_translation_returns_none(monkeypatch):
    assert await _run(monkeypatch, "Ban a user.", "Ban a user.") is None


async def test_command_names_are_never_translated(monkeypatch):
    out = await _run(
        monkeypatch,
        "ban",
        "bannir",
        location=app_commands.TranslationContextLocation.command_name,
    )
    assert out is None
