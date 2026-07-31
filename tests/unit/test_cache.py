"""The cache layer: schema, migrations, state transitions, cascades, FTS."""

from __future__ import annotations

import sqlite3

import pytest

from davpunk.core import cache
from davpunk.core.cache import (
    ReadOnlyResourceError,
    SchemaTooNew,
    TaskConflictError,
    TaskNotFound,
)
from davpunk.models.calendar import calendar_id as compute_calendar_id
from davpunk.models.remote import Remote
from davpunk.models.task import Alarm, AlarmRelated, ReadOnlyReason, Status, SyncState, Task

# ---------------------------------------------------------------- connection


def test_all_four_pragmas_are_set(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connection_is_in_autocommit_mode(conn):
    """isolation_level=None is what makes tx() the only transaction boundary."""
    assert conn.isolation_level is None
    assert not conn.in_transaction


def test_foreign_keys_is_per_connection_not_per_database(db_path):
    """A second connection must set the pragma for itself; connect() does."""
    first = cache.open_db(db_path)
    second = cache.connect(db_path)
    try:
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        first.close()
        second.close()


# -------------------------------------------------------------------- tx()


def test_tx_commits_on_success(conn, remote_id):
    with cache.tx(conn):
        conn.execute("UPDATE remotes SET name = 'renamed' WHERE id = ?", (remote_id,))
    assert conn.execute("SELECT name FROM remotes WHERE id = ?", (remote_id,)).fetchone()[0] == (
        "renamed"
    )


def test_tx_rolls_back_on_exception(conn, remote_id):
    with pytest.raises(RuntimeError), cache.tx(conn):
        conn.execute("UPDATE remotes SET name = 'renamed' WHERE id = ?", (remote_id,))
        raise RuntimeError("boom")
    assert conn.execute("SELECT name FROM remotes WHERE id = ?", (remote_id,)).fetchone()[0] == (
        "Work"
    )


def test_tx_uses_begin_immediate_not_deferred(conn):
    """DEFERRED would deadlock two read-then-write transactions under WAL."""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        with cache.tx(conn):
            pass
    finally:
        conn.set_trace_callback(None)
    assert "BEGIN IMMEDIATE" in seen
    assert not any(s.startswith("BEGIN") and "IMMEDIATE" not in s for s in seen)


def test_nested_tx_asserts(conn):
    # The nesting is the point: tx() must refuse rather than silently produce a
    # savepoint-free inner "transaction" whose COMMIT ends the outer one.
    with cache.tx(conn):  # noqa: SIM117
        with pytest.raises(AssertionError, match="must not be nested"), cache.tx(conn):
            pass


def test_tx_rolls_back_on_keyboard_interrupt(conn, remote_id):
    """BaseException, not Exception: a Ctrl-C must not commit a half-write."""
    with pytest.raises(KeyboardInterrupt), cache.tx(conn):
        conn.execute("UPDATE remotes SET name = 'x' WHERE id = ?", (remote_id,))
        raise KeyboardInterrupt
    assert not conn.in_transaction
    assert conn.execute("SELECT name FROM remotes WHERE id = ?", (remote_id,)).fetchone()[0] == (
        "Work"
    )


# -------------------------------------------------------------- migrations


def test_migrate_reaches_the_current_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == cache.SCHEMA_VERSION


def test_migrate_is_idempotent(db_path):
    first = cache.open_db(db_path)
    first.close()
    second = cache.open_db(db_path)
    try:
        assert second.execute("PRAGMA user_version").fetchone()[0] == cache.SCHEMA_VERSION
    finally:
        second.close()


def test_a_newer_schema_is_refused_not_guessed_at(db_path):
    conn = cache.open_db(db_path)
    conn.execute(f"PRAGMA user_version = {cache.SCHEMA_VERSION + 5}")
    conn.close()
    conn = cache.connect(db_path)
    try:
        with pytest.raises(SchemaTooNew):
            cache.migrate(conn)
    finally:
        conn.close()


def test_partial_unique_index_permits_one_open_conflict_only(conn, synced_task):
    task_id = synced_task()
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "local", "remote", 'W/"2"', conn)
        cache.upsert_conflict(task_id, "local", "remote-newer", 'W/"3"', conn)
    rows = list(conn.execute("SELECT * FROM conflict_queue WHERE task_id = ?", (task_id,)))
    assert len(rows) == 1
    assert rows[0]["remote_raw_ics"] == "remote-newer"


