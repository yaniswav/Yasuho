"""Suggestion box: config, votes and coalescing, staff gate, statuses, digest, privacy."""

import asyncio
import datetime
import inspect
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.community.suggestions import cog as sg_cog
from cogs.community.suggestions import panel, render, store, views
from cogs.community.suggestions.cog import Suggestions
from tools import i18n, premium, privacy, retention, settings

NOW = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.timezone.utc)
CONFIG = {"enabled": True, "channel_id": 9, "staff_role_id": 7}


def row(**over):
    base = {
        "id": 1, "guild_id": 1, "number": 3, "channel_id": 9, "message_id": 50,
        "thread_id": 60, "author_id": 10, "title": "Dark mode", "details": "Please",
        "status": "open", "status_reason": None, "created_at": NOW, "previous": "open",
    }
    base.update(over)
    return base


class FakePool:
    """Records the statements; returns what a test scripted."""

    def __init__(self):
        self.calls = []
        self.script = {}

    def _answer(self, query):
        self.calls.append(query)
        for key, value in self.script.items():
            if key in query:
                return value
        return None

    async def fetchrow(self, query, *args):
        return self._answer(query)

    async def fetch(self, query, *args):
        return self._answer(query) or []

    async def execute(self, query, *args):
        self._answer(query)

    def acquire(self):
        pool = self

        class _Conn:
            def transaction(self_):
                class _Tx:
                    async def __aenter__(self_):
                        return None

                    async def __aexit__(self_, *exc):
                        return False

                return _Tx()

            async def execute(self_, query, *args):
                pool.calls.append(query)

            async def fetchrow(self_, query, *args):
                pool.calls.append(query)
                return row(number=1)

        class _Acq:
            async def __aenter__(self_):
                return _Conn()

            async def __aexit__(self_, *exc):
                return False

        return _Acq()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    settings.invalidate_all()
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=dict(CONFIG)))
    monkeypatch.setattr(i18n, "resolve_guild_locale", AsyncMock(return_value="en"))
    yield
    settings.invalidate_all()


def make_cog(plus=False, pool=None):
    limits = premium.GUILD_PREMIUM if plus else premium.GUILD_FREE
    bot = types.SimpleNamespace(
        db_pool=pool or FakePool(),
        premium=types.SimpleNamespace(for_guild=lambda gid: limits),
        get_cog=lambda name: None,
        get_guild=lambda gid: None,
        add_dynamic_items=MagicMock(),
        remove_dynamic_items=MagicMock(),
    )
    return Suggestions(bot)


def member(uid=20, manage=False, roles=()):
    return types.SimpleNamespace(
        id=uid, display_name="Ann", display_avatar=types.SimpleNamespace(url="http://a"),
        guild_permissions=discord.Permissions(manage_guild=manage),
        roles=[types.SimpleNamespace(id=r) for r in roles],
    )


def interaction(user, guild=None, message=None):
    return types.SimpleNamespace(
        user=user, guild=guild, guild_id=1, message=message,
        response=types.SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
            is_done=lambda: False,
        ),
        followup=types.SimpleNamespace(send=AsyncMock()),
    )


# -- config and pure helpers --------------------------------------------------


def test_normalise_types_everything_and_caps_custom_statuses():
    cfg = store.normalise(
        {
            "enabled": "yes", "channel_id": "9", "staff_role_id": 0, "digest": 1,
            "digest_weekday": 9,
            "custom_statuses": [{"label": f"s{i}", "colour": 5} for i in range(9)]
            + [{"label": "s1"}, "junk", {"label": "x" * 80, "colour": "red"}],
        }
    )
    assert cfg["enabled"] is False and cfg["digest"] is False and cfg["digest_weekday"] == 0
    assert cfg["channel_id"] == 9 and cfg["staff_role_id"] is None
    assert len(cfg["custom_statuses"]) == store.MAX_CUSTOM_STATUSES
    assert store.normalise(None)["custom_statuses"] == []
    long = store.normalise({"custom_statuses": [{"label": "x" * 80, "colour": "red"}]})
    assert len(long["custom_statuses"][0]["label"]) == store.LABEL_MAX
    assert long["custom_statuses"][0]["colour"] == store.DEFAULT_COLOUR


