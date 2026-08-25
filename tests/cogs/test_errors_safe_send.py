"""Behaviour of the guarded error-report send (``cogs/system/errors._safe_send``).

Prod incident, 2026-08-25 03:23:03: a member ran ``/anilist login``; the command
failed with ``discord.errors.NotFound: 404 (error code 10062): Unknown
interaction``. The handler logged it with an error_id (that part worked), then
tried to tell the user with ``await ctx.send(...)`` on that same dead
interaction. 10062 again, "Ignoring exception in on_command_error" in the log,
and the user got NOTHING.

The ladder under test:

1. interaction live and unacknowledged -> respond normally (via ``ctx.send``);
2. interaction already acknowledged (40060 / ``InteractionResponded``) ->
   followup;
3. interaction dead/expired -> the channel, a plain message;
4. channel refuses (403 / 404 / rate limit / anything) -> WARNING, give up.

Every rung is asserted from the outside: what the user received, on which
transport, and what the log says. No network, no Discord.
"""

import asyncio
import logging
import types

import discord
import pytest
from discord.ext import commands

from cogs.system import errors
from tools import interactions


class _Recorder:
    """A send-like callable that records calls and optionally raises."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(id=1)


class _Response:
    """Stand-in for ``interaction.response`` with a settable done flag."""

    def __init__(self, done=False):
        self.done = done

    def is_done(self):
        return self.done


def _http_error(status, code):
    """Build a real ``discord.HTTPException`` subclass carrying ``code``.

    discord.py reads ``code`` out of the JSON body, so passing a dict gives an
    exception whose ``.code`` matches what the API actually sends - the routing
    signal ``_safe_send`` keys on.
    """
    response = types.SimpleNamespace(status=status, reason="", headers={})
    cls = {403: discord.Forbidden, 404: discord.NotFound}.get(
        status, discord.HTTPException
    )
    return cls(response, {"code": code, "message": "boom"})


UNKNOWN_INTERACTION = 10062
ALREADY_ACKNOWLEDGED = 40060


def _ctx(*, send_error=None, channel_error=None, followup_error=None, interaction=True,
         expired=False, done=False, ephemeral_flow=False):
    """A fake Context wired for one rung to fail and the next to be observed."""
    send = _Recorder(send_error)
    channel_send = _Recorder(channel_error)
    followup_send = _Recorder(followup_error)

    inter = None
    if interaction:
        # ``extras`` is the real discord.py Interaction attribute
        # (interactions.py: ``self.extras: Dict[Any, Any] = {}``) that
        # ``tools.interactions`` marks an ephemeral flow on.
        inter = types.SimpleNamespace(
            response=_Response(done),
            followup=types.SimpleNamespace(send=followup_send),
            is_expired=lambda: expired,
            extras={interactions.EPHEMERAL_FLOW: True} if ephemeral_flow else {},
        )

    ctx = types.SimpleNamespace(
        send=send,
        channel=types.SimpleNamespace(send=channel_send),
        interaction=inter,
        author=types.SimpleNamespace(id=42, mention="<@42>"),
        guild=types.SimpleNamespace(id=7),
        command=types.SimpleNamespace(qualified_name="anilist login"),
    )
    ctx.recorded = types.SimpleNamespace(
        send=send, channel=channel_send, followup=followup_send
    )
    return ctx


# ---------------------------------------------------------------------------
# Rung 1: the happy path stays exactly as it was
# ---------------------------------------------------------------------------


async def test_rung_1_live_interaction_replies_normally():
    ctx = _ctx()
    embed = discord.Embed(description="report")

    await errors._safe_send(ctx, embed=embed, surface="unit")

    assert ctx.recorded.send.calls == [((), {"embed": embed})]
    assert ctx.recorded.channel.calls == []
    assert ctx.recorded.followup.calls == []


async def test_only_the_kwargs_the_caller_set_are_forwarded():
    """A text-only branch must not start passing ``embed=None``.

    ``handle_message_parameters`` reads an explicit ``embed=None`` as "clear the
    embeds", and the existing suite asserts the CheckFailure reply carries no
    embed key at all. Keep the call shape byte-identical to the old one.
    """
    ctx = _ctx()

    await errors._safe_send(ctx, "no permission", delete_after=10)

    (args, kwargs) = ctx.recorded.send.calls[0]
    assert args == ("no permission",)
    assert kwargs == {"delete_after": 10}
    assert "embed" not in kwargs


# ---------------------------------------------------------------------------
# Rung 2: the interaction was acknowledged behind discord.py's back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(400, ALREADY_ACKNOWLEDGED),
        discord.InteractionResponded(types.SimpleNamespace(id=1)),
    ],
    ids=["api-40060", "local-InteractionResponded"],
)
async def test_rung_2_already_acknowledged_falls_back_to_followup(failure):
    """40060 means the initial-response slot is spent; the webhook is not.

    ``InteractionResponse.is_done()`` is a purely LOCAL flag, so an ack made by
    another task/view/pre-hook is invisible to ``ctx.send`` and it burns the
    response slot. The followup is the right transport, and the channel must NOT
    be used - the interaction is alive, only its first slot is gone.
    """
    ctx = _ctx(send_error=failure)
    embed = discord.Embed(description="report")

    await errors._safe_send(ctx, embed=embed, surface="unit")

    assert ctx.recorded.followup.calls == [((), {"embed": embed})]
    assert ctx.recorded.channel.calls == []


async def test_rung_2_is_skipped_when_ctx_send_already_used_the_followup():
    """When the response was already done, ``ctx.send`` itself went to followup.

    Retrying the followup would repeat the request that just failed, so a
    failure there must drop straight to the channel instead.
    """
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION), done=True)

    await errors._safe_send(ctx, "boom", surface="unit")

    assert ctx.recorded.followup.calls == []
    assert len(ctx.recorded.channel.calls) == 1


async def test_rung_2_failure_drops_to_the_channel():
    ctx = _ctx(
        send_error=_http_error(400, ALREADY_ACKNOWLEDGED),
        followup_error=_http_error(404, UNKNOWN_INTERACTION),
    )

    await errors._safe_send(ctx, "boom", surface="unit")

    assert len(ctx.recorded.followup.calls) == 1
    assert len(ctx.recorded.channel.calls) == 1


# ---------------------------------------------------------------------------
# Rung 3: the incident itself - a dead interaction falls back to the channel
# ---------------------------------------------------------------------------


async def test_rung_3_dead_interaction_reaches_the_user_in_the_channel():
    """THE regression. 10062 on the reply must not leave the user with nothing."""
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))
    embed = discord.Embed(description="report")

    await errors._safe_send(ctx, embed=embed, surface="unit")

    assert len(ctx.recorded.channel.calls) == 1
    (args, kwargs) = ctx.recorded.channel.calls[0]
    assert kwargs["embed"] is embed


async def test_safe_send_never_raises_on_a_dead_interaction():
    """The reporter must not become the error, whatever the caller does next."""
    ctx = _ctx(
        send_error=_http_error(404, UNKNOWN_INTERACTION),
        channel_error=_http_error(403, 50013),
    )

    assert await errors._safe_send(ctx, "boom", surface="unit") is None


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(404, UNKNOWN_INTERACTION),
        _http_error(404, 10015),  # Unknown Webhook: an expired followup token
        _http_error(401, 50027),  # Invalid Webhook Token
        _http_error(429, 0),  # rate limited by the API
        discord.RateLimited(retry_after=5.0),  # NOT an HTTPException subclass
        RuntimeError("aiohttp exploded"),  # nothing discord-shaped at all
    ],
    ids=["10062", "10015", "50027", "429", "RateLimited", "non-http"],
)
async def test_every_failure_shape_still_reaches_the_channel(failure):
    """``except discord.HTTPException`` would be too narrow.

    ``discord.RateLimited`` subclasses ``DiscordException`` only, and a raw
    aiohttp/connection failure subclasses neither. The helper catches
    ``Exception`` so none of them can strand the user.
    """
    ctx = _ctx(send_error=failure)

    await errors._safe_send(ctx, "boom", surface="unit")

    assert len(ctx.recorded.channel.calls) == 1


async def test_channel_fallback_mentions_the_invoker():
    """A slash invocation is invisible to bystanders; address the one person.

    An unaddressed embed dropped into a busy channel reads as noise to everyone
    else and can be missed by the user it is actually for.
    """
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))

    await errors._safe_send(ctx, "no permission", surface="unit")

    (args, _kwargs) = ctx.recorded.channel.calls[0]
    assert args[0] == "<@42> no permission"


async def test_channel_fallback_mention_is_the_whole_content_for_an_embed():
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))

    await errors._safe_send(ctx, embed=discord.Embed(description="x"), surface="unit")

    (args, _kwargs) = ctx.recorded.channel.calls[0]
    assert args[0] == "<@42>"


async def test_channel_fallback_can_only_ping_the_invoker():
    """Error text is attacker-influenced (a bad argument, a guild name).

    Embeds never ping, and the content only holds our own mention, but the
    allow-list is pinned to the single author anyway so no future branch can
    turn a crash report into a mass ping.
    """
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))

    await errors._safe_send(ctx, "boom", surface="unit")

    mentions = ctx.recorded.channel.calls[0][1]["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    assert [u.id for u in mentions.users] == [42]


async def test_channel_fallback_self_deletes_when_the_caller_asked_for_nothing():
    """A public crash embed in a busy channel is not free.

    The error_id is already in the log before any send is attempted, so nothing
    diagnosable is lost when the message goes away.
    """
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))

    await errors._safe_send(ctx, embed=discord.Embed(description="x"), surface="unit")

    assert ctx.recorded.channel.calls[0][1]["delete_after"] == errors._FALLBACK_DELETE_AFTER


async def test_channel_fallback_never_lives_shorter_than_the_floor():
    """A branch's own delete_after was chosen for a reply the user was WATCHING.

    The Forbidden branch clears "I need more permissions!" after 3 seconds
    because on the interaction it appears exactly where the user is looking. The
    channel fallback is the opposite case - it exists because the user is NOT
    looking there - so honouring 3 seconds literally would delete the rescue
    message before it can be read, or its error_id copied.
    """
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))

    await errors._safe_send(ctx, "boom", delete_after=3, surface="unit")

    assert (
        ctx.recorded.channel.calls[0][1]["delete_after"]
        == errors._FALLBACK_DELETE_AFTER
    )


async def test_channel_fallback_honours_a_longer_explicit_delete_after():
    """The floor only raises: a caller asking for MORE time still gets it."""
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))
    longer = errors._FALLBACK_DELETE_AFTER + 60

    await errors._safe_send(ctx, "boom", delete_after=longer, surface="unit")

    assert ctx.recorded.channel.calls[0][1]["delete_after"] == longer


async def test_the_interaction_reply_still_honours_the_short_delete_after():
    """...and the floor applies to the CHANNEL rung only.

    Rung 1 is the reply the caller sized its delete_after for; changing that
    would be a UX change nobody asked for.
    """
    ctx = _ctx()

    await errors._safe_send(ctx, "boom", delete_after=3, surface="unit")

    assert ctx.recorded.send.calls[0][1]["delete_after"] == 3


# ---------------------------------------------------------------------------
# Cancellation is not an error to be swallowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rung",
    ["primary", "followup", "channel"],
    ids=["rung-1", "rung-2", "rung-3"],
)
async def test_cancellation_propagates_out_of_every_rung(rung):
    """``except Exception``, NOT ``except BaseException`` - and it matters.

    ``asyncio.CancelledError`` is a ``BaseException`` on 3.8+. Widening any of
    these rungs to ``BaseException`` would make the error reporter eat a
    cancellation and quietly keep sending on a task the loop is trying to tear
    down, so shutdown would hang on the one code path that runs when things are
    already going wrong. Every rung is pinned, not just the first.
    """
    cancelled = asyncio.CancelledError()
    dead = _http_error(404, UNKNOWN_INTERACTION)
    ctx = {
        "primary": lambda: _ctx(send_error=cancelled),
        "followup": lambda: _ctx(
            send_error=_http_error(400, ALREADY_ACKNOWLEDGED),
            followup_error=cancelled,
        ),
        "channel": lambda: _ctx(send_error=dead, channel_error=cancelled),
    }[rung]()

    with pytest.raises(asyncio.CancelledError):
        await errors._safe_send(ctx, "boom", surface="unit")


# ---------------------------------------------------------------------------
# An ephemeral flow stays ephemeral
# ---------------------------------------------------------------------------


async def test_the_report_is_ephemeral_when_the_command_deferred_ephemerally():
    """``/anilist login`` and ``/anilist code`` keep the whole flow private.

    ``Context.send`` forwards ``ephemeral`` on every interaction path and
    defaults it to False, so without the marker a crash inside an ephemerally
    deferred command would answer PUBLICLY - a new inconsistency introduced by
    the defers, on the two commands that handle an OAuth link and a PIN.
    """
    ctx = _ctx(ephemeral_flow=True)

    await errors._safe_send(ctx, "boom", surface="unit")

    assert ctx.recorded.send.calls[0][1]["ephemeral"] is True


async def test_the_followup_rung_is_ephemeral_too():
    ctx = _ctx(
        send_error=_http_error(400, ALREADY_ACKNOWLEDGED), ephemeral_flow=True
    )

    await errors._safe_send(ctx, "boom", surface="unit")

    assert ctx.recorded.followup.calls[0][1]["ephemeral"] is True


async def test_an_unmarked_command_sends_exactly_as_before():
    """No marker, no kwarg: the call shape of every other branch is untouched."""
    ctx = _ctx()

    await errors._safe_send(ctx, "boom", surface="unit")

    assert "ephemeral" not in ctx.recorded.send.calls[0][1]


async def test_the_channel_fallback_of_an_ephemeral_flow_still_delivers():
    """A dead token leaves no private route, and silence is the worse failure.

    The channel message carries an error_id and a usage signature, never the
    arguments the user typed, so falling back in public is safe - and it is the
    only thing standing between the user and the 2026-08-25 incident again.
    """
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION), ephemeral_flow=True)

    await errors._safe_send(ctx, "boom", surface="unit")

    assert len(ctx.recorded.channel.calls) == 1
    assert "ephemeral" not in ctx.recorded.channel.calls[0][1]


# ---------------------------------------------------------------------------
# No duplicate posts
# ---------------------------------------------------------------------------


async def test_no_channel_retry_when_ctx_send_was_already_a_channel_send():
    """A prefix command has no interaction: ``ctx.send`` IS the channel send.

    discord.py's ``Context.send`` starts with ``if self.interaction is None or
    self.interaction.is_expired(): return await super().send(...)``. Retrying
    ``ctx.channel.send`` would reissue the identical request that just failed.
    """
    ctx = _ctx(send_error=_http_error(403, 50013), interaction=False)

    await errors._safe_send(ctx, "boom", surface="unit")

    assert ctx.recorded.channel.calls == []


async def test_no_channel_retry_when_the_15_minute_token_had_already_expired():
    """``is_expired()`` True means ``ctx.send`` also went to the channel."""
    ctx = _ctx(send_error=_http_error(403, 50013), expired=True)

    await errors._safe_send(ctx, "boom", surface="unit")

    assert ctx.recorded.channel.calls == []


async def test_a_reply_that_landed_is_not_posted_twice():
    """``send_message`` succeeded, only the post-send bookkeeping blew up.

    ``ctx.send`` also does ``original_response()`` and schedules ``delete_after``
    after responding; either can fail on a reply the user already received.
    discord.py sets ``_response_type`` only AFTER a successful
    ``create_interaction_response``, so ``is_done()`` flipping False -> True is
    local proof the reply landed - and a channel fallback here would be a public
    duplicate.
    """
    ctx = _ctx()
    post_send_failure = _http_error(404, 10008)  # Unknown Message

    async def send(*args, **kwargs):
        ctx.interaction.response.done = True  # the response DID land...
        raise post_send_failure  # ...and only what came after it failed

    ctx.send = send

    await errors._safe_send(ctx, "boom", surface="unit")

    assert ctx.recorded.channel.calls == []


async def test_a_landed_reply_still_leaves_an_error_id_trail(caplog):
    """The one branch that neither delivers nor falls back must still be found.

    ``is_done()`` flipping across our own send is strong evidence the reply
    landed - but it is not proof: another task could have acked the interaction
    while our send failed for an unrelated reason, in which case nobody told the
    user anything. The message is deliberately not retried (a duplicate crash
    embed is a real cost on the far likelier branch), so the log line is the ONLY
    thing left, and a DEBUG line is invisible in production. It must carry the
    error_id, or "the user says they saw nothing" stops being diagnosable -
    which is the single invariant this ladder sells.
    """
    ctx = _ctx()

    async def send(*args, **kwargs):
        ctx.interaction.response.done = True
        raise _http_error(500, 0)  # NOT 40060: could be either reading

    ctx.send = send

    with caplog.at_level(logging.WARNING, logger=errors.log.name):
        await errors._safe_send(
            ctx, "boom", surface="command-invoke-error", error_id="cafebabe"
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "cafebabe" in message
    assert "command-invoke-error" in message
    assert "anilist login" in message
    assert warnings[0].exc_info is not None
    assert ctx.recorded.channel.calls == []  # still no public duplicate


# ---------------------------------------------------------------------------
# Rung 4: give up, but never silently in the log
# ---------------------------------------------------------------------------


async def test_rung_4_warns_once_when_every_transport_refuses(caplog):
    ctx = _ctx(
        send_error=_http_error(404, UNKNOWN_INTERACTION),
        channel_error=_http_error(403, 50013),
    )

    with caplog.at_level(logging.WARNING, logger=errors.log.name):
        await errors._safe_send(
            ctx, "boom", surface="command-invoke-error", error_id="cafebabe"
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "undeliverable" in message
    assert "cafebabe" in message
    assert "command-invoke-error" in message
    assert "anilist login" in message


async def test_rung_4_warning_carries_the_traceback(caplog):
    """Triage needs to know WHY, not just that it failed."""
    ctx = _ctx(
        send_error=_http_error(404, UNKNOWN_INTERACTION),
        channel_error=_http_error(404, 10003),
    )

    with caplog.at_level(logging.WARNING, logger=errors.log.name):
        await errors._safe_send(ctx, "boom", surface="unit")

    warning = [r for r in caplog.records if r.levelno == logging.WARNING][0]
    assert warning.exc_info is not None


async def test_rung_4_survives_a_half_built_context(caplog):
    """The give-up line is the last thing standing; it must not raise either.

    ``ctx.command`` is None on CommandNotFound, and a Context can reach the
    handler before every attribute is populated. An AttributeError while
    building the WARNING would hand the caller exactly the raise this helper
    exists to prevent.
    """
    ctx = types.SimpleNamespace(send=_Recorder(_http_error(403, 50013)))

    with caplog.at_level(logging.WARNING, logger=errors.log.name):
        await errors._safe_send(ctx, "boom", surface="unit")  # must not raise

    assert "undeliverable" in caplog.text


async def test_a_successful_reply_logs_no_warning(caplog):
    ctx = _ctx()

    with caplog.at_level(logging.WARNING, logger=errors.log.name):
        await errors._safe_send(ctx, "boom", surface="unit")

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_channel_rescue_logs_no_warning(caplog):
    """The fallback WORKED: the user has the report, so nothing is wrong."""
    ctx = _ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))

    with caplog.at_level(logging.WARNING, logger=errors.log.name):
        await errors._safe_send(ctx, "boom", surface="unit")

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# The error_id bridge: it is in the log BEFORE any send is attempted
# ---------------------------------------------------------------------------


def _handler_ctx(**kwargs):
    """A cog + ctx wired like the real handler, for end-to-end branch tests."""
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"), on_command_error=None
    )
    cog = errors.Errors(bot)
    ctx = _ctx(**kwargs)
    command = types.SimpleNamespace(
        qualified_name="anilist login", cog_name="AniList", signature=""
    )
    command.__str__ = lambda self: "anilist login"
    ctx.command = command
    ctx.cog = None
    ctx.bot = bot
    ctx.prefix = "/"
    ctx.me = types.SimpleNamespace(mention="<@1>")
    ctx.invoked_with = "login"
    ctx.message = types.SimpleNamespace(created_at=discord.utils.utcnow())
    ctx.author.display_avatar = types.SimpleNamespace(url="https://example.com/a.png")
    return cog, ctx


async def test_error_id_is_logged_even_when_the_user_sees_nothing(monkeypatch, caplog):
    """The whole point of the id: "the user saw nothing" stays diagnosable.

    Every transport refuses, so the member gets no message at all - yet the
    ERROR line with the error_id and the full traceback was already written
    before the first send was attempted, and the give-up WARNING repeats the id.
    """
    cog, ctx = _handler_ctx(
        send_error=_http_error(404, UNKNOWN_INTERACTION),
        channel_error=_http_error(403, 50013),
    )
    monkeypatch.setattr(errors.secrets, "token_hex", lambda _size: "cafebabe")

    with caplog.at_level(logging.DEBUG, logger=errors.log.name):
        await cog._on_command_error(
            ctx, commands.CommandInvokeError(RuntimeError("anilist token refresh"))
        )

    assert ctx.recorded.send.calls and ctx.recorded.channel.calls
    errors_logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors_logged) == 1
    assert "cafebabe" in errors_logged[0].getMessage()
    assert "anilist token refresh" in caplog.text
    warning = [r for r in caplog.records if r.levelno == logging.WARNING][0]
    assert "cafebabe" in warning.getMessage()


async def test_the_error_id_is_logged_before_the_send_is_attempted(monkeypatch):
    """Ordering guard: log first, then try to reply.

    Inverting these would lose the traceback of an unhandled error whenever the
    reply path raises - which is exactly what a dead interaction does.
    """
    order = []
    cog, ctx = _handler_ctx()
    monkeypatch.setattr(errors.secrets, "token_hex", lambda _size: "cafebabe")
    monkeypatch.setattr(
        errors.log, "error", lambda *a, **kw: order.append("log"), raising=True
    )

    async def send(*args, **kwargs):
        order.append("send")

    ctx.send = send

    await cog._on_command_error(ctx, commands.CommandInvokeError(RuntimeError("x")))

    assert order == ["log", "send"]


async def test_the_incident_replay_user_still_gets_the_error_id(monkeypatch):
    """Full replay of 2026-08-25: /anilist login, dead token, 10062 on the reply.

    Before the fix the user received nothing and the log gained a second,
    confusing traceback. Now the report lands in the channel, and it still
    carries the id the log is keyed by.
    """
    cog, ctx = _handler_ctx(send_error=_http_error(404, UNKNOWN_INTERACTION))
    monkeypatch.setattr(errors.secrets, "token_hex", lambda _size: "cafebabe")

    await cog._on_command_error(
        ctx, commands.CommandInvokeError(discord.NotFound.__new__(discord.NotFound))
    )

    (args, kwargs) = ctx.recorded.channel.calls[0]
    assert args[0] == "<@42>"
    assert "cafebabe" in kwargs["embed"].fields[0].value


async def test_no_branch_can_raise_out_of_the_handler(monkeypatch):
    """Blanket check across the branches: none of them re-raises on a dead token.

    ``on_command_error`` raising is what produced the second traceback and the
    "Ignoring exception in on_command_error" line in the incident log.
    """
    cases = [
        commands.MissingRequiredArgument(
            types.SimpleNamespace(name="user", displayed_name=None)
        ),
        commands.BadArgument("nope"),
        commands.CommandOnCooldown(
            commands.Cooldown(1, 60), 30.0, commands.BucketType.default
        ),
        discord.Forbidden(
            types.SimpleNamespace(status=403, reason="", headers={}),
            {"code": 50013, "message": "no"},
        ),
        commands.NoPrivateMessage(),
        commands.TooManyArguments(),
        commands.MissingPermissions(["manage_guild"]),
        commands.BotMissingPermissions(["send_messages"]),
        commands.CheckFailure(),
        commands.CommandInvokeError(RuntimeError("boom")),
    ]

    monkeypatch.setattr(
        errors.arg_completion,
        "start",
        _Recorder(RuntimeError("no interactive form")),
        raising=False,
    )

    for error in cases:
        cog, ctx = _handler_ctx(
            send_error=_http_error(404, UNKNOWN_INTERACTION),
            channel_error=_http_error(403, 50013),
        )
        await cog._on_command_error(ctx, error)  # must not raise
        assert ctx.recorded.send.calls, f"{type(error).__name__} sent nothing at all"
