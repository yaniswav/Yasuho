"""Delivery-layer integration tests for AniList feed card coalescing (LOT AF2).

The pure fold decision is covered by tests/tools/test_anilist_feed_coalesce.py;
here we pin how :class:`AniListFeed` wires it into real delivery:

* an EDIT silently edits the STORED message and rebuilds the card with the
  NEWEST activity id (so the Like/Reply/Add buttons act on it),
* a POST_NEW (first save) upserts the coalescing record,
* a 404 on the edit (card deleted in that channel) falls back to a fresh post
  for THAT channel only, overwriting the stale record - it does not raise,
* the digest (busy-tick remainder) path is left untouched and writes no record,
* the record is read from the DB, so an edit resumes after a restart.

The cog is built with ``__new__`` (no bot/db/Discord) and fed hand-rolled fakes
for the pool, channel and messages; nothing here touches the network or a DB.
"""

from datetime import datetime, timedelta, timezone

import discord

from cogs.anilist.feed import AniListFeed
from cogs.anilist.feed_render import ActivityDigest
from tools import anilist_feed_coalesce as afc


def _now():
    return datetime.now(timezone.utc)


# --- Discord/DB fakes -------------------------------------------------------


class _Resp:
    status = 404
    reason = "Not Found"


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class _FakePartialMessage:
    def __init__(self, channel, message_id):
        self._channel = channel
        self.id = message_id

    async def edit(self, **kwargs):
        self._channel.edits.append((self.id, kwargs))
        if self.id in self._channel.gone:
            raise discord.NotFound(_Resp(), "gone")
        return _FakeMessage(self.id)


class _FakeChannel:
    def __init__(self, channel_id=100, *, gone=()):
        self.id = channel_id
        # resolve_guild_locale returns the default locale for a None guild, so
        # _deliver_channel needs no locale DB access in these tests.
        self.guild = None
        self.sends = []
        self.edits = []
        self.gone = set(gone)
        self._next_message_id = 5000

    def is_nsfw(self):
        return False

    async def send(self, **kwargs):
        self._next_message_id += 1
        self.sends.append((self._next_message_id, kwargs))
        return _FakeMessage(self._next_message_id)

    def get_partial_message(self, message_id):
        return _FakePartialMessage(self, message_id)


