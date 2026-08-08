"""User-controlled export and avatar-history deletion."""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import zipfile

from tools import settings
from tools.db import affected_rows

EXPORT_ARCHIVE_TARGET_BYTES = 6 * 1024 * 1024
AVATAR_TRACKING_KEY = "avatar_history_tracking"

# One personal-data export per user per hour, wherever it is asked from. Was a
# per-process ``@commands.cooldown(1, 3600, user)`` bucket on ``?mydata export``;
# it is now the window of the DB clock below, so the Discord command and the
# dashboard's ``mydata_export`` queue action share ONE limiter and a restart no
# longer hands out a free export.
EXPORT_COOLDOWN_SECONDS = 3600

# Export schema version. v2 added the social profile: `profile` (user_profiles)
# and `profile_visibility`, and moved the old gamer-ID row to `legacy_profile`.
# v3 added `profile_connections`: the external accounts linked to a profile,
# with the display cache each one keeps. v4 added the two rows the dashboard
# lot created under this user's id: `data_export_requests` (the single timestamp
# the shared export limiter keeps - see claim_export_slot) and
# `dashboard_requests` (the user-scoped rows of the dashboard action queue: what
# was asked, when, and how it ended).
# v5 added `topgg_votes`: the vote ledger (when you last voted for the bot, your
# streak, your lifetime count and the deadline of the XP boost it armed).
# Additive, but a consumer that keys on the version must be able to tell them
# apart.
EXPORT_VERSION = 5

# THE list of tables a profile lives in, deleted together. This mirrors
# retention.GUILD_DELETE_QUERIES for the USER side: profile data is keyed by
# user_id and carries no guild_id, so the guild purge never sees it - it is
# erased here, on the user's own request, or not at all.
# cogs/community/profile/storage.delete_profile delegates to this so the cog and
# the privacy path cannot drift.
PROFILE_DELETE_QUERIES = (
    ("user_profiles", "DELETE FROM user_profiles WHERE user_id = $1"),
    ("profile_visibility", "DELETE FROM profile_visibility WHERE user_id = $1"),
    # The external accounts linked to the profile, and the display cache each
    # one holds. Forgetting a profile that leaves a Steam handle (and the games
    # fetched with it) behind is not forgetting it.
    (
        "profile_connections",
        "DELETE FROM profile_connections WHERE user_id = $1",
    ),
    # The pre-migration table. Still holds the same gamer IDs until it is
    # dropped, so forgetting a profile must clear it too.
    ("profiles", "DELETE FROM profiles WHERE user_id = $1"),
)

# THE WIDER list: everything a profile is, PLUS the user-scoped records that are
# not profile data but are still "about you". Behind `?mydata deleteprofile`
# (delete_user_data) and nothing else.
#
# WHY TWO LISTS. The narrow one above is reached by `/profile clear`, which has
# no confirmation step at all - it deletes inside a `ctx.typing()`. That is fine
# for data the user typed in and can type again, and it is the whole of what
# that command promises ("clear your entire profile, including who could see
# what"). It is NOT fine for a record the user cannot recreate: a member who
# runs `profile clear` to reset a bio must not silently lose an earned vote
# streak and a lifetime count with no undo and no warning. So anything
# unrecreatable goes here instead, on the path that asks first and says what it
# is about to destroy.
#
# The privacy invariant is untouched by the split: every table below is still
# user-erasable on request, still exported, and `?mydata deleteprofile` is the
# erasure verb PRIVACY.md names.
USER_DELETE_QUERIES = PROFILE_DELETE_QUERIES + (
    # The top.gg vote ledger. Not "profile" data in the fields-and-visibility
    # sense, but it is a per-user record of a behaviour ("this person votes for
    # us, and last did on this date") with no other way out, so a forget that
    # left it behind would not be a forget.
    # This is the OPPOSITE call from mydata_export_cooldown, which is exported
    # but deliberately NOT deleted, and the difference is the direction of the
    # gain: deleting the limiter row would HAND the user a fresh export slot,
    # while deleting this one only ever costs them (streak back to 1, boost
    # gone). There is nothing here to game.
    # The row is the durable half; the live XP boost is an in-memory entry on
    # the Leveling cog, dropped by cogs/community/votes.forget_vote_boost at the
    # one call site of delete_user_data.
    ("topgg_votes", "DELETE FROM topgg_votes WHERE user_id = $1"),
)


def _records(rows):
    return [dict(row) for row in rows]


def _json_value(value, default):
    """asyncpg returns JSONB as text (no codec is registered on the pool)."""
    if value is None:
        return default
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


