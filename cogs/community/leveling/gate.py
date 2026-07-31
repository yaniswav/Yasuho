"""Pure, synchronous helpers for the leveling XP hot path (on_message).

on_message runs for EVERY message on EVERY guild, so the decisions that gate
whether a message can earn XP are kept here as pure functions over plain values
(no bot, no I/O, no awaits) - trivially unit-tested and allocation-light. The
stateful pieces (the enabled-guild set, the per-user cooldown) live on the cog;
this module holds only the decision logic.
"""

from __future__ import annotations


def is_command_invocation(content, prefixes):
    """True when ``content`` starts with any command prefix in ``prefixes``.

    A message that opens with the guild's command prefix (or a bot mention) is
    treated as a command and earns no XP - even when it resolves to no real
    command (a typo'd ``?lol``). Skipping that near-miss too is deliberate: it
    keeps this a pure ``startswith`` test with no command-registry lookup, and
    "looks like a command" is a fair proxy for "not organic chat". Empty or blank
    prefixes are ignored so a stray "" can never swallow every message.
    """
    for prefix in prefixes:
        if prefix and content.startswith(prefix):
            return True
    return False
