"""Tests for the connector framework (cogs/community/profile/connectors/).

The lot ships a FRAMEWORK, so the risk is not "does Steam work" (P4) but "can a
future connector do something the rest of the system cannot survive": store a
handle nobody validated, resurrect a link the user deleted, leave a section
published with nothing behind it, smuggle an unbounded blob into every profile
card, or quietly escape /mydata.

Four groups of guards:

* the interface and its bounded registry - names come from the parent registry,
  the registry can only ever hold reserved names, and NO connector is registered
  in production (a fake AniList serving users would be worse than none);
* storage - one statement per write, validation before SQL, the UPDATE-not-upsert
  invariant on refresh, and the unlink that un-publishes in the same transaction.
  Every statement here was additionally probed against the real local Postgres in
  a rolled-back transaction (the CHECKs, the ``xmax = 0`` created flag, the
  refresh index plan);
* the cog - what the user actually sees: coming-soon refusals, the visibility
  hint, re-linking, unknown names, and the fact that every answer is ephemeral;
* privacy and schema - the new table joins the export and the forget path, and
  carries no guild_id so the guild purge correctly never sees it.

Offline: the storage seam is monkeypatched or driven against the conftest fake
pool, so no database, no network and no Discord are involved.
"""

import ast
import json
import os
import re
import types

import discord
import pytest
from discord.ext import commands

from cogs.community.profile import registry as profile_registry
from cogs.community.profile import storage as profile_storage
from cogs.community.profile import visibility
from cogs.community.profile.connectors import base, storage
from cogs.community.profile.connectors.cog import (
    LINK_RATE,
    ConnectorName,
    ProfileConnectors,
)
from cogs.community.profile.connectors.example import ExampleConnector
from tools import privacy

USER = 4242

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
)


def _schema():
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        return re.sub(r"--[^\n]*", "", handle.read())


def _table_body(name):
    match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + name + r"\s*\((.*?)\n\s*\)\s*;",
        _schema(),
        re.S | re.I,
    )
    assert match, f"{name} is not declared in schema.sql"
    return match.group(1)


def _queries(pool):
    return [query for _method, query, _args in pool.calls]


@pytest.fixture
def example():
    """Register the reference connector under a real reserved name, then undo.

    Registering it (rather than special-casing fakes in the registry) is the
    point: the cog, the storage seam and the error mapping are exercised through
    the SAME routing a P4 connector will take.

    P4 has now landed a real connector under every LINKABLE name (this lot's
    AniList/Steam/osu!, P4B's Last.fm/Backloggd), so "steam" is no longer a
    free slot the way it was when this fixture was written - the real
    ``SteamConnector`` is saved and restored around the swap so this fixture's
    borrowed name does not leave production without its Steam connector for
    the rest of the test session.
    """
    previous = base.CONNECTORS.pop("steam", None)
    connector = base.register(ExampleConnector("steam"))
    try:
        yield connector
    finally:
        base.unregister("steam")
        if previous is not None:
            base.CONNECTORS["steam"] = previous


# ---------------------------------------------------------------------------
# The vocabulary: derived, never restated
# ---------------------------------------------------------------------------


def test_sections_are_the_parent_registrys_connector_fields():
    """One list of section names, in the module that already owned it."""
    reserved = tuple(
        field.name
        for field in profile_registry.FIELDS
        if field.kind == "connector"
    )
    assert base.SECTIONS == reserved
    assert set(base.SECTIONS) == {
        "anilist",
        "steam",
        "lastfm",
        "osu",
        "backloggd",
        "presence_gaming",
        "spotify_presence",
    }


def test_presence_sections_are_reachable_but_not_linkable():
    """There is no handle to type for a presence section, so link refuses it.

    They stay inside SECTIONS (and inside the schema CHECK) because P5 may want
    a marker row, but offering them in `connections link` would be a prompt with
    no possible answer.
    """
    for name in base.PRESENCE_SECTIONS:
        assert name in base.SECTIONS
        assert name not in base.LINKABLE
        with pytest.raises(base.UnknownConnector):
            base.get(name)
    assert base.LINKABLE == ("anilist", "steam", "lastfm", "osu", "backloggd")


