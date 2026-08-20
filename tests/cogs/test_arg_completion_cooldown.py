"""The cooldown the interactive argument-completion form is allowed to spend.

THE BUG THIS PINS. When a prefix command is invoked with a required argument
missing, discord.py has ALREADY charged its cooldown (``Command.prepare`` bills
before it parses), and the rebuilt command line is then re-invoked through the
full ``bot.invoke``, which bills again. The flow used to square that by calling
``reset_cooldown`` - which does not repay one token, it refills the bucket.

On any command with ``rate >= 2`` that is an unlimited-use bypass driven
entirely by the member: invoke with an argument missing (charge 1), let the form
refill the bucket, let the rebuilt line charge 1, and the bucket never reaches
zero however many times the cycle runs. (``rate == 1`` was accidentally safe:
the next cycle's first attempt is refused by the cooldown before parsing, so no
form is ever offered - which is why the tests below use rate 2.)

Everything here drives the REAL discord.py cooldown machinery: a real
``commands.Command`` carrying a real ``CooldownMapping``, billed through the
library's own ``_prepare_cooldowns``. A fake bucket would prove nothing about
the arithmetic that actually runs in production.
"""

import datetime
import types

from discord.ext import commands

from cogs.system import arg_completion

UTC = datetime.timezone.utc
AUTHOR_ID = 4242
OTHER_AUTHOR_ID = 909
EPOCH = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A real command, a real bucket, and the two halves of one completion cycle
# ---------------------------------------------------------------------------


def _command(rate=2, per=3600.0):
    @commands.cooldown(rate, per, commands.BucketType.user)
    async def demo(ctx, target: str):  # pragma: no cover - never called
        return None

    return commands.Command(demo, name="demo")


def _message(offset_seconds):
    """A message stand-in with the two fields discord.py bills against."""
    return types.SimpleNamespace(
        id=1000 + int(offset_seconds),
        content="?demo",
        created_at=EPOCH + datetime.timedelta(seconds=offset_seconds),
        edited_at=None,
    )


def _ctx(message, command, bot, author_id=AUTHOR_ID):
    return types.SimpleNamespace(
        message=message,
        author=types.SimpleNamespace(id=author_id),
        guild=None,
        channel=None,
        command=command,
        bot=bot,
        prefix="?",
    )


class _Bot:
    """A bot whose ``invoke`` does the one thing that matters here: bill.

    ``Command.prepare`` charges the cooldown on every invocation, including the
    rebuilt one, which is the whole point of re-invoking through ``bot.invoke``
    rather than through ``reinvoke`` - every check runs again. So the stand-in
    calls the library's own ``_prepare_cooldowns`` and records which of the two
    outcomes it got.
    """

    def __init__(self, command, author_id=AUTHOR_ID):
        self.command = command
        self.author_id = author_id
        self.ran = 0
        self.refused = 0

    async def get_context(self, message):
        return _ctx(message, self.command, self, self.author_id)

    async def invoke(self, ctx):
        try:
            ctx.command._prepare_cooldowns(ctx)
        except commands.CommandOnCooldown:
            self.refused += 1
            return
        self.ran += 1


def _view(ctx):
    """The three attributes ``_CompletionView._reinvoke`` actually reads."""
    view = object.__new__(arg_completion._CompletionView)
    view.ctx = ctx
    view.command = ctx.command
    view.provided = {"target"}
    return view


def _attempt(bot, message, author_id=AUTHOR_ID):
    """The invocation that fails on a missing argument.

    Returns the context when the command got far enough to raise
    ``MissingRequiredArgument`` (i.e. it was billed and a form would be
    offered), or ``None`` when the cooldown refused it before parsing - in which
    case the member never sees a form at all.
    """
    ctx = _ctx(message, bot.command, bot, author_id)
    try:
        bot.command._prepare_cooldowns(ctx)
    except commands.CommandOnCooldown:
        return None
    return ctx


async def _cycle(bot, offset_seconds, author_id=AUTHOR_ID):
    """One full use of the form: a billed failed attempt, then the rebuild.

    Returns False when the cooldown refused the attempt outright.
    """
    ctx = _attempt(bot, _message(offset_seconds), author_id)
    if ctx is None:
        return False
    await arg_completion._CompletionView._reinvoke(_view(ctx), "?demo value")
    return True


def _tokens(command, bot, offset_seconds=0):
    ctx = _ctx(_message(offset_seconds), command, bot)
    current = ctx.message.created_at.timestamp()
    return command._buckets.get_bucket(ctx, current).get_tokens(current)


# ---------------------------------------------------------------------------
# The bypass
# ---------------------------------------------------------------------------


