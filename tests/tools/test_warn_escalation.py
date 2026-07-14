"""Unit tests for tools.warn_escalation (pure warn-escalation decision engine).

This is the whole contract of the configurable warn-escalation feature: bounds
and shape validation of a single rule, validation + normalization of a whole
policy (cap, duplicate thresholds, sorting), resolving a stored (absent / valid /
malformed) JSONB payload into a usable policy, the equals-threshold firing
decision, and the add/remove policy transforms the panel drives. No discord, no
database, no awaits.
"""

import pytest

from tools import warn_escalation as we

# ---------------------------------------------------------------------------
# default_policy / constants
# ---------------------------------------------------------------------------


def test_default_policy_is_kick_at_three():
    assert we.default_policy() == [
        {"threshold": 3, "action": "kick", "duration": None}
    ]


def test_default_policy_returns_fresh_mutable_copies():
    a = we.default_policy()
    b = we.default_policy()
    assert a == b and a is not b
    a[0]["threshold"] = 99  # mutating one must never leak into the next call
    assert we.default_policy()[0]["threshold"] == 3


def test_valid_actions_are_exactly_timeout_kick_ban():
    assert set(we.VALID_ACTIONS) == {"timeout", "kick", "ban"}


def test_timeout_cap_is_28_days():
    assert we.MAX_TIMEOUT_SECONDS == 28 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# clamp_timeout
# ---------------------------------------------------------------------------


def test_clamp_timeout_within_range_unchanged():
    assert we.clamp_timeout(600) == 600


def test_clamp_timeout_floors_below_minimum():
    assert we.clamp_timeout(1) == we.MIN_TIMEOUT_SECONDS


def test_clamp_timeout_caps_above_discord_maximum():
    assert we.clamp_timeout(10**9) == we.MAX_TIMEOUT_SECONDS


def test_clamp_timeout_bad_input_falls_back_to_default():
    assert we.clamp_timeout("nonsense") == we.DEFAULT_TIMEOUT_SECONDS
    assert we.clamp_timeout(None) == we.DEFAULT_TIMEOUT_SECONDS
    # bool is an int subclass but is never a real duration.
    assert we.clamp_timeout(True) == we.DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# normalize_rule
# ---------------------------------------------------------------------------


def test_normalize_kick_rule_forces_duration_none():
    rule = we.normalize_rule({"threshold": 5, "action": "kick", "duration": 600})
    assert rule == {"threshold": 5, "action": "kick", "duration": None}


def test_normalize_ban_rule_forces_duration_none():
    rule = we.normalize_rule({"threshold": 7, "action": "ban"})
    assert rule == {"threshold": 7, "action": "ban", "duration": None}


def test_normalize_timeout_rule_clamps_duration():
    rule = we.normalize_rule({"threshold": 2, "action": "timeout", "duration": 5})
    assert rule["duration"] == we.MIN_TIMEOUT_SECONDS


def test_normalize_timeout_rule_defaults_missing_duration():
    rule = we.normalize_rule({"threshold": 2, "action": "timeout"})
    assert rule["duration"] == we.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "bad",
    [
        {"threshold": 3, "action": "warn"},       # 'warn' is not an escalation action
        {"threshold": 3, "action": "mute"},       # automod's word, not ours
        {"threshold": 3},                         # missing action
        {"action": "kick"},                       # missing threshold
        {"threshold": 0, "action": "kick"},       # below MIN_THRESHOLD
        {"threshold": 51, "action": "kick"},      # above MAX_THRESHOLD
        {"threshold": "3", "action": "kick"},     # threshold must be an int
        {"threshold": True, "action": "kick"},    # bool is not a threshold
        {"threshold": 3.0, "action": "kick"},     # float is not an int
        "not-a-dict",
        None,
    ],
)
def test_normalize_rule_rejects_bad_rules(bad):
    with pytest.raises(ValueError):
        we.normalize_rule(bad)


def test_normalize_rule_accepts_boundary_thresholds():
    assert we.normalize_rule({"threshold": 1, "action": "kick"})["threshold"] == 1
    assert we.normalize_rule({"threshold": 50, "action": "ban"})["threshold"] == 50


# ---------------------------------------------------------------------------
# validate_policy
# ---------------------------------------------------------------------------


def test_validate_policy_sorts_by_threshold():
    rules = [
        {"threshold": 7, "action": "ban"},
        {"threshold": 3, "action": "timeout", "duration": 600},
        {"threshold": 5, "action": "kick"},
    ]
    result = we.validate_policy(rules)
    assert [r["threshold"] for r in result] == [3, 5, 7]


def test_validate_policy_empty_is_valid_and_empty():
    assert we.validate_policy([]) == []


def test_validate_policy_rejects_duplicate_thresholds():
    with pytest.raises(ValueError):
        we.validate_policy(
            [{"threshold": 3, "action": "kick"}, {"threshold": 3, "action": "ban"}]
        )


def test_validate_policy_rejects_over_the_cap():
    too_many = [
        {"threshold": i, "action": "kick"} for i in range(1, we.MAX_RULES + 2)
    ]
    with pytest.raises(ValueError):
        we.validate_policy(too_many)


def test_validate_policy_accepts_exactly_the_cap():
    at_cap = [{"threshold": i, "action": "kick"} for i in range(1, we.MAX_RULES + 1)]
    assert len(we.validate_policy(at_cap)) == we.MAX_RULES


def test_validate_policy_rejects_non_list():
    with pytest.raises(ValueError):
        we.validate_policy({"threshold": 3, "action": "kick"})


