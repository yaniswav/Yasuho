"""Unit tests for :func:`tools.premium.guild_status`/:func:`tools.premium.user_status`
(M3c) - the "why" behind ``EntitlementCache.is_guild_premium``/
``has_comfort_pack``'s bare booleans, used by ``/premium``
(cogs/system/premium_panel.py) to say purchase vs gift vs free, and until
when.

No network, DB, Discord: a tiny query-dispatching fake pool
(:class:`_StatusPool`) stands in for asyncpg, same spirit as the repo's
``fake_pool`` fixture but able to answer the TWO different queries
(``premium_entitlements`` and ``premium_grants``) each function makes with
different rows - the shared ``fake_pool`` fixture only configures one
``fetch_return`` for every call, which cannot tell those two apart.
"""

from __future__ import annotations

import datetime

from tools import premium

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)


class _StatusPool:
    """Dispatches ``fetch`` by table name - see the module docstring."""

    def __init__(self, entitlement_rows=(), grant_rows=()):
        self.entitlement_rows = list(entitlement_rows)
        self.grant_rows = list(grant_rows)
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "premium_entitlements" in query:
            return self.entitlement_rows
        if "premium_grants" in query:
            return self.grant_rows
        raise AssertionError(f"unexpected query: {query}")


def _entitlement_row(**overrides):
    row = {"entitlement_id": 1, "sku_id": 111, "ends_at": None, "last_synced_at": NOW}
    row.update(overrides)
    return row


def _grant_row(**overrides):
    row = {
        "id": 1,
        "product": premium.PRODUCT_YASUHO_PLUS,
        "scope_type": "guild",
        "guild_id": 42,
        "user_id": None,
        "reason": None,
        "granted_by": 1,
        "granted_at": NOW,
        "expires_at": None,
        "revoked_at": None,
        "revoked_by": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# guild_status
# ---------------------------------------------------------------------------


async def test_guild_status_free_when_nothing_active(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    pool = _StatusPool(entitlement_rows=[], grant_rows=[])
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status == {"active": False, "source": None, "ends_at": None}


async def test_guild_status_purchase_from_a_configured_sku_entitlement(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    ends_at = NOW + datetime.timedelta(days=10)
    pool = _StatusPool(
        entitlement_rows=[_entitlement_row(sku_id=111, ends_at=ends_at)],
        grant_rows=[],
    )
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status == {"active": True, "source": "purchase", "ends_at": ends_at}


async def test_guild_status_gift_when_only_a_grant_is_active(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    pool = _StatusPool(
        entitlement_rows=[],
        grant_rows=[_grant_row(product=premium.PRODUCT_YASUHO_PLUS, expires_at=None)],
    )
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status == {"active": True, "source": "gift", "ends_at": None}


async def test_guild_status_prefers_purchase_over_gift_when_both_present(monkeypatch):
    """Mirrors EntitlementCache.is_guild_premium's own precedence: a
    configured-SKU Discord entitlement is checked (and returned) before an
    owner grant is ever consulted."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    ends_at = NOW + datetime.timedelta(days=5)
    pool = _StatusPool(
        entitlement_rows=[_entitlement_row(sku_id=111, ends_at=ends_at)],
        grant_rows=[_grant_row(product=premium.PRODUCT_YASUHO_PLUS)],
    )
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status["source"] == "purchase"
    assert status["ends_at"] == ends_at


async def test_guild_status_ignores_an_entitlement_for_a_different_sku(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    pool = _StatusPool(
        entitlement_rows=[_entitlement_row(sku_id=999)],  # some other product
        grant_rows=[],
    )
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status["active"] is False


async def test_guild_status_no_sku_configured_still_sees_the_grant(monkeypatch):
    """The plan's own "owner can gift before any store is open" scenario:
    no [Premium] yasuho_plus_sku at all, but an active grant still counts."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    pool = _StatusPool(
        entitlement_rows=[],
        grant_rows=[_grant_row(product=premium.PRODUCT_YASUHO_PLUS)],
    )
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status == {"active": True, "source": "gift", "ends_at": None}


async def test_guild_status_expired_entitlement_past_grace_is_not_active(monkeypatch):
    """Threads ``now`` through to :func:`tools.premium.is_active` rather than
    re-implementing the ACTIVE rule - a confirmed-ended row past the grace
    window must read as inactive here exactly like it does everywhere else."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    ended = NOW - datetime.timedelta(days=30)
    pool = _StatusPool(
        entitlement_rows=[
            _entitlement_row(sku_id=111, ends_at=ended, last_synced_at=NOW)
        ],
        grant_rows=[],
    )
    status = await premium.guild_status(pool, 42, now=NOW)
    assert status["active"] is False


# ---------------------------------------------------------------------------
# user_status - the Pack Confort twin
# ---------------------------------------------------------------------------


async def test_user_status_free_when_nothing_active(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    pool = _StatusPool(entitlement_rows=[], grant_rows=[])
    status = await premium.user_status(pool, 7, now=NOW)
    assert status == {"active": False, "source": None, "ends_at": None}


async def test_user_status_purchase_from_a_configured_sku_entitlement(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    ends_at = NOW + datetime.timedelta(days=10)
    pool = _StatusPool(
        entitlement_rows=[_entitlement_row(sku_id=222, ends_at=ends_at)],
        grant_rows=[],
    )
    status = await premium.user_status(pool, 7, now=NOW)
    assert status == {"active": True, "source": "purchase", "ends_at": ends_at}


async def test_user_status_gift_when_only_a_grant_is_active(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    pool = _StatusPool(
        entitlement_rows=[],
        grant_rows=[
            _grant_row(
                product=premium.PRODUCT_COMFORT_PACK,
                scope_type="user",
                guild_id=None,
                user_id=7,
                expires_at=NOW + datetime.timedelta(days=1),
            )
        ],
    )
    status = await premium.user_status(pool, 7, now=NOW)
    assert status == {
        "active": True,
        "source": "gift",
        "ends_at": NOW + datetime.timedelta(days=1),
    }
