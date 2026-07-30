"""Tests for the profile storage seam (cogs/community/profile/storage.py).

Against the conftest fake pool, so these assert the CONTRACT of each statement
rather than Postgres behaviour (the DDL, the CHECKs and every statement here were
also probed against the real local Postgres in a rolled-back transaction):

* validation happens BEFORE any SQL - a rejected value never reaches the pool;
* every write is ONE statement touching ONE column, with `updated_at` maintained;
* the JSONB writes merge server-side, so there is no read-modify-write to lose;
* choosing 'private' DELETES the visibility row - the default is never stored.
"""

import json

import pytest

from cogs.community.profile import registry, storage
from cogs.community.profile.visibility import InvalidLevel

USER = 4242


def _queries(pool):
    return [query for _method, query, _args in pool.calls]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_get_profile_returns_none_when_the_user_has_no_row(fake_pool):
    fake_pool.fetchrow_return = None
    assert await storage.get_profile(fake_pool, USER) is None
    method, query, args = fake_pool.calls[0]
    assert (method, args) == ("fetchrow", (USER,))
    assert "FROM user_profiles WHERE user_id = $1" in query


async def test_get_profile_decodes_the_jsonb_columns_asyncpg_returns_as_text(fake_pool):
    fake_pool.fetchrow_return = {
        "user_id": USER,
        "bio": "hi",
        "pronouns": None,
        "accent": 0,
        "custom_fields": json.dumps([{"label": "a", "value": "b"}]),
        "gaming_ids": json.dumps({"switch": "SW-1"}),
        "created_at": "then",
        "updated_at": "now",
    }
    profile = await storage.get_profile(fake_pool, USER)
    assert profile["custom_fields"] == [{"label": "a", "value": "b"}]
    assert profile["gaming_ids"] == {"switch": "SW-1"}
    assert profile["accent"] == 0


async def test_get_profile_tolerates_decoded_or_missing_json(fake_pool):
    """A future JSONB codec on the pool must not break the decode, and junk
    must degrade to the empty default rather than explode a profile view."""
    fake_pool.fetchrow_return = {
        "user_id": USER,
        "custom_fields": [{"label": "a", "value": "b"}],
        "gaming_ids": None,
    }
    profile = await storage.get_profile(fake_pool, USER)
    assert profile["custom_fields"] == [{"label": "a", "value": "b"}]
    assert profile["gaming_ids"] == {}

    fake_pool.fetchrow_return = {"user_id": USER, "custom_fields": "not json"}
    assert (await storage.get_profile(fake_pool, USER))["custom_fields"] == []


async def test_get_profile_drops_stored_entries_the_registry_refuses(fake_pool):
    """The CHECKs guard the outer shape only, and the dashboard writes here too.

    A row holding an over-long value, an unknown gamer-ID key or an entry of the
    wrong type must degrade entry by entry instead of crashing `profile view` -
    which renders `pair["label"]` and hands the value straight to Discord.
    """
    fake_pool.fetchrow_return = {
        "user_id": USER,
        "custom_fields": json.dumps(
            [
                {"label": "keep", "value": "me"},
                "a bare string",
                {"value": "no label"},
                {"label": "x" * 999, "value": "too long a label"},
            ]
        ),
        "gaming_ids": json.dumps(
            {
                "switch": "SW-1",
                "nintendo64": "not a registry key",
                "steam_id": "s" * (registry.GAMING_ID_MAX + 1),
                "riot": 12345,
            }
        ),
    }
    profile = await storage.get_profile(fake_pool, USER)
    assert profile["custom_fields"] == [{"label": "keep", "value": "me"}]
    assert profile["gaming_ids"] == {"switch": "SW-1"}


async def test_get_profile_caps_how_many_custom_pairs_a_row_can_smuggle(fake_pool):
    fake_pool.fetchrow_return = {
        "user_id": USER,
        "custom_fields": [
            {"label": f"l{index}", "value": "v"} for index in range(12)
        ],
        "gaming_ids": {},
    }
    profile = await storage.get_profile(fake_pool, USER)
    assert len(profile["custom_fields"]) == registry.CUSTOM_FIELDS_MAX


async def test_get_visibility_returns_only_the_rows_that_exist(fake_pool):
    fake_pool.fetch_return = [
        {"field": "bio", "level": "public"},
        {"field": "gaming_ids", "level": "server"},
    ]
    assert await storage.get_visibility(fake_pool, USER) == {
        "bio": "public",
        "gaming_ids": "server",
    }
    _method, query, args = fake_pool.calls[0]
    assert args == (USER,)
    assert "FROM profile_visibility WHERE user_id = $1" in query


# ---------------------------------------------------------------------------
# set_field
# ---------------------------------------------------------------------------


