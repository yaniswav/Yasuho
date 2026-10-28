"""The /announce builder schedules through the scheduled-announcement engine,
and /announcements manages what is scheduled."""

import datetime
import types

import discord

from cogs.community.announcements_store import render
from cogs.config import announcements
from cogs.config.announcements import AnnouncePanel, ScheduledPanel


class _Store:
    def __init__(self, rows=()):
        self.created = []
        self.rows = [dict(r) for r in rows]
        self.enabled_calls = []
        self.deleted = []

    async def create(self, guild_id, channel_id, author_id, **kwargs):
        self.created.append((guild_id, channel_id, author_id, kwargs))

    async def list_for_guild(self, guild_id):
        return list(self.rows)

    async def set_enabled(self, guild_id, announcement_id, enabled):
        self.enabled_calls.append((announcement_id, enabled))
        for row in self.rows:
            if row["id"] == announcement_id:
                row["enabled"] = enabled

    async def delete(self, guild_id, announcement_id):
        self.deleted.append(announcement_id)
        self.rows = [r for r in self.rows if r["id"] != announcement_id]


class _Response:
    def __init__(self):
        self.sent = []
        self.edits = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self._done = True
        self.sent.append((args, kwargs))

    async def edit_message(self, **kwargs):
        self._done = True
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self._done = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _setup(store):
    perms = discord.Permissions(send_messages=True, embed_links=True)
    channel = types.SimpleNamespace(
        id=9, mention="<#9>", permissions_for=lambda _m: perms
    )
    guild = types.SimpleNamespace(
        id=3, name="G", member_count=5, me=object(),
        get_channel=lambda cid: channel if cid == 9 else None,
        get_role=lambda rid: None,
    )
    engine = types.SimpleNamespace(store=store)
    bot = types.SimpleNamespace(get_cog=lambda name: engine)
    cog = types.SimpleNamespace(bot=bot)
    return cog, guild, channel


async def test_schedule_hands_the_draft_to_the_engine_in_its_time_zone():
    store = _Store()
    cog, guild, _channel = _setup(store)
    draft = {"embed": {"title": "Movie night"}, "channel_id": None, "role_id": None}
    panel = AnnouncePanel(cog, guild, 1, draft)
    draft["channel_id"] = 9  # picked in the channel select
    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=42), response=_Response(), followup=_Followup()
    )
    zone = datetime.timezone(datetime.timedelta(hours=1))
    when = datetime.datetime(2030, 1, 6, 21, 0, tzinfo=zone)

    await panel.schedule(interaction, when, "weekly", "Europe/Paris")

    ((guild_id, channel_id, author_id, kwargs),) = store.created
    assert (guild_id, channel_id, author_id) == (3, 9, 42)
    assert kwargs["local_time"] == datetime.datetime(2030, 1, 6, 21, 0)
    assert kwargs["timezone"] == "Europe/Paris"
    assert kwargs["recurrence"] == "weekly"
    assert kwargs["embed"] == {"title": "Movie night"}
    # Acknowledged before the database write, answered by a followup.
    assert interaction.response.is_done()
    assert interaction.followup.sent[0][1]["ephemeral"] is True


def test_the_engine_renders_builder_placeholders_at_fire_time():
    row = {"embed": {"description": "{members} members in {server}"},
           "role_id": None, "content": None}
    guild = types.SimpleNamespace(name="Cafe", member_count=1234)
    _content, embed, _mentions = render(row, guild, None)
    assert embed.description == "1,234 members in Cafe"


def _row(rid, *, enabled=True):
    return {
        "id": rid, "channel_id": 9, "recurrence": "daily", "enabled": enabled,
        "next_run_at": discord.utils.utcnow(), "failures": 0,
        "embed": {"title": "Hello"},
    }


