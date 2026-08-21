"""Unit tests for the image-render/CPU-offload hardening in ``cogs/fun/fun.py``.

Both ``give_hug`` (Pillow GIF render) and ``ascii`` (pyfiglet) must offload
their blocking work through ``tools.rendering.run_image_job`` (the shared
``bot.image_render_semaphore``-gated executor) rather than a raw, uncapped
``run_in_executor`` - see the welcome-card / rank-card precedent in
``cogs/config/welcome.py`` and ``cogs/community/leveling/leveling.py``. ``ascii`` must
also carry a cooldown, since it previously ran pyfiglet synchronously on the
event loop with none at all.

Also covers the ?say hardening: the link filter must SEARCH the whole argument
(it was anchored, so a leading character published arbitrary links under the
bot's name), the invocation must be deleted BY ID rather than by purging
whatever is newest, and one member must not be able to start two hug renders at
once in the two-slot bot-wide image pool.
"""

import io
import types

import discord
import pytest

import cogs.fun.fun as fun_module
from cogs.fun.fun import Fun


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Message:
    """Just enough of discord.Message for the ?say paths."""

    def __init__(self, mention_everyone=False, delete_error=None):
        self.mention_everyone = mention_everyone
        self.author = types.SimpleNamespace(mention="<@1>")
        self.created_at = None
        self.deletes = 0
        self._delete_error = delete_error

    async def delete(self):
        self.deletes += 1
        if self._delete_error is not None:
            raise self._delete_error


class _Channel:
    def __init__(self):
        self.purges = []

    async def purge(self, **kwargs):
        self.purges.append(kwargs)
        return []


class _Ctx:
    def __init__(self, author_id=1, message=None):
        self.sends = []
        self.author = types.SimpleNamespace(
            display_name="Author", id=author_id, mention=f"<@{author_id}>"
        )
        self.invoked_subcommand = None
        self.message = _Message() if message is None else message
        self.channel = _Channel()

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _Member:
    def __init__(self, name="Target"):
        self.display_name = name


def _make_cog(bot=None):
    return Fun(bot if bot is not None else types.SimpleNamespace())


def _http_error(status=404, reason="Not Found"):
    """A real discord.HTTPException without needing a real HTTP response."""

    return discord.HTTPException(
        types.SimpleNamespace(status=status, reason=reason), "gone"
    )


# ---------------------------------------------------------------------------
# give_hug: Pillow GIF render must go through run_image_job.
# ---------------------------------------------------------------------------
async def test_give_hug_routes_through_run_image_job(monkeypatch):
    calls = []

    async def _fake_run_image_job(bot, function, *args, **kwargs):
        calls.append((bot, function, args, kwargs))
        return io.BytesIO(b"gif-bytes")

    monkeypatch.setattr(fun_module.rendering, "run_image_job", _fake_run_image_job)

    bot = types.SimpleNamespace()
    cog = _make_cog(bot)
    ctx = _Ctx()

    await cog.give_hug.callback(cog, ctx, _Member())

    assert len(calls) == 1
    bot_arg, function, args, kwargs = calls[0]
    assert bot_arg is bot
    assert callable(function)  # the closure that does the Pillow work
    assert len(ctx.sends) == 1
    _, send_kwargs = ctx.sends[0]
    assert send_kwargs["file"].filename == "hug.gif"


async def test_give_hug_falls_back_on_render_failure(monkeypatch):
    async def _boom(bot, function, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(fun_module.rendering, "run_image_job", _boom)

    cog = _make_cog()
    ctx = _Ctx()

    await cog.give_hug.callback(cog, ctx, _Member())

    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    assert "file" not in kwargs  # text fallback, not a broken attachment


# ---------------------------------------------------------------------------
# ascii: must have a cooldown AND offload pyfiglet through run_image_job.
# ---------------------------------------------------------------------------
def test_ascii_has_a_cooldown():
    cog = _make_cog()
    cooldown = cog.ascii._buckets._cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 5.0)


