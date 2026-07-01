"""Unit tests for tools/embed_creator.py.

Everything under test here is pure: default_embed / merge_embed / parse_colour /
is_url / render / embed_has_content / summarise / hint_line / placeholder_guide.
render() returns a real discord.Embed built entirely in memory, so no live
Discord, network, database, or Lavalink is ever touched.

The i18n locale is pinned to the default ('en', NullTranslations) by the autouse
reset_locale fixture in conftest, so _() returns each message id verbatim and the
English source strings below are the exact rendered output.

Typography rule: ASCII '-' and '...' only. The zero-width space that render()
substitutes for empty field parts is referenced via embed_creator._ZERO_WIDTH so
no non-ASCII byte appears in this file.
"""

import inspect

import discord

from tools import embed_creator
from tools.embed_creator import (
    COLOUR_NAMES,
    default_embed,
    embed_has_content,
    hint_line,
    is_url,
    merge_embed,
    parse_colour,
    placeholder_guide,
    render,
    summarise,
)

ZW = embed_creator._ZERO_WIDTH


# ----------------------------------------------------------------------
# default_embed
# ----------------------------------------------------------------------
def test_default_embed_shape():
    cfg = default_embed()
    assert cfg == {
        "title": "",
        "description": "",
        "color": None,
        "author": {"name": "", "icon": ""},
        "footer": {"text": "", "icon": ""},
        "thumbnail": "",
        "image": "",
        "fields": [],
    }


def test_default_embed_has_no_shared_nested_refs():
    a = default_embed()
    b = default_embed()
    # Distinct top-level dicts and distinct nested containers.
    assert a is not b
    assert a["author"] is not b["author"]
    assert a["footer"] is not b["footer"]
    assert a["fields"] is not b["fields"]
    # Mutating one must never bleed into a freshly minted sibling.
    a["author"]["name"] = "x"
    a["footer"]["text"] = "y"
    a["fields"].append({"name": "n", "value": "v"})
    assert b["author"] == {"name": "", "icon": ""}
    assert b["footer"] == {"text": "", "icon": ""}
    assert b["fields"] == []


# ----------------------------------------------------------------------
# merge_embed
# ----------------------------------------------------------------------
def test_merge_embed_fills_defaults():
    result = merge_embed({"title": "T"})
    assert result["title"] == "T"
    assert result["description"] == ""
    assert result["color"] is None
    assert result["author"] == {"name": "", "icon": ""}
    assert result["footer"] == {"text": "", "icon": ""}
    assert result["thumbnail"] == ""
    assert result["image"] == ""
    assert result["fields"] == []


def test_merge_embed_copies_known_top_level_keys():
    blob = {
        "title": "Hi",
        "description": "Body",
        "color": 0x123456,
        "thumbnail": "https://x/t.png",
        "image": "https://x/i.png",
    }
    result = merge_embed(blob)
    for key, expected in blob.items():
        assert result[key] == expected


def test_merge_embed_rebuilds_nested_containers():
    blob = {
        "author": {"name": "A", "icon": "ai"},
        "footer": {"text": "F", "icon": "fi"},
        "fields": [{"name": "n", "value": "v", "inline": True}],
    }
    result = merge_embed(blob)
    # Values preserved...
    assert result["author"] == {"name": "A", "icon": "ai"}
    assert result["footer"] == {"text": "F", "icon": "fi"}
    assert result["fields"] == [{"name": "n", "value": "v", "inline": True}]
    # ...but every container is a fresh object, never an alias of the input.
    assert result["author"] is not blob["author"]
    assert result["footer"] is not blob["footer"]
    assert result["fields"] is not blob["fields"]
    assert result["fields"][0] is not blob["fields"][0]
    # Mutating the merged result leaves the source blob untouched.
    result["author"]["name"] = "X"
    result["fields"][0]["value"] = "Z"
    assert blob["author"]["name"] == "A"
    assert blob["fields"][0]["value"] == "v"


def test_merge_embed_none_nested_values_become_defaults():
    result = merge_embed({"author": None, "footer": None, "fields": None})
    assert result["author"] == {"name": "", "icon": ""}
    assert result["footer"] == {"text": "", "icon": ""}
    assert result["fields"] == []


def test_merge_embed_field_defaults_and_inline_coercion():
    blob = {"fields": [{"name": "n"}, {"value": "v", "inline": 1}]}
    result = merge_embed(blob)
    assert result["fields"] == [
        {"name": "n", "value": "", "inline": False},
        {"name": "", "value": "v", "inline": True},
    ]


