"""B1: the Features (adoption) and Premium pages of ?botstats.

Pure render/SQL tests for tools/botstats_features.py and
tools/botstats_premium.py, plus the card wiring in cogs/system/botstats.py.
"""

import types

from cogs.system import botstats
from tools import botstats_features as bf
from tools import botstats_premium as bp
from tools import premium_usage


def _flat(sections):
    return "\n".join(h + "\n" + "\n".join(lines) for h, lines in sections)


# ---------------------------------------------------------------------------
# Adoption SQL
# ---------------------------------------------------------------------------
def test_adoption_sql_is_one_statement_per_feature_and_filters_guilds():
    sql = bf.build_adoption_sql(bf.FEATURES)
    assert sql.count("UNION ALL") == len(bf.FEATURES) - 1
    guild_features = [f for f in bf.FEATURES if f.scope == "guild"]
    assert sql.count("guild_id = ANY($1::bigint[])") == len(guild_features)
    assert "$2" not in sql
    assert ";" not in sql


def test_adoption_sql_user_features_are_not_guild_filtered():
    sql = bf.build_adoption_sql(
        [bf.Feature("p", "P", "user_profiles", scope="user")]
    )
    assert "ANY" not in sql
    assert "COUNT(DISTINCT user_id)" in sql


def test_adoption_sql_applies_where_and_column():
    sql = bf.build_adoption_sql(
        [
            bf.Feature("a", "A", "t", where="enabled"),
            bf.Feature("b", "B", "timers", scope="user", where="x = 1", column="e->>'a'"),
        ]
    )
    assert "FROM t WHERE guild_id = ANY($1::bigint[]) AND (enabled)" in sql
    assert "COUNT(DISTINCT e->>'a')::bigint AS n FROM timers WHERE (x = 1)" in sql


def test_feature_keys_are_unique_and_tables_exist_in_schema():
    keys = [f.key for f in bf.FEATURES]
    assert len(keys) == len(set(keys))
    import os
    import re

    path = os.path.join(os.path.dirname(__file__), "..", "..", "schema.sql")
    with open(path, encoding="utf-8") as fh:
        schema = fh.read()
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
    assert {f.table for f in bf.FEATURES} <= tables


