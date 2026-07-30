"""THE visibility matrix (cogs/community/profile/visibility.py).

Everything in the profile is invisible until its owner says otherwise, so this
file exists to make that impossible to break by accident. It walks the full
cross-product of the three levels against every kind of viewer (the owner, a
member of a shared server, a logged-in stranger, an anonymous reader) and pins
the two fail-closed invariants:

* an ABSENT row means private - the default is never materialised, so "never
  decided" and "chose private" must be the same answer;
* an UNKNOWN field name is ignored - a section written by a newer deploy (a P4
  connector) or left behind by a rename is never rendered by older code.

Pure and offline: no bot, no database, no discord.
"""

import pytest

from cogs.community.profile import registry
from cogs.community.profile.visibility import (
    DEFAULT_LEVEL,
    LEVELS,
    PRIVATE,
    PUBLIC,
    SERVER,
    InvalidLevel,
    ViewerContext,
    can_view,
    level_for,
    normalise_level,
    resolve_visible_fields,
    visible_field_names,
)

OWNER = 111
FRIEND = 222
STRANGER = 333

VIEWERS = {
    "owner": ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=True),
    # The owner in a DM: still the owner, even with no shared-guild flag.
    "owner_alone": ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False),
    "shared_guild": ViewerContext(owner_id=OWNER, viewer_id=FRIEND, shares_guild=True),
    "stranger": ViewerContext(owner_id=OWNER, viewer_id=STRANGER, shares_guild=False),
    "anonymous": ViewerContext(owner_id=OWNER, viewer_id=None, shares_guild=False),
}

# level -> viewer -> may see it. This table IS the feature.
MATRIX = {
    PUBLIC: {
        "owner": True,
        "owner_alone": True,
        "shared_guild": True,
        "stranger": True,
        "anonymous": True,
    },
    SERVER: {
        "owner": True,
        "owner_alone": True,
        "shared_guild": True,
        "stranger": False,
        "anonymous": False,
    },
    PRIVATE: {
        "owner": True,
        "owner_alone": True,
        "shared_guild": False,
        "stranger": False,
        "anonymous": False,
    },
}


def _profile(**fields):
    """A profile row as storage returns it: registry fields plus metadata."""
    base = {
        "user_id": OWNER,
        "bio": None,
        "pronouns": None,
        "accent": None,
        "custom_fields": [],
        "gaming_ids": {},
        "created_at": "2026-01-01",
        "updated_at": "2026-01-02",
    }
    base.update(fields)
    return base


# ---------------------------------------------------------------------------
# The matrix, one assertion per cell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", sorted(MATRIX))
@pytest.mark.parametrize("viewer_name", sorted(VIEWERS))
def test_can_view_matrix(level, viewer_name):
    assert can_view(level, VIEWERS[viewer_name]) is MATRIX[level][viewer_name]


@pytest.mark.parametrize("level", sorted(MATRIX))
@pytest.mark.parametrize("viewer_name", sorted(VIEWERS))
def test_resolve_matches_the_matrix_for_a_real_field(level, viewer_name):
    profile = _profile(bio="hello")
    visible = resolve_visible_fields(profile, {"bio": level}, VIEWERS[viewer_name])
    assert ("bio" in visible) is MATRIX[level][viewer_name]


@pytest.mark.parametrize("viewer_name", sorted(VIEWERS))
def test_an_absent_row_behaves_exactly_like_private(viewer_name):
    viewer = VIEWERS[viewer_name]
    profile = _profile(bio="hello")
    assert resolve_visible_fields(profile, {}, viewer) == resolve_visible_fields(
        profile, {"bio": PRIVATE}, viewer
    )


# ---------------------------------------------------------------------------
# level_for: everything that is not a real level is private
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "visibility_map",
    (
        {},
        None,
        {"bio": None},
        {"bio": "PUBLIC"},  # levels are stored lower-case; junk is not honoured
        {"bio": "world-readable"},
        {"bio": 1},
        {"other_field": PUBLIC},
    ),
)
def test_level_for_falls_back_to_private(visibility_map):
    assert level_for(visibility_map, "bio") == PRIVATE == DEFAULT_LEVEL


def test_level_for_returns_a_stored_level():
    assert level_for({"bio": SERVER}, "bio") == SERVER


@pytest.mark.parametrize("level", LEVELS)
def test_normalise_level_accepts_the_three_levels_case_insensitively(level):
    assert normalise_level(level.upper()) == level
    assert normalise_level(f"  {level} ") == level


@pytest.mark.parametrize("level", ("", "friends", None, 1, True, ["public"]))
def test_normalise_level_refuses_anything_else(level):
    with pytest.raises(InvalidLevel):
        normalise_level(level)


