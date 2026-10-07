"""Technical logs are kept LOG_RETENTION_DAYS days (PRIVACY.md, Retention)."""

import logging
import os

import core


def test_legacy_size_rotated_logs_past_the_window_are_deleted(tmp_path):
    now = 1_000_000_000.0
    old = tmp_path / "yasuho.log.3"
    recent = tmp_path / "yasuho.log.1"
    current = tmp_path / "yasuho.log"
    dated = tmp_path / "yasuho.log.2026-10-01"
    other = tmp_path / "notes.txt"
    for path in (old, recent, current, dated, other):
        path.write_text("x")
    too_old = now - (core.LOG_RETENTION_DAYS + 1) * 86400
    for path in (old, current, dated, other):
        os.utime(path, (too_old, too_old))
    os.utime(recent, (now, now))

    removed = core._prune_legacy_logs(str(tmp_path), now=now)

    assert removed == 1
    assert not old.exists()
    # Only the numbered legacy files are ever touched here.
    assert recent.exists() and current.exists() and dated.exists() and other.exists()


def test_the_file_handler_rotates_daily_and_keeps_the_retention_window(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(core, "__file__", str(tmp_path / "core.py"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        core._attach_file_logging()
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        handler = added[0]
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == core.LOG_RETENTION_DAYS
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()


def test_the_retention_window_matches_the_privacy_policy():
    with open(os.path.join(os.path.dirname(core.__file__), "PRIVACY.md"), encoding="utf-8") as fp:
        policy = fp.read()
    assert "{} days".format(core.LOG_RETENTION_DAYS) in policy
