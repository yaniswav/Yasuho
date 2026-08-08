"""Unit tests for ``cogs.config.tickets.lifecycle`` (lot T2).

Four surfaces, and one of them is really an ORDER:

* claim - who may take a ticket, and the four answers the single atomic
  statement can give (taken / already claimed / already closed / your own);
* close - who may end one, the confirm in front of it, and the exact sequence
  transcript -> row -> notice -> archive -> log, which is the whole safety story
  of the flow (see the module docstring in lifecycle.py). The ordering test
  records every side effect into one list and asserts the list;
* the two listeners - Discord's own auto-archive as the inactivity signal, and a
  deleted thread as a released cap slot - including the cheap guards that make
  them cost nothing for threads that are not tickets;
* the sweep - one bounded query per pass, the ACTIVE-thread cache as the
  liveness test, each guild's own window, and the cursor that keeps a healthy
  guild from starving a stale one.

Discord objects are subclassed where the code legitimately asserts a type
(``discord.Member``, ``discord.Thread``, ``discord.TextChannel``): the subclass
skips discord.py's ``__init__`` and sets only what the flow reads, so
``isinstance`` stays a real check instead of being weakened for the tests. No
network, no database, no Discord.
"""

import asyncio
import datetime
import re
import types

import discord
import pytest

from cogs.config.tickets import guild_config, lifecycle
from cogs.config.tickets import open as ticket_open
from tools import settings

GUILD_ID = 31337
PANEL_ID = 555000111
LOG_ID = 222000333
ROLE_ID = 999000111
THREAD_ID = 7001
OPENER_ID = 42
STAFF_ID = 43
STRANGER_ID = 44

NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.timezone.utc)
LONG_AGO = NOW - datetime.timedelta(days=30)


@pytest.fixture(autouse=True)
def _isolate_settings():
    settings._cache.clear()
    yield
    settings._cache.clear()


def _seed(blob, guild_id=GUILD_ID):
    settings._cache[("guild_settings", guild_id)] = dict(blob)


def _configured(**extra):
    blob = {
        guild_config.KEY_PANEL_CHANNEL: PANEL_ID,
        guild_config.KEY_SUPPORT_ROLE: ROLE_ID,
        guild_config.KEY_LOG_CHANNEL: LOG_ID,
    }
    blob.update(extra)
    _seed(blob)


def _row(**over):
    row = {
        "id": 1,
        "guild_id": GUILD_ID,
        "ticket_number": 7,
        "thread_id": THREAD_ID,
        "opener_id": OPENER_ID,
        "status": "open",
        "opened_at": LONG_AGO,
        "closed_at": None,
        "closed_by": None,
        "claimed_by": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Member(discord.Member):
    def __init__(self, user_id=OPENER_ID, name="Kira", roles=(), manage_guild=False):
        self._user = types.SimpleNamespace(id=user_id, name=name)
        self._display = name
        self._role_ids = set(roles)
        self.guild = None
        self._manage_guild = manage_guild

    @property
    def id(self):
        return self._user.id

    @property
    def guild_permissions(self):
        return types.SimpleNamespace(manage_guild=self._manage_guild)

    @property
    def display_name(self):
        return self._display

    @property
    def mention(self):
        return "<@{0}>".format(self._user.id)

    def get_role(self, role_id):
        return object() if role_id in self._role_ids else None

    def __str__(self):
        return self._display


class _History:
    def __init__(self, messages, journal, error=None):
        self.messages = list(messages)
        self.journal = journal
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.journal.append("history")
        return self._iterate()

    async def _iterate(self):
        if self.error is not None:
            raise self.error
        for message in self.messages:
            yield message


class _Thread(discord.Thread):
    def __init__(self, journal, *, archived=False, parent_id=PANEL_ID, messages=()):
        self.id = THREAD_ID
        self.archived = archived
        self.parent_id = parent_id
        self.guild = None
        self.journal = journal
        self.edits = []
        self.sends = []
        self.edit_error = None
        self.send_error = None
        self.history = _History(messages, journal)

    async def edit(self, **kwargs):
        self.journal.append("thread-edit")
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)
        return self

    async def send(self, *args, **kwargs):
        self.journal.append("thread-send")
        if self.send_error is not None:
            raise self.send_error
        self.sends.append((args, kwargs))


class _Perms:
    def __init__(self, **granted):
        for name in (
            "view_channel",
            "send_messages",
            "embed_links",
            "attach_files",
        ):
            setattr(self, name, granted.get(name, True))