def test_next_digest_is_the_next_monday_0900_utc():
    wednesday = datetime.datetime(2026, 10, 28, 12, tzinfo=datetime.timezone.utc)
    nxt = store.next_digest(wednesday)
    assert (nxt.weekday(), nxt.hour, nxt.minute) == (0, 9, 0) and nxt > wednesday
    monday_early = datetime.datetime(2026, 11, 2, 8, tzinfo=datetime.timezone.utc)
    assert store.next_digest(monday_early).day == 2
    monday_late = datetime.datetime(2026, 11, 2, 10, tzinfo=datetime.timezone.utc)
    assert store.next_digest(monday_late).day == 9


def test_parse_colour():
    assert store.parse_colour("#ff0000") == 0xFF0000
    assert store.parse_colour("zzzzzz") is None and store.parse_colour("12") is None


def test_closed_view_disables_the_vote_buttons_only():
    closed = views.SuggestionView(1, 4, 2, closed=True)
    up, down, status = closed.children
    assert up.item.disabled and down.item.disabled and not status.item.disabled
    assert up.item.label == "4" and down.item.label == "2"
    opened = views.SuggestionView(1)
    assert not any(c.item.disabled for c in opened.children)


def test_embed_carries_number_status_votes_and_reason():
    cfg = store.normalise({"custom_statuses": [{"label": "Planned", "colour": 0x123456}]})
    embed = render.build_embed(
        row(status="c:Planned", status_reason="soon"), 3, 1, "Ann", "http://a", cfg
    )
    assert embed.title == "#3 Dark mode" and embed.colour.value == 0x123456
    assert [f.name for f in embed.fields] == ["Status", "Votes", "Reason"]
    assert "3" in embed.fields[1].value and embed.fields[0].value == "Planned"


def test_templates_are_disjoint_and_match_their_ids():
    import re

    for template, cid in (
        (views.UP_TEMPLATE, "sg:up:12"), (views.DOWN_TEMPLATE, "sg:dn:12"),
        (views.STATUS_TEMPLATE, "sg:st:12"),
    ):
        assert re.fullmatch(template, cid)
    assert not re.fullmatch(views.UP_TEMPLATE, "sg:dn:12")


async def test_cog_registers_and_removes_its_dynamic_items():
    cog = make_cog()
    await cog.cog_load()
    cog.bot.add_dynamic_items.assert_called_once_with(*views.DYNAMIC_ITEMS)
    cog.cog_unload()
    cog.bot.remove_dynamic_items.assert_called_once_with(*views.DYNAMIC_ITEMS)


# -- votes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,text,refreshes",
    [("cast", "recorded", 1), ("removed", "removed", 1), ("closed", "closed", 0),
     ("missing", "no longer exists", 0)],
)
async def test_vote_outcomes(monkeypatch, outcome, text, refreshes):
    cog = make_cog()
    monkeypatch.setattr(store, "toggle_vote", AsyncMock(return_value=outcome))
    cog._request_refresh = MagicMock()
    ix = interaction(member(), guild=types.SimpleNamespace(id=1))
    await cog.vote(ix, 1, 1)
    ix.response.defer.assert_awaited_once()  # acknowledged before any database work
    assert text in ix.followup.send.await_args.args[0]
    assert cog._request_refresh.call_count == refreshes


async def test_a_vote_burst_costs_one_edit_and_the_last_message_wins(monkeypatch):
    monkeypatch.setattr(sg_cog, "EDIT_DELAY", 0.01)
    cog = make_cog()
    cog.refresh_message = AsyncMock()
    guild = types.SimpleNamespace(id=1)
    for n in range(10):
        cog._request_refresh(1, guild, f"message-{n}")
    cog._request_refresh(2, guild, "other")
    await asyncio.gather(*cog._tasks)
    assert cog.refresh_message.await_count == 2  # one per message
    calls = {c.args[1]: c.args[2] for c in cog.refresh_message.await_args_list}
    assert calls == {1: "message-9", 2: "other"}
    assert cog._pending == {}
    cog._request_refresh(1, guild, "again")  # a new window opens afterwards
    await asyncio.gather(*cog._tasks)
    assert cog.refresh_message.await_count == 3