def test_merge_embed_skips_non_dict_fields():
    blob = {"fields": [{"name": "keep", "value": "v"}, "nope", 42, None]}
    result = merge_embed(blob)
    assert result["fields"] == [{"name": "keep", "value": "v", "inline": False}]


def test_merge_embed_non_dict_input_returns_default():
    baseline = default_embed()
    for bad in (None, "string", 123, [1, 2, 3], (1,), 3.5, True):
        assert merge_embed(bad) == baseline


# ----------------------------------------------------------------------
# parse_colour
# ----------------------------------------------------------------------
def test_parse_colour_hash_hex():
    assert parse_colour("#5865F2") == 0x5865F2


def test_parse_colour_bare_hex():
    assert parse_colour("5865f2") == 0x5865F2
    assert parse_colour("000000") == 0x000000
    assert parse_colour("ffffff") == 0xFFFFFF


def test_parse_colour_palette_name():
    assert parse_colour("blurple") == 0x5865F2
    assert parse_colour("gold") == COLOUR_NAMES["gold"]
    # Case- and whitespace-insensitive.
    assert parse_colour("  #5865F2  ") == 0x5865F2
    assert parse_colour("BLURPLE") == 0x5865F2


def test_parse_colour_random_is_in_range():
    value = parse_colour("random")
    assert isinstance(value, int)
    assert 0 <= value <= 0xFFFFFF


def test_parse_colour_invalid_returns_none():
    assert parse_colour("notacolour") is None
    assert parse_colour("") is None
    assert parse_colour(None) is None
    # Numerically valid hex but above 0xFFFFFF -> rejected.
    assert parse_colour("1000000") is None
    assert parse_colour("FFFFFFF") is None


def test_parse_colour_custom_palette():
    palette = {"brand": 0x112233}
    assert parse_colour("brand", palette) == 0x112233
    # A default name is NOT honoured when a custom palette is supplied, and it
    # is not valid hex either, so it falls through to None.
    assert parse_colour("blurple", palette) is None
    # Hex parsing still works against a custom palette.
    assert parse_colour("#abcdef", palette) == 0xABCDEF


# ----------------------------------------------------------------------
# is_url
# ----------------------------------------------------------------------
def test_is_url():
    assert is_url("http://example.com") is True
    assert is_url("https://example.com/a.png") is True
    assert is_url("ftp://example.com") is False
    assert is_url("example.com") is False
    assert is_url("") is False
    assert is_url(None) is False
    assert is_url(123) is False
    assert is_url(["https://x"]) is False


# ----------------------------------------------------------------------
# render
# ----------------------------------------------------------------------
def test_render_full_config_all_parts():
    config = {
        "title": "Hello",
        "description": "Desc",
        "color": 0x5865F2,
        "author": {"name": "Auth", "icon": "https://cdn/a.png"},
        "footer": {"text": "Foot", "icon": "https://cdn/f.png"},
        "thumbnail": "https://cdn/t.png",
        "image": "https://cdn/i.png",
        "fields": [{"name": "F1", "value": "V1", "inline": True}],
    }
    e = render(config)
    assert e.title == "Hello"
    assert e.description == "Desc"
    assert e.colour.value == 0x5865F2
    assert e.author.name == "Auth"
    assert e.author.icon_url == "https://cdn/a.png"
    assert e.footer.text == "Foot"
    assert e.footer.icon_url == "https://cdn/f.png"
    assert e.thumbnail.url == "https://cdn/t.png"
    assert e.image.url == "https://cdn/i.png"
    assert len(e.fields) == 1
    assert e.fields[0].name == "F1"
    assert e.fields[0].value == "V1"
    assert e.fields[0].inline is True


def test_render_caps_each_part():
    config = {
        "title": "T" * 300,
        "description": "D" * 5000,
        "author": {"name": "A" * 300, "icon": ""},
        "footer": {"text": "F" * 3000, "icon": ""},
        "fields": [{"name": "N" * 300, "value": "V" * 2000, "inline": False}],
    }
    e = render(config)
    assert len(e.title) == embed_creator.LIMIT_TITLE == 256
    assert len(e.description) == embed_creator.LIMIT_DESC == 4096
    assert len(e.author.name) == embed_creator.LIMIT_AUTHOR == 256
    assert len(e.footer.text) == embed_creator.LIMIT_FOOTER == 2048
    assert len(e.fields[0].name) == embed_creator.LIMIT_FIELD_NAME == 256
    assert len(e.fields[0].value) == embed_creator.LIMIT_FIELD_VALUE == 1024


