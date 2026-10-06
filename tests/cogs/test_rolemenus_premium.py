"""Role menus under the premium resolver (M4a-3).

Three things exercised here, all pure/in-memory (no DB, no real Discord):

* ``/rolemenu``'s creation cap reads the EFFECTIVE max (FREE 25, Yasuho+ 50 -
  the plan's own numbers), resolved defensively, with the refusal message
  showing that effective max.
* ``RoleMenus.is_menu_archived`` - the guild-scoped, zero-query classification
  a component click runs - archives the NEWEST menus past the cap (oldest
  message ids survive), and tracks cache mutation on create/delete.
* ``RoleMenuSelect.callback``: an archived menu refuses new grants but still
  lets a member shed a role they already hold through it.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import types
from unittest import mock

import discord
import pytest

from cogs.config.rolemenus import RoleMenus, RoleMenuSelect
from tools import premium

# ---------------------------------------------------------------------------
# Resolver stand-ins
# ---------------------------------------------------------------------------


class _Resolver:
    """A minimal stand-in for ``tools.premium.EntitlementCache``."""

    def __init__(self, limits):
        self._limits = limits

    def for_guild(self, guild_id):
        return self._limits


class _RaisingResolver:
    def for_guild(self, guild_id):
        raise RuntimeError("boom")


def _bot(pool, premium_resolver=None):
    return types.SimpleNamespace(db_pool=pool, premium=premium_resolver)


# ---------------------------------------------------------------------------
# effective_max_menus / resolver wiring
# ---------------------------------------------------------------------------


def test_effective_max_menus_is_free_with_no_premium_attribute(fake_pool):
    cog = RoleMenus(types.SimpleNamespace(db_pool=fake_pool))
    assert cog.effective_max_menus(1) == premium.FREE_MAX_MENUS_PER_GUILD


def test_effective_max_menus_is_free_when_the_resolver_raises(fake_pool):
    cog = RoleMenus(_bot(fake_pool, _RaisingResolver()))
    assert cog.effective_max_menus(1) == premium.FREE_MAX_MENUS_PER_GUILD


def test_effective_max_menus_is_premium_for_a_premium_guild(fake_pool):
    cog = RoleMenus(_bot(fake_pool, _Resolver(premium.GUILD_PREMIUM)))
    assert cog.effective_max_menus(1) == premium.GUILD_PREMIUM.max_menus_per_guild
    assert cog.effective_max_menus(1) == 50


async def test_rolemenu_command_refuses_at_the_free_cap_and_names_it(fake_pool):
    fake_pool.fetchval_return = premium.FREE_MAX_MENUS_PER_GUILD
    cog = RoleMenus(_bot(fake_pool))
    ctx = types.SimpleNamespace(
        guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=2)
    )
    sent = []

    async def _send(*args, **kwargs):
        sent.append(args[0] if args else None)

    ctx.send = _send

    await cog.rolemenu.callback(cog, ctx)

    assert len(sent) == 1
    assert str(premium.FREE_MAX_MENUS_PER_GUILD) in sent[0]


async def test_premium_guild_is_not_capped_at_the_free_count(fake_pool):
    """The same count that refuses a FREE guild must NOT refuse a premium one
    - the resolver wiring, not just the message, has to change behaviour.
    Exercises the exact guard ``rolemenu`` runs (``count >= max_menus``)
    without paying for the full Components V2 builder construction."""
    fake_pool.fetchval_return = premium.FREE_MAX_MENUS_PER_GUILD
    cog = RoleMenus(_bot(fake_pool, _Resolver(premium.GUILD_PREMIUM)))

    count = await cog._menu_count(1)
    max_menus = cog.effective_max_menus(1)

    assert count == premium.FREE_MAX_MENUS_PER_GUILD
    assert max_menus == premium.GUILD_PREMIUM.max_menus_per_guild
    assert count < max_menus  # NOT capped, unlike a FREE guild at this count


async def test_rolemenu_command_refuses_at_the_premium_cap_and_names_it(fake_pool):
    fake_pool.fetchval_return = premium.GUILD_PREMIUM.max_menus_per_guild
    cog = RoleMenus(_bot(fake_pool, _Resolver(premium.GUILD_PREMIUM)))
    ctx = types.SimpleNamespace(
        guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=2)
    )
    sent = []

    async def _send(*args, **kwargs):
        sent.append(args[0] if args else None)

    ctx.send = _send

    await cog.rolemenu.callback(cog, ctx)

    assert len(sent) == 1
    assert str(premium.GUILD_PREMIUM.max_menus_per_guild) in sent[0]


# ---------------------------------------------------------------------------
# is_menu_archived: ordering + classification
# ---------------------------------------------------------------------------


def test_archival_keeps_the_oldest_message_ids_active(fake_pool):
    """Message ids are Discord snowflakes - strictly increasing with creation
    time - so the SMALLEST ids are the OLDEST menus, which must survive a cap
    of 2 while the newest is archived."""
    cog = RoleMenus(_bot(fake_pool))
    cog._guild_menus[1] = {100, 200, 300}

    with mock.patch.object(cog, "effective_max_menus", return_value=2):
        assert cog.is_menu_archived(1, 100) is False
        assert cog.is_menu_archived(1, 200) is False
        assert cog.is_menu_archived(1, 300) is True


def test_archival_resolves_nothing_archived_for_a_premium_guild(fake_pool):
    cog = RoleMenus(_bot(fake_pool, _Resolver(premium.GUILD_PREMIUM)))
    cog._guild_menus[1] = {100, 200, 300}

    assert cog.is_menu_archived(1, 300) is False


def test_archival_is_guild_scoped_a_different_guilds_menus_do_not_leak(fake_pool):
    """The reaction-role cross-tenant gotcha, guarded against here: a menu id
    only ever classifies against ITS OWN guild's list."""
    cog = RoleMenus(_bot(fake_pool))
    cog._guild_menus[1] = {100, 200, 300}
    cog._guild_menus[2] = {999}

    with mock.patch.object(cog, "effective_max_menus", return_value=2):
        # id 999 is the only menu of guild 2, so it is never archived there -
        # even though it would be "the newest of 4" if the caches were merged.
        assert cog.is_menu_archived(2, 999) is False


