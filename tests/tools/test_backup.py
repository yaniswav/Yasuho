"""Tests for the pure parts of tools/backup.py.

The subprocess seam (run_backup) is exercised live by the orchestrator against a
real Postgres; these tests pin the pure logic that must be correct regardless of
the backend: the filename shape, rotation selection, encryption tool selection
and argv shapes, passphrase-file handling, and - critically - that neither the
parsed DSN nor any argv ever reveals a secret.
"""

import os
import shutil
import signal
import subprocess
from datetime import datetime, timedelta

import pytest

from tools import backup

_HAS_GPG = shutil.which("gpg") is not None
_HAS_OPENSSL = shutil.which("openssl") is not None
_HAS_PG_RESTORE = shutil.which("pg_restore") is not None

# Captured before the autouse fixture below redirects it, for the one test that
# has to assert on the REAL location.
_REAL_DEFAULT_KEY_PATH = backup.default_key_path


@pytest.fixture(autouse=True)
def _never_touch_the_real_key(tmp_path, monkeypatch):
    """Redirect default_key_path into tmp_path for every test in this module.

    ensure_key CREATES the file it is pointed at, so any test that reaches
    run_backup or verify_backup without passing an explicit key_path would
    generate a real config/backup.key in the developer's checkout - and, worse,
    a later run could then encrypt real dumps under a key a test invented.
    Nothing under tests/ may write into config/, so the default is redirected
    unconditionally rather than left to each test to remember.
    """
    monkeypatch.setattr(
        backup, "default_key_path", lambda: str(tmp_path / "default-backup.key")
    )


class _FakeProc:
    """Stand-in for an asyncio subprocess: a fixed exit code and stderr.

    Lets the failure-attribution paths of run_backup be driven exactly (an exit
    code such as -SIGPIPE is not reproducible on demand with real binaries).
    """

    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _fake_pipeline(monkeypatch, dump_proc, enc_proc):
    """Make run_backup's two create_subprocess_exec calls return these procs."""
    procs = iter([dump_proc, enc_proc])

    async def fake_exec(*args, **kwargs):
        return next(procs)

    monkeypatch.setattr(backup.asyncio, "create_subprocess_exec", fake_exec)

# ---------------------------------------------------------------------------
# backup_filename
# ---------------------------------------------------------------------------


def test_backup_filename_exact_format():
    name = backup.backup_filename(datetime(2026, 7, 12, 13, 45, 1))
    assert name == "yasuho-20260712-134501.dump.gpg"


def test_backup_filename_zero_pads_all_fields():
    name = backup.backup_filename(datetime(2026, 1, 2, 3, 4, 5))
    assert name == "yasuho-20260102-030405.dump.gpg"


def test_backup_filename_defaults_to_the_encrypted_suffix():
    # No caller may accidentally produce a plaintext-looking name.
    assert backup.backup_filename(datetime(2026, 1, 1)).endswith(".dump.gpg")


def test_backup_filename_honours_the_openssl_suffix():
    name = backup.backup_filename(datetime(2026, 1, 2, 3, 4, 5), backup.OPENSSL_SUFFIX)
    assert name == "yasuho-20260102-030405.dump.enc"


@pytest.mark.parametrize(
    "suffix", [backup.GPG_SUFFIX, backup.OPENSSL_SUFFIX, backup.PLAIN_SUFFIX]
)
def test_backup_filename_roundtrips_through_parser(suffix):
    # A name we emit must be recognised as a dump by the rotation parser,
    # whichever tool produced it (and for the legacy plaintext shape too).
    now = datetime(2026, 12, 31, 23, 59, 59)
    assert backup._parse_ts(backup.backup_filename(now, suffix)) == now


def test_parse_ts_rejects_the_in_progress_part_file():
    # A .part file is a half-written dump: rotation must never see it as one.
    name = backup.backup_filename(datetime(2026, 7, 12, 13, 45, 1)) + ".part"
    assert backup._parse_ts(name) is None


