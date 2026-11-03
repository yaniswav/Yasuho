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


class _Feeds:
    """The feed cog and pool seams the help line reads."""

    def __init__(self, rows=None, anime_channels=(), archived=(), boom=False):
        self.rows, self.boom, self.calls = rows or [], boom, 0
        self.anime_channels, self.archived = set(anime_channels), set(archived)
        self.queries = []

    async def _feeds_for_guild(self, guild_id):
        self.calls += 1
        if self.boom:
            raise RuntimeError("db down")
        return self.rows

    async def _feed_archival(self, guild_id, feeds=None):
        archived = self.archived
        return types.SimpleNamespace(is_active=lambda cid: cid not in archived)

    async def fetch(self, sql, guild_id, channel_ids):
        self.queries.append((sql, channel_ids))
        return [{"channel_id": c} for c in channel_ids if c in self.anime_channels]


def _feed(cid, enabled=True):
    return {"channel_id": cid, "enabled": enabled}


def _home(feed_cog, *, leveling_on=False, manager=True):
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(
            name="Yasuho", display_avatar=types.SimpleNamespace(url="https://x/a.png")
        ),
        get_cog=lambda name: {
            "Leveling": types.SimpleNamespace(is_enabled=lambda gid: leveling_on),
            "AniListFeed": feed_cog,
        }.get(name),
        db_pool=feed_cog,
    )
    guild = types.SimpleNamespace(id=1, member_count=3)
    author = types.SimpleNamespace(
        id=9, guild_permissions=discord.Permissions(manage_guild=manager)
    )
    context = types.SimpleNamespace(author=author, bot=bot, clean_prefix="?", guild=guild)
    return help_mod.HelpView(types.SimpleNamespace(context=context), [])


async def _home_text(view):
    container = discord.ui.Container()
    await view._build_home(container)
    return "\n".join(c.content for c in container.walk_children() if isinstance(c, discord.ui.TextDisplay))


async def test_home_counts_only_enabled_unarchived_feeds_with_an_anime_title():
    feeds = _Feeds(
        [_feed(1), _feed(2), _feed(3, enabled=False), _feed(4), _feed(5)],
        anime_channels={1, 3, 4, 5},  # 2 tracks no anime; 3 is disabled
        archived={4},
    )
    text = await _home_text(_home(feeds))
    assert "Anime alerts: set up in 2 channels" in text
    assert feeds.calls == 1
    assert len(feeds.queries) == 1 and feeds.queries[0][1] == [1, 2, 5]


async def test_a_feed_with_no_anime_title_is_not_set_up():
    text = await _home_text(_home(_Feeds([_feed(1)], anime_channels=())))
    assert "not set up" in text and "set up in" not in text


async def test_home_shows_how_to_set_up_to_managers_only():
    feeds = _Feeds([])
    text = await _home_text(_home(feeds))
    assert "Anime alerts: not set up - `/anilistfeed quicksetup`" in text
    plain = await _home_text(_home(_Feeds([]), manager=False))
    assert "Anime alerts" not in plain and "quicksetup" not in plain
    assert "Prefix:" in plain


async def test_home_shows_the_count_to_everyone():
    feeds = _Feeds([_feed(1)], anime_channels={1})
    assert "set up in 1 channel" in await _home_text(_home(feeds, manager=False))


async def test_home_leveling_off_is_not_a_fault():
    text = await _home_text(_home(_Feeds([])))
    assert "Leveling: off" in text
    assert "\u274c" not in text and "\U0001F534" not in text
    assert "Leveling: on" in await _home_text(_home(_Feeds([]), leveling_on=True))


@pytest.mark.parametrize("feed_cog", [None, _Feeds(boom=True)], ids=["cog-missing", "query-fails"])
async def test_home_never_breaks_when_the_anime_line_is_unavailable(feed_cog):
    view = _home(feed_cog or _Feeds([]))
    if feed_cog is None:
        view.bot.get_cog = lambda name: types.SimpleNamespace(is_enabled=lambda gid: False) if name == "Leveling" else None
    text = await _home_text(view)
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
