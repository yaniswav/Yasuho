"""``/premium`` (cogs/system/premium_panel.py, M3c, redesigned 2026-10-07 - L2
of the premium adjustments).

No network, DB or Discord gateway: :class:`PremiumPanelView` is built
directly with plain Python stand-ins (a status dict is exactly what
:func:`tools.premium.guild_status`/``user_status`` return - see
tests/tools/test_premium_status.py for those), and every component is read
back with ``view.walk_children()`` (proven in a throwaway script to recurse
into Container/ActionRow and expose ``TextDisplay.content`` /
``Button.sku_id`` / ``Button.url``).

Covers:
1. the pitch text is read ENTIRELY from the GuildLimits/UserLimits passed
   in, never a hardcoded number - with the mandated negative control proving
   that check is not vacuous; the three headline benefits are present and
   the ticket caps (per-member AND the internal per-server one) are not;
2. the purchase buttons: shown only for a configured SKU, the guild one
   gated to Manage Server, the DM variant (no guild block at all);
3. "Premium is not on sale yet" appears AT MOST ONCE, even when both
   products lack a SKU;
4. status line wording: purchase / free keep their existing shape, a GIFT
   gets its own "offered to this server/you" wording (permanent vs dated).
"""

from __future__ import annotations

import datetime
import re
import types

import discord
import pytest

from cogs.system import premium_panel as pp
from tools import premium

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

FREE_STATUS = {"active": False, "source": None, "ends_at": None}


def _author(user_id=1):
    return types.SimpleNamespace(id=user_id)


def _text_displays(view):
    return [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]


