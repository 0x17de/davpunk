"""Corrupt-DB triage: dump unsent intent, move aside, rebuild."""

from __future__ import annotations

import json
import sqlite3

from davpunk.core import cache, recovery
from davpunk.models.calendar import calendar_id as compute_calendar_id
from davpunk.models.remote import Remote
from davpunk.models.task import Task


def _populated_db(db_path):
    conn = cache.open_db(db_path)
    cache.reconcile_remotes([Remote(id="work", url="https://x.test/")], conn)
    with cache.tx(conn):
        cal = cache.upsert_calendar("work", "/dav/work/tasks/", conn)
    with cache.tx(conn):
        synced = cache.insert_server_task(
            Task(uid="synced", calendar_id=cal, href="synced.ics", summary="Synced"),
            'W/"v1"',
            conn,
        )
    cache.update_task_optimistic(synced, {"summary": "Edited but unsent"}, conn)
    with cache.tx(conn):
        cache.upsert_conflict(synced, "local-ics", "remote-ics", 'W/"v2"', conn)
    cache.close_db(conn)
    return cal


def _corrupt(db_path):
    """Overwrite the header so SQLite refuses the file outright."""
    with db_path.open("r+b") as handle:
        handle.write(b"this is not a sqlite database at all\x00")


def test_a_healthy_database_opens_with_no_report(db_path):
    conn, report = cache.open_or_recover(db_path)
    assert report is None
    conn.close()


def test_a_corrupt_database_is_moved_aside_and_rebuilt(db_path):
    _populated_db(db_path)
    _corrupt(db_path)

    conn, report = cache.open_or_recover(db_path)
    try:
        assert report is not None
        assert report.corrupt_path.exists()
        assert not report.corrupt_path.name.endswith(".db")
        # A fresh database at the current schema version.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == cache.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_unsent_intent_is_dumped_before_the_file_moves(db_path):
    _populated_db(db_path)
    # Corrupt a page in the middle rather than the header, so a read-only
    # connection can still see the schema and most rows.
    size = db_path.stat().st_size
    with db_path.open("r+b") as handle:
        handle.seek(size - 512)
        handle.write(b"\xde\xad\xbe\xef" * 64)

    conn, report = cache.open_or_recover(db_path)
    conn.close()

    if report is None:
        # SQLite tolerated the damage; nothing to recover and nothing to assert.
        return
    if report.dump_path is None:
        assert report.lost
        return

    dump = json.loads(report.dump_path.read_text())
    assert dump["schema"] == "davpunk-recovery-1"
    assert report.dump_path.stat().st_mode & 0o077 == 0


def test_dump_captures_pending_changes_and_open_conflicts(db_path):
    _populated_db(db_path)
    dump, error = recovery._dump_unsent_intent(db_path)

    assert error is None
    assert len(dump["pending_changes"]) == 1
    assert dump["pending_changes"][0]["change_type"] == "update"
    assert dump["pending_changes"][0]["uid"] == "synced"
    assert len(dump["conflicts"]) == 1
    assert dump["conflicts"][0]["local_raw_ics"] == "local-ics"


def test_an_unreadable_database_reports_loss_rather_than_raising(db_path):
    _populated_db(db_path)
    _corrupt(db_path)

    report = recovery.recover(db_path)
    assert report.lost is True
    assert report.dump_path is None
    assert "moved aside" in report.summary()
    assert report.corrupt_path.exists()


def test_wal_and_shm_siblings_move_with_the_database(db_path):
    _populated_db(db_path)
    db_path.with_name(db_path.name + "-wal").write_bytes(b"stale wal")
    db_path.with_name(db_path.name + "-shm").write_bytes(b"stale shm")

    report = recovery.recover(db_path)

    assert not db_path.with_name(db_path.name + "-wal").exists()
    assert report.corrupt_path.with_name(report.corrupt_path.name + "-wal").exists()


def test_task_refs_still_resolve_after_a_rebuild(db_path):
    """The point of a deterministic calendar_id.

    A rebuild is a normal event; an agent holding ``<calendar_id>:<uid>`` must
    not have its references invalidated by one.
    """
    cal_before = _populated_db(db_path)
    _corrupt(db_path)

    conn, _ = cache.open_or_recover(db_path)
    try:
        cache.reconcile_remotes([Remote(id="work", url="https://x.test/")], conn)
        with cache.tx(conn):
            cal_after = cache.upsert_calendar("work", "/dav/work/tasks/", conn)
        assert cal_after == cal_before == compute_calendar_id("work", "/dav/work/tasks/")
    finally:
        conn.close()


def test_recovery_dir_is_owner_only(db_path):
    _populated_db(db_path)
    report = recovery.recover(db_path)
    if report.dump_path is not None:
        assert report.dump_path.parent.stat().st_mode & 0o077 == 0


def test_summary_names_the_files_the_user_needs(db_path):
    _populated_db(db_path)
    report = recovery.recover(db_path)
    text = report.summary()
    assert report.corrupt_path.name in text
    if report.dump_path:
        assert str(report.dump_path) in text
        assert "1 unsent local edit" in text


def test_open_db_still_raises_on_corruption(db_path):
    """open_db is the raw path; open_or_recover is the one that triages."""
    _populated_db(db_path)
    _corrupt(db_path)
    try:
        cache.open_db(db_path)
    except sqlite3.DatabaseError:
        return
    raise AssertionError("open_db swallowed a corrupt database")
