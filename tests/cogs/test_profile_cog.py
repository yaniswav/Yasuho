"""Tests for the profile cog surface (cogs/community/profile/cog.py).

The commands kept their names and their shape, so the risk this file covers is
behavioural: does `set` route each field to the right single-column write, does
`view` actually APPLY the visibility rules (the owner sees their unpublished
fields, another member does not), and does a rejected value produce a message
that names the cap instead of a traceback.

`view` (and the bare group) now sends the Components V2 card from
cogs/community/profile/views.py instead of an embed (this lot's P2
conversion); the tests below check that the right VIEW is sent with the right
content, while the deep per-visibility-level rendering matrix and the
worst-case Components V2 budget are covered directly against the card in
tests/cogs/test_profile_views.py, not duplicated here.

Offline: the storage seam is monkeypatched, so no database is involved.
"""

import types
import typing

import discord
import pytest
from discord.ext.commands.view import StringView

from cogs.community.profile import registry, storage, visibility
from cogs.community.profile.cog import Profiles
from cogs.community.profile.connectors import storage as connectors_storage

OWNER = 111
FRIEND = 222


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Member:
    def __init__(self, user_id, name):
        self.id = user_id
        self.display_name = name
        self.display_avatar = types.SimpleNamespace(url="https://cdn/avatar.png")


class _Ctx:
    def __init__(self, author):
        self.author = author
        self.guild = types.SimpleNamespace(id=9)
        self.interaction = None
        self.invoked_subcommand = None
        self.clean_prefix = "?"
        self.sends = []
        self.message = types.SimpleNamespace(attachments=[])

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


def _cog(pool=None):
    cog = Profiles(types.SimpleNamespace(db_pool=pool or object()))
    # What Cog._inject does when the bot loads the extension. Without it the
    # group's delegation to `self.profile_view(...)` would call the callback
    # without a cog, which is a test artefact, not a production shape.
    for command in cog.__cog_commands__:
        command.cog = cog
    return cog


def _last_embed(ctx):
    return ctx.sends[-1][1]["embed"]


def _last_view(ctx):
    return ctx.sends[-1][1]["view"]


def _last_text(ctx):
    args, kwargs = ctx.sends[-1]
    return args[0] if args else kwargs.get("content")


def _walk(node):
    """Every nested Components V2 payload dict inside a to_components() tree."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (list, dict)):
                yield from _walk(value)


def _card_text(view):
    """Every TextDisplay's content in a rendered card, joined by newlines."""
    return "\n".join(
        node["content"] for node in _walk(view.to_components()) if node.get("type") == 10
    )


def _card_accent(view):
    """The card Container's accent_color, or None."""
    for node in _walk(view.to_components()):
        if node.get("type") == 17:
            return node.get("accent_color")
    return None


@pytest.fixture
def owner():
    return _Member(OWNER, "Owner")


@pytest.fixture
def friend():
    return _Member(FRIEND, "Friend")


async def _invoke_through_the_parser(cog, attribute, ctx, argument_string):
    """Run the REAL prefix parser, then the callback it prepared.

    Calling the callback directly proves the helper, not the command: whether
    `?profile set bio` (no value at all) even reaches the callback is decided by
    discord.py's argument parser, so the clearing path has to be driven through
    it. The command is taken off the INSTANCE, because Cog.__new__ copies every
    command and only the copy carries the cog the parser prepends.
    """
    command = getattr(cog, attribute)
    ctx.command = command
    ctx.view = StringView(argument_string)
    await command._parse_arguments(ctx)
    return await command.callback(*ctx.args, **ctx.kwargs)


