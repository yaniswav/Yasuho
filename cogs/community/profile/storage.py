"""Purpose: the only door to the profile tables - read a profile, write ONE
field, read or set ONE visibility, delete everything.

Every write touches a single COLUMN through statements that are individually
mono-statement (asyncpg refuses anything else for a parameterized query), so two
concurrent edits to different fields cannot clobber each other: there is no
read-modify-write anywhere in this module, not even for the JSONB ones (the
gamer-ID setter merges server-side with ``||``). The one write that needs two
statements - a gamer ID and the pre-migration column it supersedes - runs them
in a transaction so the legacy copy cannot survive the value it shadows.

Reads re-validate the JSONB columns through the registry and drop what no longer
passes, because the CHECK constraints only guard the outer shape and this cog is
not the only writer.

Validation happens BEFORE the statement, through :mod:`registry`, so a rejected
value never reaches Postgres and the caller gets a typed error instead of an
asyncpg constraint violation. Column identifiers come from the registry, never
from user input - the same fixed-whitelist posture the legacy profiles cog used.

``updated_at`` is maintained by every write. Deletion is delegated to
``tools.privacy`` so the user-forget path and this cog cannot drift apart: there
is one list of profile tables to delete, and it lives with the rest of the
privacy machinery.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import json

from . import registry, visibility
from tools import privacy

_PROFILE_COLUMNS = (
    "user_id, bio, pronouns, accent, custom_fields, gaming_ids, "
    "created_at, updated_at"
)

_JSON_DEFAULTS = {"custom_fields": [], "gaming_ids": {}}

# The legacy columns the gamer-ID keys were migrated FROM. Writing a key clears
# its pre-migration twin so the old table cannot keep serving a value the user
# has since changed or erased (it is still exported by tools/privacy.py until it
# is dropped). Fixed whitelist: these identifiers are never user input.
_LEGACY_GAMING_COLUMNS = {
    "switch": "switch_fc",
    "3ds": "threeds_fc",
    "battletag": "battletag",
    "riot": "riotid",
    "steam_id": "steamid",
}


def _decode_json(value, default):
    """asyncpg hands JSONB back as text (no codec is registered on the pool)."""
    if value is None:
        return default
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _sanitise_gaming_ids(value):
    """Keep the entries the registry still accepts; drop the rest."""
    if not isinstance(value, dict):
        return {}
    ids = {}
    for key, text in value.items():
        try:
            ids.update(registry.normalise("gaming_ids", {key: text}))
        except registry.ProfileFieldError:
            continue
    return ids


def _sanitise_custom_fields(value):
    """Same, entry by entry, so one bad pair does not lose the other four."""
    if not isinstance(value, (list, tuple)):
        return []
    pairs = []
    for entry in value:
        try:
            pairs.extend(registry.normalise("custom_fields", [entry]))
        except registry.ProfileFieldError:
            continue
        if len(pairs) >= registry.CUSTOM_FIELDS_MAX:
            break
    return pairs[: registry.CUSTOM_FIELDS_MAX]


# The JSONB columns are re-validated on READ, not only on write: the CHECK
# constraints in schema.sql only guard the outer shape (array / object / count),
# and a second writer (the dashboard) or a hand-edited row can hold an entry
# longer than the registry cap, an unknown gamer-ID key, or an outright wrong
# type. Degrading entry by entry keeps a hostile row from crashing or 400-ing
# `profile view` for its owner.
_SANITISERS = {
    "custom_fields": _sanitise_custom_fields,
    "gaming_ids": _sanitise_gaming_ids,
}


def _row_to_profile(row):
    profile = dict(row)
    for name, default in _JSON_DEFAULTS.items():
        decoded = _decode_json(profile.get(name), default)
        profile[name] = _SANITISERS[name](decoded)
    return profile


async def get_profile(pool, user_id):
    """Return the user's profile as a plain dict, or None when they have none."""
    row = await pool.fetchrow(
        f"SELECT {_PROFILE_COLUMNS} FROM user_profiles WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return None
    return _row_to_profile(row)


async def get_visibility(pool, user_id):
    """Return ``{field: level}`` for the rows that exist (absent = private)."""
    rows = await pool.fetch(
        "SELECT field, level FROM profile_visibility WHERE user_id = $1",
        user_id,
    )
    return {row["field"]: row["level"] for row in rows}


def _is_cleared(field, stored):
    """True when the normalised value is this field's "unset" form.

    Text and colour clear to None; the JSONB sections clear to [] / {} (never
    NULL - schema.sql forbids it). Accent 0 is BLACK, a real value, which is why
    this tests ``is None`` and not falsiness.
    """
    if stored is None:
        return True
    return field.json_column and not stored


async def set_field(pool, user_id, name, value):
    """Validate and store ONE field; returns the stored (normalised) value.

    Raises :class:`registry.UnknownField`, :class:`registry.FieldNotStored` or
    :class:`registry.InvalidValue` before touching the database.

    Clearing is an UPDATE on purpose, the same discipline as
    :func:`set_gaming_id`: erasing a field must not conjure an all-NULL profile
    row for a user who has none. A phantom row is not free - /mydata would hand
    back a "profile" object made of nulls to someone who never wrote one, and
    the dashboard would count them as having a profile.
    """
    field = registry.stored_field(name)
    stored = field.normalise(value)
    column = field.column
    placeholder = "$2::jsonb" if field.json_column else "$2"
    param = json.dumps(stored) if field.json_column else stored
    # `column` comes from the registry whitelist and is NEVER user input, so the
    # identifier interpolation is safe; the value stays a bound parameter.
    if _is_cleared(field, stored):
        await pool.execute(
            f"UPDATE user_profiles SET {column} = {placeholder}, "
            f"updated_at = now() WHERE user_id = $1",
            user_id,
            param,
        )
        return stored
    await pool.execute(
        f"INSERT INTO user_profiles (user_id, {column}) "
        f"VALUES ($1, {placeholder}) "
        f"ON CONFLICT (user_id) DO UPDATE SET {column} = EXCLUDED.{column}, "
        f"updated_at = now()",
        user_id,
        param,
    )
    return stored


async def set_gaming_id(pool, user_id, key, value):
    """Set or clear ONE gamer ID inside the ``gaming_ids`` mapping.

    Merged server-side, so setting a Switch code never overwrites a Riot ID that
    another session wrote a millisecond earlier. Clearing is an UPDATE on
    purpose: erasing a key must not conjure a profile row for a user who has none.

    The pre-migration column for that key is NULLed in the same transaction, so
    changing or clearing an ID also removes the copy the legacy `profiles` table
    still holds - otherwise /mydata would keep handing back a value the user
    believes they replaced.
    """
    field = registry.stored_field("gaming_ids")
    if key not in registry.GAMING_ID_KEYS:
        raise registry.InvalidValue(field.name, "unknown_key")
    text = field.normalise({key: value}).get(key)
    legacy = _LEGACY_GAMING_COLUMNS[key]
    async with pool.acquire() as connection:
        async with connection.transaction():
            if text is None:
                await connection.execute(
                    "UPDATE user_profiles SET gaming_ids = gaming_ids - $2::text, "
                    "updated_at = now() WHERE user_id = $1",
                    user_id,
                    key,
                )
            else:
                await connection.execute(
                    "INSERT INTO user_profiles (user_id, gaming_ids) "
                    "VALUES ($1, jsonb_build_object($2::text, $3::text)) "
                    "ON CONFLICT (user_id) DO UPDATE SET gaming_ids = "
                    "user_profiles.gaming_ids || "
                    "jsonb_build_object($2::text, $3::text), "
                    "updated_at = now()",
                    user_id,
                    key,
                    text,
                )
            # Identifier from the fixed whitelist above, never user input.
            await connection.execute(
                f"UPDATE profiles SET {legacy} = NULL "
                f"WHERE user_id = $1 AND {legacy} IS NOT NULL",
                user_id,
            )
    return text


async def set_visibility(pool, user_id, name, level):
    """Publish ONE field at ``level``; 'private' DELETES the row.

    The default is never materialised, so an absent row and an explicit private
    choice are indistinguishable - which is exactly the fail-closed property
    :mod:`visibility` relies on. Connector sections (P3/P4) are addressable here
    even though nothing stores their data yet.
    """
    field = registry.get(name)
    level = visibility.normalise_level(level)
    if level == visibility.PRIVATE:
        await pool.execute(
            "DELETE FROM profile_visibility WHERE user_id = $1 AND field = $2",
            user_id,
            field.name,
        )
        return level
    await pool.execute(
        "INSERT INTO profile_visibility (user_id, field, level) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id, field) DO UPDATE SET level = EXCLUDED.level",
        user_id,
        field.name,
        level,
    )
    return level


async def delete_profile(pool, user_id):
    """Erase the whole profile (fields, visibilities and the legacy row)."""
    return await privacy.delete_user_profile(pool, user_id)
