"""Per-user and per-guild settings, stored as small JSONB blobs.

Cached in-process so hot paths (e.g. leveling's on_message, i18n's locale
lookup) don't hit the DB on every event. For this single-process bot the cache is
authoritative: writes go through set_* which update both the DB and the cache.

The cache is SIZE-BOUNDED (see :class:`SettingsCache`): a plain dict grew one
entry per id ever seen and never shrank, and user ids are unbounded, so the map
could grow for the whole life of the process. Each scope now rides its own
least-recently-used cache (tools.lru_cache.BoundedLRU) with a cap; an evicted id
just re-reads from the DB on its next access, so bounding changes nothing a caller
can observe beyond the (rare) extra read after an eviction.
"""

from __future__ import annotations

import json

from tools.lru_cache import BoundedLRU

# Fixed table identifiers - never user input, so safe to interpolate into SQL.
_USER = ("user_settings", "user_id")
_GUILD = ("guild_settings", "guild_id")

# Cache ceilings. Guild blobs are naturally bounded by guild count, so the guild
# cap is only a safety ceiling comfortably above any plausible membership; user
# blobs are genuinely unbounded (any user who runs a command has their locale
# read), so the user cap is the firm bound that keeps the map from growing for
# the whole life of the process. Both are generous: exceeding a cap only trades a
# slot for a cheap DB re-read of the least-recently-used id, never a wrong value.
_USER_CACHE_CAP = 8192
_GUILD_CACHE_CAP = 4096


class SettingsCache:
    """Bounded cache for the settings blobs, split by scope with per-scope caps.

    Keyed by the same ``(table, id_val)`` tuples the module has always used, so it
    is a drop-in for the former plain dict (``key in cache``, ``cache[key]``,
    ``cache[key] = data``, ``cache.setdefault``, ``cache.clear``). Internally it
    routes each key to the user or guild :class:`BoundedLRU` by its table name, so
    a flood of user ids can never evict hot guild blobs and vice versa.
    """

    def __init__(self, *, user_cap=_USER_CACHE_CAP, guild_cap=_GUILD_CACHE_CAP):
        self._user = BoundedLRU(user_cap)
        self._guild = BoundedLRU(guild_cap)

    def _bucket(self, key):
        table = key[0]
        if table == _USER[0]:
            return self._user
        if table == _GUILD[0]:
            return self._guild
        raise KeyError(f"unknown settings scope: {table!r}")

    def __contains__(self, key):
        return key in self._bucket(key)

    def __getitem__(self, key):
        return self._bucket(key)[key]

    def __setitem__(self, key, value):
        self._bucket(key)[key] = value

    def setdefault(self, key, default):
        return self._bucket(key).setdefault(key, default)

    def clear(self):
        self._user.clear()
        self._guild.clear()

    def discard(self, key):
        self._bucket(key).discard(key)

    def __len__(self):
        return len(self._user) + len(self._guild)


# (table_name, id_value) -> settings dict, size-bounded per scope.
_cache = SettingsCache()


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
        # setdefault, not assignment: if another task populated (or wrote) this
        # entry while we awaited the fetch above, keep that newer value rather
        # than clobbering it with our now-stale DB read. This closes a cold-cache
        # race where two concurrent writes to the same id could lose one.
        _cache.setdefault(cache_key, data)
    return _cache[cache_key]


async def _save_key(pool, spec, id_val, key, value):
    """Persist a SINGLE preference key, patching the row in place.

    The write is a per-key ``jsonb_set`` (a single parameterized statement), not
    a whole-blob overwrite. This is what closes the lost-update against
    ``tools.privacy``: that module writes the avatar-tracking flag out-of-band
    under an advisory lock, and a stale whole-blob write here would silently
    revert it. A per-key patch only ever touches the one key we changed, so any
    sibling key another writer set survives untouched.

    The DB row is patched first; only then is the in-process cache reconciled.
    ``_load`` re-reads the authoritative row on a cold or invalidated cache (so a
    concurrent out-of-band write is picked up), and the final patch guarantees
    our own key is present regardless of what the reload observed.
    """
    table, id_col = spec
    await pool.execute(
        f"INSERT INTO {table} ({id_col}, settings) "
        f"VALUES ($1, jsonb_build_object($2::text, $3::jsonb)) "
        f"ON CONFLICT ({id_col}) DO UPDATE SET settings = "
        f"jsonb_set({table}.settings, ARRAY[$2::text], $3::jsonb, true)",
        id_val,
        key,
        json.dumps(value),
    )
    data = await _load(pool, spec, id_val)
    data[key] = value


async def get_user(pool, user_id, key, default=None):
    return (await _load(pool, _USER, user_id)).get(key, default)


async def set_user(pool, user_id, key, value):
    await _save_key(pool, _USER, user_id, key, value)


async def get_guild(pool, guild_id, key, default=None):
    return (await _load(pool, _GUILD, guild_id)).get(key, default)


async def set_guild(pool, guild_id, key, value):
    await _save_key(pool, _GUILD, guild_id, key, value)


def invalidate_user(user_id):
    """Drop one user's cached blob after an out-of-band transactional write."""
    _cache.discard((_USER[0], user_id))


def invalidate_guild(guild_id):
    """Drop one guild's cached blob after retention deletes its source row."""
    _cache.discard((_GUILD[0], guild_id))
