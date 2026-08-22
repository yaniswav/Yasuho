"""No music EDIT may re-apply the client default to attacker-supplied text.

THE FINDING. A Components V2 message carries its text inside the view, so every
re-render resends every TextDisplay - and those hold third-party text (track
titles come straight off ICY / ID3 metadata on a file the requester hosts) plus
raw ``<@id>`` tokens the panel legitimately draws (``**DJ:** {mention}``).
``Message.edit`` folds the CLIENT default into any edit that does not say
otherwise (``core.Yasuho``: ``users=True``), so a first send that carefully
suppressed mentions was undone by the first page flip, the first progress tick,
the first synced-lyrics line - and the DJ was re-pinged every few seconds.

The initial sends already knew this (``Music._send_controller`` passes
``AllowedMentions.none()`` with a comment saying why, and so does
``tools.views.AuthorLayoutView.on_timeout``). The EDITS did not. These tests pin
that they do now, and - via the package sweep - that the next one will too.
"""

import ast
import pathlib
import types

import pytest

# music.py first: views.py imports from it at module level, so importing views
# on its own hits the package's documented circular-import order (the same
# reason tests/cogs/test_music_queue_view.py imports music alongside it).
from cogs.music import lyrics, music, views  # noqa: F401


def _pings_nobody(kwargs):
    """``AllowedMentions`` has no ``__eq__``, so compare what it MEANS."""

    allowed = kwargs.get("allowed_mentions")
    return (
        allowed is not None
        and allowed.everyone is False
        and allowed.users is False
        and allowed.roles is False
    )


# --- Fakes (shaped like tests/cogs/test_music_queue_view.py's) --------------


class _Track:
    def __init__(self, title, encoded=None):
        self.title = title
        self.author = "Artist"
        self.length = 125000
        self.is_stream = False
        self.encoded = encoded if encoded is not None else title
        self.extras = types.SimpleNamespace(requester=None)


class _Queue:
    def __init__(self, tracks=()):
        self._items = list(tracks)
        self.current_track = None
        self.history = []

    @property
    def tracks(self):
        return list(self._items)

    @property
    def autoplay_tracks(self):
        return []


class _Player:
    def __init__(self, tracks=(), current=None, dj=None):
        self.queue = _Queue(tracks)
        self.current = current
        self.channel = types.SimpleNamespace(name="General")
        self.dj = dj


class _Cog:
    async def _can_control(self, player, actor):
        return True

    async def _snapshot(self, player, track=None):
        return None


class _Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _Response:
    def __init__(self):
        self.edits = []

    def is_done(self):
        return False

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class _Interaction:
    def __init__(self):
        self.response = _Response()


def _queue_view(*titles):
    view = views.QueueView(_Cog(), _Player(tracks=[_Track(t) for t in titles]))
    view._build()
    view.message = _Message()
    return view


# --- The queue browser ------------------------------------------------------


def test_the_queue_view_really_draws_the_hostile_text():
    """The premise: the title IS in the payload every re-render resends."""

    view = _queue_view("ping <@1234567890123456> now")
    drawn = " ".join(
        child.content for child in view.walk_children() if hasattr(child, "content")
    )
    assert "<@1234567890123456>" in drawn


async def test_the_queue_view_in_place_rerender_suppresses_mentions():
    view = _queue_view("A", "B")
    await view._rerender()
    assert _pings_nobody(view.message.edits[-1])


async def test_the_queue_view_timeout_edit_suppresses_mentions():
    view = _queue_view("A")
    await view.on_timeout()
    assert _pings_nobody(view.message.edits[-1])


async def test_the_queue_view_click_edit_suppresses_mentions():
    """The exact seam of the finding: a defused first send, undone by the first
    page flip - the click edits the message it landed on."""

    view = _queue_view("A", "B")
    interaction = _Interaction()
    await view._rerender_from(interaction)
    assert _pings_nobody(interaction.response.edits[-1])


# --- The synced-lyrics card -------------------------------------------------


def _session():
    session = lyrics.SyncedLyricsSession.__new__(lyrics.SyncedLyricsSession)
    session.message = _Message()
    session._card = types.SimpleNamespace(
        set_state=lambda **kwargs: None, stop=lambda: None
    )
    session._stopped = False
    session._last_body = "**line**"
    return session