class _LogChannel(discord.TextChannel):
    def __init__(self, journal, perms=None):
        self.id = LOG_ID
        self.journal = journal
        self.guild = None
        self.sends = []
        self.send_error = None
        self._perms = perms if perms is not None else _Perms()

    def permissions_for(self, _obj):
        return self._perms

    async def send(self, *args, **kwargs):
        self.journal.append("log-send")
        if self.send_error is not None:
            raise self.send_error
        self.sends.append((args, kwargs))


class _Guild:
    def __init__(self, *, threads=(), channels=(), members=(), unavailable=False):
        self.id = GUILD_ID
        self.name = "Server"
        self.me = object()
        # Real ``discord.Guild`` attribute: True for the stub parse_ready
        # re-adds on a re-IDENTIFY, whose thread cache is empty.
        self.unavailable = unavailable
        self._threads = {t.id: t for t in threads}
        self._channels = {c.id: c for c in channels}
        self._members = {m.id: m for m in members}

    def get_thread(self, ident):
        return self._threads.get(ident)

    def get_channel(self, ident):
        return self._channels.get(ident)

    def get_member(self, ident):
        return self._members.get(ident)

    def get_role(self, _ident):
        # No support role object is needed: the seam test below only cares that
        # the opening message carries the controls view.
        return None


class _Response:
    def __init__(self, parent):
        self._parent = parent
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self._parent.sent.append((args, kwargs))
        self._done = True

    async def edit_message(self, **kwargs):
        self._parent.edits.append(kwargs)
        self._done = True

    async def defer(self, *args, **kwargs):
        self._done = True


class _Followup:
    def __init__(self, parent):
        self._parent = parent

    async def send(self, *args, **kwargs):
        self._parent.followups.append((args, kwargs))


class _Interaction:
    def __init__(self, guild, member, client, channel=None):
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.user = member
        self.client = client
        self.channel = channel
        self.sent = []
        self.followups = []
        self.edits = []
        self.response = _Response(self)
        self.followup = _Followup(self)

    async def original_response(self):
        return types.SimpleNamespace(edit=self._noop)

    async def _noop(self, **_kwargs):
        return None

    @property
    def replies(self):
        return [args[0] for args, _kw in self.sent + self.followups if args]


class _Pool:
    """Answers the five statements lifecycle issues, and records their order."""

    def __init__(self, journal=None, *, by_thread=None, claim=None, close="row",
                 sweep=()):
        self.journal = journal if journal is not None else []
        self.calls = []
        self.by_thread = by_thread
        self.claim = claim
        # ``"row"`` means "derive the closed row from by_thread"; None means the
        # exactly-once gate refused this caller.
        self.close = close
        self.sweep = list(sweep)
        self.raise_on = None

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if self.raise_on and self.raise_on in query:
            raise RuntimeError("db down")
        if "WITH claimed AS" in query:
            self.journal.append("db-claim")
            return self.claim
        if "SET status = 'closed'" in query:
            self.journal.append("db-close")
            if self.close == "row":
                base = dict(self.by_thread or _row())
                base.update(
                    status="closed", closed_at=NOW, closed_by=args[1]
                )
                return base
            return self.close
        if "FROM tickets WHERE thread_id" in query:
            self.journal.append("db-read")
            return self.by_thread
        raise AssertionError("unexpected statement: " + query)

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        self.journal.append("db-sweep")
        return self.sweep

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return None

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "UPDATE 1"


class _Bot:
    def __init__(self, pool, guilds=()):
        self.db_pool = pool
        self._guilds = {g.id: g for g in guilds}
        self.dynamic = []
        self.removed = []
        self._ready = True

    def get_guild(self, ident):
        return self._guilds.get(ident)

    def is_ready(self):
        return self._ready

    def add_dynamic_items(self, *items):
        self.dynamic.extend(items)

    def remove_dynamic_items(self, *items):
        self.removed.extend(items)


def _world(*, journal=None, archived=False, log_channel=True, thread_messages=(),
           pool_kwargs=None, member=None, guild_threads=None):
    """One assembled guild: thread, log channel, bot, pool and a shared journal."""
    journal = journal if journal is not None else []
    thread = _Thread(journal, archived=archived, messages=thread_messages)
    channel = _LogChannel(journal)
    channels = [channel] if log_channel else []
    opener = _Member(OPENER_ID, "Kira")
    threads = (thread,) if guild_threads is None else guild_threads
    guild = _Guild(threads=threads, channels=channels, members=[opener])
    thread.guild = guild
    channel.guild = guild
    pool = _Pool(journal, **(pool_kwargs or {}))
    bot = _Bot(pool, guilds=[guild])
    if member is not None:
        member.guild = guild
    return types.SimpleNamespace(
        journal=journal,
        thread=thread,
        channel=channel,
        guild=guild,
        pool=pool,
        bot=bot,
        member=member,
    )


