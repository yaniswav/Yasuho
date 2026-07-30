"""Tests for the profile Components V2 surfaces (cogs/community/profile/views.py).

Covers what cogs/community/profile/cog.py's own tests deliberately do NOT
duplicate (see tests/cogs/test_profile_cog.py's module docstring):

* :func:`build_profile_card` / :class:`ProfileCard` rendered per visibility
  level for three distinct viewers (the owner, a friend who shares a server,
  and a stranger who does not);
* the connector-section seam - a section is drawn only when the viewer may see
  it AND the owner really has a connection row, a linked one with no registered
  renderer shows the "Linked" badge, and a registered (async) renderer is
  awaited instead, with the connection row and the remaining budget;
* the worst-case Components V2 budget (40 components / 4000 characters),
  measured against the real ``to_components()`` payload, plus the truncation
  footer when the budget is deliberately forced too small;
* :class:`ProfileVisibilityPanel` - the visibility set/roundtrip, the
  author-gate and locale-resolution every ``AuthorLayoutView`` shares, and the
  gaming-ID edit button reusing the existing modal.

Offline throughout: no real database, no network, no live Discord gateway.
"""

import types

import discord
import pytest

from conftest import Record

from cogs.community.profile import registry, views, visibility

OWNER = 111
FRIEND = 222
STRANGER = 333


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Member:
    def __init__(self, user_id, name):
        self.id = user_id
        self.display_name = name
        self.display_avatar = types.SimpleNamespace(url="https://cdn/avatar.png")


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


def _connections(*names):
    """The rows ``connectors.storage.get_connections`` would return.

    Shaped like the real thing (payload already decoded), because that row is
    now part of the renderer contract, not just a presence flag.
    """
    return [
        {
            "connector": name,
            "external_id": f"{name}-42",
            "display_name": f"{name} handle",
            "linked_at": None,
            "last_refresh": None,
            "payload": {},
        }
        for name in names
    ]


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


def _real_components(view):
    """Every dict that is an actual API component (carries a ``type`` key) -
    excludes SelectOption entries, which nest under a select's ``options`` but
    are not a component of their own."""
    return [node for node in _walk(view.to_components()) if "type" in node]


def _texts(view):
    return [node["content"] for node in _real_components(view) if node["type"] == 10]


def _accent(view):
    for node in _real_components(view):
        if node["type"] == 17:
            return node.get("accent_color")
    return None


# ---------------------------------------------------------------------------
# ProfileCard: rendered per visibility level, for three distinct viewers
# ---------------------------------------------------------------------------


async def test_owner_sees_their_own_unpublished_fields():
    profile = _profile(bio="secret bio")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    assert "secret bio" in "\n".join(_texts(card))


async def test_friend_who_shares_a_guild_sees_server_level_fields():
    profile = _profile(bio="hello")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=FRIEND, shares_guild=True)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"), profile, {"bio": "server"}, viewer, []
    )
    assert "hello" in "\n".join(_texts(card))


async def test_friend_does_not_see_a_private_field():
    profile = _profile(bio="hello")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=FRIEND, shares_guild=True)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    assert card is None


async def test_stranger_without_a_shared_guild_only_sees_public_fields():
    profile = _profile(bio="hello", pronouns="she/her")
    viewer = visibility.ViewerContext(
        owner_id=OWNER, viewer_id=STRANGER, shares_guild=False
    )
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"bio": "public", "pronouns": "server"},
        viewer,
        [],
    )
    text = "\n".join(_texts(card))
    assert "hello" in text
    assert "she/her" not in text  # server-level, and this viewer shares no guild


async def test_stranger_sees_nothing_when_nothing_is_public():
    profile = _profile(bio="hello")
    viewer = visibility.ViewerContext(
        owner_id=OWNER, viewer_id=STRANGER, shares_guild=False
    )
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    assert card is None


async def test_no_profile_at_all_yields_no_card_for_any_viewer():
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), None, {}, viewer, [])
    assert card is None


async def test_the_accent_becomes_the_containers_colour():
    profile = _profile(bio="hi", accent=0x5865F2)
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    assert _accent(card) == 0x5865F2


async def test_header_carries_the_display_name_and_pronouns():
    profile = _profile(bio="hi", pronouns="she/her")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Cool Owner"), profile, {}, viewer, []
    )
    text = "\n".join(_texts(card))
    assert "Cool Owner" in text
    assert "she/her" in text