# ---------------------------------------------------------------------------
# rotation_victims
# ---------------------------------------------------------------------------


def _dump(ts: str) -> str:
    return f"yasuho-{ts}.dump.gpg"


def _plain_dump(ts: str) -> str:
    """A pre-encryption dump, as already sits in backups/ on the live host."""
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


def test_rotation_ignores_in_progress_part_files():
    # A concurrent backup's .part must never be rotated out from under it.
    names = [_dump("20260701-120000"), _dump("20260702-120000") + ".part"]
    assert backup.rotation_victims(names, keep=0) == [_dump("20260701-120000")]


def test_rotation_ages_out_legacy_plaintext_dumps():
    # The pre-encryption dumps already on disk are left alone by this change,
    # but rotation must still recognise them or they would linger forever.
    names = [
        _plain_dump("20260701-120000"),
        _plain_dump("20260702-120000"),
        _dump("20260703-120000"),
    ]
    victims = backup.rotation_victims(names, keep=1)
    assert set(victims) == {
        _plain_dump("20260701-120000"),
        _plain_dump("20260702-120000"),
    }


def test_rotation_orders_plaintext_and_encrypted_on_one_timeline():
    # Mixed suffixes must sort by embedded timestamp, not by suffix.
    names = [
        _plain_dump("20260704-120000"),  # newest, but plaintext
        _dump("20260701-120000"),
        _dump("20260702-120000"),
    ]
    assert backup.rotation_victims(names, keep=1) == [
        _dump("20260702-120000"),
        _dump("20260701-120000"),
    ]


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


# ---------------------------------------------------------------------------
# Encryptor selection (by availability for writing, by suffix for reading)
# ---------------------------------------------------------------------------


