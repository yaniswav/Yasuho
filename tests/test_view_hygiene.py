"""Regression guard for the ``_refresh`` View-shadow crash.

Background (verified prod incident): a ``discord.ui.View`` subclass defined a
method named ``_refresh``. discord.py's ``BaseView`` already owns an internal
``_refresh(self, components)`` used when a ``MESSAGE_UPDATE`` gateway event
re-syncs a live component message. The subclass method shadowed the framework
one, so on the next edit discord.py called it with a ``components`` list and the
bot crashed. The fix was to rename the method to ``_rerender``.

This module is the highest-value guard in the suite. It:

1. Provides a pure :func:`find_collisions` that, for a given View/Modal/
   LayoutView subclass, returns the names of *methods* the subclass (and its
   own user-defined bases, walking the MRO up to the discord.py boundary)
   defines that collide with a discord.py View/Modal internal attribute and are
   NOT documented, legitimate override hooks.
2. Unit-tests that the guard is not vacuous: a synthetic subclass that defines
   ``_refresh`` IS flagged, while a clean subclass is NOT.
3. Integration-tests the WHOLE codebase: it imports every module under
   ``cogs/`` and ``tools/``, collects every View/Modal/LayoutView subclass, and
   asserts there is not a single collision. Reintroducing a ``_refresh`` (or any
   other internal-method shadow) anywhere fails this test with the exact
   offending ``module.Class.method`` listed.

The suite never touches the network, a database, Discord, or Lavalink: it only
imports modules and inspects class dictionaries.
"""

import gc
import importlib
import inspect
import pathlib

import discord
from discord import ui

# ---------------------------------------------------------------------------
# Reference sets: discord.py framework internals + the legitimate override hooks
# ---------------------------------------------------------------------------

# In discord.py 2.7 ``View``, ``Modal`` and ``LayoutView`` are siblings that all
# inherit from the private ``BaseView`` (which is where ``_refresh`` actually
# lives). Unioning ``dir()`` of the three public bases captures every framework
# attribute a subclass could shadow - including the inherited ``_refresh`` - and
# stays correct across the exact set of base types the codebase subclasses.
_BASE_TYPES = [ui.View, ui.Modal, getattr(ui, "LayoutView", None)]

_INTERNAL_NAMES = set()
for _base in _BASE_TYPES:
    if _base is not None:
        _INTERNAL_NAMES |= set(dir(_base))
# Dunders (``__init__``, ``__init_subclass__``, ...) are conventional, expected
# overrides - never treat them as dangerous shadows.
_INTERNAL_NAMES = {
    n for n in _INTERNAL_NAMES if not (n.startswith("__") and n.endswith("__"))
}

# Documented hooks discord.py EXPECTS subclasses to override. Shadowing these is
# the whole point of the API, so they are exempt from the collision check.
_ALLOWED_OVERRIDES = frozenset(
    {"interaction_check", "on_timeout", "on_error", "on_submit", "callback"}
)


def _is_method_like(value) -> bool:
    """True when a class-dict value is a method (what the crash was about).

    The prod bug was a *method* shadowing an internal *method*. Restricting to
    method-like values keeps legitimate data attributes from tripping the guard,
    most notably the ``class Foo(discord.ui.Modal, title="...")`` pattern, which
    injects a plain ``title`` string that collides in name only with the
    ``Modal.title`` property but is not a dangerous shadow.
    """
    if isinstance(value, (staticmethod, classmethod, property)):
        return True
    return inspect.isroutine(value)


def find_collisions(view_cls):
    """Return the method names on ``view_cls`` that shadow a framework internal.

    Walks ``view_cls.__mro__`` from the class itself up to (but not into) the
    discord.py base classes, i.e. only the user-defined classes in the chain.
    For each such class it reports any method whose name is a discord.py
    View/Modal internal attribute and is not one of the allow-listed override
    hooks. Pure: no imports, no I/O, no side effects.
    """
    collisions = []
    for klass in view_cls.__mro__:
        # Stop at the discord.py framework classes (View / Modal / LayoutView /
        # BaseView) and at ``object`` - their internals are the reference set we
        # check *against*, not code we police.
        if klass is object or klass.__module__.startswith("discord"):
            continue
        for name, value in vars(klass).items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in _ALLOWED_OVERRIDES:
                continue
            if name in _INTERNAL_NAMES and _is_method_like(value):
                collisions.append(name)
    return collisions


# ---------------------------------------------------------------------------
# (1) The guard is not vacuous
# ---------------------------------------------------------------------------


def test_refresh_is_a_real_framework_internal():
    """If discord.py ever drops ``_refresh`` the guard would silently pass.

    Pin the assumption the whole regression test rests on: ``_refresh`` really
    is an internal attribute of the View machinery, so shadowing it is really
    detectable. If a future discord.py refactor removes it, this fails loudly
    and tells the maintainer to revisit the guard rather than trust a green tick.
    """
    assert "_refresh" in _INTERNAL_NAMES