async def test_the_card_is_static_so_it_neither_times_out_nor_is_stored():
    """No button, no select: discord.py's ``is_dispatchable`` is what decides
    both the view store and the timeout task, and it is False here - a timeout
    would be a timer over nothing, and there is no message to bind."""
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    assert card.is_dispatchable() is False
    assert card.timeout is None
    assert not hasattr(card, "message")


# ---------------------------------------------------------------------------
# Connector sections: the data-driven render_sections seam
# ---------------------------------------------------------------------------


def _clear_renderers(monkeypatch):
    monkeypatch.setattr(views, "SECTION_RENDERERS", {})


async def test_a_published_but_unlinked_section_is_dropped_not_badged(monkeypatch):
    """THE badge-truth guard. A visibility row says who may look, never that
    an account exists: publishing every section from the panel must not print
    seven "Linked" badges for somebody with no connection at all."""
    _clear_renderers(monkeypatch)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {name: "public" for name in registry.FIELD_NAMES},
        viewer,
        [],  # nothing linked
    )
    text = "\n".join(_texts(card))
    assert "Linked" not in text
    for name in registry.FIELD_NAMES:
        field = registry.get(name)
        if not field.stored:
            assert field.label not in text


async def test_a_presence_section_is_never_badged_before_p5(monkeypatch):
    """Nobody CAN link `presence_gaming` / `spotify_presence` (they are not in
    base.LINKABLE), so until P5 writes their marker rows there is no connection
    and the card says nothing about them - published or not."""
    _clear_renderers(monkeypatch)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"presence_gaming": "public", "spotify_presence": "public"},
        viewer,
        [],
    )
    text = "\n".join(_texts(card))
    assert "Now playing" not in text
    assert "Spotify" not in text


async def test_a_user_with_no_profile_and_no_connection_gets_no_card(monkeypatch):
    """... so the cog falls back to its "has no profile set" line instead of
    sending a card made of nothing but published-and-empty sections."""
    _clear_renderers(monkeypatch)
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        None,
        {name: "public" for name in registry.FIELD_NAMES},
        viewer,
        [],
    )
    assert card is None


async def test_a_linked_connector_without_a_renderer_shows_the_linked_badge(monkeypatch):
    _clear_renderers(monkeypatch)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public"},
        viewer,
        _connections("anilist"),
    )
    text = "\n".join(_texts(card))
    assert "AniList" in text
    assert "Linked" in text


async def test_a_linked_section_with_no_visibility_row_stays_owner_only(monkeypatch):
    """Linking publishes nothing: an absent row is private, and private still
    means "the owner sees it, nobody else does"."""
    _clear_renderers(monkeypatch)
    profile = _profile(bio="hi")
    connections = _connections("steam")
    owner_card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"bio": "public"},
        visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False),
        connections,
    )
    friend_card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"bio": "public"},
        visibility.ViewerContext(owner_id=OWNER, viewer_id=FRIEND, shares_guild=True),
        connections,
    )
    assert "Steam" in "\n".join(_texts(owner_card))
    assert "Steam" not in "\n".join(_texts(friend_card))


async def test_a_registered_renderer_is_awaited_with_its_connection_row(monkeypatch):
    """The FINAL P4 contract: async, handed the row the card already read (so
    the renderer opens no connection of its own) and the room left."""
    _clear_renderers(monkeypatch)
    calls = []

    async def _fake_renderer(container, field, viewer, connection, budget):
        calls.append((field.name, viewer, connection, budget))
        container.add_item(discord.ui.TextDisplay("custom AniList block"))

    views.register_section_renderer("anilist", _fake_renderer)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public"},
        viewer,
        _connections("anilist"),
    )
    text = "\n".join(_texts(card))
    assert "custom AniList block" in text
    assert "Linked" not in text
    assert len(calls) == 1
    name, seen_viewer, connection, budget = calls[0]
    assert name == "anilist"
    assert seen_viewer is viewer
    assert connection["external_id"] == "anilist-42"
    assert connection["payload"] == {}
    assert isinstance(budget, views.SectionBudget)
    assert 0 < budget.text <= views.CARD_TEXT_BUDGET
    assert 0 < budget.components <= views.CARD_COMPONENT_CAP


