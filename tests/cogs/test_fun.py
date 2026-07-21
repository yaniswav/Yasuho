"""Unit tests for the image-render/CPU-offload hardening in ``cogs/fun/fun.py``.

Both ``give_hug`` (Pillow GIF render) and ``ascii`` (pyfiglet) must offload
their blocking work through ``tools.rendering.run_image_job`` (the shared
``bot.image_render_semaphore``-gated executor) rather than a raw, uncapped
``run_in_executor`` - see the welcome-card / rank-card precedent in
``cogs/config/welcome.py`` and ``cogs/community/leveling.py``. ``ascii`` must
also carry a cooldown, since it previously ran pyfiglet synchronously on the
event loop with none at all.
"""

import io
import types

import cogs.fun.fun as fun_module
from cogs.fun.fun import Fun


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Ctx:
    def __init__(self):
        self.sends = []
        self.author = types.SimpleNamespace(display_name="Author")
        self.invoked_subcommand = None

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _Member:
    def __init__(self, name="Target"):
        self.display_name = name


def _make_cog(bot=None):
    return Fun(bot if bot is not None else types.SimpleNamespace())


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
