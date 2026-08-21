"""The starboard may never widen a message's audience.

The bug: ``handle`` re-posted ANY message that collected enough stars into the
starboard channel, without ever asking who was allowed to read the source. Staff
talk in a restricted channel, one person stars it, and the whole server reads it
in the starboard.

THE RULE these tests pin (``_may_republish``):

* @everyone can view the source channel -> allowed (the ordinary public case);
* otherwise, allowed only if every ROLE that can view the STARBOARD channel can
  also view the SOURCE channel - so a members-only server, where nothing is
  @everyone-visible, keeps a working starboard without the starboard ever
  showing anyone something they could not already read;
* never for a PRIVATE thread, whatever its parent allows;
* never when the source is age-restricted and the starboard channel is not;
* anything unevaluable answers NO.

No Discord, no DB: channels answer ``permissions_for`` from a set of role ids.

Typography rule: ASCII '-' and '...' only.
"""

import types

from cogs.config.starboard import STAR, Starboard, _may_republish


class _Role:
    def __init__(self, rid):
        self.id = rid


class _Channel:
    """A channel whose ``view_channel`` answer is a set of role ids."""

    def __init__(self, cid, viewers, *, guild=None, nsfw=False, private=False):
        self.id = cid
        self.viewers = set(viewers)
        self.guild = guild
        self._nsfw = nsfw
        self._private = private
        self.fetches = 0
        self.sent = []

    def permissions_for(self, obj):
        return types.SimpleNamespace(view_channel=obj.id in self.viewers)

    def is_nsfw(self):
        return self._nsfw

    def is_private(self):
        return self._private

    def get_partial_message(self, message_id):  # pragma: no cover - never reached
        raise AssertionError("a hidden channel must never reach the star post")

    async def send(self, **kwargs):  # pragma: no cover - the whole point
        raise AssertionError("a hidden channel must never be republished")

    async def fetch_message(self, message_id):
        self.fetches += 1
        raise AssertionError("a hidden channel must never even be read")


class _Guild:
    def __init__(self, gid=42):
        self.id = gid
        # Discord gives @everyone the guild's own id.
        self.default_role = _Role(gid)
        self.roles = [self.default_role]
        self.channels = {}

    def add_role(self, rid):
        role = _Role(rid)
        self.roles.append(role)
        return role

    def add_channel(self, channel):
        channel.guild = self
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_channel_or_thread(self, cid):
        return self.channels.get(cid)


def _public_pair():
    """A public source and a public starboard in the same guild."""
    guild = _Guild()
    everyone = guild.default_role.id
    src = guild.add_channel(_Channel(3, [everyone]))
    star = guild.add_channel(_Channel(9, [everyone]))
    return guild, src, star


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------
def test_a_public_channel_is_republished():
    _guild, src, star = _public_pair()
    assert _may_republish(src, star) is True


def test_a_staff_only_channel_is_never_republished():
    """THE finding: a channel @everyone cannot read, starred into a public
    starboard, used to publish the staff conversation to the whole server."""
    guild = _Guild()
    staff = guild.add_role(77)
    src = guild.add_channel(_Channel(3, [staff.id]))  # @everyone denied
    star = guild.add_channel(_Channel(9, [guild.default_role.id, staff.id]))

    assert _may_republish(src, star) is False


# ---------------------------------------------------------------------------
# ... without breaking a server that hides everything behind a verified role
# ---------------------------------------------------------------------------
def test_a_members_only_server_still_has_a_working_starboard():
    """Nothing is @everyone-visible here. The starboard's audience (the Members
    role) can already read the source, so republishing widens nothing."""
    guild = _Guild()
    members = guild.add_role(77)
    src = guild.add_channel(_Channel(3, [members.id]))
    star = guild.add_channel(_Channel(9, [members.id]))

    assert _may_republish(src, star) is True


def test_a_starboard_audience_wider_than_the_source_is_refused():
    """Same server, but the starboard is also open to a Visitors role that the
    source channel keeps out: one of its readers would learn something new."""
    guild = _Guild()
    members = guild.add_role(77)
    visitors = guild.add_role(88)
    src = guild.add_channel(_Channel(3, [members.id]))
    star = guild.add_channel(_Channel(9, [members.id, visitors.id]))

    assert _may_republish(src, star) is False


def test_a_starboard_no_role_can_view_is_refused():
    """Reachable only through per-member overwrites: unverifiable, so no."""
    guild = _Guild()
    members = guild.add_role(77)
    src = guild.add_channel(_Channel(3, [members.id]))
    star = guild.add_channel(_Channel(9, []))

    assert _may_republish(src, star) is False


