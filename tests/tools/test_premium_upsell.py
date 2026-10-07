"""Unit tests for :mod:`tools.premium_upsell` (M5: the limit-reached message).

No network, DB, Discord, or Lavalink is touched. The 7-day claim is tested
against a small in-memory double that implements the CLAIM query's exact
semantics (max of the specific key's own row and the ``'*'`` sentinel,
compared against the cooldown) rather than mocking SQL text, so the test is
really exercising the rule, not a string match.
"""

from __future__ import annotations

import datetime
import types

import discord

from tools import premium, premium_upsell

UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# A faithful in-memory double of the premium_upsells table + the claim query.
# ---------------------------------------------------------------------------


class _FakeStore:
    """Implements exactly what ``_CLAIM_SQL``/``mark_premium_opened`` do -
    not a mock of the SQL text, a model of its semantics."""

    def __init__(self, now=None, *, raise_on_fetchrow=False, raise_on_execute=False):
        self.rows = {}  # (user_id, limit_key) -> shown_at
        self.now = now or datetime.datetime(2026, 1, 8, tzinfo=UTC)
        self._raise_fetchrow = raise_on_fetchrow
        self._raise_execute = raise_on_execute
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, user_id, limit_key, cooldown, sentinel):
        self.fetchrow_calls.append((user_id, limit_key, cooldown, sentinel))
        if self._raise_fetchrow:
            raise RuntimeError("database unavailable")
        candidates = [
            self.rows[k]
            for k in ((user_id, limit_key), (user_id, sentinel))
            if k in self.rows
        ]
        last_shown = max(candidates) if candidates else datetime.datetime.min.replace(
            tzinfo=UTC
        )
        if self.now - last_shown < cooldown:
            return None
        self.rows[(user_id, limit_key)] = self.now
        return {"shown_at": self.now}

    async def execute(self, query, user_id, sentinel):
        self.execute_calls.append((user_id, sentinel))
        if self._raise_execute:
            raise RuntimeError("database unavailable")
        self.rows[(user_id, sentinel)] = self.now


def _bot(pool, *, is_guild_premium=False, has_comfort_pack=False):
    resolver = types.SimpleNamespace(
        is_guild_premium=lambda gid: is_guild_premium,
        has_comfort_pack=lambda uid: has_comfort_pack,
    )
    return types.SimpleNamespace(db_pool=pool, premium=resolver)


# ---------------------------------------------------------------------------
# The 7-day rule per (person, key)
# ---------------------------------------------------------------------------


async def test_a_second_refusal_within_seven_days_shows_nothing():
    store = _FakeStore()
    bot = _bot(store)
    kwargs = dict(
        limit_key="guild_playlists",
        guild_id=1,
        person_id=42,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )

    first = await premium_upsell.for_guild_refusal(bot, **kwargs)
    assert first is not None

    second = await premium_upsell.for_guild_refusal(bot, **kwargs)
    assert second is None


async def test_the_same_key_shows_again_after_seven_days():
    store = _FakeStore(now=datetime.datetime(2026, 1, 1, tzinfo=UTC))
    bot = _bot(store)
    kwargs = dict(
        limit_key="guild_playlists",
        guild_id=1,
        person_id=42,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )
    assert await premium_upsell.for_guild_refusal(bot, **kwargs) is not None

    store.now = store.now + datetime.timedelta(days=7, seconds=1)
    assert await premium_upsell.for_guild_refusal(bot, **kwargs) is not None


async def test_a_different_key_for_the_same_person_is_independent():
    store = _FakeStore()
    bot = _bot(store)
    assert (
        await premium_upsell.for_guild_refusal(
            bot,
            limit_key="guild_playlists",
            guild_id=1,
            person_id=42,
            is_admin=True,
            already_top_tier=False,
            benefit="75",
        )
        is not None
    )
    # A different limit_key for the SAME person is its own 7-day slot.
    assert (
        await premium_upsell.for_guild_refusal(
            bot,
            limit_key="role_menus",
            guild_id=1,
            person_id=42,
            is_admin=True,
            already_top_tier=False,
            benefit="50",
        )
        is not None
    )


async def test_a_different_person_for_the_same_key_is_independent():
    store = _FakeStore()
    bot = _bot(store)
    kwargs = dict(
        limit_key="guild_playlists",
        guild_id=1,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )
    assert await premium_upsell.for_guild_refusal(bot, person_id=1, **kwargs) is not None
    assert await premium_upsell.for_guild_refusal(bot, person_id=2, **kwargs) is not None