def test_an_id_the_cache_never_saw_is_not_archived(fake_pool):
    """Fail OPEN on an unknown id (module docstring): a bug elsewhere must
    never turn into a trapped grant on a menu this cog cannot even see."""
    cog = RoleMenus(_bot(fake_pool))
    cog._guild_menus[1] = {100}
    assert cog.is_menu_archived(1, 12345) is False


# --- Negative control: without the slice, the oldest id would archive too ---


def test_negative_control_without_kept_first_ordering_the_oldest_would_archive():
    """NEGATIVE CONTROL for test_archival_keeps_the_oldest_message_ids_active:
    classifying in DESCENDING order (newest-first "kept") would archive the
    OLDEST id instead - proving the test actually distinguishes the two
    orderings rather than passing regardless. Hand-verified during this lot by
    editing tools.premium_archive.classify's sort key to sort DESCENDING and
    re-running the test above, which went red (100 was reported archived); the
    file was restored immediately after by editing it back (never git
    stash/checkout/reset) and the full suite re-run green."""
    from tools.premium_archive import classify

    resources = [{"id": mid, "created_at": mid} for mid in (100, 200, 300)]
    # The REAL (ascending/oldest-first) order: 100 and 200 survive a cap of 2.
    result = classify(resources, 2)
    assert result.is_active(100) is True
    assert result.is_active(300) is False


# ---------------------------------------------------------------------------
# cache bookkeeping: cog_load / store_menu / on_raw_message_delete
# ---------------------------------------------------------------------------


