"""Member milestones: exactly-once announcing, high-water init, Yasuho+ gating."""

import asyncio
import json
import types
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.config import milestones as ms
from cogs.config.milestones import MilestonePanel, Milestones
from tools import premium, settings


class Pool:
    """guild_settings blobs and milestone_state marks, with a query journal."""

    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.state = {}
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        await asyncio.sleep(0)  # let a concurrent join interleave
        if "FROM guild_settings" in query:
            blob = self.blobs.get(args[0])
            return None if blob is None else json.dumps(blob)
        if query.startswith("INSERT INTO milestone_state"):
            guild_id, level = args
            if "GREATEST" in query:
                self.state[guild_id] = max(self.state.get(guild_id, 0), level)
            else:
                self.state.setdefault(guild_id, level)
            return self.state[guild_id]
        if query.startswith("UPDATE milestone_state"):
            guild_id, level = args
            if guild_id in self.state and self.state[guild_id] < level:
                self.state[guild_id] = level
                return level
            return None
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.queries.append(query)
        self.blobs.setdefault(args[0], {})[args[1]] = json.loads(args[2])


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    settings.invalidate_all()
    yield
    settings.invalidate_all()


def _guild(count, channel=None, gid=1):
    channel = channel or types.SimpleNamespace(
        id=9,
        permissions_for=lambda me: discord.Permissions(view_channel=True, send_messages=True),
        send=AsyncMock(),
    )
    guild = types.SimpleNamespace(
        id=gid, name="Cafe", member_count=count, me=object(),
        preferred_locale=discord.Locale.american_english,
        get_channel=lambda cid: channel if cid == 9 else None,
    )
    return guild, channel


def _member(guild, name="Nia"):
    return types.SimpleNamespace(guild=guild, display_name=name, bot=False)


def _cog(pool, plus=False):
    limits = premium.GUILD_PREMIUM if plus else premium.GUILD_FREE
    bot = types.SimpleNamespace(
        db_pool=pool, premium=types.SimpleNamespace(for_guild=lambda gid: limits)
    )
    return Milestones(bot)


ENABLED = {"enabled": True, "channel_id": 9}


async def test_a_guild_without_the_feature_costs_no_query_once_its_blob_is_cached():
    pool = Pool({1: {"locale": "en"}})
    cog = _cog(pool)
    guild, channel = _guild(100)
    await cog.on_member_join(_member(guild))  # warms the shared guild blob
    pool.queries.clear()
    await cog.on_member_join(_member(guild))
    assert pool.queries == []
    channel.send.assert_not_awaited()


async def test_two_concurrent_joins_post_once():
    pool = Pool({1: {"milestones": ENABLED}})
    pool.state[1] = 50
    cog = _cog(pool)
    guild, channel = _guild(100)
    await asyncio.gather(
        cog.on_member_join(_member(guild)), cog.on_member_join(_member(guild, "Bo"))
    )
    assert channel.send.await_count == 1
    assert pool.state[1] == 100
    assert "100 members" in channel.send.await_args.args[0]


async def test_a_count_that_drops_and_rises_again_never_re_announces():
    pool = Pool({1: {"milestones": ENABLED}})
    pool.state[1] = 50
    cog = _cog(pool)
    guild, channel = _guild(100)
    await cog.on_member_join(_member(guild))
    guild.member_count = 99
    await cog.on_member_join(_member(guild))
    guild.member_count = 100
    pool.queries.clear()
    await cog.on_member_join(_member(guild))
    assert channel.send.await_count == 1
    assert pool.queries == [] or all("guild_settings" in q for q in pool.queries)


async def test_several_milestones_crossed_at_once_post_only_the_highest():
    pool = Pool({1: {"milestones": ENABLED}})
    pool.state[1] = 50
    cog = _cog(pool)
    cog._floor[1] = 50  # warm: a burst of joins moved the count past several
    guild, channel = _guild(2600)
    await cog.on_member_join(_member(guild))
    assert channel.send.await_count == 1
    assert "2,500 members" in channel.send.await_args.args[0]
    assert pool.state[1] == 2500


async def test_a_cold_enabled_guild_does_not_replay_old_milestones():
    # Enabled (for instance from the dashboard) on a 3000-member server.
    pool = Pool({1: {"milestones": ENABLED}})
    cog = _cog(pool)
    guild, channel = _guild(3000)
    await cog.on_member_join(_member(guild))
    channel.send.assert_not_awaited()
    assert pool.state[1] == 2500
    # ...but the join that really crosses the next one is still celebrated.
    guild.member_count = 5000
    await cog.on_member_join(_member(guild))
    assert channel.send.await_count == 1


async def test_a_cold_join_that_crosses_a_milestone_is_celebrated():
    pool = Pool({1: {"milestones": ENABLED}})
    cog = _cog(pool)
    guild, channel = _guild(100)
    await cog.on_member_join(_member(guild))
    assert channel.send.await_count == 1


