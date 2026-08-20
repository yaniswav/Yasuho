"""AutoMod must judge an EDITED message, not only a new one.

The bypass: post "hello" (clean, nothing fires), then edit it into
``discord.gg/whatever``. ``on_message`` had already run and there was no
``on_message_edit`` at all, so every content filter - invites, links - was
effectively opt-out for anyone who typed twice.

The fix runs the same content pipeline on the edit, and these tests pin the three
things that make it safe rather than merely present:

* it actually catches the edited-in invite/link (the regression itself);
* it stays cheap on a listener that fires for every link preview Discord
  attaches on its own: an edit whose CONTENT did not change must not read
  settings, and a guild with automod off must not have its author's permissions
  folded (the on_message ordering discipline, carried over);
* it cannot punish one message twice, and does not feed the anti-spam window.

No Discord, no DB: settings are answered from memory and the author counts its
own permission folds.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import types

import discord

from cogs.moderation import automod

UTC = datetime.timezone.utc


class _Payload:
    """The bit of ``RawMessageUpdateEvent`` the listener reads.

    ``message`` is the updated message discord.py builds for EVERY
    MESSAGE_UPDATE, cached or not; ``cached_message`` is the pre-edit copy, and
    it is ``None`` exactly when the message has aged out of the 1000-entry
    bot-wide message cache - which, at 1000+ guilds, is the normal case.
    """

    def __init__(self, after, cached=None):
        self.message = after
        self.cached_message = cached
        self.message_id = after.id


async def _edit(cog, before, after):
    """A CACHED edit: discord.py knows the old content, so it is compared."""
    return await cog.on_raw_message_edit(_Payload(after, cached=before))


async def _uncached_edit(cog, after):
    """The same edit AFTER the message fell out of the message cache."""
    return await cog.on_raw_message_edit(_Payload(after, cached=None))


class _Author:
    """Message author that COUNTS how often its permissions are folded."""

    def __init__(self, *, manage_messages=False, uid=7):
        self.bot = False
        self.id = uid
        self.mention = f"<@{uid}>"
        self.roles = []
        self._manage_messages = manage_messages
        self.permission_reads = 0

    @property
    def guild_permissions(self):
        self.permission_reads += 1
        return types.SimpleNamespace(manage_messages=self._manage_messages)


class _Channel:
    def __init__(self):
        self.id = 1
        self.parent_id = None
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class _Message:
    """The bit of discord.Message the scanner touches, with a deletable body."""

    _next_id = 1000

    def __init__(self, author, content, *, message_id=None):
        if message_id is None:
            _Message._next_id += 1
            message_id = _Message._next_id
        self.id = message_id
        self.author = author
        self.content = content
        self.guild = types.SimpleNamespace(
            id=42, me=None, get_member=lambda _uid: author
        )
        self.channel = _Channel()
        # Discord sets this ONLY when the author edits. An embed unfurl, a pin
        # or a flag change leaves it None, which is how the uncached path tells
        # a real edit from the storm.
        self.edited_timestamp = None
        self.deleted = 0

    async def delete(self):
        self.deleted += 1

    def edited(self, content):
        """The ``after`` half of an edit: same id, new content."""
        clone = _Message(self.author, content, message_id=self.id)
        clone.channel = self.channel
        clone.edited_timestamp = datetime.datetime.now(UTC)
        return clone

    def unfurled(self):
        """A MESSAGE_UPDATE Discord sent by itself: an embed was attached.

        Same content, and no ``edited_timestamp`` - the author never touched it.
        """
        clone = _Message(self.author, self.content, message_id=self.id)
        clone.channel = self.channel
        return clone


def _cog(monkeypatch, *, antilink=False, antispam=False, antiinvite=False):
    """An AutoMod whose settings answer from memory, counting the reads."""
    cog = automod.AutoMod(types.SimpleNamespace(db_pool=object(), user=None))
    cog._settings[42] = {"antilink": antilink, "antispam": antispam}
    cog.settings_reads = 0

    async def _get_guild(_pool, _guild_id, key, default=None):
        cog.settings_reads += 1
        if key == "antiinvite":
            return antiinvite
        return default

    monkeypatch.setattr(automod.settings, "get_guild", _get_guild)

    cog.violations = []

    async def _handle_violation(message, *, kind, notice, reason):
        # Mirror the real one's synchronous mark, which is what makes the
        # double-punish guard atomic.
        cog._scanned[message.id] = True
        cog.violations.append((message.id, kind))

    cog._handle_violation = _handle_violation
    return cog


# ---------------------------------------------------------------------------
# The regression: an edit is scanned like a send
# ---------------------------------------------------------------------------
async def test_editing_a_clean_message_into_an_invite_is_caught(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()
    before = _Message(author, "hello")

    await cog.on_message(before)
    assert cog.violations == []  # clean on send, as expected

    after = before.edited("join discord.gg/raidserver")
    await _edit(cog, before, after)

    assert cog.violations == [(before.id, "invite")]


async def test_editing_a_clean_message_into_a_link_is_caught(monkeypatch):
    cog = _cog(monkeypatch, antilink=True)
    author = _Author()
    before = _Message(author, "hello")
    after = before.edited("https://example.com/free-nitro")

    await _edit(cog, before, after)

    assert cog.violations == [(before.id, "link")]


async def test_an_edit_that_stays_clean_is_left_alone(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True, antilink=True)
    author = _Author()
    before = _Message(author, "hello")

    await _edit(cog, before, before.edited("hello there"))

    assert cog.violations == []


# ---------------------------------------------------------------------------
# ... and it must not depend on the 1000-entry bot-wide message cache
# ---------------------------------------------------------------------------
async def test_an_edit_is_scanned_even_when_the_message_left_the_cache(monkeypatch):
    """THE reason this listener is the RAW one.

    ``on_message_edit`` is dispatched only when discord.py still holds the
    message: ``ConnectionState.parse_message_update`` calls it inside
    ``if cached_message is not None`` and otherwise dispatches
    ``raw_message_edit`` alone. This bot never passes ``max_messages``, so that
    cache is the default 1000 entries BOT-WIDE across every guild - at the scale
    this project designs for, seconds of traffic. An edit-in-the-invite bypass
    that only works "after a little while" is not a bypass that was fixed.
    """
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()
    before = _Message(author, "hello")

    await cog.on_message(before)
    assert cog.violations == []

    await _uncached_edit(cog, before.edited("join discord.gg/raidserver"))

    assert cog.violations == [(before.id, "invite")]


async def test_the_listener_is_the_raw_one():
    """Pinned by name: the cached listener would silently shrink the coverage
    above back to whatever discord.py still happens to remember."""
    assert hasattr(automod.AutoMod, "on_raw_message_edit")
    assert not hasattr(automod.AutoMod, "on_message_edit")


async def test_an_uncached_unfurl_never_even_reads_settings(monkeypatch):
    """The other half of going raw: we now see the unfurl storm for EVERY guild,
    including messages we no longer hold, so the free gate has to work without a
    before-copy. Discord stamps ``edited_timestamp`` only for an author's edit."""
    cog = _cog(monkeypatch, antiinvite=True, antilink=True)
    author = _Author()
    posted = _Message(author, "look https://example.com")

    await _uncached_edit(cog, posted.unfurled())

    assert cog.settings_reads == 0
    assert author.permission_reads == 0
    assert cog.violations == []


