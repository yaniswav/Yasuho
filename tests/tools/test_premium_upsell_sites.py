"""Call-site census: ``tools.premium_upsell`` may only be imported by an
actual limit-REFUSAL site, never by a background task (a poller, a
dispatcher, a periodic job) and never from inside ``tools/`` itself (which
must stay import-neutral - a background job living in ``tools/`` must not be
able to reach a user-facing nudge either).

THE RULE THIS GUARDS. tools/premium_upsell.py's own module docstring is
explicit: "a background poller importing this module at all would be a
defect worth failing a test over". The 7-day throttle and the admin/member
wording are only meaningful on a REPLY to the person who just got refused -
a periodic task has no "person who just asked" to reply to, and calling in
from one would either spam nobody usefully or (worse) silently drain a
person's weekly slot from a context with no user-facing effect at all.

THE DETECTOR is a plain source-text regex for an import of
``tools.premium_upsell`` (``import tools.premium_upsell``,
``from tools import premium_upsell`` or ``from tools import ..., premium_upsell``),
scanned across every shipped ``.py`` file under ``cogs/`` and ``tools/``
(the test suite itself is excluded - this guards shipped code, not tests).

THE NEGATIVE CONTROL (test_the_detector_actually_flags_an_extra_import_site)
proves the detector is not vacuously green: it runs the SAME regex against a
synthetic snippet that imports the module from a disallowed location and
asserts it IS flagged, and against one that does not import it at all and
asserts it is NOT. Without this, an allowlist test that only ever asserts
"the file set equals {the known-good files}" would stay green even if the
regex itself were broken (e.g. a typo that never matches anything) - exactly
the "guards need a negative control" lesson this tree has learned the hard
way before.
"""

from __future__ import annotations

import os
import re

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")

_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+tools\.premium_upsell\b"
    r"|from\s+tools\s+import\s+(?:[\w, ]*,\s*)?premium_upsell\b)",
    re.MULTILINE,
)

# Every file that is ALLOWED to import tools.premium_upsell - one entry per
# wired refusal site, plus the /premium panel (which marks the person's
# upsells "seen"). Deliberately exhaustive and hand-reviewed: a new import
# anywhere else fails this test until it is added here, which is the whole
# point of a census over an open-ended "looks fine" review.
_ALLOWED_IMPORTERS = {
    "cogs/system/premium_panel.py",
    "cogs/music/music.py",
    "cogs/music/views.py",
    "cogs/music/playlists_shared.py",
    "cogs/community/reminders.py",
    "cogs/config/rolemenus.py",
    "cogs/config/rooms_config.py",
    "cogs/config/tickets/open.py",
    "cogs/anilist/feed.py",
    # M5 coverage-gap fixes: the panel's own follow-add pre-check and the
    # move/create-feed ChannelSelect both show the upsell themselves now
    # (cog helpers return a plain error string the panel already relays).
    "cogs/anilist/feed_views.py",
}


def _scanned_files():
    """Every shipped .py file under cogs/ and tools/ (not the test suite)."""
    for subdir in ("cogs", "tools"):
        base = os.path.join(_REPO_ROOT, subdir)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def _importers():
    found = set()
    for path in _scanned_files():
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        if _IMPORT_RE.search(source):
            found.add(os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/"))
    return found


def test_only_known_refusal_sites_import_premium_upsell():
    assert _importers() == _ALLOWED_IMPORTERS


def test_the_detector_actually_flags_an_extra_import_site():
    """Negative control: prove the regex itself works, on synthetic text the
    allowlist test above never sees - a background-task snippet that DOES
    import the module (must be flagged) and one that does not (must not)."""
    background_task_with_import = (
        "from tools import db, premium_upsell\n"
        "\n"
        "async def run_nightly_digest(pool):\n"
        "    pass\n"
    )
    assert _IMPORT_RE.search(background_task_with_import) is not None

    background_task_without_import = (
        "from tools import db\n"
        "\n"
        "async def run_nightly_digest(pool):\n"
        "    pass\n"
    )
    assert _IMPORT_RE.search(background_task_without_import) is None

    # The other accepted import spelling.
    plain_module_import = "import tools.premium_upsell\n"
    assert _IMPORT_RE.search(plain_module_import) is not None


def test_every_allowed_importer_file_actually_exists():
    """The allowlist above cannot silently rot into naming a file that was
    renamed or deleted - every entry must resolve on disk."""
    for relpath in _ALLOWED_IMPORTERS:
        assert os.path.isfile(os.path.join(_REPO_ROOT, relpath)), relpath
