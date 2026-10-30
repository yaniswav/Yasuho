"""Remembered temporary-room settings, one row per (server, member, hub).

A member who changes their room (name, user limit, lock, hide) through the room
panel gets those choices re-applied the next time they create a room from the SAME
hub (a Squad hub's limit never leaks into a Duo hub). Only the changed field is
written. The opt-out is a row in ``room_prefs_off`` (no settings, only the on/off choice):
while it exists every save is a no-op. Forgetting settings and erasing the profile
leave that row alone, so neither silently turns saving back on.

Per-user data: exported by /mydata, erased by deleteprofile and by the server purge
(see tools/privacy.py and tools/retention.py). Bitrate is not remembered: the room
panel has no bitrate control.

Typography rule: ASCII '-' and '...' only.
"""

NAME_LIMIT = 100
MAX_USER_LIMIT = 99
OPT_OUT_HUB = 0  # hub_channel_id of the opt-out marker row
_CASTS = {"name": "text", "user_limit": "smallint", "locked": "boolean", "hidden": "boolean"}
# Whitelist: the SET clause below is built from these names only.
FIELDS = ("name", "user_limit", "locked", "hidden")


def clean(field, value):
    """Normalise one value for storage; raises ValueError for an unusable one."""

    if field == "name":
        value = str(value).strip()[:NAME_LIMIT]
        if not value:
            raise ValueError("empty name")
        return value
    if field == "user_limit":
        return max(0, min(MAX_USER_LIMIT, int(value)))
    if field in ("locked", "hidden"):
        return bool(value)
    raise ValueError(field)


async def load(pool, guild_id, user_id, hub_channel_id):
    """The member's saved settings for one hub as a dict, or None."""

    row = await pool.fetchrow(
        "SELECT name, user_limit, locked, hidden FROM room_prefs "
        "WHERE guild_id = $1 AND user_id = $2 AND hub_channel_id = $3",
        guild_id,
        user_id,
        hub_channel_id,
    )
    return None if row is None else {field: row[field] for field in FIELDS}


async def load_all(pool, guild_id, user_id):
    """Every saved row of the member in this server."""

    return await pool.fetch(
        "SELECT hub_channel_id, name, user_limit, locked, hidden FROM room_prefs "
        "WHERE guild_id = $1 AND user_id = $2 ORDER BY hub_channel_id",
        guild_id,
        user_id,
    )


async def save(pool, guild_id, user_id, hub_channel_id, **fields):
    """Upsert only the given fields; a no-op for a member who opted out."""

    columns = [(f, clean(f, v)) for f, v in fields.items() if f in FIELDS]
    if not columns:
        return
    names = ", ".join(f for f, _v in columns)
    marks = ", ".join("$%d::%s" % (i + 4, _CASTS[f]) for i, (f, _v) in enumerate(columns))
    sets = ", ".join("%s = EXCLUDED.%s" % (f, f) for f, _v in columns)
    await pool.execute(
        "INSERT INTO room_prefs (guild_id, user_id, hub_channel_id, %s) "
        "SELECT $1, $2, $3, %s WHERE NOT EXISTS ("
        "SELECT 1 FROM room_prefs_off WHERE guild_id = $1 AND user_id = $2) "
        "ON CONFLICT (guild_id, user_id, hub_channel_id) DO UPDATE SET %s, updated_at = now()"
        % (names, marks, sets),
        guild_id,
        user_id,
        hub_channel_id,
        *[v for _f, v in columns],
    )


async def is_opted_out(pool, guild_id, user_id):
    return bool(
        await pool.fetchval(
            "SELECT 1 FROM room_prefs_off WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
    )


async def forget(pool, guild_id, user_id):
    """Delete the member's saved settings in this server; the opt-out stays."""

    await pool.execute(
        "DELETE FROM room_prefs WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
    )


async def set_remember(pool, guild_id, user_id, enabled):
    """On: drop the opt-out only (saved settings are kept). Off: wipe and opt out."""

    if enabled:
        await pool.execute(
            "DELETE FROM room_prefs_off WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
        return
    await pool.execute(
        "WITH wiped AS (DELETE FROM room_prefs WHERE guild_id = $1 AND user_id = $2) "
        "INSERT INTO room_prefs_off (guild_id, user_id) VALUES ($1, $2) "
        "ON CONFLICT (guild_id, user_id) DO NOTHING",
        guild_id,
        user_id,
    )


def plan_room(prefs, rendered_name, hub_user_limit, hub_private):
    """Merge saved settings over the hub defaults: ``(name, limit, locked, hidden)``.

    A private hub always locks (the admin's policy beats a saved unlock). A saved
    user limit never exceeds the hub's own limit when the hub has one (a saved 0,
    "unlimited", becomes the hub's limit).
    """

    prefs = prefs or {}
    name = prefs.get("name") or rendered_name
    limit = prefs["user_limit"] if prefs.get("user_limit") is not None else hub_user_limit
    if hub_user_limit > 0 and (limit == 0 or limit > hub_user_limit):
        limit = hub_user_limit
    locked = bool(hub_private or prefs.get("locked"))
    hidden = bool(prefs.get("hidden"))
    return name[:NAME_LIMIT], limit, locked, hidden
