"""Structural guard: requirements.lock must actually cover requirements.txt.

Background (the bug this closes): requirements.lock pinned cryptography==49.0.0
while requirements.txt had already moved to cryptography~=50.0.0 - a pin OUTSIDE
the range the human-maintained file declares. Nothing caught it because nothing
ever compared the two files; the lock just silently drifted the day
requirements.txt was bumped without a regeneration.

Now that run.sh/setup.sh install requirements.lock FIRST (see its own header and
tools/gen_lock.py), a lock that falls outside requirements.txt's bounds is no
longer a cosmetic inconsistency: it is what the venv actually runs. This module
is the guard against that drifting again.

It:

1. Provides a pure :func:`find_lock_issues` that, given the TEXT of a
   requirements file and a lock file, reports every requirement whose lock pin
   is missing or falls outside its specifier, plus every lock line that is not
   an exact ``Name==version`` pin. It uses ``packaging`` (``Requirement``,
   ``canonicalize_name``) for all name/version comparisons, so `discord.py` vs
   `discord-py` style spelling differences never cause a false mismatch.
2. Unit-tests the guard is not vacuous with a NEGATIVE CONTROL: a synthetic
   requirements text + synthetic lock with (a) an out-of-bounds pin and (b) a
   missing root, and asserts BOTH are reported - plus asserts the real check
   examined a non-zero number of requirements (a guard that checks nothing must
   not pass).
3. Integration-tests the real requirements.txt / requirements.lock pair, that
   tools/gen_lock.py's RUNTIME_ROOTS mirrors requirements.txt's roots, that
   every requirements-dev.txt entry carries a version specifier, and that
   run.sh / setup.sh are syntactically valid bash.

The suite never touches the network, a database, Discord, or Lavalink: it only
reads text files and parses them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"
REQUIREMENTS_LOCK = REPO_ROOT / "requirements.lock"
REQUIREMENTS_DEV_TXT = REPO_ROOT / "requirements-dev.txt"


# ---------------------------------------------------------------------------
# Pure parsing / checking helpers (no filesystem access - text in, data out)
# ---------------------------------------------------------------------------


def parse_requirements(text: str) -> list[Requirement]:
    """Parse a requirements-file-style text into Requirement objects.

    Skips blank lines and whole-line comments (``#...``); every other line is
    parsed with packaging's own Requirement grammar, so extras (``pkg[extra]``)
    and specifiers (``~=``, ``==``, ...) are handled exactly as pip sees them.
    """
    reqs: list[Requirement] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        reqs.append(Requirement(line))
    return reqs


def parse_lock_pins(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse a lock file's exact pins.

    Returns (canonical name -> pinned version, malformed non-comment lines).
    A line counts as malformed unless it is exactly ``Name==version`` with no
    extra whitespace or additional specifiers - i.e. a real pin, not a range.
    """
    pins: dict[str, str] = {}
    malformed: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            malformed.append(line)
            continue
        name, _, version = line.partition("==")
        name = name.strip()
        version = version.strip()
        if not name or not version or " " in name or " " in version:
            malformed.append(line)
            continue
        pins[canonicalize_name(name)] = version
    return pins, malformed


def find_lock_issues(requirements_text: str, lock_text: str) -> list[str]:
    """The guard itself: every requirement must have a satisfied lock pin.

    Returns a list of human-readable issue strings (empty means clean). Each
    issue is prefixed with its kind (``missing root:``, ``out-of-bounds pin:``
    or ``malformed lock line:``) so callers and tests can match on the prefix
    without parsing English prose.
    """
    issues: list[str] = []

    pins, malformed = parse_lock_pins(lock_text)
    for line in malformed:
        issues.append(f"malformed lock line: {line!r} is not an exact Name==version pin")

    for req in parse_requirements(requirements_text):
        cname = canonicalize_name(req.name)
        if cname not in pins:
            issues.append(f"missing root: {req.name} has no pin in the lock")
            continue
        pinned = pins[cname]
        if not req.specifier.contains(pinned):
            issues.append(
                f"out-of-bounds pin: {req.name} requires "
                f"{req.specifier or 'any version'} but the lock pins {pinned}"
            )
    return issues


# ---------------------------------------------------------------------------
# Negative control - prove the guard actually detects what it claims to
# ---------------------------------------------------------------------------


def test_find_lock_issues_catches_out_of_bounds_and_missing_root():
    # (a) an out-of-bounds pin: foo~=2.0.0 requires >=2.0.0,==2.*, the lock pins
    #     a 1.x version - exactly the shape of the real cryptography 49-vs-50 bug.
    # (b) a missing root: bar is required but absent from the lock entirely.
    synthetic_requirements = "foo~=2.0.0\nbar~=1.0.0\n"
    synthetic_lock = "foo==1.5.0\n"

    issues = find_lock_issues(synthetic_requirements, synthetic_lock)

    assert any(i.startswith("out-of-bounds pin:") and "foo" in i for i in issues), issues
    assert any(i.startswith("missing root:") and "bar" in i for i in issues), issues
    assert len(issues) == 2, issues


def test_find_lock_issues_is_silent_on_a_clean_pair():
    # The counterpart to the negative control: a lock that DOES satisfy every
    # requirement must report nothing, so the guard is not just always-noisy.
    synthetic_requirements = "foo~=2.0.0\n"
    synthetic_lock = "foo==2.0.5\n"
    assert find_lock_issues(synthetic_requirements, synthetic_lock) == []


def test_find_lock_issues_flags_a_non_exact_lock_line():
    synthetic_requirements = "foo~=2.0.0\n"
    synthetic_lock = "foo>=2.0.0\n"
    issues = find_lock_issues(synthetic_requirements, synthetic_lock)
    assert any(i.startswith("malformed lock line:") for i in issues), issues


# ---------------------------------------------------------------------------
# Integration tests - the real files in this repo
# ---------------------------------------------------------------------------


def test_lock_satisfies_every_requirement():
    req_text = REQUIREMENTS_TXT.read_text(encoding="utf-8")
    lock_text = REQUIREMENTS_LOCK.read_text(encoding="utf-8")

    # Guard against a vacuous pass: if parsing ever finds zero requirements
    # (e.g. requirements.txt went missing or the parser broke), an empty
    # issues list would be a false "all clean", not a real one.
    requirements = parse_requirements(req_text)
    assert len(requirements) > 0, "examined zero requirements - guard would pass on anything"

    issues = find_lock_issues(req_text, lock_text)
    assert issues == [], "\n".join(issues)


def test_gen_lock_runtime_roots_mirror_requirements():
    from tools import gen_lock

    req_text = REQUIREMENTS_TXT.read_text(encoding="utf-8")
    req_names = {canonicalize_name(r.name) for r in parse_requirements(req_text)}
    root_names = {canonicalize_name(name) for name in gen_lock.RUNTIME_ROOTS}

    assert req_names, "examined zero requirements - guard would pass on anything"
    assert root_names == req_names, (
        f"tools/gen_lock.py RUNTIME_ROOTS has drifted from requirements.txt: "
        f"only in requirements.txt: {req_names - root_names}; "
        f"only in RUNTIME_ROOTS: {root_names - req_names}"
    )


def test_requirements_dev_entries_are_pinned():
    text = REQUIREMENTS_DEV_TXT.read_text(encoding="utf-8")
    entries = parse_requirements(text)
    assert entries, "examined zero dev requirements - guard would pass on anything"

    unpinned = [str(req) for req in entries if not req.specifier]
    assert unpinned == [], f"requirements-dev.txt entries without a version specifier: {unpinned}"


@pytest.mark.parametrize("script", ["run.sh", "setup.sh"])
def test_scripts_are_syntactically_valid_bash(script: str):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on this host")

    result = subprocess.run(
        [bash, "-n", str(REPO_ROOT / script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