async def test_cog_load_populates_the_guild_scoped_cache(fake_pool):
    fake_pool.fetch_return = [
        {"message_id": 1, "guild_id": 10, "config": {"options": []}},
        {"message_id": 2, "guild_id": 10, "config": {"options": []}},
        {"message_id": 3, "guild_id": 20, "config": {"options": []}},
    ]
    bot = types.SimpleNamespace(db_pool=fake_pool, add_view=lambda *a, **k: None)
    cog = RoleMenus(bot)

    await cog.cog_load()

    assert cog._guild_menus[10] == {1, 2}
    assert cog._guild_menus[20] == {3}


async def test_store_menu_adds_to_the_guild_scoped_cache(fake_pool):
    cog = RoleMenus(_bot(fake_pool))
    await cog.store_menu(42, 10, 99, {"options": []})
    assert cog._guild_menus[10] == {42}


async def test_on_raw_message_delete_discards_from_the_guild_scoped_cache(fake_pool):
    cog = RoleMenus(_bot(fake_pool))
    cog._menu_ids = {42}
    cog._guild_menus[10] = {42}
    payload = types.SimpleNamespace(message_id=42, guild_id=10)

    await cog.on_raw_message_delete(payload)

    assert cog._guild_menus[10] == set()
    assert 42 not in cog._menu_ids


async def test_on_raw_bulk_message_delete_discards_every_menu_in_the_batch(fake_pool):
    # Regression: discord.py fires on_raw_bulk_message_delete (not a run of
    # individual on_raw_message_delete calls) for a purge. Without this
    # listener a menu caught in a bulk delete stayed "live" in the cache
    # forever, corrupting is_menu_archived's count for every other menu of
    # the same guild.
    cog = RoleMenus(_bot(fake_pool))
    cog._menu_ids = {41, 42, 43}
    cog._guild_menus[10] = {41, 42, 43}
    payload = types.SimpleNamespace(message_ids={42, 43, 999}, guild_id=10)

    await cog.on_raw_bulk_message_delete(payload)

    assert cog._guild_menus[10] == {41}
    assert cog._menu_ids == {41}
    [(method, query, args)] = fake_pool.calls
    assert method == "execute"
    assert sorted(args[0]) == [42, 43]


async def test_on_raw_bulk_message_delete_is_a_no_op_with_no_known_menus(fake_pool):
    cog = RoleMenus(_bot(fake_pool))
    cog._menu_ids = {41}
    cog._guild_menus[10] = {41}
    payload = types.SimpleNamespace(message_ids={777, 888}, guild_id=10)

    await cog.on_raw_bulk_message_delete(payload)

    assert cog._guild_menus[10] == {41}
    assert cog._menu_ids == {41}
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# RoleMenuSelect.callback: archived -> grant refused, removal still allowed
# ---------------------------------------------------------------------------


class _Role:
    def __init__(self, role_id, position=1, managed=False):
        self.id = role_id
        self.position = position
        self.managed = managed
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other):
        return self.position >= other.position


def _fake_member(roles, add_log, remove_log):
    """A stand-in that passes ``isinstance(member, discord.Member)`` - the
    callback guards on that, so a plain SimpleNamespace would take the wrong
    branch and prove nothing (the house pattern from
    tests/cogs/test_interaction_deadline_defers.py)."""
    member = mock.MagicMock(spec=discord.Member)
    member.id = 7
    member.roles = roles

    async def _add(role, **_kwargs):
        add_log.append(role.id)

    async def _remove(role, **_kwargs):
        remove_log.append(role.id)

    member.add_roles = _add
    member.remove_roles = _remove
    return member


def _select(message_id, config, values):
    select = RoleMenuSelect.__new__(RoleMenuSelect)
    select.message_id = message_id
    select.config = config
    select._test_values = values
    return select


@pytest.fixture
def patched_select_values(monkeypatch):
    monkeypatch.setattr(
        discord.ui.Select,
        "values",
        property(lambda self: getattr(self, "_test_values", [])),
        raising=False,
    )
    yield


