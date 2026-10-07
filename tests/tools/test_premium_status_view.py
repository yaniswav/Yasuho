"""Tests for the ``premium_status`` SQL view (schema.sql) - the dashboard
bridge's read-only "is this guild/user premium right now" surface built on
top of ``premium_entitlements``/``premium_grants``/``premium_skus``.

Two layers, same split the rest of this repo's DB-adjacent tests use:

1. A PURE test (always runs, no DB) that the view's own SQL text still
   carries the SAME 48-hour grace window as :data:`tools.premium.GRACE` -
   so a change to one is never silently out of sync with the other.
2. A LIVE-Postgres equivalence test, skipped unless ``YASUHO_TEST_PG_DSN``
   is set (this repo has no other DB-backed test convention to follow - see
   tests/tools/test_fixups.py, the closest precedent, which fakes asyncpg
   entirely). It connects with asyncpg, applies schema.sql into a fresh,
   disposable database, inserts a fixture matrix covering every branch of
   :func:`tools.premium.is_active`/:func:`tools.premium.is_grant_active`,
   and asserts the view's ``active`` column agrees with those same Python
   functions computed from the identical rows - row shapes the view's own
   schema.sql comment documents.

Never touches the production database: the DSN is only ever a throwaway
Postgres (the project's PG11 probe container), never read from a secret or
a real deployment config.
"""

from __future__ import annotations

import datetime
import os
import re
import uuid

import pytest

from tools import premium

UTC = datetime.timezone.utc

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "schema.sql")


# ---------------------------------------------------------------------------
# Pure test: the view's own text must cite the SAME grace constant tools.premium
# does, not an independently-typed "48 hours" that can drift out of sync.
# ---------------------------------------------------------------------------


def test_view_sql_grace_constant_matches_tools_premium_GRACE():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fp:
        schema_text = fp.read()

    # Pull every occurrence of an INTERVAL '<N> hours' literal that appears
    # between the view's own markers, so this test fails loudly (not
    # silently passing on zero matches) if the view's SQL is ever rewritten
    # without an explicit grace literal, or moved/renamed.
    start = schema_text.index("CREATE OR REPLACE VIEW premium_status")
    end = schema_text.index(";", schema_text.rindex("LEFT JOIN grant_agg", start))
    view_sql = schema_text[start:end]

    matches = re.findall(r"INTERVAL '(\d+) hours'", view_sql)
    assert matches, "premium_status view must spell out its grace window as an INTERVAL literal"
    grace_hours_in_sql = {int(value) for value in matches}

    expected_hours = premium.GRACE.total_seconds() / 3600
    assert expected_hours == int(expected_hours), "GRACE is assumed to be a whole number of hours"
    assert grace_hours_in_sql == {int(expected_hours)}


