"""Entry points to the anime alerts wizard (lot O3): /config panel, the empty
/anilistfeed panel, the /anilistfeed set follow-up, the channel schedule dead
end and the /help home. The wizard itself is covered by
test_anilist_alerts_wizard.py; here each entry point is only checked to reach
it (or to point at it), never to duplicate it."""

from __future__ import annotations

import ast
import pathlib
import types
from unittest import mock

import discord
import pytest

from cogs.anilist import feed_views
from cogs.config import settings as config_settings
from cogs.system import help as help_mod
from tools.i18n import _


def _texts(view):
    return "\n".join(
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )


# --- /config panel ---------------------------------------------------------


def _panel(monkeypatch=None):
    state = {
        "prefix": "?", "autorole": None, "leveling": False, "starboard": None,
        "automod": None, "modlog": None, "welcome": None,
    }
    guild = types.SimpleNamespace(name="G", id=1, icon=None)
    return config_settings.ConfigPanel(types.SimpleNamespace(), 9, guild, state)


def test_config_select_lists_anime_alerts_first_or_second():
    panel = _panel()
    select = next(c for c in panel.walk_children() if isinstance(c, config_settings.ConfigSelect))
    values = [o.value for o in select.options]
    assert values.index("anime") <= 1
    assert len(values) <= 25


async def test_config_panel_anime_choice_opens_the_wizard(monkeypatch):
    opened = []

    async def fake_open(interaction):
        opened.append(interaction)

    monkeypatch.setattr("cogs.anilist.alerts_wizard.open_anime_alerts_wizard", fake_open)
    panel = _panel()
    panel._rerender = mock.AsyncMock()
    interaction = object()
    await panel.handle_select(interaction, "anime")
    panel._rerender.assert_awaited_once_with(interaction)
    assert opened == [interaction]


# --- empty /anilistfeed panel ---------------------------------------------


def _feed_panel():
    cog = types.SimpleNamespace(
        _guild_limits=lambda gid: types.SimpleNamespace(
            max_feeds_per_guild=2, max_subs_per_feed=50, max_follows_per_feed=10
        )
    )
    guild = types.SimpleNamespace(id=1, get_channel=lambda cid: None)
    return feed_views.AniListFeedPanel(cog, guild, 9, [], None, [])


def test_empty_panel_explains_a_feed_and_offers_quick_setup():
    panel = _feed_panel()
    buttons = [c for c in panel.walk_children() if isinstance(c, feed_views._QuickSetupButton)]
    assert len(buttons) == 1 and buttons[0].label == _("Quick setup (anime alerts)")
    assert "new episodes and chapters" in _texts(panel)


async def test_quick_setup_button_opens_the_wizard(monkeypatch):
    opened = []

    async def fake_open(interaction):
        opened.append(interaction)

    monkeypatch.setattr(feed_views, "open_anime_alerts_wizard", fake_open)
    interaction = object()
    await feed_views._QuickSetupButton().callback(interaction)
    assert opened == [interaction]


# --- text pointers ---------------------------------------------------------


def test_feed_set_and_schedule_dead_end_point_at_quicksetup():
    root = pathlib.Path(feed_views.__file__).parent
    assert "/anilistfeed quicksetup" in (root / "feed.py").read_text()
    assert "/anilistfeed quicksetup" in (root / "schedule.py").read_text()


# --- /help home ------------------------------------------------------------


def test_anime_and_manga_is_the_first_help_category():
    assert help_mod.CATEGORIES[0][1] == "Anime & Manga"


def _home(feed_cog, *, leveling_on=False):
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(
            name="Yasuho", display_avatar=types.SimpleNamespace(url="https://x/a.png")
        ),
        get_cog=lambda name: {
            "Leveling": types.SimpleNamespace(is_enabled=lambda gid: leveling_on),
            "AniListFeed": feed_cog,
        }.get(name),
    )
    guild = types.SimpleNamespace(id=1, member_count=3)
    context = types.SimpleNamespace(author=types.SimpleNamespace(id=9), bot=bot, clean_prefix="?", guild=guild)
    view = help_mod.HelpView(types.SimpleNamespace(context=context), [])
    return view


async def _home_text(view):
    container = discord.ui.Container()
    await view._build_home(container)
    return "\n".join(c.content for c in container.walk_children() if isinstance(c, discord.ui.TextDisplay))


class _Feeds:
    def __init__(self, rows=None, boom=False):
        self.rows, self.boom, self.calls = rows or [], boom, 0

    async def _feeds_for_guild(self, guild_id):
        self.calls += 1
        if self.boom:
            raise RuntimeError("db down")
        return self.rows


async def test_home_shows_set_up_channels_count():
    feeds = _Feeds([{"enabled": True}, {"enabled": True}, {"enabled": False}])
    text = await _home_text(_home(feeds))
    assert "Anime alerts: set up in 2 channels" in text
    assert feeds.calls == 1


async def test_home_shows_how_to_set_up_when_there_is_no_feed():
    text = await _home_text(_home(_Feeds([])))
    assert "Anime alerts: not set up - `/anilistfeed quicksetup`" in text


async def test_home_leveling_off_is_not_a_fault():
    text = await _home_text(_home(_Feeds([])))
    assert "Leveling: off" in text
    assert "❌" not in text and "\U0001F534" not in text
    assert "Leveling: on" in await _home_text(_home(_Feeds([]), leveling_on=True))


@pytest.mark.parametrize("feed_cog", [None, _Feeds(boom=True)], ids=["cog-missing", "query-fails"])
async def test_home_never_breaks_when_the_anime_line_is_unavailable(feed_cog):
    text = await _home_text(_home(feed_cog))
    assert "Anime alerts" not in text
    assert "Prefix:" in text and "Leveling: off" in text


def test_touched_modules_have_no_fstring_prose_in_new_lines():
    # The wizard entry points added here use _() only; guard the settings hook.
    tree = ast.parse(pathlib.Path(config_settings.__file__).read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_open_anime_alerts"
    )
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr)]