async def test_an_author_with_no_member_data_is_still_scanned(monkeypatch):
    """A raw payload's author is a Member via the gateway's ``member`` field or
    the guild cache. If neither answered we cannot fold permissions - and the
    safe direction is to SCAN, not to skip: the other way round, suppressing
    that field would be a permission-free opt-out of automod."""
    cog = _cog(monkeypatch, antiinvite=True)

    class _Userish:
        bot = False
        id = 7
        mention = "<@7>"
        roles = []

    author = _Userish()
    message = _Message(author, "discord.gg/raidserver")
    message.guild = types.SimpleNamespace(id=42, me=None, get_member=lambda _u: None)
    message.edited_timestamp = datetime.datetime.now(UTC)
    assert not isinstance(author, discord.Member)

    await _uncached_edit(cog, message)

    assert cog.violations == [(message.id, "invite")]


# ---------------------------------------------------------------------------
# Hot path: MESSAGE_UPDATE also fires when Discord attaches a link preview
# ---------------------------------------------------------------------------
async def test_an_unchanged_content_edit_never_even_reads_settings(monkeypatch):
    """Embed unfurls edit ``embeds``, not ``content`` - they must cost nothing."""
    cog = _cog(monkeypatch, antiinvite=True, antilink=True)
    author = _Author()
    before = _Message(author, "look https://example.com")

    await _edit(cog, before, before.edited("look https://example.com"))

    assert cog.settings_reads == 0
    assert author.permission_reads == 0
    assert cog.violations == []