async def test_set_field_upserts_one_column_and_bumps_updated_at(fake_pool):
    stored = await storage.set_field(fake_pool, USER, "bio", "  hello  ")
    assert stored == "hello"
    (method, query, args) = fake_pool.calls[0]
    assert method == "execute"
    assert query == (
        "INSERT INTO user_profiles (user_id, bio) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET bio = EXCLUDED.bio, updated_at = now()"
    )
    assert args == (USER, "hello")


async def test_set_field_casts_and_serialises_the_jsonb_columns(fake_pool):
    await storage.set_field(fake_pool, USER, "custom_fields", [("a", "b")])
    _method, query, args = fake_pool.calls[0]
    assert "VALUES ($1, $2::jsonb)" in query
    assert json.loads(args[1]) == [{"label": "a", "value": "b"}]

    fake_pool.calls.clear()
    await storage.set_field(fake_pool, USER, "gaming_ids", {"switch": "SW-1"})
    _method, query, args = fake_pool.calls[0]
    assert "SET gaming_ids = EXCLUDED.gaming_ids" in query
    assert json.loads(args[1]) == {"switch": "SW-1"}


async def test_set_field_stores_the_normalised_value_not_the_raw_one(fake_pool):
    assert await storage.set_field(fake_pool, USER, "accent", "#58F") == 0x5588FF
    assert fake_pool.calls[0][2] == (USER, 0x5588FF)


async def test_set_field_clears_with_null_rather_than_an_empty_string(fake_pool):
    assert await storage.set_field(fake_pool, USER, "pronouns", "   ") is None
    assert fake_pool.calls[0][2] == (USER, None)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("bio", "   "),
        ("bio", None),
        ("pronouns", None),
        ("accent", ""),
        ("custom_fields", []),
        ("gaming_ids", {}),
    ),
)
async def test_clearing_a_field_updates_and_never_creates_a_row(
    fake_pool, name, value
):
    """Same discipline as set_gaming_id: erasing must not CONJURE a profile.

    An INSERT ... ON CONFLICT on a user with no row writes an all-NULL profile
    for someone who never wrote one - which /mydata would then export as a
    "profile" object instead of null, and the dashboard would count as a user
    with a profile.
    """
    await storage.set_field(fake_pool, USER, name, value)
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert query.startswith(f"UPDATE user_profiles SET {name} = ")
    assert "INSERT" not in query
    assert "WHERE user_id = $1" in query
    assert "updated_at = now()" in query
    assert args[0] == USER
    assert len(fake_pool.calls) == 1


async def test_clearing_a_jsonb_field_still_casts_its_parameter(fake_pool):
    await storage.set_field(fake_pool, USER, "gaming_ids", {})
    _method, query, args = fake_pool.calls[0]
    assert "SET gaming_ids = $2::jsonb" in query
    assert json.loads(args[1]) == {}


async def test_accent_black_is_a_real_value_and_still_upserts(fake_pool):
    """0 is BLACK, not "unset": clearing it takes a hex-less call, not #000000."""
    assert await storage.set_field(fake_pool, USER, "accent", "#000000") == 0
    _method, query, args = fake_pool.calls[0]
    assert query.startswith("INSERT INTO user_profiles (user_id, accent)")
    assert args == (USER, 0)


@pytest.mark.parametrize(
    ("name", "value", "error"),
    (
        ("nope", "x", registry.UnknownField),
        ("anilist", "x", registry.FieldNotStored),
        ("bio", "x" * 301, registry.InvalidValue),
        ("accent", "nonsense", registry.InvalidValue),
        ("gaming_ids", {"epic": "x"}, registry.InvalidValue),
    ),
)
async def test_set_field_rejects_before_touching_the_database(
    fake_pool, name, value, error
):
    with pytest.raises(error):
        await storage.set_field(fake_pool, USER, name, value)
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# set_gaming_id
# ---------------------------------------------------------------------------


async def test_set_gaming_id_merges_server_side_without_reading_first(fake_pool):
    assert await storage.set_gaming_id(fake_pool, USER, "switch", " SW-1 ") == "SW-1"
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert "user_profiles.gaming_ids || jsonb_build_object($2::text, $3::text)" in query
    assert "updated_at = now()" in query
    assert args == (USER, "switch", "SW-1")
    # No read anywhere: the merge happens in the statement, not in Python.
    assert [call[0] for call in fake_pool.calls] == ["execute", "execute"]


@pytest.mark.parametrize(
    ("key", "column"),
    (
        ("switch", "switch_fc"),
        ("3ds", "threeds_fc"),
        ("battletag", "battletag"),
        ("riot", "riotid"),
        ("steam_id", "steamid"),
    ),
)
async def test_writing_a_gaming_id_nulls_its_pre_migration_copy(
    fake_pool, key, column
):
    """The legacy `profiles` row must not keep serving a superseded value.

    /mydata still exports that table, so leaving the old friend code there would
    hand the user back an ID they changed - or thought they had erased.
    """
    await storage.set_gaming_id(fake_pool, USER, key, "value-1")
    _method, query, args = fake_pool.calls[-1]
    assert query == (
        f"UPDATE profiles SET {column} = NULL "
        f"WHERE user_id = $1 AND {column} IS NOT NULL"
    )
    assert args == (USER,)


