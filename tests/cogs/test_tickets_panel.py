"""Unit tests for the tickets cog and the open flow (lot T1).

Covers the two surfaces a guild actually touches:

* ``/ticket setup|status|disable`` - the preflight that refuses to write a
  configuration the bot cannot act on, the fact that a setup writes ONLY the
  options it was given (moving a panel must not wipe the support role) and warns
  when the support role cannot reach a private thread, the read-only Components
  V2 card, and the fact that disabling clears ONLY the panel channel;
* the persistent panel button and the modal submit behind it - the checks a
  click runs in order, the shape of the PRIVATE thread that gets created, the
  compensating delete when the cap guard refuses after the thread exists, and
  the fact that the subject a member typed never reaches the database.

Discord objects are subclassed rather than duck-typed where the code under test
legitimately asserts a type (``discord.Member``, ``discord.TextChannel``): the
subclass skips discord.py's ``__init__`` and sets only what the flow reads, so
``isinstance`` stays a real check instead of being weakened for the tests.
"""

import asyncio
import json
import pathlib
import re
import types

import discord
import pytest

from cogs.config.tickets import guild_config, panel, preflight
from cogs.config.tickets import open as ticket_open
from tools import i18n, settings

GUILD_ID = 31337
CHANNEL_ID = 555000111
ROLE_ID = 999000111
LOG_ID = 222000333
MEMBER_ID = 42


@pytest.fixture(autouse=True)
def _isolate_module_state():
    settings._cache.clear()
    ticket_open._IN_FLIGHT.clear()
    yield
    settings._cache.clear()
    ticket_open._IN_FLIGHT.clear()


def _seed(blob, guild_id=GUILD_ID):
    settings._cache[("guild_settings", guild_id)] = dict(blob)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Member(discord.Member):
    def __init__(self, user_id=MEMBER_ID, name="Kira"):
        self._user = types.SimpleNamespace(id=user_id, name=name)
        self._display = name

    def __str__(self):
        return self._display


class _Role:
    def __init__(self, role_id=ROLE_ID):
        self.id = role_id
        self.mention = f"<@&{role_id}>"


class _Thread(discord.Thread):
    def __init__(self, thread_id=7001, send_error=None, delete_error=None):
        self.id = thread_id
        self.edits = []
        self.sends = []
        self.deleted = False
        self._send_error = send_error
        self._delete_error = delete_error
        self.edit_error = None
        self.add_user_error = None
        self.added = []

    async def edit(self, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)
        return self

    async def send(self, *args, **kwargs):
        if self._send_error is not None:
            raise self._send_error
        self.sends.append((args, kwargs))

    async def add_user(self, user):
        if self.add_user_error is not None:
            raise self.add_user_error
        self.added.append(user)

    async def delete(self, **kwargs):
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted = True


class _Perms:
    def __init__(self, **granted):
        for name in preflight.SETUP_PERMISSIONS:
            setattr(self, name, granted.get(name, True))


