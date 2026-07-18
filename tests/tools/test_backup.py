"""Tests for the pure parts of tools/backup.py.

The subprocess seam (run_backup) is exercised live by the orchestrator against a
real Postgres; these tests pin the pure logic that must be correct regardless of
the backend: the filename shape, rotation selection, and - critically - that the
parsed DSN never reveals the password through str/repr.
"""

from datetime import datetime, timedelta

from tools import backup

# ---------------------------------------------------------------------------
# backup_filename
# ---------------------------------------------------------------------------


def test_backup_filename_exact_format():
    name = backup.backup_filename(datetime(2026, 7, 12, 13, 45, 1))
    assert name == "yasuho-20260712-134501.dump"


def test_backup_filename_zero_pads_all_fields():
    name = backup.backup_filename(datetime(2026, 1, 2, 3, 4, 5))
    assert name == "yasuho-20260102-030405.dump"


def test_backup_filename_roundtrips_through_parser():
    # A name we emit must be recognised as a dump by the rotation parser.
    now = datetime(2026, 12, 31, 23, 59, 59)
    assert backup._parse_ts(backup.backup_filename(now)) == now


# ---------------------------------------------------------------------------
# rotation_victims
# ---------------------------------------------------------------------------


def _dump(ts: str) -> str:
    return f"yasuho-{ts}.dump"


def _names(n: int) -> list[str]:
    # n dumps, one per day, oldest first.
    return [_dump(f"202607{day:02d}-120000") for day in range(1, n + 1)]


def test_rotation_keeps_newest_n_deletes_the_rest():
    names = _names(20)  # 20 dated dumps
    victims = backup.rotation_victims(names, keep=14)
    # The 6 oldest go; the 14 newest stay.
    assert len(victims) == 6
    assert set(victims) == set(names[:6])
    assert set(names[6:]).isdisjoint(victims)


def test_rotation_default_keep_is_fourteen():
    names = _names(20)
    victims = backup.rotation_victims(names)  # default BACKUP_KEEP
    assert backup.BACKUP_KEEP == 14
    assert len(victims) == 6


def test_rotation_fewer_than_keep_deletes_nothing():
    assert backup.rotation_victims(_names(5), keep=14) == []


def test_rotation_exactly_keep_deletes_nothing():
    assert backup.rotation_victims(_names(14), keep=14) == []


def test_rotation_ignores_foreign_files():
    names = [
        "notes.txt",
        "yasuho.dump",  # no timestamp
        "yasuho-bogus.dump",  # unparseable timestamp
        "readme",
        _dump("20260701-120000"),
        _dump("20260702-120000"),
    ]
    victims = backup.rotation_victims(names, keep=1)
    # Only the older real dump is a victim; foreign files are untouched.
    assert victims == [_dump("20260701-120000")]


def test_rotation_handles_duplicate_timestamps_stably():
    # Two files can share a second (startup + an immediate manual ?backup would
    # not, since names collide, but a copied/renamed file could). Selection must
    # be deterministic: keep=1 keeps exactly one, drops the rest by name order.
    names = [
        _dump("20260701-120000"),
        _dump("20260701-120000"),  # exact duplicate string
        _dump("20260702-120000"),
    ]
    victims = backup.rotation_victims(names, keep=1)
    assert victims == [_dump("20260701-120000"), _dump("20260701-120000")]


def test_rotation_keep_zero_deletes_all_real_dumps():
    names = _names(3)
    assert set(backup.rotation_victims(names, keep=0)) == set(names)


# ---------------------------------------------------------------------------
# parse_dsn / PgConn redaction
# ---------------------------------------------------------------------------


_DSN = "postgresql://yasuho:s3cr3t-p%40ss@localhost:5432/yasuho_db"


def test_parse_dsn_extracts_connection_pieces():
    conn = backup.parse_dsn(_DSN)
    assert conn.host == "localhost"
    assert conn.port == "5432"
    assert conn.user == "yasuho"
    assert conn.dbname == "yasuho_db"


def test_parse_dsn_percent_decodes_password():
    conn = backup.parse_dsn(_DSN)
    # The real password (for PGPASSWORD) is the decoded form.
    assert conn.pgpassword == "s3cr3t-p@ss"


