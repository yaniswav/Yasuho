"""Structural guard: no cog may listen for a cache-dependent message event
without a written reason.

THE BUG THIS GUARDS AGAINST
----------------------------
discord.py dispatches ``on_message_edit`` only when the edited message is
still sitting in ``ConnectionState``'s message cache
(``parse_message_update``), and dispatches ``on_message_delete`` only when the
deleted message was still cached at the time it was removed
(``parse_message_delete``). This bot never passes ``max_messages``, so that
cache is the default 1000 entries BOT-WIDE across roughly 185 guilds - it
turns over in seconds to minutes of real traffic. ``cogs/moderation/modlog.py``
listened on ``on_message_edit`` for a long time: most edits were simply never
logged, with no error and no sign anything was missing, and a moderator
reading the mod-log had every reason to believe it was complete. The same bug
was fixed once already in ``cogs/moderation/automod.py`` (``on_raw_message_edit``
replacing a plain ``on_message_edit`` content-filter bypass) - this guard is
what stops it from growing back a second or third time, in either cog or in a
new one not written yet.

The guard is not "ban the cached event outright": ``on_raw_message_delete``
cannot be attributed (no author, no content) the way ``on_raw_message_edit``
can, so a listener that genuinely needs only what the CACHED event carries is
legitimate - it just has to say so, here, in a place a reviewer will see it
next to every other such decision. ``on_message_edit`` has no such excuse
(``on_raw_message_edit`` carries everything the cached event does, plus the
cases it misses), so its exempt dict starts, and should stay, empty.

NEGATIVE CONTROLS
------------------
A scan whose only possible result is silence is worthless unless something
proves it can also NOT be silent. ``test_the_detector_flags_a_synthetic_*``
feeds the extracted scanner (:func:`_scan_tree`) a fabricated class with
exactly the listener it exists to catch, off disk entirely, and asserts it is
reported. ``test_the_real_scan_examines_more_than_a_handful_of_modules`` pins
that the real, on-disk scan is not accidentally walking zero files (an empty
glob and a vacuous pass look identical otherwise).

AST, not regex: a regex over source text cannot tell ``def on_message_edit``
(a listener discord.py will call) from a string literal, a comment, or a
helper method of the same name on a class discord.py never asks about. Every
hit here is a real method defined directly in a class body somewhere under
``cogs/``.
"""

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COGS_DIR = _REPO_ROOT / "cogs"

# ---------------------------------------------------------------------------
# The exempt lists - every entry MUST carry a reason, checked below.
# ---------------------------------------------------------------------------

# on_message_edit: nothing in this tree should use it. on_raw_message_edit
# (discord.py 2.5+) carries a full payload.message for EVERY MESSAGE_UPDATE,
# cached or not, so it is a strict superset of what the cached event offers -
# there is no legitimate reason left to prefer the cached-only listener.
# Started, and meant to stay, empty.
EXEMPT_MESSAGE_EDIT = {}

# on_message_delete: the raw event (on_raw_message_delete) carries only IDs -
# no author, no content - so anything that needs either cannot move to it.
# Two listeners in this tree are genuinely cached-only for that reason; each
# reason is also stated as a docstring at the listener itself.
EXEMPT_MESSAGE_DELETE = {
    "cogs.moderation.modlog:ModLog.on_message_delete": (
        "an uncached delete payload carries neither an author nor content, "
        "so the log entry could not be attributed, and without an author "
        "the bot cannot even run its own 'skip bot messages' guard - every "
        "bot-initiated cleanup in the guild (ticket transcripts, panel "
        "refreshes, moderation's own replaced embeds) would flood the log "
        "channel. Covering the uncached case needs a dedicated bounded "
        "content cache - a separate feature, not a one-listener fix."
    ),
    "cogs.utility.utility:Utility.on_message_delete": (
        "the snipe command shows the deleted message's CONTENT, which exists "
        "only for a message discord.py still had cached at delete time; "
        "there is nothing for a raw, uncached delete to show."
    ),
}


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------
def _scan_tree(module, tree, listener_name):
    """``{'module:Class.method': lineno}`` for every ``listener_name`` method
    defined directly in a class body of ``tree``."""

    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == listener_name
            ):
                found[f"{module}:{node.name}.{item.name}"] = item.lineno
    return found