def test_resolve_encryptor_prefers_gpg_when_both_exist(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    enc = backup.resolve_encryptor()
    assert enc.name == "gpg"
    assert enc.suffix == backup.GPG_SUFFIX


def test_resolve_encryptor_falls_back_to_openssl_without_gpg(monkeypatch):
    monkeypatch.setattr(
        backup.shutil, "which", lambda binary: None if binary == "gpg" else "/usr/bin/x"
    )
    enc = backup.resolve_encryptor()
    assert enc.name == "openssl"
    assert enc.suffix == backup.OPENSSL_SUFFIX


def test_resolve_encryptor_none_when_no_tool_installed(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda binary: None)
    assert backup.resolve_encryptor() is None


def test_encryptor_for_resolves_by_suffix_not_availability(monkeypatch):
    # Even on a host where only openssl exists, a .dump.gpg must resolve to gpg:
    # a file is only ever readable by the tool that wrote it.
    monkeypatch.setattr(
        backup.shutil, "which", lambda binary: None if binary == "gpg" else "/usr/bin/x"
    )
    assert backup.encryptor_for(_dump("20260701-120000")).name == "gpg"


def test_encryptor_for_openssl_suffix():
    assert backup.encryptor_for("yasuho-20260701-120000.dump.enc").name == "openssl"


def test_encryptor_for_none_on_legacy_plaintext_dump():
    assert backup.encryptor_for(_plain_dump("20260701-120000")) is None


def test_encryptor_for_handles_a_full_path():
    enc = backup.encryptor_for("/srv/backups/" + _dump("20260701-120000"))
    assert enc.name == "gpg"


# ---------------------------------------------------------------------------
# Encryptor argv shapes (the secret must never be an argument)
# ---------------------------------------------------------------------------

_KEY = "/repo/config/backup.key"


@pytest.mark.parametrize("enc", backup.ENCRYPTORS, ids=lambda e: e.name)
def test_encrypt_args_name_the_key_file_never_its_contents(enc):
    args = enc.encrypt_args(_KEY, "/b/out.part")
    assert any(_KEY in arg for arg in args), args
    # The passphrase itself is read by the child from that file; nothing here
    # may look like a literal secret being passed through.
    assert "--passphrase" not in args
    assert args[0] == enc.binary


@pytest.mark.parametrize("enc", backup.ENCRYPTORS, ids=lambda e: e.name)
def test_encrypt_args_write_to_the_given_output_path(enc):
    args = enc.encrypt_args(_KEY, "/b/out.part")
    assert "/b/out.part" in args


@pytest.mark.parametrize("enc", backup.ENCRYPTORS, ids=lambda e: e.name)
def test_encrypt_args_take_no_input_path(enc):
    # Encryption reads stdin (pg_dump is piped in), so the plaintext dump never
    # exists as a file. An input path creeping in would break that guarantee.
    args = enc.encrypt_args(_KEY, "/b/out.part")
    assert "--decrypt" not in args
    assert "-in" not in args


@pytest.mark.parametrize("enc", backup.ENCRYPTORS, ids=lambda e: e.name)
def test_decrypt_args_are_file_to_file(enc):
    args = enc.decrypt_args(_KEY, "/b/in.gpg", "/tmp/out.dump")
    assert "/b/in.gpg" in args
    assert "/tmp/out.dump" in args
    assert any(_KEY in arg for arg in args)


def test_gpg_encrypt_args_use_aes256_and_loopback_pinentry():
    args = backup.GpgEncryptor().encrypt_args(_KEY, "/b/out.part")
    assert "--symmetric" in args
    assert args[args.index("--cipher-algo") + 1] == "AES256"
    # Required for --passphrase-file to be honoured in batch mode on gpg 2.x.
    assert args[args.index("--pinentry-mode") + 1] == "loopback"
    assert "--batch" in args
    # Do not leave the passphrase sitting in the gpg-agent cache.
    assert "--no-symkey-cache" in args


def test_openssl_encrypt_args_use_aes256_cbc_with_pbkdf2_and_salt():
    args = backup.OpensslEncryptor().encrypt_args(_KEY, "/b/out.part")
    assert "-aes-256-cbc" in args
    assert "-pbkdf2" in args  # never the legacy one-pass MD5 derivation
    assert "-salt" in args
    assert f"file:{_KEY}" in args


def test_openssl_decrypt_args_pass_the_decrypt_flag():
    args = backup.OpensslEncryptor().decrypt_args(_KEY, "/b/in.enc", "/tmp/out")
    assert "-d" in args


# ---------------------------------------------------------------------------
# ensure_key
# ---------------------------------------------------------------------------


def test_ensure_key_creates_a_key_file_when_missing(tmp_path):
    path = tmp_path / "sub" / "backup.key"
    assert backup.ensure_key(str(path)) == str(path)
    assert path.exists()
    assert path.read_text().strip()


def test_ensure_key_mode_is_owner_only_even_under_a_wide_umask(tmp_path):
    path = tmp_path / "backup.key"
    old = os.umask(0)  # a permissive umask must not widen the key file
    try:
        backup.ensure_key(str(path))
    finally:
        os.umask(old)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_ensure_key_clamps_an_existing_loose_key(tmp_path):
    path = tmp_path / "backup.key"
    path.write_text("already-here")
    path.chmod(0o644)
    backup.ensure_key(str(path))
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_ensure_key_never_overwrites_an_existing_key(tmp_path):
    # Overwriting would silently orphan every dump encrypted with the old key.
    path = tmp_path / "backup.key"
    path.write_text("do-not-touch-me")
    backup.ensure_key(str(path))
    assert path.read_text() == "do-not-touch-me"


def test_ensure_key_is_idempotent_across_calls(tmp_path):
    path = tmp_path / "backup.key"
    backup.ensure_key(str(path))
    first = path.read_text()
    backup.ensure_key(str(path))
    assert path.read_text() == first


def test_ensure_key_generates_a_high_entropy_secret(tmp_path):
    path = tmp_path / "backup.key"
    backup.ensure_key(str(path))
    # token_urlsafe(48) is ~64 url-safe chars; anything short would mean the
    # generator was swapped for something weak.
    assert len(path.read_text()) >= 43


def test_ensure_key_writes_no_trailing_newline(tmp_path):
    # Both tools read only the first line; keeping the file exactly one token
    # long removes any doubt about what the passphrase actually is.
    path = tmp_path / "backup.key"
    backup.ensure_key(str(path))
    assert "\n" not in path.read_text()


def test_ensure_key_two_calls_generate_distinct_keys(tmp_path):
    a, b = tmp_path / "a.key", tmp_path / "b.key"
    backup.ensure_key(str(a))
    backup.ensure_key(str(b))
    assert a.read_text() != b.read_text()


def test_ensure_key_raises_on_an_empty_key_file(tmp_path):
    # Encrypting with an empty passphrase must never happen silently.
    path = tmp_path / "backup.key"
    path.write_text("")
    with pytest.raises(backup.BackupKeyError):
        backup.ensure_key(str(path))


def test_ensure_key_raises_on_a_whitespace_only_key_file(tmp_path):
    path = tmp_path / "backup.key"
    path.write_text("   \n")
    with pytest.raises(backup.BackupKeyError):
        backup.ensure_key(str(path))


def test_default_key_path_is_the_gitignored_config_file():
    path = _REAL_DEFAULT_KEY_PATH()  # the autouse fixture redirects the live one
    assert os.path.basename(path) == "backup.key"
    assert os.path.basename(os.path.dirname(path)) == "config"
    assert os.path.isabs(path)


# ---------------------------------------------------------------------------
# run_backup: no encryption tool means NO backup (never a plaintext fallback)
# ---------------------------------------------------------------------------


async def test_run_backup_fails_loudly_without_an_encryption_tool(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(backup, "resolve_encryptor", lambda: None)
    with caplog.at_level("ERROR"):
        result = await backup.run_backup(
            "postgresql://u:p@localhost/db", str(tmp_path / "backups")
        )
    assert result.ok is False
    assert "refusing to write a plaintext dump" in result.error
    assert "BACKUP-NOCRYPT" in caplog.text


async def test_run_backup_without_a_tool_writes_nothing_at_all(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(backup, "resolve_encryptor", lambda: None)
    await backup.run_backup("postgresql://u:p@localhost/db", str(backups))
    assert list(backups.iterdir()) == []


async def test_run_backup_fails_on_an_unusable_key(tmp_path, monkeypatch, caplog):
    key = tmp_path / "backup.key"
    key.write_text("")  # present but empty
    monkeypatch.setattr(backup, "resolve_encryptor", lambda: backup.GpgEncryptor())
    with caplog.at_level("ERROR"):
        result = await backup.run_backup(
            "postgresql://u:p@localhost/db",
            str(tmp_path / "backups"),
            key_path=str(key),
        )
    assert result.ok is False
    assert "backup key unusable" in result.error
    assert "BACKUP-KEY" in caplog.text


# ---------------------------------------------------------------------------
# run_backup: which stage gets the blame when the pipeline breaks
# ---------------------------------------------------------------------------


async def _run_with_procs(tmp_path, monkeypatch, dump_rc, enc_rc, enc_err=b""):
    key = tmp_path / "backup.key"
    backup.ensure_key(str(key))
    backups = tmp_path / "backups"
    monkeypatch.setattr(backup, "resolve_encryptor", lambda: backup.GpgEncryptor())
    _fake_pipeline(monkeypatch, _FakeProc(dump_rc), _FakeProc(enc_rc, enc_err))
    result = await backup.run_backup(
        "postgresql://u:p@localhost/db", str(backups), key_path=str(key)
    )
    return result, backups


async def test_encryptor_dying_mid_stream_is_blamed_on_the_encryptor(
    tmp_path, monkeypatch, caplog
):
    # An encryptor that dies before draining stdin (disk full, crash) drops the
    # pipe, so the kernel kills pg_dump with SIGPIPE. Blaming pg_dump for that
    # would report an empty-stderr "exit -13" and skip BACKUP-NOCRYPT entirely,
    # which is the marker the module docstring tells operators to grep for.
    with caplog.at_level("ERROR"):
        result, backups = await _run_with_procs(
            tmp_path, monkeypatch, dump_rc=-signal.SIGPIPE, enc_rc=1
        )
    assert result.ok is False
    assert result.error.startswith("gpg exit 1")
    assert "pg_dump exit" not in result.error
    assert "BACKUP-NOCRYPT" in caplog.text
    assert not backups.exists() or list(backups.iterdir()) == []


async def test_encryptor_failure_after_draining_stdin_is_reported_the_same_way(
    tmp_path, monkeypatch, caplog
):
    # Same fault, but the encryptor happened to consume the whole dump first so
    # pg_dump exits cleanly. Whether this or the SIGPIPE shape occurs depends on
    # the dump's size; the operator must see the same diagnosis either way.
    with caplog.at_level("ERROR"):
        result, _ = await _run_with_procs(
            tmp_path, monkeypatch, dump_rc=0, enc_rc=1, enc_err=b"No space left"
        )
    assert result.ok is False
    assert result.error.startswith("gpg exit 1")
    assert "No space left" in result.error
    assert "BACKUP-NOCRYPT" in caplog.text


async def test_a_real_pg_dump_failure_is_still_blamed_on_pg_dump(
    tmp_path, monkeypatch, caplog
):
    # The inversion is scoped to SIGPIPE: an ordinary pg_dump failure keeps its
    # own attribution, and the encryptor's fallout complaint stays suppressed.
    with caplog.at_level("ERROR"):
        result, _ = await _run_with_procs(tmp_path, monkeypatch, dump_rc=1, enc_rc=1)
    assert result.ok is False
    assert result.error.startswith("pg_dump exit 1")
    assert "BACKUP-NOCRYPT" not in caplog.text


async def test_sigpipe_with_a_clean_encryptor_is_blamed_on_pg_dump(
    tmp_path, monkeypatch
):
    # SIGPIPE while the encryptor exits 0 means the DUMP was cut short, not the
    # encryption: the ciphertext would be a valid encryption of a partial dump,
    # so this must fail as a pg_dump problem and write nothing.
    result, backups = await _run_with_procs(
        tmp_path, monkeypatch, dump_rc=-signal.SIGPIPE, enc_rc=0
    )
    assert result.ok is False
    assert result.error.startswith(f"pg_dump exit {-signal.SIGPIPE}")
    assert not backups.exists() or list(backups.iterdir()) == []


# ---------------------------------------------------------------------------
# Integrity check: size floor, decrypt mapping, verify dispatch
# ---------------------------------------------------------------------------


def test_size_floor_rejects_a_truncated_file(tmp_path):
    path = tmp_path / "x.dump.gpg"
    path.write_bytes(b"tiny")
    result = backup._check_size_floor(str(path))
    assert result.ok is False
    assert "truncated or empty" in result.error


def test_size_floor_accepts_a_plausible_file(tmp_path):
    path = tmp_path / "x.dump.gpg"
    path.write_bytes(b"x" * backup._MIN_ENCRYPTED_BYTES)
    assert backup._check_size_floor(str(path)).ok is True


def test_size_floor_reports_a_missing_file(tmp_path):
    result = backup._check_size_floor(str(tmp_path / "nope.dump.gpg"))
    assert result.ok is False
    assert "cannot stat" in result.error


def test_map_decrypt_result_ok_on_zero_exit():
    assert backup._map_decrypt_result("gpg", 0, b"").ok is True


def test_map_decrypt_result_error_carries_the_tool_and_exit():
    result = backup._map_decrypt_result("gpg", 2, b"decryption failed: Bad session key")
    assert result.ok is False
    assert "gpg --decrypt exit 2" in result.error
    assert "Bad session key" in result.error


def test_map_decrypt_result_bounds_the_stderr_tail():
    result = backup._map_decrypt_result("openssl", 1, b"x" * 5000)
    assert len(result.error) < 600


async def test_verify_backup_rejects_a_truncated_encrypted_dump(tmp_path):
    path = tmp_path / _dump("20260701-120000")
    path.write_bytes(b"not really a dump")
    result = await backup.verify_backup(str(path))
    assert result.ok is False
    assert "truncated or empty" in result.error


async def test_verify_backup_reports_a_missing_key(tmp_path):
    path = tmp_path / _dump("20260701-120000")
    path.write_bytes(b"x" * 4096)
    result = await backup.verify_backup(
        str(path), key_path=str(tmp_path / "absent.key")
    )
    assert result.ok is False
    assert "missing" in result.error


async def test_verify_backup_reports_a_missing_tool(tmp_path, monkeypatch):
    path = tmp_path / _dump("20260701-120000")
    path.write_bytes(b"x" * 4096)
    monkeypatch.setattr(backup.shutil, "which", lambda binary: None)
    result = await backup.verify_backup(str(path))
    assert result.ok is False
    assert "not installed" in result.error


async def test_verify_backup_falls_back_to_plain_pg_restore_for_legacy_dumps(
    tmp_path, monkeypatch
):
    # A pre-encryption .dump must still be verifiable: no decrypt stage at all.
    seen = {}

    async def fake_list(path):
        seen["path"] = path
        return backup.VerifyResult(ok=True)

    monkeypatch.setattr(backup, "_pg_restore_list", fake_list)
    path = tmp_path / _plain_dump("20260701-120000")
    path.write_bytes(b"x" * 4096)
    result = await backup.verify_backup(str(path))
    assert result.ok is True
    assert seen["path"] == str(path)


async def _verify_with_a_clean_decrypt(tmp_path, monkeypatch, list_result):
    """Drive verify_backup with a decrypt that succeeds, and a stubbed stage 2."""
    path = tmp_path / _dump("20260701-120000")
    path.write_bytes(b"x" * 4096)
    key = tmp_path / "backup.key"
    backup.ensure_key(str(key))
    monkeypatch.setattr(backup.shutil, "which", lambda binary: "/usr/bin/" + binary)

    async def fake_exec(*args, **kwargs):
        return _FakeProc(0)  # the decrypt reports success

    monkeypatch.setattr(backup.asyncio, "create_subprocess_exec", fake_exec)

    seen = {}

    async def fake_list(plain_path):
        seen["path"] = plain_path
        return list_result

    monkeypatch.setattr(backup, "_pg_restore_list", fake_list)
    result = await backup.verify_backup(str(path), key_path=str(key))
    return result, seen, path


async def test_verify_backup_runs_pg_restore_list_on_the_decrypted_file(
    tmp_path, monkeypatch
):
    # Stage 2 must run, and must run on the RECOVERED PLAINTEXT - never on the
    # ciphertext, which pg_restore could not parse anyway.
    result, seen, cipher = await _verify_with_a_clean_decrypt(
        tmp_path, monkeypatch, backup.VerifyResult(ok=True)
    )
    assert result.ok is True
    assert seen["path"] != str(cipher)
    assert os.path.basename(seen["path"]).startswith(".verify-")


async def test_verify_backup_fails_when_stage_two_fails_despite_a_clean_decrypt(
    tmp_path, monkeypatch
):
    # "Both must pass": a ciphertext that decrypts perfectly into something that
    # is not a -Fc archive is NOT a good backup, and must not report ok.
    result, _, _ = await _verify_with_a_clean_decrypt(
        tmp_path,
        monkeypatch,
        backup.VerifyResult(ok=False, error="pg_restore --list exit 1: bad header"),
    )
    assert result.ok is False
    assert "pg_restore --list" in result.error


@pytest.mark.skipif(
    not (_HAS_GPG and _HAS_PG_RESTORE),
    reason="needs gpg and pg_restore on this host",
)
async def test_verify_backup_rejects_a_real_ciphertext_of_a_non_archive(tmp_path):
    # End to end with the real tools: stage 1 passes (the file decrypts cleanly,
    # MDC and all) and stage 2 is the only thing standing between the operator
    # and a false all-clear on a dump that is not an archive.
    key = tmp_path / "backup.key"
    backup.ensure_key(str(key))
    enc = backup.GpgEncryptor()
    path = tmp_path / _dump("20260701-120000")
    subprocess.run(
        enc.encrypt_args(str(key), str(path)), input=os.urandom(20000), check=True
    )
    assert path.stat().st_size > backup._MIN_ENCRYPTED_BYTES  # clears the floor
    result = await backup.verify_backup(str(path), key_path=str(key))
    assert result.ok is False
    assert "pg_restore --list" in result.error
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".verify-")] == []


async def test_verify_backup_leaves_no_decrypted_copy_behind(tmp_path, monkeypatch):
    # The temp plaintext must never outlive the probe, on success or failure.
    path = tmp_path / _dump("20260701-120000")
    path.write_bytes(b"x" * 4096)
    backup.ensure_key(str(tmp_path / "backup.key"))
    monkeypatch.setattr(backup.shutil, "which", lambda binary: "/usr/bin/" + binary)

    async def boom(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(backup.asyncio, "create_subprocess_exec", boom)
    result = await backup.verify_backup(str(path), key_path=str(tmp_path / "backup.key"))
    assert result.ok is False
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".verify-")] == []


