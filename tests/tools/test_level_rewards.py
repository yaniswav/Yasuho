"""Unit tests for tools.level_rewards (pure grant-decision engine).

decide_role_changes is the whole contract: given a guild's reward rules, its
mode, a level-up span, and the roles a member currently holds, which role ids
get added and (replace mode only) removed. These tests pin stack vs replace,
multi-level jumps (including catch-up for a rule added below the member's
already-passed level), idempotency, and the "no rules" / no-level-up no-ops.
"""

from tools import level_rewards as lr

# ---------------------------------------------------------------------------
# owed_role_ids
# ---------------------------------------------------------------------------


def test_owed_stack_unions_every_rule_at_or_below_level():
    rules = [(1, 10), (5, 20), (10, 30)]
    assert lr.owed_role_ids(rules, 7, lr.STACK) == {10, 20}
    assert lr.owed_role_ids(rules, 10, lr.STACK) == {10, 20, 30}


def test_owed_replace_keeps_only_the_highest_tier():
    rules = [(1, 10), (5, 20), (10, 30)]
    assert lr.owed_role_ids(rules, 7, lr.REPLACE) == {20}
    assert lr.owed_role_ids(rules, 4, lr.REPLACE) == {10}


def test_owed_replace_ties_at_the_top_level_all_count():
    """Two roles configured for the SAME level both count as the top tier."""
    rules = [(1, 10), (5, 20), (5, 21)]
    assert lr.owed_role_ids(rules, 5, lr.REPLACE) == {20, 21}


def test_owed_below_every_rule_is_empty():
    rules = [(5, 20), (10, 30)]
    assert lr.owed_role_ids(rules, 1, lr.STACK) == frozenset()
    assert lr.owed_role_ids(rules, 1, lr.REPLACE) == frozenset()


def test_owed_no_rules_is_empty():
    assert lr.owed_role_ids([], 50, lr.STACK) == frozenset()
    assert lr.owed_role_ids([], 50, lr.REPLACE) == frozenset()


# ---------------------------------------------------------------------------
# decide_role_changes - stack mode
# ---------------------------------------------------------------------------


def test_stack_single_level_grants_that_levels_role():
    rules = [(5, 20)]
    to_add, to_remove = lr.decide_role_changes(rules, lr.STACK, 4, 5, held_role_ids=[])
    assert to_add == {20}
    assert to_remove == frozenset()


def test_stack_never_removes():
    rules = [(1, 10), (5, 20)]
    # holds a role tied to a rule that is still owed at the new level - nothing
    # should ever be removed in stack mode, regardless of what's held.
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.STACK, 4, 5, held_role_ids=[10]
    )
    assert to_add == {20}
    assert to_remove == frozenset()


def test_stack_multi_level_jump_grants_everything_owed():
    """A user jumping 3 levels (2 -> 5) gets every rule crossed, in one shot."""
    rules = [(3, 10), (4, 20), (5, 30)]
    to_add, to_remove = lr.decide_role_changes(rules, lr.STACK, 2, 5, held_role_ids=[])
    assert to_add == {10, 20, 30}
    assert to_remove == frozenset()


def test_stack_catch_up_grants_a_rule_added_below_the_current_level():
    """A rule for a level the member already passed is owed at their NEXT
    level-up (catch-up on demand), even though the jump itself doesn't cross
    that rule's level."""
    rules = [(2, 10)]  # added after the member was already level 8
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.STACK, 8, 9, held_role_ids=[]
    )
    assert to_add == {10}
    assert to_remove == frozenset()


def test_stack_already_held_roles_are_not_re_added():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.STACK, 4, 5, held_role_ids=[10, 20]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()


# ---------------------------------------------------------------------------
# decide_role_changes - replace mode
# ---------------------------------------------------------------------------


def test_replace_single_level_grants_and_holds_nothing_else():
    rules = [(5, 20)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.REPLACE, 4, 5, held_role_ids=[]
    )
    assert to_add == {20}
    assert to_remove == frozenset()


def test_replace_swaps_the_previous_tier_for_the_new_one():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.REPLACE, 4, 5, held_role_ids=[10]
    )
    assert to_add == {20}
    assert to_remove == {10}


def test_replace_multi_level_jump_lands_on_only_the_top_tier():
    """A user jumping 3 levels in replace mode ends up with exactly the
    highest tier owed - lower tiers along the way are never granted at all."""
    rules = [(3, 10), (4, 20), (5, 30)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.REPLACE, 2, 5, held_role_ids=[]
    )
    assert to_add == {30}
    assert to_remove == frozenset()


def test_replace_never_touches_a_role_outside_the_rule_set():
    """A member's unrelated role (not tied to any rule) must never be removed,
    even if it isn't in the owed set."""
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.REPLACE, 4, 5, held_role_ids=[10, 999]
    )
    assert to_add == {20}
    assert to_remove == {10}  # 999 (not a reward role) is left alone