# ---------------------------------------------------------------------------
# resolve_visible_fields: the whole-profile behaviour
# ---------------------------------------------------------------------------


def test_owner_sees_every_field_they_set_whatever_the_levels():
    profile = _profile(
        bio="hi",
        pronouns="she/her",
        accent=0x5865F2,
        custom_fields=[{"label": "a", "value": "b"}],
        gaming_ids={"switch": "SW-1"},
    )
    visible = resolve_visible_fields(profile, {}, VIEWERS["owner"])
    assert set(visible) == {"bio", "pronouns", "accent", "custom_fields", "gaming_ids"}


def test_a_shared_guild_viewer_sees_public_and_server_only():
    profile = _profile(bio="public bio", pronouns="she/her", gaming_ids={"switch": "SW"})
    visible = resolve_visible_fields(
        profile,
        {"bio": PUBLIC, "gaming_ids": SERVER, "pronouns": PRIVATE},
        VIEWERS["shared_guild"],
    )
    assert set(visible) == {"bio", "gaming_ids"}


def test_a_stranger_sees_public_only():
    profile = _profile(bio="public bio", pronouns="she/her", gaming_ids={"switch": "SW"})
    visible = resolve_visible_fields(
        profile,
        {"bio": PUBLIC, "gaming_ids": SERVER, "pronouns": SERVER},
        VIEWERS["stranger"],
    )
    assert set(visible) == {"bio"}


def test_nothing_is_visible_by_default_even_to_a_shared_guild_member():
    profile = _profile(bio="hi", pronouns="she/her", gaming_ids={"switch": "SW"})
    assert resolve_visible_fields(profile, {}, VIEWERS["shared_guild"]) == {}


def test_metadata_columns_never_leak_into_the_result():
    profile = _profile(bio="hi")
    visible = resolve_visible_fields(profile, {"bio": PUBLIC}, VIEWERS["owner"])
    assert visible == {"bio": "hi"}
    for key in ("user_id", "created_at", "updated_at"):
        assert key not in visible


def test_an_unknown_field_is_ignored_even_when_published():
    """Forward-compat: a P4 connector this deploy does not know stays invisible."""
    profile = _profile(bio="hi")
    profile["from_the_future"] = "secret"
    visible = resolve_visible_fields(
        profile,
        {"bio": PUBLIC, "from_the_future": PUBLIC},
        VIEWERS["owner"],
    )
    assert visible == {"bio": "hi"}


def test_an_unknown_visibility_row_is_inert():
    profile = _profile(bio="hi")
    visible = resolve_visible_fields(
        profile, {"renamed_field": PUBLIC, "bio": PUBLIC}, VIEWERS["stranger"]
    )
    assert visible == {"bio": "hi"}


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("bio", None),
        ("bio", ""),
        ("pronouns", None),
        ("custom_fields", []),
        ("gaming_ids", {}),
    ),
)
def test_unset_fields_are_dropped_rather_than_shown_empty(name, value):
    profile = _profile(**{name: value})
    assert resolve_visible_fields(profile, {name: PUBLIC}, VIEWERS["owner"]) == {}


def test_black_is_a_real_accent_and_survives_the_empty_check():
    profile = _profile(accent=0)
    visible = resolve_visible_fields(profile, {"accent": PUBLIC}, VIEWERS["stranger"])
    assert visible == {"accent": 0}


def test_missing_profile_resolves_to_nothing():
    assert resolve_visible_fields(None, {"bio": PUBLIC}, VIEWERS["owner"]) == {}
    assert resolve_visible_fields({}, {"bio": PUBLIC}, VIEWERS["owner"]) == {}


def test_result_follows_registry_order_not_input_order():
    profile = _profile(gaming_ids={"switch": "SW"}, bio="hi", pronouns="she/her")
    reordered = {"gaming_ids": profile["gaming_ids"], "bio": "hi", "pronouns": "she/her"}
    visible = resolve_visible_fields(reordered, {}, VIEWERS["owner"])
    assert list(visible) == ["bio", "pronouns", "gaming_ids"]


# ---------------------------------------------------------------------------
# visible_field_names: what the P2 panel asks about connector sections
# ---------------------------------------------------------------------------


def test_visible_field_names_covers_connectors_with_no_stored_value():
    names = visible_field_names({"anilist": PUBLIC}, VIEWERS["stranger"])
    assert names == ("anilist",)
    assert "anilist" in registry.FIELD_NAMES


def test_visible_field_names_gives_the_owner_everything():
    assert visible_field_names({}, VIEWERS["owner"]) == registry.FIELD_NAMES


def test_viewer_context_is_owner_only_for_the_owner():
    assert VIEWERS["owner"].is_owner is True
    assert VIEWERS["shared_guild"].is_owner is False
    assert VIEWERS["anonymous"].is_owner is False