def test_resolved_conflicts_do_not_block_a_new_one(conn, synced_task):
    task_id = synced_task()
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "l1", "r1", None, conn)
    conn.execute("UPDATE conflict_queue SET resolved = 1 WHERE task_id = ?", (task_id,))
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "l2", "r2", None, conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM conflict_queue WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 2
    )


# --------------------------------------------------------------- reconcile


def test_reconcile_upserts_and_marks_orphans(conn):
    cache.reconcile_remotes(
        [Remote(id="a", url="https://a.test/"), Remote(id="b", url="https://b.test/")], conn
    )
    cache.reconcile_remotes([Remote(id="a", url="https://a.test/")], conn)
    rows = {r["id"]: r["orphaned"] for r in conn.execute("SELECT id, orphaned FROM remotes")}
    assert rows == {"a": 0, "b": 1}


def test_reconcile_never_deletes_cached_tasks(conn, calendar_id, make_task):
    cache.create_task_local(make_task("keep-me"), conn)
    cache.reconcile_remotes([], conn)  # every remote gone from config
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_reconcile_is_idempotent_under_repetition(conn):
    remote = Remote(id="a", url="https://a.test/")
    for _ in range(3):
        cache.reconcile_remotes([remote], conn)
    assert conn.execute("SELECT COUNT(*) FROM remotes").fetchone()[0] == 1