def test_the_schema_check_lists_exactly_the_reserved_sections():
    """schema.sql and base.py must agree on which connectors can exist."""
    body = _table_body("profile_connections")
    match = re.search(r"connector IN \(([^)]*)\)", body, re.S)
    assert match, "profile_connections has no connector whitelist"
    listed = tuple(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert set(listed) == set(base.SECTIONS)


def test_the_literal_choices_match_the_linkable_tuple():
    """The slash CHOICES are spelled statically; they must not drift."""
    import typing

    assert typing.get_args(ConnectorName) == base.LINKABLE


def test_the_visibility_literal_matches_the_level_tuple():
    import typing

    from cogs.community.profile.connectors.cog import VisibilityLevel

    assert set(typing.get_args(VisibilityLevel)) == set(visibility.LEVELS)


# ---------------------------------------------------------------------------
# The bounded registry
# ---------------------------------------------------------------------------


def test_production_only_ever_registers_reserved_linkable_names():
    """THE guard against a fake shipping, updated for a post-P4 world: every
    name in the registry is a real reserved LINKABLE section (never a rogue
    name), and the ones this lot ships (AniList, Steam, osu!) really are
    registered - P3's original "the registry starts empty" version of this
    guard held only until P4 landed its first real connector, which is
    exactly what this lot does."""
    assert set(base.CONNECTORS) <= set(base.LINKABLE)
    assert {"anilist", "steam", "osu"} <= set(base.CONNECTORS)
    assert set(base.available()) <= set(base.LINKABLE)


@pytest.fixture
def coming_soon_connector():
    """One LINKABLE name with NO implementation, for the duration of the test.

    Once every P4 module has landed, all five LINKABLE names are always
    registered in production - so a test that wants to exercise the "coming
    soon" refusal path has to manufacture that state itself. Borrows "osu"
    (this lot's own connector, so nothing else needs updating to know the
    slot might disappear) and restores it afterward.
    """
    name = "osu"
    previous = base.CONNECTORS.pop(name, None)
    try:
        yield name
    finally:
        if previous is not None:
            base.CONNECTORS[name] = previous


def test_the_registry_only_accepts_reserved_linkable_names(example):
    """Bounded by construction: keys are a subset of a seven-name whitelist."""

    class _Rogue(base.Connector):
        name = "crunchyroll"

    with pytest.raises(base.UnknownConnector):
        base.register(_Rogue())
    with pytest.raises(base.UnknownConnector):
        ExampleConnector("crunchyroll")
    with pytest.raises(base.UnknownConnector):
        ExampleConnector("presence_gaming")
    assert set(base.CONNECTORS) <= set(base.LINKABLE)
    assert len(base.CONNECTORS) <= len(base.SECTIONS)


def test_registering_twice_is_refused_rather_than_silently_overwriting(example):
    """Load order must not decide which Steam connector users get."""
    with pytest.raises(ValueError):
        base.register(ExampleConnector("steam"))
    assert base.CONNECTORS["steam"] is example


def test_get_separates_a_typo_from_a_connector_that_has_not_landed(
    example, coming_soon_connector
):
    assert base.get("steam") is example
    with pytest.raises(base.UnknownConnector):
        base.get("crunchyroll")
    with pytest.raises(base.ConnectorUnavailable) as caught:
        base.get(coming_soon_connector)
    assert caught.value.reason == "coming_soon"


def test_label_falls_back_to_the_parent_registry(example):
    assert base.label_for("steam") == ExampleConnector.label
    assert base.label_for("osu") == profile_registry.get("osu").label


# ---------------------------------------------------------------------------
# What a connector is allowed to return
# ---------------------------------------------------------------------------


def test_validate_link_result_refuses_an_empty_or_oversized_handle():
    with pytest.raises(base.InvalidHandle):
        base.validate_link_result("steam", base.LinkResult(external_id="   "))
    with pytest.raises(base.InvalidHandle) as caught:
        base.validate_link_result(
            "steam", base.LinkResult(external_id="x" * (base.EXTERNAL_ID_MAX + 1))
        )
    assert caught.value.reason == "too_long"
    assert caught.value.limit == base.EXTERNAL_ID_MAX
    with pytest.raises(base.InvalidHandle):
        base.validate_link_result("steam", {"external_id": "not a LinkResult"})


def test_validate_link_result_trims_rather_than_fails_on_the_cosmetic_field():
    """A display name is decoration: it must never cost the user their link."""
    checked = base.validate_link_result(
        "steam",
        base.LinkResult(
            external_id="  76561198  ",
            display_name="y" * (base.DISPLAY_NAME_MAX + 50),
            payload="not a dict",
        ),
    )
    assert checked.external_id == "76561198"
    assert len(checked.display_name) == base.DISPLAY_NAME_MAX
    assert checked.payload == {}


def test_encode_payload_caps_what_a_refresh_can_write():
    assert json.loads(base.encode_payload("steam", None)) == {}
    with pytest.raises(base.InvalidPayload):
        base.encode_payload("steam", ["not", "an", "object"])
    with pytest.raises(base.InvalidPayload) as caught:
        base.encode_payload("steam", {"blob": "x" * base.PAYLOAD_MAX_BYTES})
    assert caught.value.reason == "too_large"
    # A stray datetime must not blow up a whole refresh.
    import datetime

    encoded = base.encode_payload(
        "steam", {"when": datetime.datetime(2030, 1, 1)}
    )
    assert "2030-01-01" in encoded


# ---------------------------------------------------------------------------
# The reference connector
# ---------------------------------------------------------------------------


async def test_the_example_connector_decides_offline_and_canonicalises():
    connector = ExampleConnector("steam")
    result = await connector.link(USER, "  Yanis.W  ")
    assert result.external_id == "yanis.w"
    assert result.display_name == "Yanis.W"
    assert result.payload == {"handle": "Yanis.W"}


@pytest.mark.parametrize("handle", ("", "  ", "x", "no spaces here", "a" * 33, None))
async def test_the_example_connector_refuses_a_malformed_handle(handle):
    with pytest.raises(base.InvalidHandle):
        await ExampleConnector("steam").link(USER, handle)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


async def test_link_upserts_one_row_and_reports_whether_it_was_created(fake_pool):
    fake_pool.fetchrow_return = {
        "connector": "steam",
        "external_id": "yanis",
        "display_name": "Yanis",
        "linked_at": "then",
        "last_refresh": None,
        "payload": json.dumps({"handle": "Yanis"}),
        "created": True,
    }
    stored = await storage.link(
        fake_pool,
        USER,
        "steam",
        base.LinkResult("yanis", "Yanis", {"handle": "Yanis"}),
    )
    method, query, args = fake_pool.calls[0]
    assert method == "fetchrow"
    assert query.startswith("INSERT INTO profile_connections")
    assert "ON CONFLICT (user_id, connector) DO UPDATE SET" in query
    assert "(xmax = 0) AS created" in query
    assert args[:4] == (USER, "steam", "yanis", "Yanis")
    assert json.loads(args[4]) == {"handle": "Yanis"}
    # The payload comes back decoded, never as the quoted blob asyncpg returns.
    assert stored["payload"] == {"handle": "Yanis"}
    assert stored["created"] is True


async def test_relinking_resets_the_cache_because_it_describes_someone_else(
    fake_pool,
):
    fake_pool.fetchrow_return = {
        "connector": "steam",
        "external_id": "other",
        "display_name": None,
        "linked_at": "then",
        "last_refresh": None,
        "payload": "{}",
        "created": False,
    }
    stored = await storage.link(fake_pool, USER, "steam", base.LinkResult("other"))
    _method, query, _args = fake_pool.calls[0]
    assert "last_refresh = NULL" in query
    assert "linked_at = now()" in query
    assert stored["created"] is False


async def test_link_validates_before_touching_the_database(fake_pool):
    with pytest.raises(base.InvalidHandle):
        await storage.link(fake_pool, USER, "steam", base.LinkResult(""))
    with pytest.raises(base.InvalidPayload):
        await storage.link(
            fake_pool,
            USER,
            "steam",
            base.LinkResult("ok", None, {"blob": "x" * base.PAYLOAD_MAX_BYTES}),
        )
    with pytest.raises(base.UnknownConnector):
        await storage.link(fake_pool, USER, "crunchyroll", base.LinkResult("ok"))
    assert fake_pool.calls == []


async def test_unlink_deletes_the_row_and_unpublishes_in_one_transaction(fake_pool):
    """A published section with nothing behind it is a promise the card cannot
    keep - and re-linking later would re-expose a new account at an old level."""
    fake_pool.execute_return = "DELETE 1"
    assert await storage.unlink(fake_pool, USER, "steam") is True
    queries = _queries(fake_pool)
    assert queries[0].startswith("DELETE FROM profile_connections")
    assert fake_pool.calls[0][2] == (USER, "steam")
    # The visibility row goes through the parent seam, which DELETES for
    # 'private' - the default is never materialised.
    assert queries[1].startswith("DELETE FROM profile_visibility")
    assert fake_pool.calls[1][2] == (USER, "steam")
    assert len(queries) == 2


async def test_unlink_reports_that_nothing_was_linked(fake_pool):
    fake_pool.execute_return = "DELETE 0"
    assert await storage.unlink(fake_pool, USER, "steam") is False
    # The visibility row is still cleaned: a stale published section must not
    # survive because the connection was already gone.
    assert _queries(fake_pool)[1].startswith("DELETE FROM profile_visibility")


async def test_unlink_refuses_a_name_outside_the_whitelist(fake_pool):
    with pytest.raises(base.UnknownConnector):
        await storage.unlink(fake_pool, USER, "crunchyroll")
    assert fake_pool.calls == []


async def test_set_payload_updates_and_never_resurrects_an_unlinked_account(
    fake_pool,
):
    """The refresh loop and the user race; the user must win."""
    fake_pool.execute_return = "UPDATE 1"
    assert await storage.set_payload(fake_pool, USER, "steam", {"games": 12}) is True
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert query.startswith("UPDATE profile_connections SET payload = $3::jsonb")
    assert "INSERT" not in query
    assert "last_refresh = now()" in query
    assert args[:2] == (USER, "steam")
    assert json.loads(args[2]) == {"games": 12}

    fake_pool.calls.clear()
    fake_pool.execute_return = "UPDATE 0"
    with pytest.raises(base.NotLinked):
        await storage.set_payload(fake_pool, USER, "steam", {"games": 12})


async def test_set_payload_keeps_the_previous_display_name_when_given_none(
    fake_pool,
):
    fake_pool.execute_return = "UPDATE 1"
    await storage.set_payload(fake_pool, USER, "steam", {})
    _method, query, args = fake_pool.calls[0]
    assert "display_name = COALESCE($4, display_name)" in query
    assert args[3] is None


async def test_set_payload_validates_before_touching_the_database(fake_pool):
    with pytest.raises(base.InvalidPayload):
        await storage.set_payload(fake_pool, USER, "steam", "not an object")
    assert fake_pool.calls == []


async def test_reads_decode_the_jsonb_asyncpg_returns_as_text(fake_pool):
    fake_pool.fetch_return = [
        {"connector": "steam", "external_id": "a", "payload": json.dumps({"x": 1})},
        {"connector": "osu", "external_id": "b", "payload": None},
        {"connector": "lastfm", "external_id": "c", "payload": "[1, 2]"},
        {"connector": "anilist", "external_id": "d", "payload": "not json"},
    ]
    connections = await storage.get_connections(fake_pool, USER)
    assert [item["payload"] for item in connections] == [{"x": 1}, {}, {}, {}]
    _method, query, args = fake_pool.calls[0]
    assert "WHERE user_id = $1 ORDER BY connector" in query
    assert args == (USER,)


async def test_get_connection_returns_none_when_it_is_not_linked(fake_pool):
    fake_pool.fetchrow_return = None
    assert await storage.get_connection(fake_pool, USER, "steam") is None
    _method, query, args = fake_pool.calls[0]
    assert "WHERE user_id = $1 AND connector = $2" in query
    assert args == (USER, "steam")


async def test_every_statement_is_a_single_one_and_interpolates_no_value(fake_pool):
    """asyncpg runs ONE parameterized statement per call; a stray ';' would fail
    at runtime, not here, so pin it - and pin that user text always rides as a
    bound parameter."""
    fake_pool.fetchrow_return = {"connector": "steam", "payload": "{}"}
    hostile = "'; DROP TABLE profile_connections --"
    await storage.link(fake_pool, USER, "steam", base.LinkResult(hostile))
    await storage.unlink(fake_pool, USER, "steam")
    await storage.get_connection(fake_pool, USER, "steam")
    await storage.get_connections(fake_pool, USER)
    fake_pool.execute_return = "UPDATE 1"
    await storage.set_payload(fake_pool, USER, "steam", {"a": hostile})
    for query in _queries(fake_pool):
        assert ";" not in query
        assert "DROP TABLE" not in query


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Ctx:
    """A context that tells the two invocation paths apart.

    ``interaction`` is what decides whether ``ephemeral=`` means anything at
    all (discord.py drops the flag on the prefix path), so the fake carries it
    and records DMs separately from channel sends.
    """

    def __init__(self, author_id=USER, interaction=False, command=None):
        self.dms = []
        self.dm_error = None
        self.author = types.SimpleNamespace(id=author_id, send=self._dm)
        self.guild = types.SimpleNamespace(id=9)
        self.interaction = types.SimpleNamespace() if interaction else None
        self.invoked_subcommand = None
        self.clean_prefix = "?"
        self.sends = []
        self.typing_kwargs = []
        # discord.py binds the running Command here; calling a callback
        # directly never does, so it is None unless a test is exercising the
        # cooldown refund and hands the real command over.
        self.command = command

    def typing(self, **kwargs):
        self.typing_kwargs.append(kwargs)
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)

    async def _dm(self, *args, **kwargs):
        if self.dm_error is not None:
            raise self.dm_error
        self.dms.append((args, kwargs))
        return types.SimpleNamespace(id=2)


