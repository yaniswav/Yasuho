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
