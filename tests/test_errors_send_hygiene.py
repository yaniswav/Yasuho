"""Structural guard: the global error handler may only send through ``_safe_send``.

Background (verified prod incident, 2026-08-25 03:23:03): ``/anilist login``
failed with ``discord.errors.NotFound: 404 (error code 10062): Unknown
interaction`` - the interaction token had died on the 3-second initial-response
deadline. ``cogs/system/errors.py`` logged the crash correctly with an error_id,
then tried to tell the user with a bare ``await ctx.send(...)`` on that SAME dead
interaction. That raised 10062 too, discord.py logged "Ignoring exception in
on_command_error", and the user got NOTHING - not even an error message. Thirteen
branches of that module had the identical shape.

The fix is one guarded helper (``_safe_send``) that never raises and walks a
fallback ladder ending in a WARNING. A helper is only worth anything if it cannot
be bypassed, so this module walks the AST of ``cogs/system/errors.py`` and fails
if ANY ``.send`` / ``.reply`` / ``.send_message`` call in it lives outside the
helper.

A guard whose success is silence is worthless, so the guard is proved three ways
before it is trusted on the real file:

1. a NEGATIVE CONTROL - synthetic sources with a bare send that the detector MUST
   report, one per attribute name it claims to police;
2. a non-vacuity check on the real module - the sends inside ``_safe_send`` are
   found, so the scan really does see sends and the allow-list is doing work
   rather than the file simply containing none;
3. a converted-sites count, so silently deleting a branch's reply cannot pass as
   "no bare sends found".

SCOPE, STATED (what this guard does NOT cover)
----------------------------------------------
* ONE file: ``cogs/system/errors.py``. The handler also delegates the
  MissingRequiredArgument branch to ``arg_completion.start``, which sends from
  its own module and is therefore invisible here. That is a chosen boundary, not
  an oversight: the call site wraps it in ``try/except Exception`` and falls
  through to the usage text, so it cannot raise out of the handler either -
  pinned at runtime by ``test_no_branch_can_raise_out_of_the_handler`` in
  ``tests/cogs/test_errors_safe_send.py``, which makes that helper explode.
* An ALIASED send: ``dm = ctx.author.send`` then ``await dm(...)`` matches no
  ``ast.Attribute`` at the call site and is not reported. Nothing in the module
  writes that shape, and it is not a plausible accidental reintroduction (the
  incident's shape was a plain ``await ctx.send(...)``), but the limit is real
  and stated rather than discovered later.

No network, no database, no Discord: this module only reads a file and parses it.
"""

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TARGET = _REPO_ROOT / "cogs" / "system" / "errors.py"

# The attribute names that put a message in front of a user. ``send`` covers
# ``ctx.send``, ``ctx.channel.send`` and ``interaction.followup.send``;
# ``send_message`` covers ``interaction.response.send_message``; ``reply`` covers
# ``ctx.reply``. Each one is a way to reintroduce the incident.
_SEND_ATTRS = frozenset({"send", "reply", "send_message"})

# The ONE function allowed to hold a raw send. Everything else must route
# through it. Adding a name here is a deliberate, reviewable act.
_ALLOWED_FUNCTIONS = frozenset({"_safe_send"})

# The handler branches that must each deliver their report through the helper:
# CommandNotFound, MissingRequiredArgument, BadArgument, CommandOnCooldown,
# Forbidden, NoPrivateMessage, TooManyArguments, UserInputError,
# MissingPermissions, CommandInvokeError, BotMissingPermissions, CheckFailure,
# and the catch-all else. Thirteen at the time of the fix.
_CONVERTED_SITES = 13


class _SendScanner(ast.NodeVisitor):
    """Collect every send-like call, split by whether it sits inside the helper.

    Tracks the lexical stack of enclosing function definitions, so a send is
    exempt only when one of the functions it is nested in is allow-listed. A
    nested closure inside ``_safe_send`` is therefore covered too, while a helper
    called BY ``_safe_send`` is not - it would have to be added explicitly.
    """

    def __init__(self, allowed=_ALLOWED_FUNCTIONS, attrs=_SEND_ATTRS):
        self.allowed = frozenset(allowed)
        self.attrs = frozenset(attrs)
        self._stack = []
        self.bare = []
        self.guarded = []

    def _visit_function(self, node):
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in self.attrs:
            label = f"line {node.lineno}: {ast.unparse(func)}"
            bucket = (
                self.guarded
                if self.allowed.intersection(self._stack)
                else self.bare
            )
            bucket.append(label)
        self.generic_visit(node)


def scan_sends(source, *, allowed=_ALLOWED_FUNCTIONS, attrs=_SEND_ATTRS):
    """Return ``(bare, guarded)`` send call labels found in ``source``.

    Pure: parses a string, touches nothing. ``bare`` is the failure list - sends
    that bypass the guarded helper. ``guarded`` exists so a caller can prove the
    scan is not silently finding nothing at all.
    """
    scanner = _SendScanner(allowed=allowed, attrs=attrs)
    scanner.visit(ast.parse(source))
    return scanner.bare, scanner.guarded


def count_calls(source, name):
    """How many times ``name(...)`` is called as a plain function in ``source``."""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _target_source():
    return _TARGET.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) NEGATIVE CONTROL: the detector must report a bare send
# ---------------------------------------------------------------------------

_BARE_CTX_SEND = '''
async def handle(ctx, error):
    await ctx.send(embed=build(ctx))
'''

_BARE_CTX_REPLY = '''
async def handle(ctx, error):
    await ctx.reply("boom")
'''

_BARE_RESPONSE_SEND_MESSAGE = '''
async def handle(interaction):
    await interaction.response.send_message("boom")
'''

