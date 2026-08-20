"""Unit tests for the tickets cog, the open flow (lot T1) and the config panel
(lot T3).

Covers the surfaces a guild actually touches:

* ``/ticket setup|status|disable`` - the preflight that refuses to write a
  configuration the bot cannot act on, the fact that a setup writes ONLY the
  options it was given (moving a panel must not wipe the support role) and warns
  when the support role cannot reach a private thread, the read-only Components
  V2 card, and the fact that disabling clears ONLY the panel channel;
* the persistent panel button and the modal submit behind it - the checks a
  click runs in order, the shape of the PRIVATE thread that gets created, the
  compensating delete when the cap guard refuses after the thread exists, and
  the fact that the subject a member typed never reaches the database;
* ``/ticket config`` - the editable twin of the status card, and the two rules
  it exists to enforce: a key nobody wrote means the bot default (so the panel
  marks inherited values as such), and a reset DELETES the key rather than
  storing a neutral value.

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
    # The open-rate cooldown is process-wide and keyed by (guild, member), and
    # every test here opens as the SAME member in the SAME guild - so without
    # this, test number two would be refused by test number one's ticket. The
    # cooldown itself is exercised in test_tickets_open_cooldown.py.
    settings._cache.clear()
    ticket_open._IN_FLIGHT.clear()
    ticket_open._OPEN_COOLDOWNS._seen.clear()
    yield
    settings._cache.clear()
    ticket_open._IN_FLIGHT.clear()
    ticket_open._OPEN_COOLDOWNS._seen.clear()


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
        # The @everyone role, which is how "can the whole server read the log
        # channel?" is asked (panel._is_public).
        self.default_role = types.SimpleNamespace(id=GUILD_ID)
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

    async def edit_message(self, **kwargs):
        self._parent.edits.append(kwargs)
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
        self.edits = []
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
        # The stored guild_settings row, kept HONEST: the two statements this
        # package issues against it (a per-key jsonb_set and a per-key delete)
        # are applied here, so a surface that re-reads after a write sees what it
        # actually wrote instead of a hand-maintained fixture.
        self.blob = {}
        self.execute_error = None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.execute_error is not None:
            raise self.execute_error
        if "jsonb_set" in query and "guild_settings" in query:
            self.blob[args[1]] = json.loads(args[2])
        elif "settings - $2" in query:
            self.blob.pop(args[1], None)
        return "INSERT 0 1"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "COUNT(*) FROM tickets" in query:
            return self.open_count
        if "FROM guild_settings" in query:
            # asyncpg hands JSONB back as TEXT on this pool (no codec).
            return json.dumps(self.blob)
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
        """(key, value) for every VALUE write that landed on guild_settings.

        ``_save_key`` patches ONE key with ``jsonb_set``, so the value arrives
        as JSON text and is decoded back here. Key DELETES are a different
        statement and are reported by :attr:`settings_deletes`, so a test that
        asserts on this list is asserting about values only.
        """
        out = []
        for method, query, args in self.calls:
            if method == "execute" and "jsonb_set" in query:
                out.append((args[1], json.loads(args[2])))
        return out

    @property
    def settings_deletes(self):
        """Every key REMOVED from the guild blob (``settings - $2``).

        The reset rule of this feature: a key that is reset is deleted, never
        set to null, so "was it reset" is a question about this list.
        """
        return [
            args[1]
            for method, query, args in self.calls
            if method == "execute" and "settings - $2" in query
        ]

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


def test_the_dynamic_item_namespace_belongs_to_the_lifecycle_module_alone():
    """``tk:`` belongs to the in-thread controls; nothing else may take it.

    Lot T1 asserted this namespace was UNCLAIMED, which is what a reservation
    means while the module that will use it does not exist yet. Lot T2 built
    that module, so the guard flips to its lasting form: every ``tk:`` template
    in the tree lives in cogs/config/tickets/lifecycle.py and nowhere else, so a
    later feature still cannot take a prefix whose clicks would be routed here.
    """
    assert ticket_open.DYNAMIC_NAMESPACE == "tk:"
    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    owners = set()
    templates = []
    for path in (repo / "cogs").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        found = re.findall(r"^[A-Z_]*TEMPLATE\s*=\s*r?[\"'](.+?)[\"']", source, re.M)
        templates += found
        if any(template.startswith("tk:") for template in found):
            owners.add(path.name)
    assert templates, "the template scan found nothing - the guard would be vacuous"
    assert owners == {"lifecycle.py"}


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

    # DELETED, not stored as null: a cleared blurb leaves the guild exactly as
    # unconfigured as one that never set a blurb (the reset rule).
    assert guild_config.KEY_PANEL_MESSAGE in pool.settings_deletes
    assert guild_config.KEY_PANEL_MESSAGE not in [
        key for key, _value in pool.settings_writes
    ]
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

    # The switch is REMOVED, not nulled, and it is the only key touched.
    assert pool.settings_deletes == [guild_config.KEY_PANEL_CHANNEL]
    assert pool.settings_writes == []


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


async def test_the_guild_window_is_what_the_thread_is_created_with():
    # THE thing that makes tickets_inactivity_hours a real control: the window
    # is the thread's auto-archive duration, so Discord enforces it and the
    # archive it fires is what closes the ticket.
    _seed(
        {
            guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
            guild_config.KEY_INACTIVITY_HOURS: 24,
        }
    )
    interaction, channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "printer on fire")

    assert channel.thread_calls[0]["auto_archive_duration"] == 1440


async def test_a_window_the_dashboard_wrote_off_grid_still_reaches_discord():
    # 100 hours is not a duration Discord accepts; the reader rounds it up to
    # 168, which is also what every surface shows.
    _seed(
        {
            guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
            guild_config.KEY_INACTIVITY_HOURS: 100,
        }
    )
    interaction, channel = _submit_context(_Pool())

    await ticket_open._create_ticket(interaction, "hi")

    assert channel.thread_calls[0]["auto_archive_duration"] == 10080


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


class _FlakyThread(_Thread):
    """Fails its FIRST send - the opening message - and works from then on."""

    def __init__(self, error, **kwargs):
        super().__init__(**kwargs)
        self._first_error = error

    async def send(self, *args, **kwargs):
        if self._first_error is not None:
            error, self._first_error = self._first_error, None
            raise error
        return await super().send(*args, **kwargs)


async def test_a_failed_opening_message_still_leaves_the_controls_in_the_room():
    # add_user restores ACCESS but not the buttons, and without them nobody in
    # the room can close the ticket: the opener's cap slot would be held until
    # the sweep. So the controls are re-sent on their own.
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    error = discord.HTTPException(types.SimpleNamespace(status=403, reason=""), "nope")
    thread = _FlakyThread(error)
    interaction, channel = _submit_context(_Pool(insert_result=8), thread=thread)

    await ticket_open._create_ticket(interaction, "hi")

    assert thread.added == [interaction.user]
    args, kwargs = thread.sends[0]
    assert "#8" in args[0]
    view = kwargs["view"]
    assert {child.item.custom_id for child in view.children} == {
        "tk:claim:{0}".format(thread.id),
        "tk:close:{0}".format(thread.id),
    }
    # A retry of the opening message would re-ping; this one pings nobody.
    assert kwargs["allowed_mentions"].everyone is False


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


# ---------------------------------------------------------------------------
# /ticket config: the editable panel (lot T3)
#
# Two rules carry every test below, and they are the same two the dashboard
# contract states: a key nobody wrote means "the bot default" (so the panel has
# to be able to TELL the two apart), and resetting a key DELETES it (so a guild
# that resets everything is indistinguishable from one that never configured
# anything). The panel is built from the RAW key map for exactly that reason.
# ---------------------------------------------------------------------------


def _picker_role(role_id=ROLE_ID):
    """A role a RoleSelect will accept as a default value.

    ``_Role`` above is a plain stand-in and assigns ``mention``, which is a
    read-only property on the real class. discord.py resolves a default value's
    type by EXACT class for roles (unlike channels, which it normalises to
    ``GuildChannel``), so a subclass is refused too and the picker tests need a
    real :class:`discord.Role` - built without ``__init__`` and given only the
    ``id`` everything else here derives from.
    """
    role = discord.Role.__new__(discord.Role)
    role.id = role_id
    return role


class _Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def _seed_both(pool, blob, guild_id=GUILD_ID):
    """Seed the settings LRU AND the fake row, so a re-read agrees with it."""
    _seed(blob, guild_id)
    pool.blob = dict(blob)


def _panel(pool, blob=None, channels=(), roles=(), guild=None):
    """An open /ticket config panel over ``blob``, with a message to edit."""
    _seed_both(pool, blob or {})
    guild = guild if guild is not None else _Guild(channels=channels, roles=roles)
    view = panel.TicketConfigPanel(_cog(pool), guild, MEMBER_ID, dict(pool.blob))
    view.message = _Message()
    return view


def _panel_text(view):
    return "\n".join(
        child.content
        for child in view.children[0].children
        if hasattr(child, "content")
    )


def _selects(view):
    return [
        child
        for child in view.walk_children()
        if isinstance(child, discord.ui.Select)
        or isinstance(child, (discord.ui.ChannelSelect, discord.ui.RoleSelect))
    ]


def _buttons(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def _configured_blob():
    return {
        guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
        guild_config.KEY_SUPPORT_ROLE: ROLE_ID,
        guild_config.KEY_LOG_CHANNEL: LOG_ID,
        guild_config.KEY_MAX_OPEN_PER_USER: 4,
        guild_config.KEY_INACTIVITY_HOURS: 24,
        guild_config.KEY_PANEL_MESSAGE: "Ask away",
    }


# -- what the panel shows ---------------------------------------------------


def test_an_unconfigured_guild_sees_the_defaults_marked_as_defaults():
    view = _panel(_Pool())
    text = _panel_text(view)

    assert "Disabled" in text
    assert "*Not set.*" in text
    # The two numeric keys are INHERITED here, and the card says so rather than
    # presenting the bot's default as the server's choice.
    assert "2 (default)" in text
    assert "72h (default)" in text
    assert "Default wording" in text


def test_a_configured_guild_sees_its_own_values_without_the_default_marker():
    channel = _TextChannel()
    log_channel = _TextChannel(channel_id=LOG_ID)
    role = _picker_role()
    view = _panel(
        _Pool(),
        _configured_blob(),
        channels=[channel, log_channel],
        roles=[role],
    )
    text = _panel_text(view)

    assert "Enabled" in text
    assert channel.mention in text
    assert log_channel.mention in text
    assert role.mention in text
    assert "Ask away" in text
    assert "(default)" not in text


def test_a_long_blurb_is_previewed_on_one_bounded_line():
    blob = {guild_config.KEY_PANEL_MESSAGE: "line one\nline two " + "x" * 500}
    view = _panel(_Pool(), blob)
    text = _panel_text(view)

    assert "line one line two" in text
    assert "\n\n" not in text
    assert len("x" * 500) > panel.BLURB_PREVIEW_LENGTH
    assert "..." in text


def test_the_selects_preselect_what_the_guild_stored():
    channel = _TextChannel()
    log_channel = _TextChannel(channel_id=LOG_ID)
    role = _picker_role()
    view = _panel(
        _Pool(),
        _configured_blob(),
        channels=[channel, log_channel],
        roles=[role],
    )
    counts = [s for s in _selects(view) if getattr(s, "options", None)]
    chosen = {
        option.value
        for select in counts
        for option in select.options
        if option.default
    }

    assert chosen == {"4", "24"}


def test_an_unconfigured_count_preselects_the_reset_option():
    view = _panel(_Pool())
    counts = [s for s in _selects(view) if getattr(s, "options", None)]
    defaults = [
        [o.value for o in select.options if o.default] for select in counts
    ]

    # Both count selects open on "Default", which is the option that DELETES.
    assert defaults == [[panel.RESET_VALUE], [panel.RESET_VALUE]]


def test_a_deleted_channel_is_never_offered_as_a_preselected_value():
    # The id resolves to nothing (the channel was deleted); Discord rejects a
    # default value it cannot resolve, so the picker opens empty and the
    # overview line above is what says "(deleted)".
    view = _panel(_Pool(), {guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})

    assert "(deleted)" in _panel_text(view)
    for select in _selects(view):
        assert list(getattr(select, "default_values", [])) == []


def test_the_reset_message_button_is_dead_when_there_is_no_blurb():
    without = _buttons(_panel(_Pool()))
    with_blurb = _buttons(
        _panel(_Pool(), {guild_config.KEY_PANEL_MESSAGE: "Ask away"})
    )

    assert [b.disabled for b in without] == [False, True]
    assert [b.disabled for b in with_blurb] == [False, False]


def test_the_panel_stays_inside_discords_component_budget():
    # Components V2 caps a message at 40 components; this panel is fixed-size
    # (six keys), so the check is a regression guard for whoever adds a seventh.
    view = _panel(_Pool(), _configured_blob())

    assert len(list(view.walk_children())) < 40


# -- writing ----------------------------------------------------------------


async def test_picking_a_panel_channel_writes_the_key_and_turns_tickets_on():
    channel = _TextChannel()
    pool = _Pool()
    view = _panel(pool, channels=[channel])
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_panel_channel(interaction, channel)

    assert pool.settings_writes == [(guild_config.KEY_PANEL_CHANNEL, CHANNEL_ID)]
    assert "Enabled" in _panel_text(view)


async def test_clearing_the_panel_channel_deletes_the_key_and_turns_tickets_off():
    channel = _TextChannel()
    pool = _Pool()
    view = _panel(pool, _configured_blob(), channels=[channel], roles=[_picker_role()])
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_panel_channel(interaction, None)

    assert pool.settings_deletes == [guild_config.KEY_PANEL_CHANNEL]
    assert pool.settings_writes == []
    assert guild_config.KEY_PANEL_CHANNEL not in pool.blob
    # And ONLY that key: the rest of the configuration is the server's.
    assert guild_config.KEY_SUPPORT_ROLE in pool.blob
    assert "Disabled" in _panel_text(view)


async def test_a_channel_the_bot_cannot_run_tickets_in_is_refused_unwritten():
    channel = _TextChannel(perms=_Perms(create_private_threads=False))
    pool = _Pool()
    view = _panel(pool, channels=[channel])
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_panel_channel(interaction, channel)

    assert pool.settings_writes == []
    assert pool.settings_deletes == []
    assert "Create Private Threads" in interaction.replies[0]
    # The select is showing a channel the configuration does not have, so the
    # card is redrawn to put it back.
    assert view.message.edits


async def test_a_channel_the_guild_cache_cannot_resolve_is_refused():
    # No permissions object at all reads as "everything is missing" - the safe
    # direction, and the one that never writes an unusable configuration.
    pool = _Pool()
    view = _panel(pool)
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_panel_channel(interaction, types.SimpleNamespace(
        id=CHANNEL_ID, mention=f"<#{CHANNEL_ID}>"
    ))

    assert pool.settings_writes == []
    assert "View Channel" in interaction.replies[0]


async def test_picking_and_clearing_the_support_role():
    role = _picker_role()
    pool = _Pool()
    view = _panel(pool, roles=[role])
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_support_role(interaction, role)
    assert pool.settings_writes == [(guild_config.KEY_SUPPORT_ROLE, ROLE_ID)]

    await view.set_support_role(interaction, None)
    assert pool.settings_deletes == [guild_config.KEY_SUPPORT_ROLE]
    assert guild_config.KEY_SUPPORT_ROLE not in pool.blob


async def test_picking_and_clearing_the_log_channel():
    log_channel = _TextChannel(channel_id=LOG_ID)
    pool = _Pool()
    view = _panel(pool, channels=[log_channel])
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_log_channel(interaction, log_channel)
    assert pool.settings_writes == [(guild_config.KEY_LOG_CHANNEL, LOG_ID)]

    await view.set_log_channel(interaction, None)
    assert pool.settings_deletes == [guild_config.KEY_LOG_CHANNEL]


async def test_a_count_is_stored_as_a_number_not_as_the_select_string():
    pool = _Pool()
    view = _panel(pool)
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_count(interaction, guild_config.KEY_MAX_OPEN_PER_USER, "4")

    assert pool.settings_writes == [(guild_config.KEY_MAX_OPEN_PER_USER, 4)]


async def test_choosing_default_deletes_the_count_key():
    pool = _Pool()
    view = _panel(pool, {guild_config.KEY_INACTIVITY_HOURS: 24})
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_count(
        interaction, guild_config.KEY_INACTIVITY_HOURS, panel.RESET_VALUE
    )

    assert pool.settings_deletes == [guild_config.KEY_INACTIVITY_HOURS]
    assert pool.settings_writes == []
    assert "72h (default)" in _panel_text(view)


async def test_a_count_outside_the_bounds_is_clamped_before_it_is_stored():
    # The panel only offers legal presets, so this can only happen if the preset
    # list drifts out of the storage bounds - clamped there, never written raw.
    pool = _Pool()
    view = _panel(pool)
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_count(interaction, guild_config.KEY_INACTIVITY_HOURS, "9999")

    assert pool.settings_writes == [
        (guild_config.KEY_INACTIVITY_HOURS, guild_config.MAX_INACTIVITY_HOURS)
    ]


async def test_the_blurb_modal_stores_trimmed_text():
    pool = _Pool()
    view = _panel(pool)
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_blurb(interaction, "  Ask away  ")

    assert pool.settings_writes == [(guild_config.KEY_PANEL_MESSAGE, "Ask away")]


async def test_a_blank_modal_submit_deletes_the_blurb():
    pool = _Pool()
    view = _panel(pool, {guild_config.KEY_PANEL_MESSAGE: "Ask away"})
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_blurb(interaction, "   ")

    assert pool.settings_deletes == [guild_config.KEY_PANEL_MESSAGE]
    assert pool.settings_writes == []


async def test_the_reset_message_button_deletes_the_blurb():
    pool = _Pool()
    view = _panel(pool, {guild_config.KEY_PANEL_MESSAGE: "Ask away"})
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)
    reset = _buttons(view)[1]

    await reset.callback(interaction)

    assert pool.settings_deletes == [guild_config.KEY_PANEL_MESSAGE]


async def test_the_modal_opens_on_the_stored_blurb():
    view = _panel(_Pool(), {guild_config.KEY_PANEL_MESSAGE: "Ask away"})
    modal = panel._PanelMessageModal(view)

    assert modal.field.default == "Ask away"
    assert modal.field.required is False
    assert modal.field.max_length == guild_config.MAX_PANEL_MESSAGE_LENGTH


async def test_a_redraw_never_re_pings_the_role_the_card_mentions():
    # The card carries live <@&> / <#> tokens, and a Components V2 edit resends
    # every TextDisplay - so every refresh suppresses mentions.
    pool = _Pool()
    view = _panel(pool, roles=[_picker_role()])
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_support_role(interaction, _picker_role())

    assert interaction.edits
    mentions = interaction.edits[-1]["allowed_mentions"]
    assert (mentions.roles, mentions.users, mentions.everyone) == (False, False, False)


async def test_a_failed_write_apologises_and_changes_nothing():
    pool = _Pool()
    view = _panel(pool, {guild_config.KEY_MAX_OPEN_PER_USER: 3})
    pool.execute_error = RuntimeError("db down")
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)

    await view.set_count(interaction, guild_config.KEY_MAX_OPEN_PER_USER, "5")

    assert "went wrong" in interaction.replies[0]
    assert interaction.edits == []
    assert view.state["max_open"] == 3


async def test_a_write_redraws_from_a_fresh_read_not_from_its_own_echo():
    # Another surface (a second panel, /ticket setup, the dashboard) may have
    # moved a different key since this panel opened; the redraw re-reads.
    pool = _Pool()
    view = _panel(pool, {guild_config.KEY_MAX_OPEN_PER_USER: 3})
    interaction = _Interaction(view.guild, _Member(), view.cog.bot)
    # Somebody else sets the inactivity window behind this panel's back.
    await guild_config.set_key(
        pool, GUILD_ID, guild_config.KEY_INACTIVITY_HOURS, 24
    )

    await view.set_count(interaction, guild_config.KEY_MAX_OPEN_PER_USER, "5")

    assert view.state["max_open"] == 5
    assert view.state["inactivity_hours"] == 24
    assert "24h" in _panel_text(view)
    assert "(default)" not in _panel_text(view)


# -- the log channel is a transcript destination ----------------------------


def _log_world(*, public):
    """A guild whose log channel everybody can (or cannot) read."""
    channel = _TextChannel()
    log_channel = _TextChannel(channel_id=LOG_ID)
    guild = _Guild(channels=[channel, log_channel])
    log_channel.guild = guild
    if not public:
        log_channel.set_permissions_for(
            guild.default_role, _Perms(view_channel=False)
        )
    return guild, channel, log_channel


def test_both_surfaces_call_the_log_channel_what_it_is_a_transcript_sink():
    # "Ticket activity" would be a comfortable label for a control that copies a
    # private conversation into a channel of the manager's choosing.
    guild, _channel, _log = _log_world(public=False)
    card = panel.TicketStatusView(
        guild, _config(panel_channel=CHANNEL_ID, log_channel=LOG_ID)
    )
    view = _panel(_Pool(), {guild_config.KEY_LOG_CHANNEL: LOG_ID}, guild=guild)

    assert "transcript" in _card_text(card)
    assert "transcript" in _panel_text(view)
    placeholders = [s.placeholder for s in _selects(view) if s.placeholder]
    assert any("transcript" in p for p in placeholders)


def test_a_log_channel_everybody_can_read_is_warned_about_on_both_surfaces():
    # A transcript in a public channel publishes a private thread server-wide.
    guild, _channel, _log = _log_world(public=True)
    card = panel.TicketStatusView(
        guild, _config(panel_channel=CHANNEL_ID, log_channel=LOG_ID)
    )
    view = _panel(_Pool(), {guild_config.KEY_LOG_CHANNEL: LOG_ID}, guild=guild)

    assert "everyone can read" in _card_text(card)
    assert "everyone can read" in _panel_text(view)


def test_a_staff_only_log_channel_is_not_nagged_about():
    guild, _channel, _log = _log_world(public=False)
    card = panel.TicketStatusView(
        guild, _config(panel_channel=CHANNEL_ID, log_channel=LOG_ID)
    )
    view = _panel(_Pool(), {guild_config.KEY_LOG_CHANNEL: LOG_ID}, guild=guild)

    assert "everyone can read" not in _card_text(card)
    assert "everyone can read" not in _panel_text(view)


async def test_setup_warns_when_the_log_channel_it_kept_is_public():
    _seed({guild_config.KEY_LOG_CHANNEL: LOG_ID})
    guild, channel, _log = _log_world(public=True)
    cog = _cog(_Pool())
    ctx = _Ctx(guild)

    await cog.ticket_setup.callback(cog, ctx, channel)

    assert "everyone can read" in ctx.texts[-1]
    assert "transcript" in ctx.texts[-1]


# -- the command ------------------------------------------------------------


async def test_the_config_command_opens_a_panel_gated_to_its_invoker():
    pool = _Pool()
    _seed_both(pool, _configured_blob())
    cog = _cog(pool)
    ctx = _Ctx(_Guild(), author_id=MEMBER_ID)

    await cog.ticket_config.callback(cog, ctx)

    _args, kwargs = ctx.sends[0]
    view = kwargs["view"]
    assert isinstance(view, panel.TicketConfigPanel)
    assert view.author_id == MEMBER_ID
    assert view.message is not None
    # Opened with mentions suppressed, like the status card.
    assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)


async def test_the_config_panel_survives_a_settings_read_that_failed():
    # read_raw answers None on failure; the panel must open on the defaults
    # rather than raise in front of a manager.
    class _DeadPool(_Pool):
        async def fetchval(self, query, *args):
            raise RuntimeError("db down")

    pool = _DeadPool()
    settings._cache.clear()
    cog = _cog(pool)
    ctx = _Ctx(_Guild(), author_id=MEMBER_ID)

    await cog.ticket_config.callback(cog, ctx)

    _args, kwargs = ctx.sends[0]
    assert "2 (default)" in _panel_text(kwargs["view"])


# ---------------------------------------------------------------------------
# guild_config: the pieces lot T3 added under the panel
# ---------------------------------------------------------------------------


def test_every_inactivity_preset_is_storable_and_the_default_is_one_of_them():
    assert guild_config.DEFAULT_INACTIVITY_HOURS in guild_config.INACTIVITY_PRESET_HOURS
    for hours in guild_config.INACTIVITY_PRESET_HOURS:
        assert guild_config.MIN_INACTIVITY_HOURS <= hours
        assert hours <= guild_config.MAX_INACTIVITY_HOURS
    assert list(guild_config.INACTIVITY_PRESET_HOURS) == sorted(
        guild_config.INACTIVITY_PRESET_HOURS
    )


def test_the_key_order_is_stable_and_covers_every_key():
    assert set(guild_config.KEY_ORDER) == guild_config.KEYS
    assert len(guild_config.KEY_ORDER) == len(guild_config.KEYS) == 6


async def test_set_key_removes_the_key_instead_of_storing_a_null():
    pool = _Pool()
    _seed_both(pool, {guild_config.KEY_SUPPORT_ROLE: ROLE_ID, "welcome": {"on": 1}})

    await guild_config.set_key(pool, GUILD_ID, guild_config.KEY_SUPPORT_ROLE, None)

    assert pool.settings_deletes == [guild_config.KEY_SUPPORT_ROLE]
    assert guild_config.KEY_SUPPORT_ROLE not in pool.blob
    # A sibling feature sharing the same row is untouched...
    assert pool.blob["welcome"] == {"on": 1}
    # ... and the LRU no longer serves the stale blob.
    assert ("guild_settings", GUILD_ID) not in settings._cache


async def test_set_key_writes_one_key_and_leaves_the_rest_of_the_row_alone():
    pool = _Pool()
    _seed_both(pool, {"welcome": {"on": 1}})

    await guild_config.set_key(pool, GUILD_ID, guild_config.KEY_LOG_CHANNEL, LOG_ID)

    assert pool.blob == {"welcome": {"on": 1}, guild_config.KEY_LOG_CHANNEL: LOG_ID}


async def test_read_raw_says_none_on_failure_rather_than_faking_an_empty_config():
    class _DeadPool(_Pool):
        async def fetchval(self, query, *args):
            raise RuntimeError("db down")

    assert await guild_config.read_raw(_DeadPool(), GUILD_ID) is None
    assert await guild_config.read_raw(None, GUILD_ID) is None


def test_resolve_of_a_failed_read_is_exactly_the_bot_defaults():
    assert guild_config.resolve(None) == {
        "panel_channel": None,
        "support_role": None,
        "log_channel": None,
        "max_open": guild_config.DEFAULT_MAX_OPEN_PER_USER,
        "inactivity_hours": guild_config.DEFAULT_INACTIVITY_HOURS,
        "panel_message": None,
    }


async def test_read_raw_keeps_the_values_uncoerced_so_absence_stays_visible():
    pool = _Pool()
    # The dashboard writes snowflakes as STRINGS; raw keeps them, resolve fixes
    # them, and both answers are needed (presence vs. usable value).
    _seed_both(pool, {guild_config.KEY_PANEL_CHANNEL: str(CHANNEL_ID)})

    raw = await guild_config.read_raw(pool, GUILD_ID)

    assert raw[guild_config.KEY_PANEL_CHANNEL] == str(CHANNEL_ID)
    assert raw[guild_config.KEY_MAX_OPEN_PER_USER] is None
    assert guild_config.resolve(raw)["panel_channel"] == CHANNEL_ID


def test_every_picker_can_be_cleared_which_is_how_its_key_is_reset():
    # A picker with min_values=1 could never be emptied, and the key behind it
    # would have no reset at all - the property, not the label, is the contract.
    channel = _TextChannel()
    view = _panel(
        _Pool(),
        _configured_blob(),
        channels=[channel, _TextChannel(channel_id=LOG_ID)],
        roles=[_picker_role()],
    )
    pickers = [
        child
        for child in view.walk_children()
        if isinstance(child, (discord.ui.ChannelSelect, discord.ui.RoleSelect))
    ]

    assert [type(p) for p in pickers] == [
        panel._PanelChannelSelect,
        panel._SupportRoleSelect,
        panel._LogChannelSelect,
    ]
    assert [p.min_values for p in pickers] == [0, 0, 0]
    assert [p.max_values for p in pickers] == [1, 1, 1]


def test_the_channel_pickers_only_offer_channels_a_thread_can_live_on():
    view = _panel(_Pool())
    pickers = [
        c for c in view.walk_children() if isinstance(c, discord.ui.ChannelSelect)
    ]

    assert pickers
    for picker in pickers:
        assert list(picker.channel_types) == [discord.ChannelType.text]


def test_both_count_selects_carry_exactly_one_reset_option():
    view = _panel(_Pool())
    counts = [
        c
        for c in view.walk_children()
        if isinstance(c, discord.ui.Select) and getattr(c, "options", None)
    ]

    assert len(counts) == 2
    for select in counts:
        assert select.min_values == select.max_values == 1
        resets = [o for o in select.options if o.value == panel.RESET_VALUE]
        assert len(resets) == 1
    # ... and the choices behind them are the storable ones, nothing else.
    assert [o.value for o in counts[0].options[1:]] == ["1", "2", "3", "4", "5"]
    assert [o.value for o in counts[1].options[1:]] == [
        str(h) for h in guild_config.INACTIVITY_PRESET_HOURS
    ]