def _cog(pool=None):
    cog = ProfileConnectors(types.SimpleNamespace(db_pool=pool or object()))
    # What Cog._inject does when the bot loads the extension.
    for command in cog.__cog_commands__:
        command.cog = cog
    return cog


def _last_text(ctx):
    args, kwargs = ctx.sends[-1]
    return args[0] if args else kwargs.get("content")


def _last_embed(ctx):
    return ctx.sends[-1][1]["embed"]


@pytest.fixture
def seam(monkeypatch):
    """Record every write instead of performing it; serve a chosen state."""
    state = {
        "connections": [],
        "levels": {},
        "linked": [],
        "unlinked": [],
        "visibility": [],
        "removed": True,
    }

    async def link(pool, user_id, connector, result):
        checked = base.validate_link_result(connector, result)
        state["linked"].append((user_id, connector, checked))
        return {
            "connector": connector,
            "external_id": checked.external_id,
            "display_name": checked.display_name,
            "payload": checked.payload,
            "created": True,
        }

    async def unlink(pool, user_id, connector):
        state["unlinked"].append((user_id, connector))
        return state["removed"]

    async def get_connections(pool, user_id):
        return state["connections"]

    async def get_visibility(pool, user_id):
        return state["levels"]

    async def set_visibility(pool, user_id, name, level):
        state["visibility"].append((user_id, name, level))
        return level

    monkeypatch.setattr(storage, "link", link)
    monkeypatch.setattr(storage, "unlink", unlink)
    monkeypatch.setattr(storage, "get_connections", get_connections)
    monkeypatch.setattr(profile_storage, "get_visibility", get_visibility)
    monkeypatch.setattr(profile_storage, "set_visibility", set_visibility)
    return state


