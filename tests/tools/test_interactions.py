"""Unit tests for ``tools.interactions``.

The module centralises the "has this interaction already been responded to?"
fork that button/select/modal callbacks repeat: ``response.send_message`` vs
``followup.send`` for a reply, and ``response.edit_message`` vs the stored
``message.edit`` for an in-place refresh. Every branch is best-effort and must
swallow ``discord.HTTPException`` so a flaky Discord edit never crashes a
callback.

These tests use the shared ``make_interaction`` fixture (see the repo-root
``conftest.py``), which records every async call onto the interaction:

- ``.sent``          -> response.send_message(args, kwargs)
- ``.followups``     -> followup.send(args, kwargs)
- ``.edits``         -> response.edit_message(args, kwargs)
- ``.message_edits`` -> message.edit(args, kwargs)

No network, database, Discord, or Lavalink is touched.
"""

import discord
import pytest

from tools import interactions

# ---------------------------------------------------------------------------
# Helpers: build a real discord.HTTPException so the ``except`` clauses that
# name it actually match (a bare Exception would not be caught).
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """Minimal aiohttp-response stand-in for ``discord.HTTPException.__init__``."""

    status = 429
    reason = "Too Many Requests"


def _http_exc(message: str = "boom") -> discord.HTTPException:
    return discord.HTTPException(_FakeHTTPResponse(), message)


def _raiser(exc):
    """Return an async callable that always raises ``exc`` when awaited."""

    async def _boom(*args, **kwargs):
        raise exc

    return _boom


def test_http_exc_helper_is_a_real_httpexception():
    """Guard: our stand-in exception really is a discord.HTTPException."""
    exc = _http_exc()
    assert isinstance(exc, discord.HTTPException)
    assert exc.status == 429


# ---------------------------------------------------------------------------
# reply()
# ---------------------------------------------------------------------------


async def test_reply_not_done_uses_send_message(make_interaction):
    itx = make_interaction(done=False)
    await interactions.reply(itx, "hello")
    assert itx.sent == [(("hello",), {"ephemeral": True})]
    assert itx.followups == []
    # send_message flips the response to "done".
    assert itx.response.is_done() is True


async def test_reply_done_uses_followup(make_interaction):
    itx = make_interaction(done=True)
    await interactions.reply(itx, "hello")
    assert itx.followups == [(("hello",), {"ephemeral": True})]
    assert itx.sent == []


async def test_reply_forwards_ephemeral_false(make_interaction):
    itx = make_interaction(done=False)
    await interactions.reply(itx, "hi", ephemeral=False)
    assert itx.sent == [(("hi",), {"ephemeral": False})]


async def test_reply_done_forwards_ephemeral_false_to_followup(make_interaction):
    itx = make_interaction(done=True)
    await interactions.reply(itx, "hi", ephemeral=False)
    assert itx.followups == [(("hi",), {"ephemeral": False})]


async def test_reply_swallows_httpexception_on_send_message(make_interaction):
    itx = make_interaction(done=False)
    itx.response.send_message = _raiser(_http_exc())
    # Must not propagate.
    await interactions.reply(itx, "hi")
    assert itx.followups == []


async def test_reply_swallows_httpexception_on_followup(make_interaction):
    itx = make_interaction(done=True)
    itx.followup.send = _raiser(_http_exc())
    await interactions.reply(itx, "hi")
    assert itx.sent == []


async def test_reply_lets_non_http_exceptions_propagate(make_interaction):
    """The catch is narrow: only discord.HTTPException is swallowed."""
    itx = make_interaction(done=False)
    itx.response.send_message = _raiser(ValueError("not http"))
    with pytest.raises(ValueError):
        await interactions.reply(itx, "hi")


# ---------------------------------------------------------------------------
# notify_failure()  -- routes through reply() with ephemeral=True.
# ---------------------------------------------------------------------------


async def test_notify_failure_not_done_uses_send_message_default_text(make_interaction):
    itx = make_interaction(done=False)
    await interactions.notify_failure(itx)
    assert itx.sent == [(("Something went wrong.",), {"ephemeral": True})]
    assert itx.followups == []


async def test_notify_failure_done_uses_followup(make_interaction):
    itx = make_interaction(done=True)
    await interactions.notify_failure(itx, "nope")
    assert itx.followups == [(("nope",), {"ephemeral": True})]
    assert itx.sent == []


async def test_notify_failure_swallows_httpexception(make_interaction):
    itx = make_interaction(done=False)
    itx.response.send_message = _raiser(_http_exc())
    await interactions.notify_failure(itx, "still fine")


# ---------------------------------------------------------------------------
# refresh_in_place()
# ---------------------------------------------------------------------------


async def test_refresh_in_place_not_done_edits_response(make_interaction):
    itx = make_interaction(done=False)
    embed = object()
    view = object()
    await interactions.refresh_in_place(itx, itx.message, embed=embed, view=view)
    assert itx.edits == [((), {"embed": embed, "view": view})]
    # It returns after the live edit; the stored message is untouched.
    assert itx.message_edits == []