async def test_refresh_edits_with_no_mentions_and_the_closed_view(monkeypatch):
    cog = make_cog()
    monkeypatch.setattr(store, "get", AsyncMock(return_value=row(status="rejected")))
    monkeypatch.setattr(store, "counts", AsyncMock(return_value=(5, 2)))
    message = types.SimpleNamespace(
        embeds=[discord.Embed().set_author(name="Ann", icon_url="http://a")], edit=AsyncMock()
    )
    await cog.refresh_message(types.SimpleNamespace(id=1), 1, message)
    kwargs = message.edit.await_args.kwargs
    assert kwargs["allowed_mentions"].everyone is False and kwargs["allowed_mentions"].users is False
    up, down, _status = kwargs["view"].children
    assert up.item.disabled and down.item.disabled and up.item.label == "5"
    assert kwargs["embed"].author.name == "Ann"


# -- staff gate and statuses ---------------------------------------------------


@pytest.mark.parametrize(
    "user,allowed",
    [(member(), False), (member(manage=True), True), (member(roles=[7]), True),
     (member(roles=[8]), False)],
)
async def test_status_menu_is_staff_only(user, allowed):
    cog = make_cog()
    ix = interaction(user, guild=types.SimpleNamespace(id=1))
    await cog.open_status_menu(ix, 1)
    sent = ix.response.send_message.await_args
    assert sent.kwargs["ephemeral"] is True
    assert ("view" in sent.kwargs) is allowed
    if not allowed:
        assert "staff" in sent.args[0]


async def test_staff_status_flow_rechecks_at_the_select_and_at_the_submit(monkeypatch):
    cog = make_cog()
    set_status = AsyncMock()
    monkeypatch.setattr(store, "set_status", set_status)
    ix = interaction(member(), guild=types.SimpleNamespace(id=1))
    await cog.pick_status(ix, 1, "approved")
    ix.response.send_modal.assert_not_awaited()
    await cog.apply_status(ix, 1, "approved", "")
    set_status.assert_not_awaited()
    assert "staff" in ix.followup.send.await_args.args[0]


async def test_custom_statuses_need_yasuho_plus(monkeypatch):
    blob = {**CONFIG, "custom_statuses": [{"label": "Planned", "colour": 1}]}
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=blob))
    free, plus = make_cog(plus=False), make_cog(plus=True)
    config = store.normalise(blob)
    assert free.allowed_statuses(1, config) == list(store.BUILTIN_STATUSES)
    assert plus.allowed_statuses(1, config)[-1] == "c:Planned"
    set_status = AsyncMock()
    monkeypatch.setattr(store, "set_status", set_status)
    ix = interaction(member(manage=True), guild=types.SimpleNamespace(id=1))
    await free.apply_status(ix, 1, "c:Planned", "")
    set_status.assert_not_awaited()
    assert "not available" in ix.followup.send.await_args.args[0]


async def test_rejecting_archives_and_locks_the_thread_and_reopening_undoes_it(monkeypatch):
    cog = make_cog()
    thread = types.SimpleNamespace(edit=AsyncMock(), archived=False)
    guild = types.SimpleNamespace(id=1, get_thread=lambda tid: thread)
    monkeypatch.setattr(
        store, "set_status", AsyncMock(return_value=row(status="rejected", previous="open"))
    )
    cog.refresh_message = AsyncMock()
    ix = interaction(member(manage=True), guild=guild)
    await cog.apply_status(ix, 1, "rejected", "no")
    thread.edit.assert_awaited_once_with(archived=True, locked=True)
    cog.refresh_message.assert_awaited_once()
    thread.edit.reset_mock()
    monkeypatch.setattr(
        store, "set_status", AsyncMock(return_value=row(status="approved", previous="rejected"))
    )
    await cog.apply_status(ix, 1, "approved", "")
    thread.edit.assert_awaited_once_with(archived=False, locked=False)
    thread.edit.reset_mock()
    monkeypatch.setattr(
        store, "set_status", AsyncMock(return_value=row(status="approved", previous="open"))
    )
    await cog.apply_status(ix, 1, "approved", "")
    thread.edit.assert_not_awaited()  # open -> approved changes nothing for the thread


# -- posting -------------------------------------------------------------------


