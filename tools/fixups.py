"""One-shot, idempotent data repairs ("fixups") for Yasuho.

This is deliberately NOT a versioned migration framework. ``schema.sql`` remains
THE schema source of truth and is applied on every boot (idempotent CREATE ...
IF NOT EXISTS, additive ALTERs and guarded NOT VALID constraints). This module
only carries the handful of one-shot DATA repairs that DDL alone cannot express
(e.g. recomputing a counter that an old code path let drift).

Design invariants (the anti-brick posture):
- Each fixup has a stable ``name`` recorded in ``applied_fixups`` once it
  succeeds, so it runs at most once.
- Each fixup's SQL MUST itself be idempotent, so a repeated or partial run can
  never corrupt data.
- There are NO checksums and NO ordering pins. A ``name`` recorded in
  ``applied_fixups`` that the running code no longer knows about is simply
  IGNORED - rolling back to an older commit never refuses to boot.
- A failing fixup is logged and skipped; it NEVER blocks startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fixup:
    """A named, idempotent one-shot data repair expressed as a single SQL body."""

    name: str
    sql: str


# Recompute warns.warns_count from the authoritative `cases` rows. The old
# multi-statement warn-removal path could leave that denormalised counter drifted
# (even negative); COUNT(*) of this (guild, user)'s 'warn' cases is the ground
# truth. Idempotent: a second run recomputes the same non-negative value, and the
# result always satisfies the warns_count_nonnegative CHECK.
_WARNS_RECOMPUTE = Fixup(
    "warns_count_recompute_from_cases",
    """
    UPDATE warns AS w
    SET warns_count = (
        SELECT COUNT(*)::integer
        FROM cases AS c
        WHERE c.guild_id = w.guild_id
          AND c.user_id = w.user_id
          AND c.action = 'warn'
    )
    """,
)

# The ordered set of fixups the running code knows about. Append new fixups here;
# never renumber or remove-and-reuse a name.
FIXUPS = (_WARNS_RECOMPUTE,)

_APPLIED_FIXUPS_DDL = (
    "CREATE TABLE IF NOT EXISTS applied_fixups ("
    "name TEXT PRIMARY KEY, "
    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    ")"
)


async def run_fixups(pool, fixups=FIXUPS):
    """Apply every not-yet-applied data fixup; return the names newly applied.

    Never raises for a failing fixup: the error is logged and the run continues,
    so a bad repair can never block startup. A fixup whose name is already in
    ``applied_fixups`` is skipped; names in that table that we no longer know
    about are ignored (rollback-safe). Each fixup and its bookkeeping insert run
    in one transaction, so a failed fixup is not recorded and can retry next boot.
    """
    applied_now = []
    async with pool.acquire() as connection:
        await connection.execute(_APPLIED_FIXUPS_DDL)
        rows = await connection.fetch("SELECT name FROM applied_fixups")
        already_applied = {row["name"] for row in rows}

        for fixup in fixups:
            if fixup.name in already_applied:
                continue
            try:
                async with connection.transaction():
                    await connection.execute(fixup.sql)
                    await connection.execute(
                        "INSERT INTO applied_fixups (name) VALUES ($1) "
                        "ON CONFLICT (name) DO NOTHING",
                        fixup.name,
                    )
            except Exception:
                log.exception(
                    "Data fixup %s failed; continuing startup", fixup.name
                )
                continue
            applied_now.append(fixup.name)

    return applied_now
