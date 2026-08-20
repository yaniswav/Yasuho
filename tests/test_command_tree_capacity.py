"""Capacity guard: the global slash tree must stay well under Discord's cap.

The incident this exists to prevent
-----------------------------------
Discord allows a bot **100 global application commands**, full stop. Yasuho's
tree reached **101**. discord.py raises ``CommandLimitReached`` from
``CommandTree.add_command`` the moment the 101st is registered, and
``load_extension`` rolls the whole extension back - so ONE ENTIRE COG silently
fails to load and every command it owns disappears from the running bot.

In production that cog was ``cogs/utility/utility.py``: ``/poll``,
``/quickpoll``, ``/snipe`` and ``/translate`` were simply absent, with nothing
to show for it but one line in the log. Which cog dies is decided purely by
alphabetical load order, so the next feature lot could just as easily have
taken out moderation.

The fix was to fold standalone commands into hybrid GROUPS: a group occupies
exactly ONE of the 100 slots however many subcommands it holds, so grouping
buys capacity back without removing anything (and every folded command kept a
prefix-only shim, which costs no slot at all). That took the tree from 101 to
77.

This guard is what keeps it there. It is deliberately a REAL registration
check, not a source scan: it builds the tree exactly the way ``core.py`` does -
``core.discover_extensions()`` then ``load_extension`` on each - against a
stand-in bot. So it fails on the real failure mode (an extension that cannot
attach) and counts the real number Discord will be handed, including commands
declared in ways an AST scan would miss.

Why a ceiling of 90 rather than 100
-----------------------------------
100 is the cliff, not the budget. A guard set at 100 would only fire once the
tree is already broken - the 101st command is the one that explodes, and by
then a cog is already dark. :data:`MAX_TOP_LEVEL` leaves ten slots of headroom
so a feature lot that needs a new top-level command has room to land, and so
the failure arrives as a red test on someone's branch instead of a missing cog
in production. Raising the number is not the fix: fold commands into a group.

Why a subprocess
----------------
``load_extension`` does not reuse an already-imported module: it builds a fresh
one from the spec and re-executes it, replacing the ``sys.modules`` entry. Any
cog module with import-time side effects therefore runs twice, and the second
run leaves a DIFFERENT class object behind under the same name - which quietly
breaks later tests that hold a reference to the first one (``cogs/utility/
searchweb.py`` patching ``wikipedia.wikipedia.requests`` with its
``_TimeoutRequests`` class is exactly such a case). Loading ~70 extensions is
too blunt an act to perform inside the pytest process, so it happens in a child
process that prints its verdict as JSON and dies with it. That also keeps every
background task a cog spawns out of the test event loop entirely.

Offline like every other guard here: the child bot never logs in, its database
pool is an in-memory stub that answers every query with nothing, and
``wait_until_ready`` never returns, so no cog's background worker body ever
runs.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

_THIS_FILE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parent.parent

# The most top-level application commands this tree may declare. Discord's hard
# global cap is 100; we stop at 90 so there is room to grow and so the guard
# fires while the tree still works. If this trips, fold commands into a hybrid
# group - do NOT raise the number.
MAX_TOP_LEVEL = 90

# Discord's actual limit, pinned here so the margin above is self-documenting.
DISCORD_GLOBAL_COMMAND_LIMIT = 100

# The child prints exactly one line with this prefix; everything else on its
# stdout is noise we do not care about.
_VERDICT_PREFIX = "TREE_VERDICT:"


# ---------------------------------------------------------------------------
# Child process: builds the real tree and reports it.
# ---------------------------------------------------------------------------


def _child_main():
    """Load every extension core.py would load and print the verdict as JSON."""
    import asyncio
    import logging

    sys.path.insert(0, str(_REPO_ROOT))
    os.chdir(_REPO_ROOT)

    import discord
    from discord.ext import commands

    # conftest installs the sonolink stub (absent on Python < 3.12) and copies
    # the config templates into place, both of which several cogs need at
    # import time. Importing it directly is how this child gets that same
    # bootstrap without pytest running. It must come before `core`.
    import conftest  # noqa: F401
    import core

    logging.disable(logging.CRITICAL)

    class _FakeAsyncContext:
        """``async with`` wrapper yielding a fixed value (pool / connection)."""

        def __init__(self, value):
            self._value = value

        async def __aenter__(self):
            return self._value

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _EmptyPool:
        """asyncpg-pool stand-in that answers every read with nothing.

        Several cogs prime a cache in ``cog_load``; they must survive that here
        or the extension would fail for a reason that has nothing to do with
        the command budget, and this guard would under-count.
        """

        async def execute(self, *args, **kwargs):
            return "SELECT 0"

        async def fetch(self, *args, **kwargs):
            return []

        async def fetchrow(self, *args, **kwargs):
            return None

        async def fetchval(self, *args, **kwargs):
            return None

        def acquire(self):
            return _FakeAsyncContext(self)

        def transaction(self):
            return _FakeAsyncContext(self)

    class _StandInBot(commands.Bot):
        """A never-logged-in bot carrying what cogs read at load time.

        Mirrors the surface ``core.Yasuho.__init__`` sets up, so extensions
        attach exactly as they do in production - the point of the exercise.
        """

        def __init__(self):
            super().__init__(
                command_prefix="?",
                intents=discord.Intents.none(),
                help_command=None,
            )
            self.db_pool = _EmptyPool()
            self.http_session = None
            self.image_render_semaphore = asyncio.Semaphore(2)
            self.default_prefix = "?"
            self.prefixes = {}
            self.blacklist = set()
            self.autoroles = {}
            self.muteroles = {}
            self.sl_client = None

        async def wait_until_ready(self):
            """Park forever instead of raising: this bot never logs in.

            Cogs start ``tasks.loop`` workers at load whose ``before_loop``
            waits on this; the real method raises on a bot that never called
            ``login``. Waiting on an event nobody sets parks each worker at its
            gate, so no worker BODY ever runs and nothing reaches the stub pool
            or the network.
            """
            await asyncio.Event().wait()

    async def build():
        bot = _StandInBot()
        # tasks.loop / create_task in a cog's __init__ or cog_load reach for
        # this; the real bot has it by the time setup_hook runs.
        bot.loop = asyncio.get_running_loop()

        failures = []
        for extension in core.discover_extensions():
            try:
                await bot.load_extension(extension)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append([extension, f"{type(exc).__name__}: {exc}"])

        commands_ = bot.tree.get_commands()
        return {
            "names": sorted(c.name for c in commands_),
            "groups": sorted(
                c.name
                for c in commands_
                if isinstance(c, discord.app_commands.Group)
            ),
            "failures": failures,
        }

    verdict = asyncio.run(build())
    print(_VERDICT_PREFIX + json.dumps(verdict))
    sys.stdout.flush()
    # Hard exit: cogs left background tasks and aiohttp connectors behind, and
    # this process exists only to deliver the line above.
    os._exit(0)


if __name__ == "__main__":
    _child_main()


# ---------------------------------------------------------------------------
# Parent process: run the child once, assert on its verdict.
# ---------------------------------------------------------------------------

# Built once and reused: the result is plain JSON data, and paying for ~70
# extension loads per assertion would be pure waste.
_CACHE = {}


def _tree():
    """Return the child's verdict dict, running it on first use."""
    if "value" not in _CACHE:
        proc = subprocess.run(
            [sys.executable, str(_THIS_FILE)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        line = next(
            (
                ln
                for ln in proc.stdout.splitlines()
                if ln.startswith(_VERDICT_PREFIX)
            ),
            None,
        )
        if line is None:
            pytest.fail(
                "The command-tree child process produced no verdict "
                f"(exit {proc.returncode}).\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            )
        _CACHE["value"] = json.loads(line[len(_VERDICT_PREFIX):])
    return _CACHE["value"]


def test_capacity_scan_is_not_vacuous():
    """Guard the guard: a harness that finds nothing would pass everything."""
    tree = _tree()
    assert len(tree["names"]) > 60, tree["names"]
    # Stable anchors across several cogs, including the groups the fold created.
    for anchor in ("play", "rank", "ban", "anilist", "music", "info", "lookup"):
        assert anchor in tree["names"], f"{anchor} missing from {tree['names']}"
    # Grouping is the whole mechanism this guard protects; if the tree somehow
    # had no groups at all, the count below would be measuring something else.
    assert len(tree["groups"]) > 10, tree["groups"]


def test_every_extension_loads():
    """No extension may fail to attach - the production symptom itself.

    ``CommandLimitReached`` surfaces here first: it makes ``load_extension``
    roll an entire cog back, which is exactly how /poll, /quickpoll, /snipe and
    /translate went missing. Any other load error is worth failing on too.
    """
    failures = _tree()["failures"]
    assert not failures, (
        "Extension(s) failed to load. A CommandLimitReached here means the tree "
        "exceeded Discord's 100-command global cap and a WHOLE COG was dropped "
        "(which cog is decided by load order, so this is unstable). Fold "
        "commands into a hybrid group to free slots:\n  "
        + "\n  ".join(f"{ext}: {err}" for ext, err in failures)
    )


def test_top_level_command_count_is_under_the_ceiling():
    """THE guard: the tree must stay at or below :data:`MAX_TOP_LEVEL`."""
    names = _tree()["names"]
    assert len(names) <= MAX_TOP_LEVEL, (
        f"{len(names)} top-level application commands - over the {MAX_TOP_LEVEL} "
        f"ceiling. Discord's hard cap is {DISCORD_GLOBAL_COMMAND_LIMIT}, and "
        "hitting it makes a whole cog fail to load with CommandLimitReached. "
        "Fold commands into a hybrid group (a group costs ONE slot no matter "
        "how many subcommands it holds) rather than raising this number.\n"
        f"Current tree: {names}"
    )


def test_the_ceiling_leaves_real_headroom():
    """The ceiling itself must stay meaningfully below Discord's cliff.

    Pins the intent of :data:`MAX_TOP_LEVEL`: an edit that quietly walks it up
    to 99 would restore exactly the fragility this module exists to prevent.
    """
    assert MAX_TOP_LEVEL <= DISCORD_GLOBAL_COMMAND_LIMIT - 10


@pytest.mark.parametrize(
    "protected",
    [
        # Daily drivers and moderation action verbs. These are muscle memory -
        # moderation especially, typed under pressure - and must never be
        # nested into a group. Pinned here so the next capacity squeeze reaches
        # for something else.
        "play", "queue", "skip", "stop", "pause", "resume", "nowplaying",
        "volume", "rank", "leaderboard",
        "ban", "kick", "mute", "unmute", "warn", "purge", "clean", "tempban",
        "addrole", "removerole", "case", "cases", "reason", "newusers",
        "delwarn", "warninfo", "moverole", "voicekick", "move",
    ],
)
def test_protected_commands_stay_top_level(protected):
    """These stay one word long, on the slash surface as well as the prefix one."""
    assert protected in _tree()["names"], (
        f"/{protected} is no longer a top-level command. It is on the "
        "never-nest list: fold something else instead."
    )