# ---------------------------------------------------------------------------
# The custom_id namespace
# ---------------------------------------------------------------------------


def test_the_templates_live_in_the_reserved_namespace_and_are_disjoint():
    assert lifecycle.CLOSE_TEMPLATE.startswith("tk:")
    assert lifecycle.CLAIM_TEMPLATE.startswith("tk:")
    assert not lifecycle.CLOSE_TEMPLATE.startswith(lifecycle.CLAIM_TEMPLATE[:8])


def test_a_button_round_trips_through_its_custom_id():
    """A click on a message posted before this restart must resolve its ticket."""
    button = lifecycle.TicketCloseButton(THREAD_ID)
    assert button.item.custom_id == "tk:close:{0}".format(THREAD_ID)
    match = re.fullmatch(lifecycle.CLOSE_TEMPLATE, button.item.custom_id)
    assert int(match["tid"]) == THREAD_ID


def test_the_controls_view_is_persistent_and_carries_both_buttons():
    view = lifecycle.TicketControlsView(THREAD_ID)
    assert view.timeout is None
    kinds = {type(child) for child in view.children}
    assert kinds == {lifecycle.TicketClaimButton, lifecycle.TicketCloseButton}


# ---------------------------------------------------------------------------
# Who is staff
# ---------------------------------------------------------------------------


async def test_the_support_role_is_the_one_t1_configured():
    _configured()
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    member.guild = _Guild()
    assert await lifecycle.is_support(member, _Pool()) is True


async def test_a_member_without_the_role_is_not_support():
    _configured()
    member = _Member(STRANGER_ID, "Rohan")
    member.guild = _Guild()
    assert await lifecycle.is_support(member, _Pool()) is False


async def test_manage_guild_is_staff_even_with_no_support_role_configured():
    _seed({guild_config.KEY_PANEL_CHANNEL: PANEL_ID})
    member = _Member(STAFF_ID, "Admin", manage_guild=True)
    member.guild = _Guild()
    assert await lifecycle.is_support(member, _Pool()) is True


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


async def _claim(member, claim_result, **world_kwargs):
    _configured()
    world = _world(member=member, pool_kwargs={"claim": claim_result},
                   **world_kwargs)
    interaction = _Interaction(world.guild, member, world.bot, channel=world.thread)
    await lifecycle._run_claim(interaction, THREAD_ID)
    return world, interaction


async def test_a_stranger_cannot_claim_and_no_statement_is_issued():
    member = _Member(STRANGER_ID, "Rohan")
    world, interaction = await _claim(member, None)
    assert "support team" in interaction.replies[0]
    assert "db-claim" not in world.journal


async def test_support_claims_the_ticket_and_the_thread_is_renamed():
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    claim = {"ticket_number": 7, "status": "open", "opener_id": OPENER_ID,
             "claimed_by": STAFF_ID, "taken": True}

    world, interaction = await _claim(member, claim)

    assert world.thread.edits[0]["name"] == "ticket-7-Josuke"
    assert "#7" in interaction.replies[0]
    # The room is told, without pinging anybody.
    assert world.thread.sends
    assert world.thread.sends[0][1]["allowed_mentions"].users is False


async def test_a_second_claimer_is_told_who_holds_it():
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    claim = {"ticket_number": 7, "status": "open", "opener_id": OPENER_ID,
             "claimed_by": 999, "taken": False}

    _world_, interaction = await _claim(member, claim)

    assert "<@999>" in interaction.replies[0]


async def test_the_opener_cannot_claim_their_own_ticket():
    """Even an opener who happens to be staff: the statement refused them."""
    member = _Member(OPENER_ID, "Kira", roles=[ROLE_ID])
    claim = {"ticket_number": 7, "status": "open", "opener_id": OPENER_ID,
             "claimed_by": None, "taken": False}

    _world_, interaction = await _claim(member, claim)

    assert "your own" in interaction.replies[0]