def test_register_section_renderer_refuses_an_unknown_section(monkeypatch):
    _clear_renderers(monkeypatch)
    try:
        views.register_section_renderer("not-a-real-section", lambda *a: None)
        raised = False
    except registry.UnknownField:
        raised = True
    assert raised


async def test_a_renderer_that_raises_falls_back_to_the_badge(monkeypatch, caplog):
    """One connector written in another lot must never take a whole profile
    down: the section degrades to the badge, the rest of the card survives."""
    _clear_renderers(monkeypatch)

    async def _broken(container, field, viewer, connection, budget):
        container.add_item(discord.ui.TextDisplay("half a section"))
        raise RuntimeError("connector API is down")

    views.register_section_renderer("anilist", _broken)
    profile = _profile(bio="still here")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public"},
        viewer,
        _connections("anilist"),
    )
    text = "\n".join(_texts(card))
    assert "still here" in text  # the rest of the card rendered
    assert "half a section" not in text  # the partial output was rolled back
    assert "Linked" in text  # ... and replaced by the badge


async def test_a_renderer_that_is_not_async_degrades_to_the_badge(monkeypatch):
    """The seam awaits: a P4 module that forgets `async def` is a broken
    renderer like any other, not a crash in the middle of a card."""
    _clear_renderers(monkeypatch)

    def _sync(container, field, viewer, connection, budget):
        container.add_item(discord.ui.TextDisplay("sync output"))

    views.register_section_renderer("anilist", _sync)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public"},
        viewer,
        _connections("anilist"),
    )
    text = "\n".join(_texts(card))
    assert "sync output" not in text
    assert "Linked" in text


async def test_a_renderer_that_shows_nothing_leaves_no_dangling_separator(monkeypatch):
    _clear_renderers(monkeypatch)

    async def _silent(container, field, viewer, connection, budget):
        return None

    views.register_section_renderer("anilist", _silent)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    with_section = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public"},
        viewer,
        _connections("anilist"),
    )
    without = await views.build_profile_card(
        _Member(OWNER, "Owner"), profile, {}, viewer, []
    )
    assert len(_real_components(with_section)) == len(_real_components(without))


async def test_an_enormous_renderer_is_dropped_instead_of_blowing_the_budget(monkeypatch):
    """A connector renderer's output is not knowable in advance, so it is
    charged AFTER the fact and rolled back whole when it does not fit."""
    _clear_renderers(monkeypatch)

    async def _huge(container, field, viewer, connection, budget):
        container.add_item(discord.ui.TextDisplay("x" * views.CARD_TEXT_BUDGET))

    views.register_section_renderer("anilist", _huge)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public"},
        viewer,
        _connections("anilist"),
    )
    components = _real_components(card)
    text_total = sum(len(node["content"]) for node in components if node["type"] == 10)
    assert text_total <= views.CARD_TEXT_BUDGET
    assert len(components) <= views.CARD_COMPONENT_CAP
    assert "x" * 100 not in "\n".join(_texts(card))
    assert "too long to show in full" in "\n".join(_texts(card))


async def test_renderers_cannot_push_the_card_past_the_component_cap(monkeypatch):
    _clear_renderers(monkeypatch)

    async def _greedy(container, field, viewer, connection, budget):
        for index in range(20):
            container.add_item(discord.ui.TextDisplay(f"row {index}"))

    connector_names = [
        name for name in registry.FIELD_NAMES if not registry.get(name).stored
    ]
    for name in connector_names:
        views.register_section_renderer(name, _greedy)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {name: "public" for name in registry.FIELD_NAMES},
        viewer,
        _connections(*connector_names),
    )
    assert len(_real_components(card)) <= views.CARD_COMPONENT_CAP


async def test_the_budget_handed_to_a_renderer_shrinks_as_the_card_fills(monkeypatch):
    """Each section is told what is ACTUALLY left, not a fresh full budget -
    the Components V2 ceiling is per message, not per section."""
    _clear_renderers(monkeypatch)
    seen = []

    async def _measured(container, field, viewer, connection, budget):
        seen.append(budget)
        container.add_item(discord.ui.TextDisplay("z" * 200))

    for name in ("anilist", "steam"):
        views.register_section_renderer(name, _measured)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public", "steam": "public"},
        viewer,
        _connections("anilist", "steam"),
    )
    assert len(seen) == 2
    assert seen[1].text < seen[0].text
    assert seen[1].components < seen[0].components