@pytest.fixture
def writes(monkeypatch):
    """Record every storage write instead of performing it."""
    recorded = {"fields": [], "gaming": [], "deleted": [], "visibility": []}

    async def set_field(pool, user_id, name, value):
        # Validate first, exactly like the real seam: a rejected value must
        # leave no trace of a write.
        stored = registry.normalise(name, value)
        recorded["fields"].append((user_id, name, value))
        return stored

    async def set_gaming_id(pool, user_id, key, value):
        stored = registry.normalise("gaming_ids", {key: value}).get(key)
        recorded["gaming"].append((user_id, key, value))
        return stored

    async def delete_profile(pool, user_id):
        recorded["deleted"].append(user_id)
        return {"user_profiles": 1}

    async def set_visibility(pool, user_id, name, level):
        recorded["visibility"].append((user_id, name, level))
        return level

    async def get_visibility(pool, user_id):
        # Nothing published: the state a brand-new profile is really in.
        return {}

    monkeypatch.setattr(storage, "set_field", set_field)
    monkeypatch.setattr(storage, "set_gaming_id", set_gaming_id)
    monkeypatch.setattr(storage, "delete_profile", delete_profile)
    monkeypatch.setattr(storage, "set_visibility", set_visibility)
    monkeypatch.setattr(storage, "get_visibility", get_visibility)
    return recorded


@pytest.fixture
def reads(monkeypatch):
    """Serve one profile + visibility map + connection list to `profile view`.

    The third read is what makes a "Linked" badge true (see views.py): the card
    draws a connector section only for a row that really exists, so the fixture
    serves the same three reads the command performs.
    """
    state = {"profile": None, "visibility": {}, "connections": []}

    async def get_profile(pool, user_id):
        return state["profile"]

    async def get_visibility(pool, user_id):
        return state["visibility"]

    async def get_connections(pool, user_id):
        return state["connections"]

    monkeypatch.setattr(storage, "get_profile", get_profile)
    monkeypatch.setattr(storage, "get_visibility", get_visibility)
    monkeypatch.setattr(connectors_storage, "get_connections", get_connections)
    return state


# ---------------------------------------------------------------------------
# apply_field: the routing seam shared by `set` and the modal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", registry.GAMING_ID_KEYS)
async def test_gaming_keys_go_to_the_merging_setter(writes, key):
    label, shown = await _cog().apply_field(OWNER, key, "value-1")
    assert writes["gaming"] == [(OWNER, key, "value-1")]
    assert writes["fields"] == []
    assert (label, shown) == (registry.GAMING_ID_LABELS[key], "value-1")


@pytest.mark.parametrize("name", ("bio", "pronouns", "accent"))
async def test_socle_fields_go_to_the_single_column_setter(writes, name):
    value = "#5865F2" if name == "accent" else "hello"
    label, shown = await _cog().apply_field(OWNER, name, value)
    assert writes["fields"] == [(OWNER, name, value)]
    assert writes["gaming"] == []
    assert label == registry.get(name).label
    assert shown


async def test_the_confirmation_echoes_what_was_stored_not_what_was_typed(writes):
    """The registry trims text and packs a colour; confirming the raw input
    would claim the profile holds something it does not."""
    assert (await _cog().apply_field(OWNER, "accent", "#58f"))[1] == "#5588FF"
    assert (await _cog().apply_field(OWNER, "bio", "  spaced  "))[1] == "spaced"
    assert (await _cog().apply_field(OWNER, "switch", " SW-1 "))[1] == "SW-1"


async def test_omitting_the_value_clears_the_field_through_the_real_parser(
    writes, owner
):
    """`?profile set bio` with nothing after it must reach the callback.

    A required consume-rest parameter raises MissingRequiredArgument before the
    callback runs, which would leave `profile clear` (erasing EVERYTHING) as the
    only way to empty one field.
    """
    ctx = _Ctx(owner)
    await _invoke_through_the_parser(_cog(), "profile_set", ctx, "bio")
    assert writes["fields"] == [(OWNER, "bio", None)]
    assert "Cleared" in _last_text(ctx)
    assert not any("embed" in kwargs for _args, kwargs in ctx.sends)


@pytest.mark.parametrize("key", ("switch", "steam_id"))
async def test_omitting_the_value_clears_a_gaming_id_too(writes, owner, key):
    ctx = _Ctx(owner)
    await _invoke_through_the_parser(_cog(), "profile_set", ctx, key)
    assert writes["gaming"] == [(OWNER, key, None)]
    assert "Cleared" in _last_text(ctx)


async def test_the_parser_still_passes_a_multi_word_value(writes, owner):
    ctx = _Ctx(owner)
    await _invoke_through_the_parser(
        _cog(), "profile_set", ctx, "bio  hello there"
    )
    assert writes["fields"] == [(OWNER, "bio", "hello there")]