async def test_manager_lists_pauses_and_deletes():
    store = _Store([_row(1), _row(2)])
    cog, guild, _channel = _setup(store)
    panel = ScheduledPanel(cog, guild, 1, store.rows, selected=2)
    assert "`#1`" in panel.build_embed().description

    message_edits = []

    async def _edit(**kwargs):
        message_edits.append(kwargs)

    panel.message = types.SimpleNamespace(edit=_edit)
    interaction = types.SimpleNamespace(response=_Response())
    await panel._toggle(interaction)
    assert store.enabled_calls == [(2, False)]

    panel2 = ScheduledPanel(cog, guild, 1, store.rows, selected=2)
    panel2.message = panel.message
    await panel2._delete(types.SimpleNamespace(response=_Response()))
    assert store.deleted == [2]


def test_an_empty_manager_points_at_the_builder():
    cog, guild, _channel = _setup(_Store())
    panel = ScheduledPanel(cog, guild, 1, [])
    assert "/announce" in panel.build_embed().description
    assert panel.children == []


def test_repeat_choices_match_the_engine():
    from cogs.community.announcements_schedule import RECURRENCES

    assert {value for value, _label in announcements.REPEAT_CHOICES} == RECURRENCES


def test_fifty_schedules_are_all_reachable_through_two_selects():
    rows = [_row(i) for i in range(1, 51)]
    cog, guild, _channel = _setup(_Store(rows))
    panel = ScheduledPanel(cog, guild, 1, rows)
    selects = [c for c in panel.children if isinstance(c, discord.ui.Select)]
    values = [o.value for s in selects for o in s.options]
    assert len(selects) == 2 and len(values) == 50
    assert len(panel.build_embed().description) <= 4096


async def test_the_manager_rechecks_manage_server_on_every_click():
    cog, guild, _channel = _setup(_Store([_row(1)]))
    panel = ScheduledPanel(cog, guild, 1, [_row(1)])
    demoted = types.SimpleNamespace(
        user=types.SimpleNamespace(id=1, guild_permissions=discord.Permissions.none()),
        response=_Response(), locale=None, guild_locale=None, guild=guild,
    )
    assert await panel.interaction_check(demoted) is False
    assert demoted.response.sent[0][1]["ephemeral"] is True


def test_a_malformed_stored_embed_is_a_value_error_not_a_crash():
    import pytest

    from cogs.community.announcements_store import render_embed

    for bad in ({"title": "x", "author": "not an object"},
                {"description": "x" * 4096, "footer": {"text": "y" * 2048}}):
        with pytest.raises(ValueError):
            render_embed(bad)


async def test_schedule_over_the_limit_is_refused_ephemerally_with_the_limit():
    from cogs.community.announcements_store import AnnouncementLimitReached

    store = _Store()

    async def _full(*args, **kwargs):
        raise AnnouncementLimitReached("Announcement limit reached")

    store.create = _full
    cog, guild, _channel = _setup(store)
    draft = {"embed": {"title": "x"}, "channel_id": None, "role_id": None}
    panel = AnnouncePanel(cog, guild, 1, draft)
    draft["channel_id"] = 9
    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=42), response=_Response(), followup=_Followup()
    )
    when = datetime.datetime(2030, 1, 6, 21, 0, tzinfo=datetime.timezone.utc)

    await panel.schedule(interaction, when, "none", "UTC")

    ((args, kwargs),) = interaction.followup.sent
    assert "limit of 3" in args[0]
    assert kwargs["ephemeral"] is True


async def test_manager_marks_rows_past_the_limit_archived_and_refuses_resume():
    from cogs.community.announcements_store import AnnouncementArchived

    rows = [_row(i) for i in range(1, 6)]
    store = _Store(rows)
    cog, guild, _channel = _setup(store)
    panel = ScheduledPanel(cog, guild, 1, store.rows, selected=5)
    lines = panel.build_embed().description.splitlines()
    assert "archived" not in lines[2] and "archived" in lines[3] and "archived" in lines[4]

    async def _refuse(*args):
        raise AnnouncementArchived("Announcement archived over the limit")

    store.set_enabled = _refuse
    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=42), response=_Response()
    )
    await panel._toggle(interaction)
    ((args, kwargs),) = interaction.response.sent
    assert "over its limit of 3" in args[0] and kwargs["ephemeral"] is True
