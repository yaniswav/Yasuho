"""Support tickets under the premium resolver (M4a-3).

``FREE_MAX_TICKETS_OPEN_PER_USER`` (5) is the admin-configurable HARD CEILING,
not the bot default (2): Yasuho+ raises that ceiling to 10
(``tools.premium.GUILD_PREMIUM.max_tickets_open_per_user``), which is what an
admin's own ``tickets_max_open_per_user`` setting is clamped into
(``guild_config.max_open_per_user``'s ``ceiling`` parameter, and
``guild_config.resolve``'s), not a value that bumps a guild's open count by
itself.

Covered here: the ceiling resolution (pure), the click-time courtesy
pre-check's refusal message (``TicketOpenButton.callback``), and the
structural guarantee that closing a ticket never consults any cap at all -
``storage.close_ticket`` takes no count parameter, so an existing ticket
opened before a downgrade stays closeable no matter how far over the new cap
the member now sits.

No real Discord, no real DB: the fakes are the same house pattern
tests/cogs/test_tickets_open_cooldown.py already uses (Discord-type
subclasses so the flow's own ``isinstance`` checks are the real ones).

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import types

import discord
import pytest

from cogs.config.tickets import guild_config, storage
from cogs.config.tickets import open as ticket_open
from tools import premium, premium_upsell, settings

GUILD_ID = 51515
CHANNEL_ID = 606060
MEMBER_ID = 9


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
# Fakes
# ---------------------------------------------------------------------------


class _Member(discord.Member):
    def __init__(self, user_id=MEMBER_ID, name="Kira"):
        self._user = types.SimpleNamespace(id=user_id, name=name)

    def __str__(self):
        return "Kira"


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


class _Perms:
    def __init__(self):
        from cogs.config.tickets import preflight

        for name in preflight.SETUP_PERMISSIONS:
            setattr(self, name, True)


class _TextChannel(discord.TextChannel):
    def __init__(self, channel_id=CHANNEL_ID):
        self.id = channel_id
        self._perms = _Perms()

    def permissions_for(self, obj):
        return self._perms


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


class _Resolver:
    def __init__(self, limits):
        self._limits = limits

    def for_guild(self, guild_id):
        return self._limits


class _RaisingResolver:
    def for_guild(self, guild_id):
        raise RuntimeError("boom")


class _Bot:
    def __init__(self, pool, premium_resolver=None):
        self.db_pool = pool
        self.blacklist = set()
        self.premium = premium_resolver


class _CountPool:
    """Answers only the open-count read the click's courtesy pre-check runs."""

    def __init__(self, open_count):
        self.open_count = open_count

    async def fetchval(self, query, *args):
        return self.open_count


def _click(pool, *, open_count, premium_resolver=None, guild_id=GUILD_ID, member_id=MEMBER_ID):
    channel = _TextChannel()
    guild = _Guild(channels=[channel], guild_id=guild_id)
    bot = _Bot(_CountPool(open_count), premium_resolver=premium_resolver)
    return _Interaction(guild, _Member(member_id), bot)


class _DualCountPool:
    """Answers the click's TWO open-count reads independently: the per-member
    one (``storage.count_open_for_user``, whose query names ``opener_id``)
    and the per-SERVER one (``storage.count_open_for_guild``, which does
    not)."""

    def __init__(self, *, member_count, guild_count):
        self.member_count = member_count
        self.guild_count = guild_count

    async def fetchval(self, query, *args):
        return self.member_count if "opener_id" in query else self.guild_count


def _click_with_counts(
    *,
    member_count,
    guild_count,
    premium_resolver=None,
    guild_id=GUILD_ID,
    member_id=MEMBER_ID,
):
    channel = _TextChannel()
    guild = _Guild(channels=[channel], guild_id=guild_id)
    bot = _Bot(
        _DualCountPool(member_count=member_count, guild_count=guild_count),
        premium_resolver=premium_resolver,
    )
    return _Interaction(guild, _Member(member_id), bot)


# ---------------------------------------------------------------------------
# guild_config.max_open_per_user / resolve: ceiling resolution
# ---------------------------------------------------------------------------


def test_max_open_per_user_defaults_to_the_free_ceiling_with_no_ceiling_arg():
    """Every existing caller (no ``ceiling=``) keeps today's FREE clamp."""
    assert guild_config.MAX_OPEN_PER_USER == premium.FREE_MAX_TICKETS_OPEN_PER_USER