def post_world(thread_perm=True):
    message = types.SimpleNamespace(
        id=77, jump_url="http://jump", create_thread=AsyncMock(return_value=types.SimpleNamespace(id=88))
    )
    channel = types.SimpleNamespace(
        id=9, send=AsyncMock(return_value=message),
        permissions_for=lambda me: discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True,
            create_public_threads=thread_perm,
        ),
    )
    guild = types.SimpleNamespace(
        id=1, me=object(), get_channel=lambda cid: channel if cid == 9 else None
    )
    return guild, channel, message


async def test_submit_posts_with_no_mentions_opens_a_thread_and_confirms(monkeypatch):
    cog = make_cog()
    guild, channel, message = post_world()
    monkeypatch.setattr(store, "create", AsyncMock(return_value=row(message_id=0)))
    attach = AsyncMock()
    monkeypatch.setattr(store, "attach_message", attach)
    ix = interaction(member(), guild=guild)
    await cog.submit(ix, "@everyone Dark mode", "details @here")
    ix.response.defer.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["allowed_mentions"].everyone is False
    assert [c.item.custom_id for c in kwargs["view"].children] == ["sg:up:1", "sg:dn:1", "sg:st:1"]
    attach.assert_awaited_once_with(cog.bot.db_pool, 1, 77, 88)
    assert "http://jump" in ix.followup.send.await_args.args[0]


async def test_no_thread_permission_means_no_thread_and_no_error(monkeypatch):
    cog = make_cog()
    guild, channel, message = post_world(thread_perm=False)
    monkeypatch.setattr(store, "create", AsyncMock(return_value=row(message_id=0)))
    attach = AsyncMock()
    monkeypatch.setattr(store, "attach_message", attach)
    await cog.submit(interaction(member(), guild=guild), "t", "d")
    message.create_thread.assert_not_awaited()
    attach.assert_awaited_once_with(cog.bot.db_pool, 1, 77, None)


async def test_a_failed_thread_is_not_an_error(monkeypatch):
    cog = make_cog()
    guild, channel, message = post_world()
    message.create_thread = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "x"))
    monkeypatch.setattr(store, "create", AsyncMock(return_value=row(message_id=0)))
    attach = AsyncMock()
    monkeypatch.setattr(store, "attach_message", attach)
    await cog.submit(interaction(member(), guild=guild), "t", "d")
    attach.assert_awaited_once_with(cog.bot.db_pool, 1, 77, None)


async def test_a_failed_post_discards_the_row(monkeypatch):
    cog = make_cog()
    guild, channel, _message = post_world()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
    monkeypatch.setattr(store, "create", AsyncMock(return_value=row(message_id=0)))
    discard = AsyncMock()
    monkeypatch.setattr(store, "discard", discard)
    ix = interaction(member(), guild=guild)
    await cog.submit(ix, "t", "d")
    discard.assert_awaited_once_with(cog.bot.db_pool, 1)
    assert "couldn't post" in ix.followup.send.await_args.args[0]


async def test_suggest_is_rate_limited_to_one_per_member_per_minute(monkeypatch):
    cog = make_cog()
    guild, channel, _message = post_world()
    create = AsyncMock(return_value=row(message_id=0))
    monkeypatch.setattr(store, "create", create)
    monkeypatch.setattr(store, "attach_message", AsyncMock())
    ann = member(20)
    await cog.submit(interaction(ann, guild=guild), "t", "d")
    second = interaction(ann, guild=guild)
    await cog.submit(second, "t2", "d2")
    assert create.await_count == 1 and "wait" in second.followup.send.await_args.args[0]
    await cog.submit(interaction(member(21), guild=guild), "t", "d")  # another member
    assert create.await_count == 2
    # ...and the command refuses before even opening the modal
    ctx = types.SimpleNamespace(
        interaction=interaction(ann), guild=guild, author=ann, send=AsyncMock()
    )
    await Suggestions.suggest.callback(cog, ctx)
    ctx.interaction.response.send_modal.assert_not_awaited()


async def test_suggest_opens_the_modal_when_enabled_and_explains_otherwise(monkeypatch):
    cog = make_cog()
    ctx = types.SimpleNamespace(
        interaction=interaction(member()), guild=types.SimpleNamespace(id=1),
        author=member(), send=AsyncMock(),
    )
    await Suggestions.suggest.callback(cog, ctx)
    ctx.interaction.response.send_modal.assert_awaited_once()
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=None))
    ctx2 = types.SimpleNamespace(
        interaction=interaction(member()), guild=types.SimpleNamespace(id=1),
        author=member(), send=AsyncMock(),
    )
    await Suggestions.suggest.callback(cog, ctx2)
    ctx2.interaction.response.send_modal.assert_not_awaited()
    assert "not set up" in ctx2.send.await_args.args[0]