async def test_ascii_routes_pyfiglet_through_run_image_job(monkeypatch):
    calls = []

    async def _fake_run_image_job(bot, function, *args, **kwargs):
        calls.append((bot, function, args, kwargs))
        return function(*args, **kwargs)

    monkeypatch.setattr(fun_module.rendering, "run_image_job", _fake_run_image_job)

    bot = types.SimpleNamespace()
    cog = _make_cog(bot)
    ctx = _Ctx()

    await cog.ascii.callback(cog, ctx, msg="hi")

    assert len(calls) == 1
    bot_arg, function, args, kwargs = calls[0]
    assert bot_arg is bot
    assert function is fun_module.figlet_format
    assert args == ("hi",)
    assert kwargs == {"font": "big"}
    assert len(ctx.sends) == 1


async def test_ascii_output_unchanged(monkeypatch):
    """Routing through run_image_job must not alter the rendered art."""

    async def _passthrough(bot, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fun_module.rendering, "run_image_job", _passthrough)

    direct = fun_module.figlet_format("hi", font="big")

    cog = _make_cog()
    ctx = _Ctx()
    await cog.ascii.callback(cog, ctx, msg="hi")

    (args, kwargs) = ctx.sends[0]
    assert args[0] == f"```fix\n{direct}\n```"


# ---------------------------------------------------------------------------
# give_hug: one member must not be able to start two renders at once.
#
# A hug is ~1s of Pillow work in one of only TWO bot-wide image slots
# (tools/rendering.py). commands.cooldown(3, 90) bounds the member's VOLUME but
# its rate of 3 lets three invocations clear prepare() back to back, so all
# three renders start together and take the whole pool - starving rank cards,
# profile cards and serverstats charts bot-wide. fun.HUG_SPACING closes that.
# ---------------------------------------------------------------------------
def _counting_render_job(calls):
    async def _fake(bot, function, *args, **kwargs):
        calls.append(function)
        return io.BytesIO(b"gif-bytes")

    return _fake


async def test_give_hug_spacing_blocks_a_second_render(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fun_module.rendering, "run_image_job", _counting_render_job(calls)
    )

    cog = _make_cog()
    ctx = _Ctx(author_id=7)

    await cog.give_hug.callback(cog, ctx, _Member())
    await cog.give_hug.callback(cog, ctx, _Member("Other"))

    # Exactly one render, and the second invoke got a text refusal - never a
    # second image job.
    assert len(calls) == 1
    assert "file" in ctx.sends[0][1]
    assert "file" not in ctx.sends[1][1]


async def test_give_hug_spacing_is_per_member(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fun_module.rendering, "run_image_job", _counting_render_job(calls)
    )

    cog = _make_cog()

    await cog.give_hug.callback(cog, _Ctx(author_id=1), _Member())
    await cog.give_hug.callback(cog, _Ctx(author_id=2), _Member())

    # One member's spacing must never throttle anybody else.
    assert len(calls) == 2


async def test_give_hug_spacing_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fun_module.rendering, "run_image_job", _counting_render_job(calls)
    )

    cog = _make_cog()
    ctx = _Ctx(author_id=7)

    await cog.give_hug.callback(cog, ctx, _Member())
    # Age the entry past its window: this is a spacing, not a ban.
    cog._hug_spacing._seen[7] = (
        cog._hug_spacing._seen[7][0] - (fun_module.HUG_SPACING + 1),
        fun_module.HUG_SPACING,
    )
    await cog.give_hug.callback(cog, ctx, _Member())

    assert len(calls) == 2


async def test_give_hug_air_does_not_burn_the_spacing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fun_module.rendering, "run_image_job", _counting_render_job(calls)
    )

    cog = _make_cog()
    ctx = _Ctx(author_id=7)

    await cog.give_hug.callback(cog, ctx, None)  # "You can't hug the air..."
    await cog.give_hug.callback(cog, ctx, _Member())

    assert len(calls) == 1


def test_give_hug_keeps_its_volume_budget():
    """The spacing is additional to the per-user rate, not a replacement."""

    cog = _make_cog()
    cooldown = cog.give_hug._buckets._cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (3, 90.0)