async def test_a_blank_value_clears_the_field(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="   ")
    assert writes["fields"] == [(OWNER, "bio", "   ")]
    assert "Cleared" in _last_text(ctx)
    assert not any("embed" in kwargs for _args, kwargs in ctx.sends)


async def test_field_names_are_case_insensitive_and_trimmed(writes):
    await _cog().apply_field(OWNER, "  BIO ", "hi")
    assert writes["fields"] == [(OWNER, "bio", "hi")]


@pytest.mark.parametrize("name", ("nope", "custom_fields", "anilist", ""))
async def test_unroutable_names_return_none_and_write_nothing(writes, name):
    assert await _cog().apply_field(OWNER, name, "x") is None
    assert writes == {
        "fields": [],
        "gaming": [],
        "deleted": [],
        "visibility": [],
    }


# ---------------------------------------------------------------------------
# profile set
# ---------------------------------------------------------------------------


async def test_set_confirms_with_the_field_label(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="hello")
    embed = _last_embed(ctx)
    assert embed.title
    assert embed.fields[0].value == "hello"


async def test_set_lists_every_choice_for_an_unknown_field(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "epic", value="x")
    message = _last_text(ctx)
    for choice in registry.GAMING_ID_KEYS + ("bio", "pronouns", "accent"):
        assert choice in message


async def test_set_names_the_cap_that_was_exceeded(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="x" * 301)
    assert "300" in _last_text(ctx)
    assert writes["fields"] == []


async def test_set_explains_a_bad_colour(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "accent", value="chartreuse")
    assert "#5865F2" in _last_text(ctx)


async def test_set_degrades_politely_when_the_database_fails(monkeypatch, owner):
    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "set_field", boom)
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="hi")
    assert "later" in _last_text(ctx)


# ---------------------------------------------------------------------------
# profile view: visibility enforced for real
# ---------------------------------------------------------------------------


def _profile(**fields):
    base = {
        "user_id": OWNER,
        "bio": None,
        "pronouns": None,
        "accent": None,
        "custom_fields": [],
        "gaming_ids": {},
    }
    base.update(fields)
    return base


async def test_view_reports_nothing_when_the_user_has_no_profile(reads, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert "no profile" in _last_text(ctx)


async def test_the_owner_sees_their_own_unpublished_fields(reads, owner):
    reads["profile"] = _profile(bio="my secret bio")
    reads["visibility"] = {}
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert "my secret bio" in _card_text(_last_view(ctx))
    assert "embed" not in ctx.sends[-1][1]


async def test_another_member_does_not_see_an_unpublished_field(reads, friend, owner):
    reads["profile"] = _profile(bio="my secret bio")
    reads["visibility"] = {}
    ctx = _Ctx(friend)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert "no profile" in _last_text(ctx)
    assert not any("embed" in kwargs for _args, kwargs in ctx.sends)
    assert not any("view" in kwargs for _args, kwargs in ctx.sends)


async def test_another_member_sees_a_server_published_field(reads, friend, owner):
    reads["profile"] = _profile(bio="hello", pronouns="she/her")
    reads["visibility"] = {"bio": "server"}
    ctx = _Ctx(friend)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    text = _card_text(_last_view(ctx))
    assert "hello" in text
    assert "she/her" not in text  # pronouns was never published


async def test_the_accent_becomes_the_card_colour_when_visible(reads, owner):
    reads["profile"] = _profile(accent=0x5865F2, bio="hi")
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert _card_accent(_last_view(ctx)) == 0x5865F2


async def test_a_hidden_accent_does_not_colour_someone_elses_view(reads, friend, owner):
    reads["profile"] = _profile(accent=0x5865F2, bio="hi")
    reads["visibility"] = {"bio": "public"}
    ctx = _Ctx(friend)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert _card_accent(_last_view(ctx)) != 0x5865F2


async def test_gaming_ids_and_custom_fields_render_in_registry_order(reads, owner):
    reads["profile"] = _profile(
        pronouns="she/her",
        gaming_ids={"steam_id": "steam-1", "switch": "SW-1"},
        custom_fields=[{"label": "Fav game", "value": "Persona 5"}],
    )
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    text = _card_text(_last_view(ctx))
    # Registry order: pronouns (header), then custom fields, then gaming IDs
    # (Switch before Steam, GAMING_ID_KEYS order).
    assert (
        text.index("she/her")
        < text.index("Fav game")
        < text.index("Switch Friend Code")
        < text.index("Steam ID")
    )


async def test_view_defaults_to_the_caller(reads, owner):
    reads["profile"] = _profile(bio="hi")
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, None)
    assert "Owner" in _card_text(_last_view(ctx))


