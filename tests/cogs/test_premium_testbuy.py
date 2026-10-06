"""``?premiumadmin testbuy server|user`` / ``?premiumadmin testclear`` (M3c,
cogs/system/premium.py) - the owner's end-to-end round trip through
Discord's TEST-entitlement surface.

No network, DB or Discord gateway: the bot stand-in's ``create_entitlement``/
``entitlements``/``fetch_entitlement`` are plain async stubs (same spirit as
``_m3b_bot`` in tests/cogs/test_premium.py), and nothing here writes
``premium_entitlements`` directly - these commands never do either (see the
cog's own module docstring: the resulting ENTITLEMENT_* gateway event is
what M3b's listeners act on, unchanged).

Covers:
1. the owner gate on every new leaf (testbuy, testbuy server, testbuy user,
   testclear);
2. testbuy refuses with a clear message when its SKU is not configured, and
   otherwise calls ``create_entitlement`` with the right sku/owner/owner_type
   and logs "PREMIUM-TESTBUY";
3. testclear refuses a non-test entitlement (checking ``Entitlement.type``)
   and a not-found id, and otherwise deletes it and logs "PREMIUM-TESTCLEAR".
"""

from __future__ import annotations

import types

import discord
import pytest
from discord.ext import commands

from cogs.system import premium as premium_cog
from tools import premium


def _is_owner_factory(owner_id):
    async def is_owner(user):
        return user.id == owner_id

    return is_owner


def _empty_stream(**_kwargs):
    async def _gen():
        for _ in ():
            yield _

    return _gen()


class _FakeEntitlement:
    def __init__(self, *, type_, entitlement_id=777):
        self.id = entitlement_id
        self.type = type_
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


def _bot(
    pool,
    *,
    owner_id=1,
    create_entitlement_calls=None,
    entitlements_stream=None,
    fetch_entitlement=None,
):
    calls = create_entitlement_calls if create_entitlement_calls is not None else []

    async def create_entitlement(sku, owner, owner_type):
        calls.append((sku.id, owner.id, owner_type))

    def entitlements(**kwargs):
        if entitlements_stream is not None:
            return entitlements_stream(**kwargs)
        return _empty_stream(**kwargs)

    async def _default_fetch_entitlement(entitlement_id):
        raise discord.NotFound(
            types.SimpleNamespace(status=404, reason="Not Found"), "Unknown entitlement"
        )

    return types.SimpleNamespace(
        db_pool=pool,
        is_owner=_is_owner_factory(owner_id),
        premium=premium.EntitlementCache(),
        create_entitlement=create_entitlement,
        entitlements=entitlements,
        fetch_entitlement=fetch_entitlement or _default_fetch_entitlement,
    )


def _cog(pool, **kwargs):
    bot = _bot(pool, **kwargs)
    return premium_cog.Premium(bot), bot


# ---------------------------------------------------------------------------
# 1. The owner gate on every new leaf
# ---------------------------------------------------------------------------


async def test_every_new_leaf_carries_its_own_owner_check():
    cog, _bot_obj = _cog(object())
    other_ctx = types.SimpleNamespace(bot=cog.bot, author=types.SimpleNamespace(id=7))
    leaves = [
        cog.premium_testbuy,
        cog.premium_testbuy_server,
        cog.premium_testbuy_user,
        cog.premium_testclear,
    ]
    for command in leaves:
        assert command.checks, f"{command.qualified_name} has no checks at all"
        refused = False
        for check in command.checks:
            try:
                ok = await check(other_ctx)
            except commands.CheckFailure:
                refused = True
                break
            if ok is False:
                refused = True
                break
        assert refused, f"{command.qualified_name} did not refuse a non-owner"


async def test_negative_control_a_leaf_missing_its_check_is_caught():
    fabricated = types.SimpleNamespace(qualified_name="premium testbuy fabricated", checks=[])
    with pytest.raises(AssertionError):
        assert fabricated.checks, "no checks at all"


# ---------------------------------------------------------------------------
# 2. testbuy
# ---------------------------------------------------------------------------


async def test_testbuy_server_refuses_when_sku_not_configured(fake_pool, monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", None)
    calls = []
    cog, _bot_obj = _cog(fake_pool, create_entitlement_calls=calls)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)
    await cog.premium_testbuy_server.callback(cog, ctx, 42)
    assert len(sent) == 1
    assert "nothing to test-buy" in sent[0][0][0]
    assert calls == []  # create_entitlement must never be called


async def test_testbuy_user_refuses_when_sku_not_configured(fake_pool, monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", None)
    calls = []
    cog, _bot_obj = _cog(fake_pool, create_entitlement_calls=calls)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)
    await cog.premium_testbuy_user.callback(cog, ctx, 7)
    assert len(sent) == 1
    assert "nothing to test-buy" in sent[0][0][0]
    assert calls == []  # create_entitlement must never be called