async def test_a_closed_ticket_cannot_be_claimed():
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    claim = {"ticket_number": 7, "status": "closed", "opener_id": OPENER_ID,
             "claimed_by": None, "taken": False}

    _world_, interaction = await _claim(member, claim)

    assert "closed" in interaction.replies[0]


async def test_a_thread_that_is_not_a_ticket_refuses_cleanly():
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    _world_, interaction = await _claim(member, None)
    assert "not a ticket" in interaction.replies[0]


async def test_a_failed_rename_never_loses_the_claim():
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    claim = {"ticket_number": 7, "status": "open", "opener_id": OPENER_ID,
             "claimed_by": STAFF_ID, "taken": True}
    _configured()
    world = _world(member=member, pool_kwargs={"claim": claim})
    world.thread.edit_error = discord.HTTPException(
        types.SimpleNamespace(status=403, reason="no"), "nope"
    )
    interaction = _Interaction(world.guild, member, world.bot, channel=world.thread)

    await lifecycle._run_claim(interaction, THREAD_ID)

    assert "#7" in interaction.replies[0]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Josuke", "ticket-7-Josuke"),
        ("  spaced   out \n", "ticket-7-spaced out"),
        ("", "ticket-7"),
        ("z" * 300, "ticket-7-" + "z" * 91),
    ],
)
def test_the_claimed_thread_name_is_bounded_and_tidy(name, expected):
    got = lifecycle.claimed_thread_name(7, name)
    assert got == expected
    assert len(got) <= lifecycle.MAX_THREAD_NAME


# ---------------------------------------------------------------------------
# Close: who may
# ---------------------------------------------------------------------------


async def _close_click(member, row=None, **world_kwargs):
    _configured()
    world = _world(member=member,
                   pool_kwargs={"by_thread": row if row is not None else _row()},
                   **world_kwargs)
    interaction = _Interaction(world.guild, member, world.bot, channel=world.thread)
    await lifecycle._run_close_click(interaction, THREAD_ID)
    return world, interaction


async def test_the_opener_may_close_their_own_ticket():
    member = _Member(OPENER_ID, "Kira")
    _world_, interaction = await _close_click(member)
    assert interaction.sent, "the opener should get the confirm prompt"
    assert interaction.sent[0][1]["ephemeral"] is True


async def test_support_may_close_somebody_elses_ticket():
    member = _Member(STAFF_ID, "Josuke", roles=[ROLE_ID])
    _world_, interaction = await _close_click(member)
    assert interaction.sent


async def test_a_bystander_may_not_close_a_ticket():
    member = _Member(STRANGER_ID, "Rohan")
    _world_, interaction = await _close_click(member)
    assert "support team" in interaction.replies[0]
    assert not any(isinstance(kw.get("view"), discord.ui.View)
                   for _a, kw in interaction.sent)


async def test_an_already_closed_ticket_is_refused_before_any_prompt():
    member = _Member(OPENER_ID, "Kira")
    _world_, interaction = await _close_click(member, row=_row(status="closed"))
    assert "already closed" in interaction.replies[0]


async def test_a_thread_that_is_not_a_ticket_cannot_be_closed():
    member = _Member(OPENER_ID, "Kira")
    _configured()
    world = _world(member=member, pool_kwargs={"by_thread": None})
    interaction = _Interaction(world.guild, member, world.bot, channel=world.thread)

    await lifecycle._run_close_click(interaction, THREAD_ID)

    assert "not a ticket" in interaction.replies[0]


def test_the_confirm_prompt_is_author_gated():
    view = lifecycle._CloseConfirmView(OPENER_ID, THREAD_ID, 7)
    assert view.author_id == OPENER_ID
    assert len(view.children) == 2


# ---------------------------------------------------------------------------
# Close: THE ORDER
# ---------------------------------------------------------------------------


async def test_the_close_runs_transcript_row_notice_archive_log_in_that_order():
    """The whole safety story of the flow, asserted as one sequence.

    Transcript before anything can archive, lock or delete the room; the row
    before any best-effort Discord call, because it frees the cap slot and
    because it is what makes our own archive event a no-op; the in-thread notice
    before the archive, because an archived thread cannot be posted in; the log
    last, because a log channel nobody configured must not block a close.
    """
    _configured()
    world = _world(thread_messages=[_message("hello")],
                   pool_kwargs={"by_thread": _row()})
    closer = _Member(OPENER_ID, "Kira")

    closed = await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=closer, reason=lifecycle.REASON_MANUAL,
    )

    assert closed is not None
    assert world.journal == [
        "history", "db-close", "thread-send", "thread-edit", "log-send"
    ]