class _TextChannel(discord.TextChannel):
    def __init__(self, channel_id=CHANNEL_ID, perms=None, thread=None):
        self.id = channel_id
        self._perms = perms if perms is not None else _Perms()
        # permissions_for is asked about the BOT and about the support role, and
        # the two answers differ in the real thing.
        self._perms_by_object = {}
        self.sends = []
        self.thread_calls = []
        self.thread = thread if thread is not None else _Thread()
        self.create_error = None
        self.send_error = None
        self.gate = None  # optional asyncio.Event to stall thread creation

    def permissions_for(self, obj):
        return self._perms_by_object.get(id(obj), self._perms)

    def set_permissions_for(self, obj, perms):
        self._perms_by_object[id(obj)] = perms

    async def create_thread(self, **kwargs):
        self.thread_calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        if self.create_error is not None:
            raise self.create_error
        return self.thread

    async def send(self, *args, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(jump_url="https://discord.test/panel")


class _Guild:
    def __init__(self, channels=(), roles=(), name="Server"):
        self.id = GUILD_ID
        self.name = name
        self.me = object()
        self._channels = {c.id: c for c in channels}
        self._roles = {r.id: r for r in roles}

    def get_channel(self, ident):
        return self._channels.get(ident)

    def get_role(self, ident):
        return self._roles.get(ident)


class _Response:
    def __init__(self, parent):
        self._parent = parent
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self._parent.sent.append((args, kwargs))
        self._done = True

    async def defer(self, *args, **kwargs):
        self._parent.defers.append(kwargs)
        self._done = True

    async def send_modal(self, modal):
        self._parent.modals.append(modal)
        self._done = True


class _Followup:
    def __init__(self, parent):
        self._parent = parent

    async def send(self, *args, **kwargs):
        self._parent.followups.append((args, kwargs))


class _Interaction:
    def __init__(self, guild, member, client):
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.user = member
        self.client = client
        self.sent = []
        self.defers = []
        self.modals = []
        self.followups = []
        self.response = _Response(self)
        self.followup = _Followup(self)

    @property
    def replies(self):
        """Every message the member actually saw, whichever route it took."""
        return [args[0] for args, _kw in self.sent + self.followups if args]


class _Bot:
    def __init__(self, pool, blacklist=()):
        self.db_pool = pool
        self.blacklist = set(blacklist)
        self.views = []

    def add_view(self, view):
        self.views.append(view)


class _Ctx:
    def __init__(self, guild, author_id=1):
        self.guild = guild
        self.author = types.SimpleNamespace(id=author_id)
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace()

    @property
    def texts(self):
        return [args[0] for args, _kw in self.sends if args]


class _Pool:
    """Records set_guild writes and answers the open-count / insert queries."""

    def __init__(self, open_count=0, insert_result=1, insert_error=None):
        self.calls = []
        self.open_count = open_count
        self.insert_result = insert_result
        self.insert_error = insert_error

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "INSERT 0 1"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "COUNT(*) FROM tickets" in query:
            return self.open_count
        return None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "INSERT INTO tickets" in query:
            if self.insert_error is not None:
                raise self.insert_error
            if self.insert_result is None:
                return None
            return {"ticket_number": self.insert_result}
        return None

    @property
    def settings_writes(self):
        """(key, value) for every tools.settings.set_guild that landed.

        ``_save_key`` patches ONE key with ``jsonb_set``, so the value arrives
        as JSON text and is decoded back here.
        """
        out = []
        for method, query, args in self.calls:
            if method == "execute" and "guild_settings" in query:
                out.append((args[1], json.loads(args[2])))
        return out

    @property
    def ticket_queries(self):
        """Every statement that touched the tickets table."""
        return [query for _method, query, _args in self.calls if "tickets" in query]


def _cog(pool, blacklist=()):
    return panel.Tickets(_Bot(pool, blacklist))


# ---------------------------------------------------------------------------
# The persistent button: identity and namespace
# ---------------------------------------------------------------------------


def test_the_panel_button_carries_a_static_custom_id_and_never_times_out():
    view = ticket_open.TicketPanelView()
    assert view.timeout is None
    assert len(view.children) == 1
    button = view.children[0]
    assert button.custom_id == ticket_open.OPEN_CUSTOM_ID == "ticket_open"
    # No guild/channel id may be baked in: one registered view serves every
    # guild, and the configuration is read at click time.
    assert not re.search(r"\d", button.custom_id)


async def test_the_cog_registers_exactly_one_global_persistent_view():
    cog = _cog(_Pool())
    await cog.cog_load()
    assert len(cog.bot.views) == 1
    assert isinstance(cog.bot.views[0], ticket_open.TicketPanelView)


def test_the_dynamic_item_namespace_for_lot_t2_is_reserved_and_unclaimed():
    """``tk:`` belongs to the in-thread controls; nothing else may take it."""
    assert ticket_open.DYNAMIC_NAMESPACE == "tk:"
    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    templates = []
    for path in (repo / "cogs").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        templates += re.findall(r"^[A-Z_]*TEMPLATE\s*=\s*r?[\"'](.+?)[\"']", source, re.M)
    assert templates, "the template scan found nothing - the guard would be vacuous"
    assert [t for t in templates if t.startswith("tk:")] == []


# ---------------------------------------------------------------------------
# /ticket setup
# ---------------------------------------------------------------------------


async def test_setup_writes_nothing_when_the_bot_lacks_a_permission():
    # A configuration the bot cannot act on is worse than none: the panel looks
    # fine and only fails in front of the member who needed help.
    channel = _TextChannel(perms=_Perms(create_private_threads=False))
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(channels=[channel]))

    await cog.ticket_setup.callback(cog, ctx, channel)

    assert pool.settings_writes == []
    assert channel.sends == []
    assert "Create Private Threads" in ctx.texts[0]


