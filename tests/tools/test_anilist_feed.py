"""Unit tests for tools/anilist_feed.py (pure feed helpers).

The feed core is pure: no Discord, database or network. These tests pin the
markdown conversion (spoilers above all - leaking one is the worst failure),
routing/filtering, burst coalescing and progress normalisation, focusing on the
edges that bite in production.
"""

from tools import anilist_feed as af

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_policy_constants():
    assert af.MAX_FEEDS_PER_GUILD == 2
    assert af.MAX_FOLLOWS_PER_FEED == 25
    assert af.MAX_FULL_POSTS_PER_TICK == 5
    assert af.TEXT_LIMIT == 2048


def test_message_type_excluded():
    # MESSAGE is private and must never be a postable feed type.
    assert "MESSAGE" not in af.ALLOWED_TYPES
    assert af.ALLOWED_TYPES == ("ANIME_LIST", "MANGA_LIST", "TEXT")
    assert af.DEFAULT_TYPES == af.ALLOWED_TYPES


# ---------------------------------------------------------------------------
# convert_text - spoilers (the critical conversion)
# ---------------------------------------------------------------------------


def test_spoiler_single():
    text, _ = af.convert_text("a ~!secret!~ b")
    assert text == "a ||secret|| b"


def test_spoiler_multiple_stay_separate():
    # non-greedy: two spoilers on a line must not merge into one giant bar.
    text, _ = af.convert_text("~!one!~ and ~!two!~")
    assert text == "||one|| and ||two||"


def test_spoiler_multiline():
    text, _ = af.convert_text("~!line1\nline2!~")
    assert text == "||line1\nline2||"


def _unescaped_bar_count(text):
    # Count '||' markers Discord would actually pair: two pipes NOT preceded by a
    # neutralising backslash. Escaped user pipes ('\|\|') are excluded.
    import re

    return len(re.findall(r"(?<!\\)\|\|", text))


def test_user_pipes_do_not_shift_spoiler_pairing():
    # Regression: an odd number of user-typed '||' before an emitted spoiler used
    # to shift Discord's positional '||' pairing and render the spoiler in the
    # clear. User pipes are now escaped, so exactly ONE emitted pair remains and
    # the secret sits between it.
    text, _ = af.convert_text(
        "Thoughts: yes || no. My hot take: ~!secret twist!~"
    )
    assert _unescaped_bar_count(text) == 2  # only the emitted spoiler pair
    assert "||secret twist||" in text  # the secret stays wrapped by that pair
    assert "\\|\\|" in text  # the prose '||' is neutralised, cannot pair


def test_literal_pipes_inside_spoiler_stay_escaped_in_one_pair():
    # A literal '||' inside a spoiler is escaped in place; the spoiler is still a
    # single balanced emitted pair wrapping the (escaped) content.
    text, _ = af.convert_text("~!a || b!~")
    assert text == "||a \\|\\| b||"
    assert _unescaped_bar_count(text) == 2  # exactly one balanced spoiler pair


# ---------------------------------------------------------------------------
# convert_text - bold / center
# ---------------------------------------------------------------------------


def test_bold_underscores_become_double_star():
    text, _ = af.convert_text("__loud__")
    assert text == "**loud**"


def test_single_underscore_italic_left_alone():
    # Discord renders _italic_ as italic too, so single underscores are kept.
    text, _ = af.convert_text("_soft_")
    assert text == "_soft_"


def test_center_markers_stripped_keep_inner():
    text, _ = af.convert_text("~~~centered~~~")
    assert text == "centered"


def test_center_wins_over_spoiler():
    # ~~~!...!~~~ is a centered block, not a stray spoiler; nothing hidden.
    text, _ = af.convert_text("~~~!hi!~~~")
    assert text == "!hi!"
    assert "||" not in text


# ---------------------------------------------------------------------------
# convert_text - images
# ---------------------------------------------------------------------------


def test_image_stripped_and_url_extracted():
    text, image = af.convert_text("look img(https://i.imgur.com/x.png) here")
    assert "img(" not in text
    assert text == "look  here"
    assert image == "https://i.imgur.com/x.png"


def test_image_width_variants_and_case_insensitive():
    for markup in (
        "img(https://a/x.png)",
        "img220(https://a/x.png)",
        "Img420(https://a/x.png)",
        "IMG40%(https://a/x.png)",
    ):
        text, image = af.convert_text(markup)
        assert image == "https://a/x.png"
        assert "img" not in text.lower()


def test_first_http_image_wins():
    text, image = af.convert_text(
        "img(https://a/1.png) then img(https://a/2.png)"
    )
    assert image == "https://a/1.png"
    assert "img(" not in text


