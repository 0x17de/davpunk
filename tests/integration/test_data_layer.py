"""The whole data layer, with no Qt: editor → pending → sync → resolution.

These exercise the seams the unit tests each look at from one side only: an
optimistic edit surviving a full round trip, a conflict detected by the sync
role and resolved by the editor role, a move with a pull interleaved between
its stages, and recovery of unsent intent from a corrupt database.
"""

from __future__ import annotations

import pytest

from davpunk.conflict.resolver import (
    Mode,
    Resolution,
    load_conflict,
    merge_from_selection,
    resolve_conflict,
)
from davpunk.core import cache, recovery, sync_engine
from davpunk.core.caldav_client import CalDAVError
from davpunk.core.ical_parser import new_resource, parse_resource
from davpunk.models.remote import Remote
from davpunk.models.task import Status, SyncState, Task
from tests.fake_caldav import FakeCalDAVServer, FakeClient

SRC = "/dav/work/tasks/"
DST = "/dav/work/other/"


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
def sync(conn, remote, client):
    def _run(**kwargs):
        return sync_engine.run_cycle(remote, client, conn, **kwargs)

    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, client, conn)
    return _run


def server_ics(uid: str, summary: str = "A task", **kwargs) -> str:
    return new_resource(Task(uid=uid, summary=summary, **kwargs))


def task_by_uid(conn, uid: str):
    rows = list(conn.execute("SELECT * FROM tasks WHERE uid = ?", (uid,)))
    return rows[0] if rows else None


def open_conflict_id(conn) -> int:
    return conn.execute("SELECT id FROM conflict_queue WHERE resolved = 0").fetchone()[0]


# ------------------------------------------------------- the optimistic path


def test_an_optimistic_edit_survives_a_full_round_trip(conn, server, sync, calendar_id_for):
    """The UI writes immediately; the network catches up later."""
    task_id = cache.create_task_local(
        Task(
            uid="t1",
            summary="Write the HLD",
            calendar_id=calendar_id_for(SRC),
            status=Status.NEEDS_ACTION,
            due_value="20260731",
        ),
        conn,
    )
    # Visible and editable before any network call.
    assert cache.get_task(task_id, conn).summary == "Write the HLD"
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.NEW.value

    sync()
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value

    cache.update_task_optimistic(task_id, {"status": Status.COMPLETED}, conn)
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.DIRTY.value
    sync()

    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["percent_complete"] == 100  # canonicalized on the way out
    stored = server.collections[SRC].resources[f"{SRC}t1.ics"].ics
    assert "STATUS:COMPLETED" in stored
    assert "PERCENT-COMPLETE:100" in stored


def test_a_date_due_survives_the_round_trip_verbatim(conn, server, sync, calendar_id_for):
    cache.create_task_local(
        Task(uid="t1", summary="s", calendar_id=calendar_id_for(SRC), due_value="20260731"), conn
    )
    sync()

    stored = server.collections[SRC].resources[f"{SRC}t1.ics"].ics
    assert "DUE;VALUE=DATE:20260731" in stored
    assert parse_resource(stored).due_value == "20260731"


def test_a_tzid_due_survives_with_a_generated_vtimezone(conn, server, sync, calendar_id_for):
    cache.create_task_local(
        Task(
            uid="t1",
            summary="s",
            calendar_id=calendar_id_for(SRC),
            due_value="20260731T170000",
            due_tzid="Europe/Berlin",
        ),
        conn,
    )
    sync()

    stored = server.collections[SRC].resources[f"{SRC}t1.ics"].ics
    assert "DUE;TZID=Europe/Berlin:20260731T170000" in stored
    assert "BEGIN:VTIMEZONE" in stored


# ------------------------------------------------ conflict detect and resolve


