"""The package's ONE adult-content rule, and the lookup surface obeying it.

The feed has always dropped an adult activity unless the destination channel is
age-restricted (``feed_policy.route_activities`` + ``feed._allow_adult``). The
LOOKUP surface renders the very same material by another door - ``/anime``,
``/manga``, ``/trending``, ``/popular``, ``/seasonal``, the hub's search and the
picker clicks all draw AniList's cover, banner and synopsis - and did it into
any channel at all.

What is pinned here:

* the rule is ONE function (:func:`feed_policy.blocks_adult`) with ONE channel
  resolver (:func:`helpers.channel_allows_adult`), used by both surfaces, so the
  two cannot drift apart;
* the lookup queries actually ASK for ``isAdult`` - without the field the gate
  reads None for every title and silently passes everything;
* a picker never lists an adult title in a channel that may not show it, a
  direct hit is refused rather than drawn, and a click is re-checked against the
  channel it came from;
* the feed's own behaviour is byte-identical after the extraction.

Offline: cogs are built with ``__new__`` and fed hand-rolled fakes, like
tests/cogs/test_anilist_feed_mutes.py.
"""

import types

import pytest

from cogs.anilist import AniList, collection, media_view
from cogs.anilist import feed_policy as af
from cogs.anilist.feed import AniListFeed
from cogs.anilist.helpers import channel_allows_adult
from cogs.anilist.queries import (
    CANDIDATE_QUERY,
    COLLECTION_QUERY,
    MEDIA_QUERY,
    PAGE_QUERY,
)

AUTHOR = 42


# --- Fakes ------------------------------------------------------------------


class _Channel:
    def __init__(self, nsfw):
        self._nsfw = nsfw

    def is_nsfw(self):
        return self._nsfw


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Ctx:
    def __init__(self, nsfw=False):
        self.channel = _Channel(nsfw)
        self.author = types.SimpleNamespace(id=AUTHOR)
        self.sent = []

    def typing(self):
        return _Typing()

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return types.SimpleNamespace(id=1)


class _Response:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return types.SimpleNamespace(id=2)


class _ClickInteraction:
    def __init__(self, nsfw=False):
        self.channel = _Channel(nsfw)
        self.message = None
        self.response = _Response()
        self.followup = _Followup()
        self.edited = None

    async def edit_original_response(self, **kwargs):
        self.edited = kwargs
        return types.SimpleNamespace(id=3)


def _media(media_id=1, *, adult=False, title="Title"):
    return {
        "id": media_id,
        "isAdult": adult,
        "type": "ANIME",
        "title": {"romaji": title, "english": title},
        "format": "TV",
        "episodes": 12,
        "seasonYear": 2024,
        "description": "A synopsis.",
        "coverImage": {"large": "https://cdn/cover.png", "color": "#112233"},
        "bannerImage": "https://cdn/banner.png",
        "siteUrl": "https://anilist.co/anime/1",
    }


def _cog(*, candidates=None, media=None, page=None, token=None):
    """An AniList cog whose GraphQL answers are canned per query."""

    cog = AniList.__new__(AniList)
    cog.queries = []

    async def _graphql(query, variables, token=None):
        cog.queries.append(query)
        if query is CANDIDATE_QUERY:
            return {"data": {"Page": {"media": list(candidates or [])}}}
        if query is PAGE_QUERY:
            return {"data": {"Page": {"media": list(page or [])}}}
        if query is MEDIA_QUERY:
            wanted = variables.get("id")
            for item in media or []:
                if item.get("id") == wanted:
                    return {"data": {"Media": item}}
            return {"data": {"Media": None}}
        return {"data": {}}

    async def _get_token(user_id):
        return token

    cog._graphql = _graphql
    cog._get_token = _get_token
    return cog


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "is_adult, allow_adult, blocked",
    [
        (True, False, True),  # THE case: adult media, ordinary channel
        (True, True, False),  # allowed where the channel is age-restricted
        (False, False, False),
        (None, False, False),  # AniList leaves the flag null on some media
    ],
)
def test_the_adult_rule_is_one_function(is_adult, allow_adult, blocked):
    assert af.blocks_adult(is_adult, allow_adult) is blocked


def test_the_list_form_drops_only_the_adult_rows_and_keeps_order():
    rows = [_media(1), _media(2, adult=True), _media(3)]

    kept = af.drop_adult(rows, False)

    assert [row["id"] for row in kept] == [1, 3]
    assert [row["id"] for row in af.drop_adult(rows, True)] == [1, 2, 3]
    assert af.drop_adult(None, False) == []
    assert af.drop_adult([None, "junk", _media(4)], False) == [_media(4)]


@pytest.mark.parametrize(
    "channel, allowed",
    [
        (_Channel(True), True),
        (_Channel(False), False),
        (object(), False),  # a DM / partial channel cannot answer -> no
        (None, False),
    ],
)
def test_the_channel_resolver_answers_no_when_it_cannot_answer(channel, allowed):
    assert channel_allows_adult(channel) is allowed


