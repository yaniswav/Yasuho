"""Unit tests for the announcement builder's pure substitution helper."""

from cogs.config.announcements import build_substitution


class _Guild:
    def __init__(self, name, member_count):
        self.name = name
        self.member_count = member_count


class _Channel:
    def __init__(self, mention):
        self.mention = mention


def test_substitution_replaces_all_tokens():
    sub = build_substitution(_Guild("Cool Server", 1234), _Channel("<#42>"))
    assert (
        sub("Welcome to {server} ({members} members) in {channel}")
        == "Welcome to Cool Server (1,234 members) in <#42>"
    )


def test_substitution_handles_missing_channel_and_zero_members():
    sub = build_substitution(_Guild("S", 0), None)
    assert sub("{server} {members} {channel}") == "S 0 "


def test_substitution_leaves_unknown_tokens_untouched():
    sub = build_substitution(_Guild("S", 5), None)
    assert sub("{unknown} stays") == "{unknown} stays"