def test_pgconn_repr_hides_password():
    conn = backup.parse_dsn(_DSN)
    assert "s3cr3t" not in repr(conn)
    assert "s3cr3t" not in str(conn)
    assert "p@ss" not in repr(conn)
    assert "***" in repr(conn)


def test_pgconn_repr_still_shows_nonsecret_fields():
    conn = backup.parse_dsn(_DSN)
    text = repr(conn)
    assert "localhost" in text
    assert "yasuho_db" in text


def test_pgconn_repr_no_password_shows_none():
    conn = backup.parse_dsn("postgresql://yasuho@localhost/yasuho_db")
    assert conn.pgpassword is None
    assert "***" not in repr(conn)


def test_dump_args_never_contain_the_password():
    conn = backup.parse_dsn(_DSN)
    args = conn.dump_args()
    joined = " ".join(args)
    assert "s3cr3t" not in joined
    assert "p@ss" not in joined
    assert "--host=localhost" in args
    assert "--port=5432" in args
    assert "--username=yasuho" in args
    assert "--dbname=yasuho_db" in args


# ---------------------------------------------------------------------------
# newest_dump
# ---------------------------------------------------------------------------


def test_newest_dump_picks_the_latest_timestamp():
    names = _names(5)  # 2026-07-01 .. 2026-07-05
    ts, name = backup.newest_dump(names)
    assert name == _dump("20260705-120000")
    assert ts == datetime(2026, 7, 5, 12, 0, 0)


def test_newest_dump_ignores_foreign_files():
    names = ["README.md", "notes.txt", _dump("20260701-120000")]
    ts, name = backup.newest_dump(names)
    assert name == _dump("20260701-120000")


def test_newest_dump_none_when_no_dumps():
    assert backup.newest_dump(["README.md", "x.log"]) is None
    assert backup.newest_dump([]) is None


def test_newest_dump_ordering_ignores_list_order():
    # Insertion order must not matter; selection is by embedded timestamp.
    names = [_dump("20260703-120000"), _dump("20260701-120000"),
             _dump("20260705-120000"), _dump("20260702-120000")]
    _, name = backup.newest_dump(names)
    assert name == _dump("20260705-120000")


# ---------------------------------------------------------------------------
# latest_backup_report
# ---------------------------------------------------------------------------


def test_latest_backup_report_none_for_missing_dir(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert backup.latest_backup_report(str(missing)) is None


def test_latest_backup_report_none_when_no_dumps(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    assert backup.latest_backup_report(str(tmp_path)) is None


def test_latest_backup_report_reports_newest_with_size(tmp_path):
    (tmp_path / _dump("20260701-120000")).write_bytes(b"old")
    newest = tmp_path / _dump("20260705-120000")
    newest.write_bytes(b"newer-content")
    report = backup.latest_backup_report(str(tmp_path))
    assert report.name == _dump("20260705-120000")
    assert report.path == str(newest)
    assert report.timestamp == datetime(2026, 7, 5, 12, 0, 0)
    assert report.size == len(b"newer-content")


def test_backup_report_age_is_now_minus_timestamp():
    report = backup.BackupReport(
        name="x", path="/x", timestamp=datetime(2026, 7, 5, 12, 0, 0), size=1
    )
    age = report.age(datetime(2026, 7, 6, 12, 0, 0))
    assert age == timedelta(hours=24)


# ---------------------------------------------------------------------------
# _map_verify_result (pg_restore --list outcome mapping; no subprocess)
# ---------------------------------------------------------------------------


def test_map_verify_result_ok_on_zero_exit():
    result = backup._map_verify_result(0, b"")
    assert result.ok is True
    assert result.error is None


def test_map_verify_result_error_on_nonzero_exit():
    result = backup._map_verify_result(1, b"pg_restore: error: did not find magic")
    assert result.ok is False
    assert "exit 1" in result.error
    assert "magic" in result.error


def test_map_verify_result_bounds_the_stderr_tail():
    result = backup._map_verify_result(1, b"x" * 5000)
    assert result.ok is False
    # Error carries a prefix plus at most 500 chars of stderr detail.
    assert len(result.error) < 600


def test_map_verify_result_handles_none_stderr():
    result = backup._map_verify_result(2, None)
    assert result.ok is False
    assert "exit 2" in result.error
