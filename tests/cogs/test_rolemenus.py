"""Unit tests for the role-menu emoji validator and the cog_load read seam."""

import json
import types

from cogs.config.rolemenus import RoleMenus, valid_emoji
from tools import role_menus


def test_unicode_emoji_accepted():
    assert valid_emoji("🔵") is True
    assert valid_emoji("🎯") is True


def test_custom_emoji_token_accepted():
    assert valid_emoji("<:smile:123456789>") is True
    assert valid_emoji("<a:party:987654321>") is True


def test_plain_text_rejected():
    assert valid_emoji("garbage-not-emoji") is False
    assert valid_emoji("blue") is False
    assert valid_emoji("") is False
    assert valid_emoji(None) is False


def test_ascii_mixed_with_emoji_rejected():
    # "letter + emoji" would 400 on send, so it must not pass the gate.
    assert valid_emoji("x🔵") is False
    assert valid_emoji("blue🔵") is False


def test_long_string_rejected():
    assert valid_emoji("🔵🔵🔵🔵🔵🔵🔵🔵🔵") is False  # too long to be one emoji


# ---------------------------------------------------------------------------
# cog_load: the only DB -> persistent view seam
# ---------------------------------------------------------------------------
def _bot_with(fake_pool):
    """Minimal bot stub: a pool plus an add_view that records what it registers."""
    registered = []

    def add_view(view, *, message_id=None):
        registered.append((view, message_id))

    return types.SimpleNamespace(db_pool=fake_pool, add_view=add_view), registered


def _select_of(view):
    return view.children[0]


async def test_cog_load_normalises_string_role_ids_from_the_db(fake_pool):
    """A row written with STRING role_ids (how a Node writer serialises a
    snowflake) must be re-normalised at this read seam. Left as strings they
    would never match the int role ids the select callback compares against,
    so every pick would silently do nothing.
    """
    fake_pool.fetch_return = [
        {
            "message_id": 999,
            "config": {
                "exclusive": False,
                "options": [
                    {"role_id": "445566", "label": "Gamer"},
                    {"role_id": "778899", "label": "Artist"},
                ],
            },
        }
    ]
    bot, registered = _bot_with(fake_pool)
    cog = RoleMenus(bot)

    await cog.cog_load()

    assert len(registered) == 1
    view, message_id = registered[0]
    assert message_id == 999
    options = _select_of(view).config["options"]
    assert [o["role_id"] for o in options] == [445566, 778899]
    assert all(isinstance(o["role_id"], int) for o in options)
    assert cog._menu_ids == {999}


async def test_cog_load_normalised_ids_actually_match_a_selection(fake_pool):
    """The point of the normalisation: the registered view's role ids must
    resolve against a live pick. With string ids resolve_selection matches
    nothing and the menu is a silent no-op.
    """
    fake_pool.fetch_return = [
        {
            "message_id": 1,
            "config": {"options": [{"role_id": "445566", "label": "Gamer"}]},
        }
    ]
    bot, registered = _bot_with(fake_pool)
    cog = RoleMenus(bot)

    await cog.cog_load()

    menu_ids = [o["role_id"] for o in _select_of(registered[0][0]).config["options"]]
    # What the callback does: int(value) from the select, compared to menu_ids.
    to_add, to_remove = role_menus.resolve_selection(
        [445566], [], menu_ids, exclusive=False
    )
    assert to_add == {445566}
    assert to_remove == set()


async def test_cog_load_normalises_a_json_string_config(fake_pool):
    """Same seam when the driver hands back the JSONB column as text."""
    fake_pool.fetch_return = [
        {
            "message_id": 5,
            "config": json.dumps({"options": [{"role_id": "42", "label": "X"}]}),
        }
    ]
    bot, registered = _bot_with(fake_pool)
    cog = RoleMenus(bot)

    await cog.cog_load()

    assert _select_of(registered[0][0]).config["options"][0]["role_id"] == 42


async def test_cog_load_drops_junk_role_ids(fake_pool):
    """Normalising also cleans the row: an unusable role_id must not reach the
    select (it would render an option that can never grant anything).
    """
    fake_pool.fetch_return = [
        {
            "message_id": 7,
            "config": {
                "options": [
                    {"role_id": "0"},
                    {"role_id": True},
                    {"role_id": "not-an-id"},
                    {"role_id": 123, "label": "Real"},
                ]
            },
        }
    ]
    bot, registered = _bot_with(fake_pool)
    cog = RoleMenus(bot)

    await cog.cog_load()

    options = _select_of(registered[0][0]).config["options"]
    assert [o["role_id"] for o in options] == [123]
