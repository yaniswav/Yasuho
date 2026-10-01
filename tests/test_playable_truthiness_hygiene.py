"""Structural guard for the Playable-truthiness bug (sonolink ``.current``).

Background (confirmed in the installed sonolink): ``Playable``
(``sonolink/models/track.py``) defines ``__len__`` - the track length in
milliseconds - and no ``__bool__``. Python falls back from ``__bool__`` to
``__len__``, so ``bool(player.current)`` is really
``player.current.length != 0``: a genuinely playing track whose length is 0
(a stream mid-probe, or the partially built track from the cold-restore race
- see ``cogs/music/views.py``'s "length-less tracks") is FALSY. Code that
asks "is something playing?" with ``if player.current:`` / ``if not
player.current:`` therefore gets that case wrong - see
``cogs/music/playerinfo.py``'s module docstring for the full rule.

This module is an AST scan, not an import-time check: it reads the SOURCE of
every ``cogs/music/*.py`` file and ``cogs/system/dashboard_music_actions.py``
and flags any attribute access named ``current`` (``player.current``,
``self._player.current``, ...) used directly in a boolean context - the test
of an ``If`` / ``While`` / ``IfExp`` / ``Assert``, the operand of ``not``, or
an operand of ``and`` / ``or``. ``x.current is None``, comparisons, and plain
calls are fine and never flagged.

It:

1. Provides a pure :func:`find_current_truthiness_sites` over a parsed AST.
2. Unit-tests the guard against synthetic source so it cannot regress into a
   silent, vacuous pass (the lesson of "a guard whose success is a silence
   needs a negative control").
3. Scans the whole music package and asserts there is not a single remaining
   site, with an ``_EXEMPT`` dict (empty) for any genuinely justified case,
   each entry carrying a written reason.

No network, no database, no Discord, no Lavalink: pure source parsing.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TARGET_FILES = (
    "cogs/music/music.py",
    "cogs/music/views.py",
    "cogs/music/player.py",
    "cogs/music/playlists_shared.py",
    "cogs/music/playerinfo.py",
    "cogs/music/voteskip.py",
    "cogs/music/search.py",
    "cogs/music/effects.py",
    "cogs/music/lyrics.py",
    "cogs/music/sponsorblock.py",
    "cogs/music/urlguard.py",
    "cogs/music/vibes.py",
    "cogs/music/safetext.py",
    "cogs/music/failures.py",
    "cogs/music/guild_config.py",
    "cogs/system/dashboard_music_actions.py",
)

# Sites deliberately left truthy-on-purpose, each with a written reason.
# Must stay empty: every real Playable truthiness test in this package has a
# fix, not an excuse.
_EXEMPT: dict[str, str] = {}


# ---------------------------------------------------------------------------
# (1) The pure detector
# ---------------------------------------------------------------------------


def _is_current_attr(node: ast.AST) -> bool:
    """True for any ``<...>.current`` attribute access (any receiver)."""
    return isinstance(node, ast.Attribute) and node.attr == "current"


def _is_not_of_current(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _is_current_attr(node.operand)
    )


def find_current_truthiness_sites(tree: ast.AST, filename: str = "<string>") -> list[int]:
    """Line numbers where a ``.current`` attribute is tested by truthiness.

    Flags: the test of ``If`` / ``While`` / ``IfExp`` / ``Assert``, the
    operand of a bare ``not``, and any operand of a ``BoolOp`` (``and`` /
    ``or``) - whether the whole boolop is itself a test or is embedded in an
    expression (e.g. ``x = player.current or fallback``). Does NOT flag
    ``x.current is None`` (a ``Compare``), ``x.current.identifier`` (plain
    attribute chaining), or a call like ``getattr(x.current, ...)``.
    """
    hits: set[int] = set()

    def check_boolean_context(node: ast.AST) -> None:
        if _is_current_attr(node):
            hits.add(node.lineno)
        elif _is_not_of_current(node):
            hits.add(node.lineno)

    class Visitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            check_boolean_context(node.test)
            self.generic_visit(node)

        def visit_While(self, node: ast.While) -> None:
            check_boolean_context(node.test)
            self.generic_visit(node)

        def visit_IfExp(self, node: ast.IfExp) -> None:
            check_boolean_context(node.test)
            self.generic_visit(node)

        def visit_Assert(self, node: ast.Assert) -> None:
            check_boolean_context(node.test)
            self.generic_visit(node)

        def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
            if isinstance(node.op, ast.Not):
                check_boolean_context(node.operand)
            self.generic_visit(node)

        def visit_BoolOp(self, node: ast.BoolOp) -> None:
            # Every operand of `and`/`or` is evaluated for its truthiness
            # (that is what BoolOp short-circuiting means), regardless of
            # where the BoolOp itself sits (a test, an assignment RHS, ...).
            for value in node.values:
                check_boolean_context(value)
                if _is_not_of_current(value):
                    hits.add(value.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(hits)


def _scan_source(source: str, filename: str = "<string>") -> list[int]:
    return find_current_truthiness_sites(ast.parse(source, filename=filename), filename)


# ---------------------------------------------------------------------------
# (2) Negative control: the guard must not be vacuous
# ---------------------------------------------------------------------------


def test_if_player_current_is_flagged():
    hits = _scan_source(
        "def f(player):\n"
        "    if player.current:\n"
        "        return 1\n"
    )
    assert hits == [2]


def test_if_not_player_current_is_flagged():
    hits = _scan_source(
        "def f(self):\n"
        "    if not self.player.current:\n"
        "        return 1\n"
    )
    assert hits == [2]


def test_boolop_or_fallback_is_flagged():
    """`x = player.current or fallback` evaluates current's truthiness too."""
    hits = _scan_source(
        "def f(player, fallback):\n"
        "    track = player.current or fallback\n"
    )
    assert hits == [2]


