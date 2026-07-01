"""Tiny smoke test proving the pytest foundation is wired correctly.

It imports the heaviest modules (which pull in the sonolink stub, config
bootstrap, and the i18n catalogs), then exercises the two boundary fixtures.
Kept intentionally small - real coverage lands in later phases.
"""

import core  # noqa: F401  (import-time config reads + sonolink)
from cogs.music import music  # noqa: F401  (def-time sonolink annotations)
from tools import embed_creator, i18n, interactions  # noqa: F401


async def test_async_runs():
    assert True


async def test_make_interaction(make_interaction):
    itx = make_interaction(user_id=42, guild_id=7)
    assert itx.response.is_done() is False
    assert itx.user.id == 42
    assert itx.guild_id == 7
    await itx.response.send_message("hi", ephemeral=True)
    assert itx.response.is_done() is True
    assert itx.sent == [(("hi",), {"ephemeral": True})]


async def test_fake_pool(fake_pool):
    fake_pool.fetchval_return = 5
    value = await fake_pool.fetchval("SELECT 1", 10)
    assert value == 5
    assert fake_pool.calls == [("fetchval", "SELECT 1", (10,))]
