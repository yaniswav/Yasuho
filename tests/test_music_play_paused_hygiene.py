"""Structural guard (lot S2-1): every call to ``<player-like>.play(...)`` in
``cogs/music/*.py`` and ``cogs/system/dashboard_music_actions.py`` must pass a
``paused=`` keyword explicitly, unless the site is listed in ``_EXEMPT`` with a
written reason.

Why this matters: sonolink's ``Player.play()`` resends whatever
``player._paused`` already is when ``paused`` is omitted (``paused = paused
if paused is not None else self._player._paused``, identical in sonolink
1.2.1 and 1.4.0). Every USER-initiated call - starting playback from idle
(``/play``, a genre zap, a favourites/playlist bulk load, re-queueing a
history/favourite entry, the add-song modal) and the queue manager's
jump-to-track - must start the new track PLAYING regardless of whatever the
player was doing before, so every one of those sites passes ``paused=False``
explicitly. The one site allowed to pass through the OLD paused state is the
cold-restore ``play()`` in ``Music._restore_one`` - and it already does that
by passing ``paused=bool(row["paused"])`` (the persisted session state), which
means it ALREADY satisfies "has a paused= keyword" and needs no exemption at
all; if a future edit ever made it omit paused=, this guard would correctly
flag it as unexempted, which is itself a bug (cold restore must not silently
start a persisted-paused session playing).

Scope: only calls shaped ``player.play(...)`` or ``<attr-chain>.player.play(...)``
(e.g. ``self.player.play(...)``) are treated as a player-like play() call -
every real call site in the scanned files is one of exactly these two shapes
(confirmed by grep; the only other occurrences of the substring ``.play(`` in
these files are inside comments/docstrings, which ast.parse never sees as
calls). This intentionally does NOT flag an unrelated ``.play()`` on some
other kind of object (e.g. an audio/animation helper) - there is none in the
scanned files today, and the narrow base-name match (``player`` /
``*.player``) keeps it that way rather than flagging every attribute named
``play`` anywhere.

``resume_after_track_change`` (the skip/back resume helper) is the paired
fix for the OTHER call shape - ``skip()``/``previous()``, which take no
``paused=`` argument at all - and is pinned separately in
``test_music_resume_on_track_change.py``.
"""

import ast
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_FILES = sorted(
    (PROJECT_ROOT / "cogs" / "music").glob("*.py")
) + [PROJECT_ROOT / "cogs" / "system" / "dashboard_music_actions.py"]

# (relative/path.py, qualname): written reason. Empty today - the one site
# that intentionally forwards an old paused state (cold restore) already
# carries its own paused= keyword and so never reaches this dict at all; see
# the module docstring.
_EXEMPT = {}


