"""Unit tests for tools/mangadex.py (pure MangaDex chapter-alert helpers).

The core is pure: no aiohttp, database or Discord. These tests pin the three
decisions the chapter tracker leans on - AniList -> MangaDex mapping (scan ALL
candidates, never the first hit), feed normalisation (stub routing, malformed
rows skipped), and the dedup + cursor planner (same-chapter multi-group dedup,
the late re-upload trap, first-run anchoring, no backfill) - plus the request
builders that pin the MangaDex contract and the translation-language filter, whose
whole point is that it shapes the REQUEST only and never the alert identity. All
payloads are fabricated; nothing touches the network.
"""

from tools import mangadex as md

# ---------------------------------------------------------------------------
# request builders (pin the MangaDex contract)
# ---------------------------------------------------------------------------


def test_user_agent_is_identifiable():
    # MangaDex's ToS requires an identifiable User-Agent on every request.
    assert "Yasuho" in md.USER_AGENT
    assert md.USER_AGENT.strip() != ""


def test_search_manga_request_shape():
    url, params, headers = md.search_manga_request("Kingdom")
    assert url == "https://api.mangadex.org/manga"
    assert ("title", "Kingdom") in params
    assert ("limit", str(md.SEARCH_LIMIT)) in params
    assert headers["User-Agent"] == md.USER_AGENT


def test_search_manga_request_clamps_limit():
    _, params, _ = md.search_manga_request("x", limit=9999)
    assert ("limit", "100") in params
    _, params, _ = md.search_manga_request("x", limit=0)
    assert ("limit", "1") in params


def test_manga_feed_request_shape():
    url, params, headers = md.manga_feed_request("uuid-123")
    assert url == "https://api.mangadex.org/manga/uuid-123/feed"
    # A per-manga feed, English only by default, newest-first by readableAt - never
    # the global /chapter feed.
    assert ("translatedLanguage[]", "en") in params
    assert ("order[readableAt]", "desc") in params
    assert ("limit", str(md.FEED_LIMIT)) in params
    assert headers["User-Agent"] == md.USER_AGENT


def test_manga_feed_request_language_override():
    _url, params, _headers = md.manga_feed_request("uuid-123", languages="fr")
    assert ("translatedLanguage[]", "fr") in params


def test_manga_feed_request_emits_one_pair_per_language():
    # Repeated translatedLanguage[] pairs are how MangaDex expresses OR, which is
    # why params is a LIST: a dict could not carry the same key twice.
    _url, params, _headers = md.manga_feed_request(
        "uuid-123", languages=["fr", "es", "fr"]
    )
    langs = [value for key, value in params if key == "translatedLanguage[]"]
    assert langs[0] == "en"  # the safety net is always requested, and always first
    assert sorted(langs) == ["en", "es", "fr"]  # deduplicated


def test_manga_feed_request_clamps_the_language_union():
    _url, params, _headers = md.manga_feed_request(
        "uuid-123", languages=["fr", "es", "de", "it", "ru", "pl"]
    )
    langs = [value for key, value in params if key == "translatedLanguage[]"]
    assert len(langs) == md.MAX_FEED_LANGUAGES
    assert langs[0] == "en"


def test_manga_feed_request_page_scales_with_the_union():
    # One request, more rows: a multi-language feed carries one row per language per
    # chapter, so the page grows to keep covering ~FEED_LIMIT distinct chapters.
    _url, params, _headers = md.manga_feed_request("uuid-123", languages=["fr", "es"])
    assert ("limit", str(md.FEED_LIMIT * 3)) in params


def test_manga_feed_request_clamps_an_explicit_limit():
    _url, params, _headers = md.manga_feed_request("uuid-123", limit=9999)
    assert ("limit", "100") in params
    _url, params, _headers = md.manga_feed_request("uuid-123", limit=0)
    assert ("limit", "1") in params


# ---------------------------------------------------------------------------
# translation languages (C0d) - the request-only filter
# ---------------------------------------------------------------------------