def test_purge_remote_reports_and_destroys(conn, calendar_id, make_task, remote_id):
    cache.create_task_local(make_task("doomed"), conn)
    assert cache.purge_remote(remote_id, conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


# ---------------------------------------------------------------- calendars


def test_calendar_id_is_deterministic_across_a_rebuild(conn, remote_id):
    with cache.tx(conn):
        first = cache.upsert_calendar(remote_id, "/dav/x/", conn)
    conn.execute("DELETE FROM calendars")
    with cache.tx(conn):
        second = cache.upsert_calendar(remote_id, "/dav/x/", conn)
    assert first == second == compute_calendar_id(remote_id, "/dav/x/")


def test_upsert_calendar_does_not_clobber_ctag_with_null(conn, remote_id):
    with cache.tx(conn):
        cal = cache.upsert_calendar(remote_id, "/dav/x/", conn, ctag="ctag-1")
    with cache.tx(conn):
        cache.upsert_calendar(remote_id, "/dav/x/", conn, display_name="Renamed")
    row = conn.execute("SELECT * FROM calendars WHERE id = ?", (cal,)).fetchone()
    assert row["ctag"] == "ctag-1"
    assert row["display_name"] == "Renamed"


def test_unavailable_calendar_quarantines_but_never_deletes(conn, calendar_id, synced_task):
    task_id = synced_task()
    with cache.tx(conn):
        cache.mark_calendar_unavailable(calendar_id, conn)
    row = cache.get_task_row(task_id, conn)
    assert row["read_only_reason"] == ReadOnlyReason.CALENDAR_UNAVAILABLE.value
    assert (
        conn.execute("SELECT available FROM calendars WHERE id=?", (calendar_id,)).fetchone()[0]
        == 0
    )


def test_calendar_returning_lifts_only_its_own_quarantine(conn, calendar_id, synced_task):
    kept = synced_task("multi")
    conn.execute(
        "UPDATE tasks SET read_only_reason = ? WHERE id = ?",
        (ReadOnlyReason.MULTIPART.value, kept),
    )
    other = synced_task("plain")
    with cache.tx(conn):
        cache.mark_calendar_unavailable(calendar_id, conn)
        cache.mark_calendar_available(calendar_id, conn)
    assert cache.get_task_row(kept, conn)["read_only_reason"] == ReadOnlyReason.MULTIPART.value
    assert cache.get_task_row(other, conn)["read_only_reason"] is None


# ------------------------------------------------------------------- create


def test_create_task_local_is_new_and_queues_a_create(conn, make_task):
    task_id = cache.create_task_local(make_task("t1", summary="Write the HLD"), conn)
    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.NEW.value
    assert row["href"] == "t1.ics"
    assert row["etag"] is None

    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["change_type"] == "create"
    assert pending["base_etag"] is None


def test_create_applies_canonicalization(conn, make_task):
    task_id = cache.create_task_local(
        make_task("t1", status=Status.COMPLETED, percent_complete=30), conn
    )
    row = cache.get_task_row(task_id, conn)
    assert row["percent_complete"] == 100
    assert row["completed"] is not None


def test_create_stores_categories_and_alarms(conn, make_task):
    task = make_task("t1", due_value="20260731")
    task.categories = ["work", "urgent"]
    task.alarms = [Alarm(related=AlarmRelated.END, trigger_offset=-3600)]
    task_id = cache.create_task_local(task, conn)
    assert cache.load_categories(task_id, conn) == ["urgent", "work"]
    assert len(cache.load_alarms(task_id, conn)) == 1


def test_create_derives_the_coarse_sort_key(conn, make_task):
    task_id = cache.create_task_local(make_task("t1", due_value="20260731"), conn)
    assert cache.get_task_row(task_id, conn)["due"] == 1785456000


def test_create_requires_a_calendar(conn, make_task):
    task = make_task("t1")
    task.calendar_id = None
    with pytest.raises(cache.CacheError):
        cache.create_task_local(task, conn)


# ------------------------------------------------------------------- update


def test_update_moves_clean_to_dirty_and_captures_base_etag(conn, synced_task):
    task_id = synced_task(etag='W/"v1"')
    cache.update_task_optimistic(task_id, {"summary": "changed"}, conn)

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.DIRTY.value
    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["change_type"] == "update"
    assert pending["base_etag"] == 'W/"v1"'


def test_a_second_edit_keeps_the_original_base_etag(conn, synced_task):
    """base_etag is the ETag at the FIRST edit after a clean sync."""
    task_id = synced_task(etag='W/"v1"')
    cache.update_task_optimistic(task_id, {"summary": "one"}, conn)
    conn.execute("UPDATE tasks SET etag = 'W/\"v9\"' WHERE id = ?", (task_id,))
    cache.update_task_optimistic(task_id, {"summary": "two"}, conn)

    assert (
        conn.execute(
            "SELECT base_etag FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 'W/"v1"'
    )


def test_editing_a_new_task_keeps_it_new_and_a_create(conn, make_task):
    """'new' never decays to 'dirty'."""
    task_id = cache.create_task_local(make_task("t1"), conn)
    cache.update_task_optimistic(task_id, {"summary": "renamed"}, conn)
    cache.update_task_optimistic(task_id, {"summary": "renamed twice"}, conn)

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.NEW.value
    assert (
        conn.execute(
            "SELECT change_type FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "create"
    )


def test_update_rejects_a_conflicted_task(conn, synced_task):
    task_id = synced_task()
    conn.execute("UPDATE tasks SET sync_state = 'conflict' WHERE id = ?", (task_id,))
    with pytest.raises(TaskConflictError):
        cache.update_task_optimistic(task_id, {"summary": "nope"}, conn)


@pytest.mark.parametrize(
    "reason",
    [ReadOnlyReason.MULTIPART, ReadOnlyReason.OVERSIZE, ReadOnlyReason.CALENDAR_UNAVAILABLE],
)
def test_update_rejects_every_read_only_reason(conn, synced_task, reason):
    task_id = synced_task()
    conn.execute("UPDATE tasks SET read_only_reason = ? WHERE id = ?", (reason.value, task_id))
    with pytest.raises(ReadOnlyResourceError) as excinfo:
        cache.update_task_optimistic(task_id, {"summary": "nope"}, conn)
    assert excinfo.value.reason == reason.value


def test_update_resets_the_backoff(conn, synced_task):
    """A user edit is an explicit retry."""
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "one"}, conn)
    with cache.tx(conn):
        cache.record_failure(task_id, "500 Server Error", conn)
    cache.update_task_optimistic(task_id, {"summary": "two"}, conn)

    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["attempts"] == 0
    assert pending["next_attempt_at"] == 0
    assert pending["last_error"] is None


def test_update_rejects_columns_it_does_not_own(conn, synced_task):
    task_id = synced_task()
    for column in ("etag", "sync_state", "calendar_id", "href", "uid"):
        with pytest.raises(cache.CacheError):
            cache.update_task_optimistic(task_id, {column: "x"}, conn)


def test_update_does_not_mutate_the_callers_dict(conn, synced_task):
    task_id = synced_task()
    fields = {"summary": "x", "categories": ["a"]}
    cache.update_task_optimistic(task_id, fields, conn)
    assert fields == {"summary": "x", "categories": ["a"]}


def test_status_less_update_does_not_clear_completed(conn, synced_task):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"status": Status.COMPLETED}, conn)
    completed = cache.get_task_row(task_id, conn)["completed"]
    cache.update_task_optimistic(task_id, {"summary": "retitled"}, conn)
    assert cache.get_task_row(task_id, conn)["completed"] == completed


