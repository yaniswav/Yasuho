"""Unit tests for tools/autoroom.py.

The autoroom core is pure: no Discord, database or network. These tests pin the
exact behaviour of every public helper, focusing on the edges that bite in
production - overflow past MAX_HUBS, limit clamping, templates with and without
placeholders, overlong output, and missing/garbage keys.
"""

from tools import autoroom

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_policy_constants():
    assert autoroom.MAX_HUBS == 5
    assert autoroom.MAX_ROOMS == 50
    assert autoroom.DEFAULT_MAX_ROOMS == 20


# ---------------------------------------------------------------------------
# render_room_name
# ---------------------------------------------------------------------------


def test_render_user_placeholder():
    assert autoroom.render_room_name("{user}'s room", "Yuki") == "Yuki's room"


def test_render_count_and_n_placeholders():
    assert autoroom.render_room_name("Room {count}", "Yuki", index=3) == "Room 3"
    assert autoroom.render_room_name("Ranked #{n}", "Yuki", index=7) == "Ranked #7"


def test_render_count_blank_when_no_index():
    # {count}/{n} collapse to nothing when no index is supplied.
    assert autoroom.render_room_name("Room {count}", "Yuki") == "Room"


def test_render_no_placeholders_kept_verbatim():
    assert autoroom.render_room_name("Lobby", "Yuki") == "Lobby"


def test_render_empty_template_falls_back_to_name():
    assert autoroom.render_room_name("", "Yuki") == "Yuki"


def test_render_whitespace_template_falls_back_to_name():
    assert autoroom.render_room_name("   ", "Yuki") == "Yuki"


def test_render_non_string_template_falls_back_to_name():
    assert autoroom.render_room_name(None, "Yuki") == "Yuki"
    assert autoroom.render_room_name(123, "Yuki") == "Yuki"


def test_render_empty_name_uses_generic_fallback():
    # No template and no name -> generic default, never an empty channel name.
    assert autoroom.render_room_name("", "") == autoroom.FALLBACK_ROOM_NAME
    assert autoroom.render_room_name(None, None) == autoroom.FALLBACK_ROOM_NAME


def test_render_caps_at_100_chars():
    out = autoroom.render_room_name("x" * 250, "Yuki")
    assert len(out) == autoroom.CHANNEL_NAME_LIMIT


def test_render_caps_expanded_placeholder():
    long_name = "n" * 250
    out = autoroom.render_room_name("{user}", long_name)
    assert len(out) == autoroom.CHANNEL_NAME_LIMIT


def test_render_strips_surrounding_whitespace():
    assert autoroom.render_room_name("  {user}  ", "Yuki") == "Yuki"


# ---------------------------------------------------------------------------
# default_hub
# ---------------------------------------------------------------------------


def test_default_hub_defaults():
    hub = autoroom.default_hub(hub_channel_id=42)
    assert hub["label"] == autoroom.DEFAULT_LABEL
    assert hub["template"] == autoroom.DEFAULT_TEMPLATE
    assert hub["user_limit"] == 0
    assert hub["max_rooms"] == autoroom.DEFAULT_MAX_ROOMS
    assert hub["private"] is False
    assert hub["hub_channel_id"] == 42
    assert hub["category_id"] is None
    assert isinstance(hub["id"], str) and hub["id"]


def test_default_hub_generates_unique_ids():
    a = autoroom.default_hub(hub_channel_id=1)
    b = autoroom.default_hub(hub_channel_id=1)
    assert a["id"] != b["id"]


def test_default_hub_explicit_id_preserved():
    hub = autoroom.default_hub(id="fixed", hub_channel_id=1)
    assert hub["id"] == "fixed"


def test_default_hub_clamps_limits():
    hub = autoroom.default_hub(hub_channel_id=1, user_limit=999, max_rooms=999)
    assert hub["user_limit"] == 99
    assert hub["max_rooms"] == autoroom.MAX_ROOMS


def test_default_hub_is_normalized_shape():
    hub = autoroom.default_hub(hub_channel_id="55", category_id="7")
    # string ids coerced to ints via the normaliser
    assert hub["hub_channel_id"] == 55
    assert hub["category_id"] == 7


# ---------------------------------------------------------------------------
# normalize_hubs
# ---------------------------------------------------------------------------


def test_normalize_non_list_returns_empty():
    assert autoroom.normalize_hubs(None) == []
    assert autoroom.normalize_hubs({}) == []
    assert autoroom.normalize_hubs("nope") == []


def test_normalize_drops_non_dict_entries():
    hubs = autoroom.normalize_hubs([1, "x", None, {"hub_channel_id": 5}])
    assert len(hubs) == 1
    assert hubs[0]["hub_channel_id"] == 5


def test_normalize_drops_entry_without_hub_channel():
    hubs = autoroom.normalize_hubs([{"label": "no trigger"}])
    assert hubs == []


def test_normalize_drops_entry_with_bad_hub_channel():
    hubs = autoroom.normalize_hubs([{"hub_channel_id": "not-a-number"}])
    assert hubs == []


def test_normalize_bool_hub_channel_rejected():
    # a stray True must not masquerade as channel id 1
    hubs = autoroom.normalize_hubs([{"hub_channel_id": True}])
    assert hubs == []


def test_normalize_clamps_user_limit():
    hubs = autoroom.normalize_hubs(
        [
            {"hub_channel_id": 1, "user_limit": -5},
            {"hub_channel_id": 2, "user_limit": 500},
            {"hub_channel_id": 3, "user_limit": 10},
        ]
    )
    assert [h["user_limit"] for h in hubs] == [0, 99, 10]


