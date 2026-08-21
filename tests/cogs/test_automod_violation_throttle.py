"""One member's violation BURST is one record and one announcement.

The bug: every AutoMod violation wrote a permanent ``cases`` row and posted a
mod-log embed, with no throttle at all. A member pasting 200 invites opened 200
cases and posted 200 embeds - the table grew without bound and the one channel
moderators actually read became unusable exactly when it mattered.

THE LINE these tests hold: the throttle covers the PAPERWORK only.

* throttled (one per member per ``_VIOLATION_LOG_WINDOW``): the case AutoMod
  opens on its own, the mod-log post, the short in-channel notice;
* NEVER throttled: the deletion of the offending message, the timeout, the kick,
  and a warn - which is recorded and counted on every single violation, because
  the warn row IS the punishment (it drives the escalation policy), not the
  paperwork.

No Discord, no DB, no clock: the cooldown module's ``time`` is swapped for a
counter the test moves by hand.

Typography rule: ASCII '-' and '...' only.
"""

import types

import pytest

from cogs.moderation import automod
from tools import cooldowns


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(cooldowns, "time", fake)
    return fake


class _Channel:
    def __init__(self):
        self.id = 5
        self.parent_id = None
        self.notices = []

    async def send(self, content, **kwargs):
        self.notices.append(content)


class _Member:
    def __init__(self, uid=7):
        self.id = uid
        self.bot = False
        self.mention = f"<@{uid}>"
        self.roles = []
        self.timeouts = 0

    async def timeout(self, duration, reason=None):
        self.timeouts += 1


class _Guild:
    def __init__(self, gid=42):
        self.id = gid
        self.me = types.SimpleNamespace(id=1, display_name="Yasuho")
        self.kicks = []

    async def kick(self, member, reason=None):
        self.kicks.append(member.id)


class _Message:
    _next_id = 2000

    def __init__(self, guild, channel, author, content="discord.gg/raid"):
        _Message._next_id += 1
        self.id = _Message._next_id
        self.guild = guild
        self.channel = channel
        self.author = author
        self.content = content
        self.deleted = 0

    async def delete(self):
        self.deleted += 1


class _Recorder:
    """Counts every write and every post AutoMod would make."""

    def __init__(self):
        self.cases = []
        self.warns = []
        self.posts = []
        self.escalations = []


def _cog(monkeypatch, *, action="mute"):
    rec = _Recorder()
    bot = types.SimpleNamespace(db_pool=object(), user=types.SimpleNamespace(id=1))
    cog = automod.AutoMod(bot)

    async def _get_guild(_pool, _guild_id, key, default=None):
        if key == "automod_action":
            return action
        return default

    async def _create_case(_pool, guild_id, user_id, _mod, act, reason):
        rec.cases.append((guild_id, user_id, act))
        return len(rec.cases)

    async def _record_warn(_pool, guild_id, user_id, _mod, reason):
        rec.warns.append((guild_id, user_id))
        return len(rec.warns), len(rec.warns)

    async def _load_policy(_pool, _guild_id):
        return {}, None

    async def _apply_escalation(_bot, _guild, member, rule):
        rec.escalations.append(member.id)

    async def _funnel(_bot, guild, embed):
        rec.posts.append((guild.id, embed))

    monkeypatch.setattr(automod.settings, "get_guild", _get_guild)
    monkeypatch.setattr(automod.modactions, "create_case", _create_case)
    monkeypatch.setattr(automod.modactions, "record_warn", _record_warn)
    monkeypatch.setattr(
        automod.modactions, "load_escalation_policy", _load_policy
    )
    monkeypatch.setattr(
        automod.modactions, "apply_escalation_action", _apply_escalation
    )
    monkeypatch.setattr(automod.modactions, "funnel_action", _funnel)
    monkeypatch.setattr(
        automod.modactions,
        "case_embed",
        lambda number, act, target, me, reason: ("case", number, act),
    )
    # Every count in this file is > 0, so the escalation rule always fires.
    monkeypatch.setattr(
        automod.warn_escalation, "action_for_count", lambda policy, count: "mute"
    )
    return cog, rec


