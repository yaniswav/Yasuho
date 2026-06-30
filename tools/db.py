"""Small shared database helpers built on the asyncpg pool.

These helpers centralise SQL that several cogs would otherwise hand-roll. They
deliberately do NOT touch any in-memory caches the cogs keep; cache writes stay
at the call sites so each cog owns its own invalidation.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# table and column are FIXED internal identifiers chosen by us, never user
# input. We still validate them so a typo fails loudly instead of producing a
# malformed (or unsafe) query when interpolated into the SQL text.
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _validate_identifier(kind, value):
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ValueError(f"invalid {kind} identifier: {value!r}")
    return value


async def upsert_guild_value(pool, table, column, guild_id, value):
    """Insert or update a single per-guild column keyed on guild_id.

    Runs an INSERT ... ON CONFLICT (guild_id) DO UPDATE so the row is created
    on first write and overwritten thereafter. ``table`` and ``column`` are
    fixed internal identifiers (never user input); they are validated against
    ^[a-z_][a-z0-9_]*$ and a ValueError is raised before any SQL is built.
    """

    _validate_identifier("table", table)
    _validate_identifier("column", column)

    query = (
        f"INSERT INTO {table} (guild_id, {column}) VALUES ($1, $2) "
        f"ON CONFLICT (guild_id) DO UPDATE SET {column} = EXCLUDED.{column}"
    )
    log.debug("upsert_guild_value table=%s column=%s guild_id=%s", table, column, guild_id)
    return await pool.execute(query, guild_id, value)