async def test_empty_title_after_strip_is_refused(monkeypatch):
    cog = make_cog()
    create = AsyncMock()
    monkeypatch.setattr(store, "create", create)
    ix = interaction(member(), guild=post_world()[0])
    await cog.submit(ix, "", "d")
    create.assert_not_awaited()


# -- number allocation -----------------------------------------------------------


async def test_number_allocation_takes_the_guild_lock_before_the_insert():
    pool = FakePool()
    created = await store.create(pool, 1, 9, 10, "t", "d")
    assert created["number"] == 1
    assert "pg_advisory_xact_lock" in pool.calls[0]
    assert "COALESCE(max(number), 0) + 1" in " ".join(pool.calls)


# -- digest ------------------------------------------------------------------------


def digest_world(cog, plus):
    channel = types.SimpleNamespace(
        id=9, send=AsyncMock(),
        permissions_for=lambda me: discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        ),
    )
    guild = types.SimpleNamespace(
        id=1, me=object(), get_channel=lambda cid: channel if cid == 9 else None
    )
    cog.bot.get_guild = lambda gid: guild if gid == 1 else None
    return channel


DIGEST_CONFIG = {**CONFIG, "digest": True}


async def test_digest_is_skipped_without_yasuho_plus_but_the_series_continues(monkeypatch):
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=DIGEST_CONFIG))
    cog = make_cog(plus=False)
    channel = digest_world(cog, False)
    order = []
    cog._write_timer = AsyncMock(side_effect=lambda *a, **k: order.append("reschedule"))
    top = AsyncMock(return_value=[])
    monkeypatch.setattr(store, "top_recent", top)
    assert await cog._digest({"guild_id": 1}) == "skip_not_plus"
    assert order == ["reschedule"]
    top.assert_not_awaited()
    channel.send.assert_not_awaited()


async def test_digest_is_skipped_when_disabled_and_not_rescheduled(monkeypatch):
    cog = make_cog(plus=True)  # CONFIG has no digest key
    digest_world(cog, True)
    cog._write_timer = AsyncMock()
    assert await cog._digest({"guild_id": 1}) == "skip_disabled"
    cog._write_timer.assert_not_awaited()
    assert await cog._digest({}) == "bad_row"


async def test_digest_reschedules_before_posting_and_posts_top_five_without_mentions(monkeypatch):
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=DIGEST_CONFIG))
    cog = make_cog(plus=True)
    channel = digest_world(cog, True)
    order = []
    cog._write_timer = AsyncMock(side_effect=lambda *a, **k: order.append("reschedule"))
    channel.send.side_effect = lambda **k: order.append("post")
    rows = [
        {"number": n, "title": "@everyone *x*", "channel_id": 9, "message_id": 50 + n, "score": 6 - n}
        for n in range(1, 6)
    ]
    monkeypatch.setattr(store, "top_recent", AsyncMock(return_value=rows))
    assert await cog._digest({"guild_id": 1}) == "sent"
    assert order == ["reschedule", "post"]
    kwargs = channel.send.call_args.kwargs
    assert kwargs["allowed_mentions"].everyone is False
    assert "@everyone" not in kwargs["embed"].description.replace("@​everyone", "")
    assert kwargs["embed"].description.count("discord.com/channels/1/9/") == 5


async def test_digest_with_nothing_to_report_posts_nothing(monkeypatch):
    monkeypatch.setattr(settings, "get_guild", AsyncMock(return_value=DIGEST_CONFIG))
    cog = make_cog(plus=True)
    channel = digest_world(cog, True)
    cog._write_timer = AsyncMock()
    monkeypatch.setattr(store, "top_recent", AsyncMock(return_value=[]))
    assert await cog._digest({"guild_id": 1}) == "skip_empty"
    channel.send.assert_not_awaited()