# ---------------------------------------------------------------------------
# Opening /premium marks every key as seen
# ---------------------------------------------------------------------------


async def test_opening_premium_resets_every_key_not_just_one():
    store = _FakeStore()
    bot = _bot(store)

    await premium_upsell.mark_premium_opened(store, 42)

    # Two DIFFERENT limit keys this person never saw before - both suppressed
    # by the single sentinel row the open just wrote.
    assert (
        await premium_upsell.for_guild_refusal(
            bot,
            limit_key="guild_playlists",
            guild_id=1,
            person_id=42,
            is_admin=True,
            already_top_tier=False,
            benefit="75",
        )
        is None
    )
    assert (
        await premium_upsell.for_guild_refusal(
            bot,
            limit_key="role_menus",
            guild_id=1,
            person_id=42,
            is_admin=True,
            already_top_tier=False,
            benefit="50",
        )
        is None
    )
    # Exactly one row written for the open - O(1), not one per limit key.
    assert store.rows == {(42, premium_upsell.ALL_KEYS_SENTINEL): store.now}


async def test_opening_premium_does_not_reset_other_people():
    store = _FakeStore()
    bot = _bot(store)
    await premium_upsell.mark_premium_opened(store, 42)

    assert (
        await premium_upsell.for_guild_refusal(
            bot,
            limit_key="guild_playlists",
            guild_id=1,
            person_id=99,
            is_admin=True,
            already_top_tier=False,
            benefit="75",
        )
        is not None
    )


# ---------------------------------------------------------------------------
# No upsell when already at the top tier
# ---------------------------------------------------------------------------


async def test_no_upsell_for_a_guild_already_on_yasuho_plus():
    store = _FakeStore()
    bot = _bot(store)

    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=42,
        is_admin=True,
        already_top_tier=True,
        benefit="75",
    )

    assert result is None
    # No 7-day slot was spent either - the DB was never even touched.
    assert store.fetchrow_calls == []


async def test_no_upsell_for_a_user_already_on_pack_confort():
    store = _FakeStore()
    bot = _bot(store)

    result = await premium_upsell.for_user_refusal(
        bot,
        limit_key="favourites",
        person_id=42,
        already_top_tier=True,
        benefit="300",
    )

    assert result is None
    assert store.fetchrow_calls == []


def test_is_guild_already_top_tier_reads_the_bot_resolver():
    bot = _bot(_FakeStore(), is_guild_premium=True)
    assert premium_upsell.is_guild_already_top_tier(bot, 1) is True
    bot2 = _bot(_FakeStore(), is_guild_premium=False)
    assert premium_upsell.is_guild_already_top_tier(bot2, 1) is False


def test_is_user_already_top_tier_reads_the_bot_resolver():
    bot = _bot(_FakeStore(), has_comfort_pack=True)
    assert premium_upsell.is_user_already_top_tier(bot, 1) is True


def test_top_tier_resolvers_fail_closed_to_not_premium():
    """A missing/raising resolver must never accidentally SUPPRESS an upsell
    that should have shown - "not premium" is the safe default here."""
    bot_no_premium = types.SimpleNamespace(db_pool=_FakeStore())
    assert premium_upsell.is_guild_already_top_tier(bot_no_premium, 1) is False
    assert premium_upsell.is_user_already_top_tier(bot_no_premium, 1) is False

    class _Raises:
        def is_guild_premium(self, gid):
            raise RuntimeError("boom")

        def has_comfort_pack(self, uid):
            raise RuntimeError("boom")

    bot_raises = types.SimpleNamespace(db_pool=_FakeStore(), premium=_Raises())
    assert premium_upsell.is_guild_already_top_tier(bot_raises, 1) is False
    assert premium_upsell.is_user_already_top_tier(bot_raises, 1) is False


# ---------------------------------------------------------------------------
# Admin vs member wording
# ---------------------------------------------------------------------------


async def test_admin_gets_the_raise_wording_with_the_number():
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=42,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )
    assert result.line == "Yasuho+ raises this limit to 75. See /premium."


async def test_member_gets_the_ask_an_admin_wording_with_no_number():
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=42,
        is_admin=False,
        already_top_tier=False,
        benefit="75",
    )
    assert result.line == "A server admin can raise this limit with /premium."
    assert "75" not in result.line


