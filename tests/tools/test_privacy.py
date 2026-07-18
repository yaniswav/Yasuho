import datetime
import io
import json
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
