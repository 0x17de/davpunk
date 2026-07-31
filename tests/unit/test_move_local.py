"""``move_task_local`` — the local half of a cross-calendar move.

The network half (two-stage PUT-before-DELETE) lives in ``test_move.py``.
"""

from __future__ import annotations

import pytest

from davpunk.core import cache
from davpunk.core.cache import ReadOnlyResourceError, TaskConflictError
from davpunk.models.task import ReadOnlyReason, SyncState


def test_moving_a_synced_task_retargets_it_and_queues_a_move(
    conn, synced_task, calendar_id, other_calendar_id
):
    task_id = synced_task("m1", etag='W/"v1"')
    cache.move_task_local(task_id, other_calendar_id, conn)

    row = cache.get_task_row(task_id, conn)
    assert row["calendar_id"] == other_calendar_id
    assert row["href"] == "m1.ics"
    assert row["etag"] is None
    # The target collection has no such resource, so If-None-Match:* is right.
    assert row["sync_state"] == SyncState.NEW.value

    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["change_type"] == "move"
    assert pending["base_etag"] == 'W/"v1"'
    assert pending["source_calendar_id"] == calendar_id
    assert pending["source_href"] == "m1.ics"
    assert pending["move_stage"] == 0


def test_a_move_pre_writes_a_tombstone_on_the_source(
    conn, synced_task, calendar_id, other_calendar_id
):
    """Guards the source during the PUT->DELETE window.

    Without it, a pull of the source collection would see an href with no local
    row and import it as a brand-new task — the move would resurrect itself.
    """
    task_id = synced_task("m1")
    cache.move_task_local(task_id, other_calendar_id, conn)

    tomb = cache.find_tombstone(calendar_id, "m1", conn)
    assert tomb is not None
    assert tomb["href"] == "m1.ics"


def test_moving_a_new_task_is_local_only(conn, make_task, other_calendar_id):
    """Never pushed anywhere: retarget, stay a create, write no tombstone."""
    task_id = cache.create_task_local(make_task("n1"), conn)
    cache.move_task_local(task_id, other_calendar_id, conn)

    row = cache.get_task_row(task_id, conn)
    assert row["calendar_id"] == other_calendar_id
    assert row["sync_state"] == SyncState.NEW.value
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT change_type FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "create"
    )


def test_moving_to_the_same_calendar_is_a_no_op(conn, synced_task, calendar_id):
    task_id = synced_task("m1")
    cache.move_task_local(task_id, calendar_id, conn)
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0


def test_moving_a_conflicted_task_is_refused(conn, synced_task, other_calendar_id):
    task_id = synced_task("m1")
    conn.execute("UPDATE tasks SET sync_state = 'conflict' WHERE id = ?", (task_id,))
    with pytest.raises(TaskConflictError):
        cache.move_task_local(task_id, other_calendar_id, conn)


@pytest.mark.parametrize("reason", [ReadOnlyReason.MULTIPART, ReadOnlyReason.OVERSIZE])
def test_multipart_and_oversize_tasks_are_movable(conn, synced_task, other_calendar_id, reason):
    """A move relocates bytes verbatim and never re-serializes."""
    task_id = synced_task("m1")
    conn.execute("UPDATE tasks SET read_only_reason = ? WHERE id = ?", (reason.value, task_id))
    cache.move_task_local(task_id, other_calendar_id, conn)
    assert cache.get_task_row(task_id, conn)["calendar_id"] == other_calendar_id


def test_an_unavailable_calendar_blocks_a_move(conn, synced_task, other_calendar_id):
    task_id = synced_task("m1")
    conn.execute(
        "UPDATE tasks SET read_only_reason = ? WHERE id = ?",
        (ReadOnlyReason.CALENDAR_UNAVAILABLE.value, task_id),
    )
    with pytest.raises(ReadOnlyResourceError):
        cache.move_task_local(task_id, other_calendar_id, conn)


def test_moving_to_a_calendar_that_does_not_exist_is_refused(conn, synced_task):
    task_id = synced_task("m1")
    with pytest.raises(cache.CacheError):
        cache.move_task_local(task_id, "0000000000000000", conn)


def test_raw_ics_is_untouched_by_a_move(conn, synced_task, other_calendar_id):
    task_id = synced_task("m1")
    before = cache.get_task_row(task_id, conn)["raw_ics"]
    cache.move_task_local(task_id, other_calendar_id, conn)
    assert cache.get_task_row(task_id, conn)["raw_ics"] == before


# ------------------------------------------------------------------ subtrees


def test_subtasks_move_with_their_parent_by_default(conn, synced_task, other_calendar_id):
    """Parent resolution is per-calendar, so leaving children behind orphans
    them."""
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")
    grandchild = synced_task("g", parent_uid="c")

    cache.move_task_local(parent, other_calendar_id, conn)

    for task_id in (parent, child, grandchild):
        assert cache.get_task_row(task_id, conn)["calendar_id"] == other_calendar_id
    assert cache.get_task_row(child, conn)["parent_uid"] == "p"


def test_declining_the_subtree_promotes_children_in_the_source(
    conn, synced_task, calendar_id, other_calendar_id
):
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")

    cache.move_task_local(parent, other_calendar_id, conn, move_subtree=False)

    assert cache.get_task_row(parent, conn)["calendar_id"] == other_calendar_id
    child_row = cache.get_task_row(child, conn)
    assert child_row["calendar_id"] == calendar_id
    assert child_row["parent_uid"] is None
    assert child_row["sync_state"] == SyncState.DIRTY.value


def test_a_conflicted_subtask_is_left_behind_rather_than_forced(
    conn, synced_task, calendar_id, other_calendar_id
):
    """Its resolution is still the user's to make; we cannot rewrite its ICS."""
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")
    conn.execute("UPDATE tasks SET sync_state = 'conflict' WHERE id = ?", (child,))

    cache.move_task_local(parent, other_calendar_id, conn)

    assert cache.get_task_row(parent, conn)["calendar_id"] == other_calendar_id
    assert cache.get_task_row(child, conn)["calendar_id"] == calendar_id


def test_each_moved_subtask_gets_its_own_pending_row_and_tombstone(
    conn, synced_task, other_calendar_id
):
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")

    cache.move_task_local(parent, other_calendar_id, conn)

    rows = {
        r["task_id"]: r
        for r in conn.execute("SELECT * FROM pending_changes WHERE change_type = 'move'")
    }
    assert set(rows) == {parent, child}
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 2


def test_a_subtask_cycle_does_not_hang_the_move(conn, synced_task, other_calendar_id):
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")
    conn.execute("UPDATE tasks SET parent_uid = 'c' WHERE id = ?", (parent,))

    cache.move_task_local(parent, other_calendar_id, conn)
    assert cache.get_task_row(child, conn)["calendar_id"] == other_calendar_id