async def test_the_archive_also_locks_the_thread():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})

    await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    assert world.thread.edits[0]["archived"] is True
    assert world.thread.edits[0]["locked"] is True


async def test_losing_the_exactly_once_gate_stops_everything_after_it():
    """A second closer archives nothing and logs nothing - one close, one log."""
    _configured()
    world = _world(pool_kwargs={"by_thread": _row(), "close": None})

    closed = await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    assert closed is None
    assert world.thread.edits == []
    assert world.channel.sends == []


async def test_a_close_with_no_log_channel_still_closes_and_says_so():
    _seed({guild_config.KEY_PANEL_CHANNEL: PANEL_ID})
    world = _world(log_channel=False, pool_kwargs={"by_thread": _row()})

    closed = await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    assert closed is not None
    assert world.thread.edits[0]["archived"] is True
    # No log channel means the history was never even read.
    assert "history" not in world.journal
    assert "no transcript was saved" in world.thread.sends[0][0][0]


async def test_the_transcript_goes_to_the_log_channel_and_nowhere_else():
    _configured()
    world = _world(thread_messages=[_message("secret")],
                   pool_kwargs={"by_thread": _row()})

    await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    _args, kwargs = world.channel.sends[0]
    assert isinstance(kwargs["file"], discord.File)
    # ... and the room itself got a plain message with no attachment.
    assert all("file" not in kw for _a, kw in world.thread.sends)


async def test_the_log_summary_carries_the_whole_ticket():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row(claimed_by=STAFF_ID)})

    await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(claimed_by=STAFF_ID),
        closed_by=_Member(STAFF_ID, "Josuke"), reason=lifecycle.REASON_MANUAL,
    )

    _args, kwargs = world.channel.sends[0]
    embed = kwargs["embed"]
    values = {field.name: field.value for field in embed.fields}
    assert "#7" in embed.title
    assert values["Opened by"] == "<@{0}>".format(OPENER_ID)
    assert values["Claimed by"] == "<@{0}>".format(STAFF_ID)
    assert values["Ended by"] == "<@{0}>".format(STAFF_ID)
    assert values["Opened"].startswith("<t:")
    assert values["Closed"].startswith("<t:")
    # A log line is a record, not a notification.
    assert kwargs["allowed_mentions"].users is False


async def test_an_unclaimed_ticket_says_nobody_rather_than_an_empty_field():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})

    await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    embed = world.channel.sends[0][1]["embed"]
    values = {field.name: field.value for field in embed.fields}
    assert values["Claimed by"] == "Nobody"


async def test_a_transcript_that_could_not_be_read_is_stated_in_the_log():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.thread.history.error = discord.HTTPException(
        types.SimpleNamespace(status=403, reason="no"), "no history"
    )

    await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    kwargs = world.channel.sends[0][1]
    assert kwargs["file"] is None
    assert "No transcript" in kwargs["embed"].footer.text


async def test_a_failed_archive_does_not_undo_the_close():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.thread.edit_error = discord.HTTPException(
        types.SimpleNamespace(status=403, reason="no"), "nope"
    )

    closed = await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=_Member(OPENER_ID), reason=lifecycle.REASON_MANUAL,
    )

    assert closed is not None
    assert world.channel.sends, "the log line still lands"


# ---------------------------------------------------------------------------
# Auto-archive as the inactivity signal
# ---------------------------------------------------------------------------


def _cog(world):
    cog = lifecycle.TicketLifecycle.__new__(lifecycle.TicketLifecycle)
    cog.bot = world.bot
    cog._sweep_cursor = 0
    return cog


async def test_an_archive_flip_closes_the_ticket_with_nobody_named():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.thread.archived = True
    before = types.SimpleNamespace(archived=False)

    await _cog(world).on_thread_update(before, world.thread)

    close_calls = [c for c in world.pool.calls if "SET status = 'closed'" in c[0]]
    assert close_calls and close_calls[0][1][1] is None
    embed = world.channel.sends[0][1]["embed"]
    values = {field.name: field.value for field in embed.fields}
    assert values["Ended by"] == "Auto-closed after inactivity."