def test_update_of_a_missing_task_raises(conn):
    with pytest.raises(TaskNotFound):
        cache.update_task_optimistic("no-such-id", {"summary": "x"}, conn)


def test_rewrite_alarms_replaces_the_whole_set(conn, synced_task):
    task_id = synced_task(due_value="20260731")
    cache.update_task_optimistic(
        task_id,
        {"alarms": [{"related": "END", "trigger_offset": -900}]},
        conn,
        rewrite_alarms=True,
    )
    assert [a.trigger_offset for a in cache.load_alarms(task_id, conn)] == [-900]

    cache.update_task_optimistic(task_id, {"alarms": []}, conn, rewrite_alarms=True)
    assert cache.load_alarms(task_id, conn) == []


def test_alarms_survive_an_edit_that_does_not_rewrite_them(conn, synced_task):
    task_id = synced_task(due_value="20260731")
    cache.update_task_optimistic(
        task_id, {"alarms": [{"related": "END", "trigger_offset": -900}]}, conn, rewrite_alarms=True
    )
    cache.update_task_optimistic(task_id, {"summary": "retitled"}, conn)
    assert len(cache.load_alarms(task_id, conn)) == 1


# ------------------------------------------------------------------- delete


def test_deleting_a_never_synced_task_purges_it_with_no_tombstone(conn, make_task):
    """No network call and no tombstone — it never existed remotely."""
    task_id = cache.create_task_local(make_task("t1"), conn)
    cache.delete_task_local(task_id, conn)

    assert cache.get_task_row(task_id, conn) is None
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0


def test_deleting_a_synced_task_queues_a_guarded_delete(conn, synced_task):
    task_id = synced_task(etag='W/"v1"')
    cache.delete_task_local(task_id, conn)

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.PENDING_DELETE.value
    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["change_type"] == "delete"
    assert pending["base_etag"] == 'W/"v1"'