async def test_fetch_adoption_passes_guild_ids_and_maps_rows():
    seen = {}

    class Pool:
        async def fetch(self, query, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return [{"key": "welcome", "n": 3}]

    out = await bf.fetch_adoption(Pool(), (5, 6), timeout=9)
    assert out == {"welcome": 3}
    assert seen["args"] == ([5, 6],)
    assert seen["kwargs"] == {"timeout": 9}


# ---------------------------------------------------------------------------
# Features render
# ---------------------------------------------------------------------------
def test_features_sorted_by_adoption_with_percentage():
    text = _flat(bf.render_features({"welcome": 50, "starboard": 10}, 100))
    assert text.index("Welcome messages - 50 of 100 (50.0%)") < text.index(
        "Starboard - 10 of 100 (10.0%)"
    )


def test_unknown_feature_is_unknown_never_zero():
    sections = bf.render_features({"welcome": 1}, 10)
    text = _flat(sections)
    assert "Starboard - unknown" in text
    assert "Starboard - 0" not in text
    # unknown features sort after every known one
    assert text.index("Welcome messages") < text.index("Starboard - unknown")


def test_failed_read_renders_unavailable_not_zeros():
    text = _flat(bf.render_features(None, 10))
    assert "unavailable" in text
    assert " 0 " not in text


def test_zero_guilds_does_not_divide_by_zero():
    text = _flat(bf.render_features({"welcome": 0}, 0))
    assert "Welcome messages - 0 of 0 (-)" in text
    assert "Least adopted" not in text


def test_least_adopted_hint_lists_features_under_five_percent():
    counts = {f.key: 50 for f in bf.FEATURES}
    counts["starboard"] = 1
    counts["welcome"] = 0
    text = _flat(bf.render_features(counts, 100))
    hint = [line for line in text.splitlines() if line.startswith("Under 5%")][0]
    assert "Starboard" in hint and "Welcome messages" in hint
    assert "Tickets" not in hint


def test_least_adopted_hint_ignores_unknown_and_says_none_when_healthy():
    counts = {f.key: 90 for f in bf.FEATURES}
    assert "No feature is under 5%" in _flat(bf.render_features(counts, 100))
    assert "Under 5%" not in _flat(bf.render_features({}, 100))


def test_features_truncate_with_a_count_of_the_rest():
    counts = {f.key: 5 for f in bf.FEATURES}
    sections = bf.render_features(counts, 100, limit=3)
    lines = sections[0][1]
    guild_total = len([f for f in bf.FEATURES if f.scope == "guild"])
    assert len(lines) == 4
    assert lines[-1] == "... and {} more".format(guild_total - 3)


def test_all_features_fit_one_card_text_budget():
    counts = {f.key: 123456 for f in bf.FEATURES}
    assert len(_flat(bf.render_features(counts, 999999))) < 3000


# ---------------------------------------------------------------------------
# Premium
# ---------------------------------------------------------------------------
def _row(at_80, at_100, scopes=10):
    return {"scopes": scopes, "at_80": at_80, "at_100": at_100}


def test_wall_rows_rank_and_skip_capability_gates_and_idle_limits():
    rows = {
        "guild_playlists": _row(5, 1),
        "anilist_feeds": _row(4, 4),
        "role_menus": _row(0, 0),
        "music_247": _row(9, 9),  # FREE is False: nothing to be 80% of
    }
    picked = [spec.key for spec, _row_ in bp.wall_rows(rows)]
    assert picked == ["anilist_feeds", "guild_playlists"]


def test_premium_page_renders_all_three_blocks():
    wall = {"guild_playlists": _row(5, 2, scopes=20)}
    text = _flat(bp.render_premium(3, 60, wall, [("guild_playlists", 4)]))
    assert "3 of 60 servers have Yasuho+ (5.0%)" in text
    assert "2 at the free limit" in text and "5 at 80% or more" in text
    assert "guild_playlists - 4 people" in text


def test_premium_unknowns_are_unknown_not_zero():
    text = _flat(bp.render_premium(None, 0, None, None))
    assert "unknown" in text
    assert text.count("unavailable") == 2
    assert " 0 " not in text


def test_premium_zero_guilds_and_empty_reads():
    text = _flat(bp.render_premium(0, 0, {}, []))
    assert "0 of 0 servers have Yasuho+ (-)" in text
    assert "No free limit has anyone at 80% or more." in text
    assert "No limit note was shown" in text


def test_premium_truncates_walls_and_notes():
    wall = {spec.key: _row(1, 1) for spec in premium_usage.RESOURCE_SPECS if premium_usage.free_value(spec)}
    sections = bp.render_premium(1, 1, wall, [("k%d" % i, 1) for i in range(bp.UPSELL_LIMIT + 1)], limit=2)
    pressed = sections[1][1]
    assert any(line.startswith("... and ") for line in pressed)
    assert sections[2][1][-2] == "... and more"


def test_count_premium_guilds_uses_the_cache_and_tolerates_none():
    cache = types.SimpleNamespace(is_guild_premium=lambda gid: gid % 2 == 0)
    guilds = [types.SimpleNamespace(id=i) for i in range(1, 7)]
    assert bp.count_premium_guilds(cache, guilds) == 3
    assert bp.count_premium_guilds(None, guilds) is None


async def test_upsell_counts_query_excludes_sentinel_and_bounds():
    seen = {}

    class Pool:
        async def fetch(self, query, *args, **kwargs):
            seen["args"] = args
            return [{"limit_key": "x", "people": 2}]

    out = await bp.fetch_upsell_counts(Pool(), timeout=1, limit=5)
    assert out == [("x", 2)]
    assert seen["args"][0] == "*" and seen["args"][2] == 6
    assert "premium_upsells" in bp.UPSELL_COUNTS


# ---------------------------------------------------------------------------
# Card wiring
# ---------------------------------------------------------------------------
class _Pool:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def fetch(self, query, *args, **kwargs):
        self.calls.append(query)
        if self.fail:
            raise RuntimeError("boom")
        if "premium_upsells" in query:
            return []
        return [{"key": "welcome", "n": 1}]

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append(query)
        return {"scopes": 0, "median": None, "p90": None, "p99": None, "max": None,
                "at_80": 0, "at_100": 0}


def _card(pool):
    bot = types.SimpleNamespace(
        guilds=[types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)],
        users=[], latency=0.1, shard_count=None, db_pool=pool,
        premium=types.SimpleNamespace(is_guild_premium=lambda gid: gid == 1),
    )
    return botstats.BotStatsDashboard(botstats.BotStats(bot), author_id=1)


async def test_features_page_memoises_and_retries_failures():
    pool = _Pool()
    card = _card(pool)
    first, _t = await card._load(botstats.PAGE_FEATURES)
    await card._load(botstats.PAGE_FEATURES)
    assert len(pool.calls) == 1
    assert "Welcome messages - 1 of 2 (50.0%)" in _flat(first)

    failing = _Pool(fail=True)
    card = _card(failing)
    sections, _t = await card._load(botstats.PAGE_FEATURES)
    assert "unavailable" in _flat(sections)
    await card._load(botstats.PAGE_FEATURES)
    assert len(failing.calls) == 2  # a failure is not memoised


async def test_premium_page_loads_and_card_lays_out_seven_buttons_in_three_rows():
    pool = _Pool()
    card = _card(pool)
    sections, taken = await card._load(botstats.PAGE_PREMIUM)
    assert "1 of 2 servers have Yasuho+ (50.0%)" in _flat(sections)
    card.index = botstats.PAGE_PREMIUM
    card._rerender(sections, taken)
    container = card.children[0]
    rows = [c for c in container.children if hasattr(c, "children") and any(
        isinstance(i, botstats._PageButton) for i in c.children)]
    assert [len(r.children) for r in rows] == [3, 3, 1]
