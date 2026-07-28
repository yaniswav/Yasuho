"""Purpose: every SQL statement the collectors run, each one a SINGLE
parameterized command (asyncpg's extended query protocol prepares exactly one).

Three statements, no more: the batched flush, the once-a-day member-count
snapshot, and the bounded 90-day prune. All are ADDITIVE upserts or bounded
deletes - nothing here ever reads a row back to decide what to write.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

# One round trip per flush writes BOTH aggregates. unnest turns the parallel
# arrays (buffer.build_flush_payload) into rows; each upsert ADDS its batch onto
# whatever the row already holds, so a flush is idempotent-by-addition and a day
# can be written by any number of ticks.
#
# This is ONE command, not several joined by ';': asyncpg prepares a single
# statement whenever arguments are passed, and PostgreSQL guarantees every
# data-modifying CTE in a WITH clause runs exactly once, in full, even when the
# primary SELECT never reads it (see "Data-Modifying Statements in WITH").
#
# NOTE on ON CONFLICT: a single INSERT may not touch the same row twice
# ("cannot affect row a second time"). The batches come straight from dict keys,
# so (guild, channel, day) and (guild, day) are unique by construction - the
# buffer's dict IS the dedup.
#
# Either batch may be empty: unnest over empty arrays yields zero rows and the
# matching INSERT ... SELECT writes nothing.
FLUSH = """
    WITH message_batch(guild_id, channel_id, day, messages) AS (
        SELECT * FROM unnest($1::bigint[], $2::bigint[], $3::date[], $4::integer[])
    ), day_batch(guild_id, day, joins, leaves) AS (
        SELECT * FROM unnest($5::bigint[], $6::date[], $7::integer[], $8::integer[])
    ), message_upsert AS (
        INSERT INTO server_stats_messages (guild_id, channel_id, day, messages)
        SELECT guild_id, channel_id, day, messages FROM message_batch
        ON CONFLICT (guild_id, channel_id, day)
        DO UPDATE SET messages = server_stats_messages.messages + EXCLUDED.messages
    ), day_upsert AS (
        INSERT INTO server_stats_days (guild_id, day, joins, leaves)
        SELECT guild_id, day, joins, leaves FROM day_batch
        ON CONFLICT (guild_id, day)
        DO UPDATE SET joins = server_stats_days.joins + EXCLUDED.joins,
                      leaves = server_stats_days.leaves + EXCLUDED.leaves
    )
    SELECT 1;
    """

# Daily member-count snapshot: one row per cached guild, written on the first
# flush of a new UTC day. REPLACES (never adds) the day's value, so re-running it
# - which a restart does, since the once-a-day marker lives in memory - is a
# harmless no-op rewrite rather than a doubling.
SNAPSHOT_MEMBER_COUNT = """
    INSERT INTO server_stats_days (guild_id, day, member_count)
    SELECT guild_id, $2::date, member_count
    FROM unnest($1::bigint[], $3::integer[]) AS snapshot(guild_id, member_count)
    ON CONFLICT (guild_id, day)
    DO UPDATE SET member_count = EXCLUDED.member_count;
    """

# Bounded 90-day prune, both tables in one command. The LIMITed ctid sub-select
# bounds BOTH halves of the work: it caps how many rows are deleted, and - because
# it runs as an InitPlan feeding a Tid Scan - it also caps how many rows are ever
# LOOKED AT, so a first run over a long-neglected table can never take a
# table-wide lock for minutes. The caller repeats the statement until a short
# batch comes back.
#
# The `ctid = ANY(ARRAY(...))` shape is LOAD BEARING, not style: the obvious
# `DELETE ... USING stale AS s WHERE target.ctid = s.ctid` form was measured on a
# real 316800-row table and plans as a Seq Scan of the WHOLE table plus an
# external merge sort on ctid (212 ms and 5 MB of temp spill PER BATCH, growing
# with the table). This form plans as `Tid Scan (TID Cond: ctid = ANY ($0))` with
# no seq scan, no sort and no temp file: 29 ms on the same data, flat in table
# size. Do not "simplify" it back to a join.
PRUNE = """
    WITH pruned_messages AS (
        DELETE FROM server_stats_messages
        WHERE ctid = ANY(ARRAY(
            SELECT ctid FROM server_stats_messages WHERE day < $1 LIMIT $2
        ))
        RETURNING 1
    ), pruned_days AS (
        DELETE FROM server_stats_days
        WHERE ctid = ANY(ARRAY(
            SELECT ctid FROM server_stats_days WHERE day < $1 LIMIT $2
        ))
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM pruned_messages) AS messages,
           (SELECT count(*) FROM pruned_days) AS days;
    """