def test_deleting_a_dirty_task_reuses_its_base_etag(conn, synced_task):
    task_id = synced_task(etag='W/"v1"')
    cache.update_task_optimistic(task_id, {"summary": "edited"}, conn)
    conn.execute("UPDATE tasks SET etag = 'W/\"v9\"' WHERE id = ?", (task_id,))
    cache.delete_task_local(task_id, conn)

    assert (
        conn.execute(
            "SELECT base_etag FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 'W/"v1"'
    )


def test_deleting_a_conflicted_task_resolves_it_unconditionally(conn, synced_task):
    """An implicit delete_anyway: the user has decided."""
    task_id = synced_task(etag='W/"v1"')
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "local", "remote", 'W/"v2"', conn)
    conn.execute("UPDATE tasks SET sync_state = 'conflict' WHERE id = ?", (task_id,))

    cache.delete_task_local(task_id, conn)

    assert (
        conn.execute(
            "SELECT resolved FROM conflict_queue WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT base_etag FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        is None
    )


def test_delete_is_permitted_for_a_read_only_task(conn, synced_task):
    """DELETE removes the whole resource; no serialization is involved."""
    task_id = synced_task()
    conn.execute(
        "UPDATE tasks SET read_only_reason = ? WHERE id = ?",
        (ReadOnlyReason.MULTIPART.value, task_id),
    )
    cache.delete_task_local(task_id, conn)
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.PENDING_DELETE.value


def test_finalize_delete_tombstones_and_cascades(conn, synced_task):
    task_id = synced_task("gone")
    conn.execute("INSERT INTO task_categories (task_id, category) VALUES (?, 'x')", (task_id,))
    with cache.tx(conn):
        cache.finalize_delete(task_id, conn)

    assert cache.get_task_row(task_id, conn) is None
    assert conn.execute("SELECT COUNT(*) FROM task_categories").fetchone()[0] == 0
    tomb = conn.execute("SELECT * FROM tombstones").fetchone()
    assert tomb["uid"] == "gone"


# ---------------------------------------------------------------- subtasks


def test_deleting_a_parent_promotes_children_to_root(conn, synced_task):
    parent = synced_task("parent")
    child = synced_task("child", parent_uid="parent")
    cache.delete_task_local(parent, conn)

    row = cache.get_task_row(child, conn)
    assert row["parent_uid"] is None
    assert row["sync_state"] == SyncState.DIRTY.value
    assert (
        conn.execute(
            "SELECT change_type FROM pending_changes WHERE task_id = ?", (child,)
        ).fetchone()[0]
        == "update"
    )


def test_children_are_never_cascade_deleted(conn, synced_task):
    parent = synced_task("parent")
    child = synced_task("child", parent_uid="parent")
    cache.delete_task_local(parent, conn)
    assert cache.get_task_row(child, conn) is not None


def test_parent_resolution_is_scoped_to_one_calendar(conn, synced_task, other_calendar_id):
    """uid is deliberately non-unique across calendars."""
    parent = synced_task("shared-uid")
    stray = synced_task("child-elsewhere", parent_uid="shared-uid")
    conn.execute("UPDATE tasks SET calendar_id = ? WHERE id = ?", (other_calendar_id, stray))
    assert cache.children_of(parent, conn) == []


def test_subtask_cycle_does_not_hang_the_walk(conn, synced_task):
    a = synced_task("a")
    synced_task("b", parent_uid="a")
    conn.execute("UPDATE tasks SET parent_uid = 'b' WHERE id = ?", (a,))
    assert len(cache._descendants(a, conn)) == 1


# ------------------------------------------------------- apply_server_version


def test_apply_server_version_writes_over_a_clean_row(conn, synced_task):
    task_id = synced_task(etag='W/"v1"')
    incoming = Task(uid="srv-1", summary="from server", href="srv-1.ics", raw_ics="X")
    assert cache.apply_server_version(task_id, incoming, 'W/"v2"', conn) is True

    row = cache.get_task_row(task_id, conn)
    assert row["summary"] == "from server"
    assert row["etag"] == 'W/"v2"'
    assert row["sync_state"] == SyncState.CLEAN.value


def test_apply_server_version_refuses_a_dirty_row(conn, synced_task):
    """The rowcount = 0 path: the editor got there first."""
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "mine"}, conn)

    incoming = Task(uid="srv-1", summary="theirs")
    assert cache.apply_server_version(task_id, incoming, 'W/"v2"', conn) is False
    assert cache.get_task_row(task_id, conn)["summary"] == "mine"


def test_apply_server_version_never_changes_the_calendar(conn, synced_task, other_calendar_id):
    """A pull must not relocate a row; only move_task_local retargets."""
    task_id = synced_task()
    original = cache.get_task_row(task_id, conn)["calendar_id"]
    incoming = Task(uid="srv-1", calendar_id=other_calendar_id, summary="s")
    cache.apply_server_version(task_id, incoming, "e", conn)
    row = cache.get_task_row(task_id, conn)
    assert row["calendar_id"] == original
    assert row["href"] == "srv-1.ics"  # href is identity too, not content


def test_apply_server_version_replaces_child_tables(conn, synced_task):
    task_id = synced_task()
    conn.execute("INSERT INTO task_categories VALUES (?, 'stale')", (task_id,))
    incoming = Task(uid="srv-1", summary="s", categories=["fresh"])
    incoming.alarms = [Alarm(trigger_offset=-60)]
    cache.apply_server_version(task_id, incoming, "e", conn)

    assert cache.load_categories(task_id, conn) == ["fresh"]
    assert len(cache.load_alarms(task_id, conn)) == 1


# ------------------------------------------------------------------ backoff