async def test_the_completion_form_cannot_be_used_to_reset_a_cooldown():
    """THE regression, at rate 2: three cycles must not fit in two tokens.

    With the bucket reset, the third attempt still gets a form and still runs
    the command - and so does the four-hundredth, because a cycle costs a net
    zero tokens and the bucket can never drain.
    """
    bot = _Bot(_command(rate=2, per=3600.0))

    assert await _cycle(bot, 0) is True  # token 1 of 2
    assert await _cycle(bot, 5) is True  # token 2 of 2
    assert await _cycle(bot, 10) is False  # exhausted, and no form is offered

    assert bot.ran == 2
    assert bot.refused == 0  # the refusal happened before the form, not after


async def test_a_completion_cycle_costs_exactly_what_typing_it_out_costs():
    """One token, once - not two (the member would be overcharged for using the
    form) and not zero (the hole)."""
    typed_command = _command(rate=3, per=3600.0)
    typed_bot = _Bot(typed_command)
    ctx = _ctx(_message(0), typed_command, typed_bot)
    typed_command._prepare_cooldowns(ctx)  # a plain, complete invocation

    form_command = _command(rate=3, per=3600.0)
    form_bot = _Bot(form_command)
    await _cycle(form_bot, 0)

    assert _tokens(form_command, form_bot) == _tokens(typed_command, typed_bot)


async def test_a_command_that_bills_after_parsing_is_not_handed_a_free_token():
    """``cooldown_after_parsing`` commands parse first, so a missing argument
    costs them NOTHING. Refunding there would not repay a debt, it would mint a
    token - so an uncharged bucket must come out of the flow untouched."""
    bot = _Bot(_command(rate=1, per=3600.0))
    message = _message(0)
    # No _attempt(): this command never billed for the failed parse.
    ctx = _ctx(message, bot.command, bot)

    await arg_completion._CompletionView._reinvoke(_view(ctx), "?demo value")

    assert bot.ran == 1
    assert _tokens(bot.command, bot, 1) == 0  # the rebuilt run spent the token
    assert _attempt(bot, _message(2)) is None  # ...and it is really gone


async def test_a_refund_never_lands_in_another_members_bucket():
    """The bucket key is taken from the CONTEXT, the way discord.py takes it, so
    the token can only ever come back to the member who paid it."""
    command = _command(rate=1, per=3600.0)
    bot = _Bot(command)

    assert await _cycle(bot, 0) is True
    assert bot.ran == 1

    # A different member, untouched by any of it, still has their own token.
    other = _attempt(bot, _message(1), OTHER_AUTHOR_ID)
    assert other is not None


async def test_a_window_that_rolled_over_is_not_credited_a_stale_token():
    """A form can sit open for three minutes. If the cooldown window expires
    while it does, the charge being 'repaid' has already expired on its own and
    the refund would be a free token on top of a fresh bucket."""
    command = _command(rate=2, per=30.0)
    bot = _Bot(command)

    ctx = _attempt(bot, _message(0))
    assert ctx is not None
    # The rebuilt line is billed at the SAME message timestamp discord.py uses,
    # so a stale window is exercised through the helper directly.
    assert arg_completion._refund_one_token(ctx) is True  # same window: owed

    late = _ctx(_message(120), command, bot)
    assert arg_completion._refund_one_token(late) is False  # rolled over: not


async def test_a_command_with_no_cooldown_still_reinvokes_cleanly():
    @commands.command(name="plain")
    async def plain(ctx, target: str):  # pragma: no cover - never called
        return None

    bot = _Bot(plain)
    ctx = _ctx(_message(0), plain, bot)

    await arg_completion._CompletionView._reinvoke(_view(ctx), "?plain value")

    assert bot.ran == 1


async def test_a_broken_refund_costs_a_token_never_the_invocation(monkeypatch):
    """Degrade in the safe direction: if the library seam ever moves, the member
    pays twice - the command still runs, and the bucket is still NOT cleared."""
    bot = _Bot(_command(rate=2, per=3600.0))

    def _boom(_ctx):
        raise RuntimeError("discord.py moved the bucket")

    monkeypatch.setattr(arg_completion, "_refund_one_token", _boom)

    ctx = _attempt(bot, _message(0))
    await arg_completion._CompletionView._reinvoke(_view(ctx), "?demo value")

    assert bot.ran == 1
    assert _tokens(bot.command, bot, 1) == 0  # both attempts paid, none refunded


def test_the_reset_that_caused_this_is_gone_for_good():
    """A guard on the shape of the fix, not just its effect: nothing on this
    path may reach for the whole bucket again."""
    import inspect

    source = inspect.getsource(arg_completion._CompletionView._reinvoke)
    assert "reset_cooldown" not in source
