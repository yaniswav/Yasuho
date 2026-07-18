"""User-controlled export and avatar-history deletion."""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import zipfile

from tools import settings

EXPORT_ARCHIVE_TARGET_BYTES = 6 * 1024 * 1024
AVATAR_TRACKING_KEY = "avatar_history_tracking"


def _records(rows):
    return [dict(row) for row in rows]


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


async def collect_user_export(pool, user_id):
    """Collect exportable personal data without ever reading OAuth ciphertext."""
    preferences = await pool.fetchval(
        "SELECT settings FROM user_settings WHERE user_id = $1", user_id
    )
    if isinstance(preferences, str):
        preferences = json.loads(preferences)
    elif preferences is not None:
        preferences = dict(preferences)

    profile = await pool.fetchrow(
        "SELECT switch_fc, threeds_fc, battletag, riotid, steamid "
        "FROM profiles WHERE user_id = $1",
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

    data = {
        "export_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc),
        "user_id": user_id,
        "preferences": preferences or {},
        "profile": dict(profile) if profile else None,
        "anilist": {
            # Deliberately report linkage/expiry without selecting the encrypted
            # token. Neither ciphertext nor plaintext can enter the archive.
            "linked": token is not None,
            "token_expires": token["expires"] if token else None,
            "airing_notifications": dict(airing) if airing else None,
            "chapter_notifications": dict(chapters) if chapters else None,
        },
        "afk": dict(afk) if afk else None,
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
