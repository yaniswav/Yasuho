"""``core.Yasuho._load_premium_cache``: a load failure must never crash boot.

``self.premium`` is built empty in ``__init__`` (FREE for everyone, today's
behaviour); this is the wrapper setup_hook calls right after
``load_eager_caches()`` to fill it in, and the one property that matters is
that a database failure here degrades to "everybody stays free" with a
logged exception, not a crashed boot - the same fail-closed direction the
module docstring states for a commercial benefit (nobody is undercharged by
a bug).
"""

from __future__ import annotations

import logging

import core

from tools import premium


class _BoomingPool:
    """Fails every read, exactly like a database that is unreachable."""

    async def fetch(self, *args, **kwargs):
        raise ConnectionError("database unreachable")


async def test_a_load_failure_logs_and_leaves_everyone_free(caplog):
    bot = core.Yasuho(db_pool=_BoomingPool())
    assert isinstance(bot.premium, premium.EntitlementCache)

    with caplog.at_level(logging.ERROR, logger=core.log.name):
        await bot._load_premium_cache()

    assert any(
        "Failed to load premium" in record.message for record in caplog.records
    )
    # Still resolves FREE for anyone - the cache was never partially filled.
    assert bot.premium.is_guild_premium(1) is False
    assert bot.premium.for_guild(1) == premium.GUILD_FREE
    assert bot.premium.has_comfort_pack(1) is False
    assert bot.premium.for_user(1) == premium.USER_FREE


async def test_a_successful_load_populates_the_cache(monkeypatch):
    class _OkPool:
        async def fetch(self, query, *args):
            if "FROM premium_entitlements" in query:
                return []
            if "FROM premium_grants" in query:
                return [
                    {
                        "id": 1,
                        "product": "yasuho_plus",
                        "scope_type": "guild",
                        "guild_id": 111,
                        "user_id": None,
                        "revoked_at": None,
                        "expires_at": None,
                    }
                ]
            raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    bot = core.Yasuho(db_pool=_OkPool())

    await bot._load_premium_cache()

    assert bot.premium.is_guild_premium(111) is True
