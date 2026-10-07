"""Integration tests for Reminder._send_with_upsell (M5): the prefix
plain-line path and the slash public-refusal-plus-ephemeral-followup path,
driven against a working in-memory premium_upsells double (not the generic
``fake_pool`` fixture, which has no compatible ``fetchrow`` and so - as the
existing M4c tests in test_reminders_recurring.py already prove - makes
every upsell silently no-op, which is correct fail-closed behaviour but
proves nothing about the HAPPY path).
"""

from __future__ import annotations

import datetime
import types

from cogs.community.reminders import Reminder
from tools import premium

UTC = datetime.timezone.utc


class _WorkingUpsellStore:
    """Same semantics as tools/premium_upsell.py's claim query, modelled
    directly - see tests/tools/test_premium_upsell.py's own copy."""

    def __init__(self):
        self.rows = {}
        self.now = datetime.datetime(2026, 1, 8, tzinfo=UTC)

    async def fetchrow(self, query, user_id, limit_key, cooldown, sentinel):
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

    async def execute(self, *_args):
        pass


class _PrefixCtx:
    def __init__(self):
        self.sends = []
        self.author = types.SimpleNamespace(id=42)
        self.interaction = None

    async def send(self, content):
        self.sends.append(content)


class _SlashCtx:
    def __init__(self):
        self.sends = []
        self.followups = []
        self.author = types.SimpleNamespace(id=42)
        self.interaction = types.SimpleNamespace(
            followup=types.SimpleNamespace(send=self._followup_send)
        )

    async def send(self, content):
        self.sends.append(content)

    async def _followup_send(self, content, **kwargs):
        self.followups.append((content, kwargs))


def _cog(store):
    cog = object.__new__(Reminder)
    cog.bot = types.SimpleNamespace(db_pool=store)
    return cog


async def test_prefix_appends_the_plain_line_to_the_one_message_no_button():
    store = _WorkingUpsellStore()
    cog = _cog(store)
    ctx = _PrefixCtx()

    await cog._send_with_upsell(
        ctx,
        "You already have 25 reminders pending - wait for some to fire before adding more.",
        limit_key="reminders_pending",
        benefit=premium.USER_PREMIUM.max_pending_reminders,
        already_top_tier=False,
        is_slash=False,
    )

    assert len(ctx.sends) == 1
    assert ctx.sends[0] == (
        "You already have 25 reminders pending - wait for some to fire before "
        "adding more.\nPack Confort raises this limit to 60. See /premium."
    )


async def test_slash_sends_the_public_refusal_unmodified_plus_an_ephemeral_followup():
    store = _WorkingUpsellStore()
    cog = _cog(store)
    ctx = _SlashCtx()
    base_text = "You already have 25 reminders pending - wait for some to fire before adding more."

    await cog._send_with_upsell(
        ctx,
        base_text,
        limit_key="reminders_pending",
        benefit=premium.USER_PREMIUM.max_pending_reminders,
        already_top_tier=False,
        is_slash=True,
    )

    # The public refusal is sent EXACTLY as given - never modified.
    assert ctx.sends == [base_text]
    # The upsell rides a SEPARATE ephemeral followup, never the public reply.
    assert len(ctx.followups) == 1
    content, kwargs = ctx.followups[0]
    assert content == "Pack Confort raises this limit to 60. See /premium."
    assert kwargs["ephemeral"] is True


async def test_already_top_tier_sends_only_the_base_text():
    store = _WorkingUpsellStore()
    cog = _cog(store)
    ctx = _PrefixCtx()

    await cog._send_with_upsell(
        ctx,
        "You already have 60 reminders pending - wait for some to fire before adding more.",
        limit_key="reminders_pending",
        benefit=premium.USER_PREMIUM.max_pending_reminders,
        already_top_tier=True,
        is_slash=False,
    )

    assert ctx.sends == [
        "You already have 60 reminders pending - wait for some to fire before "
        "adding more."
    ]


async def test_the_seven_day_throttle_applies_across_both_surfaces():
    store = _WorkingUpsellStore()
    cog = _cog(store)

    await cog._send_with_upsell(
        _PrefixCtx(),
        "base",
        limit_key="reminders_pending",
        benefit=60,
        already_top_tier=False,
        is_slash=False,
    )
    # Same person, same key, now via the slash surface - already claimed.
    slash_ctx = _SlashCtx()
    await cog._send_with_upsell(
        slash_ctx,
        "base",
        limit_key="reminders_pending",
        benefit=60,
        already_top_tier=False,
        is_slash=True,
    )
    assert slash_ctx.followups == []
