"""Unit tests for the tickets configuration leaves (lot T1).

Two pure modules, no database and no Discord:

* ``cogs.config.tickets.guild_config`` - the six ``guild_settings`` keys, their
  coercers (untrusted JSON out of the dashboard), their clamping and the rule the
  whole lot rests on: an ABSENT key means the feature is off / the bot default,
  never a materialised value;
* ``cogs.config.tickets.preflight`` - which permissions a panel channel needs and
  how the missing ones are named.

Reads go through the ``tools.settings`` LRU, so the cache is cleared around every
test and the blob is seeded directly into it rather than faked per query.
"""

import pytest

from cogs.config.tickets import guild_config, preflight
from tools import settings

GUILD_ID = 5150


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    settings._cache.clear()
    yield
    settings._cache.clear()


def _seed(blob, guild_id=GUILD_ID):
    """Put a guild_settings blob straight into the read-through cache."""
    settings._cache[("guild_settings", guild_id)] = dict(blob)


class _RaisingPool:
    async def fetchval(self, *args, **kwargs):
        raise RuntimeError("database is on fire")


# ---------------------------------------------------------------------------
# The key set is a contract
# ---------------------------------------------------------------------------


def test_the_six_keys_are_the_declared_contract():
    assert guild_config.KEYS == {
        "tickets_panel_channel",
        "tickets_support_role",
        "tickets_log_channel",
        "tickets_max_open_per_user",
        "tickets_inactivity_hours",
        "tickets_panel_message",
    }


def test_every_declared_key_constant_is_in_the_key_set():
    # A key added as a constant but forgotten in KEYS would be written by the
    # bot and never recognised by the dashboard contract.
    constants = {
        value
        for name, value in vars(guild_config).items()
        if name.startswith("KEY_") and isinstance(value, str)
    }
    assert constants == set(guild_config.KEYS)


# ---------------------------------------------------------------------------
# coerce_count: clamping, not refusing
# ---------------------------------------------------------------------------


def test_coerce_count_absent_returns_the_default():
    assert guild_config.coerce_count(None, minimum=1, maximum=5, default=2) == 2


def test_coerce_count_clamps_instead_of_falling_back():
    # Out of bounds is a dashboard bug; the useful answer is the nearest legal
    # setting, NOT silently reverting to the default.
    assert guild_config.coerce_count(99, minimum=1, maximum=5, default=2) == 5
    assert guild_config.coerce_count(0, minimum=1, maximum=5, default=2) == 1
    assert guild_config.coerce_count(-7, minimum=1, maximum=5, default=2) == 1


def test_coerce_count_accepts_the_string_shapes_a_node_writer_produces():
    assert guild_config.coerce_count(" 3 ", minimum=1, maximum=5, default=2) == 3
    assert guild_config.coerce_count("3.9", minimum=1, maximum=5, default=2) == 3


def test_coerce_count_rejects_bool_and_junk():
    # True is an int in Python; a stray toggle must never read as the count 1.
    assert guild_config.coerce_count(True, minimum=1, maximum=5, default=2) == 2
    assert guild_config.coerce_count(False, minimum=1, maximum=5, default=2) == 2
    for junk in ("many", "", [], {}, object(), float("nan"), float("inf")):
        assert guild_config.coerce_count(junk, minimum=1, maximum=5, default=2) == 2


# ---------------------------------------------------------------------------
# coerce_text
# ---------------------------------------------------------------------------


def test_coerce_text_trims_and_bounds():
    assert guild_config.coerce_text("  hi  ", limit=10) == "hi"
    assert guild_config.coerce_text("x" * 50, limit=10) == "x" * 10


def test_coerce_text_treats_blank_as_absent():
    # A blurb cleared to spaces must render as "no blurb", not as an empty line.
    assert guild_config.coerce_text("   ", limit=10) is None
    assert guild_config.coerce_text("", limit=10) is None
    assert guild_config.coerce_text(None, limit=10) is None
    assert guild_config.coerce_text(12345, limit=10) is None


# ---------------------------------------------------------------------------
# Readers: absent means OFF, never a materialised default
# ---------------------------------------------------------------------------


async def test_an_untouched_guild_has_tickets_disabled(fake_pool):
    _seed({})
    assert await guild_config.panel_channel_id(fake_pool, GUILD_ID) is None
    assert await guild_config.support_role_id(fake_pool, GUILD_ID) is None
    assert await guild_config.log_channel_id(fake_pool, GUILD_ID) is None
    assert await guild_config.panel_message(fake_pool, GUILD_ID) is None