async def test_an_edit_in_a_guild_with_automod_off_never_folds_permissions(
    monkeypatch,
):
    cog = _cog(monkeypatch)  # every feature off
    author = _Author()
    before = _Message(author, "hello")

    await _edit(cog, before, before.edited("discord.gg/raidserver"))

    assert author.permission_reads == 0
    assert cog.violations == []


async def test_antispam_alone_does_not_open_the_edit_gate(monkeypatch):
    """An edit is not a send: only the CONTENT filters apply to it."""
    cog = _cog(monkeypatch, antispam=True)
    author = _Author()
    before = _Message(author, "hello")

    await _edit(cog, before, before.edited("discord.gg/raidserver"))

    assert author.permission_reads == 0
    assert cog.violations == []


async def test_a_bot_or_dm_edit_leaves_before_anything(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True)

    from_bot = _Author()
    from_bot.bot = True
    before = _Message(from_bot, "hello")
    await _edit(cog, before, before.edited("discord.gg/x"))

    in_dm = _Author()
    dm_before = _Message(in_dm, "hello")
    dm_after = dm_before.edited("discord.gg/x")
    dm_after.guild = None
    await _edit(cog, dm_before, dm_after)

    assert cog.settings_reads == 0
    assert cog.violations == []


async def test_a_moderator_editing_is_still_never_auto_moderated(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author(manage_messages=True)
    before = _Message(author, "hello")

    async def _is_exempt(_message):  # pragma: no cover - must not be reached
        raise AssertionError("a manage_messages author must return before this")

    cog._is_exempt = _is_exempt

    await _edit(cog, before, before.edited("discord.gg/x"))

    assert author.permission_reads == 1
    assert cog.violations == []


async def test_an_exempt_author_edit_is_left_alone_and_releases_its_claim(
    monkeypatch,
):
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()
    before = _Message(author, "hello")

    async def _is_exempt(_message):
        return True

    cog._is_exempt = _is_exempt

    after = before.edited("discord.gg/x")
    await _edit(cog, before, after)

    assert cog.violations == []
    # Exempt is not "actioned": the id must not stay marked, or a later change
    # to the exemption list would never take effect on this message.
    assert after.id not in cog._scanned


# ---------------------------------------------------------------------------
# One message, one punishment
# ---------------------------------------------------------------------------
async def test_a_message_actioned_on_send_is_not_punished_again_on_edit(
    monkeypatch,
):
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()
    message = _Message(author, "discord.gg/raidserver")

    await cog.on_message(message)
    assert cog.violations == [(message.id, "invite")]

    # The member edits the (already actioned) message into another invite.
    await _edit(cog, message, message.edited("discord.gg/other"))

    assert cog.violations == [(message.id, "invite")]  # still exactly one


async def test_two_edits_of_the_same_message_action_it_once(monkeypatch):
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()
    before = _Message(author, "hello")

    await _edit(cog, before, before.edited("discord.gg/a"))
    await _edit(cog, before, before.edited("discord.gg/b"))

    assert len(cog.violations) == 1


async def test_a_clean_edit_releases_the_claim_so_the_next_one_is_scanned(
    monkeypatch,
):
    """The claim is a scan lock, not a permanent immunity stamp."""
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()
    before = _Message(author, "hello")

    await _edit(cog, before, before.edited("still clean"))
    assert cog.violations == []

    await _edit(cog, before, before.edited("discord.gg/late"))

    assert cog.violations == [(before.id, "invite")]


async def test_edits_never_feed_the_anti_spam_window(monkeypatch):
    """The sliding window counts messages SENT; an edit burst is not a send."""
    cog = _cog(monkeypatch, antispam=True, antiinvite=True)
    author = _Author()
    before = _Message(author, "hello")

    for i in range(10):
        await _edit(cog, before, before.edited(f"edit {i}"))

    assert cog._spam == {}
    assert cog.violations == []


async def test_the_scanned_map_is_bounded(monkeypatch):
    """Message ids are unbounded; the map that remembers them is not."""
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()

    for i in range(automod._SCANNED_CAP + 50):
        message = _Message(author, "discord.gg/spam", message_id=10_000 + i)
        await cog.on_message(message)

    assert len(cog._scanned) == automod._SCANNED_CAP