async def test_setup_stores_the_four_keys_and_posts_the_panel():
    channel = _TextChannel()
    role = _Role()
    log_channel = _TextChannel(channel_id=LOG_ID)
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(channels=[channel, log_channel], roles=[role]))

    await cog.ticket_setup.callback(
        cog, ctx, channel, role, log_channel, message="  Ask away  "
    )

    assert dict(pool.settings_writes) == {
        guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
        guild_config.KEY_SUPPORT_ROLE: ROLE_ID,
        guild_config.KEY_LOG_CHANNEL: LOG_ID,
        guild_config.KEY_PANEL_MESSAGE: "Ask away",  # trimmed
    }
    assert len(channel.sends) == 1
    _args, kwargs = channel.sends[0]
    assert isinstance(kwargs["view"], ticket_open.TicketPanelView)
    assert kwargs["embed"].description == "Ask away"


async def test_setup_writes_only_the_options_that_were_named():
    # `/ticket setup #somewhere-else` is how a panel is MOVED. Writing the
    # omitted optionals as null would wipe the support role and the log channel
    # of a manager who only wanted to move the panel - the same choices
    # `/ticket disable` deliberately preserves.
    _seed(
        {
            guild_config.KEY_SUPPORT_ROLE: ROLE_ID,
            guild_config.KEY_LOG_CHANNEL: LOG_ID,
            guild_config.KEY_PANEL_MESSAGE: "Ask away",
        }
    )
    channel = _TextChannel()
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(channels=[channel]))

    await cog.ticket_setup.callback(cog, ctx, channel)

    assert pool.settings_writes == [(guild_config.KEY_PANEL_CHANNEL, CHANNEL_ID)]
    # And the panel it posts still carries the blurb that was kept.
    _args, kwargs = channel.sends[0]
    assert kwargs["embed"].description == "Ask away"


async def test_setup_says_which_options_it_kept():
    # A command that preserves what you did not name has to SAY so, or it looks
    # like it forgot them.
    _seed({guild_config.KEY_SUPPORT_ROLE: ROLE_ID, guild_config.KEY_LOG_CHANNEL: LOG_ID})
    channel = _TextChannel()
    log_channel = _TextChannel(channel_id=LOG_ID)
    role = _Role()
    cog = _cog(_Pool())
    ctx = _Ctx(_Guild(channels=[channel, log_channel], roles=[role]))

    await cog.ticket_setup.callback(cog, ctx, channel)

    assert role.mention in ctx.texts[0]
    assert log_channel.mention in ctx.texts[0]
    # ... and it must not ping the role it is only naming.
    _args, kwargs = ctx.sends[-1]
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


async def test_a_blank_message_is_how_the_blurb_is_cleared():
    _seed({guild_config.KEY_PANEL_MESSAGE: "Ask away"})
    channel = _TextChannel()
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(channels=[channel]))

    await cog.ticket_setup.callback(cog, ctx, channel, message="   ")

    assert (guild_config.KEY_PANEL_MESSAGE, None) in pool.settings_writes
    _args, kwargs = channel.sends[0]
    assert "Need a hand" in kwargs["embed"].description