def _interaction(rolemenus_cog, guild, member):
    sent = []
    followups = []

    class _Response:
        def is_done(self):
            return False

        async def send_message(self, *args, **kwargs):
            sent.append((args, kwargs))

        async def defer(self, *args, **kwargs):
            pass

    class _Followup:
        async def send(self, *args, **kwargs):
            followups.append((args, kwargs))

    return types.SimpleNamespace(
        extras={},
        locale="en",
        guild=guild,
        guild_id=guild.id,
        user=member,
        message=None,
        client=types.SimpleNamespace(get_cog=lambda name: rolemenus_cog),
        response=_Response(),
        followup=_Followup(),
        sent=sent,
        followups=followups,
    )


def _guild(bot_top_position=50):
    colour = _Role(10)
    ping = _Role(20)
    bot_top = _Role(99, position=bot_top_position)
    by_id = {r.id: r for r in (colour, ping, bot_top)}
    return types.SimpleNamespace(
        id=1, me=types.SimpleNamespace(top_role=bot_top), get_role=by_id.get
    )


async def test_archived_menu_refuses_a_new_grant(fake_pool, patched_select_values):
    cog = RoleMenus(_bot(fake_pool))
    cog._guild_menus[1] = {500}  # just this one menu on record for guild 1

    guild = _guild()
    add_log, remove_log = [], []
    member = _fake_member([], add_log, remove_log)
    select = _select(
        500,
        {"options": [{"role_id": 10}, {"role_id": 20}], "exclusive": False},
        ["10"],
    )
    interaction = _interaction(cog, guild, member)

    with mock.patch.object(cog, "effective_max_menus", return_value=0):
        await RoleMenuSelect.callback(select, interaction)

    assert add_log == []  # nothing granted
    assert remove_log == []  # nothing held, nothing to remove
    assert len(interaction.followups) == 1
    body = interaction.followups[0][0][0]
    assert "archived" in body
    assert "/premium" in body


async def test_archived_menu_still_allows_removing_a_held_role(
    fake_pool, patched_select_values
):
    """The protective asymmetry: a role the member ALREADY holds through an
    archived menu can still be shed, even though no new grant can land."""
    cog = RoleMenus(_bot(fake_pool))
    cog._guild_menus[1] = {500}

    guild = _guild()
    held_role = _Role(20)
    add_log, remove_log = [], []
    member = _fake_member([held_role], add_log, remove_log)
    # Deselecting the held role (20) while NOT picking the other (10): an
    # "any" menu with nothing selected removes every held menu role.
    select = _select(
        500,
        {"options": [{"role_id": 10}, {"role_id": 20}], "exclusive": False},
        [],
    )
    interaction = _interaction(cog, guild, member)

    with mock.patch.object(cog, "effective_max_menus", return_value=0):
        await RoleMenuSelect.callback(select, interaction)

    assert remove_log == [20]  # the removal went through
    assert add_log == []


async def test_a_non_archived_menu_grants_normally(fake_pool, patched_select_values):
    """Counter-test: with the guild under its cap, a pick grants as normal -
    proving the block above is conditional on archival, not unconditional."""
    cog = RoleMenus(_bot(fake_pool))
    cog._guild_menus[1] = {500}

    guild = _guild()
    add_log, remove_log = [], []
    member = _fake_member([], add_log, remove_log)
    select = _select(
        500,
        {"options": [{"role_id": 10}, {"role_id": 20}], "exclusive": False},
        ["10"],
    )
    interaction = _interaction(cog, guild, member)

    with mock.patch.object(cog, "effective_max_menus", return_value=50):
        await RoleMenuSelect.callback(select, interaction)

    assert add_log == [10]
    assert not any("archived" in f[0][0][0] for f in [interaction.followups] if f)


async def test_delete_removes_the_menu_from_the_archival_cache(fake_pool):
    """Deletion carries no cap check (still deletable), and it also retires
    the id from the archival cache - a deleted menu can never block itself
    from being re-created fresh."""
    cog = RoleMenus(_bot(fake_pool))
    cog._menu_ids = {500}
    cog._guild_menus[1] = {500}

    await cog.on_raw_message_delete(
        types.SimpleNamespace(message_id=500, guild_id=1)
    )

    assert cog.is_menu_archived(1, 500) is False  # unseen id -> fail open
    assert 500 not in cog._guild_menus[1]
