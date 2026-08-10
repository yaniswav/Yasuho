import datetime
import inspect
import io
import json
import os
import re
import zipfile

from tools import privacy, retention

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def _repo_python_files():
    """Every shipped .py file (the bot's own code, not the test suite)."""
    for root, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = [
            name
            for name in dirs
            if name not in {".git", ".venv", "tests", "__pycache__", "locales"}
        ]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


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

# User-scoped tables (see _user_scoped_tables) deliberately absent from the
# export. Each entry is a claim about the code, so keep this list empty unless a
# table genuinely holds no personal data.
_EXPORT_EXEMPT_TABLES = set()


# Column names that identify a PERSON, not a thing. The guard used to key on the
# literal ``user_id`` alone, and that is precisely how ``blbot`` escaped it for
# the whole life of the table: it calls its column ``member_id``, so a table
# keyed by one human being with no guild anywhere read as "not user-scoped" and
# nothing ever asked whether its owner could see it. A naming convention is not
# a privacy boundary, so the guard now asks what the column MEANS.
_ACTOR_COLUMNS = (
    "user_id",
    "member_id",
    "opener_id",
    "creator_id",
    "created_by",
    "moderator_id",
    "closed_by",
    "claimed_by",
    "requested_by",
    "owner_id",
    "author_id",
)
# ... and the same lesson on the other side of the test. ``mutedmembers`` spells
# its guild column ``mguild_id``, so a scope test keyed on the literal
# ``guild_id`` would call that table user-scoped and demand it be exported -
# a false positive, because those rows die with their guild like every other
# guild record (retention.GUILD_DELETE_QUERIES deletes it WHERE mguild_id).
_GUILD_COLUMNS = ("guild_id", "mguild_id")


def _user_scoped_tables():
    """Tables in schema.sql holding rows NO guild purge can ever reach.

    That is: a BIGINT column that names a PERSON (:data:`_ACTOR_COLUMNS`,
    whatever it is spelled), and either no guild column at all or a NULLABLE
    one. The nullable case is not a technicality - it is how
    ``dashboard_actions`` gained a user scope: rows of the very same table are
    guild-scoped (purged with the guild) or user-scoped (``guild_id NULL``, which
    ``WHERE guild_id = $1`` can never match). A guard keyed on "has no guild_id
    column" stopped applying to that table the moment it grew a second scope,
    which is exactly when its user rows appeared.

    A NOT NULL guild column is the other side of the tiling: those rows die with
    their guild, and ``tests/tools/test_retention.py`` is the structural guard
    that makes sure they do.

    BIGINT is part of the test and not decoration: an actor id is a Discord
    snowflake, so a TEXT or BOOLEAN column that happens to share one of these
    names is a different thing wearing the name and must not drag its table in.
    """
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        ddl = re.sub(r"--[^\n]*", "", handle.read())
    actor = r"(?:{0})".format("|".join(_ACTOR_COLUMNS))
    guild_column = r"(?:{0})".format("|".join(_GUILD_COLUMNS))
    tables = set()
    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\s*\)\s*;",
        ddl,
        re.S | re.I,
    ):
        body = match.group(2)
        has_actor = re.search(
            rf"^\s*{actor}\s+BIGINT\b", body, re.M | re.I
        )
        guild = re.search(
            rf"^\s*{guild_column}\b([^\n,]*)", body, re.M | re.I
        )
        if not has_actor:
            continue
        if guild is None or "NOT NULL" not in guild.group(1).upper():
            tables.add(match.group(1))
    return tables


def test_every_user_scoped_table_is_covered_by_the_export():
    """The user-side twin of the guild-purge structural guard.

    A row no guild purge can reach is invisible to the guild-side guard by
    construction, so the only thing that can surface it to its owner is
    ``collect_user_export``. Deriving the expectation from schema.sql means a
    future lot cannot add one and quietly forget /mydata.
    """
    tables = _user_scoped_tables()
    # Sanity-check the parser before trusting its verdict: three tables keyed by
    # user alone, the two-scope one whose user rows have guild_id NULL, and a
    # guild-keyed table that must NOT be dragged in by the nullable rule.
    assert {"user_profiles", "profile_visibility", "afk", "profiles"} <= tables
    assert "dashboard_actions" in tables
    assert "season_podiums" not in tables
    # THE table the old ``user_id``-only guard could not see: one BIGINT naming
    # one person, spelled ``member_id``, with no guild column anywhere.
    assert "blbot" in tables
    # ... and the false positive the widening has to avoid: same spelling on the
    # actor side, but the row is guild-scoped through ``mguild_id`` and dies
    # with its guild.
    assert "mutedmembers" not in tables

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
    assert "delete_user_data" in confirm


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

    assert data["export_version"] == privacy.EXPORT_VERSION == 10
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


