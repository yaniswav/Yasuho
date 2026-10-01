"""Unit tests for the ``/modlog status`` read-only card (Lot BL3).

Covers ``ModLogStatusView`` in both states (configured / unconfigured, plus
the events-restricted subset) and the ``modlog status`` subcommand wiring.
Drives against the conftest fakes: ``fake_pool`` (records every DB call).
"""

import datetime
import importlib.util
import pathlib
import shutil
import types

import discord

from cogs.moderation import modlog
from cogs.moderation.modlog import EVENT_KEYS, ModLog, ModLogStatusView
from tools.i18n import _

_UTC = datetime.timezone.utc


def _fresh():
    """A timestamp well inside EDIT_FRESHNESS - an edit that just happened."""
    return discord.utils.utcnow() - datetime.timedelta(seconds=1)


def _stale():
    """A timestamp well outside EDIT_FRESHNESS - a LATER update of an OLD edit."""
    return discord.utils.utcnow() - datetime.timedelta(minutes=10)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeGuild:
    def __init__(self, guild_id=1, name="guild"):
        self.id = guild_id
        self.name = name

    def get_channel(self, cid):
        return None  # channel resolution isn't exercised by the status card


class _Ctx:
    def __init__(self, guild, author_id=1):
        self.guild = guild
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace()


def _make_cog(fake_pool):
    bot = types.SimpleNamespace(db_pool=fake_pool)
    return ModLog(bot)


def _text_chars(view):
    total = 0

    def walk(item):
        nonlocal total
        content = getattr(item, "content", None)
        if isinstance(content, str):
            total += len(content)
        for child in getattr(item, "children", None) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return total


def _card_text(view):
    return "\n".join(
        c.content for c in view.children[0].children if hasattr(c, "content")
    )


def _events_block(text):
    """Just the '**Events**\\n...' section (excludes the status line's own dot)."""

    return text.split("**Events**", 1)[1]


# ---------------------------------------------------------------------------
# ModLogStatusView rendering
# ---------------------------------------------------------------------------
def test_status_card_unconfigured_shows_disabled_and_hint():
    guild = _FakeGuild()
    view = ModLogStatusView(guild, None, None)
    assert len(view.children) == 1  # a single Container
    text = _card_text(view)
    assert "Disabled" in text
    assert "Not set" in text
    assert "/modlog set" in text
    # events=None -> every event key shown as enabled (green dot).
    assert _events_block(text).count("🟢") == len(EVENT_KEYS)
    assert _text_chars(view) < 4000


def test_status_card_configured_shows_channel_and_all_events_enabled():
    guild = _FakeGuild()
    view = ModLogStatusView(guild, 555, None)
    text = _card_text(view)
    assert "Enabled" in text
    assert "<#555>" in text
    assert "/modlog set" not in text
    events_text = _events_block(text)
    assert events_text.count("🟢") == len(EVENT_KEYS)
    assert "⚪" not in events_text
    assert _text_chars(view) < 4000


def test_status_card_configured_shows_events_subset():
    guild = _FakeGuild()
    view = ModLogStatusView(guild, 555, ["join", "ban"])
    events_text = _events_block(_card_text(view))
    assert events_text.count("🟢") == 2
    assert events_text.count("⚪") == len(EVENT_KEYS) - 2


# ---------------------------------------------------------------------------
# Subcommand wiring
# ---------------------------------------------------------------------------
async def test_modlog_status_command_sends_the_card(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=7))
    await cog.modlog_status.callback(cog, ctx)
    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert isinstance(kwargs["view"], ModLogStatusView)
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


async def test_modlog_status_command_is_pure_read(fake_pool):
    """A ``status`` call must never write (unlike ``set``/``disable``)."""

    cog = _make_cog(fake_pool)
    ctx = _Ctx(_FakeGuild(guild_id=8))
    await cog.modlog_status.callback(cog, ctx)
    execs = [c for c in fake_pool.calls if c[0] == "execute"]
    assert execs == []