def test_non_http_image_not_promoted():
    # A relative/non-http image url is stripped but never surfaced as the embed.
    text, image = af.convert_text("img(/local/x.png)")
    assert image is None
    assert "img(" not in text


def test_image_inside_spoiler_is_not_leaked():
    # An image hidden in a spoiler must not be promoted to the embed image.
    text, image = af.convert_text("~!img(https://a/secret.png)!~")
    assert image is None
    assert "secret.png" not in text


def test_stray_pipes_do_not_unhide_spoiler_image():
    # A literal '||' typed as prose must not flip spoiler parity and promote an
    # image the author hid inside a spoiler.
    text, image = af.convert_text("note || here ~!img(https://x/secret.png)!~")
    assert image is None
    assert "secret.png" not in text


# ---------------------------------------------------------------------------
# convert_text - videos, html, links
# ---------------------------------------------------------------------------


def test_youtube_and_webm_become_bare_url():
    text, _ = af.convert_text("youtube(https://youtu.be/abc)")
    assert text == "https://youtu.be/abc"
    text, _ = af.convert_text("webm(https://a/clip.webm)")
    assert text == "https://a/clip.webm"


def test_html_br_becomes_newline_other_tags_stripped():
    text, _ = af.convert_text("a<br>b<i>c</i>")
    assert text == "a\nbc"


def test_markdown_link_left_intact():
    text, _ = af.convert_text("see [the site](https://example.com)")
    assert text == "see [the site](https://example.com)"


# ---------------------------------------------------------------------------
# convert_text - truncation + the spoiler-safety edge
# ---------------------------------------------------------------------------


def test_no_truncation_when_within_limit():
    text, _ = af.convert_text("short", limit=100)
    assert text == "short"
    assert not text.endswith("...")


def test_truncation_appends_ellipsis():
    raw = "x" * 50
    text, _ = af.convert_text(raw, limit=10)
    assert text == "xxxxxxxxxx..."


def test_truncation_inside_open_spoiler_closes_the_bar():
    # Cut lands inside an open spoiler: the fragment must stay hidden, so the
    # bar is closed before the ellipsis and no plaintext spoiler leaks.
    raw = "~!" + "S" * 50 + "!~"
    text, _ = af.convert_text(raw, limit=8)
    # even parity of '||' -> the spoiler is closed (not left dangling open).
    assert text.count("||") % 2 == 0
    assert text.endswith("||...")
    assert text.startswith("||")


def test_truncation_split_pipe_is_dropped():
    # User-typed '||' is now escaped to '\|\|' before conversion (so it cannot
    # pair with an emitted spoiler bar), so the cut lands inside that escape
    # sequence rather than a raw '||'. What matters is unchanged: no bare '||'
    # survives, the hidden tail never leaks, and nothing dangles as a spoiler.
    raw = "hello ||done||" + "z" * 50
    text, _ = af.convert_text(raw, limit=7)  # cut lands in the escaped '\|\|'
    assert text == "hello \\..."  # escaped form: the lone '\' precedes the cut
    assert "||" not in text  # no spoiler bar, hidden tail stays cut off
    assert "z" not in text


def test_stray_pipes_do_not_leak_truncated_spoiler():
    # A literal '||' typed as prose before a real spoiler that straddles the cut
    # must not flip parity and leave the spoiler open, exposing it in the clear.
    raw = "yes || no ~!" + "S" * 50 + "!~"
    text, _ = af.convert_text(raw, limit=20)
    # The spoiler bar is closed before the ellipsis; the hidden run stays wrapped.
    assert text.endswith("||...")
    # The user '||' is escaped ('\|\|', two chars wider), so the cut fits fewer
    # S's inside the bar, but the emitted spoiler pair still wraps the run.
    assert "\\|\\|" in text  # user prose pipes neutralised, cannot pair with a bar
    assert "||" + "S" * 7 + "||..." in text


# ---------------------------------------------------------------------------
# route_activities
# ---------------------------------------------------------------------------


def _act(id, type, user_id, is_adult=False):
    return {"id": id, "type": type, "user_id": user_id, "is_adult": is_adult}


def test_route_filters_by_follow_and_type():
    activities = [
        _act(1, "ANIME_LIST", 100),
        _act(2, "TEXT", 200),          # user 200 not followed -> dropped
        _act(3, "MANGA_LIST", 100),    # type not in feed types -> dropped
    ]
    feeds = [
        {
            "channel_id": 10,
            "types": {"ANIME_LIST", "TEXT"},
            "followed_ids": {100},
            "allow_adult": False,
        }
    ]
    routed = af.route_activities(activities, feeds)
    assert list(routed.keys()) == [10]
    assert [a["id"] for a in routed[10]] == [1]


