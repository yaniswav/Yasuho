"""Unit tests for tools.level_admin (pure /xp value maths, leveling L5).

Bounds validation (give/take amount, set absolute total), the floored
resolve_new_xp, and the resetall name-match gate - all pure, no discord, no DB.
Level-crossing detection and the pager live in tools.leveling (tested in
test_leveling_service.py); the role reconcile lives in tools.level_rewards.
"""

from tools import level_admin as la

# ---------------------------------------------------------------------------
# validate_adjust_amount (give / take: 1 .. 1_000_000)
# ---------------------------------------------------------------------------


def test_adjust_amount_in_range():
    assert la.validate_adjust_amount(1) == (True, None)
    assert la.validate_adjust_amount(1_000_000) == (True, None)
    assert la.validate_adjust_amount(500) == (True, None)


def test_adjust_amount_below_min_is_rejected():
    assert la.validate_adjust_amount(0) == (False, "out_of_range")
    assert la.validate_adjust_amount(-5) == (False, "out_of_range")


def test_adjust_amount_above_max_is_rejected():
    assert la.validate_adjust_amount(1_000_001) == (False, "out_of_range")


def test_adjust_amount_rejects_bool_and_non_int():
    # True/False are int subclasses in Python; they must not slip through as 1/0.
    assert la.validate_adjust_amount(True) == (False, "out_of_range")
    assert la.validate_adjust_amount(False) == (False, "out_of_range")
    assert la.validate_adjust_amount(1.5) == (False, "out_of_range")
    assert la.validate_adjust_amount("5") == (False, "out_of_range")


# ---------------------------------------------------------------------------
# validate_set_xp (set: 0 .. 10_000_000, 0 is IN range)
# ---------------------------------------------------------------------------


def test_set_xp_in_range_including_zero():
    assert la.validate_set_xp(0) == (True, None)
    assert la.validate_set_xp(10_000_000) == (True, None)
    assert la.validate_set_xp(42) == (True, None)


def test_set_xp_negative_is_rejected():
    assert la.validate_set_xp(-1) == (False, "out_of_range")


def test_set_xp_above_max_is_rejected():
    assert la.validate_set_xp(10_000_001) == (False, "out_of_range")


def test_set_xp_rejects_bool_and_non_int():
    assert la.validate_set_xp(True) == (False, "out_of_range")
    assert la.validate_set_xp(3.0) == (False, "out_of_range")


# ---------------------------------------------------------------------------
# resolve_new_xp (floored at 0 in every branch)
# ---------------------------------------------------------------------------


def test_give_adds():
    assert la.resolve_new_xp(la.GIVE, 100, 50) == 150


def test_take_subtracts():
    assert la.resolve_new_xp(la.TAKE, 100, 40) == 60


def test_take_floors_at_zero_never_negative():
    assert la.resolve_new_xp(la.TAKE, 30, 100) == 0
    assert la.resolve_new_xp(la.TAKE, 0, 5) == 0


def test_set_replaces_the_total():
    assert la.resolve_new_xp(la.SET, 999, 0) == 0
    assert la.resolve_new_xp(la.SET, 10, 5000) == 5000


def test_give_from_zero():
    assert la.resolve_new_xp(la.GIVE, 0, 25) == 25


def test_unknown_action_is_a_no_op():
    assert la.resolve_new_xp("bogus", 77, 5) == 77


# ---------------------------------------------------------------------------
# confirm_name_matches (the resetall second gate)
# ---------------------------------------------------------------------------


def test_name_match_exact():
    assert la.confirm_name_matches("My Server", "My Server") is True


def test_name_match_strips_surrounding_whitespace():
    assert la.confirm_name_matches("  My Server  ", "My Server") is True


def test_name_match_is_case_sensitive():
    assert la.confirm_name_matches("my server", "My Server") is False


def test_name_match_wrong_name_fails():
    assert la.confirm_name_matches("Other", "My Server") is False


def test_name_match_empty_never_matches():
    assert la.confirm_name_matches("", "My Server") is False
    assert la.confirm_name_matches(None, "My Server") is False
    assert la.confirm_name_matches("My Server", "") is False


# ---------------------------------------------------------------------------
# constants / bounds are the promised values
# ---------------------------------------------------------------------------


def test_bounds_constants():
    assert la.MIN_ADJUST_AMOUNT == 1
    assert la.MAX_ADJUST_AMOUNT == 1_000_000
    assert la.MIN_SET_XP == 0
    assert la.MAX_SET_XP == 10_000_000
    assert la.ACTIONS == (la.GIVE, la.TAKE, la.SET)