async def test_setup_warns_when_the_support_role_cannot_see_ticket_threads():
    # A ROLE mention adds nobody to a private thread: staff reach it through
    # manage_threads on the parent. Without that, the role is pinged into rooms
    # it cannot open - warned about, never refused.
    channel = _TextChannel()
    role = _Role()
    channel.set_permissions_for(role, _Perms(manage_threads=False))
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(channels=[channel], roles=[role]))

    await cog.ticket_setup.callback(cog, ctx, channel, role)

    assert dict(pool.settings_writes)[guild_config.KEY_PANEL_CHANNEL] == CHANNEL_ID
    assert channel.sends  # the panel still went up: this is a warning
    assert "Manage Threads" in ctx.texts[0]


async def test_a_support_role_that_can_reach_the_threads_gets_no_warning():
    channel = _TextChannel()
    role = _Role()
    cog = _cog(_Pool())
    ctx = _Ctx(_Guild(channels=[channel], roles=[role]))

    await cog.ticket_setup.callback(cog, ctx, channel, role)

    assert "Manage Threads" not in ctx.texts[0]


async def test_setup_writes_the_on_switch_last():
    # The four writes are not one transaction. Writing the panel channel LAST
    # means a failure part-way through leaves tickets OFF with a half-written
    # configuration, never ON with one.
    channel = _TextChannel()
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(channels=[channel]))

    await cog.ticket_setup.callback(cog, ctx, channel)

    keys = [key for key, _value in pool.settings_writes]
    assert keys[-1] == guild_config.KEY_PANEL_CHANNEL


async def test_the_panel_is_rendered_in_the_guilds_language_not_the_managers(
    monkeypatch,
):
    # The panel is public and outlives the command: a manager who reads Japanese
    # must not leave a Japanese button on a French server.
    recorded = {}

    class _SpyView(ticket_open.TicketPanelView):
        def __init__(self):
            recorded["view"] = i18n.current_locale.get()
            super().__init__()

    async def _guild_locale(_bot, _guild):
        return "ja"

    monkeypatch.setattr(panel, "TicketPanelView", _SpyView)
    monkeypatch.setattr(panel.i18n, "resolve_guild_locale", _guild_locale)

    channel = _TextChannel()
    cog = _cog(_Pool())
    ctx = _Ctx(_Guild(channels=[channel]))
    with i18n.locale("fr"):  # the manager's own language
        await cog.ticket_setup.callback(cog, ctx, channel)

    assert recorded["view"] == "ja"
    # ... and the locale is handed back afterwards, so the reply to the manager
    # is still in theirs.
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE


async def test_setup_reports_a_panel_that_could_not_be_posted():
    channel = _TextChannel()
    channel.send_error = discord.HTTPException(
        types.SimpleNamespace(status=403, reason=""), "nope"
    )
    cog = _cog(_Pool())
    ctx = _Ctx(_Guild(channels=[channel]))

    await cog.ticket_setup.callback(cog, ctx, channel)

    assert "could not post" in ctx.texts[0]


# ---------------------------------------------------------------------------
# /ticket disable
# ---------------------------------------------------------------------------


async def test_disable_clears_only_the_panel_channel():
    # The role, log channel and blurb are the server's choices; turning tickets
    # off for an afternoon must not wipe them.
    pool = _Pool()
    cog = _cog(pool)
    ctx = _Ctx(_Guild())

    await cog.ticket_disable.callback(cog, ctx)

    assert pool.settings_writes == [(guild_config.KEY_PANEL_CHANNEL, None)]


# ---------------------------------------------------------------------------
# /ticket status
# ---------------------------------------------------------------------------


def _card_text(view):
    return "\n".join(
        child.content
        for child in view.children[0].children
        if hasattr(child, "content")
    )


def _config(**overrides):
    base = {
        "panel_channel": None,
        "support_role": None,
        "log_channel": None,
        "max_open": 2,
        "inactivity_hours": 72,
    }
    base.update(overrides)
    return base


def test_status_card_unconfigured_says_disabled_and_points_at_setup():
    view = panel.TicketStatusView(_Guild(), _config())
    text = _card_text(view)
    assert "Disabled" in text
    assert "Not set" in text
    assert "/ticket setup" in text