async def test_the_bare_group_shows_the_callers_profile(reads, owner):
    reads["profile"] = _profile(bio="hi")
    ctx = _Ctx(owner)
    await Profiles.profile.callback(_cog(), ctx)
    assert "hi" in _card_text(_last_view(ctx))


async def test_view_sends_with_mentions_suppressed(reads, owner):
    """Bio/custom-field/gaming-ID text is owner-typed and, unlike an embed, a
    Components V2 TextDisplay DOES get parsed for mentions - so the card must
    never be sent without AllowedMentions.none()."""
    reads["profile"] = _profile(bio="hi")
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    sent = ctx.sends[-1][1]["allowed_mentions"]
    assert sent.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_the_no_profile_answer_also_suppresses_mentions(reads, owner):
    """That branch is plain message CONTENT and interpolates a NICKNAME, which
    its owner controls: "@everyone" as a nickname would really ping."""
    ctx = _Ctx(_Member(OWNER, "@everyone"))
    await Profiles.profile_view.callback(_cog(), ctx, ctx.author)
    assert "no profile" in _last_text(ctx)
    sent = ctx.sends[-1][1]["allowed_mentions"]
    assert sent.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_the_panel_defers_before_reading_and_binds_its_message(reads, owner):
    """`ctx.typing()` is what defers the slash interaction here; without it the
    database round-trip can eat the 3-second window and lose the panel."""
    typings = []
    ctx = _Ctx(owner)
    real_typing = ctx.typing

    def _record():
        typings.append(True)
        return real_typing()

    ctx.typing = _record

    await Profiles.profile_panel.callback(_cog(), ctx)

    assert typings  # the read happened inside the typing/defer window
    view = _last_view(ctx)
    assert view.author_id == OWNER
    assert view.message is not None  # bound, so on_timeout can disable it


async def test_publishing_a_section_you_never_linked_shows_no_card(reads, owner):
    """Turning every section on in the panel is a choice about an audience, not
    an account: with nothing linked and nothing stored, `view` must fall back to
    "no profile" rather than send seven "Linked" badges over nothing."""
    reads["visibility"] = {name: "public" for name in registry.FIELD_NAMES}
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert "no profile" in _last_text(ctx)


async def test_a_section_is_badged_only_once_it_is_really_linked(reads, owner):
    reads["profile"] = _profile(bio="hi")
    reads["visibility"] = {"steam": "public", "anilist": "public"}
    reads["connections"] = [
        {
            "connector": "steam",
            "external_id": "76561198000000000",
            "display_name": "Yanis",
            "linked_at": None,
            "last_refresh": None,
            "payload": {},
        }
    ]
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    text = _card_text(_last_view(ctx))
    assert "Steam" in text
    assert "AniList" not in text


async def test_a_normal_profile_carries_no_truncation_footer(reads, owner):
    reads["profile"] = _profile(bio="hi", gaming_ids={"switch": "SW-1"})
    ctx = _Ctx(owner)
    await Profiles.profile_view.callback(_cog(), ctx, owner)
    assert "too long to show in full" not in _card_text(_last_view(ctx))


# ---------------------------------------------------------------------------
# profile visibility: the write path is not a dead end
# ---------------------------------------------------------------------------


async def test_set_tells_the_owner_nobody_can_see_the_field_yet(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="hello")
    assert "?profile visibility bio server" in _last_embed(ctx).footer.text


async def test_the_hint_names_the_shared_gaming_ids_section(writes, owner):
    """The five gamer IDs are published together, so the hint says so."""
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "switch", value="SW-1")
    assert "gaming_ids" in _last_embed(ctx).footer.text


async def test_no_hint_once_the_section_is_published(writes, monkeypatch, owner):
    async def get_visibility(pool, user_id):
        return {"bio": "server"}

    monkeypatch.setattr(storage, "get_visibility", get_visibility)
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="hello")
    assert _last_embed(ctx).footer.text is None