async def _violate(cog, guild, channel, member, times=1):
    messages = []
    for _ in range(times):
        message = _Message(guild, channel, member)
        messages.append(message)
        await cog._handle_violation(
            message,
            kind="invite",
            notice="no invites here",
            reason="Posted a Discord invite link",
        )
    return messages


# ---------------------------------------------------------------------------
# The regression: 200 violations must not become 200 rows and 200 posts
# ---------------------------------------------------------------------------
async def test_a_burst_from_one_member_is_one_case_and_one_post(monkeypatch, clock):
    cog, rec = _cog(monkeypatch)
    guild, channel, member = _Guild(), _Channel(), _Member()

    messages = await _violate(cog, guild, channel, member, times=25)

    assert len(rec.cases) == 1
    assert len(rec.posts) == 1
    assert channel.notices == ["no invites here"]
    # ... and none of that softened the moderation itself:
    assert all(message.deleted == 1 for message in messages)
    assert member.timeouts == 25


async def test_the_deletion_and_the_kick_are_never_throttled(monkeypatch, clock):
    cog, rec = _cog(monkeypatch, action="kick")
    guild, channel, member = _Guild(), _Channel(), _Member()

    messages = await _violate(cog, guild, channel, member, times=10)

    assert guild.kicks == [member.id] * 10
    assert all(message.deleted == 1 for message in messages)
    assert len(rec.cases) == 1
    assert len(rec.posts) == 1


async def test_a_warn_is_recorded_every_time_because_it_is_the_punishment(
    monkeypatch, clock
):
    """The warn row feeds the escalation counter, so throttling it would throttle
    the punishment. Only its mod-log POST is coalesced."""
    cog, rec = _cog(monkeypatch, action="warn")
    guild, channel, member = _Guild(), _Channel(), _Member()

    await _violate(cog, guild, channel, member, times=8)

    assert len(rec.warns) == 8
    assert rec.escalations == [member.id] * 8
    assert len(rec.posts) == 1
    assert rec.cases == []  # a warn opens its case through record_warn


# ---------------------------------------------------------------------------
# ... while staying a per-member, per-window coalesce and nothing more
# ---------------------------------------------------------------------------
async def test_another_member_in_the_same_window_is_not_silenced(monkeypatch, clock):
    cog, rec = _cog(monkeypatch)
    guild, channel = _Guild(), _Channel()
    first, second = _Member(7), _Member(8)

    await _violate(cog, guild, channel, first, times=3)
    await _violate(cog, guild, channel, second, times=3)

    assert len(rec.cases) == 2
    assert {case[1] for case in rec.cases} == {7, 8}
    assert len(rec.posts) == 2


async def test_the_same_member_in_another_guild_is_not_silenced(monkeypatch, clock):
    cog, rec = _cog(monkeypatch)
    channel, member = _Channel(), _Member()

    await _violate(cog, _Guild(42), channel, member, times=2)
    await _violate(cog, _Guild(43), channel, member, times=2)

    assert {case[0] for case in rec.cases} == {42, 43}
    assert len(rec.posts) == 2


async def test_a_new_offence_after_the_window_is_recorded_again(monkeypatch, clock):
    """A throttle, not an amnesty: come back later and the mod-log says so."""
    cog, rec = _cog(monkeypatch)
    guild, channel, member = _Guild(), _Channel(), _Member()

    await _violate(cog, guild, channel, member, times=3)
    assert len(rec.cases) == 1

    clock.now += automod._VIOLATION_LOG_WINDOW + 1
    await _violate(cog, guild, channel, member)

    assert len(rec.cases) == 2
    assert len(rec.posts) == 2
    assert channel.notices == ["no invites here"] * 2


async def test_the_throttle_map_prunes_itself(monkeypatch, clock):
    """The window is bounded state, not a leak: the entries age out."""
    cog, _rec = _cog(monkeypatch)
    guild, channel = _Guild(), _Channel()

    for uid in range(50):
        await _violate(cog, guild, channel, _Member(uid))
    assert len(cog._violation_log) == 50

    clock.now += automod._VIOLATION_LOG_WINDOW + 1
    assert cog._violation_log.is_active((guild.id, 0)) is False