def test_while_not_current_is_flagged():
    hits = _scan_source(
        "def f(player):\n"
        "    while not player.current:\n"
        "        pass\n"
    )
    assert hits == [2]


def test_if_player_current_is_none_is_not_flagged():
    """The fixed shape - compared to None - must never be flagged."""
    hits = _scan_source(
        "def f(player):\n"
        "    if player.current is None:\n"
        "        return 1\n"
    )
    assert hits == []


def test_if_player_current_is_not_none_is_not_flagged():
    hits = _scan_source(
        "def f(player):\n"
        "    if player.current is not None:\n"
        "        return 1\n"
    )
    assert hits == []


def test_getattr_on_current_is_not_flagged():
    """Reading an attribute THROUGH current is not a truthiness test of it."""
    hits = _scan_source(
        "def f(player):\n"
        "    return getattr(player.current, 'identifier', None)\n"
    )
    assert hits == []


def test_unrelated_attribute_named_current_elsewhere_is_still_flagged():
    """The detector is receiver-agnostic by design (any `.current`), which is
    the conservative, safe-by-default choice for a hygiene guard."""
    hits = _scan_source(
        "def f(x):\n"
        "    if x.current:\n"
        "        return 1\n"
    )
    assert hits == [2]


# ---------------------------------------------------------------------------
# (3) Integration: the whole music package must be clean
# ---------------------------------------------------------------------------


def _iter_target_paths():
    for rel in _TARGET_FILES:
        path = _REPO_ROOT / rel
        if path.is_file():
            yield rel, path


def test_scan_examines_a_nonzero_number_of_functions():
    """Guard against a vacuous pass: the scan must actually have walked real
    function bodies, not silently examined zero code (e.g. a bad path list)."""
    function_count = 0
    for _rel, path in _iter_target_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function_count += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(tree)
        )
    assert function_count > 50, (
        "The Playable-truthiness scan examined too few functions (%d) - check "
        "_TARGET_FILES before trusting a clean result." % function_count
    )


def test_no_playable_truthiness_sites_in_the_music_package():
    """THE guard: no `.current` access anywhere in cogs/music is tested by
    truthiness. Every real site must compare to ``None`` instead.

    A new offender fails here with the exact file:line, so a future `if
    player.current:` cannot silently reintroduce the zero-length-track bug
    (/play cutting the live track, a skip reporting the queue ended and
    clearing music_state, /nowplaying claiming nothing is playing, the
    dashboard skip executor reporting ended=True).
    """
    offenders = []
    for rel, path in _iter_target_paths():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for lineno in find_current_truthiness_sites(tree, rel):
            key = f"{rel}:{lineno}"
            if key in _EXEMPT:
                continue
            offenders.append(key)

    assert not offenders, (
        "Playable truthiness sites found (a zero-length track is FALSY - "
        "compare to None instead):\n  " + "\n  ".join(sorted(offenders))
    )