async def test_a_failing_visibility_read_never_costs_the_confirmation(
    writes, monkeypatch, owner
):
    async def boom(pool, user_id):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "get_visibility", boom)
    ctx = _Ctx(owner)
    await Profiles.profile_set.callback(_cog(), ctx, "bio", value="hello")
    assert _last_embed(ctx).fields[0].value == "hello"


@pytest.mark.parametrize("level", ("public", "server", "PRIVATE"))
async def test_visibility_publishes_one_section(writes, owner, level):
    ctx = _Ctx(owner)
    await Profiles.profile_visibility.callback(_cog(), ctx, "gaming_ids", level)
    assert writes["visibility"] == [(OWNER, "gaming_ids", level.lower())]
    assert "Gaming IDs" in _last_text(ctx)


async def test_visibility_lists_the_sections_for_an_unknown_one(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_visibility.callback(_cog(), ctx, "anilist", "server")
    assert writes["visibility"] == []
    for choice in ("bio", "pronouns", "accent", "custom_fields", "gaming_ids"):
        assert choice in _last_text(ctx)


async def test_visibility_lists_the_levels_for_an_unknown_one(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_visibility.callback(_cog(), ctx, "bio", "friends")
    assert writes["visibility"] == []
    for level in ("public", "server", "private"):
        assert level in _last_text(ctx)


def test_visibility_offers_real_slash_choices_pinned_to_the_registry():
    """The Literal annotations ARE the slash choices, so they must not drift from
    the registry / the level enum they mirror - and their values must stay the
    plain strings storage writes, not an enum object."""
    hints = typing.get_type_hints(Profiles.profile_visibility.callback)
    assert typing.get_args(hints["section"]) == registry.STORED_NAMES
    assert typing.get_args(hints["level"]) == visibility.LEVELS

    parameters = {
        parameter.name: parameter
        for parameter in Profiles.profile.app_command.get_command(
            "visibility"
        ).parameters
    }
    for name, expected in (
        ("section", registry.STORED_NAMES),
        ("level", visibility.LEVELS),
    ):
        choices = parameters[name].choices
        assert tuple(choice.value for choice in choices) == expected
        assert all(isinstance(choice.value, str) for choice in choices)
        # Discord refuses more than 25 choices per option; the P2 panel is the
        # answer if the registry ever grows past that, not a silent truncation.
        assert len(choices) <= 25


async def test_visibility_degrades_politely_when_the_database_fails(
    monkeypatch, owner
):
    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "set_visibility", boom)
    ctx = _Ctx(owner)
    await Profiles.profile_visibility.callback(_cog(), ctx, "bio", "public")
    assert "later" in _last_text(ctx)


# ---------------------------------------------------------------------------
# profile clear
# ---------------------------------------------------------------------------


async def test_clear_erases_the_whole_profile(writes, owner):
    ctx = _Ctx(owner)
    await Profiles.profile_clear.callback(_cog(), ctx)
    assert writes["deleted"] == [OWNER]
    assert _last_embed(ctx).title


async def test_clear_degrades_politely_when_the_database_fails(monkeypatch, owner):
    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(storage, "delete_profile", boom)
    ctx = _Ctx(owner)
    await Profiles.profile_clear.callback(_cog(), ctx)
    assert "later" in _last_text(ctx)


# ---------------------------------------------------------------------------
# The cog keeps the contract the rest of the bot depends on
# ---------------------------------------------------------------------------


def test_the_slash_command_names_are_unchanged():
    """Renaming any of these would force a command re-sync in production.

    `visibility` and `panel` are the additions this lot and its predecessor
    make (each IS a re-sync); `presence` is P5's, and it lives on THIS group
    rather than on `connections` because the two presence sections are not
    handle-linkable. Every pre-existing name must still be here.
    """
    assert Profiles.profile.name == "profile"
    assert {command.name for command in Profiles.profile.commands} == {
        "view",
        "set",
        "edit",
        "panel",
        "clear",
        "visibility",
        "presence",
    }


def test_the_cog_keeps_its_help_category_name():
    from cogs.system import help as help_mod

    assert Profiles.__cog_name__ == "Profiles"
    claimed = {name for _e, _n, _d, names in help_mod.CATEGORIES for name in names}
    assert "Profiles" in claimed