def test_normalize_clamps_max_rooms():
    hubs = autoroom.normalize_hubs(
        [
            {"hub_channel_id": 1, "max_rooms": 0},
            {"hub_channel_id": 2, "max_rooms": 9999},
            {"hub_channel_id": 3, "max_rooms": 25},
        ]
    )
    assert [h["max_rooms"] for h in hubs] == [1, autoroom.MAX_ROOMS, 25]


def test_normalize_missing_keys_get_defaults():
    hubs = autoroom.normalize_hubs([{"hub_channel_id": 9}])
    hub = hubs[0]
    assert hub["label"] == autoroom.DEFAULT_LABEL
    assert hub["template"] == autoroom.DEFAULT_TEMPLATE
    assert hub["user_limit"] == 0
    assert hub["max_rooms"] == autoroom.DEFAULT_MAX_ROOMS
    assert hub["private"] is False
    assert hub["category_id"] is None
    assert isinstance(hub["id"], str) and hub["id"]


def test_normalize_blank_template_defaulted():
    hubs = autoroom.normalize_hubs([{"hub_channel_id": 1, "template": "   "}])
    assert hubs[0]["template"] == autoroom.DEFAULT_TEMPLATE


def test_normalize_preserves_valid_template():
    hubs = autoroom.normalize_hubs([{"hub_channel_id": 1, "template": "Room {n}"}])
    assert hubs[0]["template"] == "Room {n}"


def test_normalize_label_capped():
    hubs = autoroom.normalize_hubs([{"hub_channel_id": 1, "label": "L" * 250}])
    assert len(hubs[0]["label"]) == autoroom.CHANNEL_NAME_LIMIT


def test_normalize_private_coerced_to_bool():
    hubs = autoroom.normalize_hubs(
        [
            {"hub_channel_id": 1, "private": "yes"},
            {"hub_channel_id": 2, "private": 0},
        ]
    )
    assert hubs[0]["private"] is True
    assert hubs[1]["private"] is False


def test_normalize_preserves_existing_id():
    hubs = autoroom.normalize_hubs([{"hub_channel_id": 1, "id": "abc"}])
    assert hubs[0]["id"] == "abc"


def test_normalize_overflow_truncated_to_max_hubs():
    raw = [{"hub_channel_id": i} for i in range(1, 12)]
    hubs = autoroom.normalize_hubs(raw)
    assert len(hubs) == autoroom.MAX_HUBS
    # kept the first five valid triggers, in order
    assert [h["hub_channel_id"] for h in hubs] == [1, 2, 3, 4, 5]


def test_normalize_overflow_counts_only_valid_entries():
    # malformed entries interleaved must not consume a slot
    raw = [
        {"hub_channel_id": 1},
        "junk",
        {"hub_channel_id": 2},
        {"nope": True},
        {"hub_channel_id": 3},
        {"hub_channel_id": 4},
        {"hub_channel_id": 5},
        {"hub_channel_id": 6},
    ]
    hubs = autoroom.normalize_hubs(raw)
    assert [h["hub_channel_id"] for h in hubs] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# can_add_hub
# ---------------------------------------------------------------------------


def test_can_add_hub_below_cap():
    assert autoroom.can_add_hub([]) is True
    assert autoroom.can_add_hub([{}] * 4) is True


def test_can_add_hub_at_cap():
    assert autoroom.can_add_hub([{}] * 5) is False
    assert autoroom.can_add_hub([{}] * 6) is False


# ---------------------------------------------------------------------------
# channels_needed
# ---------------------------------------------------------------------------


def test_channels_needed_empty():
    assert autoroom.channels_needed([]) == 0


def test_channels_needed_single_hub():
    # overhead (category + trigger) + max_rooms
    hubs = [{"max_rooms": 20}]
    assert autoroom.channels_needed(hubs) == autoroom.HUB_OVERHEAD_CHANNELS + 20


def test_channels_needed_sums_and_clamps():
    hubs = [{"max_rooms": 10}, {"max_rooms": 9999}, {"max_rooms": 0}]
    expected = (
        (autoroom.HUB_OVERHEAD_CHANNELS + 10)
        + (autoroom.HUB_OVERHEAD_CHANNELS + autoroom.MAX_ROOMS)
        + (autoroom.HUB_OVERHEAD_CHANNELS + 1)
    )
    assert autoroom.channels_needed(hubs) == expected


def test_channels_needed_defaults_missing_max_rooms():
    hubs = [{}]
    assert autoroom.channels_needed(hubs) == (
        autoroom.HUB_OVERHEAD_CHANNELS + autoroom.DEFAULT_MAX_ROOMS
    )


# ---------------------------------------------------------------------------
# summarise_hub
# ---------------------------------------------------------------------------


def test_summarise_hub_full():
    hub = autoroom.default_hub(
        hub_channel_id=1,
        label="Ranked",
        template="{user} ranked",
        user_limit=5,
        max_rooms=12,
        private=True,
    )
    text = autoroom.summarise_hub(hub)
    assert "Ranked" in text
    assert "limit 5" in text
    assert "up to 12 rooms" in text
    assert "private" in text
    assert "{user} ranked" in text


def test_summarise_hub_unlimited_and_open():
    hub = autoroom.default_hub(hub_channel_id=1, user_limit=0, private=False)
    text = autoroom.summarise_hub(hub)
    assert "unlimited" in text
    assert "open" in text


def test_summarise_hub_defensive_on_partial_dict():
    # must not raise on a bare/garbage dict
    text = autoroom.summarise_hub({})
    assert autoroom.DEFAULT_LABEL in text
    assert "unlimited" in text
