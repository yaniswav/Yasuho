"""Tests for the profile field registry (cogs/community/profile/registry.py).

The registry is the contract every other half of the chantier is written
against, so these tests pin it hard: which names exist (including the P3/P4
connector sections reserved in code rather than in schema.sql), which of them
this lot can write, and exactly where each cap bites. Pure and offline.
"""

import pytest

from cogs.community.profile import registry

# ---------------------------------------------------------------------------
# Shape of the registry itself
# ---------------------------------------------------------------------------


def test_socle_fields_are_stored_and_name_a_column():
    assert registry.STORED_NAMES == (
        "bio",
        "pronouns",
        "accent",
        "custom_fields",
        "gaming_ids",
    )
    for field in registry.STORED_FIELDS:
        assert field.stored is True
        assert field.column
        assert registry.COLUMNS[field.name] == field.column


def test_connector_sections_are_reserved_but_not_stored():
    """P3/P4 names must be addressable NOW so visibility can talk about them."""
    for name in (
        "anilist",
        "steam",
        "lastfm",
        "osu",
        "backloggd",
        "presence_gaming",
        "spotify_presence",
    ):
        field = registry.get(name)
        assert registry.is_known(name)
        assert field.stored is False
        assert field.column is None
        with pytest.raises(registry.FieldNotStored):
            registry.stored_field(name)
        with pytest.raises(registry.FieldNotStored):
            registry.normalise(name, "anything")


def test_names_are_unique_and_indexed():
    assert len(registry.FIELD_NAMES) == len(set(registry.FIELD_NAMES))
    assert set(registry.BY_NAME) == set(registry.FIELD_NAMES)
    assert all(registry.BY_NAME[name].name == name for name in registry.FIELD_NAMES)


def test_every_field_carries_a_label_and_gaming_labels_match_the_keys():
    assert all(field.label for field in registry.FIELDS)
    assert tuple(registry.GAMING_ID_LABELS) == registry.GAMING_ID_KEYS


def test_no_gaming_id_key_collides_with_a_registry_name():
    """The class guard behind naming the Steam friend code `steam_id`.

    Gamer-ID KEYS and field/section NAMES are two namespaces routed by the SAME
    user-typed word (`profile set <x>` / `section_for()`), so a word living in
    both is ambiguous: `steam` used to mean both "my Steam friend code" and "my
    linked Steam account section", and the router silently picked the friend
    code, making the P3 section unreachable. Any future key must be disjoint.
    """
    collisions = set(registry.GAMING_ID_KEYS) & set(registry.FIELD_NAMES)
    assert not collisions, collisions


def test_unknown_name_is_a_typed_error_carrying_the_name():
    with pytest.raises(registry.UnknownField) as excinfo:
        registry.get("does_not_exist")
    assert excinfo.value.name == "does_not_exist"
    assert registry.is_known("does_not_exist") is False


# ---------------------------------------------------------------------------
# bio / pronouns: text caps
# ---------------------------------------------------------------------------


def test_text_fields_strip_and_treat_emptied_input_as_cleared():
    assert registry.normalise("bio", "  hello  ") == "hello"
    assert registry.normalise("bio", "   ") is None
    assert registry.normalise("bio", "") is None
    assert registry.normalise("bio", None) is None
    assert registry.normalise("pronouns", " she/her ") == "she/her"


@pytest.mark.parametrize(
    ("name", "limit"),
    (("bio", registry.BIO_MAX), ("pronouns", registry.PRONOUNS_MAX)),
)
def test_text_caps_accept_the_limit_and_refuse_one_more(name, limit):
    assert registry.normalise(name, "x" * limit) == "x" * limit
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise(name, "x" * (limit + 1))
    assert excinfo.value.reason == "too_long"
    assert excinfo.value.limit == limit
    assert excinfo.value.name == name


def test_bio_cap_is_300_and_pronouns_cap_is_40():
    """The caps are a product decision; pin the numbers, not just the mechanism."""
    assert (registry.BIO_MAX, registry.PRONOUNS_MAX) == (300, 40)


def test_text_field_refuses_a_non_string():
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("bio", 42)
    assert excinfo.value.reason == "type"


# ---------------------------------------------------------------------------
# accent: reuses the rank-card colour parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("#5865F2", 0x5865F2),
        ("5865F2", 0x5865F2),
        ("0x5865F2", 0x5865F2),
        ("#58F", 0x5588FF),
        (0x000000, 0),
        (0xFFFFFF, 0xFFFFFF),
        (None, None),
        ("", None),
        ("   ", None),
    ),
)
def test_accent_parses_every_shape_the_rank_card_accepts(value, expected):
    assert registry.normalise("accent", value) == expected


