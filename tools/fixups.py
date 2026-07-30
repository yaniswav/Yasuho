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
- There are NO checksums and NO version pins. A ``name`` recorded in
  ``applied_fixups`` that the running code no longer knows about is simply
  IGNORED - rolling back to an older commit never refuses to boot. (The order of
  ``FIXUPS`` is still the order they run in within one boot, and one pair below
  depends on it; nothing is enforced across boots.)
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

# Copy the legacy `profiles` gamer IDs into user_profiles.gaming_ids, the JSONB
# section the new profile package reads. A one-shot copy rather than a permanent
# read-through: the P2 card and the dashboard then need ONE row per profile, and
# adding a sixth gamer ID becomes a line in the Python registry instead of an
# ALTER TABLE. The old table is deliberately NOT dropped (it stays the safety net
# and is still exported/forgotten by tools/privacy.py).
# Idempotent twice over: only users who actually have a legacy value are touched,
# and on conflict the EXISTING keys win (`EXCLUDED || user_profiles` puts the
# stored object on the right), so a repeat can never overwrite an edit made
# through the new package.
_PROFILE_GAMING_IDS_IMPORT = Fixup(
    "user_profiles_import_legacy_gaming_ids",
    """
    INSERT INTO user_profiles (user_id, gaming_ids)
    SELECT p.user_id, jsonb_strip_nulls(jsonb_build_object(
        'switch',    p.switch_fc,
        '3ds',       p.threeds_fc,
        'battletag', p.battletag,
        'riot',      p.riotid,
        'steam_id',  p.steamid
    ))
    FROM profiles AS p
    WHERE COALESCE(p.switch_fc, p.threeds_fc, p.battletag, p.riotid, p.steamid)
          IS NOT NULL
    ON CONFLICT (user_id) DO UPDATE
    SET gaming_ids = EXCLUDED.gaming_ids || user_profiles.gaming_ids,
        updated_at = now()
    """,
)

# Keep migrated gamer IDs as visible as they already were. Everything NEW in the
# profile is born private, but these IDs were readable by anyone who could run
# `?profile view @them` before this lot shipped; silently blanking every existing
# profile would be a surprise, and 'server' is the honest equivalent of the old
# guild-only command. Nothing is made MORE visible than it was.
#
# Self-idempotence needs more than `ON CONFLICT DO NOTHING`, because 'private' is
# stored as the ABSENCE of a row: a user who migrates, then deliberately sets
# gaming_ids to private (deleting the row), would be silently re-published by a
# replay - DO NOTHING has no row to conflict with. The `NOT EXISTS` guard closes
# that hole structurally, which is why this fixup is ordered BEFORE the import:
#   * first run  - user_profiles is still empty for these users, so every
#                  migrated user is seeded, then the import creates their row;
#   * any replay - the import has since created the row, so the guard matches
#                  nothing and the seed is a true no-op, private stays private.
# The guard is "has this user a new-package profile at all", not "has this user a
# visibility row": that is precisely the signal that distinguishes "never
# migrated" from "migrated, and has since had a say".
_PROFILE_VISIBILITY_SEED = Fixup(
    "profile_visibility_seed_legacy_gaming_ids",
    """
    INSERT INTO profile_visibility (user_id, field, level)
    SELECT p.user_id, 'gaming_ids', 'server'
    FROM profiles AS p
    WHERE COALESCE(p.switch_fc, p.threeds_fc, p.battletag, p.riotid, p.steamid)
          IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM user_profiles AS up WHERE up.user_id = p.user_id
      )
    ON CONFLICT (user_id, field) DO NOTHING
    """,
)

# The ordered set of fixups the running code knows about. Append new fixups here;
# never renumber or remove-and-reuse a name.
# ORDER IS LOAD-BEARING for the two profile ones: the visibility seed reads
# "this user has no user_profiles row yet" as "not migrated yet", so it MUST run
# before the import that creates those rows. See the seed's comment above.
FIXUPS = (
    _WARNS_RECOMPUTE,
    _PROFILE_VISIBILITY_SEED,
    _PROFILE_GAMING_IDS_IMPORT,
)

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