async def _write_avatar_tracking(connection, user_id, enabled):
    await connection.execute(
        "INSERT INTO user_settings (user_id, settings) "
        "VALUES ($1, jsonb_build_object($2::text, $3::boolean)) "
        "ON CONFLICT (user_id) DO UPDATE SET settings = "
        "jsonb_set(user_settings.settings, ARRAY[$2::text], "
        "to_jsonb($3::boolean), true)",
        user_id,
        AVATAR_TRACKING_KEY,
        enabled,
    )


async def set_avatar_tracking(pool, user_id, enabled):
    """Persist avatar consent while serialized against in-flight captures."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.fetchval(
                "SELECT pg_advisory_xact_lock($1)", user_id
            )
            await _write_avatar_tracking(connection, user_id, bool(enabled))
    settings.invalidate_user(user_id)


_CLAIM_EXPORT_SLOT = """
WITH claimed AS (
    INSERT INTO mydata_export_cooldown AS slot (user_id, last_export_at)
    VALUES ($1, now())
    ON CONFLICT (user_id) DO UPDATE SET last_export_at = now()
    WHERE slot.last_export_at <= now() - $2 * INTERVAL '1 second'
    RETURNING 1
)
SELECT
    EXISTS (SELECT 1 FROM claimed) AS granted,
    COALESCE((
        SELECT LEAST(GREATEST(0, CEIL(EXTRACT(EPOCH FROM
            (prior.last_export_at + $2 * INTERVAL '1 second') - now()
        ))), $2)
        FROM mydata_export_cooldown AS prior
        WHERE prior.user_id = $1
    ), 0)::bigint AS retry_after