async def test_timer_row_is_written_only_when_wanted_and_replaced_on_save():
    pool = FakePool()
    cog = make_cog(pool=pool)
    await cog._write_timer(1, store.normalise({**DIGEST_CONFIG}), replace=True)
    text = " ".join(pool.calls)
    assert "DELETE FROM timers" in text and "INSERT INTO timers" in text
    assert "NOT EXISTS" in text  # one pending row per guild
    pool.calls.clear()
    await cog._write_timer(1, store.normalise(CONFIG), replace=False)  # digest off
    text = " ".join(pool.calls)
    assert "DELETE FROM timers" in text and "INSERT INTO timers" not in text


# -- panel -------------------------------------------------------------------------


def panel_for(plus):
    cog = make_cog(plus=plus)
    guild = types.SimpleNamespace(
        id=1, me=object(), get_channel=lambda cid: None, get_role=lambda rid: None
    )
    return panel.SuggestionsPanel(cog, guild, 5, store.normalise(CONFIG)), cog


async def test_panel_rechecks_manage_server_on_every_click():
    view, _cog = panel_for(False)
    ix = interaction(member(uid=5, manage=False))
    assert await view.interaction_check(ix) is False
    assert "Manage Server" in ix.response.send_message.await_args.args[0]
    assert await view.interaction_check(interaction(member(uid=5, manage=True))) is True


async def test_customize_is_an_upsell_for_free_guilds_and_opens_for_plus(monkeypatch):
    view, cog = panel_for(False)
    upsell = AsyncMock(return_value=None)
    monkeypatch.setattr(panel.premium_upsell, "for_guild_refusal", upsell)
    monkeypatch.setattr(panel.premium_upsell, "invoker_is_admin", lambda m: True)
    monkeypatch.setattr(panel.premium_upsell, "is_guild_already_top_tier", lambda b, g: False)
    ix = interaction(member(uid=5, manage=True))
    await view._customize(ix)
    assert upsell.await_args.kwargs["limit_key"] == "suggestions_custom"
    assert "Yasuho+" in ix.response.send_message.await_args.args[0]
    plus_view, _ = panel_for(True)
    ix2 = interaction(member(uid=5, manage=True))
    ix2.response.edit_message = AsyncMock()
    await plus_view._customize(ix2)
    ix2.response.edit_message.assert_awaited_once()


async def test_add_status_modal_refuses_free_guilds_and_caps_the_list(monkeypatch):
    free_view, _ = panel_for(False)
    free_view.refuse_custom = AsyncMock()
    modal = panel.AddStatusModal(free_view)
    modal.label._value = "Planned"
    await modal.on_submit(interaction(member()))
    free_view.refuse_custom.assert_awaited_once()
    plus_view, _ = panel_for(True)
    plus_view.save_and_rerender = AsyncMock()
    plus_view.config["custom_statuses"] = [
        {"label": f"s{i}", "colour": 1} for i in range(store.MAX_CUSTOM_STATUSES)
    ]
    modal = panel.AddStatusModal(plus_view)
    modal.label._value = "extra"
    ix = interaction(member())
    await modal.on_submit(ix)
    plus_view.save_and_rerender.assert_not_awaited()
    plus_view.config["custom_statuses"].pop()
    await modal.on_submit(interaction(member()))
    plus_view.save_and_rerender.assert_awaited_once()
    assert plus_view.config["custom_statuses"][-1]["label"] == "extra"


# -- schema, privacy, retention ----------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def _missing_schema_guards(ddl):
    required = (
        "CREATE TABLE IF NOT EXISTS suggestions",
        "UNIQUE (guild_id, number)",
        "CREATE TABLE IF NOT EXISTS suggestion_votes",
        "value         SMALLINT NOT NULL CHECK (value IN (-1, 1))",
        "PRIMARY KEY (suggestion_id, user_id)",
        "REFERENCES suggestions(id) ON DELETE CASCADE",
        "CREATE INDEX IF NOT EXISTS suggestions_author_idx",
        "CREATE INDEX IF NOT EXISTS suggestion_votes_user_idx",
    )
    return [guard for guard in required if guard not in ddl]


def test_schema_guards_have_a_negative_control():
    ddl = (ROOT / "schema.sql").read_text()
    assert not _missing_schema_guards(ddl)
    assert _missing_schema_guards(ddl.replace("ON DELETE CASCADE", "synthetic"))