# ---------------------------------------------------------------------------
# Live tool roundtrip: the argv shapes above must actually work on this host
# ---------------------------------------------------------------------------


def _roundtrip(enc, tmp_path):
    key = tmp_path / "backup.key"
    backup.ensure_key(str(key))
    plaintext = os.urandom(20000)
    cipher = tmp_path / ("out" + enc.suffix)
    recovered = tmp_path / "back.dump"

    done = subprocess.run(
        enc.encrypt_args(str(key), str(cipher)),
        input=plaintext,
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr
    assert cipher.read_bytes() != plaintext  # actually encrypted

    done = subprocess.run(
        enc.decrypt_args(str(key), str(cipher), str(recovered)),
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr
    assert recovered.read_bytes() == plaintext


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not installed on this host")
def test_gpg_argv_roundtrips_byte_identically(tmp_path):
    _roundtrip(backup.GpgEncryptor(), tmp_path)


@pytest.mark.skipif(not _HAS_OPENSSL, reason="openssl not installed on this host")
def test_openssl_argv_roundtrips_byte_identically(tmp_path):
    _roundtrip(backup.OpensslEncryptor(), tmp_path)


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not installed on this host")
def test_gpg_decrypt_fails_on_a_truncated_ciphertext(tmp_path):
    # The property the whole integrity check rests on: a damaged file cannot
    # decrypt cleanly, so BACKUP-CORRUPT fires instead of a false all-clear.
    key = tmp_path / "backup.key"
    backup.ensure_key(str(key))
    enc = backup.GpgEncryptor()
    cipher = tmp_path / ("out" + enc.suffix)
    subprocess.run(
        enc.encrypt_args(str(key), str(cipher)), input=os.urandom(50000), check=True
    )
    cipher.write_bytes(cipher.read_bytes()[:1000])  # truncate it
    done = subprocess.run(
        enc.decrypt_args(str(key), str(cipher), str(tmp_path / "back.dump")),
        capture_output=True,
    )
    assert done.returncode != 0


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not installed on this host")
def test_gpg_decrypt_fails_with_the_wrong_key(tmp_path):
    key = tmp_path / "backup.key"
    other = tmp_path / "other.key"
    backup.ensure_key(str(key))
    backup.ensure_key(str(other))
    enc = backup.GpgEncryptor()
    cipher = tmp_path / ("out" + enc.suffix)
    subprocess.run(
        enc.encrypt_args(str(key), str(cipher)), input=os.urandom(5000), check=True
    )
    done = subprocess.run(
        enc.decrypt_args(str(other), str(cipher), str(tmp_path / "back.dump")),
        capture_output=True,
    )
    assert done.returncode != 0