def test_a_channel_that_raises_is_not_treated_as_age_restricted():
    class _Rude:
        def is_nsfw(self):
            raise RuntimeError("boom")

    assert channel_allows_adult(_Rude()) is False


def test_the_feed_resolver_is_the_same_one():
    """One question, one answer: the feed resolves the id then delegates."""

    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = types.SimpleNamespace(get_channel=lambda cid: {1: _Channel(True)}.get(cid))

    assert cog._allow_adult(1) is True
    assert cog._allow_adult(2) is False  # unresolvable channel


def test_the_feed_still_drops_adult_activities_outside_nsfw():
    """The extraction must not have changed the behaviour it came from."""

    activity = {"id": 1, "type": "TEXT", "user_id": 7, "is_adult": True}
    feed = {"channel_id": 100, "types": {"TEXT"}, "followed_ids": {7}}

    assert af.route_activities([activity], [dict(feed, allow_adult=False)]) == {}
    assert af.route_activities([activity], [dict(feed, allow_adult=True)])[100]


# ---------------------------------------------------------------------------
# The queries have to ASK for the flag the rule reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [MEDIA_QUERY, CANDIDATE_QUERY, PAGE_QUERY, COLLECTION_QUERY],
    ids=["media", "candidates", "page", "collection"],
)
def test_every_renderable_lookup_query_fetches_is_adult(query):
    """Without the field the gate reads None on every title and passes it."""

    assert "isAdult" in query


# ---------------------------------------------------------------------------
# ... including /anilist list, which renders covers into a shared channel
# ---------------------------------------------------------------------------
# The collection dashboard draws ``coverImage.large`` as a thumbnail beside a
# bold title link, publicly, in whatever channel it was opened in. It is the
# member's OWN list, but the CARD is not private - it is the same content as an
# adult feed activity, by a third door, and the panel was exempt from the rule
# every other door obeys.


def _entry(media_id=1, *, adult=False, title="Title"):
    return {
        "status": "CURRENT",
        "progress": 3,
        "score": 0,
        "media": dict(_media(media_id, adult=adult, title=title), siteUrl=None),
    }


def _collection_view(entries, *, allow_adult):
    return collection.CollectionView(
        types.SimpleNamespace(),
        AUTHOR,
        7,
        "anime",
        "CURRENT",
        entries,
        allow_adult=allow_adult,
    )


def _view_text(view):
    import discord

    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def test_an_adult_entry_is_dropped_from_the_list_in_an_ordinary_channel():
    view = _collection_view(
        [_entry(1, title="Ordinary"), _entry(2, adult=True, title="Adult Title")],
        allow_adult=False,
    )

    ids = [(e["media"] or {}).get("id") for e in view.entries]
    assert ids == [1]
    assert 2 not in view._by_id  # the quick actions cannot reach it either
    assert "Adult Title" not in _view_text(view)


def test_the_same_list_is_complete_in_an_age_restricted_channel():
    view = _collection_view(
        [_entry(1), _entry(2, adult=True, title="Adult Title")], allow_adult=True
    )

    assert sorted((e["media"] or {}).get("id") for e in view.entries) == [1, 2]
    assert view.hidden_adult == 0


def test_a_shortened_list_says_so_rather_than_looking_lost():
    """A silently short list is how a member concludes the bot ate their data."""
    view = _collection_view(
        [_entry(1), _entry(2, adult=True), _entry(3, adult=True)], allow_adult=False
    )

    assert view.hidden_adult == 2
    assert "age-restricted" in _view_text(view)


def test_the_collection_default_is_refusal_not_permission():
    """A caller that forgets to pass the channel's verdict must under-show."""
    view = collection.CollectionView(
        types.SimpleNamespace(), AUTHOR, 7, "anime", "CURRENT", [_entry(1, adult=True)]
    )

    assert view.entries == []


def test_both_collection_doors_ask_the_channel_they_answer_in():
    """The command and the hub button both post into a channel, so both have to
    thread its verdict - a door that forgets is a door with no rule."""
    import inspect

    from cogs.anilist import account, hub

    for source in (
        inspect.getsource(account.AccountMixin.anilist_list.callback),
        inspect.getsource(hub._HubListButton.callback),
    ):
        assert "channel_allows_adult" in source
        assert "_collection_payload" in source


def test_the_collection_obeys_the_one_shared_rule():
    """Same function as the feed and the lookup, not a fourth copy."""
    entry = _entry(1, adult=True)
    allow = False

    assert af.blocks_adult(entry["media"]["isAdult"], allow) is True
    assert _collection_view([entry], allow_adult=allow).entries == []


# ---------------------------------------------------------------------------
# The lookup surface
# ---------------------------------------------------------------------------


async def test_a_single_adult_hit_is_refused_in_an_ordinary_channel():
    cog = _cog(candidates=[_media(1, adult=True)], media=[_media(1, adult=True)])

    kwargs, view = await cog._lookup_payload(AUTHOR, "search", "ANIME", False)

    assert view is None
    assert "embed" not in kwargs
    assert "age-restricted" in kwargs["content"]