async def test_max_open_per_user_clamps_to_the_passed_ceiling(fake_pool):
    # settings.get_guild reads through tools.settings's cache - seed it
    # directly, the same way _seed() does for the open flow below.
    settings._cache[("guild_settings", 1)] = {guild_config.KEY_MAX_OPEN_PER_USER: 9}

    assert await guild_config.max_open_per_user(fake_pool, 1, ceiling=10) == 9
    assert await guild_config.max_open_per_user(fake_pool, 1, ceiling=5) == 5


def test_resolve_clamps_max_open_to_the_passed_ceiling():
    raw = {guild_config.KEY_MAX_OPEN_PER_USER: 9}
    assert guild_config.resolve(raw, ceiling=10)["max_open"] == 9
    assert guild_config.resolve(raw, ceiling=5)["max_open"] == 5
    # No ceiling passed: the FREE default, byte-identical to today.
    assert guild_config.resolve(raw)["max_open"] == 5


# ---------------------------------------------------------------------------
# The click's courtesy pre-check: refusal names the EFFECTIVE cap
#
# The admin's OWN setting (``tickets_max_open_per_user``) is what actually
# gets clamped - the ceiling only widens the RANGE it can be clamped into
# (see guild_config.max_open_per_user's own docstring). ADMIN_SET below (8)
# sits strictly between the FREE ceiling (5) and the premium one (10), so
# these tests show the ceiling actually changing the resolved cap rather than
# a guild that never configured anything (which would stay at the bot
# default, 2, on both tiers and prove nothing about the ceiling at all).
# ---------------------------------------------------------------------------

ADMIN_SET = 8
assert premium.FREE_MAX_TICKETS_OPEN_PER_USER < ADMIN_SET < premium.GUILD_PREMIUM.max_tickets_open_per_user


def _seed_with_admin_cap(admin_set=ADMIN_SET):
    _seed(
        {
            guild_config.KEY_PANEL_CHANNEL: CHANNEL_ID,
            guild_config.KEY_MAX_OPEN_PER_USER: admin_set,
        }
    )


async def test_click_is_refused_at_the_free_ceiling_and_names_it():
    """The admin tried to set 8; a FREE guild clamps that down to 5."""
    _seed_with_admin_cap()
    interaction = _click(None, open_count=premium.FREE_MAX_TICKETS_OPEN_PER_USER)

    await ticket_open.TicketOpenButton().callback(interaction)

    assert interaction.modals == []
    assert str(premium.FREE_MAX_TICKETS_OPEN_PER_USER) in interaction.replies[0]