def test_normalize_language_accepts_offered_codes_any_case_or_separator():
    assert md.normalize_language("fr") == "fr"
    assert md.normalize_language("PT_BR") == "pt-br"
    assert md.normalize_language(" es-LA ") == "es-la"


def test_normalize_language_falls_back_to_the_base_language():
    # A regional code we do not offer still maps to something MangaDex serves.
    assert md.normalize_language("es-mx") == "es"
    assert md.normalize_language("fr-CA") == "fr"


def test_normalize_language_refuses_anything_unoffered():
    # MangaDex answers an unknown two-letter code with a silently EMPTY feed, so a
    # pass-through would look exactly like "no new chapters" forever.
    assert md.normalize_language("xx") is None
    assert md.normalize_language("klingon") is None
    assert md.normalize_language("") is None
    assert md.normalize_language(None) is None


def test_every_offered_language_normalizes_to_itself():
    for code, name in md.LANGUAGES:
        assert md.normalize_language(code) == code
        assert md.language_name(code) == name
    assert md.DEFAULT_LANGUAGE in dict(md.LANGUAGES)
    assert len(md.LANGUAGES) <= 25  # Discord's select-option ceiling


def test_language_name_falls_back_to_the_code():
    assert md.language_name("xx") == "xx"


def test_feed_languages_always_leads_with_the_safety_net():
    assert md.feed_languages(None) == ["en"]
    assert md.feed_languages([]) == ["en"]
    assert md.feed_languages(["fr"])[0] == "en"


def test_feed_languages_dedupes_and_drops_junk():
    assert md.feed_languages(["fr", "fr", None, "xx", "en"]) == ["en", "fr"]


def test_feed_languages_orders_by_demand_then_code():
    # The caller keeps the head of this list, so the most-requested languages are
    # the ones that survive the MAX_FEED_LANGUAGES clamp.
    got = md.feed_languages(["de", "fr", "fr", "es", "es", "es"])
    assert got == ["en", "es", "fr", "de"]


def test_feed_languages_is_deterministic_on_ties():
    assert md.feed_languages(["fr", "de"]) == md.feed_languages(["de", "fr"])


def test_feed_page_limit_scales_and_clamps():
    assert md.feed_page_limit(["en"]) == md.FEED_LIMIT
    assert md.feed_page_limit(["en", "fr", "es"]) == md.FEED_LIMIT * 3
    # Never past MangaDex's 100-row page, and never below one row.
    assert md.feed_page_limit(["en"] * 50) == md.FEED_LIMIT * md.MAX_FEED_LANGUAGES
    assert md.feed_page_limit([], base=1000) == 100
    assert md.feed_page_limit(["en"], base=0) == 1


# ---------------------------------------------------------------------------
# pick_mapping - AniList id -> MangaDex UUID (scan ALL, never the first)
# ---------------------------------------------------------------------------


def _candidate(uuid, al=None):
    links = {} if al is None else {"al": al}
    return {"id": uuid, "type": "manga", "attributes": {"links": links}}


def test_pick_mapping_scans_all_candidates_not_the_first():
    # The wanted title is not first (the real "Kingdom" ranks third): the exact
    # links.al match must win over rank.
    candidates = [
        _candidate("wrong-1", al="99999"),
        _candidate("wrong-2", al="88888"),
        _candidate("right", al="30002"),
    ]
    assert md.pick_mapping(candidates, 30002) == "right"


def test_pick_mapping_matches_int_against_string_link():
    # MangaDex stores links.al as a STRING; the AniList id is an int.
    assert md.pick_mapping([_candidate("m", al="12345")], 12345) == "m"


def test_pick_mapping_accepts_full_payload_or_bare_list():
    payload = {"result": "ok", "data": [_candidate("m", al="7")]}
    assert md.pick_mapping(payload, 7) == "m"
    assert md.pick_mapping([_candidate("m", al="7")], 7) == "m"


def test_pick_mapping_miss_returns_none():
    # A niche title with no candidate carrying the AniList id is a recorded miss,
    # not an error.
    assert md.pick_mapping([_candidate("m", al="1")], 999) is None
    assert md.pick_mapping([], 999) is None


