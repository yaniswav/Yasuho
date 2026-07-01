"""Unit tests for tools/db.py and tools/modactions.py.

These exercise the two small, pure-ish helpers that several cogs lean on: the
identifier validator + guild upsert in ``tools.db`` and the case numbering,
action colours, and case embed builder in ``tools.modactions``. Every database
call goes through the ``fake_pool`` fixture (see conftest at the repo root), so
nothing here touches a real database, Discord, or the network.
"""

import datetime

import discord
import pytest

from tools import db, modactions


# ---------------------------------------------------------------------------
# small local fakes for the discord side of case_embed
# ---------------------------------------------------------------------------


class _FakeAvatar:
    def __init__(self, url):
        self.url = url


class _FakeTarget:
    """Stand-in for the moderated member/user: id, mention, display_avatar.url."""

    def __init__(self, user_id, avatar_url="https://cdn.example/av.png"):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.display_avatar = _FakeAvatar(avatar_url)


class _FakeModerator:
    def __init__(self, user_id):
        self.id = user_id
        self.mention = f"<@{user_id}>"


def _field(embed, name):
    """Return the first embed field with the given name, or None."""
    for f in embed.fields:
        if f.name == name:
            return f
    return None


# ---------------------------------------------------------------------------
# db._validate_identifier
# ---------------------------------------------------------------------------


def test_validate_identifier_accepts_snake_case():
    assert db._validate_identifier("table", "mod_log") == "mod_log"
    assert db._validate_identifier("column", "channel_id") == "channel_id"
    # a leading underscore and a bare single letter are both valid.
    assert db._validate_identifier("column", "_private") == "_private"
    assert db._validate_identifier("column", "x") == "x"


@pytest.mark.parametrize(
    "bad",
    [
        "Uppercase",  # capital letters are rejected
        "a b",        # spaces are rejected
        "x;y",        # a semicolon (SQL injection vector) is rejected
        "",           # empty string never matches ^[a-z_]...
    ],
)
def test_validate_identifier_rejects_bad_strings(bad):
    with pytest.raises(ValueError):
        db._validate_identifier("column", bad)


@pytest.mark.parametrize("bad", [123, None, ("a",), b"col"])
def test_validate_identifier_rejects_non_str(bad):
    with pytest.raises(ValueError):
        db._validate_identifier("column", bad)


# ---------------------------------------------------------------------------
# db.upsert_guild_value
# ---------------------------------------------------------------------------


async def test_upsert_guild_value_query_and_args(fake_pool):
    result = await db.upsert_guild_value(
        fake_pool, "mod_log", "channel_id", 4242, 999
    )

    # execute_return is the FakePool default 'INSERT 0 1'.
    assert result == "INSERT 0 1"

    assert len(fake_pool.calls) == 1
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert query == (
        "INSERT INTO mod_log (guild_id, channel_id) VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id"
    )
    # args are (guild_id, value) in that order.
    assert args == (4242, 999)


async def test_upsert_guild_value_rejects_bad_table_before_query(fake_pool):
    with pytest.raises(ValueError):
        await db.upsert_guild_value(fake_pool, "Bad Table", "channel_id", 1, 2)
    # validation must fail loudly BEFORE any SQL is issued.
    assert fake_pool.calls == []


async def test_upsert_guild_value_rejects_bad_column_before_query(fake_pool):
    with pytest.raises(ValueError):
        await db.upsert_guild_value(fake_pool, "mod_log", "channel;drop", 1, 2)
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# modactions.action_colour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected",
    [
        ("ban", 0xE74C3C),
        ("tempban", 0xE74C3C),
        ("softban", 0xE67E22),
        ("kick", 0xE67E22),
        ("mute", 0xE67E22),
        ("tempmute", 0xE67E22),
        ("warn", 0xF1C40F),
        ("unban", 0x2ECC71),
        ("unmute", 0x2ECC71),
        ("note", 0x95A5A6),
    ],
)
def test_action_colour_known(action, expected):
    assert modactions.action_colour(action) == expected


def test_action_colour_default_grey():
    # any unknown action falls back to the neutral grey.
    assert modactions.action_colour("frobnicate") == 0x95A5A6
    assert modactions.action_colour("") == 0x95A5A6


# ---------------------------------------------------------------------------
# modactions.case_embed
# ---------------------------------------------------------------------------


def test_case_embed_basic_fields():
    target = _FakeTarget(1001)
    moderator = _FakeModerator(2002)
    embed = modactions.case_embed(
        7, "ban", target, moderator, "spamming links"
    )

    assert embed.title == "Case #7 - Banned"
    assert embed.colour.value == 0xE74C3C

    user_field = _field(embed, "User")
    assert user_field is not None
    assert user_field.value == "<@1001> (`1001`)"

    mod_field = _field(embed, "Moderator")
    assert mod_field is not None
    assert mod_field.value == "<@2002>"

    reason_field = _field(embed, "Reason")
    assert reason_field is not None
    assert reason_field.value == "spamming links"
    assert reason_field.inline is False

    # avatar url flows into the thumbnail, footer carries the user id.
    assert embed.thumbnail.url == "https://cdn.example/av.png"
    assert embed.footer.text == "User ID: 1001"

    # no expires -> no Expires field.
    assert _field(embed, "Expires") is None


def test_case_embed_default_reason():
    target = _FakeTarget(1)
    moderator = _FakeModerator(2)
    embed = modactions.case_embed(1, "warn", target, moderator, None)
    reason_field = _field(embed, "Reason")
    assert reason_field is not None
    assert reason_field.value == "*No reason provided*"


def test_case_embed_unknown_action_titlecased():
    target = _FakeTarget(5)
    moderator = _FakeModerator(6)
    embed = modactions.case_embed(3, "frobnicate", target, moderator, "x")
    # unknown action -> verb defaults to action.title(); colour is grey.
    assert embed.title == "Case #3 - Frobnicate"
    assert embed.colour.value == 0x95A5A6


def test_case_embed_with_expires_field():
    target = _FakeTarget(1001)
    moderator = _FakeModerator(2002)
    expires = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    embed = modactions.case_embed(
        9, "tempban", target, moderator, "cooldown", expires=expires
    )

    expires_field = _field(embed, "Expires")
    assert expires_field is not None
    assert expires_field.value == discord.utils.format_dt(expires, "R")


# ---------------------------------------------------------------------------
# modactions.create_case
# ---------------------------------------------------------------------------


async def test_create_case_returns_case_number(fake_pool):
    fake_pool.fetchrow_return = {"case_number": 1}

    number = await modactions.create_case(
        fake_pool, 4242, 1001, 2002, "ban", reason="spam", expires=None
    )
    assert number == 1

    # one fetchrow, with the RETURNING clause and args in signature order.
    assert len(fake_pool.calls) == 1
    method, query, args = fake_pool.calls[0]
    assert method == "fetchrow"
    assert "INSERT INTO cases" in query
    assert "RETURNING case_number" in query
    assert args == (4242, 1001, 2002, "ban", "spam", None)
