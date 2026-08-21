"""Every AniList reply is inert: no command echo of theirs can ping anyone.

THE HAZARD. The AniList cogs quote third-party text back into message CONTENT -
the search term the member typed, a media title fetched from AniList, an AniList
username. The bot's client default is not silent about that: ``core.Yasuho``
builds itself with ``AllowedMentions(roles=False, everyone=False, users=True)``,
so a raw ``<@id>`` riding in that echoed text still resolves and notifies.

THE SHAPE OF THE FIX, and what these tests pin:

* it is NOT "a shared base, fixed once". The package ships FOUR cogs and only
  one of them (``AniList``) is built on ``AniListBase``; ``AniListFeed`` /
  ``AniListAiring`` / ``AniListChapters`` are independent ``commands.Cog``
  subclasses with echoing commands of their own. The coverage test below walks
  the package and fails on any cog that does not carry the rule - which is
  exactly what a base-only fix would have left behind;
* the rule rides ``cog_before_invoke``, which discord.py runs for prefix AND
  hybrid-app invocations, so it reaches every ``ctx.send`` in the package
  without a call site having to remember;
* a caller that MEANS to notify still wins (partial keywords are overridable);
* the component paths that re-post a shared command payload (the hub's search /
  browse / stats buttons) go through ``replies.no_ping``, because a button
  callback never runs through a command invocation.

Offline: cogs are built with ``__new__`` and fed hand-rolled fakes.
"""

import importlib
import inspect
import pkgutil
import types

import discord
import pytest
from discord.ext import commands

import cogs.anilist as anilist_pkg
from cogs.anilist import replies
from cogs.anilist.base import AniListBase

# --- Fakes ------------------------------------------------------------------


class _Ctx:
    """The two things the hook and the reply need: an author and a send."""

    def __init__(self, author_id=1):
        self.author = types.SimpleNamespace(id=author_id, display_name="member")
        self.sends = []

    async def send(self, content=None, **kwargs):
        self.sends.append((content, kwargs))
        return types.SimpleNamespace(id=99)


def _package_cogs():
    """Every ``commands.Cog`` subclass this package actually loads."""

    found = {}
    modules = [anilist_pkg]
    for info in pkgutil.iter_modules(anilist_pkg.__path__):
        modules.append(importlib.import_module("cogs.anilist." + info.name))
    for module in modules:
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, commands.Cog)
                and obj is not commands.Cog
                and obj.__module__.startswith("cogs.anilist")
            ):
                found[obj.__qualname__] = obj
    return found


# --- The claim: one rule, and it covers the whole package -------------------


def test_the_package_really_ships_four_cogs_not_one_base():
    """The premise of the coverage test: a base-only fix would miss three cogs."""

    cogs = _package_cogs()
    assert set(cogs) == {
        "AniList",
        "AniListFeed",
        "AniListAiring",
        "AniListChapters",
    }
    # Only the composed cog is built on AniListBase - the three pollers are not,
    # which is precisely why the rule cannot live there.
    on_base = {name for name, cls in cogs.items() if issubclass(cls, AniListBase)}
    assert on_base == {"AniList"}


@pytest.mark.parametrize("name", sorted(_package_cogs()))
def test_every_anilist_cog_carries_the_no_ping_rule(name):
    """Remove the mixin from any one cog and this fails for that cog."""

    assert issubclass(_package_cogs()[name], replies.NoPingReplies)


def test_no_anilist_cog_silently_overrides_the_hook():
    """A cog defining its own ``cog_before_invoke`` would take the rule away."""

    for name, cls in _package_cogs().items():
        hook = cls.cog_before_invoke
        assert hook is replies.NoPingReplies.cog_before_invoke, name


# --- The rule itself --------------------------------------------------------


def test_the_bound_default_pings_nobody():
    assert replies.NO_PINGS.everyone is False
    assert replies.NO_PINGS.roles is False
    assert replies.NO_PINGS.users is False


async def test_the_hook_binds_the_suppression_onto_ctx_send():
    ctx = _Ctx()
    await replies.NoPingReplies().cog_before_invoke(ctx)
    await ctx.send("hello")
    assert ctx.sends[-1][1]["allowed_mentions"] is replies.NO_PINGS


async def test_a_caller_that_means_to_notify_still_wins():
    """partial keywords are OVERRIDABLE: the rule is a default, not a ceiling."""

    ctx = _Ctx()
    await replies.NoPingReplies().cog_before_invoke(ctx)
    mine = discord.AllowedMentions(users=True)
    await ctx.send("hello", allowed_mentions=mine)
    assert ctx.sends[-1][1]["allowed_mentions"] is mine


def test_the_hook_can_actually_be_bound_onto_a_real_context():
    """``commands.Context`` declares ``__slots__``; assignment must still work."""

    ctx = commands.Context.__new__(commands.Context)
    ctx.send = lambda **kwargs: kwargs
    assert ctx.send(a=1) == {"a": 1}