def test_status_card_configured_shows_every_mention_and_the_numbers():
    channel = _TextChannel()
    role = _Role()
    log_channel = _TextChannel(channel_id=LOG_ID)
    guild = _Guild(channels=[channel, log_channel], roles=[role])

    view = panel.TicketStatusView(
        guild,
        _config(
            panel_channel=CHANNEL_ID,
            support_role=ROLE_ID,
            log_channel=LOG_ID,
            max_open=4,
            inactivity_hours=24,
        ),
    )

    text = _card_text(view)
    assert "Enabled" in text
    assert channel.mention in text
    assert role.mention in text
    assert log_channel.mention in text
    assert "4" in text and "24h" in text
    assert "/ticket setup" not in text


def test_status_card_tells_a_deleted_channel_apart_from_an_unset_one():
    view = panel.TicketStatusView(_Guild(), _config(panel_channel=CHANNEL_ID))
    text = _card_text(view)
    assert str(CHANNEL_ID) in text
    assert "(deleted)" in text
    assert "Disabled" in text  # a deleted channel is not a working setup


def test_status_card_resolves_ids_stored_as_strings():
    channel = _TextChannel()
    view = panel.TicketStatusView(
        _Guild(channels=[channel]), _config(panel_channel=str(CHANNEL_ID))
    )
    assert channel.mention in _card_text(view)


async def test_status_command_sends_the_card_without_mentioning_anybody():
    _seed({})
    cog = _cog(_Pool())
    ctx = _Ctx(_Guild())

    await cog.ticket_status.callback(cog, ctx)

    _args, kwargs = ctx.sends[0]
    assert isinstance(kwargs["view"], panel.TicketStatusView)
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


# ---------------------------------------------------------------------------
# The click: the checks, in order
# ---------------------------------------------------------------------------


def _click(pool, *, blacklist=(), channels=(), roles=()):
    guild = _Guild(channels=channels, roles=roles)
    interaction = _Interaction(guild, _Member(), _Bot(pool, blacklist))
    return interaction, ticket_open.TicketOpenButton()


async def test_a_click_in_a_guild_with_no_configuration_says_so():
    _seed({})
    pool = _Pool()
    interaction, button = _click(pool)

    await button.callback(interaction)

    assert "not set up" in interaction.replies[0]
    assert interaction.modals == []


async def test_a_blacklisted_member_is_refused_before_any_ticket_work():
    # The blacklist is an in-memory set read synchronously, and it is consulted
    # before the configuration and the cap - a banned user never reaches either.
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    interaction, button = _click(pool, blacklist=[MEMBER_ID])

    await button.callback(interaction)

    assert interaction.modals == []
    assert pool.ticket_queries == []


async def test_a_member_at_the_cap_never_gets_the_modal():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool(open_count=2)
    interaction, button = _click(pool)

    await button.callback(interaction)

    assert interaction.modals == []
    assert "already have" in interaction.replies[0]


async def test_the_cap_pre_check_honours_a_guild_that_raised_the_limit():
    _seed(
        {
            guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
            guild_config.KEY_MAX_OPEN_PER_USER: 4,
        }
    )
    pool = _Pool(open_count=2)
    interaction, button = _click(pool)

    await button.callback(interaction)

    assert len(interaction.modals) == 1


async def test_a_member_under_the_cap_gets_the_subject_modal():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool(open_count=1)
    interaction, button = _click(pool)

    await button.callback(interaction)

    assert isinstance(interaction.modals[0], ticket_open.TicketSubjectModal)


async def test_a_failing_count_check_apologises_instead_of_raising():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})

    class _Broken(_Pool):
        async def fetchval(self, query, *args):
            raise RuntimeError("boom")

    interaction, button = _click(_Broken())

    await button.callback(interaction)

    assert interaction.modals == []
    assert "went wrong" in interaction.replies[0]


async def test_a_click_outside_a_guild_is_refused():
    interaction = _Interaction(None, _Member(), _Bot(_Pool()))
    await ticket_open.TicketOpenButton().callback(interaction)
    assert "inside a server" in interaction.replies[0]


