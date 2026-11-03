"""Anime alerts quick-setup wizard (cogs/anilist/alerts_wizard.py).

No Discord, no DB, no AniList: the feed cog, the guild and the interaction are
small stand-ins that record what the wizard asks of them.
"""

from __future__ import annotations

import asyncio
import types
from unittest import mock

import discord
import pytest

from cogs.anilist import alerts_wizard as aw
from cogs.anilist.breaker import breaker_for
from cogs.anilist.feed_delivery import _FetchError
from tools.i18n import _


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _media(n, *, adult=False, title=True):
    return {
        "id": n,
        "isAdult": adult,
        "title": {"romaji": "Show %d" % n if title else None, "english": None},
        "format": "TV",
    }


def _payload(media):
    return {"data": {"Page": {"media": media}}}


def _titles(n):
    return [{"id": i, "title": "Show %d" % i, "format": "TV_SHORT"} for i in range(1, n + 1)]


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(aw, "_airing_cache", aw._AiringCache())


# --- parsing and options ---------------------------------------------------


def test_parse_titles_drops_adult_untitled_and_caps_at_25():
    data = _payload([_media(1, adult=True), _media(2, title=False)] + [_media(i) for i in range(3, 60)])
    titles = aw._parse_titles(data)
    assert len(titles) == aw.MAX_OPTIONS == 25
    assert [t["id"] for t in titles][:2] == [3, 4]
    assert aw._parse_titles(None) == []


class _Guild:
    id = 5
    me = object()

    def __init__(self, channels=()):
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(cid)


def _wizard(titles, channel_id=None, tracked=(), guild=None, cog=None):
    return aw.AnimeAlertsWizard(cog, guild or _Guild(), 9, titles, channel_id, tracked)


def _title_select(wizard):
    return next(c for c in wizard.walk_children() if isinstance(c, aw._TitleSelect))


def test_options_capped_at_25_and_tracked_marked_not_selected():
    wizard = _wizard(_titles(40), tracked={2, 3})
    options = _title_select(wizard).options
    assert len(options) == 25
    marked = {o.value for o in options if o.emoji is not None}
    assert marked == {"2", "3"}
    assert all(o.description == _("Already tracked here") for o in options if o.value in marked)
    # Marked, not pre-selected: re-running the wizard re-adds nothing.
    assert not any(o.default for o in options)


def test_selection_survives_a_rerender():
    wizard = _wizard(_titles(5))
    wizard.selected = {1, 4}
    wizard._build()
    assert {o.value for o in _title_select(wizard).options if o.default} == {"1", "4"}


def test_create_button_needs_a_channel_and_a_title():
    def button(wizard):
        return next(c for c in wizard.walk_children() if isinstance(c, aw._CreateButton))

    wizard = _wizard(_titles(3), channel_id=None)
    assert button(wizard).disabled
    wizard.channel_id, wizard.selected = 77, set()
    wizard._build()
    assert button(wizard).disabled
    wizard.selected = {1}
    wizard._build()
    assert not button(wizard).disabled


# --- the airing cache ------------------------------------------------------


async def test_cache_serves_from_memory_until_the_ttl():
    clock = _Clock()
    cache = aw._AiringCache(clock)
    fetch = mock.AsyncMock(return_value=_titles(3))
    await cache.get(fetch)
    clock.now += aw.SEASON_TTL - 1
    await cache.get(fetch)
    assert fetch.await_count == 1
    clock.now += 2
    await cache.get(fetch)
    assert fetch.await_count == 2


async def test_concurrent_wizards_cost_one_request():
    cache = aw._AiringCache(_Clock())
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return _titles(2)

    await asyncio.gather(*(cache.get(fetch) for _i in range(20)))
    assert calls == 1


async def test_a_failure_is_not_retried_inside_the_cooldown():
    clock = _Clock()
    cache = aw._AiringCache(clock)
    fetch = mock.AsyncMock(side_effect=aw.SeasonUnavailable("down"))
    for _i in range(3):
        with pytest.raises(aw.SeasonUnavailable):
            await cache.get(fetch)
    assert fetch.await_count == 1
    clock.now += aw.FAIL_TTL + 1
    fetch.side_effect = None
    fetch.return_value = _titles(1)
    assert await cache.get(fetch) == _titles(1)


def _feed_cog(graphql):
    bot = types.SimpleNamespace()
    return types.SimpleNamespace(bot=bot, _graphql=graphql)


async def test_fetch_uses_one_graphql_call_then_the_cache():
    graphql = mock.AsyncMock(return_value=_payload([_media(1), _media(2)]))
    cog = _feed_cog(graphql)
    first = await aw.fetch_airing_titles(cog)
    second = await aw.fetch_airing_titles(cog)
    assert first == second and graphql.await_count == 1