async def test_a_connector_without_an_implementation_is_refused_not_stored(
    seam, coming_soon_connector
):
    """'Coming soon' must not mean 'stored and silently broken'."""
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, coming_soon_connector, handle="peppy")
    assert "coming soon" in _last_text(ctx).lower()
    assert seam["linked"] == []


async def test_a_successful_link_stores_the_normalised_handle_and_hints(
    seam, example
):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="Yanis")
    (_user, connector, checked) = seam["linked"][0]
    assert (connector, checked.external_id, checked.display_name) == (
        "steam",
        "yanis",
        "Yanis",
    )
    embed = _last_embed(ctx)
    assert embed.fields[0].value == "Yanis"
    # Born private, so the answer must name the command that publishes it.
    assert "?connections visibility steam server" in embed.footer.text


async def test_a_published_section_is_told_who_can_see_the_new_handle(seam, example):
    """The publish hint goes away, the AUDIENCE never does.

    Re-linking over a section the user published months ago for a different
    account puts a brand-new handle live at that old level; the footer is the
    only place that says so.
    """
    seam["levels"] = {"steam": "server"}
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="Yanis")
    footer = _last_embed(ctx).footer.text
    assert "connections visibility" not in footer
    assert footer == "Linked as Yanis - visible to the servers you share"