async def test_the_favourites_export_states_the_blob_without_carrying_it():
    """Same doctrine as the rank-card background: describe it, do not ship it."""
    pool = _ProfileExportPool()

    await privacy.collect_user_export(pool, 42)

    query = next(q for q in pool.queries if "FROM music_favorites" in q)
    assert "encoded IS NOT NULL AS has_encoded" in query
    # The blob itself is never selected - only the fact that it is there.
    assert "SELECT identifier" in query and ", encoded," not in query


async def test_forget_deletes_every_profile_table_in_one_transaction():
    connection = _DeleteConnection()

    counts = await privacy.delete_user_profile(_DeletePool(connection), 42)

    executed = [query for kind, query, _args in connection.calls if kind == "execute"]
    assert [table for table, _query in privacy.PROFILE_DELETE_QUERIES] == [
        "user_profiles",
        "profile_visibility",
        "profile_connections",
        "profiles",
    ]
    assert len(executed) == 4
    assert all(query.startswith("DELETE FROM ") for query in executed)
    assert all(args == (42,) for _kind, _query, args in connection.calls)
    # _DeleteConnection reports "INSERT 0 1" for every statement.
    assert counts == {
        "user_profiles": 1,
        "profile_visibility": 1,
        "profile_connections": 1,
        "profiles": 1,
    }


async def test_the_wide_erasure_adds_the_vote_ledger_to_the_same_transaction():
    """`?mydata deleteprofile` erases the profile AND the records that are not
    profile data - the top.gg vote ledger, the personal rank card, the AniList
    OAuth token and the two alert opt-ins - in the SAME transaction, so a
    confirmed forget can never half-happen."""
    connection = _DeleteConnection()

    counts = await privacy.delete_user_data(_DeletePool(connection), 42)

    executed = [query for kind, query, _args in connection.calls if kind == "execute"]
    assert [table for table, _query in privacy.USER_DELETE_QUERIES] == [
        "user_profiles",
        "profile_visibility",
        "profile_connections",
        "profiles",
        "topgg_votes",
        "user_rank_cards",
        "anilist_tokens",
        "anilist_airing_optins",
        "anilist_chapter_optins",
    ]
    assert len(executed) == 9
    assert counts["topgg_votes"] == 1
    assert counts["user_rank_cards"] == 1
    assert counts["anilist_tokens"] == 1
    assert counts["anilist_airing_optins"] == 1
    assert counts["anilist_chapter_optins"] == 1


def test_the_narrow_erasure_never_reaches_what_the_wide_one_warns_about():
    """THE reason the two lists exist.

    `profile clear` erases everything in PROFILE_DELETE_QUERIES and nothing
    else. An earned vote streak and a lifetime count are not on it, and neither
    is the personal rank card whose background is an image file its owner
    uploaded and may no longer hold: nothing can give either back, and
    `profile clear` neither warns about them nor mentions them, so they live on
    the WIDE list only, behind the button that names them. `/rankcard clear` is
    the explicit verb for the card; `?mydata deleteprofile` is the erasure one.
    """
    narrow = {table for table, _query in privacy.PROFILE_DELETE_QUERIES}
    wide = {table for table, _query in privacy.USER_DELETE_QUERIES}

    assert "topgg_votes" not in narrow
    assert "topgg_votes" in wide
    assert "user_rank_cards" not in narrow
    assert "user_rank_cards" in wide
    assert narrow < wide  # the wide list is a strict superset, never a fork

    from cogs.community.profile import cog as profile_cog

    confirm = inspect.getsource(profile_cog.ProfileClearView.confirm)
    assert "delete_profile" in confirm  # the narrow verb...
    assert "delete_user_data" not in confirm  # ...never the wide one
    # And nothing to un-arm: the boost outlives a profile reset with its row.
    assert "forget_vote_boost" not in confirm


