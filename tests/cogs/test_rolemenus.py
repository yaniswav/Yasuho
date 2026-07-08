"""Unit tests for the role-menu emoji validator."""

from cogs.config.rolemenus import valid_emoji


def test_unicode_emoji_accepted():
    assert valid_emoji("🔵") is True
    assert valid_emoji("🎯") is True


def test_custom_emoji_token_accepted():
    assert valid_emoji("<:smile:123456789>") is True
    assert valid_emoji("<a:party:987654321>") is True


def test_plain_text_rejected():
    assert valid_emoji("garbage-not-emoji") is False
    assert valid_emoji("blue") is False
    assert valid_emoji("") is False
    assert valid_emoji(None) is False


def test_ascii_mixed_with_emoji_rejected():
    # "letter + emoji" would 400 on send, so it must not pass the gate.
    assert valid_emoji("x🔵") is False
    assert valid_emoji("blue🔵") is False


def test_long_string_rejected():
    assert valid_emoji("🔵🔵🔵🔵🔵🔵🔵🔵🔵") is False  # too long to be one emoji