# ---------------------------------------------------------------------------
# on_raw_message_edit (the cache-independence fix)
#
# Model: tests/cogs/test_automod_edit_scan.py's cache-miss cases, against the
# bit of ``RawMessageUpdateEvent`` this listener actually reads: ``.message``
# (the full, post-edit Message, built for EVERY MESSAGE_UPDATE since
# discord.py 2.5) and ``.cached_message`` (the pre-edit copy, ``None`` once
# the message has aged out of the 1000-entry bot-wide cache).
# ---------------------------------------------------------------------------
class _EditPayload:
    def __init__(self, after, cached=None):
        self.message = after
        self.cached_message = cached
        self.message_id = after.id


class _EditAuthor:
    def __init__(self, uid=7, bot=False):
        self.id = uid
        self.bot = bot
        self.mention = f"<@{uid}>"
        self.display_avatar = types.SimpleNamespace(url="http://avatar.test")

    def __str__(self):
        return f"user#{self.id}"


class _EditChannel:
    def __init__(self, cid=99):
        self.id = cid
        self.mention = f"<#{cid}>"
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs.get("embed"))


class _EditGuild:
    """A guild whose ``get_channel`` only ever resolves its OWN configured id -
    exactly what ``ModLog.get_log_channel`` relies on."""

    def __init__(self, gid=50, channel=None):
        self.id = gid
        self._channel = channel

    def get_channel(self, cid):
        if self._channel is not None and cid == self._channel.id:
            return self._channel
        return None


class _EditMessage:
    def __init__(self, author, content, *, guild, channel, edited=None, message_id=1):
        self.id = message_id
        self.author = author
        self.content = content
        self.guild = guild
        self.channel = channel
        self.edited_timestamp = edited
        gid = guild.id if guild is not None else "@me"
        self.jump_url = f"https://discord.com/channels/{gid}/{channel.id}/{message_id}"


def _edit_cog(monkeypatch, *, channel_id=None, events=None):
    """A ModLog with the log-channel cache pre-warmed and settings answered
    from memory, counting how many times each is consulted."""

    bot = types.SimpleNamespace(db_pool=object())
    cog = ModLog(bot)
    cog._channels[50] = channel_id
    cog.settings_reads = 0

    async def _get_guild(_pool, _guild_id, _key, default=None):
        cog.settings_reads += 1
        return events

    monkeypatch.setattr(modlog.settings, "get_guild", _get_guild)
    return cog


async def test_uncached_edit_posts_with_a_placeholder_before(monkeypatch):
    """THE regression: no cached_message, but a real author edit (edited_timestamp
    set) - must still be logged, with an honest 'unknown' Before."""

    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    after = _EditMessage(
        author, "new content", guild=guild, channel=channel, edited=_fresh()
    )

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert len(channel.sent) == 1
    embed = channel.sent[0]
    before_field = next(f for f in embed.fields if f.name == _("Before"))
    after_field = next(f for f in embed.fields if f.name == _("After"))
    assert "cache" in before_field.value
    assert after_field.value == "new content"


async def test_a_later_update_of_the_same_edit_is_not_logged_twice(monkeypatch):
    """A link preview attached AFTER an author edit arrives as another
    MESSAGE_UPDATE that still carries the edit's edited_timestamp. Uncached,
    nothing else tells it apart from a new edit: it must not post again. A
    genuinely new edit (new timestamp) still does."""

    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    first_edit = _fresh()
    after = _EditMessage(author, "see https://example.com", guild=guild, channel=channel, edited=first_edit)

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))
    await cog.on_raw_message_edit(_EditPayload(after, cached=None))
    assert len(channel.sent) == 1

    again = _EditMessage(author, "see https://example.org", guild=guild, channel=channel, edited=_fresh())
    await cog.on_raw_message_edit(_EditPayload(again, cached=None))
    assert len(channel.sent) == 2


async def test_cached_edit_shows_the_real_before_content(monkeypatch):
    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    before = _EditMessage(author, "old content", guild=guild, channel=channel)
    after = _EditMessage(
        author, "new content", guild=guild, channel=channel, edited=object()
    )

    await cog.on_raw_message_edit(_EditPayload(after, cached=before))

    assert len(channel.sent) == 1
    embed = channel.sent[0]
    before_field = next(f for f in embed.fields if f.name == _("Before"))
    assert before_field.value == "old content"


