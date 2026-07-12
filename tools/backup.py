"""Postgres backups for Yasuho: pg_dump on startup and on demand.

There is no cron/systemd on the host (the run model is ./run.sh in a terminal),
so backups are taken by the bot itself: one fire-and-forget dump at startup
(scheduled from core.py, never blocking readiness) and one on demand via the
owner-only ?backup command. Both go through run_backup below.

Design notes:

- The DSN carries the database password. It is NEVER placed on a subprocess
  command line (ps would expose it) and NEVER logged or echoed. We split the DSN
  into safe non-secret args (host/port/user/dbname on argv) and pass ONLY the
  password through the ``PGPASSWORD`` environment variable, which libpq reads
  natively. argv therefore never contains the secret; ``ps`` is clean. (Passing
  the whole URI on argv would leak it; pg_dump cannot read a value from a named
  env var, so PGPASSWORD is the clean channel for just the secret.)

- The dump is written to ``<name>.part`` and atomically renamed to ``<name>``
  only after a clean exit + fsync, so a crashed or killed dump never leaves a
  file that looks like a valid backup. Rotation only ever sees complete dumps.

- Custom format (``-Fc``): compressed, and restorable selectively with
  pg_restore. See the RESTORE section below for the exact procedure.

Everything above run_backup is pure and unit-tested (filename, rotation,
DSN parsing with password redaction). run_backup is the async subprocess seam.


RESTORE PROCEDURE (manual, deliberate - there is intentionally NO restore code)
------------------------------------------------------------------------------
A restore overwrites live data, so it is always a human act. Do this by hand:

1. STOP THE BOT FIRST. A running bot holds connections and will keep writing;
   restoring under it corrupts the result. Quit ./run.sh (Ctrl-C) and confirm
   the process is gone before touching the database.

2. Pick the dump you want from backups/ (they are named by UTC timestamp,
   newest last alphabetically):

       ls -1 backups/yasuho-*.dump

3. Restore into the EXISTING database, replacing objects as it goes:

       PGPASSWORD='<password>' pg_restore \
           --host=localhost --port=5432 --username=yasuho \
           --dbname=yasuho_db --clean --if-exists --no-owner \
           backups/yasuho-YYYYMMDD-HHMMSS.dump

   --clean --if-exists drops each object before recreating it (the drop/recreate
   caveat: this DESTROYS current table contents as it restores; that is the
   point of a restore, but there is no undo). --no-owner keeps ownership as the
   restoring role rather than failing on a missing original role.

   Never put the password on the pg_restore command line as an argument; keep it
   in PGPASSWORD as shown so ps does not expose it.

4. For a from-scratch rebuild instead (database dropped/recreated), create an
   empty yasuho_db first, then run the same command WITHOUT --clean --if-exists.

5. Restart the bot (./run.sh). Startup will take a fresh backup as usual.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

# How many dumps to keep. At ~one dump per restart plus one per manual ?backup,
# 14 is a comfortable trailing window without unbounded growth (see SCALE STORY
# in the PR notes). Tunable here; the command and startup path both honour it.
BACKUP_KEEP = 14

# Timestamp shape embedded in the filename, e.g. yasuho-20260712-134501.dump.
# UTC, second resolution, lexically sortable so string order == chronological.
_TS_FORMAT = "%Y%m%d-%H%M%S"
_PREFIX = "yasuho-"
_SUFFIX = ".dump"
_PART_SUFFIX = ".part"


def backup_filename(now: datetime) -> str:
    """Return the dump filename for ``now`` (naive/aware both fine, read as-is).

    Format: ``yasuho-YYYYMMDD-HHMMSS.dump``. The caller decides the timezone;
    core.py and the command both pass ``datetime.utcnow()`` so filenames sort
    chronologically as plain strings.
    """
    return f"{_PREFIX}{now.strftime(_TS_FORMAT)}{_SUFFIX}"


def _parse_ts(name: str) -> datetime | None:
    """Extract the embedded timestamp from a dump name, or None if it is not one.

    Foreign files (anything not matching ``yasuho-<ts>.dump`` with a parseable
    timestamp) return None so rotation ignores them rather than deleting them.
    """
    if not (name.startswith(_PREFIX) and name.endswith(_SUFFIX)):
        return None
    core = name[len(_PREFIX) : -len(_SUFFIX)]
    try:
        return datetime.strptime(core, _TS_FORMAT)
    except ValueError:
        return None


def rotation_victims(existing_names, keep: int = BACKUP_KEEP) -> list[str]:
    """Return the dump names to delete so only the newest ``keep`` remain.

    Only recognised dump names (``yasuho-<ts>.dump`` with a valid timestamp) are
    considered; foreign files are ignored and never returned. Ordering is by the
    timestamp embedded in the name (not filesystem mtime), so it is deterministic
    and testable. Ties on identical timestamps are broken by name for stability.
    With ``keep`` or fewer real dumps, nothing is deleted.
    """
    if keep < 0:
        keep = 0
    dated = [(ts, name) for name in existing_names if (ts := _parse_ts(name))]
    # Newest first; secondary sort on name keeps duplicate timestamps stable.
    dated.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [name for _, name in dated[keep:]]


@dataclass(frozen=True)
class PgConn:
    """Non-secret connection pieces plus the password, with redacted display.

    The password is a plain field but ``repr``/``str`` never reveal it, so a
    PgConn can be logged or dropped into a traceback without leaking the secret.
    Use ``pgpassword`` explicitly (and only) to feed the PGPASSWORD env var.
    """

    host: str | None
    port: str | None
    user: str | None
    dbname: str | None
    password: str | None

    def __repr__(self) -> str:  # never expose the password
        pw = "***" if self.password else None
        return (
            f"PgConn(host={self.host!r}, port={self.port!r}, user={self.user!r}, "
            f"dbname={self.dbname!r}, password={pw!r})"
        )

    __str__ = __repr__

    @property
    def pgpassword(self) -> str | None:
        """The raw password for the PGPASSWORD env var (never for argv/logs)."""
        return self.password

    def dump_args(self) -> list[str]:
        """libpq connection args safe to place on argv (no secret among them)."""
        args: list[str] = []
        if self.host:
            args.append(f"--host={self.host}")
        if self.port:
            args.append(f"--port={self.port}")
        if self.user:
            args.append(f"--username={self.user}")
        if self.dbname:
            args.append(f"--dbname={self.dbname}")
        return args


def parse_dsn(dsn: str) -> PgConn:
    """Split a ``postgresql://`` DSN into a PgConn (password kept, but redacted).

    Percent-encoded userinfo is decoded so the password handed to PGPASSWORD is
    the real one. The leading '/' of the path is the database name. Never logs
    the input.
    """
    parts = urlsplit(dsn)
    dbname = parts.path.lstrip("/") or None
    return PgConn(
        host=parts.hostname,
        port=str(parts.port) if parts.port else None,
        user=unquote(parts.username) if parts.username else None,
        dbname=unquote(dbname) if dbname else None,
        password=unquote(parts.password) if parts.password else None,
    )


@dataclass(frozen=True)
class BackupResult:
    """Outcome of one run_backup call (safe to log/echo - no secrets)."""

    ok: bool
    path: str | None = None
    size: int | None = None
    deleted: int = 0
    error: str | None = None


def human_size(num: int) -> str:
    """Human-readable byte count, e.g. 12.3 MiB. ASCII only, one decimal place."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"  # unreachable; keeps type checkers happy