def _is_player_play_call(node):
    """Whether ``node`` (an ``ast.Call``) is ``player.play(...)`` or
    ``<...>.player.play(...)`` - the two shapes every real site in the scanned
    files uses (see module docstring for why the scope stops there)."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "play":
        return False
    base = func.value
    if isinstance(base, ast.Name) and base.id == "player":
        return True
    if isinstance(base, ast.Attribute) and base.attr == "player":
        return True
    return False


def _has_paused_kwarg(node):
    return any(kw.arg == "paused" for kw in node.keywords)


def _unguarded_play_sites(path):
    """Yield ``qualname`` for every function in ``path`` that calls
    ``player.play(...)`` with no ``paused=`` keyword, directly (not merely
    somewhere in a nested call - the keyword must be on the play() call
    itself)."""
    source_text = path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(path))

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.found = []

        def _function(self, node):
            self.stack.append(node.name)
            qualname = ".".join(self.stack)
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and _is_player_play_call(n):
                    if not _has_paused_kwarg(n):
                        self.found.append(qualname)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self._function(node)

        def visit_AsyncFunctionDef(self, node):
            self._function(node)

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.found


def test_every_player_play_call_passes_paused_explicitly_or_is_exempt():
    unguarded = []
    for path in SCAN_FILES:
        rel = "{0}/{1}".format(
            "/".join(path.relative_to(PROJECT_ROOT).parts[:-1]), path.name
        )
        for qualname in _unguarded_play_sites(path):
            if (rel, qualname) in _EXEMPT:
                continue
            unguarded.append(f"{rel}:{qualname}")
    assert not unguarded, (
        "player.play(...) call site(s) with no explicit paused= keyword and "
        "not listed in _EXEMPT with a written reason (a new user-initiated "
        "play site must pass paused=False; a new intentional old-state "
        "carry-over must add a reason to _EXEMPT): " + ", ".join(unguarded)
    )


# ---------------------------------------------------------------------------
# Counter-tests: the detector must actually flag something, and must not
# flag an unrelated .play() or a guarded call.
# ---------------------------------------------------------------------------


def test_guard_flags_a_synthetic_unguarded_play_site(tmp_path):
    synthetic = tmp_path / "synthetic.py"
    synthetic.write_text(
        "class Cog:\n"
        "    async def idle_start(self, player):\n"
        "        if player.current is None:\n"
        "            await player.play(player.queue.get())\n",
        encoding="utf-8",
    )
    assert _unguarded_play_sites(synthetic) == ["Cog.idle_start"]


def test_guard_flags_the_self_player_shape_too(tmp_path):
    synthetic = tmp_path / "synthetic_self.py"
    synthetic.write_text(
        "class View:\n"
        "    async def add_song(self, interaction):\n"
        "        if self.player.current is None:\n"
        "            await self.player.play(self.player.queue.get())\n",
        encoding="utf-8",
    )
    assert _unguarded_play_sites(synthetic) == ["View.add_song"]


def test_guard_does_not_flag_a_call_with_paused_explicit(tmp_path):
    synthetic = tmp_path / "synthetic_guarded.py"
    synthetic.write_text(
        "class Cog:\n"
        "    async def idle_start(self, player):\n"
        "        if player.current is None:\n"
        "            await player.play(player.queue.get(), paused=False)\n",
        encoding="utf-8",
    )
    assert _unguarded_play_sites(synthetic) == []


def test_guard_does_not_flag_cold_restore_shape_passing_persisted_paused(tmp_path):
    """Mirrors the real cold-restore site: it carries paused= (from the
    snapshot), so it is never even a candidate for _EXEMPT."""
    synthetic = tmp_path / "synthetic_restore.py"
    synthetic.write_text(
        "class Cog:\n"
        "    async def restore_one(self, player, row):\n"
        "        await player.play(\n"
        "            current,\n"
        "            start=position,\n"
        "            paused=bool(row['paused']),\n"
        "        )\n",
        encoding="utf-8",
    )
    assert _unguarded_play_sites(synthetic) == []


def test_guard_ignores_an_unrelated_play_attribute(tmp_path):
    """A ``.play()`` on something that is not named ``player``/``*.player``
    (e.g. a sound/animation helper) is out of scope - this guard is about
    sonolink Player.play(), not every method named play."""
    synthetic = tmp_path / "synthetic_unrelated.py"
    synthetic.write_text(
        "class Thing:\n"
        "    async def chime(self, sound):\n"
        "        await sound.play()\n",
        encoding="utf-8",
    )
    assert _unguarded_play_sites(synthetic) == []


def test_guard_flags_a_call_nested_two_scopes_deep(tmp_path):
    """A play() call inside a nested closure still attributes to the OUTER
    named function (matching the role-guard hygiene test's convention), so a
    site cannot escape the scan by being written inside a local helper."""
    synthetic = tmp_path / "synthetic_nested.py"
    synthetic.write_text(
        "class Cog:\n"
        "    async def idle_start(self, player):\n"
        "        async def _go():\n"
        "            await player.play(player.queue.get())\n"
        "        await _go()\n",
        encoding="utf-8",
    )
    assert _unguarded_play_sites(synthetic) == ["Cog.idle_start", "Cog.idle_start._go"]


def test_the_real_cogs_music_tree_has_no_exempt_entries_left_unused():
    """_EXEMPT is empty today (see module docstring) - this pins that fact so
    a stale, no-longer-needed exemption cannot linger unnoticed."""
    assert _EXEMPT == {}
