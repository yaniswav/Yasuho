"""Tests for tools.i18n (runtime gettext + locale resolution).

Everything here runs against the REAL compiled catalogs under locales/*/LC_MESSAGES
(fr, ja, el are shipped with a .mo; en is the NullTranslations source language).
No network, DB, Discord, or Lavalink is touched: settings reads are served by the
fake_pool fixture and the module-level settings cache is cleared per test.

The known-French assertions pin a msgid whose fr translation is pure ASCII
('3DS Friend Code' -> 'Code ami 3DS') so this file stays ASCII-only while still
proving a real translation loaded from the compiled .mo.
"""

from __future__ import annotations

import types

import pytest

from tools import i18n, settings

# A msgid that is translated in locales/fr/LC_MESSAGES/yasuho.po and whose
# French rendering happens to be pure ASCII.
FR_MSGID = "3DS Friend Code"
FR_MSGSTR = "Code ami 3DS"


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_exact_code():
    # Real shipped catalogs are returned verbatim on an exact hit.
    assert i18n.normalize("fr") == "fr"
    assert i18n.normalize("en") == "en"
    assert i18n.normalize("ja") == "ja"
    assert i18n.normalize("el") == "el"


def test_normalize_base_language_fallback():
    # fr-CA has no catalog, but the base language 'fr' does.
    assert i18n.normalize("fr-CA") == "fr"
    # Discord sends 'en-US'; base 'en' is the default catalog.
    assert i18n.normalize("en-US") == "en"


def test_normalize_hyphen_to_underscore_exact(monkeypatch):
    # When the full region code IS a known catalog, it is returned with the
    # hyphen rewritten to an underscore (Discord 'pt-BR' -> catalog 'pt_BR').
    monkeypatch.setattr(i18n, "LOCALES", frozenset({"en", "pt_BR", "zh_CN"}))
    assert i18n.normalize("pt-BR") == "pt_BR"
    assert i18n.normalize("zh-CN") == "zh_CN"


def test_normalize_shares_base_language(monkeypatch):
    # No exact and no bare-base catalog, but one shares the base language.
    monkeypatch.setattr(i18n, "LOCALES", frozenset({"en", "pt_BR", "fr_CA"}))
    # 'pt-PT' -> 'pt_PT' (miss) -> base 'pt' (miss) -> shares base with 'pt_BR'.
    assert i18n.normalize("pt-PT") == "pt_BR"
    # 'fr-FR' -> 'fr_FR' (miss) -> base 'fr' (miss) -> shares base with 'fr_CA'.
    assert i18n.normalize("fr-FR") == "fr_CA"


def test_normalize_unknown_returns_none():
    assert i18n.normalize("xx") is None
    # Region code with no shared base among the real catalogs.
    assert i18n.normalize("pt-BR") is None
    assert i18n.normalize("zh-CN") is None


def test_normalize_empty_returns_none():
    assert i18n.normalize(None) is None
    assert i18n.normalize("") is None


# ---------------------------------------------------------------------------
# use_current_gettext
# ---------------------------------------------------------------------------


def test_gettext_english_source_passthrough():
    # reset_locale leaves the ContextVar on DEFAULT_LOCALE ('en' -> NullTranslations),
    # so both an arbitrary literal and a known source id come back verbatim.
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE
    assert i18n.use_current_gettext("Totally arbitrary source text zzz") == (
        "Totally arbitrary source text zzz"
    )
    assert i18n.use_current_gettext(FR_MSGID) == FR_MSGID


def test_gettext_french_known_translation():
    # Set the active locale and read a KNOWN translation from the compiled .mo.
    i18n.current_locale.set("fr")
    assert i18n.use_current_gettext(FR_MSGID) == FR_MSGSTR
    assert i18n.use_current_gettext(FR_MSGID) != FR_MSGID


def test_gettext_untranslated_falls_back_to_source():
    # A string with no French entry falls back to the English source verbatim.
    i18n.current_locale.set("fr")
    assert i18n.use_current_gettext("no such msgid exists here 123") == (
        "no such msgid exists here 123"
    )


def test_gettext_alias_is_the_function():
    assert i18n._ is i18n.use_current_gettext
    assert i18n.ngettext is i18n.use_current_ngettext


# ---------------------------------------------------------------------------
# ngettext (plural selection)
# ---------------------------------------------------------------------------

# These plural msgids exist in the catalogs; the SELECTED index follows each
# locale's Plural-Forms rule (fr: n>1 ; en/NullTranslations: n!=1). n=0 is the tell.
SING = "{n} category"
PLUR = "{n} categories"
FR_SING = "{n} categorie"
FR_PLUR = "{n} categories"


def test_ngettext_english_plural_rule():
    # NullTranslations: singular only when n == 1.
    assert i18n.current_locale.get() == "en"
    assert i18n.use_current_ngettext(SING, PLUR, 1) == SING
    assert i18n.use_current_ngettext(SING, PLUR, 2) == PLUR
    assert i18n.use_current_ngettext(SING, PLUR, 0) == PLUR