# ---------------------------------------------------------------------------
# Threads and the age gate
# ---------------------------------------------------------------------------
def test_a_private_thread_is_never_republished():
    """``Thread.permissions_for`` answers for the PARENT, so a private thread in
    a public channel reads as public - it is invite-only all the same."""
    guild = _Guild()
    everyone = guild.default_role.id
    thread = guild.add_channel(_Channel(3, [everyone], private=True))
    star = guild.add_channel(_Channel(9, [everyone]))

    assert _may_republish(thread, star) is False


def test_an_age_restricted_source_is_not_published_into_a_normal_channel():
    guild = _Guild()
    everyone = guild.default_role.id
    src = guild.add_channel(_Channel(3, [everyone], nsfw=True))
    star = guild.add_channel(_Channel(9, [everyone]))

    assert _may_republish(src, star) is False


def test_an_age_restricted_source_may_reach_an_age_restricted_starboard():
    guild = _Guild()
    everyone = guild.default_role.id
    src = guild.add_channel(_Channel(3, [everyone], nsfw=True))
    star = guild.add_channel(_Channel(9, [everyone], nsfw=True))

    assert _may_republish(src, star) is True


# ---------------------------------------------------------------------------
# Unknown answers are refusals
# ---------------------------------------------------------------------------
def test_an_unevaluable_channel_is_refused():
    guild = _Guild()
    star = guild.add_channel(_Channel(9, [guild.default_role.id]))

    class _Broken(_Channel):
        def permissions_for(self, obj):
            raise RuntimeError("partial channel")

    broken = guild.add_channel(_Broken(3, []))

    assert _may_republish(broken, star) is False
    assert _may_republish(None, star) is False
    assert _may_republish(star, None) is False


def test_a_channel_with_no_guild_is_refused():
    orphan = _Channel(3, [1], guild=None)
    assert _may_republish(orphan, orphan) is False


# ---------------------------------------------------------------------------
# End to end: the listener spends nothing and posts nothing on a hidden channel
# ---------------------------------------------------------------------------
class _Pool:
    def __init__(self, row=None):
        self.calls = []
        self.row = row

    async def fetchrow(self, query, *args):
        self.calls.append(query)
        return self.row

    async def execute(self, query, *args):
        self.calls.append(query)


async def _star(cog, guild, message_id=100, channel_id=3):
    await cog.handle(
        types.SimpleNamespace(
            emoji=STAR, message_id=message_id, channel_id=channel_id,
            guild_id=guild.id,
        )
    )


def _cog(guild, pool):
    bot = types.SimpleNamespace(
        db_pool=pool,
        cached_messages=[],
        get_guild=lambda gid: guild if gid == guild.id else None,
    )
    cog = Starboard(bot)
    cog._config[guild.id] = (9, 3)
    return cog


async def test_starring_a_hidden_channel_reads_no_message_and_posts_nothing():
    """The gate sits BEFORE the message is read, so a starred private message
    costs no Discord request either - the fakes assert by raising.

    It does ask the table one question (is an old post up for this message?),
    and with nothing there that is the end of it.
    """
    guild = _Guild()
    staff = guild.add_role(77)
    src = guild.add_channel(_Channel(3, [staff.id]))
    guild.add_channel(_Channel(9, [guild.default_role.id]))

    pool = _Pool()
    cog = _cog(guild, pool)

    await _star(cog, guild)

    assert src.fetches == 0
    assert [c for c in pool.calls if "DELETE" in c or "INSERT" in c] == []
    assert len(pool.calls) == 1 and "SELECT star_message_id" in pool.calls[0]
    assert cog._locks == {}


async def test_a_source_that_turns_private_loses_its_star_post():
    """The residual of the gate: it is asked live, so its verdict can flip AFTER
    a post exists. Refusing to publish is only half of it - the copy already on
    the public board has to come down, and nothing else would ever take it down
    (every later add and remove stops at the same gate).

    Delete the retraction call from ``handle`` and this fails: the post stays.
    """
    guild = _Guild()
    staff = guild.add_role(77)
    # Was public when it was starred; locked down afterwards.
    src = guild.add_channel(_Channel(3, [staff.id]))
    star_ch = guild.add_channel(_Channel(9, [guild.default_role.id]))

    deleted = []

    class _Partial:
        def __init__(self, mid):
            self.id = mid

        async def delete(self):
            deleted.append(self.id)

    star_ch.get_partial_message = _Partial

    pool = _Pool(row={"star_message_id": 555})
    cog = _cog(guild, pool)

    await _star(cog, guild)

    assert deleted == [555]
    assert any("DELETE FROM starboard_entries" in c for c in pool.calls)
    assert src.fetches == 0  # still never reads the private message
    assert star_ch.sent == []  # and never publishes anything
    assert cog._locks == {}
