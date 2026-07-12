"""Unit tests for tools.leveling_gate (pure command-prefix filter).

is_command_invocation is the cheap, synchronous check that keeps prefix-command
messages (and their near-misses) from earning XP on the leveling hot path. These
tests pin the truth table down: real commands and typo'd near-misses are skipped,
organic chat is not, bot mentions count as command prefixes, and empty prefixes
can never swallow every message.
"""

from tools.leveling_gate import is_command_invocation


def test_plain_prefix_command_is_an_invocation():
    assert is_command_invocation("?help", ("?",)) is True


def test_organic_chat_is_not_an_invocation():
    assert is_command_invocation("hello there", ("?",)) is False


def test_near_miss_is_skipped_too():
    """A message that only LOOKS like a command is skipped on purpose."""
    assert is_command_invocation("?lol", ("?",)) is True


def test_multi_char_prefix():
    assert is_command_invocation("y! rank", ("y!",)) is True
    assert is_command_invocation("yikes", ("y!",)) is False


def test_second_prefix_in_the_list_matches():
    assert is_command_invocation("!ping", ("?", "!")) is True


def test_bot_mention_counts_as_a_prefix():
    prefixes = ("y!", "<@123>", "<@!123>")
    assert is_command_invocation("<@123> rank", prefixes) is True
    assert is_command_invocation("<@!123> rank", prefixes) is True


def test_empty_content_is_not_an_invocation():
    assert is_command_invocation("", ("?",)) is False


def test_blank_prefix_is_ignored():
    """An empty prefix must never make every message look like a command."""
    assert is_command_invocation("just chatting", ("",)) is False
    assert is_command_invocation("just chatting", ("", "?")) is False


def test_no_prefixes_never_matches():
    assert is_command_invocation("?help", ()) is False