async def test_uncached_unfurl_posts_nothing_and_never_looks_up_the_channel(
    monkeypatch, fake_pool
):
    """A MESSAGE_UPDATE Discord fires on its own (a link unfurl) carries no
    ``cached_message`` and leaves ``edited_timestamp`` untouched - the free,
    synchronous gate must stop it before the first await."""

    bot = types.SimpleNamespace(db_pool=fake_pool)
    cog = ModLog(bot)  # _channels is cold: any lookup would hit fake_pool
    guild = _EditGuild()
    author = _EditAuthor()
    after = _EditMessage(author, "look https://example.com", guild=guild, channel=_EditChannel(), edited=None)

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert fake_pool.calls == []  # get_log_channel's DB read never ran


async def test_uncached_stale_edited_timestamp_posts_nothing_and_reaches_no_await(
    fake_pool,
):
    """A LATER MESSAGE_UPDATE on an uncached message (a pin, an embed flag
    flip, a late link-preview unfurl) still carries the ORIGINAL
    edited_timestamp from an edit that happened long ago. Logging it as a
    fresh edit would be wrong; the freshness gate must stop it, synchronously,
    before the channel lookup ever runs - proven the same way the unfurl case
    above proves it, via ``fake_pool.calls``."""

    bot = types.SimpleNamespace(db_pool=fake_pool)
    cog = ModLog(bot)  # _channels cold: any lookup would hit fake_pool
    guild = _EditGuild()
    author = _EditAuthor()
    after = _EditMessage(
        author, "new content", guild=guild, channel=_EditChannel(), edited=_stale()
    )

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert fake_pool.calls == []  # get_log_channel's DB read never ran


async def test_uncached_fresh_edited_timestamp_posts(monkeypatch):
    """The companion case: a FRESH edited_timestamp on an uncached message is
    a real author edit and must still be logged."""

    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    after = _EditMessage(
        author, "new content", guild=guild, channel=channel, edited=_fresh()
    )

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert len(channel.sent) == 1


async def test_cached_edit_with_changed_content_posts_even_with_a_stale_timestamp(
    monkeypatch,
):
    """The cached path keeps its exact content comparison regardless of
    EDIT_FRESHNESS: a pin of a CACHED message has equal content and is already
    skipped above, but a cached message whose content genuinely differs is a
    real edit the cache itself proves - whatever its edited_timestamp says."""

    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    before = _EditMessage(author, "old content", guild=guild, channel=channel)
    after = _EditMessage(
        author, "new content", guild=guild, channel=channel, edited=_stale()
    )

    await cog.on_raw_message_edit(_EditPayload(after, cached=before))

    assert len(channel.sent) == 1


async def test_cached_edit_with_unchanged_content_posts_nothing(monkeypatch):
    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    before = _EditMessage(author, "same", guild=guild, channel=channel)
    after = _EditMessage(author, "same", guild=guild, channel=channel, edited=object())

    await cog.on_raw_message_edit(_EditPayload(after, cached=before))

    assert channel.sent == []


async def test_bot_author_edit_posts_nothing(monkeypatch):
    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=None)
    guild = _EditGuild(channel=channel)
    author = _EditAuthor(bot=True)
    after = _EditMessage(author, "new", guild=guild, channel=channel, edited=object())

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert channel.sent == []


async def test_dm_edit_posts_nothing():
    cog = ModLog(types.SimpleNamespace(db_pool=object()))
    author = _EditAuthor()
    after = _EditMessage(
        author, "new", guild=None, channel=_EditChannel(), edited=object()
    )

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))
    # no guild means nothing to log, and no attribute on a None guild blows up


async def test_guild_without_a_log_channel_never_consults_enabled(monkeypatch):
    """Gate ordering: the cheap channel check runs BEFORE the settings read."""

    cog = _edit_cog(monkeypatch, channel_id=None, events=None)
    guild = _EditGuild(channel=None)  # configured id is None -> no log channel
    author = _EditAuthor()
    after = _EditMessage(author, "new", guild=guild, channel=_EditChannel(), edited=_fresh())

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert cog.settings_reads == 0