async def run_backup(
    dsn: str, backups_dir: str, *, keep: int = BACKUP_KEEP
) -> BackupResult:
    """Take one pg_dump (-Fc) into ``backups_dir`` and rotate old dumps.

    Never raises into the caller: any failure (spawn error, non-zero exit, IO)
    comes back as ``BackupResult(ok=False, error=...)`` with a short, secret-free
    message. On success the dump is fsynced and atomically renamed into place,
    old dumps beyond ``keep`` are deleted, and the result carries the path, byte
    size and delete count for the caller's message.

    The password travels ONLY through the PGPASSWORD environment variable; argv
    holds host/port/user/dbname and nothing secret, so ps never sees it.
    """
    conn = parse_dsn(dsn)
    try:
        os.makedirs(backups_dir, exist_ok=True)
    except OSError as exc:
        return BackupResult(ok=False, error=f"cannot create backups dir: {exc}")

    now = datetime.utcnow()
    final_name = backup_filename(now)
    final_path = os.path.join(backups_dir, final_name)
    part_path = os.path.join(backups_dir, final_name + _PART_SUFFIX)

    # Child env: inherit ours, add PGPASSWORD (the only place the secret lives).
    env = dict(os.environ)
    if conn.pgpassword is not None:
        env["PGPASSWORD"] = conn.pgpassword
    else:
        env.pop("PGPASSWORD", None)

    # -Fc custom format, -f writes straight to the .part file (so we never buffer
    # the whole dump in memory). No password anywhere on this argv.
    args = ["pg_dump", "-Fc", *conn.dump_args(), "-f", part_path]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
    except Exception as exc:  # spawn failure (pg_dump missing, etc.)
        _safe_unlink(part_path)
        return BackupResult(ok=False, error=f"pg_dump did not start: {exc}")

    if proc.returncode != 0:
        _safe_unlink(part_path)
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        # stderr may name host/user/db but never the password (it is not on argv
        # and pg_dump does not echo PGPASSWORD). Trim to keep logs bounded.
        return BackupResult(
            ok=False, error=f"pg_dump exit {proc.returncode}: {detail[:500]}"
        )

    # Durability: fsync the finished .part, then atomically rename into place so
    # a crash between here and there can never expose a half-written .dump.
    try:
        _fsync_file(part_path)
        os.replace(part_path, final_path)
        _fsync_dir(backups_dir)
        size = os.path.getsize(final_path)
    except OSError as exc:
        _safe_unlink(part_path)
        return BackupResult(ok=False, error=f"finalising dump failed: {exc}")

    # Rotation is best-effort: a delete failure must not fail an otherwise good
    # backup, so we count what we managed to remove and log the rest.
    deleted = 0
    try:
        names = os.listdir(backups_dir)
    except OSError:
        names = []
    for victim in rotation_victims(names, keep=keep):
        if _safe_unlink(os.path.join(backups_dir, victim)):
            deleted += 1

    return BackupResult(ok=True, path=final_path, size=size, deleted=deleted)


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    """fsync the directory so the rename itself is durable. Best-effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _safe_unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("backup: could not delete %s (%s)", os.path.basename(path), exc)
        return False