async def test_render_sections_will_not_mint_its_own_budget():
    """It has no way to know what the rest of the card already spent, so the
    caller must hand its own budget over."""
    with pytest.raises(TypeError):
        await views.render_sections(discord.ui.Container(), [], None, {})


async def test_a_friend_does_not_see_a_privately_published_connector(monkeypatch):
    _clear_renderers(monkeypatch)
    profile = _profile(bio="hi")
    owner_viewer = visibility.ViewerContext(
        owner_id=OWNER, viewer_id=OWNER, shares_guild=False
    )
    friend_viewer = visibility.ViewerContext(
        owner_id=OWNER, viewer_id=FRIEND, shares_guild=True
    )
    visibility_map = {"bio": "public", "steam": "private"}
    connections = _connections("steam")
    owner_card = await views.build_profile_card(
        _Member(OWNER, "Owner"), profile, visibility_map, owner_viewer, connections
    )
    friend_card = await views.build_profile_card(
        _Member(OWNER, "Owner"), profile, visibility_map, friend_viewer, connections
    )
    # An explicit "private" row for a connector still shows to its OWNER (same
    # rule as a private stored field) but never to anyone else.
    assert "Steam" in "\n".join(_texts(owner_card))
    assert "Steam" not in "\n".join(_texts(friend_card))


async def test_a_malformed_connection_row_costs_one_section_not_the_card(monkeypatch):
    _clear_renderers(monkeypatch)
    profile = _profile(bio="hi")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        profile,
        {"anilist": "public", "steam": "public"},
        viewer,
        [object(), {"nope": 1}] + _connections("steam"),
    )
    text = "\n".join(_texts(card))
    assert "hi" in text
    assert "Steam" in text
    assert "AniList" not in text


# ---------------------------------------------------------------------------
# Components V2 budget: worst case, measured via to_components()
# ---------------------------------------------------------------------------


def _maxed_profile():
    return _profile(
        bio="b" * registry.BIO_MAX,
        pronouns="p" * registry.PRONOUNS_MAX,
        accent=0x5865F2,
        custom_fields=[
            {
                "label": "l" * registry.CUSTOM_LABEL_MAX,
                "value": "v" * registry.CUSTOM_VALUE_MAX,
            }
            for _index in range(registry.CUSTOM_FIELDS_MAX)
        ],
        gaming_ids={
            key: key[0] * registry.GAMING_ID_MAX for key in registry.GAMING_ID_KEYS
        },
    )


async def test_a_maxed_out_profile_with_every_connector_stays_within_the_cv2_budget(
    monkeypatch,
):
    """Framework-only budget/truncation mechanics - the generic fallback badge,
    not whatever real content a P4 connector's own renderer happens to draw
    (every sibling test in this section isolates the same way, see
    _clear_renderers)."""
    _clear_renderers(monkeypatch)
    profile = _maxed_profile()
    visibility_map = {name: "public" for name in registry.FIELD_NAMES}
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(
        _Member(OWNER, "A Rather Long Display Name Indeed"),
        profile,
        visibility_map,
        viewer,
        _connections(
            *[name for name in registry.FIELD_NAMES if not registry.get(name).stored]
        ),
    )
    components = _real_components(card)
    text_total = sum(len(node["content"]) for node in components if node["type"] == 10)
    assert len(components) <= 40
    assert text_total <= views.CARD_TEXT_BUDGET
    # Every connector section is present (nothing silently dropped).
    linked_lines = [node["content"] for node in components if node["type"] == 10]
    assert sum(1 for text in linked_lines if "Linked" in text) == 7