def test_backoff_grows_and_blocks_after_eight_attempts(conn, synced_task, frozen_now):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)

    for attempt in range(1, cache.MAX_ATTEMPTS + 1):
        with cache.tx(conn):
            cache.record_failure(task_id, "503", conn)
        row = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
        assert row["attempts"] == attempt
        if attempt < cache.MAX_ATTEMPTS:
            assert row["blocked"] == 0
            assert row["next_attempt_at"] == frozen_now.value + min(
                60 * 2**attempt, cache.MAX_BACKOFF
            )
    assert row["blocked"] == 1
    assert row["last_error"] == "503"


@pytest.mark.parametrize("status", cache.NON_RETRYABLE_STATUS)
def test_non_retryable_statuses_block_immediately(conn, synced_task, status):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    with cache.tx(conn):
        cache.record_failure(task_id, f"{status}", conn, status=status)
    row = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert row["blocked"] == 1
    assert row["attempts"] == 1


def test_backoff_is_capped(conn, synced_task, frozen_now):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    conn.execute("UPDATE pending_changes SET attempts = 20 WHERE task_id = ?", (task_id,))
    with cache.tx(conn):
        cache.record_failure(task_id, "503", conn)
    row = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert row["next_attempt_at"] == frozen_now.value + cache.MAX_BACKOFF


def test_a_far_future_next_attempt_is_treated_as_ready(conn, calendar_id, synced_task, frozen_now):
    """Clock-skew clamp: a backward jump must not strand a change."""
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    conn.execute(
        "UPDATE pending_changes SET next_attempt_at = ? WHERE task_id = ?",
        (frozen_now.value + 10 * cache.MAX_BACKOFF, task_id),
    )
    assert [r["task_id"] for r in cache.ready_pending_changes(calendar_id, conn)] == [task_id]


def test_a_pending_change_within_its_backoff_is_not_ready(
    conn, calendar_id, synced_task, frozen_now
):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    with cache.tx(conn):
        cache.record_failure(task_id, "503", conn)
    assert cache.ready_pending_changes(calendar_id, conn) == []
    frozen_now.advance(cache.MAX_BACKOFF)
    assert len(cache.ready_pending_changes(calendar_id, conn)) == 1


def test_blocked_changes_are_not_scanned(conn, calendar_id, synced_task):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    conn.execute("UPDATE pending_changes SET blocked = 1 WHERE task_id = ?", (task_id,))
    assert cache.ready_pending_changes(calendar_id, conn) == []
    assert cache.blocked_count(conn) == 1


def test_conflicted_tasks_are_never_pushed(conn, calendar_id, synced_task):
    """No doomed attempts."""
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    conn.execute("UPDATE tasks SET sync_state = 'conflict' WHERE id = ?", (task_id,))
    assert cache.ready_pending_changes(calendar_id, conn) == []


def test_read_only_tasks_push_only_delete_and_move(conn, calendar_id, synced_task):
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    conn.execute(
        "UPDATE tasks SET read_only_reason = ? WHERE id = ?",
        (ReadOnlyReason.MULTIPART.value, task_id),
    )
    assert cache.ready_pending_changes(calendar_id, conn) == []

    conn.execute("UPDATE pending_changes SET change_type = 'delete' WHERE task_id = ?", (task_id,))
    assert len(cache.ready_pending_changes(calendar_id, conn)) == 1

    conn.execute("UPDATE pending_changes SET change_type = 'move' WHERE task_id = ?", (task_id,))
    assert len(cache.ready_pending_changes(calendar_id, conn)) == 1


# --------------------------------------------------------------- tombstones


def test_tombstones_match_on_calendar_and_uid_not_href(conn, calendar_id):
    """A stale client replaying a delete reuses the UID, not the href."""
    with cache.tx(conn):
        cache.add_tombstone(calendar_id, "original.ics", "uid-1", conn)
    assert cache.find_tombstone(calendar_id, "uid-1", conn) is not None


def test_a_tombstone_from_another_calendar_does_not_match(conn, calendar_id, other_calendar_id):
    with cache.tx(conn):
        cache.add_tombstone(calendar_id, "x.ics", "uid-1", conn)
    assert cache.find_tombstone(other_calendar_id, "uid-1", conn) is None


def test_expired_tombstones_stop_matching(conn, calendar_id, frozen_now):
    with cache.tx(conn):
        cache.add_tombstone(calendar_id, "x.ics", "uid-1", conn)
    frozen_now.advance(cache.TOMBSTONE_TTL + 1)
    assert cache.find_tombstone(calendar_id, "uid-1", conn) is None


