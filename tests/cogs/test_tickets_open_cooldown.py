"""How fast a member may cycle tickets, and why there has to be a limit.

THE BUG THIS PINS. Opening a ticket had a CAP but no RATE. The cap bounds how
many tickets a member holds at once; it says nothing about how fast they may
open and close them, and every close renders the whole thread and UPLOADS it as
a file attachment to the staff log channel. Open, close, repeat - as fast as
Discord's own rate limits allow - is a file-upload primitive aimed at a channel
the staff cannot clear faster than a script can fill it, and each turn also
burns a ticket number, a thread creation and up to ten pages of history reads.

The three properties tested here are the ones that make the limit both real and
harmless: it is checked at the CLICK and again at the SUBMIT (so a queued modal
cannot walk past it), it is armed only by a ticket that really opened (so a
refusal costs the next opener nothing), and it is per member per guild (so one
server's abuser cannot lock a member out of another server's help desk).
"""

import types

import discord
import pytest

from cogs.config.tickets import guild_config, preflight
from cogs.config.tickets import open as ticket_open
from tools import settings

GUILD_ID = 31337
OTHER_GUILD_ID = 40404
CHANNEL_ID = 555000111
MEMBER_ID = 42
OTHER_MEMBER_ID = 43


@pytest.fixture(autouse=True)
def _isolate_module_state():
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
# Fakes (the flow asserts real types, so the Discord ones are subclassed)
# ---------------------------------------------------------------------------


class _Member(discord.Member):
    def __init__(self, user_id=MEMBER_ID, name="Kira"):
        self._user = types.SimpleNamespace(id=user_id, name=name)
        self._display = name

    def __str__(self):
        return self._display


class _Thread(discord.Thread):
    def __init__(self, thread_id=7001):
        self.id = thread_id
        self.deleted = False
        self.sends = []

    async def edit(self, **kwargs):
        return self

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))

    async def delete(self, **kwargs):
        self.deleted = True


class _Perms:
    def __init__(self, **granted):
        for name in preflight.SETUP_PERMISSIONS:
            setattr(self, name, granted.get(name, True))


class _TextChannel(discord.TextChannel):
    def __init__(self, channel_id=CHANNEL_ID):
        self.id = channel_id
        self._perms = _Perms()
        self.threads_created = 0
        self.create_error = None

    def permissions_for(self, obj):
        return self._perms

    async def create_thread(self, **kwargs):
        self.threads_created += 1
        if self.create_error is not None:
            raise self.create_error
        return _Thread(7000 + self.threads_created)

    async def send(self, *args, **kwargs):
        return types.SimpleNamespace(jump_url="https://discord.test/panel")


class _Guild:
    def __init__(self, channels=(), guild_id=GUILD_ID):
        self.id = guild_id
        self.name = "Server"
        self.me = object()
        self._channels = {c.id: c for c in channels}

    def get_channel(self, ident):
        return self._channels.get(ident)

    def get_role(self, ident):
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

    async def defer(self, *args, **kwargs):
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
        self.followups = []
        self.modals = []
        self.response = _Response(self)
        self.followup = _Followup(self)

    @property
    def replies(self):
        return [args[0] for args, _kw in self.sent + self.followups if args]


class _Bot:
    def __init__(self, pool):
        self.db_pool = pool
        self.blacklist = set()


class _Pool:
    """Answers the two statements the open flow issues: the count and the insert.

    ``ticket_number=None`` is the guarded INSERT declining - the member was at
    the cap after all - which is the refusal the flow compensates for by
    deleting the thread it had already created.
    """

    def __init__(self, open_count=0, ticket_number=1):
        self.open_count = open_count
        self.ticket_number = ticket_number
        self.inserts = 0

    async def fetchval(self, query, *args):
        return self.open_count

    async def fetchrow(self, query, *args):
        self.inserts += 1
        if self.ticket_number is None:
            return None
        return {"ticket_number": self.ticket_number}

    async def execute(self, query, *args):
        return "UPDATE 1"


def _context(pool, *, guild_id=GUILD_ID, member_id=MEMBER_ID):
    channel = _TextChannel()
    guild = _Guild(channels=[channel], guild_id=guild_id)
    interaction = _Interaction(guild, _Member(member_id), _Bot(pool))
    return interaction, channel