async def test_the_archived_thread_is_never_unarchived_or_edited():
    """Reads work on an archived thread; the only edit Discord takes is undoing it."""
    _configured()
    world = _world(thread_messages=[_message("bye")],
                   pool_kwargs={"by_thread": _row()})
    world.thread.archived = True

    await _cog(world).on_thread_update(
        types.SimpleNamespace(archived=False), world.thread
    )

    assert world.thread.edits == []
    assert world.thread.sends == []
    # The transcript was still taken, straight off the archived thread.
    assert "history" in world.journal
    assert world.channel.sends[0][1]["file"] is not None
    # ... and the log line SAYS what the attachment is, every time: a server
    # that set a log channel is never asked again, so this is the notice.
    assert "Transcript attached" in world.channel.sends[0][1]["embed"].footer.text


async def test_an_update_that_is_not_the_flip_does_nothing_at_all():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.thread.archived = True

    await _cog(world).on_thread_update(
        types.SimpleNamespace(archived=True), world.thread
    )

    assert world.pool.calls == []


async def test_a_thread_outside_the_ticket_channel_costs_no_query():
    """The cheap guard: one cached settings read, then nothing."""
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.thread.archived = True
    world.thread.parent_id = 123456

    await _cog(world).on_thread_update(
        types.SimpleNamespace(archived=False), world.thread
    )

    assert world.pool.calls == []


async def test_our_own_close_is_self_suppressing():
    """Closing writes the row BEFORE archiving, so the event it fires is a no-op."""
    _configured()
    world = _world(pool_kwargs={"by_thread": _row(status="closed")})
    world.thread.archived = True

    await _cog(world).on_thread_update(
        types.SimpleNamespace(archived=False), world.thread
    )

    assert not [c for c in world.pool.calls if "SET status = 'closed'" in c[0]]


async def test_a_second_close_of_the_same_ticket_pays_for_no_second_transcript():
    """The database refuses the loser anyway - but only AFTER ten history pages.

    A double-confirmed close (or a click landing on the archive listener) must
    not have both callers render a transcript to throw one away.
    """
    _configured()
    world = _world(thread_messages=[_message("hi")], pool_kwargs={"by_thread": _row()})
    started = asyncio.Event()
    release = asyncio.Event()
    real_history = world.thread.history

    async def _gated(inner):
        started.set()
        await release.wait()
        async for message in inner:
            yield message

    world.thread.history = lambda **kwargs: _gated(real_history(**kwargs))

    first = asyncio.create_task(
        lifecycle.perform_close(
            world.bot, world.guild, world.thread, _row(),
            closed_by=None, reason=lifecycle.REASON_MANUAL,
        )
    )
    await started.wait()
    second = await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=None, reason=lifecycle.REASON_MANUAL,
    )
    release.set()

    assert second is None  # the caller reads this as "already closed"
    assert await first is not None
    assert world.journal.count("history") == 1
    # ... and the guard releases itself, so the next close is not refused.
    assert THREAD_ID not in lifecycle._CLOSING


async def test_a_close_that_raises_still_releases_its_in_flight_entry():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.pool.raise_on = "SET status = 'closed'"

    # close_ticket failures are swallowed into a None; nothing may leak either way.
    assert await lifecycle.perform_close(
        world.bot, world.guild, world.thread, _row(),
        closed_by=None, reason=lifecycle.REASON_MANUAL,
    ) is None
    assert THREAD_ID not in lifecycle._CLOSING


async def test_the_archive_close_runs_inside_the_concurrency_bound(monkeypatch):
    """The one fan-out path: a cohort that went quiet together archives together."""
    seen = []

    class _Recorder:
        async def __aenter__(self):
            seen.append("enter")

        async def __aexit__(self, *_exc):
            seen.append("exit")
            return False

    monkeypatch.setattr(lifecycle, "_ARCHIVE_CLOSES", _Recorder())
    assert isinstance(lifecycle._ARCHIVE_CLOSE_LIMIT, int)
    assert 1 <= lifecycle._ARCHIVE_CLOSE_LIMIT <= 10

    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    world.thread.archived = True

    await _cog(world).on_thread_update(
        types.SimpleNamespace(archived=False), world.thread
    )

    assert seen == ["enter", "exit"]
    assert world.channel.sends, "the close still ran, just inside the bound"


async def test_a_thread_that_is_not_a_ticket_never_takes_a_slot(monkeypatch):
    # The cheap guards stay OUTSIDE the semaphore: thousands of threads archive
    # in a big guild and none of them may queue behind a transcript.
    taken = []

    class _Recorder:
        async def __aenter__(self):
            taken.append(1)

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lifecycle, "_ARCHIVE_CLOSES", _Recorder())
    _configured()
    world = _world(pool_kwargs={"by_thread": None})
    world.thread.archived = True

    await _cog(world).on_thread_update(
        types.SimpleNamespace(archived=False), world.thread
    )

    assert taken == []


