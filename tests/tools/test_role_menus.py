"""Unit tests for tools.role_menus (pure selection + config helpers)."""

from tools import role_menus as rm


def test_add_and_remove_within_menu_only():
    # menu manages 1,2,3; user holds 2 and an outside role 9; picks 1 and 3.
    to_add, to_remove = rm.resolve_selection(
        selected_ids=[1, 3], held_ids=[2, 9], menu_ids=[1, 2, 3], exclusive=False
    )
    assert to_add == {1, 3}
    assert to_remove == {2}  # 9 is outside the menu, never touched


def test_deselecting_removes():
    to_add, to_remove = rm.resolve_selection(
        selected_ids=[], held_ids=[1, 2], menu_ids=[1, 2, 3], exclusive=False
    )
    assert to_add == set()
    assert to_remove == {1, 2}


def test_exclusive_keeps_one():
    # exclusive menu, user somehow selected two -> keep the lowest, drop the rest
    to_add, to_remove = rm.resolve_selection(
        selected_ids=[3, 1], held_ids=[], menu_ids=[1, 2, 3], exclusive=True
    )
    assert to_add == {1}
    assert to_remove == set()


def test_exclusive_swap():
    to_add, to_remove = rm.resolve_selection(
        selected_ids=[2], held_ids=[1], menu_ids=[1, 2, 3], exclusive=True
    )
    assert to_add == {2}
    assert to_remove == {1}


def test_selection_ignores_ids_outside_menu():
    to_add, to_remove = rm.resolve_selection(
        selected_ids=[99], held_ids=[], menu_ids=[1, 2], exclusive=False
    )
    assert to_add == set() and to_remove == set()


def test_normalize_options_cleans_and_caps():
    raw = [
        {"role_id": 1, "label": "Red"},
        {"role_id": 1, "label": "dup"},          # duplicate -> dropped
        {"role_id": "x"},                          # bad id -> dropped
        {"role_id": True},                         # bool -> dropped
        {"role_id": 2, "label": "", "emoji": "🔵", "description": "d" * 200},
        "junk",                                    # not a dict -> dropped
    ]
    out = rm.normalize_options(raw)
    assert [o["role_id"] for o in out] == [1, 2]
    assert out[0]["label"] == "Red"
    assert out[1]["label"] == "2"                  # empty label falls back to id
    assert out[1]["emoji"] == "🔵"
    assert len(out[1]["description"]) == rm.MAX_DESCRIPTION


def test_normalize_options_non_list():
    assert rm.normalize_options(None) == []
    assert rm.normalize_options({"role_id": 1}) == []