async def test_fetch_refuses_while_the_shared_breaker_is_open():
    graphql = mock.AsyncMock(return_value=_payload([_media(1)]))
    cog = _feed_cog(graphql)
    breaker = breaker_for(cog.bot)
    for _i in range(5):
        breaker.note_failure("feed", "boom")
    assert breaker.is_open()
    with pytest.raises(aw.SeasonUnavailable):
        await aw.fetch_airing_titles(cog)
    graphql.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome",
    [_FetchError("timeout"), _payload([])],
    ids=["fetch-error", "empty-list"],
)
async def test_fetch_failure_or_empty_is_unavailable(outcome):
    graphql = mock.AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else mock.AsyncMock(return_value=outcome)
    with pytest.raises(aw.SeasonUnavailable):
        await aw.fetch_airing_titles(_feed_cog(graphql))


# --- applying the setup ----------------------------------------------------


class _Pool:
    def __init__(self, feed):
        self.feed = feed

    async def fetchrow(self, *args):
        return self.feed


class _FeedCog:
    """Records the guarded helpers apply_alerts is allowed to call."""

    def __init__(self, feed=None, tracked=(), cap=None, create_error=None, manga=(), race=None):
        self.bot = types.SimpleNamespace(db_pool=_Pool(feed))
        self.tracked = list(tracked)
        self.manga = list(manga)
        # race: the row another admin's feed has once _create_feed refuses
        self.race = race
        self.archived = set()
        self.cap = cap
        self.create_error = create_error
        self.created = 0
        self.added = []

    async def _create_feed(self, guild_id, channel_id, person_id=None):
        self.created += 1
        if self.race is not None:
            self.bot.db_pool.feed = self.race  # the other admin's feed appears
        return self.create_error

    async def _set_enabled(self, guild_id, channel_id, enabled, person_id=None):
        return None

    async def _channel_subs_for_feed(self, guild_id, channel_id):
        rows = [{"media_id": m, "media_type": "ANIME"} for m in self.tracked]
        return rows + [{"media_id": m, "media_type": "MANGA"} for m in self.manga]

    async def _feed_sub_archival(self, guild_id, channel_id, subs=None):
        archived = self.archived
        return types.SimpleNamespace(is_active=lambda media_id: media_id not in archived)

    async def _add_channel_sub(self, guild_id, channel_id, media_id, media_type, title, added_by):
        if self.cap is not None and len(self.tracked) + len(self.added) >= self.cap:
            return "feed full"
        self.added.append(media_id)
        return None


async def test_creates_the_feed_and_every_title():
    cog = _FeedCog(feed=None)
    res = await aw.apply_alerts(cog, 5, 6, [(1, "A"), (2, "B")], 9)
    assert (res.feed_created, res.added, res.already, res.skipped, res.error) == (True, 2, 0, 0, None)
    assert cog.added == [1, 2]


async def test_rerunning_is_idempotent_for_tracked_titles():
    cog = _FeedCog(feed={"enabled": True}, tracked=[1, 2])
    res = await aw.apply_alerts(cog, 5, 6, [(1, "A"), (2, "B"), (3, "C")], 9)
    assert (res.feed_created, res.added, res.already) == (False, 1, 2)
    assert cog.created == 0 and cog.added == [3]


async def test_partial_success_stops_at_the_first_limit_refusal():
    cog = _FeedCog(feed={"enabled": True}, tracked=[100], cap=3)
    picks = [(i, "T%d" % i) for i in range(1, 6)]
    res = await aw.apply_alerts(cog, 5, 6, picks, 9)
    assert (res.added, res.skipped, res.error) == (2, 3, "feed full")
    assert cog.added == [1, 2]  # no further attempts once the cap said no


async def test_feed_limit_refusal_creates_nothing():
    cog = _FeedCog(feed=None, create_error="max feeds")
    res = await aw.apply_alerts(cog, 5, 6, [(1, "A"), (2, "B")], 9)
    assert (res.feed_created, res.added, res.skipped, res.error) == (False, 0, 2, "max feeds")
    assert cog.added == []


