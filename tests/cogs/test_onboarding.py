"""Unit tests for the onboarding promo card (Lot O1).

Covers channel selection (``_pick_channel``: priority order, and BOTH
permissions - ``send_messages`` and ``view_channel``), the 30-day repost guard
(``_marker_is_recent`` via ``_maybe_post_card``), the "marker only after a
successful send" idempotence invariant, the guild-locale wrapping of the card
build+send, the Components V2 ceilings, and the DM/no-op fallbacks. Drives
against local fakes following the ``tests/cogs/test_events_retention.py``
pattern (bare ``object.__new__`` cog + ``types.SimpleNamespace`` bot/guild).
"""

import datetime
import types

import discord
import pytest

import cogs.system.onboarding as onboarding
from cogs.system.onboarding import OnboardingCardView, _marker_is_recent, _pick_channel


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Perms:
    def __init__(self, send_messages, view_channel):
        self.send_messages = send_messages
        self.view_channel = view_channel


class _FakeChannel:
    def __init__(self, name, *, writable=True, send_messages=None, view_channel=None):
        self.name = name
        # ``writable`` sets both flags; the two explicit kwargs let a test flip
        # exactly one of them, which is how view_channel gets its own coverage.
        self._send_messages = writable if send_messages is None else send_messages
        self._view_channel = writable if view_channel is None else view_channel
        self.sent = []
        self.raises = None

    def permissions_for(self, _member):
        return _Perms(self._send_messages, self._view_channel)

    async def send(self, *args, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.sent.append((args, kwargs))


class _FakeOwner:
    def __init__(self, *, raises=None):
        self.sent = []
        self.raises = raises

    async def send(self, *args, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.sent.append((args, kwargs))


def _http_error(status=403):
    """A discord.HTTPException with no network involved."""

    response = types.SimpleNamespace(status=status, reason="nope")
    return discord.HTTPException(response, "boom")


class _FakeGuild:
    def __init__(
        self,
        *,
        guild_id=1,
        name="Guild",
        system_channel=None,
        text_channels=None,
        owner=None,
        me="ME",
    ):
        self.id = guild_id
        self.name = name
        self.system_channel = system_channel
        self.text_channels = text_channels or []
        self.owner = owner
        self.me = me


def _cog(bot):
    cog = object.__new__(onboarding.Onboarding)
    cog.bot = bot
    return cog


def _bot(*, get_guild=None, set_guild=None, resolve_locale=None, monkeypatch=None):
    async def _default_get_guild(_pool, _guild_id, _key, default=None):
        return default

    async def _default_set_guild(_pool, _guild_id, _key, _value):
        pass

    async def _default_resolve(_bot, _guild):
        return "en"

    monkeypatch.setattr(
        onboarding.settings, "get_guild", get_guild or _default_get_guild
    )
    monkeypatch.setattr(
        onboarding.settings, "set_guild", set_guild or _default_set_guild
    )
    monkeypatch.setattr(
        onboarding.i18n, "resolve_guild_locale", resolve_locale or _default_resolve
    )
    return types.SimpleNamespace(db_pool=object())


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------
def test_pick_channel_prefers_system_channel():
    system = _FakeChannel("random-name")
    named = _FakeChannel("general")
    guild = _FakeGuild(system_channel=system, text_channels=[named])
    assert _pick_channel(guild) is system


def test_pick_channel_falls_back_to_named_candidate():
    system = _FakeChannel("random-name", writable=False)
    named = _FakeChannel("bienvenue")
    other = _FakeChannel("random-2")
    guild = _FakeGuild(system_channel=system, text_channels=[other, named])
    assert _pick_channel(guild) is named


def test_pick_channel_falls_back_to_first_writable_text_channel():
    unwritable = _FakeChannel("random-1", writable=False)
    writable = _FakeChannel("random-2")
    guild = _FakeGuild(system_channel=None, text_channels=[unwritable, writable])
    assert _pick_channel(guild) is writable


def test_pick_channel_none_when_nothing_writable():
    unwritable = _FakeChannel("random-1", writable=False)
    guild = _FakeGuild(system_channel=None, text_channels=[unwritable])
    assert _pick_channel(guild) is None


def test_pick_channel_none_when_me_is_none():
    named = _FakeChannel("general")
    guild = _FakeGuild(text_channels=[named], me=None)
    assert _pick_channel(guild) is None


def test_pick_channel_honours_candidate_priority_not_sidebar_order():
    # "chat" sits above "general" in the sidebar, but the candidate list ranks
    # "general" first: the pick follows the list, not the channel positions.
    chat = _FakeChannel("chat")
    general = _FakeChannel("general")
    guild = _FakeGuild(system_channel=None, text_channels=[chat, general])
    assert _pick_channel(guild) is general


def test_pick_channel_skips_an_unwritable_named_candidate():
    # A locked-down #general must not abort the search: the next candidate name
    # wins, and only then the first-writable fallback.
    general = _FakeChannel("general", writable=False)
    lobby = _FakeChannel("lobby")
    other = _FakeChannel("random")
    guild = _FakeGuild(system_channel=None, text_channels=[other, general, lobby])
    assert _pick_channel(guild) is lobby


def test_pick_channel_requires_view_channel_not_just_send_messages():
    # send_messages alone is a trap: a channel we cannot see would 403 (or post
    # a card nobody reads), so it is skipped for one we can actually see.
    blind = _FakeChannel("general", send_messages=True, view_channel=False)
    visible = _FakeChannel("random", send_messages=True, view_channel=True)
    guild = _FakeGuild(system_channel=None, text_channels=[blind, visible])
    assert _pick_channel(guild) is visible


def test_pick_channel_skips_a_system_channel_we_cannot_see():
    system = _FakeChannel("announcements", send_messages=True, view_channel=False)
    lobby = _FakeChannel("lobby")
    guild = _FakeGuild(system_channel=system, text_channels=[lobby])
    assert _pick_channel(guild) is lobby


# ---------------------------------------------------------------------------
# _marker_is_recent
# ---------------------------------------------------------------------------
def test_marker_is_recent_true_within_30_days():
    now = datetime.datetime(2026, 7, 24, tzinfo=datetime.timezone.utc)
    marker = (now - datetime.timedelta(days=10)).isoformat()
    assert _marker_is_recent(marker, now=now) is True


def test_marker_is_recent_false_after_30_days():
    now = datetime.datetime(2026, 7, 24, tzinfo=datetime.timezone.utc)
    marker = (now - datetime.timedelta(days=31)).isoformat()
    assert _marker_is_recent(marker, now=now) is False


def test_marker_is_recent_false_when_unreadable():
    assert _marker_is_recent("not-a-timestamp") is False


def test_marker_is_recent_false_when_missing():
    assert _marker_is_recent(None) is False


def test_marker_is_recent_true_for_a_future_marker():
    # Clock skew must not turn into a double post: when in doubt, stay quiet.
    now = datetime.datetime(2026, 7, 24, tzinfo=datetime.timezone.utc)
    marker = (now + datetime.timedelta(days=2)).isoformat()
    assert _marker_is_recent(marker, now=now) is True


def test_marker_is_recent_reads_a_naive_marker_as_utc():
    # A naive value must never raise the naive-vs-aware TypeError.
    now = datetime.datetime(2026, 7, 24, tzinfo=datetime.timezone.utc)
    naive = datetime.datetime(2026, 7, 20).isoformat()
    assert _marker_is_recent(naive, now=now) is True


@pytest.mark.parametrize("junk", [12345, {"a": 1}, [], "2026-13-45T99:99"])
def test_marker_is_recent_never_raises_on_a_corrupt_value(junk):
    assert _marker_is_recent(junk) is False


# ---------------------------------------------------------------------------
# _maybe_post_card: idempotence
# ---------------------------------------------------------------------------
async def test_fresh_marker_skips_repost(monkeypatch):
    recent = datetime.datetime.now(datetime.timezone.utc).isoformat()

    async def get_guild(_pool, _guild_id, _key, default=None):
        return recent

    set_calls = []

    async def set_guild(_pool, guild_id, key, value):
        set_calls.append((guild_id, key, value))

    bot = _bot(get_guild=get_guild, set_guild=set_guild, monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    guild = _FakeGuild(system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    assert channel.sent == []
    assert set_calls == []


async def test_old_marker_reposts(monkeypatch):
    old = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)
    ).isoformat()

    async def get_guild(_pool, _guild_id, _key, default=None):
        return old

    set_calls = []

    async def set_guild(_pool, guild_id, key, value):
        set_calls.append((guild_id, key, value))

    bot = _bot(get_guild=get_guild, set_guild=set_guild, monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    guild = _FakeGuild(system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    assert len(channel.sent) == 1
    assert len(set_calls) == 1
    assert set_calls[0][1] == "onboarding_card_posted_at"


async def test_unreadable_marker_reposts(monkeypatch):
    async def get_guild(_pool, _guild_id, _key, default=None):
        return "garbage"

    bot = _bot(get_guild=get_guild, monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    guild = _FakeGuild(system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    assert len(channel.sent) == 1


async def test_no_marker_posts(monkeypatch):
    bot = _bot(monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    guild = _FakeGuild(system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    assert len(channel.sent) == 1
    kwargs = channel.sent[0][1]
    assert isinstance(kwargs["view"], OnboardingCardView)


# ---------------------------------------------------------------------------
# DM / no-op fallbacks
# ---------------------------------------------------------------------------
async def test_dm_owner_when_no_writable_channel(monkeypatch):
    owner = _FakeOwner()
    bot = _bot(monkeypatch=monkeypatch)
    guild = _FakeGuild(system_channel=None, text_channels=[], owner=owner)

    await _cog(bot).on_guild_join(guild)

    assert len(owner.sent) == 1


async def test_no_channel_no_owner_is_a_silent_noop(monkeypatch):
    bot = _bot(monkeypatch=monkeypatch)
    guild = _FakeGuild(system_channel=None, text_channels=[], owner=None)

    # Must not raise.
    await _cog(bot).on_guild_join(guild)


# ---------------------------------------------------------------------------
# The idempotence invariant: the marker is written ONLY after a send that
# actually landed. A failed send must not burn the one greeting a guild gets.
# ---------------------------------------------------------------------------
async def test_failed_channel_send_does_not_burn_the_marker(monkeypatch):
    set_calls = []

    async def set_guild(_pool, guild_id, key, value):
        set_calls.append((guild_id, key, value))

    bot = _bot(set_guild=set_guild, monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    channel.raises = _http_error(403)
    guild = _FakeGuild(system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    assert channel.sent == []
    assert set_calls == []  # a later join retries


async def test_forbidden_owner_dm_does_not_burn_the_marker(monkeypatch):
    set_calls = []

    async def set_guild(_pool, guild_id, key, value):
        set_calls.append((guild_id, key, value))

    bot = _bot(set_guild=set_guild, monkeypatch=monkeypatch)
    owner = _FakeOwner(raises=_http_error(403))
    guild = _FakeGuild(system_channel=None, text_channels=[], owner=owner)

    await _cog(bot).on_guild_join(guild)

    assert owner.sent == []
    assert set_calls == []


async def test_marker_is_written_after_the_send_never_before(monkeypatch):
    order = []

    async def set_guild(_pool, _guild_id, _key, _value):
        order.append("marker")

    class _OrderedChannel(_FakeChannel):
        async def send(self, *args, **kwargs):
            order.append("send")
            await super().send(*args, **kwargs)

    bot = _bot(set_guild=set_guild, monkeypatch=monkeypatch)
    guild = _FakeGuild(system_channel=_OrderedChannel("general"))

    await _cog(bot).on_guild_join(guild)

    assert order == ["send", "marker"]


async def test_marker_value_is_timezone_aware_and_reads_back_as_recent(
    monkeypatch,
):
    set_calls = []

    async def set_guild(_pool, guild_id, key, value):
        set_calls.append((guild_id, key, value))

    bot = _bot(set_guild=set_guild, monkeypatch=monkeypatch)
    guild = _FakeGuild(system_channel=_FakeChannel("general"))

    await _cog(bot).on_guild_join(guild)

    written = set_calls[0][2]
    assert datetime.datetime.fromisoformat(written).tzinfo is not None
    # The value it writes is exactly what the guard must accept next time.
    assert _marker_is_recent(written) is True


async def test_listener_never_raises_on_unexpected_failure(monkeypatch):
    async def boom(_pool, _guild_id, _key, default=None):
        raise RuntimeError("db down")

    bot = _bot(get_guild=boom, monkeypatch=monkeypatch)
    guild = _FakeGuild(system_channel=_FakeChannel("general"))

    # Must not raise: on_guild_join wraps everything.
    await _cog(bot).on_guild_join(guild)


# ---------------------------------------------------------------------------
# Locale wrapping
# ---------------------------------------------------------------------------
async def test_card_builds_and_sends_under_guild_locale(monkeypatch):
    seen_locale = {}

    async def resolve_locale(_bot, _guild):
        return "fr"

    class _SpyChannel(_FakeChannel):
        async def send(self, *args, **kwargs):
            seen_locale["at_send"] = onboarding.i18n.current_locale.get()
            await super().send(*args, **kwargs)

    channel = _SpyChannel("general")
    bot = _bot(resolve_locale=resolve_locale, monkeypatch=monkeypatch)
    guild = _FakeGuild(system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    assert seen_locale["at_send"] == "fr"
    # The autouse reset_locale fixture (conftest) confirms no leakage; here we
    # confirm the context manager already restored the default before return.
    assert onboarding.i18n.current_locale.get() == onboarding.i18n.DEFAULT_LOCALE


async def test_card_content_mentions_guild_and_config_hub(monkeypatch):
    bot = _bot(monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    guild = _FakeGuild(name="My Cool Server", system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    view = channel.sent[0][1]["view"]
    text = _card_text(view)
    assert "My Cool Server" in text
    assert "/config" in text
    assert "/help" in text
    assert "/language" in text


# ---------------------------------------------------------------------------
# Components V2 shape + injection safety
# ---------------------------------------------------------------------------
def _card_text(view):
    return "\n".join(
        c.content for c in view.children[0].children if hasattr(c, "content")
    )


def test_card_stays_well_inside_the_components_v2_ceilings():
    view = OnboardingCardView(_FakeGuild(name="Guild"))
    container = view.children[0]

    # 40 children max per view, 4000 display characters max: keep a wide margin
    # so a long translation can never make a card unsendable in production.
    assert len(list(view.walk_children())) <= 20
    assert len(container.children) <= 10
    assert len(_card_text(view)) < 2000
    assert container.accent_colour is not None


def test_card_has_no_interactive_component():
    # Read-only by design: nothing to dispatch, so nothing is retained in the
    # view store after the send.
    view = OnboardingCardView(_FakeGuild(name="Guild"))
    assert not any(item.is_dispatchable() for item in view.walk_children())


async def test_send_disables_every_mention(monkeypatch):
    # A guild name is attacker-controlled text that lands inside a TextDisplay;
    # the bot's default allowed_mentions still permits user pings, so the card
    # pins them off explicitly.
    bot = _bot(monkeypatch=monkeypatch)
    channel = _FakeChannel("general")
    guild = _FakeGuild(name="<@1234567890> @everyone", system_channel=channel)

    await _cog(bot).on_guild_join(guild)

    mentions = channel.sent[0][1]["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
