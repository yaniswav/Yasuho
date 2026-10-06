"""``/premium`` (cogs/system/premium_panel.py, M3c).

No network, DB or Discord gateway: :class:`PremiumPanelView` is built
directly with plain Python stand-ins (a status dict is exactly what
:func:`tools.premium.guild_status`/``user_status`` return - see
tests/tools/test_premium_status.py for those), and every component is read
back with ``view.walk_children()`` (proven in a throwaway script to recurse
into Container/ActionRow and expose ``TextDisplay.content`` /
``Button.sku_id`` / ``Button.url``).

Covers:
1. the catalog comparison text is read ENTIRELY from the GuildLimits/
   UserLimits passed in, never a hardcoded number - with the mandated
   negative control proving that check is not vacuous;
2. the purchase buttons: shown only for a configured SKU, the guild one
   gated to Manage Server, the DM variant (no guild block at all);
3. status line wording for purchase / gift / free, both scopes.
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
# 1. The catalog comparison - built ONLY from the limits passed in
# ---------------------------------------------------------------------------


def _synthetic_guild_limits(premium_tier):
    """Distinctive, ceiling-safe values - see tools.premium.GUILD_CEILINGS.

    GLOBALLY unique across every field of both tiers (not merely "far from
    the real catalog"): with :func:`_has_number`'s word-boundary matching,
    the only way a wrong field's number could pass is a plain DUPLICATE
    value elsewhere in the set, so every one of the 20 guild + 6 user
    numbers this module hands out is distinct from all the others - see
    test_negative_control_a_hardcoded_number_fails_the_catalog_check, which
    would not actually prove anything with a repeated number in the mix.
    """
    if premium_tier:
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
            serverstats_retention_days=365,
            music_247=True,
            premium_badge=True,
        )
    return premium.GuildLimits(
        max_guild_playlists=21,
        max_playlist_tracks=521,
        history_max_items=121,
        max_feeds_per_guild=3,
        max_follows_per_feed=41,
        max_subs_per_feed=141,
        max_menus_per_guild=51,
        max_hubs=5,
        max_tickets_open_per_user=9,
        serverstats_retention_days=161,
        music_247=False,
        premium_badge=False,
    )


def _synthetic_user_limits(premium_tier):
    if premium_tier:
        return premium.UserLimits(
            max_favourites=329, max_pending_reminders=59, max_recurring_reminders=13
        )
    return premium.UserLimits(
        max_favourites=29, max_pending_reminders=39, max_recurring_reminders=7
    )


def test_catalog_text_reads_every_number_from_the_limits_given():
    guild_free = _synthetic_guild_limits(False)
    guild_plus = _synthetic_guild_limits(True)
    user_free = _synthetic_user_limits(False)
    user_plus = _synthetic_user_limits(True)
    text = pp._catalog_text(guild_free, guild_plus, user_free, user_plus)

    for limits in (guild_free, guild_plus):
        for field in (
            "max_guild_playlists",
            "max_playlist_tracks",
            "history_max_items",
            "max_feeds_per_guild",
            "max_follows_per_feed",
            "max_subs_per_feed",
            "max_menus_per_guild",
            "max_hubs",
            "max_tickets_open_per_user",
            "serverstats_retention_days",
        ):
            assert _has_number(text, getattr(limits, field)), field
    for limits in (user_free, user_plus):
        for field in ("max_favourites", "max_pending_reminders", "max_recurring_reminders"):
            assert _has_number(text, getattr(limits, field)), field

    # AniList is worded "per feed" (the plan's own requirement), not as a
    # bare total that could be misread as server-wide.
    assert "per feed" in text
    # 24/7 music is the first comparison line (plan: "argument principal").
    assert text.index("24/7 music") < text.index("Server playlists")
    # Yasuho+ before Pack Confort, in that order.
    assert text.index("Yasuho+") < text.index("Pack Confort")


def _catalog_text_with_hardcoded_playlist_count(guild_free, guild_plus, user_free, user_plus):
    """A deliberately-broken clone of :func:`pp._catalog_text`'s first
    numeric line: "Server playlists" hardcodes the real production catalog's
    free-tier number (25) instead of reading ``guild_free.max_guild_playlists``
    - the exact defect class the positive test above exists to catch.
    Everything else is unchanged (delegates to the real function and only
    patches this one line), so this is a faithful stand-in for "someone
    hardcoded one number" rather than a wholesale rewrite.
    """
    text = pp._catalog_text(guild_free, guild_plus, user_free, user_plus)
    correct_line = pp._(
        "**Server playlists** - Free: {free_count} x {free_tracks} "
        "tracks | Yasuho+: {plus_count} x {plus_tracks} tracks"
    ).format(
        free_count=guild_free.max_guild_playlists,
        free_tracks=guild_free.max_playlist_tracks,
        plus_count=guild_plus.max_guild_playlists,
        plus_tracks=guild_plus.max_playlist_tracks,
    )
    broken_line = pp._(
        "**Server playlists** - Free: {free_count} x {free_tracks} "
        "tracks | Yasuho+: {plus_count} x {plus_tracks} tracks"
    ).format(
        free_count=25,  # HARDCODED - the bug
        free_tracks=guild_free.max_playlist_tracks,
        plus_count=guild_plus.max_guild_playlists,
        plus_tracks=guild_plus.max_playlist_tracks,
    )
    assert correct_line in text, "fixture drifted from pp._catalog_text's own wording"
    return text.replace(correct_line, broken_line)


def test_negative_control_a_hardcoded_number_fails_the_catalog_check():
    """Prove the check above is not vacuous: against the broken clone, the
    exact same assertion the positive test makes for this field now fails."""
    guild_free = _synthetic_guild_limits(False)
    guild_plus = _synthetic_guild_limits(True)
    user_free = _synthetic_user_limits(False)
    user_plus = _synthetic_user_limits(True)
    broken_text = _catalog_text_with_hardcoded_playlist_count(
        guild_free, guild_plus, user_free, user_plus
    )

    with pytest.raises(AssertionError):
        assert _has_number(broken_text, guild_free.max_guild_playlists)


def test_view_renders_the_real_module_catalog():
    """The command's own wiring (_build) reads premium.GUILD_FREE etc., not
    some other source - catch a view that silently stopped using the live
    catalog."""
    view = pp.PremiumPanelView(
        _author(),
        guild=None,
        guild_status=None,
        user_status=FREE_STATUS,
        can_manage_guild=False,
    )
    text = "\n".join(_text_displays(view))
    assert _has_number(text, premium.GUILD_FREE.max_guild_playlists)
    assert _has_number(text, premium.GUILD_PREMIUM.max_guild_playlists)
    assert _has_number(text, premium.USER_FREE.max_favourites)
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
# 3. Status line wording - purchase / gift / free, both scopes
# ---------------------------------------------------------------------------


def test_guild_status_text_not_active():
    assert "does not have" in pp._guild_status_text(FREE_STATUS)


def test_guild_status_text_purchase_with_end_date():
    status = {"active": True, "source": "purchase", "ends_at": NOW}
    text = pp._guild_status_text(status)
    assert "purchased" in text
    assert "until" in text


def test_guild_status_text_gift_with_no_end_date():
    status = {"active": True, "source": "gift", "ends_at": None}
    text = pp._guild_status_text(status)
    assert "gifted" in text
    assert "no end date" in text


def test_user_status_text_not_active():
    assert "do not have" in pp._user_status_text(FREE_STATUS)


def test_user_status_text_purchase_with_end_date():
    status = {"active": True, "source": "purchase", "ends_at": NOW}
    text = pp._user_status_text(status)
    assert "purchased" in text
    assert "until" in text


def test_user_status_text_gift_with_no_end_date():
    status = {"active": True, "source": "gift", "ends_at": None}
    text = pp._user_status_text(status)
    assert "gifted" in text
    assert "no end date" in text
