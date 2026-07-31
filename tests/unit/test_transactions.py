"""Atomicity under interleaving.

These are the tests that justify ``tx()`` existing at all.  Under
``isolation_level=None`` every bare statement commits on its own, so a
read-then-write helper without an explicit transaction is a race, and
``with conn:`` — the idiom that *looks* like a transaction — commits nothing.
"""

from __future__ import annotations

import threading
import time

import pytest

from davpunk.core import cache
from davpunk.models.task import SyncState, Task

# ------------------------------------------------ the premise: with conn: lies


def test_with_conn_commits_nothing_on_this_connection(conn, remote_id):
    """The bug tx() exists to prevent, demonstrated directly.

    In autocommit mode sqlite3 issues no implicit BEGIN, so the UPDATE inside
    ``with conn:`` has already committed and the rollback on exception is a
    no-op.  Anyone reaching for ``with conn:`` gets no atomicity at all.
    """
    with pytest.raises(RuntimeError):  # noqa: SIM117 - the nesting is the point
        with conn:
            conn.execute("UPDATE remotes SET name = 'leaked' WHERE id = ?", (remote_id,))
            raise RuntimeError("boom")

    assert conn.execute("SELECT name FROM remotes WHERE id = ?", (remote_id,)).fetchone()[0] == (
        "leaked"
    )


def test_tx_in_the_same_situation_rolls_back(conn, remote_id):
    with pytest.raises(RuntimeError), cache.tx(conn):
        conn.execute("UPDATE remotes SET name = 'leaked' WHERE id = ?", (remote_id,))
        raise RuntimeError("boom")

    assert conn.execute("SELECT name FROM remotes WHERE id = ?", (remote_id,)).fetchone()[0] == (
        "Work"
    )


# ------------------------------------ update_task_optimistic under a sync write