async def test_the_same_hit_opens_normally_in_an_age_restricted_channel():
    cog = _cog(candidates=[_media(1, adult=True)], media=[_media(1, adult=True)])

    kwargs, view = await cog._lookup_payload(AUTHOR, "search", "ANIME", True)

    assert view is not None
    assert kwargs["embed"].title == "Title"


async def test_adult_candidates_never_reach_the_picker():
    cog = _cog(
        candidates=[_media(1), _media(2, adult=True), _media(3)],
        media=[_media(1), _media(2, adult=True), _media(3)],
    )

    kwargs, view = await cog._lookup_payload(AUTHOR, "search", "ANIME", False)

    options = view.children[0].options
    assert [option.value for option in options] == ["1", "3"]
    assert "Found 2 results" in kwargs["content"]


async def test_a_search_that_found_only_adult_titles_says_the_rule():
    """A member who typed a title they know exists must not be told "No result."
    - they would only retype it. A search that found NOTHING still says that."""

    cog = _cog(candidates=[_media(1, adult=True), _media(2, adult=True)])
    kwargs, view = await cog._lookup_payload(AUTHOR, "search", "ANIME", False)
    assert view is None
    assert "age-restricted" in kwargs["content"]

    empty = _cog(candidates=[])
    kwargs, view = await empty._lookup_payload(AUTHOR, "search", "ANIME", False)
    assert kwargs["content"] == "No result."


async def test_the_command_asks_the_channel_it_is_answering_in():
    """The gate is worthless if the caller never passes the channel's answer."""

    cog = _cog(candidates=[_media(1, adult=True)], media=[_media(1, adult=True)])
    ctx = _Ctx(nsfw=False)

    await cog._media_lookup(ctx, "search", "ANIME")

    content, _kwargs = ctx.sent[-1]
    assert "age-restricted" in content

    nsfw_ctx = _Ctx(nsfw=True)
    await cog._media_lookup(nsfw_ctx, "search", "ANIME")
    assert "embed" in nsfw_ctx.sent[-1][1]


async def test_browsing_drops_adult_titles_from_the_listing():
    cog = _cog(page=[_media(1), _media(2, adult=True)])

    kwargs, view = await cog._browse_payload(AUTHOR, {}, "ANIME", "Trending", False)

    assert [option.value for option in view.children[0].options] == ["1"]
    assert kwargs["content"].startswith("**Trending**")


async def test_a_seasonal_page_obeys_the_same_rule():
    cog = _cog(page=[_media(1, adult=True)])

    kwargs, view = await cog._seasonal_payload(AUTHOR, "WINTER", 2024, False)

    assert view is None
    assert "No anime found" in kwargs["content"]


async def test_the_browse_default_is_refusal_not_permission():
    """A caller that forgets the flag gets the safe behaviour, not the leak."""

    cog = _cog(page=[_media(1, adult=True)])

    kwargs, view = await cog._browse_payload(AUTHOR, {}, "ANIME", "Trending")

    assert view is None
    assert kwargs["content"] == "No result."


async def test_the_wizard_editor_is_the_same_card_and_the_same_rule():
    cog = _cog(media=[_media(1, adult=True)])
    ctx = _Ctx(nsfw=False)

    await cog._open_media_editor(ctx, 1, "token")

    content, kwargs = ctx.sent[-1]
    assert "age-restricted" in content
    assert "embed" not in kwargs


# ---------------------------------------------------------------------------
# The click, re-checked against the channel the click came from
# ---------------------------------------------------------------------------


async def _click(select, interaction):
    await media_view.ResultSelect.callback(select, interaction)


async def test_a_picker_click_on_an_adult_title_is_refused_ephemerally():
    """A view can outlive an edit to the channel's age restriction, and the card
    is built from the FULL media the click fetches - so the rule is applied
    there, not assumed from the list."""

    cog = _cog(media=[_media(7, adult=True)])
    select = media_view.ResultSelect(cog, [_media(7, adult=True)], AUTHOR, "ANIME")
    select._values = ["7"]
    interaction = _ClickInteraction(nsfw=False)

    await _click(select, interaction)

    content, kwargs = interaction.followup.sent[-1]
    assert "age-restricted" in content
    assert kwargs["ephemeral"] is True
    assert interaction.edited is None  # no card was drawn


async def test_the_same_click_draws_the_card_in_an_age_restricted_channel():
    cog = _cog(media=[_media(7, adult=True)])
    select = media_view.ResultSelect(cog, [_media(7, adult=True)], AUTHOR, "ANIME")
    select._values = ["7"]
    interaction = _ClickInteraction(nsfw=True)

    await _click(select, interaction)

    assert interaction.followup.sent == []
    assert interaction.edited["embed"].title == "Title"


async def test_stepping_to_another_season_cannot_walk_around_the_rule():
    cog = _cog(page=[_media(1, adult=True)])
    view = media_view.SeasonView(cog, [_media(1)], AUTHOR, "WINTER", 2024)
    interaction = _ClickInteraction(nsfw=False)

    await view._change_season(interaction, forward=True)

    content, kwargs = interaction.followup.sent[-1]
    assert "No anime found" in content
    assert kwargs["ephemeral"] is True
