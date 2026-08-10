"""``enable_debug_events`` and its listeners must agree, in both directions.

discord.py only dispatches ``on_socket_raw_receive`` / ``on_socket_event_type``
when the client is built with ``enable_debug_events=True``. The bot used to pass
it while listening for NEITHER: that is a full event dispatch (raw payload
string included) for every single inbound gateway packet, on the busiest path
there is, into an empty listener list. The flag is gone.

Which leaves a trap for whoever adds such a listener next: with the flag off it
would simply never fire, silently, and look like a broken feature rather than a
missing option. So this guard is symmetric - the flag and the listeners must
appear together or not at all.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module the bot loads, found by walking rather than by listing: naming the
# roots (it used to say "cogs" and "tools" plus core.py) makes the guard as
# asymmetric as the bug it exists to prevent - a listener dropped in any other
# top-level module would simply not be looked for. Skipped: this suite itself
# (which spells the event names out in prose), the virtualenv, and the data /
# runtime directories that hold no bot code.
SKIP_DIRS = frozenset(
    (
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "backups",
        "config",
        "lavalink",
        "locales",
        "logs",
        "ressources",
        "tests",
    )
)

# The events that only exist when the debug flag is set.
DEBUG_EVENTS = ("on_socket_raw_receive", "on_socket_event_type", "on_socket_raw_send")

# A listener is a `def on_socket_...` / `async def on_socket_...`, never a mention
# of the name in prose (this file, and core.py's own note, say all three).
_LISTENER = re.compile(
    r"^\s*(?:async\s+)?def\s+(" + "|".join(DEBUG_EVENTS) + r")\s*\(", re.MULTILINE
)


def _python_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _debug_flag_is_on():
    source = _read(os.path.join(REPO_ROOT, "core.py"))
    return re.search(r"^\s*enable_debug_events\s*=\s*True", source, re.MULTILINE)


def test_no_module_listens_for_an_event_the_gateway_never_dispatches():
    listeners = [
        (os.path.relpath(path, REPO_ROOT), match.group(1))
        for path in _python_files()
        for match in _LISTENER.finditer(_read(path))
    ]

    if _debug_flag_is_on():
        # The flag is back on: listeners are legitimate, and the flag must be
        # earning its keep rather than sitting there for nobody.
        assert listeners, (
            "core.py sets enable_debug_events=True but nothing listens for "
            "any of " + ", ".join(DEBUG_EVENTS) + " - that is a per-packet "
            "dispatch into an empty listener list."
        )
        return

    assert not listeners, (
        "these listeners can never fire: core.py does not set "
        "enable_debug_events, so discord.py dispatches none of the socket "
        "debug events -> " + repr(listeners)
    )