def test_expire_prunes_all_three_ledgers(conn, calendar_id, synced_task, frozen_now):
    task_id = synced_task()
    with cache.tx(conn):
        cache.add_tombstone(calendar_id, "x.ics", "u", conn)
        cache.upsert_conflict(task_id, "l", "r", None, conn)
        conn.execute(
            "INSERT INTO alarm_fires (task_id, trigger_at, fired_at) VALUES (?,?,?)",
            (task_id, 1, cache.now()),
        )
    conn.execute("UPDATE conflict_queue SET resolved = 1")

    frozen_now.advance(cache.RESOLVED_CONFLICT_TTL + 1)
    counts = cache.expire(conn)
    assert counts == {"tombstones": 1, "alarm_fires": 1, "conflicts": 1}


# ---------------------------------------------------------------------- FTS


def test_fts_indexes_on_insert(conn, make_task):
    cache.create_task_local(make_task("t1", summary="unmistakable zorblat"), conn)
    assert len(cache.search_tasks("zorblat", conn)) == 1


def test_fts_follows_a_summary_update(conn, make_task):
    task_id = cache.create_task_local(make_task("t1", summary="before"), conn)
    cache.update_task_optimistic(task_id, {"summary": "zorblat"}, conn)
    assert len(cache.search_tasks("zorblat", conn)) == 1
    assert cache.search_tasks("before", conn) == []


def test_fts_follows_a_delete(conn, synced_task):
    task_id = synced_task("s", summary="zorblat")
    with cache.tx(conn):
        cache.finalize_delete(task_id, conn)
    assert cache.search_tasks("zorblat", conn) == []


def test_fts_trigger_does_not_fire_on_a_sync_state_write(conn, make_task):
    """A bare AFTER UPDATE would rewrite the index every sync cycle.

    Observed through ``dbstat``-free means: the index content must be unchanged
    and the row must still be findable after many metadata-only writes.
    """
    task_id = cache.create_task_local(make_task("t1", summary="zorblat"), conn)
    for i in range(50):
        conn.execute(
            "UPDATE tasks SET etag = ?, last_modified = ?, sync_state = 'clean' WHERE id = ?",
            (f"e{i}", i, task_id),
        )
    assert len(cache.search_tasks("zorblat", conn)) == 1
    conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('integrity-check')")


def test_fts_search_finds_descriptions(conn, make_task):
    cache.create_task_local(make_task("t1", description="mentions zorblat inside"), conn)
    assert len(cache.search_tasks("zorblat", conn)) == 1


def test_fts_rebuild_recovers_a_cleared_index(conn, make_task):
    cache.create_task_local(make_task("t1", summary="zorblat"), conn)
    conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('delete-all')")
    assert cache.search_tasks("zorblat", conn) == []
    cache.fts_rebuild(conn)
    assert len(cache.search_tasks("zorblat", conn)) == 1


# --------------------------------------------------------------- misc reads


def test_find_by_uid_returns_every_candidate(conn, calendar_id, synced_task):
    """uid is not unique within a calendar; a bad server can duplicate it."""
    first = synced_task("dup")
    second = synced_task("dup-other")
    conn.execute("UPDATE tasks SET uid = 'dup' WHERE id = ?", (second,))
    assert {r["id"] for r in cache.find_by_uid(calendar_id, "dup", conn)} == {first, second}


def test_href_is_unique_per_calendar(conn, calendar_id, synced_task):
    synced_task("a")
    with pytest.raises(sqlite3.IntegrityError):
        synced_task("a")


def test_get_task_round_trips_children(conn, make_task):
    task = make_task("t1", due_value="20260731")
    task.categories = ["x"]
    task.alarms = [Alarm(trigger_offset=-60)]
    task_id = cache.create_task_local(task, conn)

    loaded = cache.get_task(task_id, conn)
    assert loaded.categories == ["x"]
    assert loaded.alarms[0].trigger_offset == -60
    assert loaded.due_value == "20260731"


def test_close_db_checkpoints_without_raising(db_path):
    conn = cache.open_db(db_path)
    cache.close_db(conn)
