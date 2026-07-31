"""The two-stage move: PUT to the target, then DELETE from the source.

**PUT before DELETE is deliberate.**  A failure between the stages duplicates
the task rather than losing it, which is the correct failure mode for user
data — and the duplicate is self-healing through the tombstone
``move_task_local`` writes on the source before the push.
"""

from __future__ import annotations

import pytest

from davpunk.core import cache, sync_engine
from davpunk.core.caldav_client import CalDAVError
from davpunk.core.ical_parser import new_resource
from davpunk.models.remote import Remote
from davpunk.models.task import SyncState, Task
from tests.fake_caldav import FakeCalDAVServer, FakeClient, count_calls

SRC = "/dav/work/tasks/"
DST = "/dav/work/other/"

MULTIPART = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Other//EN\r\n"
    "BEGIN:VTODO\r\nUID:series\r\nSUMMARY:Weekly\r\nDUE:20260701T170000Z\r\n"
    "RRULE:FREQ=WEEKLY\r\nEND:VTODO\r\n"
    "BEGIN:VTODO\r\nUID:series\r\nRECURRENCE-ID:20260708T170000Z\r\n"
    "SUMMARY:Weekly (moved)\r\nDUE:20260709T170000Z\r\nEND:VTODO\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture
def remote():
    return Remote(id="work", name="Work", url="https://cal.example.test/dav/")


@pytest.fixture
def server():
    fake = FakeCalDAVServer()
    fake.add_collection(SRC, display_name="Tasks")
    fake.add_collection(DST, display_name="Other")
    return fake


@pytest.fixture
def client(server):
    return FakeClient(server)


@pytest.fixture
def synced(conn, remote, client):
    def _run(cancel=None, **kwargs):
        return sync_engine.run_cycle(remote, client, conn, cancel, **kwargs)

    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, client, conn)
    return _run


def server_ics(uid: str, summary: str = "A task") -> str:
    return new_resource(Task(uid=uid, summary=summary))


def cal_row(conn, href):
    return conn.execute("SELECT * FROM calendars WHERE href = ?", (href,)).fetchone()


def local_task(conn, uid: str):
    rows = list(conn.execute("SELECT * FROM tasks WHERE uid = ?", (uid,)))
    return rows[0] if rows else None


@pytest.fixture
def moved(conn, server, synced, calendar_id_for):
    """A clean task in SRC, retargeted to DST but not yet pushed."""

    def _setup(uid="m1", ics=None):
        server.put_resource(SRC, f"{SRC}{uid}.ics", ics or server_ics(uid))
        synced()
        task_id = local_task(conn, uid)["id"]
        cache.move_task_local(task_id, calendar_id_for(DST), conn)
        return task_id

    return _setup


# ------------------------------------------------------------------ happy path


def test_a_clean_move_puts_to_the_target_then_deletes_the_source(
    conn, server, synced, moved, calendar_id_for
):
    task_id = moved()
    synced()

    assert f"{DST}m1.ics" in server.collections[DST].resources
    assert f"{SRC}m1.ics" not in server.collections[SRC].resources

    row = cache.get_task_row(task_id, conn)
    assert row["calendar_id"] == calendar_id_for(DST)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_the_put_happens_before_the_delete(conn, server, synced, moved):
    moved()
    synced()

    order = [kind for kind, _ in server.calls if kind in ("put_create", "delete")]
    assert order.index("put_create") < order.index("delete")


def test_the_moved_bytes_are_transmitted_verbatim(conn, server, synced, moved):
    original = server_ics("m1", "Exactly these bytes")
    moved(ics=original)
    synced()
    assert server.collections[DST].resources[f"{DST}m1.ics"].ics == original


def test_a_multipart_task_moves_byte_identically(conn, server, synced, moved):
    """A move relocates bytes and never re-serializes, which is exactly why a
    quarantined resource is movable at all."""
    task_id = moved(uid="series", ics=MULTIPART)
    assert cache.get_task_row(task_id, conn)["read_only_reason"] == "multipart"

    synced()

    assert server.collections[DST].resources[f"{DST}series.ics"].ics == MULTIPART
    assert f"{SRC}series.ics" not in server.collections[SRC].resources