def test_update_is_atomic_against_an_interleaved_sync_write(db_path, monkeypatch):
    """A sync-role write must not land between the read and the write.

    ``update_task_optimistic`` reads ``etag`` and writes ``base_etag`` from it.
    If the two were separate transactions, a sync-role ETag refresh landing in
    between would make ``base_etag`` describe a version the local edit was never
    based on — and the resulting ``If-Match`` would silently overwrite a server
    change instead of conflicting.
    """
    from davpunk.models.remote import Remote

    editor = cache.open_db(db_path)
    cache.reconcile_remotes([Remote(id="work", url="https://x.test/")], editor)
    with cache.tx(editor):
        cal = cache.upsert_calendar("work", "/c/", editor)
    task = Task(uid="t", calendar_id=cal, href="t.ics", summary="s")
    with cache.tx(editor):
        task_id = cache.insert_server_task(task, 'W/"v1"', editor)

    editor_is_mid_transaction = threading.Event()
    syncer_is_attempting = threading.Event()

    def sync_role_writes():
        # Its own connection — connections are never shared across threads.
        own = cache.connect(db_path)
        try:
            editor_is_mid_transaction.wait(5)
            syncer_is_attempting.set()
            with cache.tx(own):  # blocks until the editor commits
                own.execute("UPDATE tasks SET etag = 'W/\"v2\"' WHERE id = ?", (task_id,))
        finally:
            own.close()

    # Wedge the sync-role write into the exact gap between the editor's read of
    # `etag` and its write of `base_etag`.
    original = cache.canonicalize_fields

    def hook(fields):
        editor_is_mid_transaction.set()
        syncer_is_attempting.wait(5)
        time.sleep(0.2)  # let the syncer actually reach BEGIN IMMEDIATE
        return original(fields)

    monkeypatch.setattr(cache, "canonicalize_fields", hook)

    thread = threading.Thread(target=sync_role_writes)
    thread.start()
    try:
        cache.update_task_optimistic(task_id, {"summary": "edited"}, editor)
    finally:
        thread.join(10)

    # The editor held the write lock for the whole helper, so base_etag is the
    # value it read — never the one the syncer wrote a moment later.  Without
    # tx(), base_etag here would be v2: an ETag the local edit was never based
    # on, and an If-Match that would silently overwrite the server's change.
    base_etag = editor.execute(
        "SELECT base_etag FROM pending_changes WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    assert base_etag == 'W/"v1"'
    assert editor.execute("SELECT etag FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == (
        'W/"v2"'
    )

    editor.close()


def test_a_failed_update_leaves_no_pending_row(conn, synced_task, monkeypatch):
    """The whole helper is one transaction: a late failure undoes the early write."""
    task_id = synced_task()

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated failure after the tasks UPDATE")

    monkeypatch.setattr(cache, "_replace_categories", explode)

    with pytest.raises(RuntimeError):
        cache.update_task_optimistic(task_id, {"summary": "x", "categories": ["a"]}, conn)

    assert cache.get_task_row(task_id, conn)["summary"] != "x"
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value


# ------------------------- apply_server_version: guard + child rewrite together


def test_apply_server_version_guard_and_child_rewrite_are_one_transaction(
    conn, synced_task, monkeypatch
):
    """All-or-nothing, or the row and its categories describe different versions."""
    task_id = synced_task()
    conn.execute("INSERT INTO task_categories VALUES (?, 'original')", (task_id,))

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated failure between the guard and the alarms")

    monkeypatch.setattr(cache, "_replace_alarms", explode)

    incoming = Task(uid="srv-1", summary="from server", categories=["new"])
    with pytest.raises(RuntimeError):
        cache.apply_server_version(task_id, incoming, 'W/"v2"', conn)

    row = cache.get_task_row(task_id, conn)
    assert row["summary"] != "from server"
    assert row["etag"] != 'W/"v2"'
    assert cache.load_categories(task_id, conn) == ["original"]


def test_the_clean_guard_is_evaluated_inside_the_transaction(conn, synced_task):
    """rowcount == 0 must mean "someone else wrote", not "we clobbered them"."""
    task_id = synced_task()
    cache.update_task_optimistic(task_id, {"summary": "local edit"}, conn)
    conn.execute("INSERT INTO task_categories VALUES (?, 'local')", (task_id,))

    incoming = Task(uid="srv-1", summary="server version", categories=["server"])
    assert cache.apply_server_version(task_id, incoming, 'W/"v2"', conn) is False

    # Neither the row nor its child tables moved.
    assert cache.get_task_row(task_id, conn)["summary"] == "local edit"
    assert cache.load_categories(task_id, conn) == ["local"]


# ------------------------------------------------------------- move atomicity


def test_a_failed_move_leaves_neither_tombstone_nor_pending_row(
    conn, synced_task, other_calendar_id, monkeypatch
):
    task_id = synced_task()
    calls = {"n": 0}
    original = cache._move_one

    def fail_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure moving a child")
        return original(*args, **kwargs)

    synced_task("child", parent_uid="srv-1")
    monkeypatch.setattr(cache, "_move_one", fail_on_second)

    with pytest.raises(RuntimeError):
        cache.move_task_local(task_id, other_calendar_id, conn)

    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
    assert cache.get_task_row(task_id, conn)["calendar_id"] != other_calendar_id


# ------------------------------------------------------------ writers queue up


def test_two_immediate_writers_queue_rather_than_deadlock(db_path):
    """The reason for IMMEDIATE: two DEFERRED read-then-write transactions
    deadlock on the upgrade, and busy_timeout cannot break the tie because
    neither side can make progress.  IMMEDIATE makes the second writer queue.

    The worker opens its **own** connection: connections are never shared
    across threads and check_same_thread=False is never used.
    """
    from davpunk.models.remote import Remote

    main = cache.open_db(db_path)
    cache.reconcile_remotes([Remote(id="work", url="https://x.test/")], main)

    order: list[str] = []
    holder_has_lock = threading.Event()
    holder_may_finish = threading.Event()

    def hold_the_write_lock():
        own = cache.connect(db_path)
        try:
            with cache.tx(own):
                own.execute("UPDATE remotes SET name = 'a' WHERE id = 'work'")
                order.append("holder-in")
                holder_has_lock.set()
                holder_may_finish.wait(5)
            order.append("holder-out")
        finally:
            own.close()

    thread = threading.Thread(target=hold_the_write_lock)
    thread.start()
    assert holder_has_lock.wait(5)

    # The holder still owns the write lock.  This BEGIN IMMEDIATE therefore has
    # to wait; it does not deadlock, and it does not fail, because busy_timeout
    # can drain a queue even though it cannot break a deadlock.
    holder_may_finish.set()
    with cache.tx(main):
        main.execute("UPDATE remotes SET name = 'b' WHERE id = 'work'")
    order.append("waiter-out")
    thread.join(5)

    assert order[:2] == ["holder-in", "holder-out"]
    assert order[-1] == "waiter-out"
    assert main.execute("SELECT name FROM remotes WHERE id = 'work'").fetchone()[0] == "b"
    main.close()


def test_begin_immediate_refuses_rather_than_corrupting_when_the_lock_is_held(db_path):
    """A BEGIN IMMEDIATE that cannot take the lock raises DatabaseBusy.

    The sync role retries next cycle; the editor role surfaces "database busy".
    Neither proceeds with a half-guarded write.
    """
    holder = cache.open_db(db_path)
    contender = cache.connect(db_path)
    contender.execute("PRAGMA busy_timeout = 0")  # do not wait; fail now

    with cache.tx(holder):
        holder.execute("PRAGMA user_version")
        with pytest.raises(cache.DatabaseBusy), cache.tx(contender):
            pass
        assert not contender.in_transaction

    holder.close()
    contender.close()
