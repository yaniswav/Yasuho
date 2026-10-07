"""LIMIT-REACHED usage counter (L4): tools/premium_upsell.py's
``for_guild_refusal``/``for_user_refusal`` are the single choke point every
wired cog refusal site already calls through (see
tests/tools/test_premium_upsell_sites.py's own call-site census) - this
tests that EVERY call emits one ``LIMIT-REACHED key=... scope=...
premium=0|1`` INFO line, regardless of whether the upsell note itself ends
up shown (an already-top-tier guild/user, a missing id, a 7-day cooldown
still running, a DB error on the claim) - because calling the function at
all already means a refusal happened; see the module's own "LIMIT-REACHED"
docstring paragraph.

No network, no DB: a tiny pool double that always lets the 7-day claim
succeed is enough, since the claim's own semantics are already covered by
tests/tools/test_premium_upsell.py.
"""

from __future__ import annotations

import datetime

from tools import premium_upsell

UTC = datetime.timezone.utc


class _AlwaysClaimPool:
    """fetchrow always returns a row (claim succeeds); execute is a no-op."""

    async def fetchrow(self, query, *args):
        return {"shown_at": datetime.datetime(2026, 1, 8, tzinfo=UTC)}

    async def execute(self, query, *args):
        return "INSERT 0 1"


def _bot(pool=None):
    import types

    return types.SimpleNamespace(db_pool=pool)


def _limit_reached_records(caplog):
    return [r for r in caplog.records if "LIMIT-REACHED" in r.message]


# ---------------------------------------------------------------------------
# Guild-scoped refusals
# ---------------------------------------------------------------------------


async def test_logged_when_guild_already_top_tier_and_nothing_else_shown(caplog):
    bot = _bot()  # no pool at all - already_top_tier short-circuits first
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_guild_refusal(
            bot,
            limit_key="guild_playlists",
            guild_id=999999,
            person_id=1,
            is_admin=True,
            already_top_tier=True,
            benefit="75",
        )
    assert result is None
    records = _limit_reached_records(caplog)
    assert len(records) == 1
    assert "key=guild_playlists" in records[0].message
    assert "scope=guild" in records[0].message
    assert "premium=1" in records[0].message


async def test_logged_with_premium_0_when_the_upsell_also_shows(caplog):
    bot = _bot(_AlwaysClaimPool())
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_guild_refusal(
            bot,
            limit_key="guild_playlists",
            guild_id=999999,
            person_id=1,
            is_admin=True,
            already_top_tier=False,
            benefit="75",
        )
    assert result is not None  # the upsell itself did show
    records = _limit_reached_records(caplog)
    assert len(records) == 1
    assert "premium=0" in records[0].message
    # The upsell's own line is a SEPARATE log record, not folded into this one.
    upsell_records = [r for r in caplog.records if "PREMIUM-UPSELL" in r.message]
    assert len(upsell_records) == 1


async def test_logged_even_when_guild_id_is_missing(caplog):
    bot = _bot(_AlwaysClaimPool())
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_guild_refusal(
            bot,
            limit_key="voice_hubs",
            guild_id=None,
            person_id=1,
            is_admin=True,
            already_top_tier=False,
            benefit="10",
        )
    assert result is None
    assert len(_limit_reached_records(caplog)) == 1


async def test_logged_even_when_the_claim_itself_fails(caplog):
    class _RaisingPool:
        async def fetchrow(self, query, *args):
            raise RuntimeError("database unavailable")

    bot = _bot(_RaisingPool())
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_guild_refusal(
            bot,
            limit_key="role_menus",
            guild_id=42,
            person_id=1,
            is_admin=True,
            already_top_tier=False,
            benefit="50",
        )
    assert result is None  # fail closed on nagging - but the counter still fired
    assert len(_limit_reached_records(caplog)) == 1


async def test_no_guild_or_person_id_appears_in_the_limit_reached_line(caplog):
    bot = _bot(_AlwaysClaimPool())
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        await premium_upsell.for_guild_refusal(
            bot,
            limit_key="guild_playlists",
            guild_id=888777,
            person_id=444555,
            is_admin=True,
            already_top_tier=False,
            benefit="75",
        )
    records = _limit_reached_records(caplog)
    assert len(records) == 1
    assert "888777" not in records[0].message
    assert "444555" not in records[0].message


# ---------------------------------------------------------------------------
# User-scoped refusals
# ---------------------------------------------------------------------------


async def test_logged_for_user_refusal_already_top_tier(caplog):
    bot = _bot()
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_user_refusal(
            bot,
            limit_key="favourites",
            person_id=321,
            already_top_tier=True,
            benefit="300",
        )
    assert result is None
    records = _limit_reached_records(caplog)
    assert len(records) == 1
    assert "key=favourites" in records[0].message
    assert "scope=user" in records[0].message
    assert "premium=1" in records[0].message
    assert "321" not in records[0].message


async def test_logged_for_user_refusal_premium_0_when_shown(caplog):
    bot = _bot(_AlwaysClaimPool())
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_user_refusal(
            bot,
            limit_key="reminders_pending",
            person_id=321,
            already_top_tier=False,
            benefit="60",
        )
    assert result is not None
    records = _limit_reached_records(caplog)
    assert len(records) == 1
    assert "premium=0" in records[0].message


async def test_logged_even_when_person_id_is_missing(caplog):
    bot = _bot(_AlwaysClaimPool())
    with caplog.at_level("INFO", logger=premium_upsell.log.name):
        result = await premium_upsell.for_user_refusal(
            bot,
            limit_key="reminders_recurring",
            person_id=None,
            already_top_tier=False,
            benefit="15",
        )
    assert result is None
    assert len(_limit_reached_records(caplog)) == 1
