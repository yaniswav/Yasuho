"""ITEM A (M5 review, fresh adversarial pass on commit ad43476): two of the
AniList feed panel's own pre-checks hit a premium-raisable cap and refused
through ``tools.interactions.reply`` with NO upsell wired at all, unlike the
cog-level helpers (``_add_follow``, ``_create_feed``) that back them:

* ``_AddFollowButton.callback`` - the "Add follow" button's pre-check before
  opening the search modal (the ``max_follows_per_feed`` cap).
* ``_TrackTitleButton.callback`` - the "Track a title" button's own
  pre-check (the ``max_subs_per_feed`` cap).

Both are admin-only surfaces (the panel requires manage_guild to open at
all), text-only (``tools.interactions.reply`` has no ``view=`` support), and
both now call :func:`tools.premium_upsell.for_guild_refusal` exactly like
every other wired refusal in this tree.

No real Discord, no DB: the panel/manager are plain ``types.SimpleNamespace``
stand-ins carrying only what each button's ``callback`` actually reads, and
``bot.db_pool`` is a tiny double implementing the real claim-query semantics
(not a canned return), same shape as tests/tools/test_premium_upsell.py's own
``_FakeStore``.
"""

from __future__ import annotations

import types

from cogs.anilist import feed_views
from tools import premium


class _ClaimingPool:
    """Grants exactly one premium-upsell claim per (user_id, limit_key)."""

    def __init__(self):
        self._claimed = set()

    async def fetchrow(self, query, user_id, limit_key, cooldown, sentinel):
        key = (user_id, limit_key)
        if key in self._claimed:
            return None
        self._claimed.add(key)
        return {"shown_at": None}

    async def execute(self, *args):
        return "INSERT 0 1"


def _bot():
    return types.SimpleNamespace(db_pool=_ClaimingPool(), premium=None)


# ---------------------------------------------------------------------------
# _AddFollowButton: the "Add follow" pre-check (max_follows_per_feed)
# ---------------------------------------------------------------------------


async def test_add_follow_button_refuses_at_the_cap_with_the_upsell():
    panel = types.SimpleNamespace(
        cog=types.SimpleNamespace(bot=_bot()),
        guild=types.SimpleNamespace(id=1),
        follows=[object()] * 50,
        limits=types.SimpleNamespace(max_follows_per_feed=50),
    )
    button = feed_views._AddFollowButton(panel)
    replies = []

    async def _send_message(*args, **kwargs):
        replies.append(args[0] if args else kwargs.get("content"))

    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=9),
        response=types.SimpleNamespace(is_done=lambda: False, send_message=_send_message),
    )

    await button.callback(interaction)

    assert len(replies) == 1
    assert "already follows the maximum" in replies[0]
    assert "Yasuho+ raises this limit to" in replies[0]
    assert str(premium.GUILD_PREMIUM.max_follows_per_feed) in replies[0]


async def test_add_follow_button_below_the_cap_opens_the_modal_not_a_reply():
    panel = types.SimpleNamespace(
        cog=types.SimpleNamespace(bot=_bot()),
        guild=types.SimpleNamespace(id=1),
        follows=[],
        limits=types.SimpleNamespace(max_follows_per_feed=50),
    )
    button = feed_views._AddFollowButton(panel)
    modals = []

    async def _send_modal(modal):
        modals.append(modal)

    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=9),
        response=types.SimpleNamespace(is_done=lambda: False, send_modal=_send_modal),
    )

    await button.callback(interaction)

    assert len(modals) == 1


# ---------------------------------------------------------------------------
# _TrackTitleButton: the "Track a title" pre-check (max_subs_per_feed)
# ---------------------------------------------------------------------------


async def test_track_title_button_refuses_at_the_cap_with_the_upsell():
    manager = types.SimpleNamespace(
        cog=types.SimpleNamespace(bot=_bot()),
        guild=types.SimpleNamespace(id=1),
        at_cap=True,
        max_subs=200,
    )
    button = feed_views._TrackTitleButton(manager)
    replies = []
    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=9),
        response=types.SimpleNamespace(is_done=lambda: False),
    )

    async def _send_message(*args, **kwargs):
        replies.append(args[0] if args else kwargs.get("content"))

    interaction.response.send_message = _send_message

    await button.callback(interaction)

    assert len(replies) == 1
    assert "already tracks the maximum" in replies[0]
    assert "Yasuho+ raises this limit to" in replies[0]
    assert str(premium.GUILD_PREMIUM.max_subs_per_feed) in replies[0]


async def test_track_title_button_below_the_cap_opens_the_modal_not_a_reply():
    manager = types.SimpleNamespace(
        cog=types.SimpleNamespace(bot=_bot()),
        guild=types.SimpleNamespace(id=1),
        at_cap=False,
        max_subs=200,
    )
    button = feed_views._TrackTitleButton(manager)
    modals = []

    async def _send_modal(modal):
        modals.append(modal)

    interaction = types.SimpleNamespace(
        user=types.SimpleNamespace(id=9),
        response=types.SimpleNamespace(is_done=lambda: False, send_modal=_send_modal),
    )

    await button.callback(interaction)

    assert len(modals) == 1
