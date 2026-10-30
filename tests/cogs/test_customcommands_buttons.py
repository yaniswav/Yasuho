"""V5c-A: Yasuho+ link buttons under a custom command's reply."""

import types

import pytest

from cogs.config import command_naming as cc
from cogs.config.customcommands import CustomCommands, _with_buttons

GOOD = {"label": "Rules", "url": "https://example.com/rules"}


def _payload(response, max_buttons):
    member = types.SimpleNamespace(id=42, mention="<@42>")
    guild = types.SimpleNamespace(id=1, name="G", member_count=3)
    return CustomCommands._payload(response, member, guild, max_buttons)


def test_parse_accepts_up_to_three_https_lines():
    raw = "A | https://a.example\n\nB | https://b.example/x?y=1\nC | https://c.example"
    buttons, err = cc.parse_buttons(raw)
    assert err is None and [b["label"] for b in buttons] == ["A", "B", "C"]
    assert cc.parse_buttons("") == ([], None)


@pytest.mark.parametrize(
    "raw,key",
    [
        ("A | https://a.example\nB | https://b.example\nC | https://c.example\nD | https://d.example", "too_many"),
        ("no separator here", "format"),
        (" | https://a.example", "label"),
        ("x" * 81 + " | https://a.example", "label"),
        ("A | http://a.example", "url"),
        ("A | javascript:alert(1)", "url"),
        ("A | https://", "url"),
        ("A | https://a.example/" + "p" * 600, "url"),
    ],
)
def test_parse_refusals(raw, key):
    assert cc.parse_buttons(raw) == (None, key)


def test_read_side_drops_junk_the_dashboard_may_have_written():
    blob = [
        GOOD,
        {"label": "Evil", "url": "http://x.example"},
        {"label": "Js", "url": "javascript:alert(1)"},
        {"label": "", "url": "https://x.example"},
        {"label": "x" * 81, "url": "https://x.example"},
        "garbage",
        {"label": 5, "url": "https://x.example"},
    ]
    assert cc.read_buttons(blob, 3) == [GOOD]
    assert cc.read_buttons("nope", 3) == []
    many = [{"label": str(i), "url": "https://x.example"} for i in range(9)]
    assert len(cc.read_buttons(many, 9)) == cc.MAX_BUTTONS
    assert len(cc.read_buttons(many, 2)) == 2


def test_entitled_text_reply_carries_the_view_and_keeps_mention_scope():
    out = _payload({"type": "text", "content": "hi {user}", "buttons": [GOOD]}, 3)
    assert [i.url for i in out["view"].children] == [GOOD["url"]]
    assert out["allowed_mentions"].everyone is False and out["allowed_mentions"].roles is False


def test_entitled_embed_reply_carries_the_view():
    out = _payload({"type": "embed", "embed": {"title": "T"}, "buttons": [GOOD]}, 3)
    assert "embed" in out and len(out["view"].children) == 1


def test_lapsed_guild_still_answers_with_text_but_no_buttons():
    """Lapse: max_buttons 0 -> the text part works, the buttons are not rendered."""
    response = {"type": "text", "content": "hi", "buttons": [GOOD]}
    out = _payload(response, 0)
    assert out["content"] == "hi" and "view" not in out
    assert response["buttons"] == [GOOD]  # nothing deleted


def test_junk_stored_buttons_never_reach_the_view():
    out = _payload({"type": "text", "content": "hi", "buttons": [{"label": "x", "url": "http://x"}]}, 3)
    assert "view" not in out


def test_embed_over_discord_total_renders_nothing():
    big = {"type": "embed", "embed": {"description": "d" * 4000, "fields": [
        {"name": "n", "value": "v" * 1000} for _ in range(3)]}}
    assert _payload(big, 0) is None


def test_edit_keeps_buttons():
    assert _with_buttons({"type": "text"}, [GOOD])["buttons"] == [GOOD]
    assert "buttons" not in _with_buttons({"type": "text"}, None)


@pytest.mark.parametrize("url", ["https://[::1", "https://a]b", "https://[x", "https://[::1]x:y"])
def test_malformed_urls_read_as_invalid_instead_of_raising(url):
    assert cc.clean_button("L", url) is None
    assert cc.read_buttons([{"label": "L", "url": url}, GOOD], 3) == [GOOD]
    assert cc.parse_buttons(f"L | {url}") == (None, "url")
    out = _payload({"type": "text", "content": "hi", "buttons": [{"label": "L", "url": url}]}, 3)
    assert "view" not in out