async def test_relinking_over_a_public_section_says_it_is_public(
    seam, example, monkeypatch
):
    async def link(pool, user_id, connector, result):
        checked = base.validate_link_result(connector, result)
        seam["linked"].append((user_id, connector, checked))
        return {
            "connector": connector,
            "external_id": checked.external_id,
            "display_name": checked.display_name,
            "payload": {},
            "created": False,
        }

    monkeypatch.setattr(storage, "link", link)
    seam["levels"] = {"steam": "public"}
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="Second")
    embed = _last_embed(ctx)
    assert embed.title == "Connection updated"
    assert embed.footer.text == "Linked as Second - visible to anyone"


async def test_relinking_the_same_connector_updates_rather_than_duplicates(
    seam, example, monkeypatch
):
    async def link(pool, user_id, connector, result):
        checked = base.validate_link_result(connector, result)
        seam["linked"].append((user_id, connector, checked))
        return {
            "connector": connector,
            "external_id": checked.external_id,
            "display_name": checked.display_name,
            "payload": {},
            "created": False,
        }

    monkeypatch.setattr(storage, "link", link)
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="First")
    await cog.connections_link.callback(cog, ctx, "steam", handle="Second")
    assert [entry[2].external_id for entry in seam["linked"]] == ["first", "second"]
    assert _last_embed(ctx).title == "Connection updated"


async def test_a_refused_handle_says_what_to_type_instead(seam, example):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="no spaces here")
    assert "username" in _last_text(ctx)
    assert seam["linked"] == []


async def test_an_unknown_connector_lists_the_real_ones(seam):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "crunchyroll", handle="x")
    assert "anilist" in _last_text(ctx)
    assert seam["linked"] == []

    ctx = _Ctx()
    await cog.connections_unlink.callback(cog, ctx, "crunchyroll")
    assert "anilist" in _last_text(ctx)
    assert seam["unlinked"] == []

    ctx = _Ctx()
    await cog.connections_visibility.callback(cog, ctx, "crunchyroll", "public")
    assert "anilist" in _last_text(ctx)
    assert seam["visibility"] == []


async def test_a_presence_section_cannot_be_linked_through_the_cog(seam):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(
        cog, ctx, "spotify_presence", handle="whatever"
    )
    assert "anilist" in _last_text(ctx)
    assert seam["linked"] == []


async def test_unlink_reports_both_outcomes(seam, example):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_unlink.callback(cog, ctx, "steam")
    assert seam["unlinked"] == [(USER, "steam")]
    assert "unlinked" in _last_text(ctx)

    seam["removed"] = False
    ctx = _Ctx()
    await cog.connections_unlink.callback(cog, ctx, "steam")
    assert "not linked" in _last_text(ctx)


async def test_a_failing_unlink_hook_never_resurrects_the_link(seam, example):
    """The row is already gone; a connector's own cleanup is best effort."""

    async def boom(user_id):
        raise RuntimeError("remote said no")

    example.unlink = boom
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_unlink.callback(cog, ctx, "steam")
    assert seam["unlinked"] == [(USER, "steam")]
    assert "unlinked" in _last_text(ctx)


def _label(name):
    return base.label_for(name)


async def test_the_list_shows_every_section_with_its_audience(
    seam, example, coming_soon_connector
):
    seam["connections"] = [
        {"connector": "steam", "external_id": "yanis", "display_name": "Yanis"}
    ]
    seam["levels"] = {"steam": "public"}
    cog = _cog()
    ctx = _Ctx(interaction=True)
    await cog.connections_list.callback(cog, ctx)
    embed = _last_embed(ctx)
    assert [field.name for field in embed.fields] == [
        _label(name) for name in base.LINKABLE
    ]
    values = {field.name: field.value for field in embed.fields}
    assert "Yanis" in values[_label("steam")]
    assert "anyone" in values[_label("steam")]
    # Everything P4 has not landed says so, instead of pretending to be unlinked.
    assert values[_label(coming_soon_connector)] == "Coming soon"


async def test_the_group_falls_back_to_the_list_for_prefix_users(seam):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections.callback(cog, ctx)
    # Prefix: the embed goes to the DM, the channel only gets a neutral line.
    assert ctx.dms and "embed" in ctx.dms[-1][1]
    assert "direct message" in _last_text(ctx)


# ---------------------------------------------------------------------------
# Rate limits: the DM the list opens, and the token a refusal must not eat
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_cooldowns():
    """Empty every bucket before each test.

    ``commands.cooldown`` builds ONE ``CooldownMapping`` at DECORATION time and
    every cog instance shares it (verified: ``cog.connections_list._buckets is
    ProfileConnectors.connections_list._buckets``), so a bucket charged by one
    test would otherwise still be charged in the next.
    """
    for command in (
        ProfileConnectors.connections_list,
        ProfileConnectors.connections_link,
    ):
        command._buckets._cache.clear()
    yield


def test_the_list_is_rate_limited_because_it_opens_a_dm():
    """Every PREFIX invocation opens a DM - a per-bot resource a channel of
    users could exhaust for everybody."""
    buckets = ProfileConnectors.connections_list._buckets
    assert buckets.valid
    assert (buckets._cooldown.rate, buckets._cooldown.per) == (1, 60.0)
    assert buckets.type is commands.BucketType.user