# ---------------------------------------------------------------------------
# (2) Unit tests on synthetic classes
# ---------------------------------------------------------------------------
#
# The synthetic View subclasses below are deliberately defined INSIDE the test
# bodies rather than at module scope. discord.py registers every subclass in the
# process-wide ``View.__subclasses__()`` list, and other tests in this suite scan
# that list for exactly this kind of shadow. A module-level ``SubclassWithRefresh``
# would therefore leak a fake ``_refresh`` offender into those scans. Defining
# them locally and forcing ``gc.collect()`` guarantees they are gone before any
# other test runs, so these fixtures never pollute the global subclass registry.


def test_synthetic_refresh_subclass_is_flagged():
    """Proves the guard actually catches the regression it exists for."""

    class SubclassWithRefresh(discord.ui.View):
        async def _refresh(self, components):  # pragma: no cover - never called
            pass

    try:
        collisions = find_collisions(SubclassWithRefresh)
        assert "_refresh" in collisions
    finally:
        del SubclassWithRefresh
        gc.collect()  # drop it from the process-wide View.__subclasses__() registry


def test_clean_subclass_is_not_flagged():
    """A subclass using ``_rerender`` and plain helpers must be collision-free."""

    class CleanSubclass(discord.ui.View):
        async def _rerender(self, interaction):  # the actual fix's replacement name
            pass

        def build_embed(self):
            return None

    try:
        assert find_collisions(CleanSubclass) == []
    finally:
        del CleanSubclass
        gc.collect()


def test_allowlisted_hook_overrides_are_not_flagged():
    """Overriding interaction_check / on_submit is the API, not a collision."""

    class HookOverridingModal(discord.ui.Modal, title="ok"):
        async def interaction_check(self, interaction):
            return True

        async def on_submit(self, interaction):  # pragma: no cover - never called
            pass

    try:
        assert find_collisions(HookOverridingModal) == []
    finally:
        del HookOverridingModal
        gc.collect()


def test_flag_survives_alongside_legit_hooks():
    """A dangerous shadow is still reported even next to allow-listed hooks."""

    class Mixed(discord.ui.View):
        async def interaction_check(self, interaction):
            return True

        async def on_timeout(self):
            pass

        async def _refresh(self, components):  # pragma: no cover
            pass

    try:
        collisions = find_collisions(Mixed)
        assert collisions == ["_refresh"]
    finally:
        del Mixed
        gc.collect()


# ---------------------------------------------------------------------------
# (3) Integration: the whole codebase must be collision-free
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TARGET_PACKAGES = ("cogs", "tools")


def _iter_target_modules():
    """Yield dotted module names for every ``.py`` under cogs/ and tools/."""
    for pkg in _TARGET_PACKAGES:
        pkg_dir = _REPO_ROOT / pkg
        if not pkg_dir.is_dir():
            continue
        for path in sorted(pkg_dir.rglob("*.py")):
            parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                yield ".".join(parts)


def _all_subclasses(base):
    """Every transitive subclass of ``base`` currently loaded in the process."""
    seen = set()
    stack = list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen


def _collect_target_view_classes():
    """Import every cogs/tools module, then gather all our View-family classes.

    Missing OPTIONAL dependencies raise ``ImportError``; those modules are
    skipped (recorded) rather than failing the run, mirroring pytest's
    ``importorskip``. Any other exception is a real problem and propagates.
    """
    skipped = []
    for modname in _iter_target_modules():
        try:
            importlib.import_module(modname)
        except ImportError as exc:  # optional dep absent -> skip, do not error
            skipped.append((modname, str(exc)))

    classes = set()
    for base in _BASE_TYPES:
        if base is not None:
            classes |= _all_subclasses(base)

    # Only police OUR code; discord.py's own internal subclasses are out of scope.
    target = {
        cls
        for cls in classes
        if cls.__module__.startswith(_TARGET_PACKAGES)
    }
    return target, skipped


def _defining_class(cls, name):
    """The nearest class in ``cls.__mro__`` that actually defines ``name``."""
    for klass in cls.__mro__:
        if name in vars(klass):
            return klass
    return cls


def test_no_view_internal_collisions_in_codebase():
    """THE guard: no View/Modal/LayoutView in cogs/ or tools/ shadows an internal.

    Reintroducing a ``_refresh`` (or any other framework-internal method name)
    on a View subclass anywhere in the bot fails here, with the offending
    ``module.Class.method`` spelled out in the failure message.
    """
    view_classes, skipped = _collect_target_view_classes()

    # Guard against a vacuous pass: if imports silently produced no classes the
    # assertion below would be trivially true and useless.
    assert view_classes, (
        "No View/Modal/LayoutView subclasses were discovered under cogs/ or "
        "tools/ - the collision scan would be vacuous. Skipped imports: "
        + repr(skipped)
    )

    offenders = set()
    for cls in view_classes:
        for name in find_collisions(cls):
            definer = _defining_class(cls, name)
            offenders.add(f"{definer.__module__}.{definer.__qualname__}.{name}")

    assert not offenders, (
        "View internal-attribute collisions found - a subclass method shadows a "
        "discord.py View/Modal internal (this is exactly the `_refresh` crash). "
        "Rename each of these to a non-internal name (e.g. `_rerender`):\n  "
        + "\n  ".join(sorted(offenders))
    )