def test_view_is_replaced_in_place_never_dropped():
    """schema.sql runs at every boot: a DROP VIEW would fail, and stop the
    boot, the day the dashboard defines anything on top of this view."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fp:
        schema_text = fp.read()

    assert "CREATE OR REPLACE VIEW premium_status" in schema_text
    assert "DROP VIEW IF EXISTS premium_status" not in schema_text


# ---------------------------------------------------------------------------
# Live-Postgres equivalence test
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")

pytestmark_skip_reason = (
    "YASUHO_TEST_PG_DSN not set; set it to a throwaway Postgres 11+ DSN to "
    "run the live premium_status equivalence test (see the PG11 Docker probe "
    "in the lot's own report for how to stand one up). This repo's CI has no "
    "Postgres, so this test is skipped there by design."
)


@pytest.mark.skipif(PG_DSN is None, reason=pytestmark_skip_reason)
async def test_premium_status_view_matches_python_is_active_and_is_grant_active():
    import asyncpg

    now = datetime.datetime.now(UTC)
    admin_conn = await asyncpg.connect(dsn=PG_DSN)
    db_name = f"yasuho_premium_status_test_{uuid.uuid4().hex[:12]}"
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    # asyncpg DSNs: swap the path (database name) component only.
    base_dsn, _, _ = PG_DSN.rpartition("/")
    test_dsn = f"{base_dsn}/{db_name}" if base_dsn else PG_DSN

    conn = await asyncpg.connect(dsn=test_dsn)
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fp:
            await conn.execute(fp.read())

        YP_SKU = 900000000000000001
        CP_SKU = 900000000000000002
        WRONG_SKU = 900000000000000999

        await conn.execute(
            "INSERT INTO premium_skus (product, sku_id) VALUES ($1, $2), ($3, $4)",
            "yasuho_plus", YP_SKU, "comfort_pack", CP_SKU,
        )

        # --- Guild-scoped entitlement matrix (product = yasuho_plus) -----
        guild_cases = {}

        async def add_entitlement(entitlement_id, guild_id, *, sku_id, deleted=False, ends_at=None, last_synced_at=None):
            await conn.execute(
                "INSERT INTO premium_entitlements "
                "(entitlement_id, sku_id, scope_type, guild_id, user_id, "
                " deleted, consumed, starts_at, ends_at, last_synced_at) "
                "VALUES ($1, $2, 'guild', $3, NULL, $4, FALSE, NULL, $5, "
                "COALESCE($6, now()))",
                entitlement_id, sku_id, guild_id, deleted, ends_at, last_synced_at,
            )

        async def add_user_entitlement(entitlement_id, user_id, *, sku_id, deleted=False, ends_at=None, last_synced_at=None):
            await conn.execute(
                "INSERT INTO premium_entitlements "
                "(entitlement_id, sku_id, scope_type, guild_id, user_id, "
                " deleted, consumed, starts_at, ends_at, last_synced_at) "
                "VALUES ($1, $2, 'user', NULL, $3, $4, FALSE, NULL, $5, "
                "COALESCE($6, now()))",
                entitlement_id, sku_id, user_id, deleted, ends_at, last_synced_at,
            )

        async def add_grant(guild_id, product, *, expires_at=None, revoked_at=None):
            await conn.execute(
                "INSERT INTO premium_grants "
                "(product, scope_type, guild_id, user_id, granted_by, expires_at, revoked_at) "
                "VALUES ($1, 'guild', $2, NULL, 1, $3, $4)",
                product, guild_id, expires_at, revoked_at,
            )

        async def add_user_grant(user_id, product, *, expires_at=None, revoked_at=None):
            await conn.execute(
                "INSERT INTO premium_grants "
                "(product, scope_type, guild_id, user_id, granted_by, expires_at, revoked_at) "
                "VALUES ($1, 'user', NULL, $2, 1, $3, $4)",
                product, user_id, expires_at, revoked_at,
            )

        next_eid = iter(range(1, 1000))

        # 1. deleted -> inactive, whatever the dates say
        g1 = 10001
        await add_entitlement(next(next_eid), g1, sku_id=YP_SKU, deleted=True, ends_at=None)
        guild_cases[g1] = [{"deleted": True, "ends_at": None, "last_synced_at": now}]

        # 2. ends_at NULL -> active forever
        g2 = 10002
        await add_entitlement(next(next_eid), g2, sku_id=YP_SKU, ends_at=None)
        guild_cases[g2] = [{"deleted": False, "ends_at": None, "last_synced_at": now}]

        # 3. future ends_at -> active
        g3 = 10003
        future = now + datetime.timedelta(days=10)
        await add_entitlement(next(next_eid), g3, sku_id=YP_SKU, ends_at=future, last_synced_at=now)
        guild_cases[g3] = [{"deleted": False, "ends_at": future, "last_synced_at": now}]

        # 4. just past, last_synced_at < ends_at -> active (grace)
        g4 = 10004
        past1h = now - datetime.timedelta(hours=1)
        synced_before = now - datetime.timedelta(hours=2)
        await add_entitlement(next(next_eid), g4, sku_id=YP_SKU, ends_at=past1h, last_synced_at=synced_before)
        guild_cases[g4] = [{"deleted": False, "ends_at": past1h, "last_synced_at": synced_before}]

        # 5. just past, last_synced_at >= ends_at -> inactive (confirmed end)
        g5 = 10005
        await add_entitlement(next(next_eid), g5, sku_id=YP_SKU, ends_at=past1h, last_synced_at=now)
        guild_cases[g5] = [{"deleted": False, "ends_at": past1h, "last_synced_at": now}]

        # 6. 49h past -> inactive regardless of sync (beyond the grace window)
        g6 = 10006
        past49h = now - datetime.timedelta(hours=49)
        synced_before_49 = now - datetime.timedelta(hours=50)
        await add_entitlement(next(next_eid), g6, sku_id=YP_SKU, ends_at=past49h, last_synced_at=synced_before_49)
        guild_cases[g6] = [{"deleted": False, "ends_at": past49h, "last_synced_at": synced_before_49}]

        # 7. wrong sku -> inactive (no entitlement can match)
        g7 = 10007
        await add_entitlement(next(next_eid), g7, sku_id=WRONG_SKU, ends_at=None)
        guild_cases[g7] = []  # the view's ent_rows join drops this row entirely

        # 9. grant active, permanent
        g9 = 10009
        await add_grant(g9, "yasuho_plus", expires_at=None)
        guild_grants = {g9: [{"revoked_at": None, "expires_at": None}]}

        # 10. grant active, future expiry
        g10 = 10010
        grant_future = now + datetime.timedelta(days=3)
        await add_grant(g10, "yasuho_plus", expires_at=grant_future)
        guild_grants[g10] = [{"revoked_at": None, "expires_at": grant_future}]

        # 11. grant expired -> inactive
        g11 = 10011
        grant_past = now - datetime.timedelta(days=1)
        await add_grant(g11, "yasuho_plus", expires_at=grant_past)
        guild_grants[g11] = [{"revoked_at": None, "expires_at": grant_past}]

        # 12. grant revoked -> inactive (even with no expiry)
        g12 = 10012
        await add_grant(g12, "yasuho_plus", expires_at=None, revoked_at=now)
        guild_grants[g12] = [{"revoked_at": now, "expires_at": None}]

        # 13. both an active entitlement and an active grant -> prefer entitlement
        g13 = 10013
        await add_entitlement(next(next_eid), g13, sku_id=YP_SKU, ends_at=None)
        await add_grant(g13, "yasuho_plus", expires_at=None)
        guild_cases[g13] = [{"deleted": False, "ends_at": None, "last_synced_at": now}]
        guild_grants[g13] = [{"revoked_at": None, "expires_at": None}]

        for gid in (g1, g2, g3, g4, g5, g6, g7):
            guild_grants.setdefault(gid, [])
        for gid in (g9, g10, g11, g12):
            guild_cases.setdefault(gid, [])

        # --- User-scoped entitlement/grant pair (product = comfort_pack) --
        u1 = 20001
        await add_user_entitlement(next(next_eid), u1, sku_id=CP_SKU, ends_at=None)
        user_cases = {u1: [{"deleted": False, "ends_at": None, "last_synced_at": now}]}
        user_grants = {u1: []}

        u2 = 20002
        await add_user_grant(u2, "comfort_pack", expires_at=None)
        user_cases[u2] = []
        user_grants[u2] = [{"revoked_at": None, "expires_at": None}]

        # --- Expected answers, computed from tools.premium itself --------
        expected_guild_active = {
            gid: (
                any(premium.is_active(row, now=now) for row in guild_cases.get(gid, ()))
                or any(premium.is_grant_active(row, now=now) for row in guild_grants.get(gid, ()))
            )
            for gid in set(guild_cases) | set(guild_grants)
        }
        expected_user_active = {
            uid: (
                any(premium.is_active(row, now=now) for row in user_cases.get(uid, ()))
                or any(premium.is_grant_active(row, now=now) for row in user_grants.get(uid, ()))
            )
            for uid in set(user_cases) | set(user_grants)
        }

        rows = await conn.fetch(
            "SELECT scope_type, scope_id, product, active FROM premium_status"
        )
        actual = {(r["scope_type"], r["scope_id"], r["product"]): r["active"] for r in rows}

        for gid, expected in expected_guild_active.items():
            got = actual.get(("guild", gid, "yasuho_plus"))
            assert got is not None, f"guild {gid} missing from premium_status"
            assert got == expected, f"guild {gid}: expected active={expected}, got {got}"

        for uid, expected in expected_user_active.items():
            got = actual.get(("user", uid, "comfort_pack"))
            assert got is not None, f"user {uid} missing from premium_status"
            assert got == expected, f"user {uid}: expected active={expected}, got {got}"

        # --- Precedence: g13 must report source='entitlement' ------------
        row13 = await conn.fetchrow(
            "SELECT active, source, ends_at FROM premium_status "
            "WHERE scope_type = 'guild' AND scope_id = $1 AND product = 'yasuho_plus'",
            g13,
        )
        assert row13["active"] is True
        assert row13["source"] == "entitlement"
        assert row13["ends_at"] is None

        # --- NULL sku in premium_skus: no entitlement can match ----------
        # Reconfigure comfort_pack to an unconfigured SKU and add a fresh
        # user whose entitlement carries SOME sku_id - it must never match.
        await conn.execute(
            "UPDATE premium_skus SET sku_id = NULL WHERE product = 'comfort_pack'"
        )
        u3 = 20003
        await add_user_entitlement(next(next_eid), u3, sku_id=CP_SKU, ends_at=None)
        row_u3 = await conn.fetchrow(
            "SELECT active, source FROM premium_status "
            "WHERE scope_type = 'user' AND scope_id = $1 AND product = 'comfort_pack'",
            u3,
        )
        assert row_u3["active"] is False
        assert row_u3["source"] is None

        # A grant still counts under the NULL-sku config.
        u4 = 20004
        await add_user_grant(u4, "comfort_pack", expires_at=None)
        row_u4 = await conn.fetchrow(
            "SELECT active, source FROM premium_status "
            "WHERE scope_type = 'user' AND scope_id = $1 AND product = 'comfort_pack'",
            u4,
        )
        assert row_u4["active"] is True
        assert row_u4["source"] == "grant"

        # --- Never expose a grant's reason/granted_by ---------------------
        column_names = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'premium_status'"
            )
        }
        assert "reason" not in column_names
        assert "granted_by" not in column_names
    finally:
        await conn.close()
        admin_conn = await asyncpg.connect(dsn=PG_DSN)
        try:
            await admin_conn.execute(f'DROP DATABASE "{db_name}"')
        finally:
            await admin_conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
