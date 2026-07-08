"""Unit tests for tools.custom_commands.validate_name (pure)."""

from tools import custom_commands as cc


def _err(name, reserved=(), existing=()):
    return cc.validate_name(name, reserved=set(reserved), existing=set(existing))


def test_valid_names_pass():
    assert _err("rules") is None
    assert _err("server-info") is None
    assert _err("faq_2") is None


def test_empty_rejected():
    assert _err("") == "empty"


def test_too_long_rejected():
    assert _err("a" * (cc.MAX_NAME_LENGTH + 1)) == "too_long"


def test_bad_chars_rejected():
    assert _err("has space") == "bad_chars"
    assert _err("UPPER") == "bad_chars"  # caller normalizes first, but guard anyway
    assert _err("-leading") == "bad_chars"
    assert _err("emoji_x") is None
    assert _err("bang!") == "bad_chars"


def test_reserved_rejected():
    assert _err("ban", reserved={"ban", "kick"}) == "reserved"


def test_existing_rejected():
    assert _err("rules", existing={"rules"}) == "exists"


def test_normalize_name():
    assert cc.normalize_name("  Rules  ") == "rules"
    assert cc.normalize_name(None) == ""