async def test_the_bare_group_charges_the_same_bucket_as_the_list(seam):
    """`?connections` runs the subcommand's body directly, which skips the
    cooldown discord.py applies in ``Command.invoke`` - so the short spelling
    would be a free door to the DM the long one rate-limits."""
    cog = _cog()
    await cog.connections.callback(cog, _Ctx())
    with pytest.raises(commands.CommandOnCooldown):
        await cog.connections.callback(cog, _Ctx())
    # ... and it is the LIST's bucket, not a second one of its own.
    assert ProfileConnectors.connections_list._buckets._cache


async def test_another_user_is_not_caught_by_someone_elses_list_cooldown(seam):
    cog = _cog()
    await cog.connections.callback(cog, _Ctx())
    other = _Ctx(author_id=USER + 1)
    await cog.connections.callback(cog, other)  # must not raise
    assert other.dms


async def test_a_coming_soon_refusal_gives_the_rate_limit_token_back(
    seam, coming_soon_connector
):
    """The link bucket protects a third party. A name refused before any remote
    call spent none of it, so waiting a minute to try the right one would be a
    penalty for being told "not yet"."""
    cog = _cog()
    command = cog.connections_link
    ctx = _Ctx(command=command)
    bucket = command._buckets.get_bucket(ctx)
    bucket.update_rate_limit()  # what Command.invoke charges before the body
    assert bucket.get_tokens() == LINK_RATE - 1

    await command.callback(cog, ctx, coming_soon_connector, handle="peppy")

    assert "coming soon" in _last_text(ctx).lower()
    assert bucket.get_tokens() == LINK_RATE


async def test_an_unknown_name_gives_the_rate_limit_token_back(seam):
    cog = _cog()
    command = cog.connections_link
    ctx = _Ctx(command=command)
    bucket = command._buckets.get_bucket(ctx)
    bucket.update_rate_limit()

    await command.callback(cog, ctx, "crunchyroll", handle="x")

    assert bucket.get_tokens() == LINK_RATE


async def test_a_real_link_keeps_its_token(seam, example):
    """Everything past the registry lookup DID touch the connector."""
    cog = _cog()
    command = cog.connections_link
    ctx = _Ctx(command=command)
    bucket = command._buckets.get_bucket(ctx)
    bucket.update_rate_limit()

    await command.callback(cog, ctx, "steam", handle="Yanis")

    assert seam["linked"]
    assert bucket.get_tokens() == LINK_RATE - 1


async def test_a_refused_handle_keeps_its_token(seam, example):
    """The connector was asked and said no: that round trip is what the bucket
    is there to limit."""
    cog = _cog()
    command = cog.connections_link
    ctx = _Ctx(command=command)
    bucket = command._buckets.get_bucket(ctx)
    bucket.update_rate_limit()

    await command.callback(cog, ctx, "steam", handle="no spaces here")

    assert seam["linked"] == []
    assert bucket.get_tokens() == LINK_RATE - 1


async def test_visibility_writes_through_the_parent_seam(seam):
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_visibility.callback(cog, ctx, "steam", "SERVER")
    assert seam["visibility"] == [(USER, "steam", "server")]
    assert "servers you share" in _last_text(ctx)


async def test_a_broken_visibility_read_never_breaks_the_link(
    seam, example, monkeypatch
):
    """The hint is a courtesy; losing it must not lose the write."""

    async def boom(pool, user_id):
        raise RuntimeError("database is down")

    monkeypatch.setattr(profile_storage, "get_visibility", boom)
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="Yanis")
    assert seam["linked"]
    # Unknown state fails CLOSED: it still tells the user the section is private.
    assert _last_embed(ctx).footer.text


async def test_a_storage_failure_answers_instead_of_raising(seam, example, monkeypatch):
    async def boom(pool, user_id, connector, result):
        raise RuntimeError("database is down")

    monkeypatch.setattr(storage, "link", boom)
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="Yanis")
    assert "later" in _last_text(ctx)


async def test_every_answer_is_ephemeral_and_defers_before_the_work(seam, example):
    """On the SLASH path nothing this cog says belongs in a channel.

    The defer matters as much as the flag: the P4 link path is a third-party
    round trip, and an interaction that is not deferred first dies at 3 seconds.
    The prefix path has no ephemeral flag at all (see the DM test below), which
    is why this one drives an interaction context.
    """
    cog = _cog()
    contexts = []
    for call, args, kwargs in (
        (cog.connections_list, (), {}),
        (cog.connections_link, ("steam",), {"handle": "Yanis"}),
        (cog.connections_unlink, ("steam",), {}),
        (cog.connections_visibility, ("steam", "public"), {}),
    ):
        ctx = _Ctx(interaction=True)
        contexts.append(ctx)
        await call.callback(cog, ctx, *args, **kwargs)
    for ctx in contexts:
        assert ctx.typing_kwargs == [{"ephemeral": True}]
        assert ctx.sends
        assert all(kwargs.get("ephemeral") is True for _args, kwargs in ctx.sends)


async def test_the_prefix_list_never_prints_a_handle_in_the_channel(seam, example):
    """`ephemeral=True` is a no-op without an interaction (discord.py 2.7.1),
    and this answer names sections the owner deliberately left private."""
    seam["connections"] = [
        {"connector": "steam", "external_id": "yanis", "display_name": "Yanis"}
    ]
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_list.callback(cog, ctx)
    assert [kwargs.get("embed") for _args, kwargs in ctx.sends] == [None]
    assert "Yanis" not in _last_text(ctx)
    embed = ctx.dms[-1][1]["embed"]
    assert any("Yanis" in field.value for field in embed.fields)
    assert ctx.dms[-1][1]["allowed_mentions"].everyone is False


