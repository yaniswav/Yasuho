"""V5e-A: YouTube upload alerts - parsing, dedup/baseline, shared fetch, limits.

No network, no real clock: feeds are built around a fixed NOW.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import types
from pathlib import Path

import pytest

from cogs.config import youtube
from cogs.config.youtube import YouTubeAlerts
from tools import premium
from tools import youtube as yt

NOW = datetime.datetime(2030, 1, 10, tzinfo=datetime.timezone.utc)
HOUR = datetime.timedelta(hours=1)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "youtube"
UC_A = "UC" + "a" * 22
UC_B = "UC" + "b" * 22
UC_C = "UC" + "c" * 22
UC_D = "UC" + "d" * 22


def feed_xml(items, title="Example"):
    """items: (video_id, age timedelta, is_short) newest first."""
    entries = ""
    for vid, age, short in items:
        path = "shorts/" + vid if short else "watch?v=" + vid
        entries += (
            "<entry><id>yt:video:%s</id><yt:videoId>%s</yt:videoId>"
            "<title>Title %s</title><link rel=\"alternate\" href=\"https://www.youtube.com/%s\"/>"
            "<published>%s</published></entry>"
            % (vid, vid, vid, path, (NOW - age).isoformat())
        )
    return (
        '<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns="http://www.w3.org/2005/Atom"><title>%s</title>%s</feed>' % (title, entries)
    )


# -- tools.youtube: input parsing ---------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        (UC_A, ("id", UC_A)),
        ("@Some.Handle", ("path", "/@Some.Handle")),
        ("https://www.youtube.com/@handle_1", ("path", "/@handle_1")),
        ("youtube.com/channel/" + UC_B, ("id", UC_B)),
        ("https://m.youtube.com/c/SomeName", ("path", "/c/SomeName")),
        ("https://www.youtube.com/user/Old", ("path", "/user/Old")),
        ("https://evil.com/@handle", None),
        ("https://youtube.com.evil.com/@handle", None),
        ("https://www.youtube.com:8080/@handle", None),
        ("https://user:pw@www.youtube.com/@handle", None),
        ("http://169.254.169.254/channel/" + UC_A, None),
        ("https://www.youtube.com/watch?v=abc", None),
        ("https://www.youtube.com/channel/UCshort", None),
        ("@ab", None),
        ("", None),
    ],
)
def test_parse_channel_input(text, expected):
    assert yt.parse_channel_input(text) == expected


def test_channel_id_is_extracted_from_the_canonical_link():
    html = (FIXTURES / "channel_page.html").read_text(encoding="utf-8")
    assert yt.extract_channel_id(html) == UC_A
    assert yt.extract_channel_id("<html>no link</html>") is None


def test_feed_parsing_flags_shorts_and_refuses_a_dtd():
    feed = yt.parse_feed(feed_xml([("v1", HOUR, False), ("v2", HOUR, True)], title="T"))
    assert feed.title == "T"
    assert [(e.video_id, e.is_short) for e in feed.entries] == [("v1", False), ("v2", True)]
    assert feed.entries[0].url == "https://www.youtube.com/watch?v=v1"
    with pytest.raises(yt.FeedError):
        yt.parse_feed('<!DOCTYPE x [<!ENTITY a "b">]>' + feed_xml([]))
    with pytest.raises(yt.FeedError):
        yt.parse_feed("not xml")


# -- plan_follow ---------------------------------------------------------------
def _entries(*items):
    return yt.parse_feed(feed_xml(list(items))).entries


def test_a_baseline_run_posts_nothing_and_records_everything():
    entries = _entries(("v1", HOUR, False), ("v2", 2 * HOUR, False))
    seen, post = youtube.plan_follow(entries, [], False, NOW, False)
    assert post == [] and seen == ["v1", "v2"]


def test_only_unseen_recent_non_short_videos_are_posted_oldest_first():
    entries = _entries(
        ("new2", HOUR, False),
        ("short", 2 * HOUR, True),
        ("new1", 3 * HOUR, False),
        ("old", 5 * 24 * HOUR, False),
        ("known", 6 * HOUR, False),
    )
    seen, post = youtube.plan_follow(entries, ["known"], True, NOW, False)
    assert [e.video_id for e in post] == ["new1", "new2"]
    assert set(["short", "old"]) <= set(seen)  # handled, never posted later
    _, with_shorts = youtube.plan_follow(entries, ["known"], True, NOW, True)
    assert "short" in [e.video_id for e in with_shorts]


def test_a_burst_is_capped_per_cycle_and_the_seen_list_is_bounded():
    entries = _entries(*[("v%d" % i, i * HOUR, False) for i in range(1, 8)])
    seen, post = youtube.plan_follow(entries, ["x%d" % i for i in range(80)], True, NOW, False)
    assert len(post) == youtube.MAX_POSTS_PER_CYCLE
    assert [e.video_id for e in post] == ["v3", "v2", "v1"]  # the newest three
    assert len(seen) == youtube.SEEN_CAP and seen[0] == "v1"


def test_backoff_grows_then_caps():
    assert youtube.backoff_delay(1) == youtube.POLL_SECONDS
    assert youtube.backoff_delay(2) == 2 * youtube.POLL_SECONDS
    assert youtube.backoff_delay(30) == youtube.MAX_BACKOFF
    assert youtube.backoff_delay(1, gone=True) == youtube.GONE_BACKOFF


# -- fakes ---------------------------------------------------------------------
class FakePool:
    """The few youtube_follows statements the cog issues, held in memory."""

    def __init__(self):
        self.rows = []
        self.counter = 0

    def add(self, guild_id, channel_id, post_channel=500, baselined=True, seen=()):
        self.counter += 1
        self.rows.append(
            dict(guild_id=guild_id, yt_channel_id=channel_id, post_channel=post_channel,
                 baselined=baselined, seen=list(seen), title="", added_at=self.counter)
        )

    async def fetch(self, sql, *args):
        assert "FROM youtube_follows ORDER BY guild_id" in sql
        return sorted(self.rows, key=lambda r: (r["guild_id"], r["added_at"], r["yt_channel_id"]))

    async def execute(self, sql, guild_id, channel_id, seen, title):
        assert sql.startswith("UPDATE youtube_follows")
        for row in self.rows:
            if row["guild_id"] == guild_id and row["yt_channel_id"] == channel_id:
                row.update(seen=list(seen), baselined=True, title=title)
                return "UPDATE 1"
        return "UPDATE 0"


class FakeSession:
    closed = False

    def __init__(self, feeds):
        self.feeds = feeds  # channel id -> (status, body)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(params["channel_id"])
        status, body = self.feeds[params["channel_id"]]

        class _Content:
            async def iter_chunked(self, n):
                yield body.encode()

        class _Resp:
            pass

        resp = _Resp()
        resp.status, resp.content = status, _Content()

        class _CM:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False

        return _CM()


class FakeChannel:
    def __init__(self):
        self.sent = []

    def permissions_for(self, member):
        return types.SimpleNamespace(send_messages=True)

    async def send(self, text, allowed_mentions=None):
        self.sent.append((text, allowed_mentions))


class FakeGuild:
    def __init__(self, gid, role=None):
        self.id = gid
        self.me = object()
        self.channel = FakeChannel()
        self.role = role

    def get_channel(self, cid):
        return self.channel

    def get_role(self, rid):
        return self.role


def build(monkeypatch, pool, session, guilds, limit=3, config=None):
    bot = types.SimpleNamespace(
        db_pool=pool, http_session=session, get_guild=lambda gid: guilds.get(gid)
    )

    async def get_guild(pool, gid, key, default=None):
        return config

    async def locale(bot, guild):
        return "en"

    monkeypatch.setattr(youtube.settings, "get_guild", get_guild)
    monkeypatch.setattr(youtube, "resolve_guild_locale", locale)
    monkeypatch.setattr(
        premium, "resolve_guild_limits",
        lambda bot, gid: types.SimpleNamespace(max_youtube_follows=limit),
    )
    return YouTubeAlerts(bot)


def ok(*items):
    return (200, feed_xml(list(items)))


# -- the poller -----------------------------------------------------------------
async def test_first_poll_baselines_silently_and_a_restart_never_reposts(monkeypatch):
    pool, guild = FakePool(), FakeGuild(1)
    pool.add(1, UC_A, baselined=False)
    session = FakeSession({UC_A: ok(("v1", HOUR, False))})
    cog = build(monkeypatch, pool, session, {1: guild})
    await cog._tick(now=NOW, mono=0)
    assert guild.channel.sent == [] and pool.rows[0]["seen"] == ["v1"]

    # "Restart": a brand-new cog over the same persisted rows, same feed.
    cog = build(monkeypatch, pool, session, {1: guild})
    await cog._tick(now=NOW, mono=0)
    assert guild.channel.sent == []

    # A new upload posts exactly once, then never again, even after another restart.
    session.feeds[UC_A] = ok(("v2", HOUR, False), ("v1", 2 * HOUR, False))
    await cog._tick(now=NOW, mono=0)
    cog = build(monkeypatch, pool, session, {1: guild})
    await cog._tick(now=NOW, mono=0)
    assert len(guild.channel.sent) == 1
    assert "https://www.youtube.com/watch?v=v2" in guild.channel.sent[0][0]


async def test_one_fetch_per_channel_is_shared_by_every_following_guild(monkeypatch):
    pool, g1, g2 = FakePool(), FakeGuild(1), FakeGuild(2)
    pool.add(1, UC_A, seen=["v1"])
    pool.add(2, UC_A, seen=["v1"])
    session = FakeSession({UC_A: ok(("v2", HOUR, False), ("v1", 2 * HOUR, False))})
    cog = build(monkeypatch, pool, session, {1: g1, 2: g2})
    await cog._tick(now=NOW, mono=0)
    assert session.calls == [UC_A]
    assert len(g1.channel.sent) == len(g2.channel.sent) == 1


async def test_follows_beyond_the_limit_stay_stored_but_are_not_polled(monkeypatch):
    pool, guild = FakePool(), FakeGuild(1)
    for uc in (UC_A, UC_B, UC_C, UC_D):
        pool.add(1, uc, seen=["v1"])
    feeds = {uc: ok(("v2", HOUR, False), ("v1", 2 * HOUR, False)) for uc in (UC_A, UC_B, UC_C, UC_D)}
    session = FakeSession(feeds)
    cog = build(monkeypatch, pool, session, {1: guild}, limit=3)
    await cog._tick(now=NOW, mono=0)
    assert sorted(session.calls) == [UC_A, UC_B, UC_C]
    assert len(guild.channel.sent) == 3
    assert len(pool.rows) == 4 and pool.rows[3]["seen"] == ["v1"]  # untouched
    # Renewal: the paused follow resumes from its stored cursor.
    cog = build(monkeypatch, pool, session, {1: guild}, limit=25)
    await cog._tick(now=NOW, mono=0)
    assert len(guild.channel.sent) == 4


async def test_a_failing_feed_backs_off_and_one_failure_blocks_nobody(monkeypatch):
    pool, guild = FakePool(), FakeGuild(1)
    pool.add(1, UC_A, seen=["v1"])
    pool.add(1, UC_B, seen=["v1"])
    session = FakeSession({UC_A: (500, ""), UC_B: ok(("v2", HOUR, False), ("v1", 2 * HOUR, False))})
    cog = build(monkeypatch, pool, session, {1: guild})
    await cog._tick(now=NOW, mono=0)
    assert len(guild.channel.sent) == 1
    await cog._tick(now=NOW, mono=youtube.POLL_SECONDS - 1)
    assert session.calls.count(UC_A) == 1  # still backing off
    await cog._tick(now=NOW, mono=youtube.POLL_SECONDS + 1)
    assert session.calls.count(UC_A) == 2


async def test_only_the_configured_role_can_be_pinged(monkeypatch):
    pool = FakePool()
    pool.add(1, UC_A, seen=["v1"])
    role = types.SimpleNamespace(id=77, mention="<@&77>")
    guild = FakeGuild(1, role=role)
    session = FakeSession({UC_A: ok(("v2", HOUR, False), ("v1", 2 * HOUR, False))})
    cog = build(monkeypatch, pool, session, {1: guild}, config={"role_id": "77", "message": "{title} {url}"})
    await cog._tick(now=NOW, mono=0)
    text, mentions = guild.channel.sent[0]
    assert text.startswith("<@&77> Title v2 https://")
    assert mentions.roles == [role] and not mentions.everyone and not mentions.users


async def test_a_row_removed_during_the_fetch_posts_nothing(monkeypatch):
    pool, guild = FakePool(), FakeGuild(1)
    pool.add(1, UC_A, seen=["v1"])
    session = FakeSession({UC_A: ok(("v2", HOUR, False), ("v1", 2 * HOUR, False))})
    cog = build(monkeypatch, pool, session, {1: guild})
    real_fetch = pool.fetch

    async def fetch_then_unfollow(sql, *args):
        rows = list(await real_fetch(sql, *args))
        pool.rows.clear()
        return rows

    pool.fetch = fetch_then_unfollow
    await cog._tick(now=NOW, mono=0)
    assert guild.channel.sent == []


# -- add_follow ------------------------------------------------------------------
class _Conn:
    def __init__(self, store):
        self.store = store

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

        return _T()

    async def execute(self, sql, *args):
        if sql.startswith("INSERT"):
            self.store.append(dict(zip(("g", "c", "post", "title", "baselined", "seen"), args)))
        elif sql.startswith("UPDATE"):
            for row in self.store:
                if row["g"] == args[0] and row["c"] == args[1]:
                    row["post"] = args[2]

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return sum(1 for r in self.store if r["g"] == args[0])
        return 1 if any(r["g"] == args[0] and r["c"] == args[1] for r in self.store) else None


class _AcquirePool:
    def __init__(self):
        self.store = []

    def acquire(self):
        conn = _Conn(self.store)

        class _A:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False

        return _A()


async def test_add_follow_baselines_with_the_feed_and_enforces_the_limit():
    pool = _AcquirePool()
    feed = yt.parse_feed(feed_xml([("v1", HOUR, False), ("v2", 2 * HOUR, False)], title="Chan"))
    assert await youtube.add_follow(pool, 1, UC_A, 9, 2, feed) == "new"
    assert pool.store[0]["baselined"] is True and pool.store[0]["seen"] == ["v1", "v2"]
    assert await youtube.add_follow(pool, 1, UC_B, 9, 2, None) == "new"
    assert pool.store[1]["baselined"] is False  # fetch failed: first poll baselines
    assert await youtube.add_follow(pool, 1, UC_C, 9, 2, None) == "limit"
    assert await youtube.add_follow(pool, 1, UC_A, 10, 2, feed) == "updated"
    assert pool.store[0]["post"] == 10 and len(pool.store) == 2
    assert await youtube.add_follow(pool, 2, UC_C, 9, 2, None) == "new"  # other server


# -- review fixes ------------------------------------------------------------------
class _PageSession:
    """Serves one channel page in 16 KB chunks and counts what was read."""

    def __init__(self, body, status=200):
        self.body, self.status, self.read = body, status, 0

    def get(self, url, **kwargs):
        outer = self

        class _Content:
            async def iter_chunked(self, n):
                for i in range(0, len(outer.body), n):
                    outer.read += min(n, len(outer.body) - i)
                    yield outer.body[i : i + n]

        resp = types.SimpleNamespace(status=self.status, content=_Content())

        class _CM:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False

        return _CM()


def _big_page(filler, tail=2_000_000):
    link = ('<link rel="canonical" href="https://www.youtube.com/channel/%s">' % UC_A).encode()
    return b"<html><head>" + b"x" * filler + link + b"y" * tail


async def test_a_canonical_link_after_800kb_is_found_and_the_read_stops_there():
    session = _PageSession(_big_page(825_000))
    assert await yt.resolve_path(session, "/@handle") == UC_A
    assert session.read < 900_000  # stopped at the match, not at the 2.8 MB end


async def test_a_link_split_across_two_chunks_is_still_found():
    # 16384 boundary falls inside the link tag.
    session = _PageSession(_big_page(16384 - 30, tail=10))
    assert await yt.resolve_path(session, "/@handle") == UC_A


async def test_a_page_without_the_link_gives_none_within_the_cap():
    session = _PageSession(b"z" * (yt.MAX_PAGE_BYTES + 100_000))
    assert await yt.resolve_path(session, "/@handle") is None
    assert session.read <= yt.MAX_PAGE_BYTES + 16384


def test_non_latin_handles_are_accepted():
    assert yt.parse_channel_input("@Привет_мир") == ("path", "/@Привет_мир")
    assert yt.parse_channel_input("https://www.youtube.com/@%E3%83%86%E3%82%B9%E3%83%88")[0] == "path"
    assert yt.parse_channel_input("@a b") is None


async def test_a_single_404_backs_off_normally_and_only_three_in_a_row_mean_gone(monkeypatch):
    pool, guild = FakePool(), FakeGuild(1)
    pool.add(1, UC_A, seen=["v1"])
    session = FakeSession({UC_A: (404, "")})
    cog = build(monkeypatch, pool, session, {1: guild})
    await cog._tick(now=NOW, mono=0)
    assert cog._backoff[UC_A][1] == youtube.POLL_SECONDS  # normal first step, not 24 h
    t = youtube.POLL_SECONDS
    await cog._tick(now=NOW, mono=t)
    t += 2 * youtube.POLL_SECONDS
    await cog._tick(now=NOW, mono=t)
    assert cog._backoff[UC_A][2] == 3
    assert cog._backoff[UC_A][1] == t + youtube.GONE_BACKOFF
    # A 200 in between resets the run.
    session.feeds[UC_A] = ok(("v1", HOUR, False))
    await cog._tick(now=NOW, mono=t + youtube.GONE_BACKOFF)
    assert UC_A not in cog._backoff


async def test_add_time_resolution_retries_once_before_not_found(monkeypatch):
    calls = []

    async def flaky(session, channel_id):
        calls.append(channel_id)
        if len(calls) == 1:
            raise yt.FeedGone(channel_id)
        return yt.parse_feed(feed_xml([("v1", HOUR, False)]))

    monkeypatch.setattr(youtube, "fetch_feed", flaky)
    channel_id, feed = await youtube.resolve_follow(None, ("id", UC_A))
    assert channel_id == UC_A and feed is not None and len(calls) == 2

    async def always_gone(session, channel_id):
        raise yt.FeedGone(channel_id)

    monkeypatch.setattr(youtube, "fetch_feed", always_gone)
    assert await youtube.resolve_follow(None, ("id", UC_A)) == (None, None)


def test_template_is_filled_in_one_pass_and_names_are_escaped():
    entry = yt.parse_feed(feed_xml([("v1", HOUR, False)])).entries[0]
    entry = entry.__class__(entry.video_id, "{url} *bold* {channel}", entry.url, entry.published, False)
    text = youtube.render_template("{channel}: {title} {url}", "Chan_1", entry)
    assert text == "Chan\\_1: {url} \\*bold\\* {channel} " + entry.url


class _Ctx:
    def __init__(self, guild_id=1):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.sent = []

    async def defer(self):
        pass

    async def send(self, text=None, **kwargs):
        self.sent.append(text)


async def test_add_has_a_per_guild_cooldown(monkeypatch):
    cog = build(monkeypatch, FakePool(), FakeSession({}), {})
    cog._add_cooldown.touch(1)
    ctx = _Ctx()
    await youtube.YouTubeAlerts.youtube_add.callback(cog, ctx, UC_A, object())
    assert len(ctx.sent) == 1 and "Slow down" in ctx.sent[0]


class _RemovePool:
    def __init__(self, titles):
        self.titles = titles  # title casefold -> channel id
        self.deleted = []

    async def fetchval(self, sql, guild_id, needle):
        return self.titles.get(needle)

    async def fetchrow(self, sql, guild_id, channel_id):
        if channel_id in self.titles.values() or channel_id == UC_B:
            self.deleted.append(channel_id)
            return 1
        return None


async def test_remove_accepts_id_url_title_and_handle(monkeypatch):
    pool = _RemovePool({"example": UC_A})
    cog = build(monkeypatch, pool, FakeSession({}), {})
    cog.bot.http_session = object()

    async def fake_resolve(session, path):
        return UC_B

    monkeypatch.setattr(youtube, "resolve_path", fake_resolve)
    monkeypatch.setattr(youtube, "get_session", lambda bot: None)
    for text, expected in (
        (UC_A, UC_A),
        ("https://www.youtube.com/channel/" + UC_A, UC_A),
        ("Example", UC_A),
        ("@Example", UC_A),
        ("@NotStoredTitle", UC_B),  # not a title: resolved through the page
    ):
        pool.deleted.clear()
        ctx = _Ctx()
        await youtube.YouTubeAlerts.youtube_remove.callback(cog, ctx, channel=text)
        assert pool.deleted == [expected], text
        assert ctx.sent == ["Unfollowed."]
