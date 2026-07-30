"""The profile migration and its schema, guarded structurally.

Two things must stay true for a live database to survive this lot:

* the boot fixups copy EVERY legacy gamer ID into the new JSONB section, are
  idempotent, never overwrite an edit made through the new package, and never
  drop the old table (it is the safety net);
* schema.sql really declares the two user-scoped tables the package reads, with
  the caps and the level CHECK the code assumes - and with NO guild_id, which is
  what keeps them out of the guild purge (see the exemption test below).

Offline: parses schema.sql as text and reads the fixup SQL, exactly like
tests/tools/test_retention.py does for the guild purge. The statements themselves
were additionally probed against the real local Postgres in a rolled-back
transaction.
"""

import os
import re

from cogs.community.profile import registry as profile_registry
from tools import fixups, retention

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
)

_LEGACY_COLUMNS = ("switch_fc", "threeds_fc", "battletag", "riotid", "steamid")


def _schema():
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        return re.sub(r"--[^\n]*", "", handle.read())


def _table_body(name):
    match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + name + r"\s*\((.*?)\n\s*\)\s*;",
        _schema(),
        re.S | re.I,
    )
    assert match, f"{name} is not declared in schema.sql"
    return match.group(1)


def _fixup(name):
    (found,) = [item for item in fixups.FIXUPS if item.name == name]
    return found


# ---------------------------------------------------------------------------
# schema.sql
# ---------------------------------------------------------------------------


def test_user_profiles_declares_every_column_the_package_reads():
    body = _table_body("user_profiles")
    for column in (
        "user_id",
        "bio",
        "pronouns",
        "accent",
        "custom_fields",
        "gaming_ids",
        "created_at",
        "updated_at",
    ):
        assert re.search(rf"^\s*{column}\b", body, re.M), column
    assert "PRIMARY KEY" in body
    # JSONB sections must never be NULL: the registry clears them to []/{}.
    assert re.search(r"custom_fields\s+JSONB\s+NOT NULL DEFAULT '\[\]'", body)
    assert re.search(r"gaming_ids\s+JSONB\s+NOT NULL DEFAULT '\{\}'", body)


def test_user_profiles_checks_mirror_the_registry_caps():
    body = _table_body("user_profiles")
    assert "accent <= 16777215" in body  # 0xFFFFFF
    assert "char_length(bio) <= 300" in body
    assert "char_length(pronouns) <= 40" in body
    assert "jsonb_array_length(custom_fields) <= 5" in body
    assert "jsonb_typeof(custom_fields) = 'array'" in body
    assert "jsonb_typeof(gaming_ids) = 'object'" in body


def test_profile_visibility_is_keyed_by_user_and_field_with_a_level_check():
    body = _table_body("profile_visibility")
    assert "PRIMARY KEY (user_id, field)" in body
    assert "level   TEXT   NOT NULL CHECK (level IN ('public', 'server', 'private'))" in body
    # `field` stays free TEXT: the registry validates it, so a P3/P4 connector
    # needs no migration.
    assert re.search(r"^\s*field\s+TEXT\s+NOT NULL\s*,", body, re.M)


def test_the_legacy_table_is_kept_as_the_migration_safety_net():
    assert _table_body("profiles")
    assert "DROP TABLE" not in _schema().upper()


def test_profile_tables_are_user_scoped_and_exempt_from_the_guild_purge():
    """Documented exemption: the guild purge covers guild_id tables. A profile is
    global to a person, has no guild_id, and dies on the USER path instead
    (tools/privacy.PROFILE_DELETE_QUERIES) - never when a guild departs."""
    for table in ("user_profiles", "profile_visibility"):
        body = _table_body(table)
        assert not re.search(r"^\s*guild_id\b", body, re.M), table
        assert table not in dict(retention.GUILD_DELETE_QUERIES)
        assert table not in retention.STORED_GUILD_IDS_QUERY


# ---------------------------------------------------------------------------
# The two boot fixups
# ---------------------------------------------------------------------------


def test_both_profile_fixups_are_registered_after_the_existing_one():
    """The seed runs BEFORE the import, and that order is load-bearing: the seed
    reads "no user_profiles row" as "not migrated yet", which the import is about
    to make false. Swapping them turns the seed into a permanent no-op."""
    names = [item.name for item in fixups.FIXUPS]
    assert names == [
        "warns_count_recompute_from_cases",
        "profile_visibility_seed_legacy_gaming_ids",
        "user_profiles_import_legacy_gaming_ids",
    ]


def test_the_import_carries_every_legacy_column_to_its_registry_key():
    sql = _fixup("user_profiles_import_legacy_gaming_ids").sql
    for column in _LEGACY_COLUMNS:
        assert column in sql, column
    for key in ("'switch'", "'3ds'", "'battletag'", "'riot'", "'steam_id'"):
        assert key in sql, key
    # The registry is the authority on the key names; the SQL must quote those.
    for key in profile_registry.GAMING_ID_KEYS:
        assert f"'{key}'" in sql, key
    # NULL columns must not become null-valued JSON keys.
    assert "jsonb_strip_nulls" in sql


def test_the_import_skips_users_with_no_legacy_data():
    sql = _fixup("user_profiles_import_legacy_gaming_ids").sql
    assert "COALESCE" in sql and "IS NOT NULL" in sql


def test_a_repeat_import_can_never_overwrite_a_newer_edit():
    """`EXCLUDED || user_profiles` puts the STORED object on the right, so the
    existing keys win. Reversing it would silently restore stale gamer IDs."""
    sql = _fixup("user_profiles_import_legacy_gaming_ids").sql
    assert "EXCLUDED.gaming_ids || user_profiles.gaming_ids" in sql
    assert "user_profiles.gaming_ids || EXCLUDED.gaming_ids" not in sql


def test_the_import_is_additive_only():
    sql = _fixup("user_profiles_import_legacy_gaming_ids").sql.upper()
    assert "DELETE" not in sql
    assert "DROP" not in sql
    assert "TRUNCATE" not in sql


def test_migrated_gamer_ids_keep_exactly_the_visibility_they_already_had():
    """They were readable by anyone who could run `?profile view @them`, so they
    are seeded at 'server' - never 'public', and never silently blanked."""
    sql = _fixup("profile_visibility_seed_legacy_gaming_ids").sql
    assert "'gaming_ids', 'server'" in sql
    assert "'public'" not in sql
    assert "ON CONFLICT (user_id, field) DO NOTHING" in sql
    assert "COALESCE" in sql and "IS NOT NULL" in sql


def test_the_seed_cannot_republish_a_deliberate_private_choice():
    """'private' is stored as an ABSENT row, so `ON CONFLICT DO NOTHING` alone is
    not self-idempotent: a replay finds nothing to conflict with and re-publishes
    at 'server'. The NOT EXISTS guard on user_profiles is what makes the replay a
    real no-op (the import has created that row by then)."""
    sql = _fixup("profile_visibility_seed_legacy_gaming_ids").sql
    assert re.search(
        r"AND\s+NOT\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+user_profiles\s+AS\s+up\s+"
        r"WHERE\s+up\.user_id\s*=\s*p\.user_id\s*\)",
        sql,
        re.I | re.S,
    ), sql


def test_the_seed_never_publishes_anything_but_the_migrated_section():
    sql = _fixup("profile_visibility_seed_legacy_gaming_ids").sql
    fields = set(re.findall(r"'(bio|pronouns|accent|custom_fields|gaming_ids)'", sql))
    assert fields == {"gaming_ids"}
