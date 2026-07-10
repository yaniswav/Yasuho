"""Unit tests for the pure helpers in ``cogs/anilist/chapters.py``.

Everything exercised here is pure and side-effect-free (no network, DB, Discord
or Lavalink): the seen-key (de)serialisation that bridges the pure
:func:`tools.mangadex.chapter_key` tuples and the ``mangadex_seen_chapters`` TEXT
column, the chapter-number / timestamp formatters the card leans on, the per-tick
alert cap, the fan-out target planner, and - the highest-value guard - that the
persistent Read button's custom_id template is fullmatch-disjoint from every other
``alf:`` DynamicItem template, so discord.py's dispatch can never cross-route a
click. Importing the cog module is enough; nothing in it runs at import time.
"""

import re
from datetime import datetime, timezone

from cogs.anilist import chapters as ch
from cogs.anilist.airing import SEEN_TEMPLATE
from cogs.anilist.chapters import (
    MAX_ALERTS_PER_MANGA,
    POLL_SECONDS,
    READ_TEMPLATE,
    _cap_alerts,
    _chapter_number_str,
    _chapter_timestamp,
    _deserialize_key,
    _search_title,
    _serialize_key,
    plan_chapter_targets,
)
from cogs.anilist.feed import ADD_TEMPLATE, LIKE_TEMPLATE, REPLY_TEMPLATE
from tools import mangadex as md

# ---------------------------------------------------------------------------
# documented constants (guard the tuned values the design leans on)
# ---------------------------------------------------------------------------


def test_documented_constants():
    assert POLL_SECONDS == 1800
    assert MAX_ALERTS_PER_MANGA == 3
    assert ch.MAX_MAPPING_SEARCHES_PER_TICK == 3
    assert ch.MAX_LIST_REFRESHES_PER_TICK == 5
    assert ch.SEEN_PRUNE_DAYS == 90
    assert ch.SEEN_PRUNE_KEEP == 200


# ---------------------------------------------------------------------------
# _serialize_key / _deserialize_key - the seen-memory <-> TEXT bridge
# ---------------------------------------------------------------------------


def test_serialize_key_shapes():
    assert _serialize_key(("ch", "386")) == "ch:386"
    assert _serialize_key(("ch", "110.5")) == "ch:110.5"
    assert _serialize_key(("id", "abc-uuid")) == "id:abc-uuid"


def test_deserialize_key_roundtrips_numeric_and_id():
    for key in [("ch", "386"), ("ch", "110.5"), ("id", "abc-uuid-123")]:
        assert _deserialize_key(_serialize_key(key)) == key


def test_deserialize_key_preserves_colon_in_value():
    # A non-numeric label keeps its stripped text (see chapter_key), which can
    # itself carry a colon; the split is on the FIRST colon so it round-trips.
    key = ("ch", "Extra: Part 1")
    assert _serialize_key(key) == "ch:Extra: Part 1"
    assert _deserialize_key("ch:Extra: Part 1") == key


def test_serialize_matches_mangadex_chapter_key():
    # The end-to-end contract: what plan_chapter_alerts hands back as a key must
    # serialise and reload identically, so the loaded seen set compares to fresh
    # keys exactly.
    key = md.chapter_key({"volume": "38", "chapter": "386"})
    assert key == ("ch", "386")
    assert _deserialize_key(_serialize_key(key)) == key

    oneshot = md.chapter_key({"id": "one", "chapter": None})
    assert oneshot == ("id", "one")
    assert _deserialize_key(_serialize_key(oneshot)) == oneshot


# ---------------------------------------------------------------------------
# _chapter_number_str - custom_id-safe chapter number, or None
# ---------------------------------------------------------------------------


def test_chapter_number_str_integer_forms():
    assert _chapter_number_str("386") == "386"
    assert _chapter_number_str(386) == "386"
    assert _chapter_number_str(" 42 ") == "42"


def test_chapter_number_str_decimal_kept():
    assert _chapter_number_str("110.5") == "110.5"
    assert _chapter_number_str("386.0") == "386.0"


def test_chapter_number_str_non_numeric_is_none():
    assert _chapter_number_str(None) is None
    assert _chapter_number_str("") is None
    assert _chapter_number_str("   ") is None
    assert _chapter_number_str("Extra") is None
    assert _chapter_number_str("12a") is None


def test_chapter_number_str_output_fits_read_template():
    # Whatever it returns must sit inside the Read template's chapter group, or the
    # button it builds could not dispatch.
    pattern = re.compile(READ_TEMPLATE)
    for raw in ("386", 386, "110.5", "386.0", " 7 "):
        num = _chapter_number_str(raw)
        assert num is not None
        assert pattern.fullmatch("alf:read:42:{n}".format(n=num))


# ---------------------------------------------------------------------------
# _chapter_timestamp - MangaDex readableAt ISO string -> epoch int
# ---------------------------------------------------------------------------


def test_chapter_timestamp_z_offset_and_naive_agree():
    expected = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
    assert _chapter_timestamp("2023-01-01T00:00:00Z") == expected
    assert _chapter_timestamp("2023-01-01T00:00:00+00:00") == expected
    # A naive value is read as UTC.
    assert _chapter_timestamp("2023-01-01T00:00:00") == expected


def test_chapter_timestamp_junk_and_empty_are_none():
    assert _chapter_timestamp(None) is None
    assert _chapter_timestamp("") is None
    assert _chapter_timestamp("   ") is None
    assert _chapter_timestamp("not-a-date") is None