def test_conflict_detected_then_merged_then_re_synced(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1", "Original"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]

    # Both sides move.
    cache.update_task_optimistic(task_id, {"summary": "My version"}, conn)
    resource = server.collections[SRC].resources[f"{SRC}t1.ics"]
    resource.ics = server_ics("t1", "Their version")
    resource.etag = '"theirs"'
    server.collections[SRC].touch()

    sync()
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CONFLICT.value

    view = load_conflict(open_conflict_id(conn), conn)
    assert view.mode is Mode.BOTH_CHANGED
    merged = merge_from_selection(view, take_remote=set())  # take all local
    resolve_conflict(view.conflict_id, Resolution.MERGE, merged, conn)

    sync()

    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["summary"] == "My version"
    assert "SUMMARY:My version" in server.collections[SRC].resources[f"{SRC}t1.ics"].ics
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 0


def test_conflict_resolved_by_taking_the_server_version_sends_no_put(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1", "Original"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]

    cache.update_task_optimistic(task_id, {"summary": "My version"}, conn)
    resource = server.collections[SRC].resources[f"{SRC}t1.ics"]
    resource.ics = server_ics("t1", "Their version")
    resource.etag = '"theirs"'
    server.collections[SRC].touch()
    sync()

    puts_before = sum(1 for k, _ in server.calls if k == "put_update")
    resolve_conflict(open_conflict_id(conn), Resolution.RESTORE_SERVER, None, conn)
    sync()

    assert sum(1 for k, _ in server.calls if k == "put_update") == puts_before
    assert cache.get_task_row(task_id, conn)["summary"] == "Their version"


def test_a_deleted_dirty_task_hitting_412_can_be_deleted_anyway(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]

    cache.delete_task_local(task_id, conn)
    server.collections[SRC].resources[f"{SRC}t1.ics"].etag = '"moved-on"'
    sync_engine.push_phase(
        conn.execute("SELECT * FROM calendars WHERE href = ?", (SRC,)).fetchone(),
        FakeClient(server),
        conn,
    )

    view = load_conflict(open_conflict_id(conn), conn)
    assert view.mode is Mode.LOCAL_DELETE
    resolve_conflict(view.conflict_id, Resolution.DELETE_ANYWAY, None, conn)
    sync()

    assert task_by_uid(conn, "t1") is None
    assert f"{SRC}t1.ics" not in server.collections[SRC].resources


def test_the_same_conflict_can_instead_keep_the_server_version(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1", "Server wins"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]

    cache.delete_task_local(task_id, conn)
    server.collections[SRC].resources[f"{SRC}t1.ics"].etag = '"moved-on"'
    sync_engine.push_phase(
        conn.execute("SELECT * FROM calendars WHERE href = ?", (SRC,)).fetchone(),
        FakeClient(server),
        conn,
    )

    resolve_conflict(open_conflict_id(conn), Resolution.RESTORE_SERVER, None, conn)
    sync()

    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["summary"] == "Server wins"
    assert f"{SRC}t1.ics" in server.collections[SRC].resources


def test_server_deleted_plus_local_modified_can_be_recreated(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]

    cache.update_task_optimistic(task_id, {"summary": "Still want this"}, conn)
    del server.collections[SRC].resources[f"{SRC}t1.ics"]
    server.collections[SRC].touch()
    sync()

    view = load_conflict(open_conflict_id(conn), conn)
    assert view.mode is Mode.SERVER_DELETED
    resolve_conflict(view.conflict_id, Resolution.RECREATE, view.local, conn)
    sync()

    assert f"{SRC}t1.ics" in server.collections[SRC].resources
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value


def test_server_deleted_plus_local_modified_can_accept_the_deletion(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]

    cache.update_task_optimistic(task_id, {"summary": "Never mind"}, conn)
    del server.collections[SRC].resources[f"{SRC}t1.ics"]
    server.collections[SRC].touch()
    sync()

    resolve_conflict(open_conflict_id(conn), Resolution.ACCEPT_DELETION, None, conn)
    sync()

    assert task_by_uid(conn, "t1") is None
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1


def test_a_pending_delete_whose_resource_is_already_gone_just_succeeds(conn, server, sync):
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1"))
    sync()
    cache.delete_task_local(task_by_uid(conn, "t1")["id"], conn)

    del server.collections[SRC].resources[f"{SRC}t1.ics"]
    sync()

    assert task_by_uid(conn, "t1") is None
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue").fetchone()[0] == 0


# --------------------------------------------------------- role interleaving


def test_a_guarded_apply_loses_to_an_editor_write_and_retries(conn, server, sync):
    """The sync role never overwrites an edit made between its read and its
    write; it defers to the next cycle."""
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1", "v1"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]
    conn.execute("INSERT INTO task_categories VALUES (?, 'local-tag')", (task_id,))

    # The editor writes while the server also moves on.
    cache.update_task_optimistic(task_id, {"summary": "editor wins"}, conn)
    resource = server.collections[SRC].resources[f"{SRC}t1.ics"]
    resource.ics = server_ics("t1", "v2")
    resource.etag = '"v2"'

    incoming = parse_resource(resource.ics)
    assert cache.apply_server_version(task_id, incoming, resource.etag, conn) is False

    # Neither the row nor its child table moved.
    assert cache.get_task_row(task_id, conn)["summary"] == "editor wins"
    assert cache.load_categories(task_id, conn) == ["local-tag"]


def test_create_then_delete_before_any_sync_never_touches_the_network(
    conn, server, sync, calendar_id_for
):
    """"""
    task_id = cache.create_task_local(
        Task(uid="ephemeral", summary="Oops", calendar_id=calendar_id_for(SRC)), conn
    )
    cache.delete_task_local(task_id, conn)
    sync()

    assert not any(k in ("put_create", "delete") for k, _ in server.calls)
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


# ----------------------------------------------------------------------- move


def test_a_move_with_a_pull_interleaved_between_its_stages(conn, server, sync, calendar_id_for):
    """The tombstone written before the push is what stops the source's copy
    being re-imported as a brand-new task."""
    server.put_resource(SRC, f"{SRC}m1.ics", server_ics("m1", "Moving"))
    sync()
    task_id = task_by_uid(conn, "m1")["id"]

    cache.move_task_local(task_id, calendar_id_for(DST), conn)
    server.fail_next["delete"] = CalDAVError("503", status=503)
    sync()  # stage 0 lands, stage 1 fails: two copies exist

    assert f"{DST}m1.ics" in server.collections[DST].resources
    assert f"{SRC}m1.ics" in server.collections[SRC].resources

    # A full cycle now pulls the source, which still offers the stranded copy.
    server.collections[SRC].touch()
    sync()

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert cache.get_task_row(task_id, conn)["calendar_id"] == calendar_id_for(DST)
    assert f"{SRC}m1.ics" not in server.collections[SRC].resources


def test_a_move_never_loses_the_task_even_if_every_stage_fails(conn, server, sync, calendar_id_for):
    server.put_resource(SRC, f"{SRC}m1.ics", server_ics("m1"))
    sync()
    task_id = task_by_uid(conn, "m1")["id"]

    cache.move_task_local(task_id, calendar_id_for(DST), conn)
    for _ in range(3):
        server.fail_next["put_create"] = CalDAVError("503", status=503)
        cache.reset_backoff(task_id, conn)
        sync()

    # Still exactly one copy, still readable, still on the server.
    assert f"{SRC}m1.ics" in server.collections[SRC].resources
    assert cache.get_task(task_id, conn) is not None


# ------------------------------------------------------------------ recovery


def test_a_corrupt_database_mid_flight_dumps_the_unsent_intent(
    conn, server, sync, db_path, calendar_id_for
):
    """"""
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1"))
    sync()
    task_id = task_by_uid(conn, "t1")["id"]
    cache.update_task_optimistic(task_id, {"summary": "unsent when it died"}, conn)
    cache.close_db(conn)

    dump, error = recovery._dump_unsent_intent(db_path)

    assert error is None
    assert len(dump["pending_changes"]) == 1
    assert dump["pending_changes"][0]["uid"] == "t1"
    assert dump["pending_changes"][0]["change_type"] == "update"


def test_task_refs_survive_a_rebuild(conn, server, sync, db_path, calendar_id_for, remote):
    """A cache rebuild is a normal event; an agent's refs must survive it."""
    server.put_resource(SRC, f"{SRC}t1.ics", server_ics("t1"))
    sync()
    cal_before = task_by_uid(conn, "t1")["calendar_id"]
    cache.close_db(conn)

    with db_path.open("r+b") as handle:
        handle.write(b"not a database at all\x00")

    fresh, report = cache.open_or_recover(db_path)
    try:
        assert report is not None
        cache.reconcile_remotes([remote], fresh)
        sync_engine.run_cycle(remote, FakeClient(server), fresh)
        assert task_by_uid(fresh, "t1")["calendar_id"] == cal_before
    finally:
        fresh.close()


# ---------------------------------------------------------------- multi-task


def test_a_realistic_mixed_cycle(conn, server, sync, calendar_id_for):
    """Server-new, locally-new, edited, deleted and conflicted, all at once."""
    cal = calendar_id_for(SRC)
    server.put_resource(SRC, f"{SRC}srv1.ics", server_ics("srv1", "From server"))
    server.put_resource(SRC, f"{SRC}srv2.ics", server_ics("srv2", "Also from server"))
    server.put_resource(SRC, f"{SRC}srv3.ics", server_ics("srv3", "To be deleted"))
    sync()

    local_id = cache.create_task_local(Task(uid="loc1", summary="Local", calendar_id=cal), conn)
    cache.update_task_optimistic(task_by_uid(conn, "srv1")["id"], {"summary": "Edited"}, conn)
    cache.delete_task_local(task_by_uid(conn, "srv3")["id"], conn)

    # And a conflict on srv2.
    cache.update_task_optimistic(task_by_uid(conn, "srv2")["id"], {"summary": "Mine"}, conn)
    resource = server.collections[SRC].resources[f"{SRC}srv2.ics"]
    resource.ics = server_ics("srv2", "Theirs")
    resource.etag = '"theirs"'
    server.collections[SRC].touch()

    sync()

    assert cache.get_task_row(local_id, conn)["sync_state"] == SyncState.CLEAN.value
    assert task_by_uid(conn, "srv1")["sync_state"] == SyncState.CLEAN.value
    assert task_by_uid(conn, "srv1")["summary"] == "Edited"
    assert task_by_uid(conn, "srv3") is None
    assert task_by_uid(conn, "srv2")["sync_state"] == SyncState.CONFLICT.value
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 1

    # Resolve the one conflict and the cycle settles.
    view = load_conflict(open_conflict_id(conn), conn)
    resolve_conflict(view.conflict_id, Resolution.RESTORE_SERVER, None, conn)
    sync()

    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE sync_state != 'clean'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