# --- The reply the finding named, end to end --------------------------------


def _media(media_id, title):
    return {
        "id": media_id,
        "title": {"romaji": title, "english": None},
        "type": "ANIME",
        "format": "TV",
        "isAdult": False,
        "episodes": 12,
        "status": "FINISHED",
        "startDate": {"year": 2020},
    }


async def test_the_search_echo_is_still_quoted_but_can_no_longer_ping():
    """The exact seam of the finding: ``_lookup_payload``'s multi-result content.

    The member's raw text is deliberately still there (an echo the member can
    read back is the point of the message); what changed is that the send it
    rides on cannot resolve the mention it carries.
    """

    cog = anilist_pkg.AniList.__new__(anilist_pkg.AniList)

    async def _graphql(query, variables, token=None):
        return {"data": {"Page": {"media": [_media(1, "A"), _media(2, "B")]}}}

    async def _get_token(user_id):
        return None

    cog._graphql = _graphql
    cog._get_token = _get_token

    ctx = _Ctx()
    await cog.cog_before_invoke(ctx)

    hostile = "<@1234567890123456>"
    kwargs, _view = await cog._lookup_payload(1, hostile, "ANIME")
    await ctx.send(**kwargs)

    content, sent = ctx.sends[-1]
    assert hostile in (content or sent.get("content") or "")
    assert sent["allowed_mentions"] is replies.NO_PINGS


# --- The component path the hook cannot reach -------------------------------


def test_no_ping_applies_the_rule_to_a_payload_dict():
    assert replies.no_ping({"content": "x"})["allowed_mentions"] is replies.NO_PINGS


def test_no_ping_never_overrides_a_caller_that_already_said_so():
    mine = discord.AllowedMentions(users=True)
    assert replies.no_ping({"allowed_mentions": mine})["allowed_mentions"] is mine


def test_every_hub_payload_repost_goes_through_no_ping():
    """The hub's buttons re-post ``_lookup_payload`` / ``_browse_payload`` /
    ``_profile_payload`` kwargs through an interaction, where the cog hook never
    ran. Drop the ``no_ping(...)`` wrapper from any of them and this fails."""

    from cogs.anilist import hub

    source = inspect.getsource(hub)
    assert "followup.send(**kwargs)" not in source
    assert source.count("followup.send(**no_ping(kwargs))") == 3


def test_no_hub_followup_can_ping_including_the_view_only_one():
    """The whole seam, not just the payload reposts.

    ``_HubListButton`` posts a ``CollectionView`` whose TextDisplays are built
    from AniList media titles, and it sends ``view=`` only - no payload dict to
    hand to ``no_ping``. A followup that names no ``allowed_mentions`` inherits
    the client default (``users=True``), so that path could still ping. This
    walks the module's AST: every ``followup.send`` must either carry an
    explicit ``allowed_mentions`` or ride a ``no_ping(...)`` payload.
    """

    import ast

    from cogs.anilist import hub

    tree = ast.parse(inspect.getsource(hub))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "send"):
            continue
        if not (
            isinstance(func.value, ast.Attribute) and func.value.attr == "followup"
        ):
            continue
        checked += 1
        explicit = any(kw.arg == "allowed_mentions" for kw in node.keywords)
        wrapped = any(
            kw.arg is None
            and isinstance(kw.value, ast.Call)
            and isinstance(kw.value.func, ast.Name)
            and kw.value.func.id == "no_ping"
            for kw in node.keywords
        )
        assert explicit or wrapped, (
            "hub followup.send at line %d can still ping" % node.lineno
        )
    assert checked >= 4


async def test_the_my_list_button_posts_a_view_that_cannot_ping():
    """Driven for real: the button's callback, with the payload seam faked.

    Remove ``allowed_mentions=NO_PINGS`` from ``_HubListButton.callback`` and
    the send arrives with no mention rule at all, which is the client default.
    """

    from cogs.anilist import hub

    sent = {}

    class _Followup:
        async def send(self, **kwargs):
            sent.update(kwargs)
            return types.SimpleNamespace(id=7)

    class _Response:
        async def defer(self, *args, **kwargs):
            return None

    view = types.SimpleNamespace(message=None)

    async def _collection_payload(user_id, kind, status, allow_adult):
        return None, view

    cog = types.SimpleNamespace(_collection_payload=_collection_payload)
    button = hub._HubListButton.__new__(hub._HubListButton)
    button._hub = types.SimpleNamespace(cog=cog)

    interaction = types.SimpleNamespace(
        response=_Response(),
        followup=_Followup(),
        user=types.SimpleNamespace(id=1),
        channel=None,
    )
    await button.callback(interaction)

    assert sent["view"] is view
    assert sent["allowed_mentions"] is replies.NO_PINGS
    assert view.message is not None