def test_route_drops_adult_unless_allowed():
    activities = [_act(1, "TEXT", 100, is_adult=True)]
    base = {"channel_id": 10, "types": {"TEXT"}, "followed_ids": {100}}

    dropped = af.route_activities(activities, [{**base, "allow_adult": False}])
    assert dropped == {}  # empty channel omitted

    kept = af.route_activities(activities, [{**base, "allow_adult": True}])
    assert [a["id"] for a in kept[10]] == [1]


def test_route_sorts_by_id_ascending():
    activities = [
        _act(5, "TEXT", 100),
        _act(2, "TEXT", 100),
        _act(9, "TEXT", 100),
    ]
    feeds = [
        {"channel_id": 10, "types": {"TEXT"}, "followed_ids": {100}, "allow_adult": True}
    ]
    routed = af.route_activities(activities, feeds)
    assert [a["id"] for a in routed[10]] == [2, 5, 9]


def test_route_empty_when_nothing_matches():
    activities = [_act(1, "TEXT", 999)]
    feeds = [
        {"channel_id": 10, "types": {"TEXT"}, "followed_ids": {100}, "allow_adult": True}
    ]
    assert af.route_activities(activities, feeds) == {}


# ---------------------------------------------------------------------------
# plan_posts + group_by_user
# ---------------------------------------------------------------------------


def test_plan_posts_splits_full_and_digest():
    activities = list(range(1, 9))  # 8 items
    full, digest = af.plan_posts(activities, max_full=5)
    assert full == [1, 2, 3, 4, 5]
    assert digest == [6, 7, 8]


def test_plan_posts_all_full_when_under_cap():
    full, digest = af.plan_posts([1, 2], max_full=5)
    assert full == [1, 2]
    assert digest == []


def test_group_by_user_preserves_order():
    activities = [
        _act(1, "TEXT", 100),
        _act(2, "TEXT", 200),
        _act(3, "TEXT", 100),
    ]
    grouped = af.group_by_user(activities)
    assert list(grouped.keys()) == [100, 200]
    assert [a["id"] for a in grouped[100]] == [1, 3]
    assert [a["id"] for a in grouped[200]] == [2]


# ---------------------------------------------------------------------------
# normalize_progress
# ---------------------------------------------------------------------------


def test_normalize_progress_single():
    assert af.normalize_progress("3") == "3"


def test_normalize_progress_range_collapses_spaces():
    assert af.normalize_progress("3 - 5") == "3-5"
    assert af.normalize_progress("12 -  15") == "12-15"


def test_normalize_progress_junk_passthrough():
    assert af.normalize_progress("") == ""
    assert af.normalize_progress(None) == ""
    assert af.normalize_progress("volume 3") == "volume 3"


# ---------------------------------------------------------------------------
# parse_hex_colour - the card accent parser (defensive against AniList junk)
# ---------------------------------------------------------------------------


def test_parse_hex_colour_with_hash():
    assert af.parse_hex_colour("#e4a15d") == 0xE4A15D


def test_parse_hex_colour_without_hash():
    assert af.parse_hex_colour("e4a15d") == 0xE4A15D


def test_parse_hex_colour_is_case_insensitive():
    assert af.parse_hex_colour("#E4A15D") == af.parse_hex_colour("#e4a15d")


def test_parse_hex_colour_trims_whitespace():
    assert af.parse_hex_colour("  #02a9ff  ") == 0x02A9FF


def test_parse_hex_colour_black_and_white_edges():
    # A literal 0x000000 must parse (falsy int, but not None) and 0xFFFFFF too.
    assert af.parse_hex_colour("#000000") == 0x000000
    assert af.parse_hex_colour("#ffffff") == 0xFFFFFF


def test_parse_hex_colour_none_and_empty():
    assert af.parse_hex_colour(None) is None
    assert af.parse_hex_colour("") is None
    assert af.parse_hex_colour("   ") is None


def test_parse_hex_colour_non_string():
    assert af.parse_hex_colour(0xE4A15D) is None
    assert af.parse_hex_colour(("#e4a15d",)) is None


def test_parse_hex_colour_rejects_bad_shapes():
    assert af.parse_hex_colour("#abc") is None       # 3-digit shorthand
    assert af.parse_hex_colour("#e4a15") is None      # 5 digits
    assert af.parse_hex_colour("#e4a15dd") is None     # 7 digits
    assert af.parse_hex_colour("#gggggg") is None      # non-hex chars
    assert af.parse_hex_colour("e4 a1 5d") is None      # embedded spaces
    assert af.parse_hex_colour("rgb(1,2,3)") is None    # not hex at all