# ---------------------------------------------------------------------------
# The submit: the thread, the row, and the compensation
# ---------------------------------------------------------------------------


def _submit_context(pool, *, perms=None, roles=(), thread=None, blacklist=()):
    channel = _TextChannel(perms=perms, thread=thread)
    guild = _Guild(channels=[channel], roles=roles)
    interaction = _Interaction(guild, _Member(), _Bot(pool, blacklist))
    return interaction, channel


async def test_a_member_blacklisted_while_the_modal_was_open_gets_nothing():
    # The click check is not enough: the modal may have been open for minutes,
    # and a blacklisting lands in the in-memory set the instant it happens.
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    interaction, channel = _submit_context(pool, blacklist=[MEMBER_ID])

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread_calls == []
    assert pool.ticket_queries == []
    assert "cannot use this" in interaction.replies[0]


async def test_the_ticket_room_is_a_private_uninvitable_thread():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "printer on fire")

    kwargs = channel.thread_calls[0]
    # message=None is what makes discord.py create a PRIVATE thread; invitable
    # defaults to True there, so it has to be passed explicitly.
    assert kwargs["message"] is None
    assert kwargs["invitable"] is False
    assert kwargs["auto_archive_duration"] == guild_config.AUTO_ARCHIVE_MINUTES


async def test_the_subject_reaches_the_thread_and_never_the_database():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    interaction, channel = _submit_context(pool)

    await ticket_open._create_ticket(interaction, "my password leaked")

    _args, kwargs = channel.thread.sends[0]
    assert kwargs["embed"].description == "my password leaked"
    for _method, _query, args in pool.calls:
        assert "my password leaked" not in [a for a in args if isinstance(a, str)]


async def test_the_thread_is_renamed_to_its_authoritative_number():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(_Pool(insert_result=17))

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread_calls[0]["name"] == "ticket"
    assert channel.thread.edits == [{"name": "ticket-17"}]
    assert "#17" in interaction.replies[0]


async def test_a_failed_rename_is_cosmetic_and_the_ticket_still_opens():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    thread = _Thread()
    thread.edit_error = discord.HTTPException(
        types.SimpleNamespace(status=429, reason=""), "slow down"
    )
    interaction, channel = _submit_context(_Pool(insert_result=3), thread=thread)

    await ticket_open._create_ticket(interaction, "hi")

    assert thread.sends  # the greeting still went out
    assert not thread.deleted
    assert "#3" in interaction.replies[0]


async def test_the_opening_message_pulls_the_opener_in_and_calls_staff():
    _seed(
        {
            guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
            guild_config.KEY_SUPPORT_ROLE: ROLE_ID,
        }
    )
    role = _Role()
    interaction, channel = _submit_context(_Pool(), roles=[role])

    await ticket_open._create_ticket(interaction, "help")

    args, kwargs = channel.thread.sends[0]
    # The MENTION in the content is what adds the opener to a private thread.
    assert f"<@{MEMBER_ID}>" in args[0]
    assert role.mention in args[0]
    allowed = kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.roles == [role]


async def test_a_subject_cannot_smuggle_an_everyone_ping():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "@everyone LOOK")

    _args, kwargs = channel.thread.sends[0]
    assert kwargs["allowed_mentions"].everyone is False
    # No support role configured: nothing but the opener may be mentioned.
    assert kwargs["allowed_mentions"].roles is False


async def test_losing_the_cap_race_deletes_the_thread_that_never_became_a_ticket():
    # The guarded INSERT declined, so there is no row - and a thread with no row
    # is not a ticket.
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(_Pool(insert_result=None))

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread.deleted is True
    assert channel.thread.sends == []
    assert "already have" in interaction.replies[0]


async def test_a_database_failure_also_deletes_the_thread():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(
        _Pool(insert_error=RuntimeError("database is gone"))
    )

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread.deleted is True
    assert "went wrong" in interaction.replies[0]