# ---------------------------------------------------------------------------
# resolve_policy - the stored-payload resolver
# ---------------------------------------------------------------------------


def test_resolve_none_is_the_default_not_malformed():
    """An unconfigured guild (no key) -> the default policy, NOT flagged."""
    policy, malformed = we.resolve_policy(None)
    assert policy == we.default_policy()
    assert malformed is False


def test_resolve_valid_list_is_used_verbatim():
    stored = [{"threshold": 5, "action": "kick", "duration": None}]
    policy, malformed = we.resolve_policy(stored)
    assert policy == stored
    assert malformed is False


def test_resolve_empty_list_is_valid_escalation_off():
    """An explicit empty list is a distinct, valid state: escalation OFF."""
    policy, malformed = we.resolve_policy([])
    assert policy == []
    assert malformed is False


@pytest.mark.parametrize(
    "bad",
    [
        "a string",
        42,
        {"threshold": 3},                                  # a dict, not a list
        [{"threshold": 3, "action": "warn"}],              # bad rule inside
        [{"threshold": 3, "action": "kick"},
         {"threshold": 3, "action": "ban"}],               # duplicate thresholds
        [{"threshold": i, "action": "kick"} for i in range(60)],  # over the cap
        ["not-a-rule"],
    ],
)
def test_resolve_malformed_falls_back_to_default_flagged(bad):
    policy, malformed = we.resolve_policy(bad)
    assert policy == we.default_policy()
    assert malformed is True


# ---------------------------------------------------------------------------
# action_for_count - the runtime firing decision (EQUALS only)
# ---------------------------------------------------------------------------


def test_action_for_count_fires_on_exact_threshold():
    policy = [
        {"threshold": 3, "action": "timeout", "duration": 600},
        {"threshold": 5, "action": "kick", "duration": None},
    ]
    assert we.action_for_count(policy, 3)["action"] == "timeout"
    assert we.action_for_count(policy, 5)["action"] == "kick"


def test_action_for_count_does_not_fire_past_a_threshold():
    """The core semantic: a re-warn PAST a threshold does not re-fire it."""
    policy = [{"threshold": 3, "action": "kick", "duration": None}]
    assert we.action_for_count(policy, 4) is None
    assert we.action_for_count(policy, 10) is None


def test_action_for_count_below_first_threshold_is_none():
    policy = [{"threshold": 3, "action": "kick", "duration": None}]
    assert we.action_for_count(policy, 1) is None
    assert we.action_for_count(policy, 2) is None


def test_action_for_count_empty_policy_never_fires():
    assert we.action_for_count([], 3) is None


def test_default_policy_reproduces_kick_at_three():
    policy = we.default_policy()
    assert we.action_for_count(policy, 2) is None
    assert we.action_for_count(policy, 3)["action"] == "kick"
    assert we.action_for_count(policy, 4) is None  # not re-fired past 3


# ---------------------------------------------------------------------------
# can_add_rule / upsert_rule / remove_threshold - the panel transforms
# ---------------------------------------------------------------------------


def test_can_add_rule_below_and_at_cap():
    assert we.can_add_rule(0) is True
    assert we.can_add_rule(we.MAX_RULES - 1) is True
    assert we.can_add_rule(we.MAX_RULES) is False


def test_upsert_adds_a_new_rule_sorted():
    policy = [{"threshold": 5, "action": "kick", "duration": None}]
    result = we.upsert_rule(policy, 3, "timeout", 600)
    assert [r["threshold"] for r in result] == [3, 5]
    assert result[0] == {"threshold": 3, "action": "timeout", "duration": 600}


def test_upsert_replaces_an_existing_threshold():
    policy = [{"threshold": 3, "action": "kick", "duration": None}]
    result = we.upsert_rule(policy, 3, "ban")
    assert result == [{"threshold": 3, "action": "ban", "duration": None}]


def test_upsert_does_not_mutate_the_input_policy():
    policy = [{"threshold": 3, "action": "kick", "duration": None}]
    we.upsert_rule(policy, 5, "ban")
    assert policy == [{"threshold": 3, "action": "kick", "duration": None}]


def test_upsert_new_threshold_at_cap_raises():
    policy = [{"threshold": i, "action": "kick"} for i in range(1, we.MAX_RULES + 1)]
    policy = we.validate_policy(policy)
    with pytest.raises(ValueError):
        we.upsert_rule(policy, 99, "ban")


def test_upsert_replacing_at_cap_is_allowed():
    """Replacing an existing threshold at the cap must NOT be blocked (it is not
    a new rule)."""
    policy = [{"threshold": i, "action": "kick"} for i in range(1, we.MAX_RULES + 1)]
    policy = we.validate_policy(policy)
    result = we.upsert_rule(policy, 1, "ban")
    assert len(result) == we.MAX_RULES
    assert result[0]["action"] == "ban"


def test_upsert_clamps_timeout_duration():
    result = we.upsert_rule([], 3, "timeout", 5)
    assert result[0]["duration"] == we.MIN_TIMEOUT_SECONDS


def test_remove_threshold_drops_the_matching_rule():
    policy = [
        {"threshold": 3, "action": "kick", "duration": None},
        {"threshold": 5, "action": "ban", "duration": None},
    ]
    result = we.remove_threshold(policy, 3)
    assert result == [{"threshold": 5, "action": "ban", "duration": None}]


def test_remove_threshold_absent_is_a_no_op_copy():
    policy = [{"threshold": 3, "action": "kick", "duration": None}]
    result = we.remove_threshold(policy, 99)
    assert result == policy
    assert result is not policy