def _rich_payloads():
    """A realistic, near-worst-case payload for every connector that ships a
    real renderer, shaped exactly like what its own ``_build_payload`` writes
    (clipped titles, three entries, an avatar)."""
    return {
        "anilist": {
            "avatar": "https://example.test/a.png",
            "anime_count": 1234,
            "anime_mean_score": 78.4,
            "anime_minutes_watched": 987654,
            "manga_count": 321,
            "manga_mean_score": 82.1,
            "manga_chapters_read": 54321,
            "favourite_anime": ["t" * 80, "t" * 80],
            "favourite_manga": ["t" * 80],
        },
        "steam": {
            "persona_name": "p" * 80,
            "avatar": "https://example.test/s.png",
            "private": False,
            "recent_games": [
                {"name": "g" * 80, "hours_2weeks": 12.5} for _index in range(3)
            ],
            "owned_games_count": 4321,
        },
        "osu": {
            "username": "u" * 15,
            "rank": 1234,
            "pp": 12345.6,
            "accuracy": 99.12,
            "level": 101.5,
            "country": "FR",
            "avatar": "https://a.ppy.sh/2",
        },
    }


async def test_the_real_connector_renderers_stay_within_the_cv2_budget():
    """The sibling of the framework-only budget test above, with the REAL P4
    renderers left registered and fed near-worst-case payloads: what a maxed
    profile costs is decided by those renderers together, and the 4000
    character / 40 component ceiling is per MESSAGE."""
    payloads = _rich_payloads()
    profile = _maxed_profile()
    visibility_map = {name: "public" for name in registry.FIELD_NAMES}
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    connections = _connections(
        *[name for name in registry.FIELD_NAMES if not registry.get(name).stored]
    )
    for connection in connections:
        connection["payload"] = payloads.get(connection["connector"], {})

    card = await views.build_profile_card(
        _Member(OWNER, "A Rather Long Display Name Indeed"),
        profile,
        visibility_map,
        viewer,
        connections,
    )

    components = _real_components(card)
    text_total = sum(len(node["content"]) for node in components if node["type"] == 10)
    assert len(components) <= 40
    assert text_total <= views.CARD_TEXT_BUDGET
    # ... and not vacuously: all three really drew, nothing was dropped.
    text = "\n".join(_texts(card))
    assert "mean score" in text  # AniList
    assert "Owns 4321" in text  # Steam
    assert "Rank #1234" in text  # osu!
    assert "too long to show in full" not in text


async def test_truncation_drops_content_cleanly_and_says_so(monkeypatch):
    """Forcing an unreasonably small budget must never crash - it drops whole
    blocks and appends the (existing, reused) truncation footer msgid."""
    monkeypatch.setattr(views, "_CONTENT_BUDGET", 10)
    profile = _profile(bio="a bio far longer than ten characters", pronouns="she/her")
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    text = "\n".join(_texts(card))
    assert "a bio far longer" not in text
    assert "too long to show in full" in text


async def test_a_hostile_bio_keeps_its_lines_but_forges_no_heading():
    """The bio is the one owner-typed value rendered as a BLOCK, so it keeps
    its newlines - what it must not keep is the power to open a section, which
    would sit right next to the verified connector data P4 draws."""
    profile = _profile(
        bio="line one\n## Gaming IDs\n**Switch Friend Code:** fake\n-# subtext\n> quote"
    )
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    lines = [line for text in _texts(card) for line in text.splitlines()]
    for forged in ("## Gaming IDs", "-# subtext", "> quote"):
        assert not any(line.startswith(forged) for line in lines)
        # ... and nothing was removed: the text is still there, still readable.
        assert any(forged in line for line in lines)
    # The bio is still a multi-line block, not flattened into one row.
    assert any(line == "line one" for line in lines)
    # A bold row is inline decoration, not structure: left exactly as typed.
    assert any(line == "**Switch Friend Code:** fake" for line in lines)


async def test_a_hostile_custom_field_cannot_forge_a_section_header():
    """``registry._clean_text`` only strips the ENDS, so a label or value can
    still carry a newline - and a newline inside a ``**label:** value`` row is
    all it takes to fake a "Gaming IDs" heading in somebody else's card."""
    profile = _profile(
        custom_fields=[
            {"label": "Fav\n## Gaming IDs", "value": "x\n**Switch Friend Code:** fake"}
        ],
        gaming_ids={"switch": "SW-1\n@everyone"},
    )
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    lines = [line for text in _texts(card) for line in text.splitlines()]
    # Every forged fragment was folded back into its own row, so none of them
    # can pass for a heading or for a real gaming-ID line of their own.
    assert not any(line.startswith("## Gaming IDs") for line in lines)
    assert not any(line.startswith("**Switch Friend Code:** fake") for line in lines)
    assert not any(line == "@everyone" for line in lines)
    # The header's own "## " / "-# " prefixes are structural too.
    hostile = await views.build_profile_card(
        _Member(OWNER, "Owner"),
        _profile(bio="hi", pronouns="she/her\n## Gaming IDs"),
        {},
        viewer,
        [],
    )
    hostile_lines = [line for text in _texts(hostile) for line in text.splitlines()]
    assert not any(line.startswith("## Gaming IDs") for line in hostile_lines)
    # ... and nothing was lost: the values are still shown, on one row each.
    assert any("Fav ## Gaming IDs" in line for line in lines)
    assert any("SW-1 @everyone" in line for line in lines)