def find_listeners(listener_name, root=_COGS_DIR):
    """The real, on-disk scan: every cog module under ``cogs/``.

    Returns ``(found, modules_scanned)`` so a caller can assert the scan was
    not vacuous.
    """

    found = {}
    modules_scanned = 0
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        modules_scanned += 1
        module = ".".join(
            path.relative_to(_REPO_ROOT).with_suffix("").parts
        )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the repo compiles in CI
            continue
        found.update(_scan_tree(module, tree, listener_name))
    return found, modules_scanned


def _assert_every_hit_is_explained(found, exempt, event_name):
    unexplained = sorted(set(found) - set(exempt))
    assert not unexplained, (
        f"{event_name} is cache-dependent (discord.py dispatches it only "
        "when the message survived in the bot-wide 1000-entry cache) - move "
        "to the raw event or add a written reason to the EXEMPT dict in "
        "tests/test_message_cache_listener_hygiene.py:\n  "
        + "\n  ".join(unexplained)
    )
    stale = sorted(set(exempt) - set(found))
    assert not stale, (
        f"stale EXEMPT entry for {event_name} - the listener it excuses no "
        f"longer exists, remove the entry:\n  " + "\n  ".join(stale)
    )
    empty_reasons = sorted(k for k, v in exempt.items() if not v.strip())
    assert not empty_reasons, (
        f"EXEMPT entry with no actual reason: {empty_reasons}"
    )


# ---------------------------------------------------------------------------
# Negative controls - prove the detector is not vacuously green
# ---------------------------------------------------------------------------
def test_the_detector_flags_a_synthetic_on_message_edit_listener():
    source = (
        "class Haunted:\n"
        "    def on_message_edit(self, before, after):\n"
        "        pass\n"
    )
    found = _scan_tree("synthetic", ast.parse(source), "on_message_edit")
    assert found == {"synthetic:Haunted.on_message_edit": 2}


def test_the_detector_flags_a_synthetic_on_message_delete_listener():
    source = (
        "class Haunted:\n"
        "    async def on_message_delete(self, message):\n"
        "        pass\n"
    )
    found = _scan_tree("synthetic", ast.parse(source), "on_message_delete")
    assert found == {"synthetic:Haunted.on_message_delete": 2}


def test_the_detector_does_not_flag_a_differently_named_method():
    source = (
        "class Clean:\n"
        "    def on_message_edit_summary(self, before, after):\n"
        "        pass\n"
        "    def handle_on_message_edit(self, before, after):\n"
        "        pass\n"
    )
    found = _scan_tree("synthetic", ast.parse(source), "on_message_edit")
    assert found == {}


def test_the_real_scan_examines_more_than_a_handful_of_modules():
    """A scan over zero files and a scan that found nothing look identical
    unless something separately counts what was examined."""

    _, modules_scanned = find_listeners("on_message_edit")
    assert modules_scanned > 10, (
        f"only {modules_scanned} modules scanned under cogs/ - the glob is "
        "probably broken, not the codebase"
    )


# ---------------------------------------------------------------------------
# The actual guards
# ---------------------------------------------------------------------------
def test_no_on_message_edit_listener_without_an_exempt_reason():
    found, modules_scanned = find_listeners("on_message_edit")
    assert modules_scanned > 10
    _assert_every_hit_is_explained(found, EXEMPT_MESSAGE_EDIT, "on_message_edit")


def test_no_on_message_delete_listener_without_an_exempt_reason():
    found, modules_scanned = find_listeners("on_message_delete")
    assert modules_scanned > 10
    _assert_every_hit_is_explained(
        found, EXEMPT_MESSAGE_DELETE, "on_message_delete"
    )
