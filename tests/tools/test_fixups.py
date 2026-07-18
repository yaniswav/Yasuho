"""Tests for the one-shot data-fixups runner (tools/fixups.py).

The runner replaced the checksum-pinned migration framework. These tests pin the
new contract: fixups are recorded once, are idempotent across runs, a failing
fixup is logged-and-skipped (never blocks startup) and is NOT recorded, and names
already in ``applied_fixups`` that the code no longer knows about are ignored.
"""

import pytest

from tools import fixups
from tools.fixups import Fixup


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Transaction:
    """Models a real transaction: staged inserts commit on clean exit only."""

    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.staged = set()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        staged = self.connection.staged
        self.connection.staged = None
        if exc_type is None:
            self.connection.recorded |= staged
        # propagate any exception so run_fixups can log-and-skip it
        return False


class _Connection:
    """In-memory stand-in for an asyncpg connection.

    ``recorded`` mirrors the applied_fixups table. Any fixup whose SQL contains a
    token in ``fail_on`` raises, exercising the log-and-skip path.
    """

    def __init__(self, *, recorded=(), fail_on=()):
        self.recorded = set(recorded)
        self.staged = None
        self.fail_on = set(fail_on)
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        for token in self.fail_on:
            if token in query:
                raise RuntimeError(f"simulated failure: {token}")
        if query.startswith("INSERT INTO applied_fixups"):
            target = self.staged if self.staged is not None else self.recorded
            target.add(args[0])
        return "OK"

    async def fetch(self, query, *args):
        assert "applied_fixups" in query
        return [{"name": name} for name in sorted(self.recorded)]

    def transaction(self):
        return _Transaction(self)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


def _fixup(name, *, fail=False):
    # A failing fixup carries a unique token in its SQL that the fake raises on.
    body = f"FAIL::{name}" if fail else f"SELECT '{name}'"
    return Fixup(name, body)


async def test_applies_unapplied_fixups_and_records_them():
    connection = _Connection()
    fx = (_fixup("alpha"), _fixup("beta"))

    applied = await fixups.run_fixups(_Pool(connection), fx)

    assert applied == ["alpha", "beta"]
    assert connection.recorded == {"alpha", "beta"}
    # The bookkeeping table is ensured before anything runs.
    assert connection.executed[0][0].startswith(
        "CREATE TABLE IF NOT EXISTS applied_fixups"
    )


async def test_skips_already_applied_fixups():
    connection = _Connection(recorded={"alpha"})
    fx = (_fixup("alpha"), _fixup("beta"))

    applied = await fixups.run_fixups(_Pool(connection), fx)

    assert applied == ["beta"]
    # alpha's body must not have been executed again.
    assert not any("SELECT 'alpha'" in q for q, _ in connection.executed)


async def test_is_idempotent_across_runs():
    connection = _Connection()
    fx = (_fixup("alpha"), _fixup("beta"))

    first = await fixups.run_fixups(_Pool(connection), fx)
    second = await fixups.run_fixups(_Pool(connection), fx)

    assert first == ["alpha", "beta"]
    assert second == []
    assert connection.recorded == {"alpha", "beta"}


async def test_unknown_recorded_names_are_ignored():
    # A name recorded by a newer commit that this (older) code does not know
    # about must not cause any error - it is simply left untouched.
    connection = _Connection(recorded={"from_the_future"})
    fx = (_fixup("alpha"),)

    applied = await fixups.run_fixups(_Pool(connection), fx)

    assert applied == ["alpha"]
    assert "from_the_future" in connection.recorded


async def test_failing_fixup_is_skipped_not_recorded_and_does_not_block():
    connection = _Connection(fail_on={"FAIL::beta"})
    fx = (_fixup("alpha"), _fixup("beta", fail=True), _fixup("gamma"))

    # Must not raise despite beta failing mid-transaction.
    applied = await fixups.run_fixups(_Pool(connection), fx)

    # beta is skipped; the run continues and gamma still applies.
    assert applied == ["alpha", "gamma"]
    assert connection.recorded == {"alpha", "gamma"}
    assert "beta" not in connection.recorded


async def test_run_fixups_defaults_to_the_real_registry():
    # Called with no explicit list, the runner applies the real FIXUPS and
    # records each by name (default-argument wiring the runner relies on).
    connection = _Connection()

    applied = await fixups.run_fixups(_Pool(connection))

    expected = [item.name for item in fixups.FIXUPS]
    assert applied == expected
    assert "warns_count_recompute_from_cases" in connection.recorded


async def test_real_warns_recompute_fixup_is_registered_and_shaped():
    names = [item.name for item in fixups.FIXUPS]
    assert "warns_count_recompute_from_cases" in names
    (warns_fixup,) = [
        item
        for item in fixups.FIXUPS
        if item.name == "warns_count_recompute_from_cases"
    ]
    # Ground-truth recompute from the cases table; no destructive DELETE.
    assert "UPDATE warns" in warns_fixup.sql
    assert "FROM cases" in warns_fixup.sql
    assert "DELETE" not in warns_fixup.sql.upper()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