async def test_a_normal_profile_never_triggers_truncation():
    profile = _profile(bio="hi", gaming_ids={"switch": "SW-1"})
    viewer = visibility.ViewerContext(owner_id=OWNER, viewer_id=OWNER, shares_guild=False)
    card = await views.build_profile_card(_Member(OWNER, "Owner"), profile, {}, viewer, [])
    assert "too long to show in full" not in "\n".join(_texts(card))


# ---------------------------------------------------------------------------
# ProfileVisibilityPanel
# ---------------------------------------------------------------------------


class _FakeCog:
    def __init__(self, bot):
        self.bot = bot


def _make_cog(fake_pool):
    return _FakeCog(types.SimpleNamespace(db_pool=fake_pool))


def test_panel_shows_the_current_visibility_state():
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {"bio": "server"})
    text = "\n".join(_texts(panel))
    assert "Bio: Server" in text
    assert "Pronouns: Private" in text  # default, never configured


def test_panel_offers_every_stored_and_connector_section():
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    components = _real_components(panel)
    select_count = sum(1 for node in components if node["type"] == 3)
    assert select_count == len(registry.FIELD_NAMES)


def test_panel_stays_within_the_component_cap():
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    assert len(_real_components(panel)) <= views.CARD_COMPONENT_CAP


def test_the_panel_component_arithmetic_matches_the_real_payload():
    """The guard below is only worth having if its formula is the truth."""
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    assert views._panel_component_count(len(registry.FIELDS)) == len(
        _real_components(panel)
    )


def test_the_panel_refuses_to_ship_an_over_cap_payload(monkeypatch):
    """Every connector a later lot adds costs 2 more components. Past the cap
    Discord answers an opaque 400 (and discord.py refuses outright), so this
    has to break in tests, for whoever adds the section - not for a member."""
    extra = registry.FIELDS + tuple(
        registry.Field(f"filler{index}", f"Filler {index}", "connector", lambda *a: None)
        for index in range(20)
    )
    monkeypatch.setattr(registry, "FIELDS", extra)
    cog = _make_cog(None)
    with pytest.raises(RuntimeError) as excinfo:
        views.ProfileVisibilityPanel(cog, OWNER, {})
    assert "paginate" in str(excinfo.value)


def test_every_select_option_names_its_own_section():
    """An option flagged ``default`` is what Discord shows on the COLLAPSED
    select, so the placeholder is never seen: without the section name on the
    options, the panel would be twelve identical pickers reading "Private"."""
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    selects = [node for node in _real_components(panel) if node["type"] == 3]
    assert len(selects) == len(registry.FIELDS)
    for field, select in zip(registry.FIELDS, selects):
        for option in select["options"]:
            assert option["label"].startswith(field.label + ":")
        assert sum(1 for option in select["options"] if option.get("default")) == 1


async def test_set_level_roundtrips_through_storage(fake_pool, make_interaction):
    fake_pool.fetch_return = [Record(field="bio", level="server")]
    cog = _make_cog(fake_pool)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    interaction = make_interaction(user_id=OWNER)

    await panel.set_level(interaction, registry.get("bio"), "server")

    assert panel.state["bio"] == "server"
    write = next(c for c in fake_pool.calls if c[0] == "execute")
    assert "bio" in write[2]
    assert "Bio: Server" in "\n".join(_texts(panel))