def _buttons(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def _has_number(text, value):
    """Whether ``value`` appears in ``text`` as a whole number, not merely
    as a substring of a longer one (e.g. plain ``"17" in text`` is also True
    for "117", "317" ... - exactly the false-positive a synthetic-number
    catalog check must not have)."""
    return re.search(rf"(?<!\d){re.escape(str(value))}(?!\d)", text) is not None


# ---------------------------------------------------------------------------
# 1. The pitch text - built ONLY from the limits passed in
# ---------------------------------------------------------------------------


def _synthetic_guild_plus():
    """Distinctive, ceiling-safe values (tools.premium.GUILD_CEILINGS) - every
    field used by :func:`pp._catalog_text` is globally unique, so
    :func:`_has_number`'s word-boundary matching cannot pass by accident on
    a wrong field (see test_negative_control_a_hardcoded_number_fails_the_catalog_check,
    which would not actually prove anything with a repeated number in the
    mix)."""
    return premium.GuildLimits(
        max_guild_playlists=93,
        max_playlist_tracks=921,
        history_max_items=321,
        max_feeds_per_guild=11,
        max_follows_per_feed=81,
        max_subs_per_feed=181,
        max_menus_per_guild=71,
        max_hubs=17,
        max_tickets_open_per_user=19,
        max_tickets_open_per_guild=219,
        serverstats_retention_days=365,
        music_247=True,
        premium_badge=True,
    )


def _synthetic_user_plus():
    return premium.UserLimits(
        max_favourites=329, max_pending_reminders=59, max_recurring_reminders=13
    )


def test_catalog_text_reads_the_headline_numbers_from_the_limits_given():
    guild_plus = _synthetic_guild_plus()
    user_plus = _synthetic_user_plus()
    text = pp._catalog_text(guild_plus, user_plus)

    for field in (
        "max_guild_playlists",
        "max_playlist_tracks",
        "history_max_items",
        "max_feeds_per_guild",
        "max_menus_per_guild",
        "max_hubs",
        "serverstats_retention_days",
    ):
        assert _has_number(text, getattr(guild_plus, field)), field
    for field in ("max_favourites", "max_pending_reminders", "max_recurring_reminders"):
        assert _has_number(text, getattr(user_plus, field)), field

    # 24/7 music is the first headline benefit (plan: "argument principal").
    assert text.index("24/7 music") < text.index("Extended server playlists")
    assert text.index("Extended server playlists") < text.index("A year of statistics")
    # Yasuho+ before Pack Confort, in that order.
    assert text.index("Yasuho+") < text.index("Pack Confort")


def test_catalog_text_never_mentions_either_ticket_cap():
    """Neither the per-member cap NOR the internal per-server cap may ever
    appear on this panel - the member one is an admin setting, not a sales
    point, and the server one must never be advertised at all."""
    guild_plus = _synthetic_guild_plus()
    user_plus = _synthetic_user_plus()
    text = pp._catalog_text(guild_plus, user_plus)

    assert not _has_number(text, guild_plus.max_tickets_open_per_user)
    assert not _has_number(text, guild_plus.max_tickets_open_per_guild)
    assert "ticket" not in text.lower()


def _catalog_text_with_hardcoded_playlist_count(guild_plus, user_plus):
    """A deliberately-broken clone of :func:`pp._catalog_text`'s playlist
    line: it hardcodes the real production catalog's Yasuho+ number (75)
    instead of reading ``guild_plus.max_guild_playlists`` - the exact defect
    class the positive test above exists to catch. Everything else is
    unchanged (delegates to the real function and only patches this one
    line), so this is a faithful stand-in for "someone hardcoded one
    number" rather than a wholesale rewrite.
    """
    text = pp._catalog_text(guild_plus, user_plus)
    correct_line = pp._("Up to {count} playlists of {tracks} tracks.").format(
        count=guild_plus.max_guild_playlists,
        tracks=guild_plus.max_playlist_tracks,
    )
    broken_line = pp._("Up to {count} playlists of {tracks} tracks.").format(
        count=75,  # HARDCODED - the bug
        tracks=guild_plus.max_playlist_tracks,
    )
    assert correct_line in text, "fixture drifted from pp._catalog_text's own wording"
    return text.replace(correct_line, broken_line)


def test_negative_control_a_hardcoded_number_fails_the_catalog_check():
    """Prove the check above is not vacuous: against the broken clone, the
    exact same assertion the positive test makes for this field now fails."""
    guild_plus = _synthetic_guild_plus()
    user_plus = _synthetic_user_plus()
    broken_text = _catalog_text_with_hardcoded_playlist_count(guild_plus, user_plus)

    with pytest.raises(AssertionError):
        assert _has_number(broken_text, guild_plus.max_guild_playlists)


def test_view_renders_the_real_module_catalog():
    """The command's own wiring (_build) reads premium.GUILD_PREMIUM etc.,
    not some other source - catch a view that silently stopped using the
    live catalog."""
    view = pp.PremiumPanelView(
        _author(),
        guild=None,
        guild_status=None,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    text = "\n".join(_text_displays(view))
    assert _has_number(text, premium.GUILD_PREMIUM.max_guild_playlists)
    assert _has_number(text, premium.USER_PREMIUM.max_favourites)


# ---------------------------------------------------------------------------
# 2. Purchase buttons
# ---------------------------------------------------------------------------


def test_guild_button_shown_to_a_manage_server_member(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=True,
    )
    sku_ids = {b.sku_id for b in _buttons(view) if b.sku_id is not None}
    assert 111 in sku_ids


def test_negative_control_guild_button_hidden_from_a_non_admin(monkeypatch):
    """Mandatory negative control: a member WITHOUT Manage Server must never
    see the Yasuho+ purchase button, only the admin-hint text."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    sku_ids = {b.sku_id for b in _buttons(view) if b.sku_id is not None}
    assert 111 not in sku_ids
    text = "\n".join(_text_displays(view))
    assert pp.COMMAND_NAME in text


def test_guild_block_absent_entirely_without_a_sku_configured(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=True,
    )
    sku_ids = {b.sku_id for b in _buttons(view) if b.sku_id is not None}
    assert sku_ids == set()
    text = "\n".join(_text_displays(view))
    assert "not on sale yet" in text


def test_comfort_pack_button_shown_when_its_sku_is_configured(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    view = pp.PremiumPanelView(
        _author(),
        guild=None,
        guild_status=None,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    sku_ids = {b.sku_id for b in _buttons(view) if b.sku_id is not None}
    assert sku_ids == {222}


def test_each_product_button_is_independent_of_the_other_skus_configuration(
    monkeypatch,
):
    """Yasuho+ configured, Pack Confort not (the plan's own rollout order:
    Yasuho+ first) - each product's block reflects only its OWN sku."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=True,
    )
    sku_ids = {b.sku_id for b in _buttons(view) if b.sku_id is not None}
    assert sku_ids == {111}
    text = "\n".join(_text_displays(view))
    assert "not on sale yet" in text  # the Pack Confort block, still closed


def test_terms_link_button_is_present_and_points_at_terms_md():
    view = pp.PremiumPanelView(
        _author(),
        guild=None,
        guild_status=None,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    links = [b for b in _buttons(view) if b.url is not None]
    assert any(b.url == pp.TERMS_URL for b in links)
    assert "TERMS.md" in pp.TERMS_URL


# ---------------------------------------------------------------------------
# 3. "Premium is not on sale yet" appears AT MOST ONCE
# ---------------------------------------------------------------------------


def test_not_on_sale_appears_exactly_once_when_neither_sku_is_configured(
    monkeypatch,
):
    """The bug this lot fixes: with NEITHER [Premium] SKU configured (today's
    actual posture - no section in bot.ini at all), the panel used to add
    the same sentence twice, once per product block."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=True,
    )
    texts = _text_displays(view)
    occurrences = sum(1 for t in texts if "not on sale yet" in t)
    assert occurrences == 1


def test_not_on_sale_still_appears_once_with_only_one_sku_missing(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=True,
    )
    texts = _text_displays(view)
    occurrences = sum(1 for t in texts if "not on sale yet" in t)
    assert occurrences == 1


# --- Negative control: without the dedup guard, the line appears twice ----
#
# Verified by hand during this lot: removing the ``if not not_on_sale_shown``
# guard from PremiumPanelView._build (calling ``container.add_item(...)``
# unconditionally in both branches instead of through ``_add_not_on_sale``)
# turned test_not_on_sale_appears_exactly_once_when_neither_sku_is_configured
# red - two TextDisplay items containing "not on sale yet" instead of one.
# Restored immediately after by editing the file back (never git
# stash/checkout/reset), and the full panel test suite was re-run green. See
# this report's "negative controls" section for the exact edit and the
# failure it produced.


# ---------------------------------------------------------------------------
# DM variant - guild=None drops the WHOLE guild block (status + button)
# ---------------------------------------------------------------------------


def test_dm_variant_has_no_guild_status_or_guild_button(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    view = pp.PremiumPanelView(
        _author(),
        guild=None,
        guild_status=None,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    text = "\n".join(_text_displays(view))
    assert "This server" not in text
    sku_ids = {b.sku_id for b in _buttons(view) if b.sku_id is not None}
    assert sku_ids == {222}  # Pack Confort only - no Yasuho+ button in a DM


def test_guild_variant_shows_both_status_lines(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    view = pp.PremiumPanelView(
        _author(),
        guild=object(),
        guild_status=FREE_STATUS,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    text = "\n".join(_text_displays(view))
    assert "This server" in text
    assert "Pack Confort" in text


# ---------------------------------------------------------------------------
# 4. Status line wording - purchase / gift / free, both scopes
# ---------------------------------------------------------------------------


def test_guild_status_text_not_active():
    assert "does not have" in pp._guild_status_text(FREE_STATUS)


def test_guild_status_text_purchase_with_end_date():
    """Purchase wording is UNCHANGED by this lot."""
    status = {"active": True, "source": "purchase", "ends_at": NOW}
    text = pp._guild_status_text(status)
    assert "purchased" in text
    assert "until" in text


def test_guild_status_text_purchase_with_no_end_date():
    status = {"active": True, "source": "purchase", "ends_at": None}
    text = pp._guild_status_text(status)
    assert "purchased" in text
    assert "no end date" in text


def test_guild_status_text_gift_with_no_end_date_reads_permanent():
    """L2's new gift wording: 'offered to this server', not '(gifted)'."""
    status = {"active": True, "source": "gift", "ends_at": None}
    text = pp._guild_status_text(status)
    assert "offered to this server" in text
    assert "permanent" in text
    assert "gifted" not in text


def test_guild_status_text_gift_with_an_end_date_names_it():
    status = {"active": True, "source": "gift", "ends_at": NOW}
    text = pp._guild_status_text(status)
    assert "offered to this server" in text
    assert "until" in text
    assert "permanent" not in text


def test_user_status_text_not_active():
    assert "do not have" in pp._user_status_text(FREE_STATUS)


def test_user_status_text_purchase_with_end_date():
    status = {"active": True, "source": "purchase", "ends_at": NOW}
    text = pp._user_status_text(status)
    assert "purchased" in text
    assert "until" in text


def test_user_status_text_gift_with_no_end_date_reads_permanent():
    status = {"active": True, "source": "gift", "ends_at": None}
    text = pp._user_status_text(status)
    assert "offered to you" in text
    assert "permanent" in text
    assert "gifted" not in text


def test_user_status_text_gift_with_an_end_date_names_it():
    status = {"active": True, "source": "gift", "ends_at": NOW}
    text = pp._user_status_text(status)
    assert "offered to you" in text
    assert "until" in text
    assert "permanent" not in text