def test_render_substitute_applied_to_text_and_asset_urls():
    def sub(text):
        return text.replace("{user}", "Alice").replace(
            "{avatar}", "https://cdn/av.png"
        )

    config = {
        "title": "Hi {user}",
        "description": "by {user}",
        "thumbnail": "{avatar}",
        "image": "{user}",  # resolves to "Alice" -> not a url -> dropped
        "author": {"name": "{user}", "icon": "{avatar}"},
        "footer": {"text": "{user}", "icon": "still-not-a-url"},
    }
    e = render(config, substitute=sub)
    assert e.title == "Hi Alice"
    assert e.description == "by Alice"
    # Asset URL went through substitute, then is_url validated it.
    assert e.thumbnail.url == "https://cdn/av.png"
    assert e.image.url is None
    assert e.author.name == "Alice"
    assert e.author.icon_url == "https://cdn/av.png"
    assert e.footer.text == "Alice"
    assert e.footer.icon_url is None


def test_render_non_url_assets_are_dropped_without_substitute():
    config = {
        "thumbnail": "not-a-url",
        "image": "also/not/a/url",
        "author": {"name": "A", "icon": "nope"},
        "footer": {"text": "F", "icon": "nope"},
    }
    e = render(config)
    assert e.thumbnail.url is None
    assert e.image.url is None
    # Name/text still render; only the invalid icon URLs drop out.
    assert e.author.name == "A"
    assert e.author.icon_url is None
    assert e.footer.text == "F"
    assert e.footer.icon_url is None


def test_render_all_empty_returns_bare_colourless_embed():
    for empty in ({}, None, default_embed()):
        e = render(empty)
        assert isinstance(e, discord.Embed)
        assert e.title is None
        assert e.description is None
        assert e.fields == []
        assert e.colour is None
        assert embed_has_content(e) is False


def test_render_all_empty_but_coloured():
    e = render({"color": 0x00FF00})
    # No visible content, yet the colour is honoured (bare coloured embed).
    assert embed_has_content(e) is False
    assert e.colour.value == 0x00FF00


def test_render_non_int_colour_ignored():
    for bad in ("red", "#5865F2", 3.5, [1], None):
        e = render({"color": bad})
        assert e.colour is None


def test_render_fields_capped_at_25():
    config = {
        "fields": [
            {"name": "n%d" % i, "value": "v%d" % i} for i in range(30)
        ]
    }
    e = render(config)
    assert len(e.fields) == embed_creator.LIMIT_FIELDS == 25
    # It keeps the first 25 in order.
    assert e.fields[0].name == "n0"
    assert e.fields[-1].name == "n24"


def test_render_empty_field_parts_become_zero_width():
    e = render({"fields": [{"name": "", "value": ""}]})
    assert e.fields[0].name == ZW
    assert e.fields[0].value == ZW


def test_render_skips_non_dict_fields():
    e = render({"fields": [{"name": "ok", "value": "v"}, "bad", 42, None]})
    assert len(e.fields) == 1
    assert e.fields[0].name == "ok"


# ----------------------------------------------------------------------
# embed_has_content
# ----------------------------------------------------------------------
def test_embed_has_content_true_for_each_part():
    cases = [
        {"title": "x"},
        {"description": "x"},
        {"fields": [{"name": "a", "value": "b"}]},
        {"image": "https://cdn/i.png"},
        {"thumbnail": "https://cdn/t.png"},
        {"author": {"name": "a", "icon": ""}},
        {"footer": {"text": "f", "icon": ""}},
    ]
    for config in cases:
        assert embed_has_content(render(config)) is True, config


def test_embed_has_content_false_for_empty():
    assert embed_has_content(render({})) is False
    # A colour-only embed still counts as empty (colour is not content).
    assert embed_has_content(render({"color": 0x123456})) is False


# ----------------------------------------------------------------------
# summarise
# ----------------------------------------------------------------------
def test_summarise_always_present_lines():
    s = summarise({"fields": [{}, {}]})
    lines = s.split("\n")
    assert "**Title:** *none*" in lines
    assert "**Description:** *none*" in lines
    assert "**Colour:** default" in lines
    assert "**Fields:** 2" in lines