async def test_the_synced_lyrics_line_edit_suppresses_mentions():
    """One edit per lyric line: the surface that would re-ping the most."""

    session = _session()
    await session._edit("**line two**")
    assert _pings_nobody(session.message.edits[-1])


async def test_the_synced_lyrics_final_edit_suppresses_mentions():
    session = _session()
    await session._finalize()
    assert _pings_nobody(session.message.edits[-1])


# --- The package sweep ------------------------------------------------------


EDIT_METHODS = ("edit", "edit_message", "edit_original_response")


def _scan_source(source, name="<synthetic>"):
    """The DETECTOR, isolated so it can be aimed at a source we control.

    Returns ``(offenders, examined)``. ``examined`` is what makes a green run
    mean something: an empty offender list is only good news if the detector
    actually looked at some edits.
    """

    offenders = []
    examined = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in EDIT_METHODS:
            continue
        examined += 1
        if not any(kw.arg == "allowed_mentions" for kw in node.keywords):
            offenders.append("{0}:{1}".format(name, node.lineno))
    return offenders, examined


def _scanned_paths(root):
    """Which files the sweep will read. Recursive, NOT a flat glob: this cog
    gets split into subpackages as it grows (that is the house rule), and a
    flat pattern would silently stop seeing the code that moved.

    Split out from the sweep so a test can aim it at a tree it built itself -
    asserting on the source text of this function would only pin the wording.
    """

    return sorted(root.rglob("*.py"))


def _unsuppressed_edits():
    """Sweep the package."""

    bad = []
    total = 0
    for path in _scanned_paths(pathlib.Path("cogs/music")):
        offenders, examined = _scan_source(
            path.read_text(encoding="utf-8"), path.name
        )
        bad.extend(offenders)
        total += examined
    return bad, total


def test_no_edit_in_the_music_package_is_left_unsuppressed():
    """The rule, not just the two surfaces named in the finding.

    Every message/interaction edit in this package resends a payload built from
    somebody else's text, and an edit never ADDS a reason to notify - so there
    is no legitimate unsuppressed edit here, and a new one is a regression.
    """

    offenders, examined = _unsuppressed_edits()
    assert offenders == []
    # Anti-vacuity: the sweep is worthless if it found nothing to judge.
    assert examined >= 20, examined


@pytest.mark.parametrize(
    "module,attribute",
    [(views, "QueueView"), (lyrics, "SyncedLyricsCard")],
)
def test_the_named_surfaces_still_exist(module, attribute):
    """Guards the sweep above from passing because a class was renamed away."""

    assert hasattr(module, attribute)


# --- The negative control ---------------------------------------------------
#
# The sweep above asserts an EMPTY list. That is exactly the shape that goes on
# passing forever once the detector stops detecting - it would not say "they are
# all suppressed", it would say "I see none", and those read identically from a
# green run. Every guard whose success condition is silence needs a case it MUST
# report. This is that case.


def test_the_detector_reports_an_edit_that_names_no_allowed_mentions():
    offenders, examined = _scan_source("await msg.edit(content=x)")
    assert offenders == ["<synthetic>:1"]
    assert examined == 1


def test_the_detector_clears_an_edit_that_does_name_them():
    offenders, examined = _scan_source(
        "await msg.edit(content=x, allowed_mentions=none)"
    )
    assert offenders == []
    assert examined == 1


@pytest.mark.parametrize("method", EDIT_METHODS)
def test_every_edit_spelling_is_covered_by_the_detector(method):
    """A new edit method added to the list must actually be scanned."""

    offenders, _ = _scan_source("await obj.{0}(content=x)".format(method))
    assert offenders == ["<synthetic>:1"]


def test_the_sweep_reaches_a_file_nested_in_a_subpackage(tmp_path):
    """Behavioural, not a grep on our own source.

    The first version of this test asserted ``"rglob" in getsource(...)`` and
    passed with a flat glob, because the DOCSTRING said "rglob". A guard that
    reads its own prose proves nothing - build a tree and look.
    """

    (tmp_path / "flat.py").write_text("")
    nested = tmp_path / "views" / "deep.py"
    nested.parent.mkdir()
    nested.write_text("")

    assert nested in _scanned_paths(tmp_path)