_BARE_SEND_IN_A_METHOD = '''
class Errors:
    async def _on_command_error(self, ctx, error):
        await ctx.send("boom")
'''

_BARE_SEND_AT_MODULE_LEVEL = '''
import asyncio
asyncio.run(ctx.send("boom"))
'''


def test_negative_control_bare_ctx_send_is_reported():
    """THE control. If this ever passes silently the guard is decorative."""
    bare, guarded = scan_sends(_BARE_CTX_SEND)
    assert len(bare) == 1, bare
    assert "ctx.send" in bare[0]
    assert guarded == []


def test_negative_control_bare_ctx_reply_is_reported():
    bare, _guarded = scan_sends(_BARE_CTX_REPLY)
    assert len(bare) == 1 and "ctx.reply" in bare[0], bare


def test_negative_control_bare_response_send_message_is_reported():
    bare, _guarded = scan_sends(_BARE_RESPONSE_SEND_MESSAGE)
    assert len(bare) == 1, bare
    assert "interaction.response.send_message" in bare[0]


def test_negative_control_bare_send_inside_a_method_is_reported():
    """The real offenders lived in a method, not a module-level function."""
    bare, _guarded = scan_sends(_BARE_SEND_IN_A_METHOD)
    assert len(bare) == 1 and "ctx.send" in bare[0], bare


def test_negative_control_bare_send_outside_any_function_is_reported():
    """Nothing on the function stack means nothing allow-listed: still bare."""
    bare, _guarded = scan_sends(_BARE_SEND_AT_MODULE_LEVEL)
    assert len(bare) == 1 and "ctx.send" in bare[0], bare


def test_negative_control_reports_the_true_line_number():
    """The failure message must point at the offending line, not just say "1"."""
    source = "\n\n\n\nasync def handle(ctx):\n    await ctx.send('boom')\n"
    bare, _guarded = scan_sends(source)
    assert bare == ["line 6: ctx.send"]


# ---------------------------------------------------------------------------
# (2) The allow-list is what exempts a send - not luck
# ---------------------------------------------------------------------------

_SENDS_INSIDE_THE_HELPER = '''
async def _safe_send(ctx, content=None):
    try:
        await ctx.send(content)
    except Exception:
        await ctx.channel.send(content)


async def handle(ctx, error):
    await _safe_send(ctx, "boom")
'''


def test_sends_inside_the_helper_are_exempt():
    bare, guarded = scan_sends(_SENDS_INSIDE_THE_HELPER)
    assert bare == []
    assert len(guarded) == 2, guarded


def test_the_same_sends_are_bare_when_the_helper_is_renamed():
    """Proof the exemption comes from the allow-list, not from the call shape.

    Same source, empty allow-list: both sends must flip to bare. If they did
    not, the detector would be exempting them for some accidental reason and the
    green tick on the real module would mean nothing.
    """
    bare, guarded = scan_sends(_SENDS_INSIDE_THE_HELPER, allowed=frozenset())
    assert len(bare) == 2, bare
    assert guarded == []


def test_a_sibling_helper_does_not_inherit_the_exemption():
    """Only ``_safe_send`` itself is exempt; a function it calls is not.

    Otherwise the ladder could be quietly moved into an unguarded helper and the
    module would pass while raising again.
    """
    source = '''
async def _channel_fallback(ctx, content):
    await ctx.channel.send(content)


async def _safe_send(ctx, content):
    await _channel_fallback(ctx, content)
'''
    bare, _guarded = scan_sends(source)
    assert len(bare) == 1 and "ctx.channel.send" in bare[0], bare


# ---------------------------------------------------------------------------
# (3) The real module
# ---------------------------------------------------------------------------


def test_scan_is_not_vacuous_on_the_real_module():
    """The real ``_safe_send`` holds raw sends, and the scan finds them.

    Without this, ``bare == []`` below could just mean the scanner is broken or
    the attribute names drifted, and nobody would notice.
    """
    bare, guarded = scan_sends(_target_source())
    assert guarded, (
        "No send call was found inside _safe_send - either the helper stopped "
        "sending or the scanner no longer recognises sends. Either way the "
        "no-bare-send assertion below is vacuous."
    )
    assert len(guarded) >= 3, guarded  # ctx.send, followup.send, channel.send
    assert bare == []


def test_errors_module_has_no_bare_send():
    """THE guard: every reply in the error handler goes through ``_safe_send``.

    A bare ``ctx.send`` here is the 2026-08-25 incident: a dead interaction makes
    the error reporter itself raise, discord.py swallows it as "Ignoring
    exception in on_command_error", and the user is left with no message at all.
    """
    bare, _guarded = scan_sends(_target_source())
    assert bare == [], (
        "Unguarded send(s) in cogs/system/errors.py - these can raise on a dead "
        "interaction (10062), an already-acknowledged one (40060), a channel the "
        "bot cannot write in (403) or a deleted channel (404), and would leave "
        "the user with nothing. Route each through _safe_send(ctx, ...):\n  "
        + "\n  ".join(bare)
    )


def test_every_branch_still_routes_through_the_helper():
    """Count guard: deleting a branch's reply must not read as "no bare sends".

    ``bare == []`` is also true of a handler that answers nobody. Thirteen sites
    were converted; the count may only grow as branches are added.
    """
    calls = count_calls(_target_source(), "_safe_send")
    assert calls >= _CONVERTED_SITES, (
        f"only {calls} _safe_send call sites remain, expected at least "
        f"{_CONVERTED_SITES} - a branch lost its user-facing reply"
    )


def test_helper_exists_and_is_async():
    """The allow-list name must match a real coroutine, or it exempts nothing."""
    tree = ast.parse(_target_source())
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_safe_send"
    ]
    assert len(helpers) == 1, "cogs/system/errors.py must define one async _safe_send"