def test_summarise_custom_empty_token():
    s = summarise({}, empty="EMPTY")
    assert "**Title:** EMPTY" in s
    assert "**Description:** EMPTY" in s


def test_summarise_colour_text_hex():
    s = summarise({"color": 0x5865F2})
    assert "**Colour:** #5865F2" in s
    # Zero-padded, uppercase, exactly six digits.
    s0 = summarise({"color": 0x0000FF})
    assert "**Colour:** #0000FF" in s0


def test_summarise_description_truncation():
    long_desc = "d" * 200
    s = summarise({"description": long_desc})
    assert ("d" * 117 + "...") in s
    # The full 200-char string must not survive verbatim.
    assert long_desc not in s


def test_summarise_title_truncated_to_120():
    s = summarise({"title": "t" * 200})
    assert ("t" * 120) in s
    assert ("t" * 121) not in s


def test_summarise_optional_lines_present_when_set():
    config = {
        "author": {"name": "Auth"},
        "footer": {"text": "Foot"},
        "thumbnail": "https://cdn/t.png",
        "image": "https://cdn/i.png",
    }
    s = summarise(config)
    assert "**Author:** Auth" in s
    assert "**Footer:** Foot" in s
    assert "**Thumbnail:** set" in s
    assert "**Image:** set" in s


def test_summarise_optional_lines_absent_when_unset():
    s = summarise({})
    assert "Author:" not in s
    assert "Footer:" not in s
    assert "Thumbnail:" not in s
    assert "Image:" not in s


# ----------------------------------------------------------------------
# hint_line
# ----------------------------------------------------------------------
def test_hint_line_joins_names_only():
    entries = [("{user}", "the invoking user"), ("{server}", "the guild name")]
    assert hint_line(entries) == "{user} {server}"


def test_hint_line_empty():
    assert hint_line([]) == ""


# ----------------------------------------------------------------------
# placeholder_guide
# ----------------------------------------------------------------------
def test_placeholder_guide_defaults():
    e = placeholder_guide([("{user}", "the user")])
    assert e.title == "Placeholders"
    # No colour supplied -> random_colour() -> a concrete int colour is set.
    assert e.colour is not None
    assert 0 <= e.colour.value <= 0xFFFFFF
    assert e.fields[0].name == "Tokens"
    assert e.fields[0].value == "`{user}` - the user"


def test_placeholder_guide_explicit_colour_and_title_and_intro():
    e = placeholder_guide(
        [("{a}", "b")], title="Custom", intro="Read me", colour=0x123456
    )
    assert e.title == "Custom"
    assert e.description == "Read me"
    assert e.colour.value == 0x123456


def test_placeholder_guide_no_entries_has_no_fields():
    e = placeholder_guide([])
    assert e.fields == []


def test_placeholder_guide_chunks_stay_within_1024():
    # Many entries whose combined length forces multiple fields.
    entries = [("tok%02d" % i, "x" * 80) for i in range(50)]
    e = placeholder_guide(entries)
    assert len(e.fields) >= 2
    for field in e.fields:
        assert len(field.value) <= embed_creator.LIMIT_FIELD_VALUE == 1024
    # First field is labelled; the continuation fields carry a zero-width name.
    assert e.fields[0].name == "Tokens"
    for field in e.fields[1:]:
        assert field.name == ZW


def test_placeholder_guide_single_long_line_truncated():
    e = placeholder_guide([("tok", "y" * 4000)])
    assert len(e.fields) == 1
    assert len(e.fields[0].value) == embed_creator.LIMIT_FIELD_VALUE


# ----------------------------------------------------------------------
# Regression guard: no embed_creator UI subclass may shadow the discord.py
# View internal _refresh (a real prod crash was caused by exactly this
# collision on MESSAGE_UPDATE). Scoped to classes defined in this module.
# ----------------------------------------------------------------------
def test_no_ui_subclass_shadows_view_internal_refresh():
    ui_bases = (discord.ui.View, discord.ui.Modal, discord.ui.Item)
    offenders = []
    for name, obj in vars(embed_creator).items():
        if inspect.isclass(obj) and issubclass(obj, ui_bases):
            if "_refresh" in obj.__dict__:
                offenders.append(name)
    assert offenders == [], (
        "UI subclass(es) define _refresh, shadowing discord.py's "
        "View._refresh(self, components): %r" % offenders
    )
