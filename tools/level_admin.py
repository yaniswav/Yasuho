"""Pure, synchronous decisions for the admin XP tools (leveling L5).

An admin adjusts a member's XP through the ``/xp`` group (give / take / set /
reset / resetall). This module answers the small, testable questions those
commands lean on with no discord, no database, no awaits: are the amounts in
range, what is the resulting (floored) XP total, and does the typed guild name
match the one required to confirm the destructive ``resetall``. The cog
(cogs/community/level_admin.py) owns the DB reads/writes, the confirmation
views/modals, and the reward/announce routing; this module only computes values.

Level-crossing detection (both directions) and the leaderboard pager live in
tools/leveling.py (the leveling curve's home): ``level_up_between`` /
``level_down_between`` and ``leaderboard_page``. Role reconciliation on a level
change lives in tools/level_rewards.py (``reconcile_to_level``). This file is
deliberately just the ``/xp`` value maths.

Typography rule: ASCII '-' and '...' only. No em/en dashes or fancy ellipsis.
"""

from __future__ import annotations

# give / take amount bounds: a single adjustment moves at least 1 and at most a
# million XP, so a fat-fingered command can never swing a member by billions.
MIN_ADJUST_AMOUNT = 1
MAX_ADJUST_AMOUNT = 1_000_000

# set bounds: an absolute total may be anything from 0 (a soft reset) up to ten
# million XP (level ~316 on the sqrt curve - far beyond any organic total, but a
# hard ceiling so `set` can never store an absurd value).
MIN_SET_XP = 0
MAX_SET_XP = 10_000_000

GIVE = "give"
TAKE = "take"
SET = "set"
ACTIONS = (GIVE, TAKE, SET)


def validate_adjust_amount(amount):
    """Validate a give/take amount. Returns ``(ok, reason)``.

    ``reason`` is ``None`` on success or ``"out_of_range"`` - a short code the
    cog turns into localized text (this module carries no i18n dependency, like
    every other tools/*.py decision engine). A bool is rejected explicitly:
    ``True``/``False`` are ``int`` subclasses in Python and would otherwise slip
    through the range test as 1/0.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        return False, "out_of_range"
    if amount < MIN_ADJUST_AMOUNT or amount > MAX_ADJUST_AMOUNT:
        return False, "out_of_range"
    return True, None


def validate_set_xp(value):
    """Validate an absolute XP total for ``/xp set``. Returns ``(ok, reason)``
    with the same short-code contract as :func:`validate_adjust_amount`. 0 is IN
    range (a soft reset to zero XP is an explicitly supported outcome).
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return False, "out_of_range"
    if value < MIN_SET_XP or value > MAX_SET_XP:
        return False, "out_of_range"
    return True, None


def resolve_new_xp(action, current_xp, amount):
    """The member's XP total after an admin ``action`` (give/take/set).

    Pure and floored at 0 in every branch: ``give`` adds, ``take`` subtracts and
    can never drive the total negative (``max(0, ...)``), and ``set`` stores the
    absolute value as-is (the caller has already validated it into
    ``[MIN_SET_XP, MAX_SET_XP]``). An unknown action returns ``current_xp``
    unchanged (a defensive no-op; the cog only ever passes a known one).
    """
    if action == GIVE:
        return current_xp + amount
    if action == TAKE:
        return max(0, current_xp - amount)
    if action == SET:
        return amount
    return current_xp


def confirm_name_matches(typed, guild_name):
    """Whether ``typed`` matches ``guild_name`` closely enough to confirm the
    destructive ``resetall``. Both sides are stripped of surrounding whitespace
    (a copy-paste often carries a leading/trailing space), then compared
    EXACTLY - case-sensitive, so an admin must reproduce the guild's real name,
    never a lazy lower-cased approximation. ``None``/empty never matches.
    """
    if not typed or not guild_name:
        return False
    return typed.strip() == guild_name.strip()