def test_the_narrow_erasure_asks_before_destroying_the_unrecreatable():
    """The doctrine, applied to the one thing on the NARROW list that its owner
    cannot type back.

    ``profile_connections`` is on the narrow list on purpose - a clear that left
    a linked Steam handle behind is not a clear - but that same table holds the
    presence aggregates, which are weeks of minutes the bot collected on its own
    and nobody can recreate. `profile clear` used to delete them on sight, with
    no confirmation and no mention. It is a prompt-first command now, and the
    prompt has to SAY what goes: the doctrine is "nothing unrecreatable dies
    unannounced", not "the wide list is the only list that asks".
    """
    from cogs.community.profile import cog as profile_cog

    assert "profile_connections" in dict(privacy.PROFILE_DELETE_QUERIES)

    clear = inspect.getsource(profile_cog.Profiles.profile_clear.callback)
    # The command itself must not delete anything any more: it puts a view in
    # front of the user and stops there.
    assert "delete_profile" not in clear
    assert "ProfileClearView" in clear
    # ... and the prompt names each kind of thing the click destroys.
    for phrase in ("visibility choice", "linked", "presence was on"):
        assert phrase in clear, phrase
    assert "cannot be recovered" in clear

    # The result message has to agree with the prompt, the same rule the wide
    # path's own screens are held to below.
    confirm = inspect.getsource(profile_cog.ProfileClearView.confirm)
    for phrase in ("visibility choice", "linked accounts", "presence had collected"):
        assert phrase in confirm, phrase


def test_the_forget_list_covers_exactly_the_profile_tables():
    """A new profile table must join the forget path, not just the export."""
    listed = {table for table, _query in privacy.PROFILE_DELETE_QUERIES}
    assert {"user_profiles", "profile_visibility", "profile_connections"} <= listed
    assert listed <= _user_scoped_tables()


def test_forget_never_widens_beyond_the_owner():
    # The WIDE list, so the narrow one is covered by inclusion: no erasure query
    # anywhere may reach a row that is not this user's.
    for _table, query in privacy.USER_DELETE_QUERIES:
        assert query.count("$1") == 1
        assert query.endswith("WHERE user_id = $1")


# ---------------------------------------------------------------------------
# The permanent leveling record (season_podiums) and the bot-wide blacklist
# ---------------------------------------------------------------------------
#
# Two rows the archive could not show before v10, and they fail the guards above
# for opposite reasons: a podium row is GUILD-scoped (so the user-side
# structural guard cannot see it, by construction) and `blbot` spells its actor
# column ``member_id`` (so the guard could not see it either, until the guard
# stopped keying on a name).


async def test_the_export_carries_the_monthly_season_podiums():
    """THE permanent leveling record, and the one PRIVACY.md names.

    Every other XP row is a running total that gets overwritten or pruned; a
    podium row is written once when a month closes and then never touched. It
    dies with its guild like the rest of that server's records, so the export is
    the only thing that can show it to the person standing on it.
    """

    class _PodiumPool(_ExportPool):
        async def fetch(self, query, *args):
            self.queries.append(query)
            if "FROM season_podiums" in query:
                return [
                    {
                        "guild_id": 7,
                        "period_key": "M2026-07",
                        "rank": 1,
                        "xp": 4242,
                        "snapshot_at": datetime.datetime(
                            2026, 8, 1, tzinfo=datetime.timezone.utc
                        ),
                    }
                ]
            return []

    pool = _PodiumPool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["season_podiums"] == [
        {
            "guild_id": 7,
            "period_key": "M2026-07",
            "rank": 1,
            "xp": 4242,
            "snapshot_at": datetime.datetime(
                2026, 8, 1, tzinfo=datetime.timezone.utc
            ),
        }
    ]
    query = next(q for q in pool.queries if "FROM season_podiums" in q)
    # This user's placements, never the rest of the podium: the other two names
    # on that month belong to them, not to the requester.
    assert "WHERE user_id = $1" in query


async def test_the_export_states_the_bot_wide_blacklist_as_a_fact():
    """One boolean, and both values are stated.

    ``blbot`` holds a single column, so "there is a row" IS the entire record.
    Keyed by one person with no guild anywhere, which is why no purge reaches it
    and why the export is the only surface that can ever state it.
    """

    class _BlockedPool(_ExportPool):
        async def fetchrow(self, query, *args):
            self.queries.append(query)
            if "FROM blbot" in query:
                return {"member_id": 42}
            return None

    data, _avatars = await privacy.collect_user_export(_BlockedPool(), 42)
    assert data["bot_access"] == {"blacklisted": True}

    # ... and a user who is not on it is told so, rather than left to read an
    # absent key as an answer.
    clean, _avatars = await privacy.collect_user_export(_ExportPool(), 42)
    assert clean["bot_access"] == {"blacklisted": False}