async def test_refresh_in_place_done_falls_back_to_message_edit(make_interaction):
    itx = make_interaction(done=True)
    embed = object()
    view = object()
    await interactions.refresh_in_place(itx, itx.message, embed=embed, view=view)
    assert itx.edits == []
    assert itx.message_edits == [((), {"embed": embed, "view": view})]


async def test_refresh_in_place_done_with_no_message_is_noop(make_interaction):
    itx = make_interaction(done=True)
    # message=None: nothing to edit, and nothing should raise.
    await interactions.refresh_in_place(itx, None, embed=object(), view=object())
    assert itx.edits == []
    assert itx.message_edits == []


async def test_refresh_in_place_edit_message_httpexception_falls_back(make_interaction):
    """A failed live edit (not done) falls through to the stored message edit."""
    itx = make_interaction(done=False)
    itx.response.edit_message = _raiser(_http_exc())
    embed = object()
    view = object()
    await interactions.refresh_in_place(itx, itx.message, embed=embed, view=view)
    assert itx.message_edits == [((), {"embed": embed, "view": view})]


async def test_refresh_in_place_both_paths_httpexception_swallowed(make_interaction):
    itx = make_interaction(done=False)
    itx.response.edit_message = _raiser(_http_exc())
    itx.message.edit = _raiser(_http_exc())
    # Both raise; nothing should propagate.
    await interactions.refresh_in_place(itx, itx.message, embed=object(), view=object())


async def test_refresh_in_place_done_message_edit_httpexception_swallowed(
    make_interaction,
):
    itx = make_interaction(done=True)
    itx.message.edit = _raiser(_http_exc())
    await interactions.refresh_in_place(itx, itx.message, embed=object(), view=object())
    # Never attempted the live edit because the interaction was already done.
    assert itx.edits == []


async def test_refresh_in_place_lets_non_http_exceptions_propagate(make_interaction):
    itx = make_interaction(done=False)
    itx.response.edit_message = _raiser(RuntimeError("not http"))
    with pytest.raises(RuntimeError):
        await interactions.refresh_in_place(
            itx, itx.message, embed=object(), view=object()
        )


# ---------------------------------------------------------------------------
# defer() and the ephemeral-flow marker
# ---------------------------------------------------------------------------
# `EPHEMERAL_FLOW` records "every reply in this flow is private" on the
# interaction's own `extras`, because discord.py keeps no readable trace of an
# ephemeral defer. It was written for the COMMAND path (a Context, whose
# interaction hangs off `ctx.interaction`); a button, select or modal callback
# is handed the raw Interaction and has no Context anywhere, so the reader has
# to accept both shapes.


async def test_defer_records_the_ephemeral_choice_on_the_interaction(make_interaction):
    """An ephemeral defer marks the flow, the way defer_ephemeral does for a ctx."""

    itx = make_interaction(done=False)

    assert await interactions.defer(itx, ephemeral=True, thinking=True) is True
    assert itx.defers == [((), {"ephemeral": True, "thinking": True})]
    assert interactions.prefers_ephemeral(itx) is True


async def test_a_public_defer_marks_nothing(make_interaction):
    """The counter-test: the marker follows the CHOICE, not the defer."""

    itx = make_interaction(done=False)

    await interactions.defer(itx)

    assert itx.defers == [((), {"ephemeral": False, "thinking": False})]
    assert interactions.prefers_ephemeral(itx) is False


async def test_a_failed_ephemeral_defer_marks_nothing(make_interaction):
    """A defer that never landed must not claim a flow it did not open."""

    itx = make_interaction(done=False)
    itx.response.defer = _raiser(_http_exc())

    assert await interactions.defer(itx, ephemeral=True, thinking=True) is False
    assert interactions.prefers_ephemeral(itx) is False


async def test_mark_ephemeral_accepts_a_raw_interaction(make_interaction):
    """The component path: no Context, so the Interaction IS the target."""

    itx = make_interaction(done=False)

    assert interactions.prefers_ephemeral(itx) is False
    interactions.mark_ephemeral(itx)
    assert interactions.prefers_ephemeral(itx) is True


def test_the_context_reading_still_wins_over_the_object_itself(make_context, make_interaction):
    """A Context is still read through ``ctx.interaction``, not through itself.

    Both readings have to land in the SAME dict, or a command would mark one
    place and the error reporter would read another.
    """

    ctx = make_context()
    ctx.interaction = make_interaction(done=False)

    interactions.mark_ephemeral(ctx)

    assert interactions.prefers_ephemeral(ctx) is True
    assert interactions.prefers_ephemeral(ctx.interaction) is True
    assert ctx.interaction.extras[interactions.EPHEMERAL_FLOW] is True


def test_marking_a_prefix_context_is_still_a_noop(make_context):
    """No interaction, no extras, nothing to remember - and nothing raised."""

    ctx = make_context()
    assert ctx.interaction is None

    interactions.mark_ephemeral(ctx)

    assert interactions.prefers_ephemeral(ctx) is False


def test_marking_something_with_no_extras_is_a_noop():
    """A bookkeeping helper must never be the thing that breaks a callback."""

    class _Bare:
        pass

    bare = _Bare()
    interactions.mark_ephemeral(bare)
    assert interactions.prefers_ephemeral(bare) is False