"""


async def claim_export_slot(pool, user_id, *, cooldown=EXPORT_COOLDOWN_SECONDS):
    """Consume this user's one export per window; return ``(granted, retry_after)``.

    THE gate, shared by ``?mydata export`` and the dashboard's ``mydata_export``
    action, so neither can be used to work around the other. ``granted`` is True
    only for the caller that actually took the slot; every other caller inside
    the window gets ``(False, seconds_left)`` with ``seconds_left`` rounded UP
    (a caller told to wait N seconds and waiting exactly N is never refused
    again for the same reason).

    ``retry_after`` is BOUNDED by the window at both ends: never negative, never
    more than ``cooldown`` seconds. The upper clamp is not cosmetic - a row
    stamped in the future (a backwards clock adjustment on the database host, a
    restored dump) would otherwise be reported verbatim, and the UI would tell
    somebody to come back in a day for an hourly limit. The lower one is applied
    in Python below: a refusal always reports at least one second, so "wait
    ``retry_after`` and try again" is never the advice "retry immediately", which
    would be refused again.

    ONE statement, so the claim is atomic without a transaction: the
    ``INSERT ... ON CONFLICT DO UPDATE ... WHERE`` is the whole test-and-set, and
    two concurrent claims serialise on the row lock - the loser re-evaluates its
    WHERE against the row the winner just wrote and updates nothing, exactly like
    the queue's own single-flight claim. The ``retry_after`` sub-select reads the
    table from the statement's own snapshot, i.e. the row as it was BEFORE the
    CTE's write, which is precisely the deadline a refused caller must be told
    (and is why it is computed in the same statement rather than in a follow-up
    round trip that a concurrent grant could shift).

    The slot is consumed the moment it is granted and is NEVER released, even if
    the export then fails to build or to deliver. That matches the bucket this
    replaces (discord.py charges its cooldown before the callback runs, so a
    closed-DM failure already burned the hour) and it is the safe direction:
    releasing on failure would let a user with DMs closed re-trigger the most
    expensive job the bot has, over and over.
    """
    row = await pool.fetchrow(_CLAIM_EXPORT_SLOT, user_id, cooldown)
    if row is None:  # defensive: a single-row SELECT always returns one
        return False, int(cooldown)
    if row["granted"]:
        return True, 0
    # Floor a refusal at one second. The sub-select reads the row from the
    # statement's own snapshot, so a caller that loses a race on the very last
    # tick of the window can compute 0 from a row the winner has already moved;
    # reporting "retry in 0s" would send it straight back into a refusal.
    return False, max(1, int(row["retry_after"] or 0))


async def collect_user_export(pool, user_id):
    """Collect exportable personal data without ever reading OAuth ciphertext."""
    preferences = await pool.fetchval(
        "SELECT settings FROM user_settings WHERE user_id = $1", user_id
    )
    if isinstance(preferences, str):
        preferences = json.loads(preferences)
    elif preferences is not None:
        preferences = dict(preferences)

    legacy_profile = await pool.fetchrow(
        "SELECT switch_fc, threeds_fc, battletag, riotid, steamid "
        "FROM profiles WHERE user_id = $1",
        user_id,
    )
    social_profile = await pool.fetchrow(
        "SELECT bio, pronouns, accent, custom_fields, gaming_ids, "
        "created_at, updated_at FROM user_profiles WHERE user_id = $1",
        user_id,
    )
    profile_visibility = await pool.fetch(
        "SELECT field, level FROM profile_visibility "
        "WHERE user_id = $1 ORDER BY field",
        user_id,
    )
    # Handles are public by nature (a username, a SteamID64) and the payload is
    # the display cache the profile card draws, so both belong in the archive.
    # No credential can appear here: none is stored in this table by design
    # (see schema.sql), which is why - unlike anilist_tokens above - the whole
    # row is selected rather than a redacted projection.
    profile_connections = await pool.fetch(
        "SELECT connector, external_id, display_name, linked_at, "
        "last_refresh, payload FROM profile_connections "
        "WHERE user_id = $1 ORDER BY connector",
        user_id,
    )
    token = await pool.fetchrow(
        "SELECT expires FROM anilist_tokens WHERE user_id = $1", user_id
    )
    airing = await pool.fetchrow(
        "SELECT anilist_user_id, enabled, created_at "
        "FROM anilist_airing_optins WHERE user_id = $1",
        user_id,
    )
    chapters = await pool.fetchrow(
        "SELECT anilist_user_id, enabled, created_at "
        "FROM anilist_chapter_optins WHERE user_id = $1",
        user_id,
    )
    afk = await pool.fetchrow(
        "SELECT message, since FROM afk WHERE user_id = $1", user_id
    )
    # The export limiter's own row (see claim_export_slot). One timestamp, but it
    # is stored under this user's id, so it is theirs and it ships - the
    # structural guard in tests/tools/test_privacy.py holds every user-scoped
    # table to that rule precisely so a new one cannot be quietly left out.
    export_slot = await pool.fetchrow(
        "SELECT last_export_at FROM mydata_export_cooldown WHERE user_id = $1",
        user_id,
    )
    # The top.gg vote ledger: the whole row, because every column of it is a
    # fact about this user and none of it is a credential. Absent row = never
    # voted, stated as null below rather than as an invented zero.
    votes = await pool.fetchrow(
        "SELECT last_vote_at, streak, total_votes, boost_expires_at "
        "FROM topgg_votes WHERE user_id = $1",
        user_id,
    )
    # The dashboard -> bot queue, USER-scoped rows ONLY (WHERE user_id, never
    # WHERE id): "you asked for this, at this time, and here is how it ended".
    # Guild-scoped rows of the same table belong to the guild, are gated by
    # manage-guild and die with it (tools/retention.GUILD_DELETE_QUERIES); they
    # are none of this user's business and the predicate cannot reach them (a
    # guild row carries user_id NULL). ``payload`` is deliberately not selected:
    # it is written by the dashboard, is empty for every user kind today, and
    # would be the one field able to carry something that is not this user's.
    dashboard_requests = await pool.fetch(
        "SELECT id, kind, status, result, requested_by, created_at, updated_at "
        "FROM dashboard_actions WHERE user_id = $1 ORDER BY id",
        user_id,
    )
    favorites = await pool.fetch(
        "SELECT identifier, title, author, uri, source_name, added_at "
        "FROM music_favorites WHERE user_id = $1 ORDER BY added_at",
        user_id,
    )
    levels = await pool.fetch(
        "SELECT guild_id, xp FROM levels WHERE user_id = $1 ORDER BY guild_id",
        user_id,
    )
    periods = await pool.fetch(
        "SELECT guild_id, period_key, xp FROM xp_period "
        "WHERE user_id = $1 ORDER BY guild_id, period_key",
        user_id,
    )
    warns = await pool.fetch(
        "SELECT guild_id, warns_count FROM warns "
        "WHERE user_id = $1 ORDER BY guild_id",
        user_id,
    )
    cases = await pool.fetch(
        "SELECT guild_id, case_number, action, reason, expires, created_at "
        "FROM cases WHERE user_id = $1 ORDER BY created_at",
        user_id,
    )
    moderated_cases = await pool.fetch(
        "SELECT guild_id, case_number, user_id AS target_user_id, action, "
        "reason, expires, created_at FROM cases "
        "WHERE moderator_id = $1 ORDER BY created_at",
        user_id,
    )
    reminders = await pool.fetch(
        "SELECT id, expires, created, extra FROM timers "
        "WHERE event = 'reminder' "
        "AND extra->>'author_id' = ($1::bigint)::text "
        "ORDER BY created",
        user_id,
    )
    playlists = await pool.fetch(
        "SELECT guild_id, name, track_count, total_ms, created_at "
        "FROM guild_playlists WHERE creator_id = $1 ORDER BY created_at",
        user_id,
    )
    custom_commands = await pool.fetch(
        "SELECT guild_id, name, response, uses, created_at "
        "FROM custom_commands "
        "WHERE created_by = $1 ORDER BY created_at",
        user_id,
    )
    avatar_rows = await pool.fetch(
        "SELECT id, guild_id, kind, ref, image_format, changed_at, avatar "
        "FROM avatar_history WHERE user_id = $1 "
        "ORDER BY changed_at, id",
        user_id,
    )

    if social_profile is not None:
        social_profile = dict(social_profile)
        social_profile["custom_fields"] = _json_value(
            social_profile.get("custom_fields"), []
        )
        social_profile["gaming_ids"] = _json_value(
            social_profile.get("gaming_ids"), {}
        )

    connections = []
    for row in profile_connections:
        connection = dict(row)
        # asyncpg hands JSONB back as text, so decode it: the archive must hold
        # the cache as an object, not a quoted blob nobody can read.
        connection["payload"] = _json_value(connection.get("payload"), {})
        connections.append(connection)

    data = {
        "export_version": EXPORT_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc),
        "user_id": user_id,
        "preferences": preferences or {},
        "profile": social_profile,
        # Absent row = private, so a field missing here is one the user never
        # published. The export states the rows, not the defaults.
        "profile_visibility": _records(profile_visibility),
        "profile_connections": connections,
        "legacy_profile": dict(legacy_profile) if legacy_profile else None,
        "anilist": {
            # Deliberately report linkage/expiry without selecting the encrypted
            # token. Neither ciphertext nor plaintext can enter the archive.
            "linked": token is not None,
            "token_expires": token["expires"] if token else None,
            "airing_notifications": dict(airing) if airing else None,
            "chapter_notifications": dict(chapters) if chapters else None,
        },
        "afk": dict(afk) if afk else None,
        "data_export_requests": dict(export_slot) if export_slot else None,
        "topgg_votes": dict(votes) if votes else None,
        # asyncpg hands JSONB back as text (no codec on the pool), so ``result``
        # is decoded rather than embedded as a quoted blob.
        "dashboard_requests": [
            {**dict(row), "result": _json_value(row["result"], None)}
            for row in dashboard_requests
        ],
        "music_favorites": _records(favorites),
        "levels": _records(levels),
        "period_xp": _records(periods),
        "warnings": _records(warns),
        "moderation_cases_as_target": _records(cases),
        "moderation_cases_as_moderator": _records(moderated_cases),
        "pending_reminders": _records(reminders),
        "guild_playlists_created": _records(playlists),
        "custom_commands_created": _records(custom_commands),
    }
    return data, avatar_rows


def _json_default(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"unsupported export value: {type(value).__name__}")


def _avatar_filename(row):
    changed = row["changed_at"]
    stamp = (
        changed.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if changed is not None
        else "unknown"
    )
    scope = (
        f"guild-{row['guild_id']}"
        if row["guild_id"] is not None
        else "global"
    )
    extension = "webp" if row["image_format"] == "webp" else "png"
    return (
        f"avatars/{row['kind']}/{scope}/"
        f"{stamp}-{row['id']}.{extension}"
    )


def build_export_archives(
    data,
    avatar_rows,
    *,
    target_bytes=EXPORT_ARCHIVE_TARGET_BYTES,
):
    """Build bounded ZIP parts containing JSON metadata and every avatar blob."""
    avatars = []
    groups = [[]]
    current_bytes = 0

    for row in avatar_rows:
        raw = bytes(row["avatar"])
        filename = _avatar_filename(row)
        avatars.append(
            {
                "id": row["id"],
                "guild_id": row["guild_id"],
                "kind": row["kind"],
                "ref": row["ref"],
                "image_format": row["image_format"],
                "changed_at": row["changed_at"],
                "filename": filename,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
        if groups[-1] and current_bytes + len(raw) > target_bytes:
            groups.append([])
            current_bytes = 0
        groups[-1].append((filename, raw))
        current_bytes += len(raw)

    manifest = dict(data)
    manifest["avatar_history"] = avatars
    manifest_json = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    ).encode("utf-8")

    archives = []
    total_parts = len(groups)
    for index, files in enumerate(groups, start=1):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
            if index == 1:
                archive.writestr(
                    "data.json",
                    manifest_json,
                    compress_type=zipfile.ZIP_DEFLATED,
                )
            else:
                archive.writestr(
                    "part.json",
                    json.dumps(
                        {
                            "user_id": data["user_id"],
                            "part": index,
                            "parts": total_parts,
                        },
                        indent=2,
                    ),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
            for filename, raw in files:
                # PNG and WebP are already compressed. Storing them avoids
                # wasting CPU for effectively no size reduction.
                archive.writestr(
                    filename, raw, compress_type=zipfile.ZIP_STORED
                )
        output.seek(0)
        archives.append((f"yasuho-data-{index}-of-{total_parts}.zip", output))
    return archives


async def _delete_tables(pool, user_id, queries):
    """Run one erasure list in ONE transaction; return per-table row counts.

    Every table in the list dies together or none does, so a half-forgotten user
    (say the profile fields gone but the visibility rows, or a linked Steam
    handle, left behind) cannot exist.
    """
    counts = {}
    async with pool.acquire() as connection:
        async with connection.transaction():
            for table, query in queries:
                status = await connection.execute(query, user_id)
                counts[table] = affected_rows(status)
    return counts


async def delete_user_profile(pool, user_id):
    """Erase a user's whole PROFILE in one transaction; per-table counts.

    :data:`PROFILE_DELETE_QUERIES` only - the data the user typed in and can
    type again. This is what `/profile clear` runs, and that command asks for no
    confirmation, so the list it reaches must never grow to hold something its
    owner cannot recreate. Nothing is cached in memory, so there is no
    invalidation to do afterwards.
    """
    return await _delete_tables(pool, user_id, PROFILE_DELETE_QUERIES)


async def delete_user_data(pool, user_id):
    """Erase everything /mydata promises, in one transaction; per-table counts.

    :data:`USER_DELETE_QUERIES`: the profile, plus the user-scoped records that
    are not profile data (today, the top.gg vote ledger). The wider, one-way
    verb, and the reason it is a separate function is that its only caller sits
    behind a confirmation button that names what is about to be destroyed.

    In-memory twins of the deleted rows (the presence opt-in set, the live XP
    boost) are dropped by that caller, right after this returns.
    """
    return await _delete_tables(pool, user_id, USER_DELETE_QUERIES)


async def delete_user_avatar_history(pool, user_id):
    """Delete every avatar row and atomically disable future capture."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.fetchval(
                "SELECT pg_advisory_xact_lock($1)", user_id
            )
            await _write_avatar_tracking(connection, user_id, False)
            row = await connection.fetchrow(
                "WITH deleted AS ("
                "DELETE FROM avatar_history WHERE user_id = $1 "
                "RETURNING octet_length(avatar) AS bytes"
                ") SELECT COUNT(*)::integer AS deleted_count, "
                "COALESCE(SUM(bytes), 0)::bigint AS deleted_bytes FROM deleted",
                user_id,
            )
    settings.invalidate_user(user_id)
    return int(row["deleted_count"]), int(row["deleted_bytes"])