def test_replace_idempotent_once_applied():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.REPLACE, 4, 5, held_role_ids=[20]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()


# ---------------------------------------------------------------------------
# no-op guards
# ---------------------------------------------------------------------------


def test_no_rules_is_a_no_op_in_either_mode():
    for mode in (lr.STACK, lr.REPLACE):
        to_add, to_remove = lr.decide_role_changes(
            [], mode, 4, 5, held_role_ids=[]
        )
        assert to_add == frozenset()
        assert to_remove == frozenset()


def test_non_level_up_span_is_a_no_op():
    """new_level <= old_level (no level-up occurred) must never grant/remove."""
    rules = [(1, 10)]
    to_add, to_remove = lr.decide_role_changes(
        rules, lr.STACK, 5, 5, held_role_ids=[]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()

    to_add, to_remove = lr.decide_role_changes(
        rules, lr.STACK, 5, 3, held_role_ids=[]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()


def test_unknown_mode_behaves_like_stack_and_never_removes():
    """An unrecognised mode value falls back to the safer stack behaviour."""
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.decide_role_changes(
        rules, "not-a-real-mode", 4, 5, held_role_ids=[10]
    )
    assert to_add == {20}
    assert to_remove == frozenset()


# ---------------------------------------------------------------------------
# can_add_rule (the 25-rule cap)
# ---------------------------------------------------------------------------


def test_can_add_rule_below_cap():
    assert lr.can_add_rule(0) is True
    assert lr.can_add_rule(lr.MAX_REWARDS_PER_GUILD - 1) is True


def test_can_add_rule_at_or_above_cap_refuses():
    assert lr.can_add_rule(lr.MAX_REWARDS_PER_GUILD) is False
    assert lr.can_add_rule(lr.MAX_REWARDS_PER_GUILD + 1) is False


# ---------------------------------------------------------------------------
# group_by_level (the /levelrewards list rendering data)
# ---------------------------------------------------------------------------


def test_group_by_level_groups_and_preserves_role_order():
    rules = [(5, 20), (1, 10), (5, 21)]
    grouped = lr.group_by_level(rules)
    assert grouped == {5: [20, 21], 1: [10]}


def test_group_by_level_empty_input():
    assert lr.group_by_level([]) == {}


# ---------------------------------------------------------------------------
# reconcile_to_level - the level-DOWN decision for the admin XP tools (L5).
# The UP case still routes through decide_role_changes; this fires only when an
# admin edit dropped a member below a tier.
# ---------------------------------------------------------------------------


def test_reconcile_stack_is_a_total_no_op():
    """Stack mode KEEPS earned roles on XP loss - the documented convention -
    so both sets come back empty regardless of what is held or owed."""
    rules = [(1, 10), (5, 20), (10, 30)]
    # Member held the top two tiers but an admin dropped them to level 5.
    to_add, to_remove = lr.reconcile_to_level(
        rules, lr.STACK, 5, held_role_ids=[10, 20, 30]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()


def test_reconcile_replace_removes_tiers_above_the_new_level():
    """Replace mode recomputes the tier: a member dropped from level 10 to 5
    keeps only the level-5 tier and loses the level-10 role."""
    rules = [(1, 10), (5, 20), (10, 30)]
    to_add, to_remove = lr.reconcile_to_level(
        rules, lr.REPLACE, 5, held_role_ids=[20, 30]
    )
    assert to_add == frozenset()   # already holds the owed (level-5) role
    assert to_remove == {30}       # the level-10 tier is stripped


def test_reconcile_replace_adds_the_new_tier_if_somehow_unheld():
    rules = [(1, 10), (5, 20), (10, 30)]
    to_add, to_remove = lr.reconcile_to_level(
        rules, lr.REPLACE, 5, held_role_ids=[30]
    )
    assert to_add == {20}      # the new tier is (re)granted
    assert to_remove == {30}   # the higher tier is removed


def test_reconcile_replace_never_touches_unrelated_roles():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.reconcile_to_level(
        rules, lr.REPLACE, 1, held_role_ids=[10, 20, 999]
    )
    assert to_remove == {20}   # 999 is not a reward role -> left alone
    assert 999 not in to_remove


def test_reconcile_replace_down_to_zero_strips_every_reward_role():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.reconcile_to_level(
        rules, lr.REPLACE, 0, held_role_ids=[10, 20]
    )
    assert to_add == frozenset()
    assert to_remove == {10, 20}


def test_reconcile_unknown_mode_behaves_like_stack():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.reconcile_to_level(
        rules, "not-a-mode", 1, held_role_ids=[10, 20]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()


def test_reconcile_replace_is_idempotent():
    rules = [(1, 10), (5, 20)]
    to_add, to_remove = lr.reconcile_to_level(
        rules, lr.REPLACE, 1, held_role_ids=[10]
    )
    assert to_add == frozenset()
    assert to_remove == frozenset()