async def test_event_disabled_posts_nothing(monkeypatch):
    channel = _EditChannel()
    cog = _edit_cog(monkeypatch, channel_id=channel.id, events=["join"])
    guild = _EditGuild(channel=channel)
    author = _EditAuthor()
    after = _EditMessage(author, "new", guild=guild, channel=channel, edited=_fresh())

    await cog.on_raw_message_edit(_EditPayload(after, cached=None))

    assert channel.sent == []
    assert cog.settings_reads == 1


def test_the_listener_is_the_raw_one():
    """Pinned by name: a cached ``on_message_edit`` would silently shrink
    coverage back to whatever discord.py still happens to remember."""

    assert hasattr(ModLog, "on_raw_message_edit")
    assert not hasattr(ModLog, "on_message_edit")


# ---------------------------------------------------------------------------
# MANDATORY negative control: strip the freshness gate on disk, prove it bites
#
# Same mechanism as tests/test_component_error_reporting.py's negative
# control for the same reason: a plain file copy, never git checkout/stash,
# and the mutated source is loaded under a THROWAWAY module name rather than
# via importlib.reload - reloading the real cogs.moderation.modlog would
# rebind ModLog under a fresh class object while anything already holding the
# original (there is nothing else in THIS suite, but the principle is the
# same one documented there) would not follow. Not imported from that other
# file either - independent, so a change to its helpers cannot make this
# control quietly pass without checking anything.
# ---------------------------------------------------------------------------


def _load_module_from_file(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_negative_control_removing_the_freshness_gate_breaks_the_stale_test(
    tmp_path, monkeypatch
):
    """Copy ``cogs/moderation/modlog.py`` aside, strip the freshness ``if``,
    prove the stale-timestamp case now posts (the exact bug this fix closes),
    then restore the original file untouched."""

    modlog_path = pathlib.Path(modlog.__file__)
    backup_path = tmp_path / "modlog.py.orig"
    shutil.copy(modlog_path, backup_path)

    marker = (
        "            if discord.utils.utcnow() - edited_at > EDIT_FRESHNESS:\n"
        "                return\n"
    )
    original = modlog_path.read_text()
    try:
        assert marker in original, (
            "the freshness-gate line changed shape - update this negative "
            "control's marker to match"
        )
        broken = original.replace(marker, "", 1)
        assert broken != original
        modlog_path.write_text(broken)

        broken_module = _load_module_from_file(
            "_negative_control_cogs_moderation_modlog", modlog_path
        )

        channel = _EditChannel()
        bot = types.SimpleNamespace(db_pool=object())
        cog = broken_module.ModLog(bot)
        cog._channels[50] = channel.id

        async def _get_guild(_pool, _guild_id, _key, default=None):
            return None

        # ``broken_module.settings`` IS the real, shared ``tools.settings``
        # module object (the re-executed source's own ``from tools import
        # settings`` just re-binds the name to the SAME module already in
        # sys.modules - it does not copy it). A plain attribute assignment
        # here would leave every OTHER test in the session reading a stubbed
        # ``get_guild`` forever; ``monkeypatch`` guarantees it is undone at
        # teardown even if this test fails before reaching the end.
        monkeypatch.setattr(broken_module.settings, "get_guild", _get_guild)

        guild = _EditGuild(channel=channel)
        author = _EditAuthor()
        after = _EditMessage(
            author, "new content", guild=guild, channel=channel, edited=_stale()
        )

        await cog.on_raw_message_edit(_EditPayload(after, cached=None))

        # With the gate stripped, the stale uncached update IS logged - the
        # exact regression test_uncached_stale_edited_timestamp_posts_nothing_
        # and_reaches_no_await exists to catch.
        assert len(channel.sent) == 1, (
            "the broken modlog still refused the stale edit - the freshness "
            "gate removal did not take effect, so this negative control "
            "proves nothing"
        )

        del cog, broken_module
    finally:
        shutil.copy(backup_path, modlog_path)
        assert modlog_path.read_text() == original, (
            "failed to restore cogs/moderation/modlog.py to its original content"
        )
