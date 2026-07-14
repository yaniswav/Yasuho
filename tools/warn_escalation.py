"""Pure decision engine for per-guild warn escalation (moderation).

A guild configures an ordered list of escalation rules - "at N warns, take
action A" - persisted as a bounded JSONB list on ``guild_settings`` (key
:data:`SETTINGS_KEY`). This module owns every PURE decision the feature makes:
validating a single rule, validating and normalizing a whole policy, resolving
the stored (possibly absent or malformed) payload into a usable policy, and the
one runtime question - given a member's new warn count, which rule (if any)
fires. It has no discord, no database, and no awaits; the moderation/automod
cogs own the settings read/write, the Discord action calls, and the panel
(house pattern, mirroring tools/level_rewards.py).

Escalation semantics (documented + tested):

* A rule fires when the member's post-warn count EQUALS its threshold, never on
  ``>=``. So re-warning a member already past a threshold does NOT re-fire that
  rule; only landing on the exact count does. Removing a warn and warning again
  re-crosses the threshold and fires again (by design - crossing is the event).
* An UNCONFIGURED guild (no stored key) uses :func:`default_policy` = kick at 3,
  reproducing the bot's historical hardcoded behaviour exactly. Zero change for
  existing servers.
* An explicit empty list is a valid, DISTINCT state: escalation is OFF.
* A malformed stored payload falls back to the default policy (fail safe); the
  loader (tools.modactions.load_escalation_policy) logs a warning in that case.
* Changing or removing rules never retro-applies: a rule fires only at the
  moment a warn lands on its threshold, so editing the policy never sweeps the
  existing member counts.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

# Where the policy lives inside the guild_settings JSONB blob.
SETTINGS_KEY = "warn_escalation"

# Bounds - also the panel's validation source of truth.
MIN_THRESHOLD = 1
MAX_THRESHOLD = 50
MAX_RULES = 10

TIMEOUT = "timeout"
KICK = "kick"
BAN = "ban"
VALID_ACTIONS = (TIMEOUT, KICK, BAN)

# Discord timeouts cap at 28 days; a rule's timeout duration is clamped into
# [MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS].
MIN_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 600  # 10 minutes
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60  # 2419200 - Discord's hard cap

# The one default rule an unconfigured guild uses: kick at 3 warns (the bot's
# historical hardcoded behaviour).
_DEFAULT_THRESHOLD = 3


def default_policy():
    """A fresh copy of the unconfigured-guild policy: kick at 3 warns.

    Returned as a new list of new dicts every call so a caller can mutate the
    result freely without corrupting the shared default.
    """
    return [{"threshold": _DEFAULT_THRESHOLD, "action": KICK, "duration": None}]


def clamp_timeout(seconds):
    """Clamp a requested timeout (in seconds) into Discord's allowed range.

    A non-integer / unparseable input falls back to :data:`DEFAULT_TIMEOUT_SECONDS`
    rather than raising, so a stray value can never crash the escalation path.
    """
    if isinstance(seconds, bool):  # bool is an int subclass - never a duration
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if seconds < MIN_TIMEOUT_SECONDS:
        return MIN_TIMEOUT_SECONDS
    if seconds > MAX_TIMEOUT_SECONDS:
        return MAX_TIMEOUT_SECONDS
    return seconds


def normalize_rule(raw):
    """Validate and normalize one rule dict; raise ``ValueError`` if invalid.

    A rule is ``{"threshold": int, "action": str, "duration": int|None}``. The
    threshold must be an int in ``[MIN_THRESHOLD, MAX_THRESHOLD]`` (booleans are
    rejected - ``bool`` is an ``int`` subclass). For a ``timeout`` action the
    duration is clamped into Discord's range (defaulting when absent); for
    ``kick``/``ban`` the duration is forced to ``None`` (a kick/ban has none).
    """
    if not isinstance(raw, dict):
        raise ValueError("rule must be an object")
    action = raw.get("action")
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    threshold = raw.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ValueError("threshold must be an int")
    if not (MIN_THRESHOLD <= threshold <= MAX_THRESHOLD):
        raise ValueError("threshold out of range")
    if action == TIMEOUT:
        duration = clamp_timeout(raw.get("duration", DEFAULT_TIMEOUT_SECONDS))
    else:
        duration = None
    return {"threshold": threshold, "action": action, "duration": duration}


def validate_policy(rules):
    """Validate a whole policy; return a normalized, threshold-sorted list.

    Raises ``ValueError`` when the input is not a list, holds more than
    :data:`MAX_RULES` rules, contains an invalid rule, or repeats a threshold
    (thresholds are unique - a count can fire at most one rule). An empty list
    is valid and returns ``[]`` (escalation off).
    """
    if not isinstance(rules, (list, tuple)):
        raise ValueError("policy must be a list")
    if len(rules) > MAX_RULES:
        raise ValueError("too many rules")
    normalized = [normalize_rule(rule) for rule in rules]
    thresholds = [rule["threshold"] for rule in normalized]
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("duplicate thresholds")
    normalized.sort(key=lambda rule: rule["threshold"])
    return normalized


def resolve_policy(raw):
    """Resolve a stored settings value into a usable policy.

    Returns ``(policy, malformed)``. ``raw is None`` (key absent) -> the default
    policy, ``malformed=False`` (unconfigured, NOT an error). A valid list
    (including an empty one) -> its normalized form, ``malformed=False``.
    Anything else (a corrupt blob, wrong type, bad rule, duplicate threshold,
    over the cap) -> the default policy with ``malformed=True`` so the caller can
    log it. Never raises.
    """
    if raw is None:
        return default_policy(), False
    try:
        return validate_policy(raw), False
    except (ValueError, TypeError):
        return default_policy(), True


def action_for_count(policy, count):
    """The rule that fires at exactly ``count`` warns, or ``None``.

    Fires on EQUALITY only (never ``>=``): a member re-warned past a threshold
    does not re-trigger it. Thresholds are unique, so at most one rule matches.
    """
    for rule in policy:
        if rule["threshold"] == count:
            return rule
    return None


def can_add_rule(existing_count):
    """Whether one more rule may be added given how many the guild already has."""
    return existing_count < MAX_RULES


def upsert_rule(policy, threshold, action, duration=None):
    """Return a NEW policy with the rule at ``threshold`` set or replaced.

    A threshold is unique, so adding one that already exists REPLACES that
    rule's action/duration rather than duplicating it. Raises ``ValueError``
    when the rule itself is invalid (via :func:`normalize_rule`) or when adding
    a genuinely NEW threshold would exceed :data:`MAX_RULES`. The input policy is
    left untouched (a fresh, sorted list is returned).
    """
    rule = normalize_rule(
        {"threshold": threshold, "action": action, "duration": duration}
    )
    kept = [r for r in policy if r["threshold"] != rule["threshold"]]
    is_new = len(kept) == len(policy)
    if is_new and not can_add_rule(len(policy)):
        raise ValueError("too many rules")
    kept.append(rule)
    kept.sort(key=lambda r: r["threshold"])
    return kept


def remove_threshold(policy, threshold):
    """Return a NEW policy with the rule at ``threshold`` removed (if present)."""
    return [r for r in policy if r["threshold"] != threshold]