async def store_avatar_if_tracking(
    pool,
    *,
    user_id,
    guild_id,
    kind,
    ref,
    avatar,
    history_limit,
):
    """Atomically recheck consent, store one image and enforce its series cap.

    The same per-user transaction lock is used by deletion. An in-flight
    capture therefore either commits before deletion and is removed by it, or
    observes the opt-out after deletion and stores nothing.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.fetchval(
                "SELECT pg_advisory_xact_lock($1)", user_id
            )
            enabled = await connection.fetchval(
                "SELECT COALESCE(("
                "SELECT (settings->>$2)::boolean FROM user_settings "
                "WHERE user_id = $1"
                "), TRUE)",
                user_id,
                AVATAR_TRACKING_KEY,
            )
            if not enabled:
                return False
            await connection.execute(
                "INSERT INTO avatar_history("
                "user_id, guild_id, kind, ref, avatar, image_format"
                ") VALUES($1, $2, $3, $4, $5, 'webp')",
                user_id,
                guild_id,
                kind,
                ref,
                avatar,
            )
            await connection.execute(
                "DELETE FROM avatar_history "
                "WHERE user_id = $1 AND kind = $2 "
                "AND guild_id IS NOT DISTINCT FROM $3 "
                "AND id NOT IN ("
                "SELECT id FROM avatar_history "
                "WHERE user_id = $1 AND kind = $2 "
                "AND guild_id IS NOT DISTINCT FROM $3 "
                "ORDER BY changed_at DESC, id DESC LIMIT $4)",
                user_id,
                kind,
                guild_id,
                history_limit,
            )
    return True