async def test_clearing_a_gaming_id_clears_the_legacy_column_too(fake_pool):
    await storage.set_gaming_id(fake_pool, USER, "riot", None)
    assert fake_pool.calls[-1][1].startswith("UPDATE profiles SET riotid = NULL")


async def test_clearing_a_gaming_id_updates_and_never_creates_a_row(fake_pool):
    assert await storage.set_gaming_id(fake_pool, USER, "switch", None) is None
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert query.startswith("UPDATE user_profiles SET gaming_ids = gaming_ids - $2")
    assert "INSERT" not in query
    assert args == (USER, "switch")


async def test_clearing_uses_the_update_path_for_an_emptied_value(fake_pool):
    assert await storage.set_gaming_id(fake_pool, USER, "steam_id", "   ") is None
    assert fake_pool.calls[0][1].startswith("UPDATE user_profiles")


@pytest.mark.parametrize("value", ("SW-1", None))
async def test_set_gaming_id_refuses_a_key_outside_the_whitelist(fake_pool, value):
    with pytest.raises(registry.InvalidValue):
        await storage.set_gaming_id(fake_pool, USER, "epic", value)
    assert fake_pool.calls == []


async def test_set_gaming_id_applies_the_value_cap(fake_pool):
    with pytest.raises(registry.InvalidValue):
        await storage.set_gaming_id(
            fake_pool, USER, "switch", "x" * (registry.GAMING_ID_MAX + 1)
        )
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# set_visibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ("public", "server"))
async def test_set_visibility_upserts_a_published_level(fake_pool, level):
    assert await storage.set_visibility(fake_pool, USER, "bio", level) == level
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert query.startswith("INSERT INTO profile_visibility")
    assert "ON CONFLICT (user_id, field) DO UPDATE SET level = EXCLUDED.level" in query
    assert args == (USER, "bio", level)


async def test_private_deletes_the_row_so_the_default_is_never_materialised(fake_pool):
    assert await storage.set_visibility(fake_pool, USER, "bio", "PRIVATE") == "private"
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert query.startswith("DELETE FROM profile_visibility")
    assert "INSERT" not in query
    assert args == (USER, "bio")


async def test_visibility_is_settable_for_a_connector_section(fake_pool):
    await storage.set_visibility(fake_pool, USER, "anilist", "server")
    assert fake_pool.calls[0][2] == (USER, "anilist", "server")


async def test_set_visibility_rejects_unknown_fields_and_levels(fake_pool):
    with pytest.raises(registry.UnknownField):
        await storage.set_visibility(fake_pool, USER, "not_a_field", "public")
    with pytest.raises(InvalidLevel):
        await storage.set_visibility(fake_pool, USER, "bio", "friends-only")
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# delete + structural guards
# ---------------------------------------------------------------------------


async def test_delete_profile_delegates_to_the_shared_privacy_path(monkeypatch):
    """One deletion implementation: /mydata and `profile clear` cannot drift."""
    seen = []

    async def _delete(pool, user_id):
        seen.append((pool, user_id))
        return {"user_profiles": 1}

    monkeypatch.setattr(storage.privacy, "delete_user_profile", _delete)
    sentinel = object()
    assert await storage.delete_profile(sentinel, USER) == {"user_profiles": 1}
    assert seen == [(sentinel, USER)]


async def test_every_write_is_a_single_statement(fake_pool):
    """asyncpg runs ONE parameterized statement per call; a stray ';' would fail
    at runtime, not here, so pin it."""
    await storage.set_field(fake_pool, USER, "bio", "hi")
    await storage.set_field(fake_pool, USER, "custom_fields", [("a", "b")])
    await storage.set_gaming_id(fake_pool, USER, "switch", "SW-1")
    await storage.set_gaming_id(fake_pool, USER, "switch", None)
    await storage.set_visibility(fake_pool, USER, "bio", "public")
    await storage.set_visibility(fake_pool, USER, "bio", "private")
    await storage.get_profile(fake_pool, USER)
    await storage.get_visibility(fake_pool, USER)
    for query in _queries(fake_pool):
        assert ";" not in query


async def test_no_write_ever_interpolates_a_value_into_the_sql(fake_pool):
    """Only registry-owned column identifiers may be interpolated; user text
    always rides as a bound parameter."""
    await storage.set_field(fake_pool, USER, "bio", "'; DROP TABLE user_profiles --")
    await storage.set_gaming_id(fake_pool, USER, "switch", "'; DROP TABLE profiles --")
    for query in _queries(fake_pool):
        assert "DROP TABLE" not in query
