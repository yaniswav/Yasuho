"""The help-taxonomy guard: the "Other" category must stay empty forever.

The help menu groups every cog under a curated, human-friendly taxonomy
(``CATEGORIES`` in cogs/system/help.py). ``build_categories`` keeps a final
"Other" catch-all so a cog nobody claimed still shows up rather than vanishing -
but that is a safety net, not a home for real features. Lot UX1 emptied it.

This test is the belt to that suspenders: it walks ``cogs/`` (importing every
module, exactly like tests/test_import_smoke.py), finds every ``commands.Cog``
subclass that owns at least one VISIBLE ROOT command, and asserts each is either
claimed by a category or explicitly excluded. So a future feature lot that adds
a cog with visible commands and forgets to file it under a category FAILS here
instead of silently minting an "Other" entry in production.

It also pins two integrity properties of the taxonomy data itself:

* Every taxonomy member (a category's cog list, or ``EXCLUDED_COGS``) names a
  cog that actually exists - a rename that leaves a dangling name is caught.
* No cog is claimed twice (by two categories, or by both a category and the
  exclusion set) - which would make the menu double-list its commands.

Pure and offline: it only imports modules and reads class-level ``__cog_commands__``
(populated by discord.py's ``CogMeta`` at class-creation time), so it never
touches Discord, the network, a database or Lavalink, and never instantiates the
bot.
"""

import importlib
import pathlib

from discord.ext import commands

from cogs.system import help as help_mod

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _iter_cog_modules():
    """Dotted names for every ``.py`` under cogs/ (packages collapse to __init__)."""
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


def _import_all_cogs():
    """Import every cog module so all Cog subclasses are registered."""
    skipped = []
    for mod in _iter_cog_modules():
        try:
            importlib.import_module(mod)
        except ImportError as exc:  # optional third-party dep absent -> skip it
            skipped.append((mod, str(exc)))
    return skipped


def _discovered_cogs():
    """First-party Cog subclasses, de-duplicated by class identity.

    Mirrors ``bot.get_cog`` keys: a cog is keyed by ``__cog_name__`` (the
    class name unless overridden). Plain mixin classes (LookupMixin, ...) are
    NOT ``commands.Cog`` subclasses, so they never appear here - only real,
    loadable cogs do.
    """
    return {
        cls
        for cls in _all_subclasses(commands.Cog)
        if cls.__module__.startswith("cogs")
    }


def _visible_root_command_names(cog_cls):
    """Names of the cog's visible ROOT commands, read statically off the class.

    ``CogMeta`` populates ``__cog_commands__`` at class-creation time, so this
    needs no bot instance. It matches ``Cog.get_commands()`` (root = parent is
    None) filtered to non-hidden - exactly what ``build_categories`` surfaces.
    """
    cmds = getattr(cog_cls, "__cog_commands__", ())
    return [
        c.name
        for c in cmds
        if getattr(c, "parent", None) is None and not getattr(c, "hidden", False)
    ]


def _taxonomy_cog_names():
    """Every cog name the taxonomy claims across all categories (with repeats)."""
    names = []
    for _emoji, _name, _description, cog_names in help_mod.CATEGORIES:
        names.extend(cog_names)
    return names


# ---------------------------------------------------------------------------
# Fixtures shared by the assertions below (import once, at collection time).
# ---------------------------------------------------------------------------

_SKIPPED = _import_all_cogs()
_COGS = _discovered_cogs()
_NAME_TO_CLS = {getattr(c, "__cog_name__", c.__name__): c for c in _COGS}


def test_discovery_is_sane():
    """Guard the walk itself, so a broken import can't make the checks vacuous."""
    assert _COGS, f"no cogs discovered; the guard would be toothless. skipped={_SKIPPED!r}"
    # A cog we know has visible root commands, and one we know is command-less.
    assert "Music" in _NAME_TO_CLS
    assert "Leveling" in _NAME_TO_CLS
    assert _visible_root_command_names(_NAME_TO_CLS["Music"])
    assert _visible_root_command_names(_NAME_TO_CLS["Help"]) == []


def test_every_visible_cog_is_categorised_or_excluded():
    """THE guard: no cog with visible commands may fall into "Other".

    Every first-party cog that owns a visible root command must be claimed by a
    category or listed in ``EXCLUDED_COGS``. A new feature that adds a visible
    cog and forgets to file it fails here - the belt to ``build_categories``'
    "Other" catch-all suspenders.
    """
    claimed = set(_taxonomy_cog_names()) | help_mod.EXCLUDED_COGS
    orphans = []
    for cog_cls in _COGS:
        name = getattr(cog_cls, "__cog_name__", cog_cls.__name__)
        if _visible_root_command_names(cog_cls) and name not in claimed:
            orphans.append(f"{name} ({cog_cls.__module__})")
    assert not orphans, (
        "Cog(s) with visible commands are not in any help category and not "
        "excluded - they would land in the 'Other' catch-all. Add each to a "
        "category in cogs/system/help.py CATEGORIES (or to EXCLUDED_COGS):\n  "
        + "\n  ".join(sorted(orphans))
    )


def test_taxonomy_members_reference_real_cogs():
    """Every claimed / excluded name must map to a real, discovered cog.

    Catches a rename (or typo) that leaves a taxonomy entry pointing at a cog
    class that no longer exists.
    """
    known = set(_NAME_TO_CLS)
    dangling = sorted(
        name
        for name in set(_taxonomy_cog_names()) | help_mod.EXCLUDED_COGS
        if name not in known
    )
    assert not dangling, (
        "Help taxonomy references cog name(s) that no first-party cog defines "
        "(a rename left a dangling entry). Fix the name(s) in cogs/system/help.py:\n  "
        + "\n  ".join(dangling)
    )


def test_no_cog_is_claimed_twice():
    """No cog appears in two categories, or in both a category and EXCLUDED_COGS."""
    names = _taxonomy_cog_names()
    seen, dupes = set(), set()
    for name in names:
        if name in seen:
            dupes.add(name)
        seen.add(name)
    both = set(names) & help_mod.EXCLUDED_COGS
    assert not dupes, f"cog(s) claimed by more than one category: {sorted(dupes)}"
    assert not both, f"cog(s) both categorised and excluded: {sorted(both)}"


def test_catch_all_safety_net_still_present():
    """The "Other" catch-all code must stay as a belt-and-suspenders backstop.

    Even though the guard above proves nothing lands there today, the runtime
    catch-all must remain so an unforeseen cog is surfaced rather than lost.
    """
    assert help_mod.OTHER_NAME == "Other"
    assert help_mod.OTHER_EMOJI
    assert help_mod.OTHER_DESCRIPTION