async def test_a_closed_dm_says_so_instead_of_falling_back_to_the_channel(
    seam, example
):
    seam["connections"] = [
        {"connector": "steam", "external_id": "yanis", "display_name": "Yanis"}
    ]
    cog = _cog()
    ctx = _Ctx()
    ctx.dm_error = discord.Forbidden(
        types.SimpleNamespace(status=403, reason="Forbidden"), "cannot send"
    )
    await cog.connections_list.callback(cog, ctx)
    assert ctx.dms == []
    assert "Yanis" not in _last_text(ctx)
    assert "direct message" in _last_text(ctx)


async def test_user_text_is_echoed_with_mentions_disabled(seam, example):
    """A handle is user text on its way back into a message."""
    cog = _cog()
    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="Yanis")
    assert ctx.sends[-1][1]["allowed_mentions"].everyone is False

    ctx = _Ctx()
    await cog.connections_link.callback(cog, ctx, "steam", handle="@everyone here")
    assert ctx.sends[-1][1]["allowed_mentions"].everyone is False


def _source(name):
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "cogs",
        "community",
        "profile",
        "connectors",
        name,
    )
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_the_link_verb_is_rate_limited_per_user():
    """P4 turns every link into a third-party round trip."""
    tree = ast.parse(_source("cog.py"))
    decorated = {
        node.name: [ast.unparse(dec) for dec in node.decorator_list]
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert any(
        "commands.cooldown" in decorator
        for decorator in decorated["connections_link"]
    )


# ---------------------------------------------------------------------------
# Privacy, retention and the schema
# ---------------------------------------------------------------------------


class _ExportPool:
    def __init__(self):
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return None

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return None

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "FROM profile_connections" in query:
            return [
                {
                    "connector": "steam",
                    "external_id": "yanis",
                    "display_name": "Yanis",
                    "linked_at": "then",
                    "last_refresh": None,
                    # asyncpg hands JSONB back as text.
                    "payload": json.dumps({"games": 12}),
                }
            ]
        return []


async def test_the_export_carries_the_linked_accounts_decoded():
    pool = _ExportPool()

    data, _avatars = await privacy.collect_user_export(pool, USER)

    assert data["profile_connections"] == [
        {
            "connector": "steam",
            "external_id": "yanis",
            "display_name": "Yanis",
            "linked_at": "then",
            "last_refresh": None,
            "payload": {"games": 12},
        }
    ]
    query = next(q for q in pool.queries if "FROM profile_connections" in q)
    assert "WHERE user_id = $1" in query
    # A new top-level key is a schema change, even an additive one.
    assert data["export_version"] == privacy.EXPORT_VERSION >= 3


def test_the_forget_path_deletes_the_linked_accounts_too():
    """A profile forgotten with a Steam handle still in it is not forgotten."""
    listed = {table for table, _query in privacy.PROFILE_DELETE_QUERIES}
    assert "profile_connections" in listed
    query = dict(privacy.PROFILE_DELETE_QUERIES)["profile_connections"]
    assert query == "DELETE FROM profile_connections WHERE user_id = $1"


def test_the_table_is_user_scoped_so_the_guild_purge_never_sees_it():
    """No guild_id: a departing guild must not delete a member's links, and the
    guild purge list must not mention the table."""
    from tools import retention

    body = _table_body("profile_connections")
    assert not re.search(r"^\s*guild_id\b", body, re.M)
    assert re.search(r"^\s*user_id\b", body, re.M)
    assert all(
        table != "profile_connections" for table, _q in retention.GUILD_DELETE_QUERIES
    )


def test_the_schema_declares_every_column_the_package_reads():
    body = _table_body("profile_connections")
    for column in (
        "user_id",
        "connector",
        "external_id",
        "display_name",
        "linked_at",
        "last_refresh",
        "payload",
    ):
        assert re.search(rf"^\s*{column}\b", body, re.M), column
    assert "PRIMARY KEY (user_id, connector)" in body
    # The cache must never be NULL: the reader expects an object.
    assert re.search(r"payload\s+JSONB\s+NOT NULL DEFAULT '\{\}'", body)
    assert "jsonb_typeof(payload) = 'object'" in body


def test_the_schema_caps_mirror_the_python_ones():
    body = _table_body("profile_connections")
    assert f"BETWEEN 1 AND {base.EXTERNAL_ID_MAX}" in body
    assert f"char_length(display_name) <= {base.DISPLAY_NAME_MAX}" in body
    # The scale story ("~800 MB worst case, not an unbounded blob written by a
    # third-party API") only holds if the DB enforces it too: the table is
    # CREATE TABLE IF NOT EXISTS, so a CHECK added to the body AFTER the first
    # boot would be a permanent no-op. octet_length on the text form is the
    # byte-for-byte mirror of the Python cap; pg_column_size measures the
    # COMPRESSED datum and would not be.
    assert (
        f"octet_length(payload::text) <= {base.PAYLOAD_MAX_BYTES}" in body
    )


def test_no_credential_column_was_smuggled_into_the_connection_table():
    """Tokens live encrypted in their own table; this one is exported verbatim."""
    body = _table_body("profile_connections").lower()
    for forbidden in ("token", "secret", "password", "refresh_token", "api_key"):
        assert forbidden not in body


def test_the_refresh_index_exists_for_the_p4_polling_loop():
    """Without it, "whose steam is stalest?" is a seq scan over every user."""
    schema = _schema()
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS profile_connections_refresh_idx\s+"
        r"ON profile_connections \(connector, last_refresh NULLS FIRST\)",
        schema,
    )