# ---------------------------------------------------------------------------
# _search_title - romaji, then english, else None
# ---------------------------------------------------------------------------


def test_search_title_prefers_romaji_then_english():
    assert _search_title({"title": {"romaji": "Kingdom", "english": "Kingdom EN"}}) == "Kingdom"
    assert _search_title({"title": {"english": "Only English"}}) == "Only English"


def test_search_title_none_when_absent():
    assert _search_title({}) is None
    assert _search_title({"title": {}}) is None
    assert _search_title(None) is None


# ---------------------------------------------------------------------------
# _cap_alerts - newest kept, oldest dropped (alerts arrive oldest-first)
# ---------------------------------------------------------------------------


def test_cap_alerts_below_cap_keeps_all():
    alerts = [{"id": 1}, {"id": 2}]
    kept, dropped = _cap_alerts(alerts, cap=3)
    assert kept == alerts
    assert dropped == []


def test_cap_alerts_at_cap_keeps_all():
    alerts = [{"id": 1}, {"id": 2}, {"id": 3}]
    kept, dropped = _cap_alerts(alerts, cap=3)
    assert kept == alerts
    assert dropped == []


def test_cap_alerts_above_cap_keeps_newest_drops_oldest():
    # Oldest-first input: [old ... new]; keeping the newest `cap` keeps the tail.
    alerts = [{"id": i} for i in range(1, 6)]  # 1..5
    kept, dropped = _cap_alerts(alerts, cap=3)
    assert [a["id"] for a in kept] == [3, 4, 5]
    assert [a["id"] for a in dropped] == [1, 2]


def test_cap_alerts_default_cap_is_module_constant():
    alerts = [{"id": i} for i in range(1, 8)]  # 7 alerts
    kept, dropped = _cap_alerts(alerts)
    assert len(kept) == MAX_ALERTS_PER_MANGA
    assert [a["id"] for a in kept] == [5, 6, 7]
    assert len(dropped) == 7 - MAX_ALERTS_PER_MANGA


# ---------------------------------------------------------------------------
# plan_chapter_targets - who receives an alert for a manga
# ---------------------------------------------------------------------------


def test_plan_targets_dm_membership():
    dm = {100: {10, 20}, 200: {20, 30}, 300: {40}}
    dm_users, channels = plan_chapter_targets(20, dm, {})
    assert dm_users == [100, 200]  # sorted, only those reading media 20
    assert channels == []


def test_plan_targets_channel_membership():
    channel_media = {(1, 111): {10, 20}, (2, 222): {30}}
    dm_users, channels = plan_chapter_targets(10, {}, channel_media)
    assert dm_users == []
    assert channels == [(1, 111)]


def test_plan_targets_both_and_sorted():
    dm = {900: {5}, 100: {5, 6}}
    channel_media = {(2, 20): {5}, (1, 10): {5}}
    dm_users, channels = plan_chapter_targets(5, dm, channel_media)
    assert dm_users == [100, 900]
    assert channels == [(1, 10), (2, 20)]


def test_plan_targets_no_match_is_empty():
    dm = {1: {10}}
    channel_media = {(1, 1): {10}}
    dm_users, channels = plan_chapter_targets(99, dm, channel_media)
    assert dm_users == []
    assert channels == []


# ---------------------------------------------------------------------------
# READ_TEMPLATE disjointness - the click-dispatch safety guard
# ---------------------------------------------------------------------------

# discord.py dispatches a DynamicItem by fullmatch-ing the clicked custom_id
# against each registered template. If a chapter Read id fullmatched the like /
# reply / add / seen template (or vice versa), a click would run the wrong
# handler. These ids are exactly what each button builds.
_SAMPLES = {
    "like": ["alf:like:1", "alf:like:123456"],
    "reply": ["alf:reply:1", "alf:reply:987654"],
    "add": ["alf:add:1", "alf:add:42"],
    "seen": ["alf:seen:1:1", "alf:seen:123:45"],
    "read": ["alf:read:1:1", "alf:read:42:386", "alf:read:7:110.5"],
}
_TEMPLATES = {
    "like": re.compile(LIKE_TEMPLATE),
    "reply": re.compile(REPLY_TEMPLATE),
    "add": re.compile(ADD_TEMPLATE),
    "seen": re.compile(SEEN_TEMPLATE),
    "read": re.compile(READ_TEMPLATE),
}


def test_each_custom_id_matches_only_its_own_template():
    for kind, ids in _SAMPLES.items():
        for cid in ids:
            assert _TEMPLATES[kind].fullmatch(cid), (kind, cid)
            for other, pattern in _TEMPLATES.items():
                if other == kind:
                    continue
                assert pattern.fullmatch(cid) is None, (other, cid)


def test_read_template_captures_mid_and_chapter():
    m = _TEMPLATES["read"].fullmatch("alf:read:42:110.5")
    assert m.group("mid") == "42"
    assert m.group("chapter") == "110.5"
    m = _TEMPLATES["read"].fullmatch("alf:read:7:386")
    assert m.group("mid") == "7"
    assert m.group("chapter") == "386"


def test_read_template_rejects_non_numeric_chapter():
    # A numberless oneshot never gets a Read button; assert the template would not
    # accept one even if some caller tried to build it.
    assert _TEMPLATES["read"].fullmatch("alf:read:42:Extra") is None
    assert _TEMPLATES["read"].fullmatch("alf:read:42:") is None
    assert _TEMPLATES["read"].fullmatch("alf:read::5") is None
