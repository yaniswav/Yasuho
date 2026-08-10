"""Guard: ``schema.sql`` is the ONLY place DDL is written.

The dashboard team's harness treats ``schema.sql`` as the authoritative schema:
it reads that one file to know every table, column, index and constraint the bot
relies on. That contract only holds if production code never quietly issues DDL
of its own - a ``CREATE TABLE`` buried in a cog would make ``schema.sql`` a lie
the moment it drifted, and the harness would never see it.

This test scans the production Python (``cogs/``, ``tools/`` and ``core.py``) for
DDL statements inside string literals and FAILS if any lives outside
``schema.sql``, with ONE explicit, allowlisted exception documented below.

MAINTAINER NOTE
---------------
If this test fails because you added a table/column/index/constraint: put the DDL
in ``schema.sql`` (it is applied on every boot, idempotently), NOT in a cog or a
tool. ``schema.sql`` is the source of truth; code that creates schema behind its
back breaks the dashboard's view of the database. The single allowlisted
exception is a redundant defensive ``CREATE TABLE IF NOT EXISTS applied_fixups``
in ``tools/fixups.py`` - that table is ALSO defined in ``schema.sql``, so it is a
belt-and-braces create, not a divergence. Do not add new entries to the allowlist
to silence a real finding; add the DDL to ``schema.sql`` instead.
"""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The DDL verb+object patterns, matched case-insensitively. They are anchored on
# the VERB followed by its OBJECT so the words appearing in an ordinary English
# sentence (e.g. "we may alter the table later", "drop me a line") never match -
# only a real statement like ``ALTER TABLE`` or ``DROP COLUMN`` does. This mirrors
# exactly the set the schema harness cares about: table/index/constraint DDL.
DDL_PATTERN = re.compile(
    r"CREATE\s+(?:TABLE|INDEX|UNIQUE)"
    r"|ALTER\s+TABLE"
    r"|ADD\s+CONSTRAINT"
    r"|DROP\s+(?:TABLE|COLUMN)",
    re.IGNORECASE,
)

# The ONE sanctioned exception: tools/fixups.py creates ``applied_fixups`` before
# it can record which one-shot fixups ran. That table is ALSO in schema.sql, so
# this is a redundant defensive create, not a schema that only code knows about.
# An offending string literal is allowlisted only if it is in this file AND names
# this table - nothing else in tools/fixups.py gets a pass.
ALLOWLIST_PATH = "tools/fixups.py"
ALLOWLIST_TABLE = "applied_fixups"

# String-literal token kinds. FSTRING_MIDDLE only exists on Python 3.12+, where an
# f-string's literal text is tokenised separately from its ``{...}`` fields; we
# include it so DDL smuggled into an f-string is caught too. COMMENT tokens are
# NOT in this set, which is exactly how a ``# ALTER TABLE ...`` comment (there is
# one in tools/fixups.py) is kept from being a false positive.
_STRING_TOKEN_TYPES = {tokenize.STRING}
if hasattr(tokenize, "FSTRING_MIDDLE"):  # pragma: no cover - version dependent
    _STRING_TOKEN_TYPES.add(tokenize.FSTRING_MIDDLE)


def _production_python_files():
    """Every production .py file the guard scans: cogs/, tools/ and core.py.

    tests/, locales/ and schema.sql itself are deliberately NOT scanned - the
    first two are not production, and schema.sql is the source of truth the guard
    protects (it is not even Python).
    """
    files = []
    for package in ("cogs", "tools"):
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    core = REPO_ROOT / "core.py"
    if core.exists():
        files.append(core)
    return files


def _string_literals(path):
    """Yield ``(lineno, text)`` for every string-literal token in ``path``.

    Only string literals are yielded, so comments and bare code are never
    examined; docstrings are strings and so are included, but the anchored DDL
    pattern keeps ordinary prose in them from matching.
    """
    with tokenize.open(path) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in _STRING_TOKEN_TYPES:
                yield token.start[0], token.string


def _is_allowlisted(rel_path, literal):
    """The lone exception: the applied_fixups create in tools/fixups.py."""
    return rel_path == ALLOWLIST_PATH and ALLOWLIST_TABLE in literal


def _snippet(literal, match):
    """A one-line excerpt of the offending statement for the failure message."""
    start = match.start()
    excerpt = literal[start : start + 80]
    return " ".join(excerpt.split())


def test_ddl_lives_only_in_schema_sql():
    """Fail loudly, naming file:line and the statement, if any production string
    literal outside schema.sql carries DDL (bar the one allowlisted create)."""
    violations = []
    for path in _production_python_files():
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        for lineno, literal in _string_literals(path):
            match = DDL_PATTERN.search(literal)
            if match is None:
                continue
            if _is_allowlisted(rel_path, literal):
                continue
            violations.append(f"{rel_path}:{lineno}: {_snippet(literal, match)}")

    assert not violations, (
        "DDL found outside schema.sql. schema.sql is the ONLY source of truth "
        "for the database schema (the dashboard harness reads it); move this DDL "
        "there instead of issuing it from code:\n  " + "\n  ".join(violations)
    )
