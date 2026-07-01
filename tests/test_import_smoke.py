"""Import-smoke coverage for every first-party module.

Discovers every ``.py`` module under ``cogs/`` and ``tools/`` plus top-level
``core.py``, imports each one via :mod:`importlib`, and asserts the import
raises nothing. This is the cheapest possible net for the whole codebase: it
catches module-level ``NameError``, bad config reads at import time, broken
``from x import y`` lines, def-time annotation evaluation (e.g. the sonolink
annotations in ``cogs/music/music.py``), and so on.

Two deliberate design points:

* A missing OPTIONAL third-party dependency SKIPS just that module instead of
  failing the run. We only treat a :class:`ModuleNotFoundError` as "optional"
  when the missing top-level package is third-party; a missing first-party
  module (``cogs`` / ``tools`` / ``core`` / ``locales``) is a real bug and
  FAILS. Any other exception (NameError, AttributeError, a plain ImportError
  such as "cannot import name X from Y", a config error, ...) also FAILS.
  On this box every runtime dep is installed and sonolink is stubbed by
  ``conftest.py``, so nothing is expected to skip here.

* A regression guard for the ``_refresh`` -> ``_rerender`` production crash: a
  ``discord.ui.View`` subclass method named ``_refresh`` shadowed discord.py's
  internal ``View._refresh(self, components)``; on a MESSAGE_UPDATE discord.py
  called it with the components list and the bot crashed. The final test fails
  if any first-party View/Modal subclass reintroduces such a private-internal
  collision.
"""

import importlib
import os

import discord
import pytest

# Repo root is the parent of this tests/ directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Top-level package names that belong to THIS project. A ModuleNotFoundError
# whose missing package is one of these is a genuine bug (the module really is
# absent or misnamed) and must fail rather than skip.
FIRST_PARTY_ROOTS = {"cogs", "tools", "core", "locales", "config", "conftest"}


def _discover_modules():
    """Return the sorted, de-duplicated dotted names of every target module.

    Walks ``tools/`` and ``cogs/`` for every ``.py`` file (packages included:
    an ``__init__.py`` collapses to its package name, e.g.
    ``cogs/anilist/__init__.py`` -> ``cogs.anilist``) and prepends ``core``.
    """
    modules = ["core"]
    for package in ("tools", "cogs"):
        pkg_dir = os.path.join(REPO_ROOT, package)
        for root, dirs, files in os.walk(pkg_dir):
            # Never descend into bytecode caches.
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(root, fname), REPO_ROOT)
                dotted = rel[:-3].replace(os.sep, ".").replace("/", ".")
                if dotted.endswith(".__init__"):
                    dotted = dotted[: -len(".__init__")]
                modules.append(dotted)
    return sorted(set(modules))


ALL_MODULES = _discover_modules()


def _import_or_skip(name):
    """Import ``name``; skip only on a missing OPTIONAL third-party package.

    A :class:`ModuleNotFoundError` for a first-party package (or with no
    resolvable name) is re-raised so the test FAILS. Every other exception
    propagates untouched and FAILS the test too - only a genuinely absent
    third-party dependency turns into a skip.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if not missing or missing in FIRST_PARTY_ROOTS:
            raise
        pytest.skip(f"optional dependency {missing!r} not installed (needed by {name})")


def test_discovery_is_sane():
    """Guard the discovery itself so a silent walk failure can't hide gaps."""
    assert "core" in ALL_MODULES
    assert "cogs.music.music" in ALL_MODULES  # def-time sonolink annotations
    assert "tools.crypto" in ALL_MODULES
    assert "cogs.anilist" in ALL_MODULES  # a package __init__ that defines setup
    # The project has dozens of modules; a walk that found only a handful means
    # the discovery broke, not that the codebase shrank.
    assert len(ALL_MODULES) >= 40, ALL_MODULES


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_module_imports(module_name):
    """Every first-party module imports with no exception (or skips on a
    missing optional dep)."""
    _import_or_skip(module_name)


# ---------------------------------------------------------------------------
# Regression guard: no View/Modal subclass may shadow a discord.py internal.
# ---------------------------------------------------------------------------

# Private discord.py View/Modal internals that a subclass must never redefine.
# The dynamic set from ``dir()`` tracks the installed discord.py; this literal
# set keeps teeth even if a future discord.py hides a name from ``dir()`` or
# renames its internals. ``_refresh`` is the exact method behind the prod crash.
_KNOWN_INTERNAL_NAMES = {
    "_refresh",
    "_refresh_timeout",
    "_dispatch_item",
    "_dispatch_timeout",
    "_dispatch_submit",
    "_scheduled_task",
}


def _base_private_names():
    """Private (single-underscore, non-dunder) attribute names on View/Modal."""
    names = set()
    for base in (discord.ui.View, discord.ui.Modal):
        names |= {
            n
            for n in dir(base)
            if n.startswith("_") and not (n.startswith("__") and n.endswith("__"))
        }
    return names


def _all_subclasses(cls):
    """Every transitive subclass of ``cls`` currently registered."""
    seen = set()
    stack = list(cls.__subclasses__())
    while stack:
        sub = stack.pop()
        if sub in seen:
            continue
        seen.add(sub)
        stack.extend(sub.__subclasses__())
    return seen


def test_no_view_internal_method_collision():
    """Fail if a first-party View/Modal subclass shadows a discord.py internal.

    This is the ``_refresh`` -> ``_rerender`` crash guard. We import every
    module best-effort first (import correctness itself is asserted by
    ``test_module_imports``) so that all View/Modal subclasses are registered,
    then check that no first-party subclass defines, in its own body, a name
    that collides with a discord.py View/Modal private internal.
    """
    for name in ALL_MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            # Import health is covered elsewhere; here we only need whatever
            # subclasses managed to load so the collision check can run.
            pass

    dangerous = _base_private_names() | _KNOWN_INTERNAL_NAMES
    # Sanity: the anchor of this guard must actually be considered dangerous,
    # otherwise the test would be silently toothless.
    assert "_refresh" in dangerous

    subclasses = _all_subclasses(discord.ui.View) | _all_subclasses(discord.ui.Modal)
    offenders = []
    for cls in subclasses:
        module = getattr(cls, "__module__", "") or ""
        root = module.split(".")[0]
        if not (module == "core" or root in ("cogs", "tools")):
            continue  # ignore discord.py's own / third-party View subclasses
        clashes = sorted(set(vars(cls)) & dangerous)
        if clashes:
            offenders.append(f"{module}.{cls.__qualname__} redefines {clashes}")

    assert not offenders, (
        "View/Modal subclass member(s) collide with discord.py View/Modal "
        "internals - this is exactly the _refresh->_rerender production crash "
        "(discord.py calls e.g. View._refresh(self, components) on MESSAGE_UPDATE). "
        "Rename the offending member(s):\n  " + "\n  ".join(offenders)
    )