async def test_enabling_from_the_panel_arms_the_high_water_mark():
    pool = Pool({1: {"milestones": {"channel_id": 9}}})
    cog = _cog(pool)
    guild, channel = _guild(3000)
    guild.get_channel = lambda cid: discord.Object(cid, type=discord.abc.GuildChannel)
    config = ms.normalise({"channel_id": 9})
    panel = MilestonePanel(cog, guild, 7, config)
    panel.message = None
    interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(
            is_done=lambda: False, edit_message=AsyncMock(), defer=AsyncMock()
        )
    )
    await panel._toggle(interaction)
    assert pool.blobs[1]["milestones"]["enabled"] is True
    assert pool.state[1] == 2500
    channel.send.assert_not_awaited()


async def test_enabling_without_a_channel_is_refused():
    cog = _cog(Pool())
    guild, _ = _guild(10)
    panel = MilestonePanel(cog, guild, 7, ms.normalise(None))
    response = types.SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    await panel._toggle(types.SimpleNamespace(response=response))
    response.send_message.assert_awaited_once()
    assert panel.config["enabled"] is False


CUSTOM = {**ENABLED, "milestones": [75, 150], "message": "{user} made {server} reach {members}!"}


async def test_custom_milestones_and_message_need_yasuho_plus():
    guild, channel = _guild(75)
    for plus, expected in ((False, "75 members"), (True, "made Cafe reach 75!")):
        channel.send.reset_mock()
        pool = Pool({1: {"milestones": CUSTOM}})
        pool.state[1] = 50  # 50 already celebrated
        cog = _cog(pool, plus=plus)
        await cog.on_member_join(_member(guild))
        if plus:
            assert expected in channel.send.await_args.args[0]
        else:
            # Free: the fixed list applies, 75 is not on it, nothing is posted.
            channel.send.assert_not_awaited()
        settings.invalidate_all()


async def test_a_lapsed_subscription_keeps_the_stored_custom_config():
    pool = Pool({1: {"milestones": CUSTOM}})
    cog = _cog(pool, plus=False)
    config = ms.normalise(pool.blobs[1]["milestones"])
    assert config["milestones"] == [75, 150] and config["message"]
    assert ms.effective_milestones(config, False) == ms.FREE_MILESTONES
    assert ms.effective_milestones(config, True) == (75, 150)
    assert cog.bot.db_pool.blobs[1] == {"milestones": CUSTOM}  # nothing was rewritten


async def test_customize_is_refused_for_a_free_guild_and_opens_for_plus():
    for plus in (False, True):
        guild, _ = _guild(10)
        panel = MilestonePanel(_cog(Pool(), plus=plus), guild, 7, ms.normalise(None))
        response = types.SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock())
        interaction = types.SimpleNamespace(
            response=response, user=types.SimpleNamespace(id=7)
        )
        await panel._customize(interaction)
        assert response.send_modal.await_count == (1 if plus else 0)
        assert response.send_message.await_count == (0 if plus else 1)


async def test_a_failed_send_is_logged_and_never_raises(caplog):
    pool = Pool({1: {"milestones": ENABLED}})
    pool.state[1] = 50
    cog = _cog(pool)
    channel = types.SimpleNamespace(
        id=9,
        permissions_for=lambda me: discord.Permissions(view_channel=True),
        send=AsyncMock(),
    )
    guild, _ = _guild(100, channel)
    await cog.on_member_join(_member(guild))
    assert "MILESTONE-FAIL guild=1 reason=missing_permissions" in caplog.text
    channel.send.assert_not_awaited()


def test_parse_milestones():
    assert ms.parse_milestones("300, 75 150,75") == [75, 150, 300]
    assert ms.parse_milestones("") == []
    for bad in ("0", "abc", "-5", "99999999", ",".join(str(i) for i in range(1, 12))):
        with pytest.raises(ValueError):
            ms.parse_milestones(bad)


def test_the_fixed_list_and_highest_passed():
    assert ms.FREE_MILESTONES[0] == 50 and ms.FREE_MILESTONES[-1] == 100000
    assert ms.highest_passed(ms.FREE_MILESTONES, 49) == 0
    assert ms.highest_passed(ms.FREE_MILESTONES, 50) == 50
    assert ms.highest_passed(ms.FREE_MILESTONES, 2499) == 1000


def test_the_schema_and_retention_cover_the_new_table():
    from pathlib import Path

    from tools import retention

    ddl = (Path(__file__).parents[2] / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS milestone_state" in ddl
    assert "CHECK (last_milestone >= 0)" in ddl
    assert any(name == "milestone_state" for name, _q in retention.GUILD_DELETE_QUERIES)
    assert "FROM milestone_state" in retention.STORED_GUILD_IDS_QUERY


async def test_two_cold_joins_after_a_restart_still_post_the_crossed_milestone():
    # Stored mark 50 from before the restart; joins at 100 and 101 race while cold.
    pool = Pool({1: {"milestones": ENABLED}})
    pool.state[1] = 50
    cog = _cog(pool)
    guild, channel = _guild(100)
    first = _member(guild)
    guild_b = types.SimpleNamespace(**{**vars(guild), "member_count": 101})
    await asyncio.gather(cog.on_member_join(first), cog.on_member_join(_member(guild_b, "Bo")))
    assert pool.state[1] == 100
    assert channel.send.await_count == 1


def test_a_long_template_is_capped_to_one_discord_message():
    guild, _channel = _guild(100)
    guild.name = "x" * 100
    text = ms.render_message(guild, _member(guild), 100, "{server}" * 62)
    assert len(text) == 2000