# ---------------------------------------------------------------------------
# A deleted thread releases its cap slot
# ---------------------------------------------------------------------------


async def test_a_deleted_ticket_thread_closes_its_row():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    payload = types.SimpleNamespace(
        guild_id=GUILD_ID, thread_id=THREAD_ID, parent_id=PANEL_ID
    )

    await _cog(world).on_raw_thread_delete(payload)

    close_calls = [c for c in world.pool.calls if "SET status = 'closed'" in c[0]]
    assert close_calls and close_calls[0][1][1] is None
    embed = world.channel.sends[0][1]["embed"]
    values = {field.name: field.value for field in embed.fields}
    assert values["Ended by"] == "The thread was deleted."
    assert "history" not in world.journal


async def test_a_deleted_non_ticket_thread_costs_no_query():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    payload = types.SimpleNamespace(
        guild_id=GUILD_ID, thread_id=THREAD_ID, parent_id=987654
    )

    await _cog(world).on_raw_thread_delete(payload)

    assert world.pool.calls == []


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _sweep_row(**over):
    row = {
        "id": 10,
        "guild_id": GUILD_ID,
        "ticket_number": 7,
        "thread_id": THREAD_ID,
        "opener_id": OPENER_ID,
        "claimed_by": None,
        "opened_at": LONG_AGO,
    }
    row.update(over)
    return row


async def test_one_pass_issues_exactly_one_bounded_query():
    _configured()
    world = _world(guild_threads=(), pool_kwargs={"sweep": []})

    await _cog(world).run_sweep_once()

    assert len(world.pool.calls) == 1
    query, args = world.pool.calls[0]
    assert "ORDER BY id LIMIT $3" in query
    assert args == (0, guild_config.MIN_INACTIVITY_HOURS, lifecycle.SWEEP_BATCH)


async def test_a_thread_still_in_the_active_cache_is_left_alone():
    """A cache HIT proves the room is live: archived threads are not retained."""
    _configured()
    world = _world(pool_kwargs={"sweep": [_sweep_row()], "by_thread": _row()})

    closed = await _cog(world).run_sweep_once()

    assert closed == 0
    assert not [c for c in world.pool.calls if "SET status = 'closed'" in c[0]]


async def test_a_missing_thread_past_the_guild_window_is_closed():
    _configured()
    world = _world(guild_threads=(),
                   pool_kwargs={"sweep": [_sweep_row()], "by_thread": _row()})

    closed = await _cog(world).run_sweep_once()

    assert closed == 1
    close_calls = [c for c in world.pool.calls if "SET status = 'closed'" in c[0]]
    assert close_calls[0][1] == (THREAD_ID, None)
    # No thread object means no history fetch: the sweep never fetches.
    assert "history" not in world.journal


async def test_a_missing_thread_inside_the_guild_window_waits():
    """A cache miss alone is never enough - a partial gateway state must be safe."""
    _configured(**{guild_config.KEY_INACTIVITY_HOURS: 168})
    fresh = _sweep_row(opened_at=discord.utils.utcnow() - datetime.timedelta(hours=2))
    world = _world(guild_threads=(), pool_kwargs={"sweep": [fresh]})

    assert await _cog(world).run_sweep_once() == 0


async def test_a_guild_that_is_only_a_ready_stub_is_never_swept():
    """A re-IDENTIFY looks exactly like fifty dead tickets, and is not one.

    ``parse_ready`` clears the guild cache and re-adds every guild as an
    unavailable, thread-less stub while ``is_ready()`` is still true from the
    previous connection - so every live ticket would read as unreachable. The
    rows here are a month old, i.e. past every guild window: only the
    ``unavailable`` flag stands between them and an unrecoverable close.
    """
    _configured()
    rows = [_sweep_row(id=n, thread_id=n) for n in range(5)]
    world = _world(guild_threads=(), pool_kwargs={"sweep": rows, "by_thread": _row()})
    world.guild.unavailable = True

    assert await _cog(world).run_sweep_once() == 0
    assert not [c for c in world.pool.calls if "SET status = 'closed'" in c[0]]


async def test_a_guild_this_process_cannot_see_is_skipped():
    _configured()
    world = _world(guild_threads=(), pool_kwargs={"sweep": [_sweep_row(guild_id=1)]})

    assert await _cog(world).run_sweep_once() == 0