# ---------------------------------------------------------------------------
# say: the link filter must look at the WHOLE argument.
#
# It used to be an anchored ^...$ pattern consulted with re.match, so any
# leading character defeated it and a member could publish arbitrary links as
# Yasuho - bot-authored phishing.
# ---------------------------------------------------------------------------
LINKY = [
    "hi http://evil.example/free-nitro",
    ".https://evil.example",
    "  https://evil.example",
    "read this https://evil.example/x then leave",
    "look discord.gg/abcdef",
    "join discord.me/xyz please",
    "here https://discord.com/invite/abcdef now",
]


@pytest.mark.parametrize("text", LINKY)
async def test_say_refuses_a_link_anywhere_in_the_message(text):
    cog = _make_cog()
    ctx = _Ctx()

    await cog.say.callback(cog, ctx, args=text)

    assert len(ctx.sends) == 1
    args, kwargs = ctx.sends[0]
    # A warning embed, never the member's text echoed under the bot's name.
    assert args == ()
    assert "embed" in kwargs


@pytest.mark.parametrize(
    "text", ["hello world", "i love you", "no links here at all", "3 > 2"]
)
async def test_say_still_echoes_ordinary_text(text):
    cog = _make_cog()
    ctx = _Ctx()

    await cog.say.callback(cog, ctx, args=text)

    args, kwargs = ctx.sends[0]
    assert args[0] == text
    assert ctx.message.deletes == 0  # nothing to take down


async def test_say_echo_can_never_ping():
    """mention_everyone is False for a member without the permission - but the
    bot has it, so the echo itself must carry AllowedMentions.none()."""

    cog = _make_cog()
    ctx = _Ctx()

    await cog.say.callback(cog, ctx, args="@everyone free nitro")

    _, kwargs = ctx.sends[0]
    mentions = kwargs.get("allowed_mentions")
    assert mentions is not None
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False


def test_say_patterns_are_the_automod_patterns_verbatim():
    """Drift guard: the ?say filter is automod's, copied rather than imported.

    Byte-identical sources, so 'links' means one thing bot-wide. If either side
    is ever tuned, this test turns red and the other side has to follow.
    """

    from cogs.moderation.automod import AutoMod

    assert fun_module.LINK_RE.pattern == AutoMod.url_re.pattern
    assert fun_module.INVITE_RE.pattern == AutoMod.invite_re.pattern


async def test_say_warning_defuses_the_quoted_message():
    """The warning embed quotes the offending text back under Yasuho's name, so
    it must be inert: unescaped, a rejected markdown link became a bot-authored
    clickable link inside the very warning about it."""

    cog = _make_cog()
    ctx = _Ctx()

    await cog.say.callback(cog, ctx, args="[free nitro](https://evil.example)")

    _, kwargs = ctx.sends[0]
    quoted = kwargs["embed"].fields[0].value.split("Message : ", 1)[1]
    assert quoted != "[free nitro](https://evil.example)"
    # escape_markdown backslashes the link syntax, so Discord renders the whole
    # thing as visible text instead of a link the bot appears to endorse.
    assert quoted.startswith("\\[")
    assert kwargs["allowed_mentions"].everyone is False


# ---------------------------------------------------------------------------
# say: take down THIS command, not "the newest message in the channel".
# ---------------------------------------------------------------------------
async def test_say_deletes_the_invocation_and_never_purges():
    cog = _make_cog()
    ctx = _Ctx()

    await cog.say.callback(cog, ctx, args="hi https://evil.example")

    assert ctx.message.deletes == 1
    # purge(limit=1) removed whatever was most recent, which a member posting
    # in the meantime could win - deleting somebody else's message and leaving
    # the offending ?say in place.
    assert ctx.channel.purges == []


async def test_say_warns_even_when_the_invocation_is_already_gone():
    ctx = _Ctx(message=_Message(delete_error=_http_error()))
    cog = _make_cog()

    await cog.say.callback(cog, ctx, args="hi https://evil.example")

    assert ctx.message.deletes == 1
    assert "embed" in ctx.sends[0][1]


async def test_say_mention_everyone_path_also_deletes_by_id():
    ctx = _Ctx(message=_Message(mention_everyone=True))
    cog = _make_cog()

    await cog.say.callback(cog, ctx, args="@everyone hi")

    assert ctx.message.deletes == 1
    assert ctx.channel.purges == []
    assert "embed" in ctx.sends[0][1]
