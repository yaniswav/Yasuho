"""Unit tests for the custom-commands cooldown parser."""

from cogs.config.customcommands import parse_cooldown


def test_parse_cooldown_valid():
    assert parse_cooldown("30") == 30
    assert parse_cooldown("  15  ") == 15


def test_parse_cooldown_empty_and_junk_is_zero():
    assert parse_cooldown("") == 0
    assert parse_cooldown(None) == 0
    assert parse_cooldown("abc") == 0


def test_parse_cooldown_is_clamped():
    assert parse_cooldown("-5") == 0
    assert parse_cooldown("999999") == 3600
