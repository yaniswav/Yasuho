import datetime
import inspect
import io
import json
import os
import re
import zipfile

from tools import privacy


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DeleteConnection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return _Context(self)

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return {"deleted_count": 3, "deleted_bytes": 1234}

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "INSERT 0 1"


class _DeletePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Context(self.connection)


class _ExportPool:
    def __init__(self):
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return {"avatar_history_tracking": False}

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "anilist_tokens" in query:
            return {
                "expires": datetime.datetime(
                    2030, 1, 1, tzinfo=datetime.timezone.utc
                )
            }
        return None

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []


def _avatar(row_id, raw, *, kind="global", guild_id=None):
    return {
        "id": row_id,
        "guild_id": guild_id,
        "kind": kind,
        "ref": f"ref-{row_id}",
        "image_format": "webp",
        "changed_at": datetime.datetime(
            2030, 1, row_id, tzinfo=datetime.timezone.utc
        ),
        "avatar": raw,
    }


def test_export_archives_include_manifest_and_every_avatar():
    data = {
        "export_version": 1,
        "generated_at": datetime.datetime(
            2030, 1, 1, tzinfo=datetime.timezone.utc
        ),
        "user_id": 42,
    }
    avatars = [
        _avatar(1, b"first"),
        _avatar(2, b"second", kind="guild", guild_id=7),
    ]

    archives = privacy.build_export_archives(
        data, avatars, target_bytes=5
    )

    assert len(archives) == 2
    files = {}
    manifest = None
    for _name, buffer in archives:
        with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
            for filename in archive.namelist():
                files[filename] = archive.read(filename)
            if "data.json" in archive.namelist():
                manifest = json.loads(archive.read("data.json"))

    assert b"first" in files.values()
    assert b"second" in files.values()
    assert manifest["user_id"] == 42
    assert len(manifest["avatar_history"]) == 2
    assert all(item["sha256"] for item in manifest["avatar_history"])


async def test_collect_export_never_selects_oauth_token_material():
    pool = _ExportPool()

    data, avatars = await privacy.collect_user_export(pool, 42)

    assert data["anilist"]["linked"] is True
    assert avatars == []
    token_query = next(
        query for query in pool.queries if "anilist_tokens" in query
    )
    assert token_query.startswith("SELECT expires FROM anilist_tokens")
    assert "SELECT token" not in token_query
    assert any("moderator_id = $1" in query for query in pool.queries)
    assert any("event = 'reminder'" in query for query in pool.queries)
    assert any(
        "name, response, uses" in query
        for query in pool.queries
        if "custom_commands" in query
    )


async def test_avatar_delete_is_atomic_disables_tracking_and_invalidates_cache(
    monkeypatch,
):
    connection = _DeleteConnection()
    invalidated = []
    monkeypatch.setattr(
        privacy.settings, "invalidate_user", invalidated.append
    )

    result = await privacy.delete_user_avatar_history(
        _DeletePool(connection), 42
    )

    assert result == (3, 1234)
    assert invalidated == [42]
    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    assert "DELETE FROM avatar_history" in connection.calls[2][1]
    assert connection.calls[0][2] == (42,)
    assert connection.calls[1][2] == (
        42,
        privacy.AVATAR_TRACKING_KEY,
        False,
    )
    assert connection.calls[2][2] == (42,)


async def test_avatar_tracking_toggle_uses_consent_lock_and_invalidates_cache(
    monkeypatch,
):
    connection = _DeleteConnection()
    invalidated = []
    monkeypatch.setattr(
        privacy.settings, "invalidate_user", invalidated.append
    )

    await privacy.set_avatar_tracking(_DeletePool(connection), 42, False)

    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    assert connection.calls[1][2] == (
        42,
        privacy.AVATAR_TRACKING_KEY,
        False,
    )
    assert invalidated == [42]