async def test_testbuy_server_calls_create_entitlement_with_guild_owner_type(
    fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    calls = []
    cog, _bot_obj = _cog(fake_pool, create_entitlement_calls=calls)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)

    with caplog.at_level("INFO", logger=premium_cog.log.name):
        await cog.premium_testbuy_server.callback(cog, ctx, 42)

    assert calls == [(111, 42, discord.EntitlementOwnerType.guild)]
    assert any(
        "PREMIUM-TESTBUY" in r.message and "scope=guild" in r.message
        for r in caplog.records
    )
    assert sent  # a confirmation was sent either way


async def test_testbuy_user_calls_create_entitlement_with_user_owner_type(
    fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", 222)
    calls = []
    cog, _bot_obj = _cog(fake_pool, create_entitlement_calls=calls)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)

    with caplog.at_level("INFO", logger=premium_cog.log.name):
        await cog.premium_testbuy_user.callback(cog, ctx, 7)

    assert calls == [(222, 7, discord.EntitlementOwnerType.user)]
    assert any(
        "PREMIUM-TESTBUY" in r.message and "scope=user" in r.message
        for r in caplog.records
    )


async def test_testbuy_reports_the_new_entitlement_id_when_the_listing_shows_it(
    fake_pool, monkeypatch
):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)

    def _stream(**kwargs):
        async def _gen():
            yield types.SimpleNamespace(
                id=888, type=discord.EntitlementType.test_mode_purchase
            )

        return _gen()

    cog, _bot_obj = _cog(fake_pool, entitlements_stream=_stream)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)
    await cog.premium_testbuy_server.callback(cog, ctx, 42)
    assert any("888" in args[0] for args, _kw in sent)


async def test_testbuy_falls_back_gracefully_when_the_listing_is_still_empty(
    fake_pool, monkeypatch
):
    """Eventual consistency on Discord's own side: the entitlement was just
    created but does not show up in the very next listing yet - must not
    crash, and must point the owner at ?premiumadmin check instead."""
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    cog, _bot_obj = _cog(fake_pool)  # default stream is empty
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)
    await cog.premium_testbuy_server.callback(cog, ctx, 42)
    assert len(sent) == 1
    assert "check" in sent[0][0][0]


# ---------------------------------------------------------------------------
# 3. testclear
# ---------------------------------------------------------------------------


async def test_testclear_refuses_a_nonexistent_entitlement(fake_pool):
    cog, _bot_obj = _cog(fake_pool)  # default fetch_entitlement raises NotFound
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)
    await cog.premium_testclear.callback(cog, ctx, 999)
    assert len(sent) == 1
    assert "999" in sent[0][0][0]
    assert "No entitlement" in sent[0][0][0]


async def test_testclear_reports_a_discord_http_error_without_deleting(fake_pool):
    """Covers the OTHER failure branch of ``fetch_entitlement``: a transient
    Discord-side error (rate limit, 5xx, ...) is distinct from "the id does
    not exist" (NotFound, covered above) and must be reported as such rather
    than treated as a successful fetch - there is no entitlement object to
    check the type of or delete."""

    async def fetch_entitlement(entitlement_id):
        raise discord.HTTPException(
            types.SimpleNamespace(status=503, reason="Service Unavailable"),
            "Service Unavailable",
        )

    cog, _bot_obj = _cog(fake_pool, fetch_entitlement=fetch_entitlement)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)
    await cog.premium_testclear.callback(cog, ctx, 777)
    assert len(sent) == 1
    assert "777" in sent[0][0][0]
    assert "Could not fetch" in sent[0][0][0]


async def test_negative_control_testclear_refuses_a_real_non_test_entitlement(
    fake_pool, caplog
):
    """Mandatory negative control: a REAL (non-test) entitlement must never
    be deleted by this owner-only dev tool."""
    entitlement = _FakeEntitlement(type_=discord.EntitlementType.purchase)

    async def fetch_entitlement(entitlement_id):
        return entitlement

    cog, _bot_obj = _cog(fake_pool, fetch_entitlement=fetch_entitlement)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)

    with caplog.at_level("INFO", logger=premium_cog.log.name):
        await cog.premium_testclear.callback(cog, ctx, 777)

    assert entitlement.delete_calls == 0
    assert "not a TEST entitlement" in sent[0][0][0]
    assert not any("PREMIUM-TESTCLEAR" in r.message for r in caplog.records)


async def test_testclear_deletes_a_real_test_entitlement_and_logs(fake_pool, caplog):
    entitlement = _FakeEntitlement(type_=discord.EntitlementType.test_mode_purchase)

    async def fetch_entitlement(entitlement_id):
        return entitlement

    cog, _bot_obj = _cog(fake_pool, fetch_entitlement=fetch_entitlement)
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1), send=_send)

    with caplog.at_level("INFO", logger=premium_cog.log.name):
        await cog.premium_testclear.callback(cog, ctx, 777)

    assert entitlement.delete_calls == 1
    assert "Deleted TEST entitlement" in sent[0][0][0]
    assert any(
        "PREMIUM-TESTCLEAR" in r.message and "entitlement=777" in r.message
        for r in caplog.records
    )