class _FakePool:
    """asyncpg-pool stand-in: records every execute and serves one canned row."""

    def __init__(self, row=None):
        self._row = row
        self.fetchrow_calls = []
        self.executes = []

    def set_row(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self._row

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "OK"


class _FakeBot:
    def __init__(self, pool, channel):
        self.db_pool = pool
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


def _cog(pool, channel):
    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = _FakeBot(pool, channel)
    return cog


# --- Activity / record builders ---------------------------------------------


def _list_activity(
    activity_id=999, *, progress="54", status="read chapter", user_id=7, media_id=42
):
    return {
        "id": activity_id,
        "kind": "ListActivity",
        "type": "MANGA_LIST",
        "user_id": user_id,
        "user_name": "Reader",
        "user_url": "https://anilist.co/user/Reader",
        "status": status,
        "progress": progress,
        "media": {"id": media_id, "title": {"romaji": "Berserk"}, "coverImage": {}},
        "site_url": "https://anilist.co/activity/%d" % activity_id,
    }


def _text_activity(activity_id=111, user_id=7):
    return {
        "id": activity_id,
        "kind": "TextActivity",
        "type": "TEXT",
        "user_id": user_id,
        "user_name": "Reader",
        "text": "hello",
        "site_url": "https://anilist.co/activity/%d" % activity_id,
    }


def _db_row(
    *,
    message_id=777,
    status="read chapter",
    last_progress="50",
    created_age=timedelta(minutes=5),
    updated_age=timedelta(minutes=1),
):
    now = _now()
    return {
        "message_id": message_id,
        "status": status,
        "last_progress": last_progress,
        "created_at": now - created_age,
        "updated_at": now - updated_age,
    }


def _like_ids(view):
    """The activity ids carried by a rendered card's persistent Like buttons."""

    ids = []
    for child in view.walk_children():
        custom_id = getattr(child, "custom_id", None) or ""
        if custom_id.startswith("alf:like:"):
            ids.append(int(custom_id.rsplit(":", 1)[1]))
    return ids


def _sql_of(call):
    return call[0]


def _upserts(pool):
    return [c for c in pool.executes if "INSERT INTO anilist_feed_posts" in _sql_of(c)]


def _touches(pool):
    return [
        c for c in pool.executes if _sql_of(c).startswith("UPDATE anilist_feed_posts SET")
    ]


# --- POST_NEW ---------------------------------------------------------------


async def test_first_save_posts_fresh_and_upserts_record():
    pool = _FakePool(row=None)  # no live record for this slot
    channel = _FakeChannel(channel_id=100)
    cog = _cog(pool, channel)

    act = _list_activity(activity_id=999, progress="54", status="read chapter")
    await cog._deliver_card(guild_id=1, channel=channel, activity=act)

    # A fresh card was sent, nothing was edited.
    assert len(channel.sends) == 1
    assert channel.edits == []
    # The record was upserted with this activity's id / progress / status.
    ups = _upserts(pool)
    assert len(ups) == 1
    args = ups[0][1]
    # args: guild, channel, user, media, message_id, activity_id, progress, status
    assert args[0] == 1
    assert args[1] == 100
    assert args[2] == 7
    assert args[3] == 42
    assert args[4] == channel.sends[0][0]  # the sent message id
    assert args[5] == 999
    assert args[6] == "54"
    assert args[7] == "read chapter"
    assert _touches(pool) == []


async def test_text_activity_posts_fresh_and_is_never_recorded():
    pool = _FakePool(row=None)
    channel = _FakeChannel()
    cog = _cog(pool, channel)

    await cog._deliver_card(guild_id=1, channel=channel, activity=_text_activity())

    assert len(channel.sends) == 1
    # A text post never keys a coalescing record: no fetch, no upsert.
    assert pool.fetchrow_calls == []
    assert pool.executes == []


async def test_progressless_list_activity_is_never_recorded():
    pool = _FakePool(row=None)
    channel = _FakeChannel()
    cog = _cog(pool, channel)

    act = _list_activity(progress=None)
    await cog._deliver_card(guild_id=1, channel=channel, activity=act)

    assert len(channel.sends) == 1
    assert pool.fetchrow_calls == []
    assert pool.executes == []


# --- EDIT (the fold) --------------------------------------------------------


async def test_increment_edits_stored_message_with_new_activity_id():
    # A live record at ch.50 on message 777; the new save advances to ch.54.
    pool = _FakePool(row=_db_row(message_id=777, last_progress="50"))
    channel = _FakeChannel(channel_id=100)
    cog = _cog(pool, channel)

    act = _list_activity(activity_id=1234, progress="54", status="read chapter")
    await cog._deliver_card(guild_id=1, channel=channel, activity=act)

    # The stored message was edited in place; no fresh card was posted.
    assert channel.sends == []
    assert len(channel.edits) == 1
    edited_id, kwargs = channel.edits[0]
    assert edited_id == 777
    # The rebuilt card carries the NEWEST activity id, so its buttons act on it.
    assert _like_ids(kwargs["view"]) == [1234]
    # The record advanced (updated_at moves; created_at untouched) - no new upsert.
    assert _upserts(pool) == []
    touches = _touches(pool)
    assert len(touches) == 1
    targs = touches[0][1]
    # args: channel, user, media, activity_id, progress, status
    assert targs[:3] == (100, 7, 42)
    assert targs[3] == 1234
    assert targs[4] == "54"


async def test_status_change_posts_fresh_card_and_overwrites_record():
    # Live record says CURRENT/read chapter; a completed save must NOT fold.
    pool = _FakePool(row=_db_row(status="read chapter", last_progress="50"))
    channel = _FakeChannel()
    cog = _cog(pool, channel)

    act = _list_activity(activity_id=1500, progress="80", status="completed")
    await cog._deliver_card(guild_id=1, channel=channel, activity=act)

    assert len(channel.sends) == 1
    assert channel.edits == []
    assert len(_upserts(pool)) == 1  # a fresh record overwrites the slot


# --- Fan-out 404 fallback ---------------------------------------------------


async def test_edit_404_falls_back_to_fresh_post_in_that_channel():
    # The record points at message 777, but it was deleted in this channel.
    pool = _FakePool(row=_db_row(message_id=777, last_progress="50"))
    channel = _FakeChannel(channel_id=100, gone={777})
    cog = _cog(pool, channel)

    act = _list_activity(activity_id=2222, progress="54")
    # Must not raise - the 404 is swallowed and a fresh card is posted instead.
    await cog._deliver_card(guild_id=1, channel=channel, activity=act)

    assert len(channel.edits) == 1  # the edit was attempted...
    assert channel.edits[0][0] == 777
    assert len(channel.sends) == 1  # ...then a fresh card replaced it here
    # The stale record is overwritten (upsert), not merely touched.
    assert len(_upserts(pool)) == 1
    assert _touches(pool) == []
    assert _upserts(pool)[0][1][4] == channel.sends[0][0]  # new message id stored


# --- Digest untouched -------------------------------------------------------


async def test_digest_path_is_untouched_and_writes_no_record():
    # More activities than MAX_FULL_POSTS_PER_TICK (5): the first five get cards,
    # the remainder collapses into ONE digest that keys no coalescing record.
    pool = _FakePool(row=None)
    channel = _FakeChannel(channel_id=100)
    cog = _cog(pool, channel)

    feed = {"guild_id": 1, "channel_id": 100, "fail_count": 0}
    items = [
        _list_activity(activity_id=1000 + i, media_id=1000 + i, progress="1")
        for i in range(7)
    ]
    await cog._deliver_channel(feed, 100, items)

    # 5 full cards + 1 digest = 6 sends; the last send is the ActivityDigest.
    assert len(channel.sends) == 6
    assert isinstance(channel.sends[-1][1]["view"], ActivityDigest)
    # Exactly one record per full card; the two digest items are never recorded.
    ups = _upserts(pool)
    assert len(ups) == 5
    recorded_activity_ids = {c[1][5] for c in ups}
    assert recorded_activity_ids == {1000, 1001, 1002, 1003, 1004}


# --- Restart: record comes from the DB, not memory --------------------------


async def test_edit_resumes_from_db_record_after_restart():
    # A brand-new cog (as after a restart) holds no in-memory state. The record
    # is served purely by the DB; the edit must still target the stored message.
    pool = _FakePool(row=_db_row(message_id=888, last_progress="50"))
    channel = _FakeChannel(channel_id=100)
    cog = _cog(pool, channel)

    act = _list_activity(activity_id=3333, progress="60")
    await cog._deliver_card(guild_id=1, channel=channel, activity=act)

    # The decision was driven by the DB row (fetchrow was consulted for the slot).
    assert len(pool.fetchrow_calls) == 1
    assert pool.fetchrow_calls[0][1] == (100, 7, 42)
    # And the stored message id from the DB row was the one edited.
    assert [e[0] for e in channel.edits] == [888]
    assert channel.sends == []


# --- Prune sweep ------------------------------------------------------------


async def test_prune_uses_bounded_delete_past_age_cap_plus_grace():
    pool = _FakePool()
    channel = _FakeChannel()
    cog = _cog(pool, channel)

    await cog._prune_coalesce_posts()

    assert len(pool.executes) == 1
    sql, args = pool.executes[0]
    assert sql.startswith("DELETE FROM anilist_feed_posts")
    assert "LIMIT $2" in sql
    from cogs.anilist import feed as feed_mod

    assert args[0] == afc.AGE_CAP + afc.PRUNE_GRACE
    assert args[1] == feed_mod.COALESCE_PRUNE_BATCH