@pytest.mark.parametrize("value", ("#12345", "not a colour", -1, 0x1000000, True))
def test_accent_refuses_junk_as_a_registry_error_not_a_rank_card_one(value):
    """Callers catch registry errors only; the rank-card error must be wrapped."""
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("accent", value)
    assert excinfo.value.reason == "colour"
    assert isinstance(excinfo.value, registry.ProfileFieldError)


# ---------------------------------------------------------------------------
# custom_fields: bounded label/value pairs
# ---------------------------------------------------------------------------


def test_custom_fields_accept_tuples_dicts_and_mappings():
    expected = [{"label": "a", "value": "b"}]
    assert registry.normalise("custom_fields", [("a", "b")]) == expected
    assert registry.normalise("custom_fields", [{"label": "a", "value": "b"}]) == expected
    assert registry.normalise("custom_fields", {"a": "b"}) == expected


def test_custom_fields_clear_to_an_empty_list_never_null():
    """The column is NOT NULL DEFAULT '[]', so clearing must produce []."""
    assert registry.normalise("custom_fields", None) == []
    assert registry.normalise("custom_fields", []) == []


def test_custom_fields_drop_half_filled_rows():
    pairs = registry.normalise(
        "custom_fields", [("label", ""), ("", "value"), ("keep", "me")]
    )
    assert pairs == [{"label": "keep", "value": "me"}]


def test_custom_fields_cap_the_number_of_pairs():
    five = [(f"l{i}", f"v{i}") for i in range(registry.CUSTOM_FIELDS_MAX)]
    assert len(registry.normalise("custom_fields", five)) == registry.CUSTOM_FIELDS_MAX
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("custom_fields", five + [("one", "too many")])
    assert excinfo.value.reason == "too_many"
    assert excinfo.value.limit == registry.CUSTOM_FIELDS_MAX == 5


def test_custom_fields_cap_each_label_and_value():
    label = "x" * registry.CUSTOM_LABEL_MAX
    value = "y" * registry.CUSTOM_VALUE_MAX
    assert registry.normalise("custom_fields", [(label, value)]) == [
        {"label": label, "value": value}
    ]
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("custom_fields", [(label + "x", value)])
    assert excinfo.value.limit == registry.CUSTOM_LABEL_MAX == 30
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("custom_fields", [(label, value + "y")])
    assert excinfo.value.limit == registry.CUSTOM_VALUE_MAX == 100


@pytest.mark.parametrize("value", ("a string", 5, [("a", "b", "c")], [None]))
def test_custom_fields_refuse_a_shape_they_cannot_read(value):
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("custom_fields", value)
    assert excinfo.value.reason == "type"


# ---------------------------------------------------------------------------
# gaming_ids: the migrated legacy section
# ---------------------------------------------------------------------------


def test_gaming_ids_keep_the_legacy_keys():
    assert registry.GAMING_ID_KEYS == (
        "switch",
        "3ds",
        "battletag",
        "riot",
        # `steam_id`, not `steam`: the bare name belongs to the P3 connector
        # section (see test_no_gaming_id_key_collides_with_a_registry_name).
        "steam_id",
    )


def test_gaming_ids_normalise_to_a_registry_ordered_mapping():
    ids = registry.normalise(
        "gaming_ids", {"steam_id": " steam-1 ", "switch": "SW-1", "riot": ""}
    )
    # Empty value drops the key; order follows the registry, not the input.
    assert ids == {"switch": "SW-1", "steam_id": "steam-1"}
    assert list(ids) == ["switch", "steam_id"]


def test_gaming_ids_clear_to_an_empty_object_never_null():
    assert registry.normalise("gaming_ids", None) == {}
    assert registry.normalise("gaming_ids", {}) == {}


def test_gaming_ids_refuse_a_key_outside_the_whitelist():
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("gaming_ids", {"epic": "nope"})
    assert excinfo.value.reason == "unknown_key"


def test_gaming_id_cap_is_the_legacy_one_so_migration_truncates_nothing():
    """The legacy `profile set` refused >1000 chars; a migrated value must stay
    re-settable, so the cap cannot shrink under an existing row."""
    assert registry.GAMING_ID_MAX == 1000
    long_id = "x" * registry.GAMING_ID_MAX
    assert registry.normalise("gaming_ids", {"switch": long_id}) == {"switch": long_id}
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("gaming_ids", {"switch": long_id + "x"})
    assert excinfo.value.reason == "too_long"


def test_gaming_ids_refuse_a_non_mapping():
    with pytest.raises(registry.InvalidValue) as excinfo:
        registry.normalise("gaming_ids", ["switch", "SW-1"])
    assert excinfo.value.reason == "type"
