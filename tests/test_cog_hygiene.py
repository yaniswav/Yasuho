"""Regression guard for shadowing a discord.py Cog internal method.

Prod incident (2026-07-09): the ``CustomCommands`` cog defined a method
``get_commands(self, guild_id)``. discord.py's ``commands.Cog`` already owns
``get_commands()`` (no args), which the help command calls on EVERY cog to
enumerate its commands (``build_categories`` in cogs/system/help.py). The
subclass method shadowed the framework one, so ``?help`` crashed with
``TypeError: get_commands() missing 1 required positional argument: 'guild_id'``.
The fix renamed the cog method to ``get_custom_commands``.

This guard imports every cog and asserts no Cog subclass defines a method that
shadows a framework-called Cog method (the command / listener enumerators),
while allowing the documented override hooks (cog_load, cog_check, ...). It is
the Cog-side sibling of tests/test_view_hygiene.py.

The suite never touches the network, a database, Discord, or Lavalink: it only
imports modules and inspects class dictionaries.
"""

import gc
import importlib
import inspect
import pathlib

from discord.ext import commands

# Methods discord.py CALLS on a cog itself - shadowing them breaks the framework
# (help enumeration, command tree sync, listener routing). Do not override these.
_FRAMEWORK_CALLED = frozenset(
    {
        "get_commands",
        "walk_commands",
        "get_listeners",
        "get_app_commands",
        "walk_app_commands",
    }
)

# Hooks discord.py EXPECTS subclasses to override - overriding these is the API.
_ALLOWED_OVERRIDES = frozenset(
    {
        "cog_load",
        "cog_unload",
        "cog_check",
        "cog_command_error",
        "cog_app_command_error",
        "cog_before_invoke",
        "cog_after_invoke",
        "bot_check",
        "bot_check_once",
    }
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _iter_cog_modules():
    """Yield dotted module names for every ``.py`` under cogs/."""
    for path in sorted((_REPO_ROOT / "cogs").rglob("*.py")):
        parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            yield ".".join(parts)


def _all_subclasses(base):
    seen, stack = set(), list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls not in seen:
            seen.add(cls)
            stack.extend(cls.__subclasses__())
    return seen


def find_cog_collisions(cog_cls):
    """Names on ``cog_cls`` (user-defined classes only, up to the discord.py
    boundary) that shadow a framework-called Cog method as an actual routine."""
    out = []
    for klass in cog_cls.__mro__:
        if klass is object or klass.__module__.startswith("discord"):
            continue
        for name, value in vars(klass).items():
            if name in _ALLOWED_OVERRIDES:
                continue
            if name in _FRAMEWORK_CALLED and inspect.isroutine(value):
                out.append(name)
    return out


def test_framework_called_methods_are_real():
    """Pin the assumption: these really are Cog methods, so shadowing is real."""
    for name in _FRAMEWORK_CALLED:
        assert hasattr(commands.Cog, name), name


def test_synthetic_shadow_is_flagged():
    """Prove the guard catches the regression it exists for (not vacuous)."""

    class BadCog(commands.Cog):
        def get_commands(self, guild_id):  # shadows Cog.get_commands
            return None

    try:
        assert "get_commands" in find_cog_collisions(BadCog)
    finally:
        del BadCog
        gc.collect()  # drop it from the process-wide Cog.__subclasses__() registry


def test_clean_cog_is_not_flagged():
    """A cog with normal helpers and legit hooks must be collision-free."""

    class CleanCog(commands.Cog):
        async def cog_load(self):
            pass

        async def get_custom_commands(self, guild_id):  # the fix's name
            return {}

    try:
        assert find_cog_collisions(CleanCog) == []
    finally:
        del CleanCog
        gc.collect()


def test_no_cog_internal_collisions_in_codebase():
    """THE guard: no cog under cogs/ shadows a framework-called Cog method."""
    skipped = []
    for mod in _iter_cog_modules():
        try:
            importlib.import_module(mod)
        except ImportError as exc:  # optional dep absent -> skip
            skipped.append((mod, str(exc)))

    cogs = {
        cls
        for cls in _all_subclasses(commands.Cog)
        if cls.__module__.startswith("cogs")
    }
    assert cogs, f"no cogs discovered; the scan would be vacuous. skipped={skipped!r}"

    offenders = set()
    for cls in cogs:
        for name in find_cog_collisions(cls):
            offenders.add(f"{cls.__module__}.{cls.__qualname__}.{name}")

    assert not offenders, (
        "Cog method(s) shadow a framework-called discord.py Cog internal - this "
        "is exactly the get_commands()/help crash. Rename each to a non-internal "
        "name:\n  " + "\n  ".join(sorted(offenders))
    )
