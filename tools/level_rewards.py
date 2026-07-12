"""Pure, synchronous decision engine for level-up role rewards (leveling L2).

A guild's admin configures "reach level N, get role R" rules (the level_rewards
table). This module answers exactly one question with no discord, no database,
no awaits: given the guild's rules, its rewards_mode, and a member who just
leveled up, which reward roles should be ADDED and (in 'replace' mode) REMOVED?
The cog (cogs/community/level_rewards.py) owns the DB reads, the role
add/remove calls, the hierarchy check and the lazy pruning of stale rules; this
module only computes the two role-id sets.

Design: "owed" is always recomputed as every rule at or below the member's NEW
level, never just the levels crossed by this jump. That single choice is what
delivers catch-up-on-demand for free: a rule added for a level the member
already passed is simply part of "owed" the next time they level up, with no
retroactive sweep needed. It also makes a multi-level jump correct by
construction - a member jumping straight past three reward levels in stack mode
receives all three, and in replace mode ends up holding only the top tier.
"""

from __future__ import annotations

# Discord caps a single select at 25 options, and the admin-facing "remove a
# rule" picker (cogs/community/level_rewards.py) lists every rule in one select
# - so this cap is also what keeps that picker within Discord's own limit.
MAX_REWARDS_PER_GUILD = 25

STACK = "stack"
REPLACE = "replace"
VALID_MODES = (STACK, REPLACE)
DEFAULT_MODE = STACK


def can_add_rule(existing_count, cap=MAX_REWARDS_PER_GUILD):
    """Whether one more rule may be added given how many the guild already has."""
    return existing_count < cap


def owed_role_ids(rules, level, mode):
    """The role ids a member AT ``level`` should hold, for this guild's rules.

    ``rules`` is an iterable of ``(level, role_id)`` pairs (repeats and
    out-of-order input are fine). In :data:`STACK` mode this is the union of
    every rule at or below ``level`` (every reward ever earned). In
    :data:`REPLACE` mode this is only the role(s) tied to the single HIGHEST
    rule level at or below ``level`` (ties at that level all count - a level can
    carry more than one reward role). No eligible rule yields an empty set.
    """
    eligible = [(lvl, rid) for lvl, rid in rules if lvl <= level]
    if not eligible:
        return frozenset()
    if mode == REPLACE:
        top = max(lvl for lvl, _rid in eligible)
        return frozenset(rid for lvl, rid in eligible if lvl == top)
    return frozenset(rid for _lvl, rid in eligible)


def decide_role_changes(rules, mode, old_level, new_level, held_role_ids):
    """The (to_add, to_remove) role-id sets for a level-up from old to new.

    ``rules``: this guild's ``(level, role_id)`` reward rules (any order).
    ``mode``: :data:`STACK` or :data:`REPLACE` (any other value is treated as
    stack, the safer of the two - it never removes a role).
    ``old_level``/``new_level``: the level-up span; if ``new_level`` does not
    exceed ``old_level`` this is not a level-up and both sets come back empty
    (a defensive no-op, mirroring ``leveling.level_up_between``'s own gate).
    ``held_role_ids``: role ids the member currently holds, so the sets returned
    are a genuine diff (idempotent: re-running with the same held set after
    applying the first result yields two empty sets).

    ``to_remove`` is always empty in stack mode - earned roles are never taken
    away. In replace mode it is limited to roles that appear in ``rules`` at
    SOME level (the guild's own reward roles) so a member's unrelated roles are
    never touched, even if their id happens to collide with nothing here.
    """
    if new_level <= old_level:
        return frozenset(), frozenset()

    rules = list(rules)
    held = frozenset(held_role_ids)
    owed = owed_role_ids(rules, new_level, mode)
    to_add = owed - held

    if mode != REPLACE:
        return to_add, frozenset()

    all_reward_role_ids = frozenset(rid for _lvl, rid in rules)
    to_remove = (held & all_reward_role_ids) - owed
    return to_add, to_remove


def reconcile_to_level(rules, mode, level, held_role_ids):
    """The ``(to_add, to_remove)`` sets to make a member's reward roles match
    ``level`` again after an admin XP edit MOVED them below a tier (leveling L5).

    This is the level-DOWN counterpart to :func:`decide_role_changes` (which
    only ever fires on a level UP - it returns two empty sets when
    ``new_level <= old_level``). The two directions are deliberately different:

    * :data:`STACK` mode is a total no-op here - both sets come back empty.
      Earned roles are KEPT even when an admin removes XP (the documented
      convention: a member who genuinely reached a tier does not lose its role
      just because their number went down), and nothing is force-added on a
      downward move either.
    * :data:`REPLACE` mode RECOMPUTES the tier: it removes any reward role the
      member holds that is no longer owed at ``level`` (the higher tiers they
      fell below) and adds the new tier's role(s) if somehow not already held.
      ``to_remove`` is limited to roles that appear in ``rules`` at some level,
      so a member's unrelated roles are never touched.

    ``held_role_ids`` is the set of role ids the member currently holds, so the
    result is a genuine diff (idempotent: re-running after applying it yields two
    empty sets).
    """
    if mode != REPLACE:
        return frozenset(), frozenset()
    rules = list(rules)
    held = frozenset(held_role_ids)
    owed = owed_role_ids(rules, level, REPLACE)
    to_add = owed - held
    all_reward_role_ids = frozenset(rid for _lvl, rid in rules)
    to_remove = (held & all_reward_role_ids) - owed
    return to_add, to_remove


def group_by_level(rules):
    """``{level: [role_id, ...]}`` from a rule list, for the list-card rendering.

    Role ids within a level keep their input order; levels come back as plain
    dict keys (the caller sorts, e.g. ``sorted(grouped)``) so this stays a
    trivial, allocation-light grouping with no opinion on presentation order.
    """
    grouped: dict[int, list[int]] = {}
    for level, role_id in rules:
        grouped.setdefault(level, []).append(role_id)
    return grouped