async def test_click_is_not_refused_at_the_free_cap_when_premium():
    """The SAME open count that refuses a FREE guild (clamped to 5) must not
    refuse a premium one, where the same admin setting clamps to 8 instead -
    the resolver wiring has to change the actual decision, not just a
    message."""
    _seed_with_admin_cap()
    interaction = _click(
        None,
        open_count=premium.FREE_MAX_TICKETS_OPEN_PER_USER,
        premium_resolver=_Resolver(premium.GUILD_PREMIUM),
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert len(interaction.modals) == 1  # the modal opened - no refusal
    assert interaction.replies == []


async def test_click_is_refused_at_the_premium_ceilings_clamp_and_names_it():
    """Once the premium guild reaches ITS clamp (8, not 10 - the admin never
    asked for the full ceiling), the refusal names THAT number."""
    _seed_with_admin_cap()
    interaction = _click(
        None,
        open_count=ADMIN_SET,
        premium_resolver=_Resolver(premium.GUILD_PREMIUM),
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert interaction.modals == []
    assert str(ADMIN_SET) in interaction.replies[0]


async def test_click_with_a_missing_premium_attribute_resolves_the_free_ceiling():
    _seed_with_admin_cap()
    interaction = _click(
        None, open_count=premium.FREE_MAX_TICKETS_OPEN_PER_USER, premium_resolver=None
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert str(premium.FREE_MAX_TICKETS_OPEN_PER_USER) in interaction.replies[0]


async def test_click_with_a_raising_resolver_resolves_the_free_ceiling():
    _seed_with_admin_cap()
    interaction = _click(
        None,
        open_count=premium.FREE_MAX_TICKETS_OPEN_PER_USER,
        premium_resolver=_RaisingResolver(),
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert str(premium.FREE_MAX_TICKETS_OPEN_PER_USER) in interaction.replies[0]


# --- Negative control: without the ceiling wiring, a premium guild would
# still be refused at the free count -------------------------------------
#
# Verified by hand during this lot: editing open.py's courtesy pre-check to
# call ``guild_config.max_open_per_user(pool, guild.id)`` with NO ``ceiling=``
# argument (dropping the resolver wiring entirely) turned
# test_click_is_not_refused_at_the_free_cap_when_premium red - the premium
# guild was refused at 5, exactly like a free one. Restored immediately after
# by editing the file back (never git stash/checkout/reset), and the full
# ticket test suite was re-run green. See this report's "negative controls"
# section for the exact edit and the failure it produced.


# ---------------------------------------------------------------------------
# L1b: the internal, never-advertised per-SERVER ticket cap - the click's
# own courtesy pre-check (storage.open_ticket's guarded INSERT is the
# authority; tests/cogs/test_tickets_storage.py covers that statement
# directly). Anti-abuse only: no /premium mention, no premium_upsell call.
# ---------------------------------------------------------------------------


async def test_click_is_refused_at_the_server_cap_with_the_anti_abuse_message():
    """Well under the per-MEMBER cap, but the SERVER itself is full - the
    refusal names the cap, suggests waiting, and mentions no upgrade."""
    _seed_with_admin_cap()
    interaction = _click_with_counts(
        member_count=0, guild_count=premium.FREE_MAX_TICKETS_OPEN_PER_GUILD
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert interaction.modals == []
    text = interaction.replies[0]
    assert str(premium.FREE_MAX_TICKETS_OPEN_PER_GUILD) in text
    assert "wait" in text.lower()
    # Anti-abuse backstop, not a sales lever: never a /premium mention here.
    assert "premium" not in text.lower()


async def test_click_is_not_refused_below_the_server_cap():
    """FREE byte-identical up to the cap: a guild nowhere near its 50-ticket
    ceiling opens the modal exactly as it did before this lot existed."""
    _seed_with_admin_cap()
    interaction = _click_with_counts(member_count=0, guild_count=5)

    await ticket_open.TicketOpenButton().callback(interaction)

    assert len(interaction.modals) == 1
    assert interaction.replies == []


async def test_click_is_refused_at_the_premium_server_ceiling_not_the_free_one():
    """A premium guild's server cap is 200, not 50 - the SAME count (50)
    that refuses a free guild must not refuse a premium one."""
    _seed_with_admin_cap()
    interaction = _click_with_counts(
        member_count=0,
        guild_count=premium.FREE_MAX_TICKETS_OPEN_PER_GUILD,  # 50
        premium_resolver=_Resolver(premium.GUILD_PREMIUM),
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert len(interaction.modals) == 1  # not refused - premium raises to 200
    assert interaction.replies == []


async def test_server_cap_refusal_never_calls_the_premium_upsell_helper(monkeypatch):
    """Explicitly NOT wired: unlike the member cap's refusal, this one must
    never touch tools.premium_upsell at all."""
    called = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(premium_upsell, "for_guild_refusal", _fail_if_called)
    _seed_with_admin_cap()
    interaction = _click_with_counts(
        member_count=0, guild_count=premium.FREE_MAX_TICKETS_OPEN_PER_GUILD
    )

    await ticket_open.TicketOpenButton().callback(interaction)

    assert called is False
    assert interaction.modals == []  # the refusal still happened


# ---------------------------------------------------------------------------
# Close always works - structurally, not just today: storage.close_ticket
# takes no count/cap parameter at all, so an existing ticket opened before a
# downgrade stays closeable regardless of how far over the new cap it sits.
# ---------------------------------------------------------------------------


async def test_close_ticket_takes_no_cap_parameter(fake_pool):
    fake_pool.fetchrow_return = {
        "thread_id": 777,
        "status": "closed",
        "ticket_number": 3,
    }
    row = await storage.close_ticket(fake_pool, 777, closed_by=MEMBER_ID)

    assert row["status"] == "closed"
    _method, _query, args = fake_pool.calls[0]
    assert args == (777, MEMBER_ID)  # no cap/ceiling anywhere in the call