def test_result_card_reports_partial_success_with_the_refusal():
    wizard = _wizard(_titles(3), channel_id=6)
    res = aw.AlertsResult()
    res.feed_created, res.added, res.skipped, res.error = True, 5, 2, "LIMIT TEXT"
    wizard.result = res
    wizard._build()
    text = "\n".join(c.content for c in wizard.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "5 titles are now tracked." in text
    assert "2 titles were skipped:" in text and "LIMIT TEXT" in text
    assert "<#6>" in text


# --- create button: bot permissions ---------------------------------------


class _Channel:
    id = 6
    mention = "<#6>"

    def __init__(self, perms, type=discord.ChannelType.text):
        self._perms = perms
        self.type = type

    def permissions_for(self, me):
        return self._perms


def _interaction(*, manage=True):
    return types.SimpleNamespace(
        user=types.SimpleNamespace(
            id=9, guild_permissions=discord.Permissions(manage_guild=manage)
        ),
        response=types.SimpleNamespace(
            is_done=lambda: True, defer=mock.AsyncMock(), send_message=mock.AsyncMock()
        ),
        followup=types.SimpleNamespace(send=mock.AsyncMock()),
        edit_original_response=mock.AsyncMock(),
    )


async def test_missing_bot_permissions_create_nothing_and_name_them():
    cog = _FeedCog(feed=None)
    perms = discord.Permissions(view_channel=True, send_messages=False, embed_links=False)
    wizard = _wizard(_titles(3), channel_id=6, guild=_Guild([_Channel(perms)]), cog=cog)
    wizard.selected = {1}
    await wizard.create(_interaction())
    assert cog.created == 0 and cog.added == []
    assert wizard.result is None
    assert _("Send Messages") in wizard.note and _("Embed Links") in wizard.note
    assert _("View Channel") not in wizard.note


async def test_create_with_good_permissions_applies_and_shows_the_result():
    cog = _FeedCog(feed=None)
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    wizard = _wizard(_titles(3), channel_id=6, guild=_Guild([_Channel(perms)]), cog=cog)
    wizard.selected = {1, 3}
    await wizard.create(_interaction())
    assert cog.created == 1 and cog.added == [1, 3]
    assert wizard.result.added == 2


async def test_create_rechecks_manage_guild_at_click_time():
    cog = _FeedCog(feed=None)
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    wizard = _wizard(_titles(3), channel_id=6, guild=_Guild([_Channel(perms)]), cog=cog)
    wizard.selected = {1}
    await wizard.create(_interaction(manage=False))
    assert cog.created == 0 and cog.added == []


# --- entry point -----------------------------------------------------------


def _entry_interaction(cog, *, manage):
    inter = _interaction(manage=manage)
    done = []
    inter.response.defer = mock.AsyncMock(side_effect=lambda **kw: done.append(1))
    inter.response.is_done = lambda: bool(done)
    inter.client = types.SimpleNamespace(get_cog=lambda name: cog)
    inter.guild = _Guild()
    inter.guild_id = 5
    inter.channel = None
    return inter


async def test_non_manager_gets_an_ephemeral_refusal_and_no_anilist_call():
    graphql = mock.AsyncMock(return_value=_payload([_media(1)]))
    cog = _feed_cog(graphql)
    inter = _entry_interaction(cog, manage=False)
    await aw.open_anime_alerts_wizard(inter)
    inter.response.defer.assert_awaited()  # answered, ephemerally
    inter.followup.send.assert_awaited_once()
    assert "Manage Server" in inter.followup.send.await_args.args[0]
    graphql.assert_not_awaited()


async def test_anilist_down_gives_a_friendly_message_not_a_wizard():
    cog = _feed_cog(mock.AsyncMock(side_effect=_FetchError("down")))
    inter = _entry_interaction(cog, manage=True)
    await aw.open_anime_alerts_wizard(inter)
    inter.followup.send.assert_awaited_once()
    sent = inter.followup.send.await_args
    assert "view" not in sent.kwargs
    assert "AniList isn't answering" in sent.args[0]


async def test_manager_gets_the_wizard_ephemerally():
    cog = _feed_cog(mock.AsyncMock(return_value=_payload([_media(1), _media(2)])))
    cog._channel_subs_for_feed = mock.AsyncMock(return_value=[])
    inter = _entry_interaction(cog, manage=True)
    await aw.open_anime_alerts_wizard(inter)
    sent = inter.followup.send.await_args
    assert isinstance(sent.kwargs["view"], aw.AnimeAlertsWizard)
    assert sent.kwargs["ephemeral"] is True


def test_missing_bot_permissions_helper_lists_only_the_missing_ones():
    channel = _Channel(discord.Permissions(view_channel=True, send_messages=True))
    assert aw.missing_bot_permissions(channel, None) == [_("Embed Links")]
    full = _Channel(discord.Permissions(view_channel=True, send_messages=True, embed_links=True))
    assert aw.missing_bot_permissions(full, None) == []


def test_current_channel_is_the_default_only_when_a_feed_may_live_there():
    text = types.SimpleNamespace(id=6, type=discord.ChannelType.text)
    voice = types.SimpleNamespace(id=7, type=discord.ChannelType.voice)
    thread = types.SimpleNamespace(id=8, type=discord.ChannelType.public_thread)
    guild = _Guild([text, voice, thread])
    assert aw._default_channel(guild, text) == 6
    assert aw._default_channel(guild, voice) is None
    assert aw._default_channel(guild, thread) is None
    assert aw._default_channel(guild, None) is None
    wizard = _wizard(_titles(3), channel_id=6, guild=guild)
    select = next(c for c in wizard.walk_children() if isinstance(c, aw._ChannelSelect))
    assert [d.id for d in select.default_values] == [6]


# --- review fixes -----------------------------------------------------------


async def test_losing_the_feed_race_still_adds_the_titles():
    cog = _FeedCog(feed=None, create_error="already a feed", race={"enabled": True})
    res = await aw.apply_alerts(cog, 5, 6, [(1, "A"), (2, "B")], 9)
    assert (res.feed_created, res.added, res.skipped, res.error) == (False, 2, 0, None)


async def test_losing_the_race_to_a_disabled_feed_reenables_it():
    cog = _FeedCog(feed=None, create_error="already a feed", race={"enabled": False})
    res = await aw.apply_alerts(cog, 5, 6, [(1, "A")], 9)
    assert (res.feed_enabled, res.added) == (True, 1)


async def test_a_real_create_refusal_still_skips_everything():
    cog = _FeedCog(feed=None, create_error="max feeds")  # no feed appears
    res = await aw.apply_alerts(cog, 5, 6, [(1, "A")], 9)
    assert (res.skipped, res.error, cog.added) == (1, "max feeds", [])


async def test_manga_subs_do_not_count_as_tracked_anime():
    cog = _FeedCog(feed={"enabled": True}, tracked=[1], manga=[2, 3])
    assert await aw.tracked_titles(cog, 5, 6) == ({1}, set())
    res = await aw.apply_alerts(cog, 5, 6, [(2, "B")], 9)
    assert (res.added, res.already) == (1, 0)


async def test_archived_anime_subs_are_told_apart():
    cog = _FeedCog(feed={"enabled": True}, tracked=[1, 2])
    cog.archived = {2}
    active, archived = await aw.tracked_titles(cog, 5, 6)
    assert (active, archived) == ({1}, {2})
    wizard = _wizard(_titles(3), channel_id=6, tracked=active)
    wizard.archived = archived
    wizard._build()
    by_value = {o.value: o for o in _title_select(wizard).options}
    assert by_value["1"].description == _("Already tracked here")
    assert by_value["2"].description != _("Already tracked here")
    assert by_value["2"].emoji is not None
    # Only the active title is counted in the footer line.
    text = "\n".join(c.content for c in wizard.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "1 title is already tracked" in text


async def test_channel_must_be_an_allowed_type_the_invoker_can_see():
    cog = _FeedCog(feed=None)
    bot_ok = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    voice = _Channel(bot_ok, type=discord.ChannelType.voice)
    wizard = _wizard(_titles(3), channel_id=6, guild=_Guild([voice]), cog=cog)
    wizard.selected = {1}
    await wizard.create(_interaction())
    assert cog.created == 0 and wizard.result is None and wizard.note

    class _Hidden(_Channel):
        def permissions_for(self, who):
            # the bot sees it, the invoker does not
            return bot_ok if who is _Guild.me else discord.Permissions(view_channel=False)

    wizard = _wizard(_titles(3), channel_id=6, guild=_Guild([_Hidden(bot_ok)]), cog=cog)
    wizard.selected = {1}
    await wizard.create(_interaction())
    assert cog.created == 0 and cog.added == [] and wizard.result is None and wizard.note


async def test_double_click_runs_the_creation_once():
    cog = _FeedCog(feed=None)
    gate = asyncio.Event()
    original = cog._create_feed

    async def slow_create(*args, **kwargs):
        await gate.wait()
        return await original(*args, **kwargs)

    cog._create_feed = slow_create
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    wizard = _wizard(_titles(3), channel_id=6, guild=_Guild([_Channel(perms)]), cog=cog)
    wizard.selected = {1}
    first = asyncio.ensure_future(wizard.create(_interaction()))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(wizard.create(_interaction()))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)
    assert cog.created == 1 and cog.added == [1]


async def test_preselect_channel_false_starts_without_a_channel():
    cog = _feed_cog(mock.AsyncMock(return_value=_payload([_media(1)])))
    cog._channel_subs_for_feed = mock.AsyncMock(return_value=[])
    text = types.SimpleNamespace(id=6, type=discord.ChannelType.text)
    member = types.SimpleNamespace(
        id=9, guild_permissions=discord.Permissions(manage_guild=True)
    )
    guild = _Guild([text])
    view = await aw.build_alerts_wizard(cog, guild, member, text, preselect_channel=False)
    assert view.channel_id is None
    cog._channel_subs_for_feed = mock.AsyncMock(return_value=[])
    cog._feed_sub_archival = mock.AsyncMock(
        return_value=types.SimpleNamespace(is_active=lambda m: True)
    )
    view = await aw.build_alerts_wizard(cog, guild, member, text)
    assert view.channel_id == 6