def _missing_coverage(deletes, export_source, guild_queries):
    missing = []
    if deletes.get("suggestions") != "DELETE FROM suggestions WHERE author_id = $1":
        missing.append("erase-suggestions")
    if deletes.get("suggestion_votes") != "DELETE FROM suggestion_votes WHERE user_id = $1":
        missing.append("erase-votes")
    if "FROM suggestions WHERE author_id = $1" not in export_source:
        missing.append("export-suggestions")
    if "WHERE v.user_id = $1" not in export_source:
        missing.append("export-votes")
    if guild_queries.get("suggestions") != "DELETE FROM suggestions WHERE guild_id = $1":
        missing.append("retention")
    return missing


def test_export_erasure_and_retention_cover_the_suggestion_box():
    deletes = dict(privacy.USER_DELETE_QUERIES)
    source = inspect.getsource(privacy.collect_user_export)
    guilds = dict(retention.GUILD_DELETE_QUERIES)
    assert not _missing_coverage(deletes, source, guilds)
    assert "erase-suggestions" in _missing_coverage({}, source, guilds)
    assert "export-votes" in _missing_coverage(deletes, "synthetic", guilds)
    assert "retention" in _missing_coverage(deletes, source, {})
    assert "SELECT guild_id FROM suggestions" in retention.STORED_GUILD_IDS_QUERY
    # votes die with the suggestion, so they need no list of their own
    assert "suggestion_votes" not in guilds
    assert privacy.EXPORT_VERSION >= 16


async def test_erasure_deletes_the_message_but_keeps_the_thread_locked():
    gone = types.SimpleNamespace(delete=AsyncMock())
    thread = types.SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    failing = types.SimpleNamespace(edit=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "x")))
    channel = types.SimpleNamespace(get_partial_message=lambda mid: gone)
    bot = types.SimpleNamespace(
        get_channel=lambda cid: {60: thread, 61: failing}.get(cid, channel),
        fetch_channel=AsyncMock(),
    )
    refs = [{"number": 7, "channel_id": 9, "message_id": 50, "thread_id": 61},
            {"number": 8, "channel_id": 9, "message_id": 52, "thread_id": 60}]
    await store.delete_posted(bot, refs)  # a Forbidden thread must not stop the rest
    assert gone.delete.await_count == 2
    thread.delete.assert_not_awaited()  # other members' replies stay
    assert thread.edit.await_args_list[0].kwargs == {"name": "#8", "archived": False, "locked": True}
    assert thread.edit.await_args_list[1].kwargs == {"archived": True}


async def test_closing_an_already_archived_thread_unarchives_it_first(monkeypatch):
    thread = types.SimpleNamespace(edit=AsyncMock(), archived=True)
    guild = types.SimpleNamespace(id=1, get_thread=lambda tid: thread)
    cog = Suggestions(types.SimpleNamespace(db_pool=None))
    await cog._sync_thread(guild, {"thread_id": 60, "status": "rejected", "previous": "open"})
    assert [c.kwargs for c in thread.edit.await_args_list] == [
        {"archived": False, "locked": True}, {"archived": True, "locked": True},
    ]


def test_everyone_is_never_the_staff_role():
    guild = types.SimpleNamespace(id=1)
    member = types.SimpleNamespace(
        guild=guild, guild_permissions=discord.Permissions.none(),
        roles=[types.SimpleNamespace(id=1)],
    )
    assert not Suggestions.is_staff(member, {"staff_role_id": 1})


async def test_the_dashboard_reconcile_action_returns_the_next_digest():
    from cogs.system import dashboard_actions

    pool = FakePool()
    cog = make_cog(pool=pool)
    run = dashboard_actions._EXECUTORS["suggestions_reconcile"]
    cog.bot.get_guild = lambda gid: object() if gid == 1 else None
    cog.bot.get_cog = lambda name: cog if name == "Suggestions" else None
    cog.config_for = AsyncMock(return_value=store.normalise({**DIGEST_CONFIG}))
    result = await run(cog.bot, 1, {})
    assert result["ok"] is True and result["next_digest"]
    cog.config_for = AsyncMock(return_value=store.normalise(CONFIG))  # digest off
    assert await run(cog.bot, 1, {}) == {"ok": True, "next_digest": None}
    assert await run(cog.bot, 2, {}) == {"ok": False, "error": "guild_unavailable"}