async def test_a_greeting_that_fails_still_gets_the_opener_into_their_ticket():
    # The mention that failed to send is ALSO what adds the opener to a private
    # thread, so without the fallback add they would be locked out of the ticket
    # they just opened (a link to a private thread you are not in opens nothing).
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    thread = _Thread(
        send_error=discord.HTTPException(
            types.SimpleNamespace(status=403, reason=""), "nope"
        )
    )
    interaction, channel = _submit_context(_Pool(insert_result=8), thread=thread)

    await ticket_open._create_ticket(interaction, "hi")

    assert thread.added == [interaction.user]
    assert thread.deleted is False
    assert "#8" in interaction.replies[0]


async def test_the_happy_path_never_pays_for_an_explicit_add():
    # The mention is what pulls the opener in; add_user is the fallback only.
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread.added == []


async def test_a_fallback_add_that_also_fails_still_leaves_a_real_ticket():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    error = discord.HTTPException(types.SimpleNamespace(status=403, reason=""), "nope")
    thread = _Thread(send_error=error)
    thread.add_user_error = error
    interaction, channel = _submit_context(_Pool(insert_result=8), thread=thread)

    await ticket_open._create_ticket(interaction, "hi")

    assert thread.deleted is False
    assert "#8" in interaction.replies[0]


async def test_a_thread_that_cannot_be_created_is_reported_without_a_row():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    interaction, channel = _submit_context(pool)
    channel.create_error = discord.HTTPException(
        types.SimpleNamespace(status=403, reason=""), "nope"
    )

    await ticket_open._create_ticket(interaction, "hi")

    assert not any("INSERT INTO tickets" in call[1] for call in pool.calls)
    assert "could not create" in interaction.replies[0]


async def test_tickets_disabled_while_the_modal_was_open_creates_nothing():
    # The member may have taken a minute to type; configuration is re-read here.
    _seed({})
    interaction, channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread_calls == []
    assert "not set up" in interaction.replies[0]


async def test_a_permission_lost_since_setup_is_named_and_stops_the_open():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(
        _Pool(), perms=_Perms(manage_threads=False)
    )

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread_calls == []
    assert "Manage Threads" in interaction.replies[0]


async def test_the_submit_defers_before_touching_discord():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, _channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "hi")

    assert interaction.defers[0]["ephemeral"] is True
    assert interaction.defers[0]["thinking"] is True


# ---------------------------------------------------------------------------
# Double-submit safety
# ---------------------------------------------------------------------------


async def test_two_simultaneous_submits_create_one_thread_not_two():
    # The database guard bounds the CAP, but a member at 0 of 2 passes it twice.
    # The in-flight set is what stops one accident becoming two tickets.
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    interaction_a, channel = _submit_context(pool)
    interaction_b = _Interaction(interaction_a.guild, _Member(), interaction_a.client)
    channel.gate = asyncio.Event()

    first = asyncio.create_task(ticket_open._create_ticket(interaction_a, "hi"))
    await asyncio.sleep(0)  # let the first reach create_thread and stall
    await ticket_open._create_ticket(interaction_b, "hi again")
    channel.gate.set()
    await first

    assert len(channel.thread_calls) == 1
    assert "one moment" in interaction_b.replies[0]


async def test_the_in_flight_slot_is_released_even_when_the_flow_explodes():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _submit_context(_Pool())
    channel.create_error = RuntimeError("unexpected")

    with pytest.raises(RuntimeError):
        await ticket_open._create_ticket(interaction, "hi")

    assert ticket_open._IN_FLIGHT == set()


async def test_a_click_while_an_open_is_in_flight_is_refused():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    ticket_open._IN_FLIGHT.add((GUILD_ID, MEMBER_ID))
    interaction, button = _click(_Pool())

    await button.callback(interaction)

    assert interaction.modals == []
    assert "one moment" in interaction.replies[0]


# ---------------------------------------------------------------------------
# The modal
# ---------------------------------------------------------------------------


def test_the_subject_input_is_bounded_and_required():
    modal = ticket_open.TicketSubjectModal()
    field = modal.subject
    assert field.required is True
    assert field.max_length == guild_config.MAX_SUBJECT_LENGTH
    assert field.style is discord.TextStyle.short
