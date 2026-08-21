"""What one star reaction is allowed to cost.

Two problems, one hot listener (``on_raw_reaction_add`` /
``on_raw_reaction_remove`` fire for EVERY reaction in EVERY guild):

1. every star did an uncached ``fetch_message`` on the source channel, and an
   entry already on the starboard did a second ``fetch_message`` on the star
   post just to edit it. At 1000+ guilds that is 2..3 REST calls per reaction;
2. crossing back under the threshold DELETED the star post and the next star
   re-SENT it, so one member toggling their star drove a delete/send loop in the
   starboard channel.

The budget these tests pin, per star reaction:

* source message: 0 REST on a cache hit, exactly 1 ``fetch_message`` on a miss;
* star post: 0 REST always - it is addressed by id (``get_partial_message``);
* the write: at most 1 REST, and 0 when the displayed count did not change;
* below the threshold with nothing posted: 0 REST.

The fakes assert by RAISING: any ``fetch_message`` on the starboard channel
fails the test outright.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import types

import discord

from cogs.config.starboard import STAR, Starboard, _keep_floor

UTC = datetime.timezone.utc


class _Response:
    status = 404
    reason = "Not Found"


def _not_found():
    return discord.NotFound(_Response(), "unknown message")


class _Role:
    def __init__(self, rid):
        self.id = rid


class _Guild:
    def __init__(self, gid=42):
        self.id = gid
        self.default_role = _Role(gid)
        self.roles = [self.default_role]
        self.channels = {}

    def add_channel(self, channel):
        channel.guild = self
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_channel_or_thread(self, cid):
        return self.channels.get(cid)


class _PublicChannel:
    """Everything here is @everyone-visible: visibility is tested elsewhere."""

    def __init__(self, cid):
        self.id = cid
        self.guild = None

    def permissions_for(self, obj):
        return types.SimpleNamespace(view_channel=True)

    def is_nsfw(self):
        return False


class _Source(_PublicChannel):
    def __init__(self, message=None, cid=3):
        super().__init__(cid)
        self.message = message
        self.fetches = 0

    async def fetch_message(self, message_id):
        self.fetches += 1
        if self.message is None:
            raise _not_found()
        return self.message


class _Partial:
    def __init__(self, channel, message_id):
        self.channel = channel
        self.id = message_id

    async def edit(self, **kwargs):
        if self.channel.edit_raises is not None:
            raise self.channel.edit_raises
        self.channel.edits.append(self.id)

    async def delete(self):
        if self.channel.delete_raises is not None:
            raise self.channel.delete_raises
        self.channel.deletes.append(self.id)


class _StarChannel(_PublicChannel):
    def __init__(self, cid=9):
        super().__init__(cid)
        self.sent = []
        self.edits = []
        self.deletes = []
        self.edit_raises = None
        self.delete_raises = None
        self.fetches = 0
        self._next_id = 500

    def get_partial_message(self, message_id):
        return _Partial(self, message_id)

    async def send(self, **kwargs):
        self._next_id += 1
        self.sent.append(self._next_id)
        return types.SimpleNamespace(id=self._next_id, delete=self._delete_sent)

    async def _delete_sent(self):  # pragma: no cover - rollback path only
        self.deletes.append(self._next_id)

    async def fetch_message(self, message_id):
        # COUNTED as well as raised: the cog catches broad exceptions around its
        # edit path, so a bare raise here could be swallowed and read as a pass.
        self.fetches += 1
        raise AssertionError(
            "the starboard post must be addressed by id, never fetched"
        )

    @property
    def rest_calls(self):
        return self.fetches + len(self.sent) + len(self.edits) + len(self.deletes)


class _Pool:
    """The starboard_entries row, with just enough SQL understanding to evolve."""

    def __init__(self, row=None):
        self.row = row
        self.executed = []

    async def fetchrow(self, query, *args):
        return self.row

    async def execute(self, query, *args):
        flat = " ".join(query.split())
        self.executed.append(flat)
        if flat.startswith("DELETE"):
            self.row = None
        elif "INSERT INTO starboard_entries" in flat:
            self.row = {"star_message_id": args[2], "star_count": args[4]}
        elif "SET star_message_id" in flat:
            self.row = {"star_message_id": args[1], "star_count": args[2]}
        elif "SET star_count" in flat:
            self.row = dict(self.row or {})
            self.row["star_count"] = args[1]


def _message(message_id=100, stars=0):
    reactions = []
    if stars:
        reactions.append(types.SimpleNamespace(emoji=STAR, count=stars))
    return types.SimpleNamespace(
        id=message_id,
        content="a message worth starring",
        reactions=reactions,
        author=types.SimpleNamespace(
            display_name="Vera",
            display_avatar=types.SimpleNamespace(url="https://cdn.test/a.png"),
        ),
        attachments=[],
        jump_url="https://discord.com/channels/42/3/100",
        created_at=datetime.datetime.now(UTC),
    )


def _setup(*, stars, threshold=3, row=None, cached=True):
    """A cog, a guild, and one starred message either cached or not."""
    guild = _Guild()
    msg = _message(stars=stars)
    src = guild.add_channel(_Source(msg if not cached else None))
    star_ch = guild.add_channel(_StarChannel())
    pool = _Pool(row)
    bot = types.SimpleNamespace(
        db_pool=pool,
        cached_messages=[_message(999), msg] if cached else [],
        get_guild=lambda gid: guild if gid == guild.id else None,
    )
    cog = Starboard(bot)
    cog._config[guild.id] = (star_ch.id, threshold)
    return cog, src, star_ch, pool, msg


def _payload(message_id=100, channel_id=3, guild_id=42):
    return types.SimpleNamespace(
        emoji=STAR, message_id=message_id, channel_id=channel_id, guild_id=guild_id
    )


# ---------------------------------------------------------------------------
# The source message: cache first, fetch only on a miss
# ---------------------------------------------------------------------------
async def test_a_cached_message_costs_no_fetch():
    cog, src, star_ch, _pool, _msg = _setup(stars=3)

    await cog.on_raw_reaction_add(_payload())

    assert src.fetches == 0
    assert len(star_ch.sent) == 1


async def test_a_cache_miss_still_fetches_exactly_once():
    cog, src, star_ch, _pool, _msg = _setup(stars=3, cached=False)

    await cog.on_raw_reaction_add(_payload())

    assert src.fetches == 1
    assert len(star_ch.sent) == 1


async def test_a_star_below_the_threshold_spends_nothing():
    cog, src, star_ch, _pool, _msg = _setup(stars=1)

    await cog.on_raw_reaction_add(_payload())

    assert src.fetches == 0
    assert star_ch.rest_calls == 0


# ---------------------------------------------------------------------------
# The star post: addressed by id, never fetched
# ---------------------------------------------------------------------------
async def test_updating_a_star_post_is_one_request():
    cog, _src, star_ch, pool, _msg = _setup(
        stars=4, row={"star_message_id": 555, "star_count": 3}
    )

    await cog.on_raw_reaction_add(_payload())

    # _StarChannel.fetch_message raises AND counts: reaching it fails the test.
    assert star_ch.fetches == 0
    assert star_ch.edits == [555]
    assert star_ch.rest_calls == 1
    assert pool.row["star_count"] == 4


async def test_a_repeat_event_with_an_unchanged_count_spends_nothing():
    """A gateway resume re-delivers reactions, and a second star from someone
    who already starred it changes no pixel: neither may cost a request."""
    cog, _src, star_ch, pool, _msg = _setup(
        stars=4, row={"star_message_id": 555, "star_count": 4}
    )

    await cog.on_raw_reaction_add(_payload())

    assert star_ch.rest_calls == 0
    assert pool.executed == []


async def test_a_deleted_star_post_is_reposted_once():
    cog, _src, star_ch, pool, _msg = _setup(
        stars=4, row={"star_message_id": 555, "star_count": 3}
    )
    star_ch.edit_raises = _not_found()

    await cog.on_raw_reaction_add(_payload())

    assert len(star_ch.sent) == 1
    assert pool.row["star_message_id"] == star_ch.sent[0]


# ---------------------------------------------------------------------------
# Hysteresis: crossing back down edits, it does not delete and repost
# ---------------------------------------------------------------------------
def test_the_floor_sits_one_star_under_the_threshold():
    assert _keep_floor(3) == 2
    # ... but never under 1: zero stars always leaves the starboard.
    assert _keep_floor(1) == 1


async def test_falling_one_under_the_threshold_edits_instead_of_deleting():
    cog, _src, star_ch, pool, _msg = _setup(
        stars=2, threshold=3, row={"star_message_id": 555, "star_count": 3}
    )

    await cog.on_raw_reaction_remove(_payload())

    assert star_ch.deletes == []
    assert star_ch.edits == [555]
    assert pool.row["star_message_id"] == 555  # the row survived
    assert not any(entry.startswith("DELETE") for entry in pool.executed)


async def test_toggling_around_the_threshold_never_reposts():
    """One member starring and unstarring, four times. The old code deleted and
    re-sent the post on every crossing; the post must now simply be edited."""
    cog, _src, star_ch, pool, msg = _setup(
        stars=3, threshold=3, row=None
    )

    star = msg.reactions[0]
    for count in (3, 2, 3, 2, 3, 2):
        star.count = count
        await cog.on_raw_reaction_add(_payload())

    assert len(star_ch.sent) == 1  # posted once, at the first crossing
    assert star_ch.deletes == []
    assert len(star_ch.edits) == 5  # every later crossing is an edit
    assert pool.row["star_count"] == 2


async def test_a_message_nobody_stars_any_more_does_leave():
    """Hysteresis is a band, not an amnesty: under the floor the post goes."""
    cog, _src, star_ch, pool, _msg = _setup(
        stars=0, threshold=3, row={"star_message_id": 555, "star_count": 2}
    )

    await cog.on_raw_reaction_remove(_payload())

    assert star_ch.deletes == [555]
    assert star_ch.edits == []
    assert pool.row is None


async def test_at_threshold_one_the_last_star_removes_the_post():
    cog, _src, star_ch, pool, _msg = _setup(
        stars=0, threshold=1, row={"star_message_id": 555, "star_count": 1}
    )

    await cog.on_raw_reaction_remove(_payload())

    assert star_ch.deletes == [555]
    assert pool.row is None


async def test_a_star_post_already_gone_still_clears_its_row():
    cog, _src, star_ch, pool, _msg = _setup(
        stars=0, threshold=3, row={"star_message_id": 555, "star_count": 2}
    )
    star_ch.delete_raises = _not_found()

    await cog.on_raw_reaction_remove(_payload())

    assert star_ch.deletes == []
    assert pool.row is None