async def _open(pool, *, guild_id=GUILD_ID, member_id=MEMBER_ID, subject="printer on fire"):
    interaction, channel = _context(pool, guild_id=guild_id, member_id=member_id)
    await ticket_open._create_ticket(interaction, subject)
    return interaction, channel


# ---------------------------------------------------------------------------
# The rate limit
# ---------------------------------------------------------------------------


async def test_a_second_ticket_straight_after_the_first_is_refused():
    """THE regression: open + close in a loop is a file-upload primitive
    pointed at the staff log channel."""
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()

    first, channel_a = await _open(pool)
    second, channel_b = await _open(pool)

    assert channel_a.threads_created == 1
    assert channel_b.threads_created == 0  # no thread, no transcript, no upload
    assert "wait a moment" in second.replies[0]
    assert pool.inserts == 1


async def test_the_click_is_refused_before_any_database_work():
    """A member cycling the button pays a dict lookup per click, not a COUNT."""
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    await _open(pool)

    class _Explodes(_Pool):
        async def fetchval(self, query, *args):
            raise AssertionError("the cooldown must be answered before this")

    interaction, _channel = _context(_Explodes())
    await ticket_open.TicketOpenButton().callback(interaction)

    assert interaction.modals == []
    assert "wait a moment" in interaction.replies[0]


async def test_a_modal_held_open_cannot_walk_past_the_cooldown():
    """The click check alone is not enough: a member can hold a modal and submit
    it later, so the submit is where the limit actually holds."""
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()

    # Two clicks BEFORE anything opened: both legitimately get a modal.
    first_click, _c = _context(pool)
    await ticket_open.TicketOpenButton().callback(first_click)
    second_click, _c2 = _context(pool)
    await ticket_open.TicketOpenButton().callback(second_click)
    assert len(first_click.modals) == 1 and len(second_click.modals) == 1

    await _open(pool)  # the first modal is submitted and opens a ticket
    _second, channel = await _open(pool)  # the one that was held open

    assert channel.threads_created == 0


async def test_an_open_that_never_happened_does_not_start_the_clock():
    """A refusal costs nothing: the cap, a permission problem or a Discord error
    must not make the member wait for a ticket they never got."""
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    # The cap guard refuses the row after the thread already exists, which the
    # flow compensates for by deleting the thread.
    refusing = _Pool(ticket_number=None)

    refused, channel = await _open(refusing)
    assert channel.threads_created == 1
    assert "already have" in refused.replies[0]

    accepted, channel_two = await _open(_Pool())
    assert channel_two.threads_created == 1  # not made to wait


async def test_a_thread_that_could_not_be_created_does_not_start_the_clock():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    interaction, channel = _context(_Pool())
    channel.create_error = discord.HTTPException(
        types.SimpleNamespace(status=500, reason="test"), "nope"
    )

    await ticket_open._create_ticket(interaction, "hi")
    _second, channel_two = await _open(_Pool())

    assert channel_two.threads_created == 1


async def test_the_cooldown_is_per_member_and_per_guild():
    """One server's abuser must not be able to lock somebody else - or himself
    in another server - out of a help desk."""
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID}, guild_id=OTHER_GUILD_ID)
    pool = _Pool()

    await _open(pool)

    _other_member, channel_a = await _open(pool, member_id=OTHER_MEMBER_ID)
    _other_guild, channel_b = await _open(pool, guild_id=OTHER_GUILD_ID)

    assert channel_a.threads_created == 1
    assert channel_b.threads_created == 1


async def test_the_wait_is_short_enough_to_be_invisible_to_a_person():
    """A limit nobody can live with gets removed. One minute is the deal: it
    costs a genuine second ticket one wait and costs a loop two orders of
    magnitude of throughput."""
    assert 30 <= ticket_open.OPEN_COOLDOWN_SECONDS <= 120


async def test_the_cooldown_lets_the_member_back_in_when_it_expires():
    _seed({guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID})
    pool = _Pool()
    await _open(pool)

    # Fast-forward the debounce rather than sleeping through it.
    ticket_open._OPEN_COOLDOWNS._seen.clear()
    _again, channel = await _open(pool)

    assert channel.threads_created == 1