async def test_avatar_store_rechecks_consent_under_same_transaction_lock():
    class _StoreConnection(_DeleteConnection):
        def __init__(self, enabled):
            super().__init__()
            self.enabled = enabled

        async def fetchval(self, query, *args):
            self.calls.append(("fetchval", query, args))
            if "pg_advisory_xact_lock" in query:
                return None
            return self.enabled

    disabled = _StoreConnection(False)
    stored = await privacy.store_avatar_if_tracking(
        _DeletePool(disabled),
        user_id=42,
        guild_id=None,
        kind="global",
        ref="avatar-ref",
        avatar=b"image",
        history_limit=30,
    )

    assert stored is False
    assert len(disabled.calls) == 2
    assert "pg_advisory_xact_lock" in disabled.calls[0][1]

    enabled = _StoreConnection(True)
    stored = await privacy.store_avatar_if_tracking(
        _DeletePool(enabled),
        user_id=42,
        guild_id=7,
        kind="guild",
        ref="avatar-ref",
        avatar=b"image",
        history_limit=30,
    )

    assert stored is True
    assert "INSERT INTO avatar_history" in enabled.calls[2][1]
    assert "DELETE FROM avatar_history" in enabled.calls[3][1]


# ---------------------------------------------------------------------------
# The social profile on the USER path: export and forget
# ---------------------------------------------------------------------------
#
# Profile data is keyed by user_id and carries no guild_id, so the guild purge
# (tools/retention.py, guarded by tests/tools/test_retention.py) never sees it.
# These are the guards for the path that DOES cover it.

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
)

# User-scoped tables (a user_id, no guild_id) deliberately absent from the
# export. Each entry is a claim about the code, so keep this list empty unless a
# table genuinely holds no personal data.
_EXPORT_EXEMPT_TABLES = set()


def _user_scoped_tables():
    """Tables in schema.sql that have a user_id and NO guild_id."""
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        ddl = re.sub(r"--[^\n]*", "", handle.read())
    tables = set()
    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\s*\)\s*;",
        ddl,
        re.S | re.I,
    ):
        body = match.group(2)
        has_user = re.search(r"^\s*user_id\b", body, re.M | re.I)
        has_guild = re.search(r"^\s*guild_id\b", body, re.M | re.I)
        if has_user and not has_guild:
            tables.add(match.group(1))
    return tables


def test_every_user_scoped_table_is_covered_by_the_export():
    """The user-side twin of the guild-purge structural guard.

    A new table keyed by user_id alone is invisible to the guild purge by
    construction, so the only thing that can surface it to its owner is
    ``collect_user_export``. Deriving the expectation from schema.sql means a
    future lot cannot add one and quietly forget /mydata.
    """
    tables = _user_scoped_tables()
    # Sanity-check the parser before trusting its verdict.
    assert {"user_profiles", "profile_visibility", "afk", "profiles"} <= tables

    source = inspect.getsource(privacy.collect_user_export)
    missing = sorted(
        table
        for table in tables - _EXPORT_EXEMPT_TABLES
        if not re.search(rf"FROM {table}\b", source)
    )
    assert missing == [], (
        "user-scoped table(s) never read by collect_user_export - their owner "
        "cannot export them: " + ", ".join(missing)
    )


def test_the_mydata_surface_can_erase_the_profile_it_exports():
    """A privacy surface that exports a profile must be able to delete it.

    ``delete_user_profile`` is the user-side twin of the guild purge; before
    this it was reachable only from `profile clear`, in another command tree and
    guild-only, while /mydata happily exported the same data.
    """
    from cogs.community import usersettings

    names = {command.name for command in usersettings.UserSettings.mydata.commands}
    assert {"export", "deleteavatars", "deleteprofile"} <= names

    # The group's own help text is the only discovery path for those verbs.
    help_text = inspect.getsource(usersettings.UserSettings.mydata.callback)
    for name in ("export", "deleteprofile", "deleteavatars"):
        assert f"mydata {name}" in help_text

    confirm = inspect.getsource(usersettings.ProfileDeletionView)
    assert "delete_user_profile" in confirm


