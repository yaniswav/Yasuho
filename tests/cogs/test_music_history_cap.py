"""Unit tests for the premium-aware per-player history cap (M4a-1,
.claude/plans/monetisation/4-plan-retenu.md).

``cogs/music/player.py``'s ``Player.__init__`` reads the client/channel
``discord.py`` hands it positionally (the class-pass connect form,
``channel.connect(cls=Player)``) to resolve ``HistorySettings.max_items`` from
``bot.premium.for_guild(guild_id).history_max_items`` BEFORE construction, so
these tests exercise :func:`cogs.music.player._resolve_history_max_items`
directly against that exact ``(client, channel)`` shape - no real sonolink
node or voice connection needed.
"""

import types

from cogs.music import player as player_mod


def _client(premium):
    return types.SimpleNamespace(premium=premium)


def _channel(guild_id):
    return types.SimpleNamespace(guild=types.SimpleNamespace(id=guild_id))


class _StubPremium:
    """A minimal ``bot.premium``-shaped stub: per-guild history_max_items only."""

    def __init__(self, by_guild):
        self._by_guild = by_guild

    def for_guild(self, guild_id):
        return types.SimpleNamespace(
            history_max_items=self._by_guild[guild_id]
        )


# ---------------------------------------------------------------------------
# The happy path: a different cap per guild, read from the resolver
# ---------------------------------------------------------------------------


def test_free_guild_gets_the_free_cap():
    premium = _StubPremium({1: player_mod.HISTORY_MAX_ITEMS})
    args = (_client(premium), _channel(1))
    assert player_mod._resolve_history_max_items(args) == player_mod.HISTORY_MAX_ITEMS


def test_premium_guild_gets_the_premium_cap():
    premium = _StubPremium({1: 500})
    args = (_client(premium), _channel(1))
    assert player_mod._resolve_history_max_items(args) == 500


def test_cap_is_per_guild_not_global():
    premium = _StubPremium({1: player_mod.HISTORY_MAX_ITEMS, 2: 500})
    free_args = (_client(premium), _channel(1))
    premium_args = (_client(premium), _channel(2))
    assert (
        player_mod._resolve_history_max_items(free_args)
        != player_mod._resolve_history_max_items(premium_args)
    )
    assert player_mod._resolve_history_max_items(premium_args) == 500


# ---------------------------------------------------------------------------
# Every miss degrades to the free default - a voice connect must never fail
# because the premium resolver had nothing to say.
# ---------------------------------------------------------------------------


def test_no_args_falls_back_to_free():
    assert player_mod._resolve_history_max_items(()) == player_mod.HISTORY_MAX_ITEMS


def test_instance_pass_form_has_no_channel_yet_falls_back_to_free():
    # The instance-pass connect form (unused in this repo today) builds the
    # player with no client/channel at __init__ time at all.
    assert player_mod._resolve_history_max_items((types.SimpleNamespace(),)) == (
        player_mod.HISTORY_MAX_ITEMS
    )


def test_client_with_no_premium_attribute_falls_back_to_free():
    client = types.SimpleNamespace()  # no .premium at all (a bare test double)
    args = (client, _channel(1))
    assert player_mod._resolve_history_max_items(args) == player_mod.HISTORY_MAX_ITEMS


def test_channel_with_no_guild_falls_back_to_free():
    premium = _StubPremium({1: 500})
    channel = types.SimpleNamespace(guild=None)
    args = (_client(premium), channel)
    assert player_mod._resolve_history_max_items(args) == player_mod.HISTORY_MAX_ITEMS


def test_resolver_raising_falls_back_to_free_rather_than_crashing_the_connect():
    class _Explodes:
        def for_guild(self, guild_id):
            raise RuntimeError("boom")

    args = (_client(_Explodes()), _channel(1))
    assert player_mod._resolve_history_max_items(args) == player_mod.HISTORY_MAX_ITEMS


# ---------------------------------------------------------------------------
# Negative control: a resolver that is never consulted would wrongly return
# the free value for every guild, including a premium one - pinning that the
# premium guild's test above actually depends on the resolver being read.
# ---------------------------------------------------------------------------


def test_negative_control_premium_guild_is_not_the_free_value():
    premium = _StubPremium({1: 500})
    args = (_client(premium), _channel(1))
    result = player_mod._resolve_history_max_items(args)
    assert result != player_mod.HISTORY_MAX_ITEMS
    assert result == 500


# ---------------------------------------------------------------------------
# Tied to the real catalog (L1a, 2026-10-07): 200 -> 500. Every test above
# uses a stand-in premium number; this one resolves against the actual
# tools.premium.GUILD_PREMIUM value, so a catalog change shows up here too.
# ---------------------------------------------------------------------------


def test_resolves_the_real_premium_catalog_history_cap():
    from tools import premium as premium_mod

    class _RealResolver:
        def for_guild(self, guild_id):
            return premium_mod.GUILD_PREMIUM

    args = (_client(_RealResolver()), _channel(1))
    result = player_mod._resolve_history_max_items(args)
    assert result == premium_mod.GUILD_PREMIUM.history_max_items
    assert result == 500