# --------------------------------------------------------------- stage 0 fails


def test_a_failed_put_leaves_the_source_intact(conn, server, synced, moved):
    """NOTHING IS LOST — the task is still visible in the source after a pull."""
    task_id = moved()
    server.fail_next["put_create"] = CalDAVError("503", status=503)
    synced()

    assert f"{SRC}m1.ics" in server.collections[SRC].resources
    assert f"{DST}m1.ics" not in server.collections[DST].resources
    assert (
        conn.execute(
            "SELECT move_stage FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT attempts FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 1
    )


def test_a_failed_put_is_retried_next_cycle(conn, server, synced, moved):
    task_id = moved()
    server.fail_next["put_create"] = CalDAVError("503", status=503)
    synced()
    cache.reset_backoff(task_id, conn)
    synced()

    assert f"{DST}m1.ics" in server.collections[DST].resources
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value


def test_a_412_on_the_target_with_our_own_uid_adopts_it(conn, server, synced, moved):
    """Our PUT landed and the response was lost; move on to stage 1."""
    task_id = moved()
    server.put_resource(DST, f"{DST}m1.ics", server_ics("m1"))

    synced()

    assert len(server.collections[DST].resources) == 1
    assert f"{SRC}m1.ics" not in server.collections[SRC].resources
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value


def test_a_412_on_the_target_with_a_foreign_uid_rehrefs(conn, server, synced, moved):
    task_id = moved()
    server.put_resource(DST, f"{DST}m1.ics", server_ics("somebody-else"))

    synced()

    row = cache.get_task_row(task_id, conn)
    assert row["uid"] == "m1"  # the UID never changes
    assert row["href"].startswith("m1-")
    assert len(server.collections[DST].resources) == 2
    assert f"{SRC}m1.ics" not in server.collections[SRC].resources


# --------------------------------------------------------------- stage 1 fails


def test_a_crash_after_stage_zero_resumes_the_delete_and_never_re_puts(conn, server, synced, moved):
    """``move_stage`` is persisted precisely so this cannot double-PUT."""
    task_id = moved()
    server.fail_next["delete"] = CalDAVError("503", status=503)
    synced()

    assert (
        conn.execute(
            "SELECT move_stage FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 1
    )
    puts_so_far = count_calls(server, "put_create")

    cache.reset_backoff(task_id, conn)
    synced()

    assert count_calls(server, "put_create") == puts_so_far  # stage 0 never re-ran
    assert f"{SRC}m1.ics" not in server.collections[SRC].resources
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value


def test_a_failure_between_the_stages_duplicates_rather_than_losing(conn, server, synced, moved):
    """The correct failure mode for user data: two copies, never zero."""
    moved()
    server.fail_next["delete"] = CalDAVError("503", status=503)
    synced()

    assert f"{DST}m1.ics" in server.collections[DST].resources
    assert f"{SRC}m1.ics" in server.collections[SRC].resources


def test_a_412_on_the_source_delete_is_a_mode_a_prime_conflict(conn, server, synced, moved):
    """ "The original changed after you moved it — delete it, or keep both?" """
    task_id = moved()
    server.fail_next["delete"] = CalDAVError("stage 0 only", status=503)
    synced()  # stage 0 succeeds, stage 1 fails and backs off

    # Somebody edits the source before we retry the DELETE.
    server.collections[SRC].resources[f"{SRC}m1.ics"].etag = '"moved-on"'
    cache.reset_backoff(task_id, conn)
    synced()

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CONFLICT.value
    row = conn.execute("SELECT * FROM conflict_queue WHERE resolved = 0").fetchone()
    assert row["local_raw_ics"] is None  # the local intent is DELETE
    assert row["remote_raw_ics"] is not None
    # The target copy is retained: nothing was lost.
    assert f"{DST}m1.ics" in server.collections[DST].resources


def test_a_404_on_the_source_delete_completes_the_move(conn, server, synced, moved):
    task_id = moved()
    server.fail_next["delete"] = CalDAVError("stage 0 only", status=503)
    synced()

    del server.collections[SRC].resources[f"{SRC}m1.ics"]
    cache.reset_backoff(task_id, conn)
    synced()

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value


# ------------------------------------------------------------- the tombstone


def test_a_pull_of_the_source_between_the_stages_does_not_re_import(
    conn, server, synced, moved, calendar_id_for
):
    """Without the pre-written tombstone the source's href would look like a
    brand-new task and the move would resurrect itself."""
    task_id = moved()
    server.fail_next["delete"] = CalDAVError("503", status=503)
    synced()

    # A full cycle now pulls the source collection, which still holds the copy.
    assert f"{SRC}m1.ics" in server.collections[SRC].resources
    synced()

    # Exactly one task row, still at the target.
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert cache.get_task_row(task_id, conn)["calendar_id"] == calendar_id_for(DST)


def test_the_stranded_source_copy_is_re_deleted_when_the_source_is_next_pulled(
    conn, server, synced, moved
):
    """The second, independent healing path for a half-finished move.

    The primary one is the move's own DELETE retry.  This is the backstop for
    the case where that never succeeds: the next pull of the source collection
    sees an href with no local row, finds the tombstone on ``(source_calendar,
    uid)``, and re-issues the delete.

    The source's ctag is bumped here because a collection nobody has touched is
    short-circuited by design — the backstop fires the next time the source is
    genuinely enumerated, not on a cycle that had no reason to look.
    """
    moved()
    server.fail_next["delete"] = CalDAVError("503", status=503)
    synced()
    assert f"{SRC}m1.ics" in server.collections[SRC].resources

    server.collections[SRC].touch()
    synced()

    assert f"{SRC}m1.ics" not in server.collections[SRC].resources
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


# ------------------------------------------------------------------- subtrees


def test_a_new_task_moves_with_no_network_traffic_at_all(conn, server, synced, calendar_id_for):
    task_id = cache.create_task_local(
        Task(uid="n1", summary="s", calendar_id=calendar_id_for(SRC)), conn
    )
    cache.move_task_local(task_id, calendar_id_for(DST), conn)

    synced()

    assert f"{DST}n1.ics" in server.collections[DST].resources
    assert count_calls(server, "delete") == 0
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0


def test_a_subtree_moves_together(conn, server, synced, calendar_id_for):
    server.put_resource(SRC, f"{SRC}p.ics", server_ics("p", "Parent"))
    server.put_resource(
        SRC, f"{SRC}c.ics", new_resource(Task(uid="c", summary="Child", parent_uid="p"))
    )
    synced()

    cache.move_task_local(local_task(conn, "p")["id"], calendar_id_for(DST), conn)
    synced()

    assert f"{DST}p.ics" in server.collections[DST].resources
    assert f"{DST}c.ics" in server.collections[DST].resources
    assert server.collections[SRC].resources == {}
    assert local_task(conn, "c")["parent_uid"] == "p"


def test_declining_the_subtree_leaves_the_child_behind_at_root(
    conn, server, synced, calendar_id_for
):
    server.put_resource(SRC, f"{SRC}p.ics", server_ics("p", "Parent"))
    server.put_resource(
        SRC, f"{SRC}c.ics", new_resource(Task(uid="c", summary="Child", parent_uid="p"))
    )
    synced()

    cache.move_task_local(
        local_task(conn, "p")["id"], calendar_id_for(DST), conn, move_subtree=False
    )
    synced()

    assert f"{DST}p.ics" in server.collections[DST].resources
    assert f"{SRC}c.ics" in server.collections[SRC].resources
    assert local_task(conn, "c")["parent_uid"] is None
    # The RELATED-TO removal actually reached the server.
    assert "RELATED-TO" not in server.collections[SRC].resources[f"{SRC}c.ics"].ics


def test_a_move_is_scoped_to_one_remote(conn, calendar_id_for, synced):
    """Cross-remote moves are out of scope; the target must exist locally."""
    with pytest.raises(cache.CacheError):
        cache.move_task_local("no-such-task", calendar_id_for("/elsewhere/", "other"), conn)