async def test_an_untouched_guild_gets_the_bot_defaults_for_the_numbers(fake_pool):
    _seed({})
    assert await guild_config.max_open_per_user(fake_pool, GUILD_ID) == 2
    assert await guild_config.inactivity_hours(fake_pool, GUILD_ID) == 72


async def test_ids_arriving_as_strings_still_resolve(fake_pool):
    # JS cannot hold a snowflake in a Number, so the dashboard writes strings.
    _seed(
        {
            guild_config.KEY_PANEL_CHANNEL: "123456789012345678",
            guild_config.KEY_SUPPORT_ROLE: "876543210987654321",
            guild_config.KEY_LOG_CHANNEL: "111111111111111111",
        }
    )
    assert await guild_config.panel_channel_id(fake_pool, GUILD_ID) == 123456789012345678
    assert await guild_config.support_role_id(fake_pool, GUILD_ID) == 876543210987654321
    assert await guild_config.log_channel_id(fake_pool, GUILD_ID) == 111111111111111111


async def test_a_cleared_panel_channel_reads_as_disabled(fake_pool):
    # /ticket disable writes null rather than deleting the key.
    _seed({guild_config.KEY_PANEL_CHANNEL: None})
    assert await guild_config.panel_channel_id(fake_pool, GUILD_ID) is None


async def test_stored_numbers_are_clamped_on_read(fake_pool):
    _seed(
        {
            guild_config.KEY_MAX_OPEN_PER_USER: 400,
            guild_config.KEY_INACTIVITY_HOURS: 0,
        }
    )
    assert await guild_config.max_open_per_user(fake_pool, GUILD_ID) == 5
    assert await guild_config.inactivity_hours(fake_pool, GUILD_ID) == 1


async def test_a_settings_failure_degrades_to_the_default_rather_than_raising():
    pool = _RaisingPool()
    assert await guild_config.panel_channel_id(pool, GUILD_ID) is None
    assert await guild_config.max_open_per_user(pool, GUILD_ID) == 2
    assert await guild_config.inactivity_hours(pool, GUILD_ID) == 72


async def test_no_pool_and_no_guild_read_as_absent(fake_pool):
    assert await guild_config.panel_channel_id(None, GUILD_ID) is None
    assert await guild_config.panel_channel_id(fake_pool, None) is None


def test_the_auto_archive_duration_is_one_discord_accepts():
    # Discord accepts exactly these four values; anything else is a 400.
    assert guild_config.AUTO_ARCHIVE_MINUTES in (60, 1440, 4320, 10080)
    assert guild_config.AUTO_ARCHIVE_MINUTES == 4320


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class _Perms:
    def __init__(self, **granted):
        for name in preflight.SETUP_PERMISSIONS:
            setattr(self, name, granted.get(name, True))


def test_nothing_is_missing_when_everything_is_granted():
    assert preflight.missing_permissions(_Perms()) == []


def test_missing_permissions_reports_them_in_declaration_order():
    perms = _Perms(manage_threads=False, view_channel=False)
    assert preflight.missing_permissions(perms) == [
        "view_channel",
        "manage_threads",
    ]


def test_absent_permissions_object_means_everything_is_missing():
    assert preflight.missing_permissions(None) == list(preflight.SETUP_PERMISSIONS)


def test_an_unknown_attribute_counts_as_missing_not_as_granted():
    # If discord.py ever renames one of these, the safe direction is to SAY so.
    class _Stale:
        pass

    assert preflight.missing_permissions(_Stale()) == list(
        preflight.SETUP_PERMISSIONS
    )


def test_the_thread_permissions_the_private_room_needs_are_all_required():
    for name in (
        "create_private_threads",
        "send_messages_in_threads",
        "manage_threads",
        "read_message_history",
    ):
        assert name in preflight.SETUP_PERMISSIONS
        assert name in preflight.OPEN_PERMISSIONS


def test_opening_a_ticket_does_not_require_the_panel_only_permissions():
    # embed_links is needed to POST the panel; a member must still be able to
    # open a ticket if it is later removed.
    assert "embed_links" not in preflight.OPEN_PERMISSIONS
    assert "embed_links" in preflight.SETUP_PERMISSIONS


def test_every_required_permission_has_a_human_label():
    assert set(preflight.SETUP_PERMISSIONS) <= set(preflight._LABELS)


def test_describe_renders_labels_and_falls_back_to_the_raw_name():
    assert preflight.describe(["manage_threads"]) == "Manage Threads"
    assert preflight.describe(["not_a_permission"]) == "not_a_permission"
    assert preflight.describe([]) == ""