def test_pick_mapping_skips_malformed_candidates_without_crashing():
    candidates = [
        "not-a-dict",
        {"id": "no-attrs"},
        {"id": "attrs-not-dict", "attributes": "nope"},
        {"id": "links-not-dict", "attributes": {"links": "nope"}},
        {"id": "no-al", "attributes": {"links": {"mal": "5"}}},
        _candidate("good", al="42"),
    ]
    assert md.pick_mapping(candidates, 42) == "good"


def test_pick_mapping_ignores_match_with_no_id():
    # A candidate that matches but carries no UUID cannot be a mapping target.
    candidates = [{"attributes": {"links": {"al": "42"}}}, _candidate("good", al="42")]
    assert md.pick_mapping(candidates, 42) == "good"


# ---------------------------------------------------------------------------
# reader_url - MangaDex reader page vs official stub
# ---------------------------------------------------------------------------


def test_reader_url_builds_mangadex_page():
    assert md.reader_url({"id": "abc"}) == "https://mangadex.org/chapter/abc"


def test_reader_url_routes_to_external_stub():
    # externalUrl (e.g. MangaPlus) means there is NO MangaDex reader page.
    chapter = {"id": "abc", "externalUrl": "https://mangaplus.shueisha.co.jp/x"}
    assert md.reader_url(chapter) == "https://mangaplus.shueisha.co.jp/x"


def test_reader_url_none_when_no_id_and_no_stub():
    assert md.reader_url({}) is None


# ---------------------------------------------------------------------------
# parse_chapter_feed - normalisation
# ---------------------------------------------------------------------------


def _feed_entry(cid, chapter="1", volume=None, external=None, readable="2023-01-01T00:00:00Z"):
    return {
        "id": cid,
        "type": "chapter",
        "attributes": {
            "volume": volume,
            "chapter": chapter,
            "title": "A title",
            "translatedLanguage": "en",
            "externalUrl": external,
            "readableAt": readable,
        },
    }


def test_parse_chapter_feed_basic_fields():
    parsed = md.parse_chapter_feed({"data": [_feed_entry("c1", chapter="386", volume="38")]})
    assert len(parsed) == 1
    ch = parsed[0]
    assert ch["id"] == "c1"
    assert ch["volume"] == "38"
    assert ch["chapter"] == "386"
    assert ch["title"] == "A title"
    assert ch["readableAt"] == "2023-01-01T00:00:00Z"
    assert ch["url"] == "https://mangadex.org/chapter/c1"


def test_parse_chapter_feed_external_stub_routes_url():
    parsed = md.parse_chapter_feed(
        {"data": [_feed_entry("c1", external="https://mangaplus.shueisha.co.jp/x")]}
    )
    assert parsed[0]["externalUrl"] == "https://mangaplus.shueisha.co.jp/x"
    assert parsed[0]["url"] == "https://mangaplus.shueisha.co.jp/x"


def test_parse_chapter_feed_accepts_bare_list():
    parsed = md.parse_chapter_feed([_feed_entry("c1")])
    assert [c["id"] for c in parsed] == ["c1"]


def test_parse_chapter_feed_empty():
    assert md.parse_chapter_feed({"data": []}) == []
    assert md.parse_chapter_feed({}) == []
    assert md.parse_chapter_feed(None) == []


def test_parse_chapter_feed_skips_malformed_without_crashing():
    payload = {
        "data": [
            "not-a-dict",
            {"attributes": {"chapter": "1"}},          # no id
            {"id": "no-attrs"},                          # no attributes
            {"id": "attrs-not-dict", "attributes": 5},   # attributes not a dict
            _feed_entry("good", chapter="7"),
        ]
    }
    parsed = md.parse_chapter_feed(payload)
    assert [c["id"] for c in parsed] == ["good"]


def test_parse_chapter_feed_preserves_null_volume_and_decimal_chapter():
    parsed = md.parse_chapter_feed(
        {"data": [_feed_entry("c1", chapter="110.5", volume=None)]}
    )
    assert parsed[0]["volume"] is None
    assert parsed[0]["chapter"] == "110.5"