async def test_the_cursor_advances_on_a_full_batch_and_wraps_on_a_short_one():
    _configured()
    rows = [_sweep_row(id=n, thread_id=n) for n in range(lifecycle.SWEEP_BATCH)]
    world = _world(guild_threads=(), pool_kwargs={"sweep": rows})
    cog = _cog(world)

    await cog.run_sweep_once()
    assert cog._sweep_cursor == rows[-1]["id"]

    world.pool.sweep = [_sweep_row(id=999)]
    await cog.run_sweep_once()
    assert cog._sweep_cursor == 0


async def test_one_bad_row_does_not_abort_the_pass():
    _configured()
    good = _sweep_row(id=11, thread_id=THREAD_ID)
    bad = _sweep_row(id=12, thread_id=None)  # int(None) raises inside the row
    world = _world(guild_threads=(), pool_kwargs={"sweep": [bad, good]})

    assert await _cog(world).run_sweep_once() == 1


async def test_the_sweep_does_nothing_before_the_bot_is_ready():
    _configured()
    world = _world(guild_threads=(), pool_kwargs={"sweep": [_sweep_row()]})
    world.bot._ready = False

    assert await _cog(world).run_sweep_once() == 0
    assert world.pool.calls == []


def test_the_sweep_is_hourly_and_is_the_only_clock_in_the_feature():
    assert lifecycle.SWEEP_INTERVAL_HOURS == 1
    assert lifecycle.SWEEP_BATCH == 50
    assert lifecycle.TicketLifecycle.sweep_stale_tickets.hours == 1


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_the_cog_registers_both_dynamic_items_and_drops_them_on_unload():
    world = _world()
    cog = _cog(world)
    cog.sweep_stale_tickets = types.SimpleNamespace(cancel=lambda: None)

    await cog.cog_load()
    assert world.bot.dynamic == [
        lifecycle.TicketClaimButton, lifecycle.TicketCloseButton
    ]

    cog.cog_unload()
    assert world.bot.removed == [
        lifecycle.TicketClaimButton, lifecycle.TicketCloseButton
    ]


class _MessageAuthor:
    id = OPENER_ID

    def __str__(self):
        return "Kira"


def _message(content):
    return types.SimpleNamespace(
        created_at=NOW,
        author=_MessageAuthor(),
        content=content,
        attachments=[],
        embeds=[],
    )


# ---------------------------------------------------------------------------
# The seam into open.py
# ---------------------------------------------------------------------------


async def test_the_opening_message_carries_the_controls_for_its_own_thread():
    """The one line open.py contributes to the lifecycle, asserted end to end."""
    _configured()
    world = _world()
    opener = _Member(OPENER_ID, "Kira")

    await ticket_open._post_opening_message(
        world.guild, world.thread, opener, "my subject", 7, world.pool
    )

    _args, kwargs = world.thread.sends[0]
    view = kwargs["view"]
    assert isinstance(view, lifecycle.TicketControlsView)
    assert {child.item.custom_id for child in view.children} == {
        "tk:claim:{0}".format(THREAD_ID),
        "tk:close:{0}".format(THREAD_ID),
    }


# ---------------------------------------------------------------------------
# Confirm to close, end to end
# ---------------------------------------------------------------------------


async def test_confirming_the_prompt_actually_runs_the_close():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    opener = _Member(OPENER_ID, "Kira")
    opener.guild = world.guild
    interaction = _Interaction(world.guild, opener, world.bot, channel=world.thread)
    view = lifecycle._CloseConfirmView(OPENER_ID, THREAD_ID, 7)

    await view._confirm(interaction)

    # The click was answered first (the prompt loses its buttons), then the work.
    assert interaction.edits[0]["view"] is None
    assert world.journal == [
        "db-read", "history", "db-close", "thread-send", "thread-edit", "log-send"
    ]
    assert "#7" in interaction.replies[-1]


async def test_cancelling_the_prompt_leaves_the_ticket_alone():
    _configured()
    world = _world(pool_kwargs={"by_thread": _row()})
    opener = _Member(OPENER_ID, "Kira")
    interaction = _Interaction(world.guild, opener, world.bot, channel=world.thread)
    view = lifecycle._CloseConfirmView(OPENER_ID, THREAD_ID, 7)

    await view._cancel(interaction)

    assert world.pool.calls == []
    assert "stays open" in interaction.edits[0]["content"]