def test_the_blacklist_is_exported_and_reachable_by_no_erasure_path():
    """The mydata_export_cooldown call, made a second time and for the same
    reason: erasing this row would HAND its subject something.

    A blacklist entry is the bot owner's only anti-abuse decision, it is
    guild-independent (no guild purge can reach it), and listing it on
    `?mydata deleteprofile` would turn "delete my profile" into a self-service
    unban. So it is exported - the person is already told directly when it
    happens - and deleted by nothing here.
    """
    narrow = {table for table, _query in privacy.PROFILE_DELETE_QUERIES}
    wide = {table for table, _query in privacy.USER_DELETE_QUERIES}
    assert "blbot" not in narrow
    assert "blbot" not in wide

    source = inspect.getsource(privacy.collect_user_export)
    assert "FROM blbot WHERE member_id = $1" in source

    # REPO-WIDE, the same shape as the export-limiter guard below: exactly one
    # file may delete from this table, and it is the owner-only command, never a
    # privacy path.
    deleters = set()
    for path in _repo_python_files():
        source = open(path, encoding="utf-8").read()
        if re.search(r"DELETE FROM\s+blbot", source):
            deleters.add(os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/"))
    assert deleters == {"cogs/moderation/blacklist.py"}


# ---------------------------------------------------------------------------
# The shared export limiter (claim_export_slot)
# ---------------------------------------------------------------------------
#
# One export per user per hour, enforced by a DB clock instead of a per-process
# bucket, because two callers now ask for the same archive: `?mydata export` in
# Discord and the dashboard's `mydata_export` queue action. These guard the
# claim's semantics AND the properties that make it tamper-proof.


class _SlotPool:
    """Records the claim and replays a canned row."""

    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


async def test_claim_export_slot_grants_when_the_window_has_elapsed():
    pool = _SlotPool({"granted": True, "retry_after": 0})

    granted, retry_after = await privacy.claim_export_slot(pool, 42)

    assert (granted, retry_after) == (True, 0)
    _query, args = pool.calls[0]
    assert args == (42, privacy.EXPORT_COOLDOWN_SECONDS)


async def test_claim_export_slot_refuses_with_the_exact_remaining_seconds():
    pool = _SlotPool({"granted": False, "retry_after": 2599})

    granted, retry_after = await privacy.claim_export_slot(pool, 42)

    assert granted is False
    assert retry_after == 2599


async def test_a_refusal_never_tells_the_caller_to_retry_immediately():
    """The sub-select reads the row from the statement's own snapshot, so the
    loser of a race on the last tick of the window can compute 0 from a row the
    winner has already moved. "Retry in 0s" would walk straight back into a
    refusal, so a refusal always reports at least one second."""
    pool = _SlotPool({"granted": False, "retry_after": 0})

    granted, retry_after = await privacy.claim_export_slot(pool, 42)

    assert (granted, retry_after) == (False, 1)


def test_the_claim_clamps_the_wait_to_the_window_itself():
    """A row stamped in the FUTURE (backwards clock adjustment on the DB host, a
    restored dump) must not produce "come back in a day" for an hourly limit.
    The clamp lives in SQL because that is where the arithmetic is."""
    sql = privacy._CLAIM_EXPORT_SLOT
    assert "LEAST(GREATEST(0, CEIL(" in sql
    # ... clamped against the window PARAMETER, not a literal copy of it.
    assert "))), $2)" in sql


async def test_claim_export_slot_takes_the_window_from_the_caller():
    pool = _SlotPool({"granted": True, "retry_after": 0})

    await privacy.claim_export_slot(pool, 42, cooldown=60)

    assert pool.calls[0][1] == (42, 60)


async def test_claim_export_slot_fails_closed_when_the_row_is_missing():
    """A claim that somehow returns nothing must refuse, not hand out a free
    export: the limiter guards the most expensive job the bot has."""
    granted, retry_after = await privacy.claim_export_slot(_SlotPool(None), 42)

    assert granted is False
    assert retry_after == privacy.EXPORT_COOLDOWN_SECONDS


def test_the_claim_is_one_atomic_statement_on_its_own_table():
    """The properties the limiter's integrity rests on, pinned:

    * ONE statement - asyncpg refuses multiple, and a read-then-write pair would
      let two concurrent claims both pass;
    * a test-and-set (INSERT ... ON CONFLICT DO UPDATE ... WHERE), so the loser
      of a race updates nothing;
    * the DEDICATED table, never the user_settings blob the /preferences panel
      and the dashboard both write - anything stored there is rewritable by the
      very party being rate-limited;
    * the user id is a bound parameter, never interpolated.
    """
    sql = privacy._CLAIM_EXPORT_SLOT
    assert sql.strip().count(";") == 0
    assert "INSERT INTO mydata_export_cooldown" in sql
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    assert "user_settings" not in sql
    assert "$1" in sql and "$2" in sql
    assert "%" not in sql and ".format(" not in sql


def test_the_limiter_row_is_out_of_reach_of_every_user_write_path():
    """The clock must not be resettable by its own subject.

    ``?mydata deleteprofile`` erases every table in USER_DELETE_QUERIES, so
    listing the limiter there would turn "delete my profile" into "give me
    another export now" - a one-click bypass. It is deliberately absent from
    BOTH erasure lists, and the export below is what keeps the user's right to
    SEE it intact.
    """
    listed = {table for table, _query in privacy.USER_DELETE_QUERIES}
    assert "mydata_export_cooldown" not in listed

    # REPO-WIDE, not just this module: a writer added anywhere else would defeat
    # the property just as thoroughly, and grepping only privacy.py would call
    # that safe. Exactly two files may touch the table, and only in these ways.
    writers = {}
    for path in _repo_python_files():
        source = open(path, encoding="utf-8").read()
        verbs = re.findall(
            r"(INSERT INTO|UPDATE|DELETE FROM)\s+mydata_export_cooldown", source
        )
        if verbs:
            writers[os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")] = verbs

    assert writers == {
        # The claim: the ONLY thing that ever stamps the clock.
        "tools/privacy.py": ["INSERT INTO"],
        # Retention: deletes rows whose window has already elapsed. Those grant
        # on sight, so this cannot hand anybody an export they were owed a wait
        # for - and the age predicate is what makes that true.
        "tools/retention.py": ["DELETE FROM"],
    }
    prune = inspect.getsource(retention.prune_expired_export_slots)
    assert "last_export_at < now() - $1 * INTERVAL '1 second'" in prune


async def test_the_export_carries_the_limiter_row():
    """It is one timestamp, but it is stored under the user's id, so it ships -
    and the user-scoped structural guard above holds every such table to it."""

    class _SlotExportPool(_ExportPool):
        async def fetchrow(self, query, *args):
            self.queries.append(query)
            if "mydata_export_cooldown" in query:
                return {
                    "last_export_at": datetime.datetime(
                        2030, 5, 4, tzinfo=datetime.timezone.utc
                    )
                }
            return None

    pool = _SlotExportPool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["data_export_requests"] == {
        "last_export_at": datetime.datetime(
            2030, 5, 4, tzinfo=datetime.timezone.utc
        )
    }
    query = next(q for q in pool.queries if "mydata_export_cooldown" in q)
    assert "WHERE user_id = $1" in query


async def test_the_export_carries_ticket_metadata_and_no_ticket_content():
    """A support ticket's CONVERSATION is a Discord thread and is never stored,
    so the only thing there is to export is the bookkeeping - and the export must
    both carry it and be incapable of carrying anything else."""

    class _TicketPool(_ExportPool):
        async def fetch(self, query, *args):
            self.queries.append(query)
            if "FROM tickets" in query:
                return [
                    {
                        "guild_id": 7,
                        "ticket_number": 3,
                        "thread_id": 999,
                        "status": "closed",
                        "opened_at": datetime.datetime(
                            2030, 5, 4, tzinfo=datetime.timezone.utc
                        ),
                        "closed_at": datetime.datetime(
                            2030, 5, 5, tzinfo=datetime.timezone.utc
                        ),
                    }
                ]
            return []

    pool = _TicketPool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["tickets_opened"] == [
        {
            "guild_id": 7,
            "ticket_number": 3,
            "thread_id": 999,
            "status": "closed",
            "opened_at": datetime.datetime(2030, 5, 4, tzinfo=datetime.timezone.utc),
            "closed_at": datetime.datetime(2030, 5, 5, tzinfo=datetime.timezone.utc),
        }
    ]
    query = next(q for q in pool.queries if "FROM tickets" in q)
    # Scoped to the tickets this user OPENED...
    assert "WHERE opener_id = $1" in query
    # ...and `closed_by` stays out: who acted on the ticket is the server's
    # record, exactly like the moderator id on `cases`.
    assert "closed_by" not in query


async def test_the_export_states_a_never_exported_user_as_null():
    data, _avatars = await privacy.collect_user_export(_ExportPool(), 42)

    assert data["data_export_requests"] is None
    assert data["dashboard_requests"] == []


async def test_the_export_carries_the_users_own_queue_rows():
    """A ``dashboard_actions`` user row says "you asked for an export at T, and
    it ended like this". That is this user's data, it is unreachable by the guild
    purge (guild_id NULL), so the export is what makes it visible to them."""

    class _QueuePool(_ExportPool):
        def __init__(self):
            super().__init__()
            self.args = []

        async def fetch(self, query, *args):
            self.queries.append(query)
            self.args.append(args)
            if "FROM dashboard_actions" in query:
                return [
                    {
                        "id": 7,
                        "kind": "mydata_export",
                        "status": "done",
                        # asyncpg hands JSONB back as TEXT: a quoted blob in the
                        # archive would be unreadable to the person opening it.
                        "result": json.dumps({"ok": True, "delivered": "dm"}),
                        "requested_by": 42,
                        "created_at": datetime.datetime(
                            2030, 5, 4, tzinfo=datetime.timezone.utc
                        ),
                        "updated_at": datetime.datetime(
                            2030, 5, 4, tzinfo=datetime.timezone.utc
                        ),
                    }
                ]
            return []

    pool = _QueuePool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["dashboard_requests"][0]["kind"] == "mydata_export"
    assert data["dashboard_requests"][0]["result"] == {
        "ok": True,
        "delivered": "dm",
    }

    query = next(q for q in pool.queries if "FROM dashboard_actions" in q)
    # Scoped by USER: never by id (that would be somebody else's row), and never
    # the guild rows of the same table (they belong to the guild, are gated by
    # manage-guild, and die with it).
    assert "WHERE user_id = $1" in query
    assert "guild_id" not in query
    # The dashboard-written payload is deliberately not exported.
    assert "payload" not in query


# ---------------------------------------------------------------------------
# The top.gg vote ledger (V1)
# ---------------------------------------------------------------------------
#
# A per-user record of a behaviour, keyed by user alone, so the guild purge can
# never reach it: /mydata is the whole of its lifecycle. It is exported AND
# erased - the opposite call from mydata_export_cooldown above, and the two
# tests below pin both halves so a future lot cannot quietly drop one.


async def test_the_export_carries_the_vote_ledger():
    class _VotePool(_ExportPool):
        async def fetchrow(self, query, *args):
            self.queries.append(query)
            if "FROM topgg_votes" in query:
                return {
                    "last_vote_at": datetime.datetime(
                        2030, 5, 4, tzinfo=datetime.timezone.utc
                    ),
                    "streak": 7,
                    "total_votes": 31,
                    "boost_expires_at": datetime.datetime(
                        2030, 5, 5, tzinfo=datetime.timezone.utc
                    ),
                }
            return None

    pool = _VotePool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["topgg_votes"]["streak"] == 7
    assert data["topgg_votes"]["total_votes"] == 31
    query = next(q for q in pool.queries if "FROM topgg_votes" in q)
    assert "WHERE user_id = $1" in query


async def test_the_export_states_a_user_who_never_voted_as_null():
    """An absent row means "never voted", stated as null rather than as an
    invented streak of zero."""
    data, _avatars = await privacy.collect_user_export(_ExportPool(), 42)

    assert data["topgg_votes"] is None


def test_the_forget_path_covers_the_vote_ledger():
    """Erasing it can only ever COST its owner (streak back to 1, boost gone),
    which is exactly why - unlike the export limiter - it belongs here."""
    listed = dict(privacy.USER_DELETE_QUERIES)
    assert listed["topgg_votes"] == "DELETE FROM topgg_votes WHERE user_id = $1"


# ---------------------------------------------------------------------------
# The AniList rows: the OAuth token and the two alert opt-ins (WAVE-B-D1)
# ---------------------------------------------------------------------------
#
# All three are keyed by user alone, so the guild purge can never reach them and
# /mydata is the only place their deletion can live. The token is a live
# credential the bot holds on the user's behalf; the opt-ins were deleted by
# NOTHING at all before this - their off-switch only sets `enabled = FALSE`.


def test_the_forget_path_takes_the_anilist_oauth_token():
    """A forget that leaves the token behind leaves the bot able to read and
    write that person's AniList list after they asked to be forgotten."""
    listed = dict(privacy.USER_DELETE_QUERIES)
    assert listed["anilist_tokens"] == "DELETE FROM anilist_tokens WHERE user_id = $1"
    assert listed["anilist_tokens"] == privacy.ANILIST_TOKEN_DELETE

    # Not on the narrow, unconfirmed list: `/profile clear` says nothing about
    # AniList, and the row only exists again after a fresh OAuth round trip.
    assert "anilist_tokens" not in dict(privacy.PROFILE_DELETE_QUERIES)


def test_the_voluntary_unlink_runs_the_very_same_statement():
    """`?anilist logout` and `?mydata deleteprofile` must reach the same row by
    the same predicate; an inline copy of the DELETE is exactly how the two
    drift apart."""
    from cogs.anilist import account

    logout = inspect.getsource(account.AccountMixin.anilist_logout.callback)
    assert "delete_anilist_token" in logout
    assert "DELETE FROM" not in logout

    helper = inspect.getsource(privacy.delete_anilist_token)
    assert "ANILIST_TOKEN_DELETE" in helper


def test_the_forget_path_takes_both_alert_optins():
    listed = dict(privacy.USER_DELETE_QUERIES)
    assert (
        listed["anilist_airing_optins"]
        == "DELETE FROM anilist_airing_optins WHERE user_id = $1"
    )
    assert (
        listed["anilist_chapter_optins"]
        == "DELETE FROM anilist_chapter_optins WHERE user_id = $1"
    )


def test_the_alert_optins_have_no_other_deletion_path_in_the_repo():
    """THE finding, pinned repo-wide rather than in this module only.

    Turning an alert off writes `enabled = FALSE` and keeps the row on purpose
    (a member whose DMs closed re-arms it with one command), so the toggle is
    not an erasure and never was. If this list ever grows a second file, that
    file is either a real second erasure path - fine, say so here - or a row
    quietly deleted somewhere the privacy surface knows nothing about.
    """
    deleters = {}
    for path in _repo_python_files():
        source = open(path, encoding="utf-8").read()
        tables = sorted(
            set(
                re.findall(
                    r"DELETE FROM\s+(anilist_(?:airing|chapter)_optins)", source
                )
            )
        )
        if tables:
            deleters[os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")] = tables

    assert deleters == {
        "tools/privacy.py": ["anilist_airing_optins", "anilist_chapter_optins"],
    }


async def test_the_export_still_shows_both_optins_it_now_deletes():
    """Erasure and export are the two halves of the same right: a row the user
    can now delete must still be one they can SEE."""

    class _OptinPool(_ExportPool):
        async def fetchrow(self, query, *args):
            self.queries.append(query)
            if "anilist_airing_optins" in query or "anilist_chapter_optins" in query:
                return {
                    "anilist_user_id": 7,
                    "enabled": True,
                    "created_at": datetime.datetime(
                        2030, 5, 4, tzinfo=datetime.timezone.utc
                    ),
                }
            return None

    pool = _OptinPool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["anilist"]["airing_notifications"]["anilist_user_id"] == 7
    assert data["anilist"]["chapter_notifications"]["enabled"] is True
    for table in ("anilist_airing_optins", "anilist_chapter_optins"):
        query = next(q for q in pool.queries if table in q)
        assert "WHERE user_id = $1" in query


# ---------------------------------------------------------------------------
# The moderator side of `cases` (WAVE-B-D1)
# ---------------------------------------------------------------------------


async def test_the_moderator_export_carries_the_action_and_not_the_third_party():
    """A former moderator's export must not be a roster of who they sanctioned.

    The rows filed under `moderator_id` are the requester's ACTION facts (which
    server, which case, what they did, when). The target's user id, the
    free-text reason and the sanction's expiry are about the OTHER person, and
    they leave with them - same call as `closed_by` on tickets.
    """

    class _ModeratorPool(_ExportPool):
        async def fetch(self, query, *args):
            self.queries.append(query)
            if "moderator_id = $1" in query:
                return [
                    {
                        "guild_id": 7,
                        "case_number": 12,
                        "action": "ban",
                        "created_at": datetime.datetime(
                            2030, 5, 4, tzinfo=datetime.timezone.utc
                        ),
                    }
                ]
            return []

    pool = _ModeratorPool()
    data, _avatars = await privacy.collect_user_export(pool, 42)

    assert data["moderation_cases_as_moderator"] == [
        {
            "guild_id": 7,
            "case_number": 12,
            "action": "ban",
            "created_at": datetime.datetime(2030, 5, 4, tzinfo=datetime.timezone.utc),
        }
    ]

    query = next(q for q in pool.queries if "moderator_id = $1" in q)
    assert query.startswith("SELECT guild_id, case_number, action, created_at")
    for column in ("target_user_id", "user_id AS", "reason", "expires"):
        assert column not in query

    # The TARGET-side projection is untouched: that record IS the requester's,
    # reason and all, and reducing it would take away their own data.
    target_query = next(
        q
        for q in pool.queries
        if "FROM cases" in q and "WHERE user_id = $1" in q
    )
    assert "reason" in target_query
    # ...and it still never names the moderator who acted.
    assert "moderator_id" not in target_query


def test_the_confirmed_erasure_drops_the_live_xp_boost_with_the_row():
    """The row is the durable half; the boost the XP hot path reads is an
    in-memory entry on the Leveling cog. A forget that deleted only the row
    would keep boosting someone who just asked to be forgotten."""
    from cogs.community import usersettings

    source = inspect.getsource(usersettings.ProfileDeletionView.confirm)
    assert "delete_user_data" in source
    assert "forget_vote_boost" in source


# Every table the WIDE list deletes that the narrow `/profile clear` does not
# already cover, mapped to the phrase the confirmation prompt and the result
# message must each contain for it. The mapping is asserted EXHAUSTIVE against
# USER_DELETE_QUERIES below, which is the whole point: a table added to the wide
# list with no phrase registered here fails this test until somebody writes the
# sentence that tells its owner it is about to go.
_WIDE_ONLY_PROMPT_PHRASES = {
    "topgg_votes": "top.gg vote record",
    "user_rank_cards": "background image you",
    "anilist_tokens": "AniList link",
    "anilist_airing_optins": "alert opt-in",
    "anilist_chapter_optins": "alert opt-in",
}
_WIDE_ONLY_RESULT_PHRASES = {
    "topgg_votes": "top.gg vote record is gone",
    "user_rank_cards": "personal rank card",
    "anilist_tokens": "AniList link is gone",
    "anilist_airing_optins": "alert opt-in",
    "anilist_chapter_optins": "alert opt-in",
}


def test_the_confirmation_names_everything_it_destroys():
    """A permanent, unrecreatable loss must be stated BEFORE the button, not
    discovered afterwards. That is the entry price for being on the wide list at
    all, so every table there that its owner cannot simply type again has to be
    named in the warning: the vote streak and lifetime count, the rank-card
    background (an image FILE they may no longer hold a copy of), and the
    AniList link (a live OAuth credential that comes back only through a fresh
    sign-in, never by typing something again).

    Derived from USER_DELETE_QUERIES rather than hardcoded, so growing the wide
    list without growing the consent screen is a test failure and not a silent
    widening of what one button destroys."""
    from cogs.community import usersettings

    narrow = {table for table, _query in privacy.PROFILE_DELETE_QUERIES}
    wide_only = {
        table for table, _query in privacy.USER_DELETE_QUERIES if table not in narrow
    }
    assert wide_only == set(_WIDE_ONLY_PROMPT_PHRASES)
    assert wide_only == set(_WIDE_ONLY_RESULT_PHRASES)

    warning = inspect.getsource(
        usersettings.UserSettings.mydata_deleteprofile.callback
    )
    for table in sorted(wide_only):
        assert _WIDE_ONLY_PROMPT_PHRASES[table] in warning, table
    assert "cannot be undone" in warning

    # ... and the message shown AFTER the button has to agree with the prompt:
    # a screen that warned about something must confirm it actually went.
    result = inspect.getsource(usersettings.ProfileDeletionView.confirm)
    for table in sorted(wide_only):
        assert _WIDE_ONLY_RESULT_PHRASES[table] in result, table