# ---------------------------------------------------------------------------
# chapter_key - the per-manga dedup identity
# ---------------------------------------------------------------------------


def test_chapter_key_ignores_volume():
    # Groups disagree on the volume tag; identity is the chapter number alone,
    # so a volume-tagged and a volume-less upload of ch 386 are the SAME chapter.
    tagged = md.chapter_key({"volume": "38", "chapter": "386"})
    bare = md.chapter_key({"volume": None, "chapter": "386"})
    assert tagged == bare == ("ch", "386")


def test_chapter_key_null_volume():
    assert md.chapter_key({"volume": None, "chapter": "5"}) == ("ch", "5")


def test_chapter_key_canonicalises_numeric_forms():
    # Whatever the source spelling, a numeric chapter compares canonically.
    assert (
        md.chapter_key({"chapter": 386})
        == md.chapter_key({"chapter": "386"})
        == md.chapter_key({"chapter": "386.0"})
        == ("ch", "386")
    )
    assert md.chapter_key({"chapter": "110.5"}) == ("ch", "110.5")
    # A non-numeric label keeps its stripped text.
    assert md.chapter_key({"chapter": " Extra "}) == ("ch", "Extra")


def test_chapter_key_oneshot_falls_back_to_id():
    # A numberless oneshot must stay distinct, not collapse with every other.
    # A volume-only row is numberless too (no trustworthy chapter identity).
    key = md.chapter_key({"id": "one", "volume": None, "chapter": None})
    assert key == ("id", "one")
    other = md.chapter_key({"id": "two", "volume": "3", "chapter": None})
    assert other == ("id", "two")
    assert key != other


def test_chapter_key_none_when_no_identity_at_all():
    assert md.chapter_key({"volume": None, "chapter": None}) is None


# ---------------------------------------------------------------------------
# chapter_sort_key - decimals and null handling
# ---------------------------------------------------------------------------


def test_chapter_sort_key_orders_decimals():
    chapters = [
        {"chapter": "110.5"},
        {"chapter": "110"},
        {"chapter": "111"},
    ]
    ordered = sorted(chapters, key=md.chapter_sort_key)
    assert [c["chapter"] for c in ordered] == ["110", "110.5", "111"]


def test_chapter_sort_key_null_chapter_sorts_last():
    chapters = [{"chapter": None}, {"chapter": "5"}, {"chapter": "1"}]
    ordered = sorted(chapters, key=md.chapter_sort_key)
    assert [c["chapter"] for c in ordered] == ["1", "5", None]


def test_chapter_sort_key_orders_by_volume_then_chapter():
    chapters = [
        {"volume": "2", "chapter": "1"},
        {"volume": "1", "chapter": "9"},
    ]
    ordered = sorted(chapters, key=md.chapter_sort_key)
    assert [(c["volume"], c["chapter"]) for c in ordered] == [("1", "9"), ("2", "1")]


# ---------------------------------------------------------------------------
# plan_chapter_alerts - the dedup + cursor core
# ---------------------------------------------------------------------------


def _ch(cid, chapter, readable, volume=None, external=None):
    return {
        "id": cid,
        "volume": volume,
        "chapter": chapter,
        "readableAt": readable,
        "externalUrl": external,
    }


def test_plan_first_run_anchors_and_alerts_nothing():
    # cursor None -> anti-backfill anchor: post nothing, seed the seen memory with
    # every current key, and set the cursor to the newest readableAt.
    feed = [
        _ch("b", "101", "2023-01-02T00:00:00Z"),
        _ch("a", "100", "2023-01-01T00:00:00Z"),
    ]
    alerts, cursor, seen = md.plan_chapter_alerts(feed, None, set())
    assert alerts == []
    assert cursor == "2023-01-02T00:00:00Z"
    assert seen == {("ch", "100"), ("ch", "101")}


def test_plan_first_run_empty_feed_leaves_cursor_none():
    alerts, cursor, seen = md.plan_chapter_alerts([], None, set())
    assert alerts == []
    assert cursor is None
    assert seen == set()