async def test_set_level_re_reads_the_whole_map_after_writing(
    fake_pool, make_interaction
):
    """The overview claims to show EVERY section, so a change another surface
    made (the text command, a second panel, the dashboard) has to appear too -
    the panel re-reads instead of only patching the row it just wrote."""
    fake_pool.fetch_return = [
        Record(field="bio", level="server"),
        Record(field="accent", level="public"),  # moved elsewhere meanwhile
    ]
    cog = _make_cog(fake_pool)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    interaction = make_interaction(user_id=OWNER)

    await panel.set_level(interaction, registry.get("bio"), "server")

    assert panel.state["accent"] == "public"
    assert "Accent colour: Public" in "\n".join(_texts(panel))
    assert sum(1 for call in fake_pool.calls if call[0] == "fetch") == 1


async def test_a_failed_re_read_never_undoes_the_write(fake_pool, make_interaction):
    async def boom(*args, **kwargs):
        raise RuntimeError("read replica is gone")

    fake_pool.fetch = boom
    cog = _make_cog(fake_pool)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    interaction = make_interaction(user_id=OWNER)

    await panel.set_level(interaction, registry.get("bio"), "public")

    # The write landed, so the echoed level stands and the panel still shows it.
    assert panel.state["bio"] == "public"
    assert "Bio: Public" in "\n".join(_texts(panel))
    assert not interaction.sent  # not an error the clicker has to be told about


async def test_an_empty_select_payload_is_refused_not_crashed(
    fake_pool, make_interaction
):
    """Discord can deliver an empty ``values``; indexing it in the callback
    would raise where nothing answers the interaction."""
    cog = _make_cog(fake_pool)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    select = next(
        child
        for child in panel.walk_children()
        if isinstance(child, views._SectionVisibilitySelect)
    )
    select._selected_values = []
    interaction = make_interaction(user_id=OWNER)

    await select.callback(interaction)  # must not raise

    assert interaction.sent  # the clicker got an answer
    assert not any(call[0] == "execute" for call in fake_pool.calls)


async def test_set_level_to_private_deletes_the_row(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {"bio": "public"})
    interaction = make_interaction(user_id=OWNER)

    await panel.set_level(interaction, registry.get("bio"), "private")

    assert panel.state["bio"] == "private"
    delete_call = next(c for c in fake_pool.calls if c[0] == "execute")
    assert "DELETE" in delete_call[1]


async def test_set_level_answers_the_clicker_when_storage_fails(
    fake_pool, make_interaction, caplog
):
    async def boom(*args, **kwargs):
        raise RuntimeError("pool is gone")

    fake_pool.execute = boom
    cog = _make_cog(fake_pool)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    interaction = make_interaction(user_id=OWNER)

    await panel.set_level(interaction, registry.get("bio"), "server")  # must not raise

    assert interaction.sent  # the clicker got an ephemeral answer


class _ModalCaptureResponse:
    def __init__(self):
        self.sent_modal = None

    async def send_modal(self, modal):
        self.sent_modal = modal


class _ModalCaptureInteraction:
    """A minimal local fake - the shared conftest FakeInteraction has no
    ``send_modal`` (nothing else in the suite needed it before this button)."""

    def __init__(self):
        self.response = _ModalCaptureResponse()


async def test_the_edit_button_opens_the_existing_gaming_id_modal():
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    button = next(
        child
        for child in panel.walk_children()
        if isinstance(child, views._EditGamingIdButton)
    )
    interaction = _ModalCaptureInteraction()

    await button.callback(interaction)

    assert isinstance(interaction.response.sent_modal, views.ProfileEditModal)
    assert interaction.response.sent_modal.cog is cog


# ---------------------------------------------------------------------------
# Panel plumbing: author-gate + locale (shared AuthorLayoutView contract)
# ---------------------------------------------------------------------------


async def test_panel_rejects_a_non_author_interaction(make_interaction):
    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    interaction = make_interaction(user_id=FRIEND)

    allowed = await panel.interaction_check(interaction)

    assert allowed is False
    assert interaction.sent
    assert "isn't for you" in interaction.sent[0][0][0]


async def test_panel_resolves_the_clicker_locale(make_interaction, monkeypatch):
    from tools import i18n

    cog = _make_cog(None)
    panel = views.ProfileVisibilityPanel(cog, OWNER, {})
    interaction = make_interaction(user_id=OWNER)
    calls = []

    async def _spy(interaction_arg):
        calls.append(interaction_arg)

    monkeypatch.setattr(i18n, "apply_interaction_locale", _spy)

    allowed = await panel.interaction_check(interaction)

    assert allowed is True
    assert calls == [interaction]
