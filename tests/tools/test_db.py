"""Tests for the shared database helpers (tools/db.py).

``affected_rows`` is the one line both purge paths now depend on to report what
they deleted (the guild retention run and the user-profile forget), so its
degenerate cases are pinned here rather than in either caller.
"""

import pytest

from tools.db import affected_rows, upsert_guild_value


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("DELETE 3", 3),
        ("DELETE 0", 0),
        ("INSERT 0 1", 1),
        ("UPDATE 12", 12),
    ),
)
def test_affected_rows_reads_the_command_tag(status, expected):
    assert affected_rows(status) == expected


@pytest.mark.parametrize("status", (None, "", "DELETE", "nonsense", 7))
def test_an_unparseable_tag_counts_as_zero_instead_of_raising(status):
    """A bad count must never fail a deletion that already committed."""
    assert affected_rows(status) == 0


async def test_upsert_guild_value_refuses_a_bogus_identifier(fake_pool):
    with pytest.raises(ValueError):
        await upsert_guild_value(fake_pool, "prefixes; DROP TABLE x", "prefix", 1, "!")
    assert fake_pool.calls == []