# ---------------------------------------------------------------------------
# Command-tree hygiene
# ---------------------------------------------------------------------------


def test_the_new_group_collides_with_nothing_in_the_tree():
    """`disconnect` is already the music cog's leave command, which is why this
    lot ships a `connections` GROUP instead of a connect/disconnect pair."""
    import sys

    tests_dir = os.path.normpath(
        os.path.join(os.path.dirname(_SCHEMA_PATH), "tests")
    )
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_command_tree_hygiene as hygiene

    defs = hygiene._all_defs()
    assert not hygiene.find_tree_collisions(defs)
    root = {d["name"] for d in defs if d["receiver"] in hygiene._ROOT_RECEIVERS}
    assert "connections" in root
    group = {
        d["name"]
        for d in defs
        if d["receiver"] == "connections"
    }
    assert {"list", "link", "unlink", "visibility"} <= group


# ---------------------------------------------------------------------------
# The rate-limit refund covers every PRE-FLIGHT refusal, not just two of them
# ---------------------------------------------------------------------------


async def test_a_not_configured_refusal_gives_the_rate_limit_token_back(
    seam, monkeypatch
):
    """'not_configured' is decided before a single byte leaves this process:
    every connector reads its API key as the first statement of its call path.
    A member who typed a perfectly good handle at a bot whose admin never
    provisioned the key spent nothing, so making them wait a minute would be a
    penalty for someone else's missing config."""
    previous = base.CONNECTORS.pop("steam", None)

    class _Unconfigured(base.Connector):
        name = "steam"

        async def link(self, user_id, raw_input):
            raise base.ConnectorUnavailable(self.name, "not_configured")

    base.register(_Unconfigured())
    try:
        cog = _cog()
        command = cog.connections_link
        ctx = _Ctx(command=command)
        bucket = command._buckets.get_bucket(ctx)
        bucket.update_rate_limit()

        await command.callback(cog, ctx, "steam", handle="Yanis")

        assert "admin" in _last_text(ctx).lower()
        assert bucket.get_tokens() == LINK_RATE
    finally:
        base.unregister("steam")
        if previous is not None:
            base.CONNECTORS["steam"] = previous


async def test_a_remote_failure_still_keeps_its_token(seam, monkeypatch):
    """The counter-test: a third party that WAS called and had a bad day cost
    the round trip this bucket exists to limit."""
    previous = base.CONNECTORS.pop("steam", None)

    class _Down(base.Connector):
        name = "steam"

        async def link(self, user_id, raw_input):
            raise base.ConnectorUnavailable(self.name, "remote")

    base.register(_Down())
    try:
        cog = _cog()
        command = cog.connections_link
        ctx = _Ctx(command=command)
        bucket = command._buckets.get_bucket(ctx)
        bucket.update_rate_limit()

        await command.callback(cog, ctx, "steam", handle="Yanis")

        assert bucket.get_tokens() == LINK_RATE - 1
    finally:
        base.unregister("steam")
        if previous is not None:
            base.CONNECTORS["steam"] = previous


# ---------------------------------------------------------------------------
# The framework's shared third-party value hygiene (base.safe_url /
# base.safe_number), consumed by all five connectors on BOTH sides of the
# payload rather than restated five times.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "   ",
        "/relative/avatar.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "//protocol-relative.example/x.png",
        "ftp://example.test/x.png",
        "https://example.test/" + "a" * 500,
    ),
)
def test_safe_url_refuses_anything_a_thumbnail_could_choke_on(value):
    assert base.safe_url(value) is None


@pytest.mark.parametrize(
    "value, expected",
    (
        ("https://example.test/a.png", "https://example.test/a.png"),
        ("  https://example.test/a.png  ", "https://example.test/a.png"),
        ("HTTP://EXAMPLE.TEST/A.PNG", "HTTP://EXAMPLE.TEST/A.PNG"),
    ),
)
def test_safe_url_keeps_an_absolute_http_url_verbatim(value, expected):
    assert base.safe_url(value) == expected


def test_safe_url_drops_an_over_long_url_rather_than_truncating_it():
    """Half a url is not a url - it is exactly the Thumbnail Discord refuses
    the whole message over, which is the failure this filter exists for."""
    url = "https://example.test/" + "a" * base.URL_MAX
    assert base.safe_url(url) is None
    assert base.safe_url(url, limit=len(url)) == url


@pytest.mark.parametrize(
    "value",
    (None, "12", True, False, [], {}, float("inf"), float("-inf"), float("nan"), 10**40),
)
def test_safe_number_refuses_what_is_not_a_plausible_number(value):
    assert base.safe_number(value) is None


@pytest.mark.parametrize("value", (0, 3, -3, 78.5, 10**12 - 1))
def test_safe_number_keeps_a_plausible_number(value):
    assert base.safe_number(value) == value