def test_ngettext_french_plural_rule():
    # fr Plural-Forms is 'plural=(n > 1)', so 0 and 1 select the singular form.
    i18n.current_locale.set("fr")
    assert i18n.use_current_ngettext(SING, PLUR, 0) == FR_SING
    assert i18n.use_current_ngettext(SING, PLUR, 1) == FR_SING
    assert i18n.use_current_ngettext(SING, PLUR, 2) == FR_PLUR


# ---------------------------------------------------------------------------
# resolve_locale precedence
# ---------------------------------------------------------------------------


def _bot(pool):
    return types.SimpleNamespace(db_pool=pool)


def _serve(pool, *, user=None, guild=None):
    """Serve distinct settings blobs per scope while still recording calls.

    ``user``/``guild`` are the raw JSONB blobs (dict or None) returned for the
    respective settings table.
    """

    async def _fetchval(query, *args):
        pool.calls.append(("fetchval", query, args))
        if "user_settings" in query:
            return user
        if "guild_settings" in query:
            return guild
        return None

    pool.fetchval = _fetchval
    return pool


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # The settings module caches blobs in-process; keep tests independent.
    settings._cache.clear()
    yield
    settings._cache.clear()


async def test_resolve_user_wins(fake_pool, make_interaction):
    _serve(fake_pool, user={"locale": "fr"}, guild={"locale": "ja"})
    interaction = make_interaction(locale="el")
    loc = await i18n.resolve_locale(
        _bot(fake_pool), user_id=10, guild_id=20, interaction=interaction
    )
    assert loc == "fr"
    # Short-circuits: guild settings are never consulted once the user resolves.
    assert not any("guild_settings" in q for _, q, _ in fake_pool.calls)


async def test_resolve_guild_wins_when_user_absent(fake_pool, make_interaction):
    _serve(fake_pool, user=None, guild={"locale": "ja"})
    interaction = make_interaction(locale="el")
    loc = await i18n.resolve_locale(
        _bot(fake_pool), user_id=10, guild_id=20, interaction=interaction
    )
    assert loc == "ja"


async def test_resolve_interaction_locale_when_user_and_guild_absent(
    fake_pool, make_interaction
):
    _serve(fake_pool, user=None, guild=None)
    # Discord-style region code is normalized on the way through.
    interaction = make_interaction(locale="fr-CA")
    loc = await i18n.resolve_locale(
        _bot(fake_pool), user_id=10, guild_id=20, interaction=interaction
    )
    assert loc == "fr"


async def test_resolve_falls_back_to_default(fake_pool, make_interaction):
    _serve(fake_pool, user=None, guild=None)
    # Unmappable interaction locale -> nothing resolves -> DEFAULT_LOCALE.
    interaction = make_interaction(locale="xx")
    loc = await i18n.resolve_locale(
        _bot(fake_pool), user_id=10, guild_id=20, interaction=interaction
    )
    assert loc == i18n.DEFAULT_LOCALE == "en"


async def test_resolve_default_with_no_guild_and_no_interaction(fake_pool):
    _serve(fake_pool, user=None, guild=None)
    loc = await i18n.resolve_locale(
        _bot(fake_pool), user_id=10, guild_id=None, interaction=None
    )
    assert loc == "en"
    # guild_id is None, so the guild branch is skipped entirely.
    assert not any("guild_settings" in q for _, q, _ in fake_pool.calls)


async def test_resolve_invalid_user_setting_is_skipped(fake_pool):
    # An unmappable per-user value must not block the per-guild fallback.
    _serve(fake_pool, user={"locale": "xx"}, guild={"locale": "ja"})
    loc = await i18n.resolve_locale(
        _bot(fake_pool), user_id=10, guild_id=20, interaction=None
    )
    assert loc == "ja"


# ---------------------------------------------------------------------------
# apply_interaction_locale
# ---------------------------------------------------------------------------


async def test_apply_interaction_locale_sets_contextvar(fake_pool, make_interaction):
    _serve(fake_pool, user={"locale": "ja"})
    interaction = make_interaction(user_id=5, guild_id=None, locale="en")
    interaction.client = _bot(fake_pool)

    await i18n.apply_interaction_locale(interaction)
    assert i18n.current_locale.get() == "ja"


async def test_apply_interaction_locale_uses_interaction_locale(
    fake_pool, make_interaction
):
    _serve(fake_pool, user=None, guild=None)
    interaction = make_interaction(user_id=7, guild_id=99, locale="fr")
    interaction.client = _bot(fake_pool)

    await i18n.apply_interaction_locale(interaction)
    assert i18n.current_locale.get() == "fr"


async def test_apply_interaction_locale_falls_back_to_default_on_error(
    make_interaction,
):
    # A bot without db_pool makes resolve_locale raise; apply must swallow it and
    # reset the ContextVar to the default rather than leaking a stale locale.
    i18n.current_locale.set("fr")
    interaction = make_interaction(user_id=3, guild_id=None, locale="fr")
    interaction.client = types.SimpleNamespace()  # no db_pool -> AttributeError

    await i18n.apply_interaction_locale(interaction)
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE == "en"
