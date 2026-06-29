"""Per-user and per-guild settings, stored as small JSONB blobs.

Cached in-process so hot paths (e.g. leveling's on_message) don't hit the DB on
every event. For this single-process bot the cache is authoritative: writes go
through set_* which update both the DB and the cache.
"""

from __future__ import annotations

import json

# (table_name, id_value) -> settings dict
_cache: dict = {}

# Fixed table identifiers - never user input, so safe to interpolate into SQL.
_USER = ("user_settings", "user_id")
_GUILD = ("guild_settings", "guild_id")


async def _load(pool, spec, id_val):
    table, id_col = spec
    cache_key = (table, id_val)
    if cache_key not in _cache:
        raw = await pool.fetchval(
            f"SELECT settings FROM {table} WHERE {id_col} = $1", id_val
        )
        if raw is None:
            data = {}
        elif isinstance(raw, str):
            data = json.loads(raw)
        else:
            data = dict(raw)
        _cache[cache_key] = data
    return _cache[cache_key]


async def _save(pool, spec, id_val, data):
    table, id_col = spec
    _cache[(table, id_val)] = data
    await pool.execute(
        f"INSERT INTO {table} ({id_col}, settings) VALUES ($1, $2::jsonb) "
        f"ON CONFLICT ({id_col}) DO UPDATE SET settings = $2::jsonb",
        id_val,
        json.dumps(data),
    )


async def get_user(pool, user_id, key, default=None):
    return (await _load(pool, _USER, user_id)).get(key, default)


async def set_user(pool, user_id, key, value):
    data = dict(await _load(pool, _USER, user_id))
    data[key] = value
    await _save(pool, _USER, user_id, data)


async def get_guild(pool, guild_id, key, default=None):
    return (await _load(pool, _GUILD, guild_id)).get(key, default)


async def set_guild(pool, guild_id, key, value):
    data = dict(await _load(pool, _GUILD, guild_id))
    data[key] = value
    await _save(pool, _GUILD, guild_id, data)
