"""Unit tests for tools.message_ref.parse_message_ref (pure function)."""

from tools.message_ref import parse_message_ref


def test_jump_link_yields_all_three_ids():
    link = "https://discord.com/channels/111/222/333"
    assert parse_message_ref(link, default_channel_id=999) == (111, 222, 333)


def test_canary_and_discordapp_hosts_parse():
    assert parse_message_ref(
        "https://canary.discord.com/channels/1/2/3", 0
    ) == (1, 2, 3)
    assert parse_message_ref(
        "https://discordapp.com/channels/4/5/6", 0
    ) == (4, 5, 6)


def test_bare_id_uses_default_channel():
    assert parse_message_ref("777", default_channel_id=42) == (None, 42, 777)


def test_whitespace_is_stripped():
    assert parse_message_ref("  777  ", default_channel_id=42) == (None, 42, 777)


def test_garbage_and_empty_return_none():
    assert parse_message_ref("not-an-id", 42) is None
    assert parse_message_ref("", 42) is None
    assert parse_message_ref(None, 42) is None
