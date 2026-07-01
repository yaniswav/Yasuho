"""Unit tests for the pure helpers in ``cogs/moderation/moderation.py``.

These cover the two module-level building blocks that have no live I/O:

* ``trim_reason`` - clamps a moderation reason to 100 characters, appending an
  ellipsis only when it actually clips.
* ``_MentionFallback`` - a slotted shim standing in for a user who has left the
  guild; it must render ``<@id>`` and deliberately expose no ``display_avatar``
  (so ``modactions.case_embed`` omits the thumbnail).

A small guard also asserts the module's ``discord.ui.View`` subclasses never
reintroduce the ``_refresh`` name collision that caused a production crash
(discord.py calls ``View._refresh(self, components)`` on MESSAGE_UPDATE; a
subclass method of the same name shadows it and crashes the gateway).
"""

import discord

from cogs.moderation.moderation import (
    ConfirmView,
    WarningsView,
    _MentionFallback,
    trim_reason,
)


# ---------------------------------------------------------------------------
# trim_reason
# ---------------------------------------------------------------------------


def test_trim_reason_short_unchanged():
    reason = "Spamming in general chat"
    assert trim_reason(reason) == reason


def test_trim_reason_empty_unchanged():
    assert trim_reason("") == ""


def test_trim_reason_exactly_100_unchanged():
    reason = "a" * 100
    result = trim_reason(reason)
    assert result == reason
    assert len(result) == 100
    assert not result.endswith("...")


def test_trim_reason_99_unchanged():
    reason = "b" * 99
    assert trim_reason(reason) == reason


def test_trim_reason_101_truncated():
    reason = "c" * 101
    result = trim_reason(reason)
    # First 100 characters preserved, then a literal three-dot ellipsis.
    assert result == "c" * 100 + "..."
    assert len(result) == 103
    assert result.endswith("...")


def test_trim_reason_long_truncated_keeps_first_100():
    reason = "x" * 250
    result = trim_reason(reason)
    assert result[:100] == reason[:100]
    assert result == reason[:100] + "..."
    assert len(result) == 103


def test_trim_reason_ellipsis_is_three_ascii_dots():
    # Guard against a fancy single-character ellipsis sneaking in.
    result = trim_reason("y" * 120)
    assert result[-3:] == "..."
    assert chr(0x2026) not in result  # no U+2026 HORIZONTAL ELLIPSIS


# ---------------------------------------------------------------------------
# _MentionFallback
# ---------------------------------------------------------------------------


def test_mention_fallback_mention_format():
    fallback = _MentionFallback(123456789)
    assert fallback.mention == "<@123456789>"


def test_mention_fallback_stores_id():
    fallback = _MentionFallback(42)
    assert fallback.id == 42


def test_mention_fallback_has_no_display_avatar():
    fallback = _MentionFallback(1)
    assert not hasattr(fallback, "display_avatar")


def test_mention_fallback_is_slotted_no_dict():
    # __slots__ = ("id",) means there is no __dict__ and no arbitrary attrs.
    fallback = _MentionFallback(7)
    assert not hasattr(fallback, "__dict__")
    try:
        fallback.display_avatar = "nope"
    except AttributeError:
        pass
    else:  # pragma: no cover - only reached if slots were removed
        raise AssertionError("_MentionFallback should not accept new attributes")


def test_mention_fallback_mention_reflects_id():
    fallback = _MentionFallback(999)
    assert fallback.mention == f"<@{fallback.id}>"


# ---------------------------------------------------------------------------
# Regression guard: no View subclass may shadow View._refresh
# ---------------------------------------------------------------------------


def test_moderation_views_do_not_shadow_refresh():
    """Reproduce the prod-crash guard for this module's View subclasses.

    A subclass method named ``_refresh`` overrides discord.py's internal
    ``View._refresh(self, components)`` and crashes on MESSAGE_UPDATE. Ensure the
    moderation Views inherit the base implementation untouched.
    """
    base_refresh = discord.ui.View._refresh
    for view_cls in (ConfirmView, WarningsView):
        assert view_cls._refresh is base_refresh, (
            f"{view_cls.__name__} shadows View._refresh; rename it "
            "(the prod fix renamed such a method to _rerender)."
        )