class _ProfileExportPool(_ExportPool):
    def __init__(self):
        super().__init__()
        self.args = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        self.args.append(args)
        if "FROM user_profiles" in query:
            return {
                "bio": "hello",
                "pronouns": "she/her",
                "accent": 0x5865F2,
                # asyncpg hands JSONB back as text: the export must decode it,
                # not embed a quoted blob.
                "custom_fields": json.dumps([{"label": "Fav", "value": "P5"}]),
                "gaming_ids": json.dumps({"switch": "SW-1"}),
                "created_at": datetime.datetime(
                    2030, 1, 1, tzinfo=datetime.timezone.utc
                ),
                "updated_at": datetime.datetime(
                    2030, 1, 2, tzinfo=datetime.timezone.utc
                ),
            }
        if "FROM profiles" in query:
            return {
                "switch_fc": "SW-1",
                "threeds_fc": None,
                "battletag": None,
                "riotid": None,
                "steamid": None,
            }
        return await super().fetchrow(query, *args)

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "FROM profile_visibility" in query:
            return [{"field": "gaming_ids", "level": "server"}]
        return []


async def test_export_carries_the_profile_its_visibilities_and_the_legacy_row():
    data, _avatars = await privacy.collect_user_export(_ProfileExportPool(), 42)

    assert data["export_version"] == privacy.EXPORT_VERSION == 2
    assert data["profile"]["bio"] == "hello"
    assert data["profile"]["accent"] == 0x5865F2
    # Decoded, not a JSON string.
    assert data["profile"]["custom_fields"] == [{"label": "Fav", "value": "P5"}]
    assert data["profile"]["gaming_ids"] == {"switch": "SW-1"}
    # An absent row means private, so the export states the rows, not defaults.
    assert data["profile_visibility"] == [{"field": "gaming_ids", "level": "server"}]
    assert data["legacy_profile"]["switch_fc"] == "SW-1"


async def test_export_states_an_absent_profile_as_null():
    data, _avatars = await privacy.collect_user_export(_ExportPool(), 42)

    assert data["profile"] is None
    assert data["legacy_profile"] is None
    assert data["profile_visibility"] == []


async def test_the_export_reads_the_profile_of_that_user_only():
    pool = _ProfileExportPool()

    await privacy.collect_user_export(pool, 42)

    for table in ("user_profiles", "profile_visibility"):
        query = next(q for q in pool.queries if f"FROM {table}" in q)
        assert "WHERE user_id = $1" in query


async def test_forget_deletes_every_profile_table_in_one_transaction():
    connection = _DeleteConnection()

    counts = await privacy.delete_user_profile(_DeletePool(connection), 42)

    executed = [query for kind, query, _args in connection.calls if kind == "execute"]
    assert [table for table, _query in privacy.PROFILE_DELETE_QUERIES] == [
        "user_profiles",
        "profile_visibility",
        "profiles",
    ]
    assert len(executed) == 3
    assert all(query.startswith("DELETE FROM ") for query in executed)
    assert all(args == (42,) for _kind, _query, args in connection.calls)
    # _DeleteConnection reports "INSERT 0 1" for every statement.
    assert counts == {"user_profiles": 1, "profile_visibility": 1, "profiles": 1}


def test_the_forget_list_covers_exactly_the_profile_tables():
    """A new profile table must join the forget path, not just the export."""
    listed = {table for table, _query in privacy.PROFILE_DELETE_QUERIES}
    assert {"user_profiles", "profile_visibility"} <= listed
    assert listed <= _user_scoped_tables()


def test_forget_never_widens_beyond_the_owner():
    for _table, query in privacy.PROFILE_DELETE_QUERIES:
        assert query.count("$1") == 1
        assert query.endswith("WHERE user_id = $1")
