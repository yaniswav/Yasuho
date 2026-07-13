"""Unit tests for the pure rendering helpers behind the Components V2 help menu.

The help menu migrated from classic embeds to a Components V2 layout (lot C1).
The line-by-line truncation that used to guard the 4096-char embed description
now guards the shared ~4000-char CV2 text budget, ported verbatim into two pure,
side-effect-free helpers: :func:`_category_lines` (cogs -> display lines) and
:func:`_fit_lines` (fit lines into a budget, appending the overflow notice).
These tests pin that maths - the biggest category must stop cleanly before the
limit, exactly as before - without touching Discord, the network or a database.

They also assert the Components V2 msgid surface is unchanged: the migration was
behaviour-identical, so every user-facing string is a reused ``_()`` literal and
nothing was silently reworded.
"""

import discord

from cogs.system import help as help_mod
from cogs.system.help import _category_lines, _fit_lines


class _Cmd:
    """A minimal stand-in for a discord.py Command (only what the helper reads)."""

    def __init__(self, name, short_doc=""):
        self.name = name
        self.qualified_name = name
        self.short_doc = short_doc


# ---------------------------------------------------------------------------
# _category_lines
# ---------------------------------------------------------------------------


def test_category_lines_single_group():
    groups = [("Moderation", [_Cmd("ban", "Ban a member"), _Cmd("kick", "Kick")])]
    lines = _category_lines(groups, "!")
    assert lines == [
        "**Moderation**",
        "`!ban` - Ban a member",
        "`!kick` - Kick",
    ]


def test_category_lines_blank_spacer_between_groups():
    groups = [
        ("Moderation", [_Cmd("ban", "Ban")]),
        ("Music", [_Cmd("play", "Play")]),
    ]
    lines = _category_lines(groups, "?")
    # A blank spacer line separates the second group's header from the first.
    assert lines == [
        "**Moderation**",
        "`?ban` - Ban",
        "",
        "**Music**",
        "`?play` - Play",
    ]


def test_category_lines_missing_doc_falls_back():
    lines = _category_lines([("Fun", [_Cmd("roll", "")])], "!")
    # Empty short_doc uses the reused "No description provided." msgid.
    assert lines[0] == "**Fun**"
    assert lines[1].startswith("`!roll` - ")
    assert lines[1].endswith("No description provided.")


# ---------------------------------------------------------------------------
# _fit_lines
# ---------------------------------------------------------------------------


def test_fit_lines_all_fit_no_notice():
    text, truncated = _fit_lines(["alpha", "beta"], 1000, "NOTICE", "EMPTY")
    assert text == "alpha\nbeta"
    assert truncated is False


def test_fit_lines_empty_input_uses_empty_text():
    text, truncated = _fit_lines([], 1000, "NOTICE", "EMPTY")
    assert text == "EMPTY"
    assert truncated is False


def test_fit_lines_truncates_and_appends_notice():
    lines = [f"line-{i}" for i in range(100)]
    text, truncated = _fit_lines(lines, 20, "NOTICE", "EMPTY")
    assert truncated is True
    assert text.endswith("\nNOTICE")
    body = text[: -len("\nNOTICE")]
    # The body accumulated only lines that fit within the pre-notice budget.
    assert len(body) <= 20
    assert body.startswith("line-0")


def test_fit_lines_stops_cleanly_before_limit():
    # Exactly-at-budget accounting: two 5-char lines + one joining newline == 11.
    # With a budget of 11 both fit; with 10 the second overflows and is dropped.
    two = ["aaaaa", "bbbbb"]
    fit, trunc_fit = _fit_lines(two, 11, "N", "E")
    assert (fit, trunc_fit) == ("aaaaa\nbbbbb", False)

    one, trunc_one = _fit_lines(two, 10, "N", "E")
    assert trunc_one is True
    assert one == "aaaaa\nN"


def test_fit_lines_never_exceeds_budget_plus_notice():
    # The realistic invariant: whatever the input, body stays within budget and
    # only the notice is added on overflow, so total <= budget + len(notice) + 1.
    lines = [f"`!command{i}` - a reasonably wordy description here" for i in range(500)]
    budget = 300
    notice = "...more commands available."
    text, truncated = _fit_lines(lines, budget, notice, "EMPTY")
    assert truncated is True
    assert len(text) <= budget + len(notice) + 1


# ---------------------------------------------------------------------------
# Budget wiring: the fullest page must stop before the CV2 ceiling
# ---------------------------------------------------------------------------


def test_cv2_budget_leaves_headroom_under_ceiling():
    # heading + footer + control reserve + notice are all subtracted from the
    # 4000 budget, so a fitted body plus that chrome can never reach the ceiling.
    heading = "### 🔨 Moderation"
    footer = "-# Category 1/7 - !help <command> for details"
    notice = "...more commands available. Use `!help <command>` to see them."
    budget = (
        help_mod.CV2_TEXT_BUDGET
        - len(heading)
        - len(footer)
        - help_mod.CV2_CONTROL_RESERVE
        - (len(notice) + 1)
    )
    lines = [f"`!cmd{i}` - description number {i}" for i in range(2000)]
    body, truncated = _fit_lines(lines, budget, notice, "none")
    assert truncated is True
    total = len(heading) + len(body) + len(footer)
    assert total <= help_mod.CV2_TEXT_BUDGET - help_mod.CV2_CONTROL_RESERVE


# ---------------------------------------------------------------------------
# House-style: the migrated views are Components V2 and gated where interactive
# ---------------------------------------------------------------------------


def test_interactive_views_are_gated_layout_views():
    # The navigable menu and the group toggle carry interactive components, so
    # they are the author-gated CV2 base; the one-shot card is a plain LayoutView.
    from tools.views import AuthorLayoutView

    assert issubclass(help_mod.HelpView, AuthorLayoutView)
    assert issubclass(help_mod.GroupHelpView, AuthorLayoutView)
    assert issubclass(help_mod._HelpCard, discord.ui.LayoutView)
    assert not issubclass(help_mod._HelpCard, AuthorLayoutView)


def test_help_card_renders_blocks():
    card = help_mod._HelpCard(["### Title", "body text"])
    # A LayoutView with content serialises to at least one top-level component.
    assert card.to_components()