async def test_unlock_kind_has_its_own_wording_with_no_number():
    store = _FakeStore()
    bot = _bot(store)
    admin = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="music_247",
        guild_id=1,
        person_id=1,
        is_admin=True,
        already_top_tier=False,
        kind="unlock",
    )
    assert admin.line == "Yasuho+ unlocks this. See /premium."

    member = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="music_247",
        guild_id=1,
        person_id=2,
        is_admin=False,
        already_top_tier=False,
        kind="unlock",
    )
    assert member.line == "A server admin can unlock this with /premium."


async def test_user_scoped_refusal_has_its_own_wording_with_the_number():
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_user_refusal(
        bot,
        limit_key="favourites",
        person_id=1,
        already_top_tier=False,
        benefit="300",
    )
    assert result.line == "Pack Confort raises this limit to 300. See /premium."


# ---------------------------------------------------------------------------
# A button only with a configured SKU, and only when allowed
# ---------------------------------------------------------------------------


async def test_no_button_without_a_configured_sku(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=1,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )
    assert result.button is None
    assert result.view() is None


async def test_a_button_with_a_configured_sku_for_an_admin_on_slash(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 999)
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=1,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
        allow_button=True,
    )
    assert result.button is not None
    assert result.button.style == discord.ButtonStyle.premium
    assert result.button.sku_id == 999
    view = result.view()
    assert view is not None
    assert list(view.children) == [result.button]


async def test_member_never_gets_a_button_even_with_a_configured_sku(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 999)
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=1,
        is_admin=False,
        already_top_tier=False,
        benefit="75",
    )
    assert result.button is None


async def test_allow_button_false_forces_text_only_even_for_an_admin(monkeypatch):
    """The prefix-command path: no component support, so no button, whatever
    the SKU configuration says."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 999)
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=1,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
        allow_button=False,
    )
    assert result.button is None


async def test_user_scoped_button_uses_the_comfort_pack_sku(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 111)
    store = _FakeStore()
    bot = _bot(store)
    result = await premium_upsell.for_user_refusal(
        bot,
        limit_key="favourites",
        person_id=1,
        already_top_tier=False,
        benefit="300",
    )
    assert result.button.sku_id == 111


# ---------------------------------------------------------------------------
# DB failure shows nothing (fail closed on nagging)
# ---------------------------------------------------------------------------


async def test_a_claim_failure_shows_nothing_and_does_not_raise():
    store = _FakeStore(raise_on_fetchrow=True)
    bot = _bot(store)
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=1,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )
    assert result is None


async def test_a_missing_pool_shows_nothing():
    bot = types.SimpleNamespace()  # no db_pool attribute at all
    result = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="guild_playlists",
        guild_id=1,
        person_id=1,
        is_admin=True,
        already_top_tier=False,
        benefit="75",
    )
    assert result is None


async def test_mark_premium_opened_swallows_a_write_failure():
    store = _FakeStore(raise_on_execute=True)
    # Must not raise - a render failure must never follow from this.
    await premium_upsell.mark_premium_opened(store, 1)


# ---------------------------------------------------------------------------
# invoker_is_admin / is_slash_context - never raise
# ---------------------------------------------------------------------------


def test_invoker_is_admin_true_for_manage_guild():
    member = types.SimpleNamespace(
        guild_permissions=types.SimpleNamespace(manage_guild=True)
    )
    assert premium_upsell.invoker_is_admin(member) is True


def test_invoker_is_admin_false_on_a_broken_member_object():
    """A member stand-in with no ``.guild`` (``guild_permissions`` itself
    reads ``member.guild``) must read as "not admin", never raise."""
    broken = types.SimpleNamespace()
    assert premium_upsell.invoker_is_admin(broken) is False


def test_is_slash_context_true_for_a_bare_interaction():
    fake_interaction = object.__new__(discord.Interaction)
    assert premium_upsell.is_slash_context(fake_interaction) is True


def test_is_slash_context_reads_ctx_dot_interaction():
    assert premium_upsell.is_slash_context(types.SimpleNamespace(interaction=None)) is False
    assert (
        premium_upsell.is_slash_context(types.SimpleNamespace(interaction=object()))
        is True
    )


def test_is_slash_context_false_on_a_ctx_double_with_no_attribute():
    assert premium_upsell.is_slash_context(types.SimpleNamespace()) is False