def test_plan_alerts_a_new_chapter_and_advances_cursor():
    feed = [_ch("a", "200", "2023-06-02T00:00:00Z", volume="20")]
    alerts, cursor, seen = md.plan_chapter_alerts(
        feed, "2023-06-01T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts] == ["a"]
    assert cursor == "2023-06-02T00:00:00Z"
    assert seen == {("ch", "200")}


def test_plan_two_groups_same_chapter_alert_once_first_seen_wins():
    # The same logical chapter uploaded by two groups: alert exactly once, and the
    # FIRST-SEEN (earliest readableAt) row wins even though the feed is newest-first.
    group_b = _ch("grp-b", "386", "2023-06-02T00:00:00Z", volume="38")
    group_a = _ch("grp-a", "386", "2023-06-01T00:00:00Z", volume="38")
    feed = [group_b, group_a]  # newest-first, as MangaDex returns it
    alerts, cursor, seen = md.plan_chapter_alerts(
        feed, "2023-05-01T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts] == ["grp-a"]  # first-seen wins
    assert cursor == "2023-06-02T00:00:00Z"        # newest processed
    assert seen == {("ch", "386")}


def test_plan_volume_inconsistent_groups_alert_once():
    # Orchestrator counter-test regression: one group tags the volume, the other
    # does not. Same chapter number = same chapter, both same-tick and cross-tick.
    tagged = _ch("grp-a", "386", "2023-06-01T10:00:00Z", volume="2")
    bare = _ch("grp-b", "386", "2023-06-01T11:00:00Z", volume=None)

    alerts, _, seen = md.plan_chapter_alerts(
        [bare, tagged], "2023-05-01T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts] == ["grp-a"]  # one alert, first-seen wins
    assert seen == {("ch", "386")}

    # Cross-tick: the tagged row alerted last tick, the bare one arrives later.
    alerts1, cursor1, seen1 = md.plan_chapter_alerts(
        [tagged], "2023-05-01T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts1] == ["grp-a"]
    alerts2, _, _ = md.plan_chapter_alerts([bare], cursor1, seen1)
    assert alerts2 == []


def test_plan_late_reupload_trap_does_not_realert():
    # THE trap: a second group re-uploads an ALREADY-alerted chapter with a LATER
    # readableAt. A naive "readableAt > cursor" would re-alert it; the seen memory
    # must suppress it while the cursor still advances past the re-upload.
    reupload = _ch("grp-b", "386", "2023-06-05T00:00:00Z", volume="38")
    alerts, cursor, seen = md.plan_chapter_alerts(
        [reupload], "2023-06-01T00:00:00Z", {("ch", "386")}
    )
    assert alerts == []
    assert cursor == "2023-06-05T00:00:00Z"
    assert seen == {("ch", "386")}


def test_plan_never_realerts_a_seen_key_even_when_newer():
    # Same guarantee stated directly: a seen key is never alerted, whatever its
    # readableAt.
    feed = [_ch("x", "10", "2999-01-01T00:00:00Z", volume="1")]
    alerts, _, _ = md.plan_chapter_alerts(feed, "2023-01-01T00:00:00Z", {("ch", "10")})
    assert alerts == []


def test_plan_alerts_oldest_first_from_newest_first_feed():
    feed = [
        _ch("c", "3", "2023-01-03T00:00:00Z", volume="1"),
        _ch("b", "2", "2023-01-02T00:00:00Z", volume="1"),
        _ch("a", "1", "2023-01-01T00:00:00Z", volume="1"),
    ]
    alerts, cursor, _ = md.plan_chapter_alerts(feed, "2022-12-01T00:00:00Z", set())
    assert [c["id"] for c in alerts] == ["a", "b", "c"]
    assert cursor == "2023-01-03T00:00:00Z"


def test_plan_skips_chapters_at_or_below_cursor_no_backfill():
    # A chapter older than the cursor is old ground: never alerted, and NOT added
    # to the seen memory (the cursor already guards that range).
    feed = [
        _ch("old", "1", "2023-01-01T00:00:00Z", volume="1"),   # below cursor
        _ch("new", "2", "2023-01-05T00:00:00Z", volume="1"),   # above cursor
    ]
    alerts, cursor, seen = md.plan_chapter_alerts(
        feed, "2023-01-03T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts] == ["new"]
    assert cursor == "2023-01-05T00:00:00Z"
    assert seen == {("ch", "2")}  # only the fresh key, the old one is not tracked


def test_plan_cursor_never_regresses():
    # A feed whose newest row is still below the cursor cannot move it back.
    feed = [_ch("old", "1", "2023-01-01T00:00:00Z", volume="1")]
    alerts, cursor, seen = md.plan_chapter_alerts(
        feed, "2023-06-01T00:00:00Z", set()
    )
    assert alerts == []
    assert cursor == "2023-06-01T00:00:00Z"  # unchanged
    assert seen == set()


def test_plan_empty_feed_returns_inputs_unchanged():
    alerts, cursor, seen = md.plan_chapter_alerts(
        [], "2023-01-01T00:00:00Z", {("ch", "1")}
    )
    assert alerts == []
    assert cursor == "2023-01-01T00:00:00Z"
    assert seen == {("ch", "1")}


def test_plan_skips_malformed_rows_without_crashing():
    feed = [
        _ch("no-date", "5", None, volume="1"),  # undateable readableAt
        # no id, no volume, no chapter -> no identity key at all
        {"volume": None, "chapter": None, "readableAt": "2023-02-01T00:00:00Z"},
        _ch("good", "6", "2023-02-02T00:00:00Z", volume="1"),
    ]
    alerts, cursor, seen = md.plan_chapter_alerts(
        feed, "2023-01-01T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts] == ["good"]
    assert cursor == "2023-02-02T00:00:00Z"
    assert seen == {("ch", "6")}


def test_plan_handles_decimal_chapter_and_null_volume():
    feed = [_ch("dec", "110.5", "2023-03-01T00:00:00Z", volume=None)]
    alerts, cursor, seen = md.plan_chapter_alerts(
        feed, "2023-01-01T00:00:00Z", set()
    )
    assert [c["id"] for c in alerts] == ["dec"]
    assert seen == {("ch", "110.5")}


def test_plan_accepts_datetime_and_epoch_cursor():
    # The cog may hand back a datetime (asyncpg TIMESTAMPTZ) or an epoch; both must
    # compare correctly against ISO readableAt strings.
    from datetime import datetime, timezone

    feed = [_ch("a", "2", "2023-01-05T00:00:00Z", volume="1")]
    dt_cursor = datetime(2023, 1, 3, tzinfo=timezone.utc)
    alerts, _, _ = md.plan_chapter_alerts(feed, dt_cursor, set())
    assert [c["id"] for c in alerts] == ["a"]

    epoch_cursor = datetime(2023, 1, 3, tzinfo=timezone.utc).timestamp()
    alerts, _, _ = md.plan_chapter_alerts(feed, epoch_cursor, set())
    assert [c["id"] for c in alerts] == ["a"]


def test_plan_handles_z_and_offset_readable_at_equivalently():
    # "...Z" and "...+00:00" denote the same instant; neither should double-alert
    # nor mis-order relative to the cursor.
    feed_z = [_ch("a", "1", "2023-01-05T00:00:00Z", volume="1")]
    feed_offset = [_ch("a", "1", "2023-01-05T00:00:00+00:00", volume="1")]
    a1, _, _ = md.plan_chapter_alerts(feed_z, "2023-01-01T00:00:00Z", set())
    a2, _, _ = md.plan_chapter_alerts(feed_offset, "2023-01-01T00:00:00Z", set())
    assert [c["id"] for c in a1] == [c["id"] for c in a2] == ["a"]


# ---------------------------------------------------------------------------
# index_variants / pick_variant - the per-recipient link, and the invariant that
# language is a PRESENTATION detail: it never enters the alert identity, so the
# at-most-once guarantee survives a multi-language feed untouched.
# ---------------------------------------------------------------------------


def _lang_ch(cid, chapter, readable, language):
    row = _ch(cid, chapter, readable)
    row["translatedLanguage"] = language
    return row


def test_index_variants_groups_one_chapter_by_language():
    feed = [
        _lang_ch("en-386", "386", "2023-01-02T00:00:00Z", "en"),
        _lang_ch("fr-386", "386", "2023-01-01T00:00:00Z", "fr"),
        _lang_ch("en-385", "385", "2022-12-30T00:00:00Z", "en"),
    ]
    variants = md.index_variants(feed)
    assert set(variants) == {("ch", "386"), ("ch", "385")}
    assert variants[("ch", "386")]["fr"]["id"] == "fr-386"
    assert variants[("ch", "385")].keys() == {"en"}


def test_index_variants_keeps_the_newest_row_per_language():
    # The feed is readableAt DESC, so the FIRST row of a language is its newest
    # upload; a second scanlation group of the same language must not replace it.
    feed = [
        _lang_ch("new", "386", "2023-01-02T00:00:00Z", "en"),
        _lang_ch("old", "386", "2023-01-01T00:00:00Z", "en"),
    ]
    assert md.index_variants(feed)[("ch", "386")]["en"]["id"] == "new"


def test_index_variants_skips_rows_with_no_language_or_identity():
    feed = [
        _ch("no-lang", "386", "2023-01-02T00:00:00Z"),
        {"translatedLanguage": "fr"},  # no id, no number -> no identity
    ]
    assert md.index_variants(feed) == {}


def test_pick_variant_returns_the_recipients_language():
    en = _lang_ch("en-386", "386", "2023-01-02T00:00:00Z", "en")
    fr = _lang_ch("fr-386", "386", "2023-01-01T00:00:00Z", "fr")
    variants = md.index_variants([en, fr])
    assert md.pick_variant(variants, en, "fr") is fr
    assert md.pick_variant(variants, fr, "en") is en


def test_pick_variant_falls_back_to_the_alerted_row():
    # Their language is not in this window (not released yet, or below the page):
    # they still get the alert, pointed at the release it fired on - never nothing.
    en = _lang_ch("en-386", "386", "2023-01-02T00:00:00Z", "en")
    variants = md.index_variants([en])
    assert md.pick_variant(variants, en, "fr") is en
    assert md.pick_variant(variants, en, "xx") is en
    assert md.pick_variant(variants, en, None) is en
    assert md.pick_variant({}, en, "fr") is en


def test_a_later_translation_never_re_alerts():
    # THE invariant of C0d. Tick 1 alerts chapter 386 from its English release; tick
    # 2's feed carries the French upload of the SAME chapter with a LATER readableAt
    # (above the advanced cursor). The identity is the number alone, so it is
    # deduplicated exactly like a second scanlation group: no second alert, ever.
    tick1 = [_lang_ch("en-386", "386", "2023-01-01T00:00:00Z", "en")]
    alerts, cursor, seen = md.plan_chapter_alerts(tick1, "2022-12-01T00:00:00Z", set())
    assert [c["id"] for c in alerts] == ["en-386"]

    tick2 = [_lang_ch("fr-386", "386", "2023-01-05T00:00:00Z", "fr")]
    alerts, cursor2, seen2 = md.plan_chapter_alerts(tick2, cursor, seen)
    assert alerts == []
    assert seen2 == seen  # the identity was already recorded, nothing new to persist
    assert cursor2 == "2023-01-05T00:00:00Z"  # the cursor still advances


def test_same_tick_multi_language_rows_collapse_to_one_alert():
    # Both translations land in the SAME window: one alert (oldest-first wins), and
    # the other language stays available to pick_variant for the link.
    feed = [
        _lang_ch("fr-386", "386", "2023-01-02T00:00:00Z", "fr"),
        _lang_ch("en-386", "386", "2023-01-01T00:00:00Z", "en"),
    ]
    alerts, _cursor, seen = md.plan_chapter_alerts(feed, "2022-12-01T00:00:00Z", set())
    assert [c["id"] for c in alerts] == ["en-386"]
    assert seen == {("ch", "386")}
    variants = md.index_variants(feed)
    assert md.pick_variant(variants, alerts[0], "fr")["id"] == "fr-386"
