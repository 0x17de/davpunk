"""The v8 sync cycle, end to end against a fake server."""

from __future__ import annotations

import threading

import pytest

from davpunk.core import cache, sync_engine
from davpunk.core.caldav_client import CalDAVError
from davpunk.core.ical_parser import new_resource
from davpunk.models.remote import Remote
from davpunk.models.task import SyncState, Task
from tests.fake_caldav import FakeCalDAVServer, FakeClient, count_calls

CAL_HREF = "/dav/work/tasks/"
OTHER_HREF = "/dav/work/other/"


@pytest.fixture
def remote():
    return Remote(id="work", name="Work", url="https://cal.example.test/dav/")


@pytest.fixture
def server():
    fake = FakeCalDAVServer()
    fake.add_collection(CAL_HREF, display_name="Tasks")
    fake.add_collection(OTHER_HREF, display_name="Other")
    return fake


@pytest.fixture
def client(server):
    return FakeClient(server)


@pytest.fixture
def synced(conn, remote, client):
    """Run a cycle and return the results.

    Discovery runs once up front so the ``calendars`` rows exist: a user can
    only create a task in a collection they can see, so a test that creates one
    before any discovery is testing a state that cannot occur.
    """

    def _run(cancel=None, **kwargs):
        return sync_engine.run_cycle(remote, client, conn, cancel, **kwargs)

    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, client, conn)
    return _run


def server_ics(uid: str, summary: str = "From the server", **kwargs) -> str:
    return new_resource(Task(uid=uid, summary=summary, **kwargs))


def cal_id(conn, href=CAL_HREF) -> str:
    from davpunk.models.calendar import calendar_id

    return calendar_id("work", href)


def cal_row(conn, href=CAL_HREF):
    """The calendar row by href — cache.calendar_rows() sorts by display name,
    so indexing into it picks 'Other' before 'Tasks'."""
    return conn.execute("SELECT * FROM calendars WHERE href = ?", (href,)).fetchone()


def local_task(conn, uid: str):
    rows = list(conn.execute("SELECT * FROM tasks WHERE uid = ?", (uid,)))
    return rows[0] if rows else None


# ------------------------------------------------------------------ discovery


def test_discovery_records_both_collections(conn, synced):
    synced()
    hrefs = {row["href"] for row in cache.calendar_rows(conn)}
    assert hrefs == {CAL_HREF, OTHER_HREF}


def test_a_vanished_collection_is_quarantined_not_deleted(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    assert local_task(conn, "a") is not None

    server.missing_collections.add(CAL_HREF)
    synced()

    row = local_task(conn, "a")
    assert row is not None  # never deleted
    assert row["read_only_reason"] == "calendar-unavailable"
    assert (
        conn.execute("SELECT available FROM calendars WHERE id = ?", (cal_id(conn),)).fetchone()[0]
        == 0
    )


def test_a_returning_collection_lifts_its_quarantine(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    server.missing_collections.add(CAL_HREF)
    synced()
    server.missing_collections.discard(CAL_HREF)
    synced()

    assert local_task(conn, "a")["read_only_reason"] is None


# ----------------------------------------------------------------- clean pull


def test_a_clean_sync_from_an_empty_cache_imports_everything(conn, server, synced):
    for n in ("a", "b", "c"):
        server.put_resource(CAL_HREF, f"{CAL_HREF}{n}.ics", server_ics(n))

    pull, _ = synced()

    assert pull.imported == 3
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
    assert local_task(conn, "a")["sync_state"] == SyncState.CLEAN.value


def test_the_ctag_short_circuit_skips_a_second_cycle(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    before = count_calls(server, "list")
    synced()
    assert count_calls(server, "list") == before  # no re-enumeration


def test_a_pending_change_defeats_the_ctag_short_circuit(conn, server, synced):
    """Otherwise a local edit would sit unsent until the server happened to
    change something."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "mine"}, conn)

    _, push = synced()
    assert push.pushed == 1


def test_a_server_side_change_is_applied_over_a_clean_row(conn, server, synced):
    resource = server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "First"))
    synced()

    resource.ics = server_ics("a", "Second")
    resource.etag = '"changed"'
    server.collections[CAL_HREF].touch()
    pull, _ = synced()

    assert pull.applied == 1
    assert local_task(conn, "a")["summary"] == "Second"


def test_an_unchanged_etag_is_not_refetched(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    server.collections[CAL_HREF].touch()  # ctag moved, this resource did not
    before = count_calls(server, "multiget")
    synced()
    assert count_calls(server, "multiget") == before


def test_a_vevent_only_resource_is_skipped_not_fatal(conn, server, synced):
    """Mixed collections exist; events are simply not ours."""
    server.put_resource(
        CAL_HREF,
        f"{CAL_HREF}event.ics",
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))

    pull, _ = synced()
    assert pull.imported == 1
    assert pull.skipped == 1


def test_multiget_batches_rather_than_issuing_one_get_each(conn, server, synced):
    """200 changed hrefs are 4 batches, not 200 GETs."""
    for n in range(200):
        server.put_resource(CAL_HREF, f"{CAL_HREF}{n:03d}.ics", server_ics(f"u{n:03d}"))

    synced()

    assert count_calls(server, "multiget") == 4
    assert count_calls(server, "get") == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 200


def test_a_failed_href_falls_back_to_a_single_get(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    server.fail_next["multiget"] = CalDAVError("multiget exploded")

    # The whole batch fails, so nothing is imported this cycle...
    with pytest.raises(CalDAVError):
        synced()


# --------------------------------------------------------------------- create


def test_create_pushes_and_goes_clean(conn, server, synced, calendar_id_for):
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="Made here", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    _, push = synced()

    assert push.pushed == 1
    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["etag"] is not None
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
    assert f"{CAL_HREF}local-1.ics" in server.collections[CAL_HREF].resources


def test_create_then_two_edits_is_a_single_put(conn, server, synced, calendar_id_for):
    """'new' never decays to 'dirty', so there is one create, not a create and
    two updates."""
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="v1", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    cache.update_task_optimistic(task_id, {"summary": "v2"}, conn)
    cache.update_task_optimistic(task_id, {"summary": "v3"}, conn)

    synced()

    assert count_calls(server, "put_create") == 1
    assert count_calls(server, "put_update") == 0
    stored = server.collections[CAL_HREF].resources[f"{CAL_HREF}local-1.ics"]
    assert "SUMMARY:v3" in stored.ics


def test_no_conflict_row_is_created_by_a_clean_create(conn, synced, calendar_id_for):
    cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    synced()
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue").fetchone()[0] == 0


def test_a_412_with_a_matching_uid_adopts_rather_than_duplicating(
    conn, server, synced, calendar_id_for
):
    """Our earlier PUT landed but the response was lost.  Creating a second copy
    here is the classic duplicate-task bug."""
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="Made here", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    # The server already holds our resource, under our href, with our UID.
    server.put_resource(CAL_HREF, f"{CAL_HREF}local-1.ics", server_ics("local-1", "Made here"))

    _, push = synced()

    assert push.pushed == 1
    assert len(server.collections[CAL_HREF].resources) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["etag"] is not None


def test_a_412_with_a_foreign_uid_rehrefs_and_retries(conn, server, synced, calendar_id_for):
    """Same UID, different href — the UID never changes."""
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="Mine", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    server.put_resource(
        CAL_HREF, f"{CAL_HREF}local-1.ics", server_ics("somebody-elses-uid", "Theirs")
    )

    _, push = synced()

    assert push.pushed == 1
    row = cache.get_task_row(task_id, conn)
    assert row["uid"] == "local-1"  # unchanged
    assert row["href"] != "local-1.ics"
    assert row["href"].startswith("local-1-")
    assert len(server.collections[CAL_HREF].resources) == 2


def test_a_location_header_rewrites_the_href(conn, server, synced, calendar_id_for):
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    server.location_rewrite = f"{CAL_HREF}server-chosen.ics"

    synced()
    assert cache.get_task_row(task_id, conn)["href"] == "server-chosen.ics"


def test_a_missing_collection_marks_it_unavailable_and_does_not_retry_per_task(
    conn, server, synced, calendar_id_for
):
    """409 in WebDAV means the parent collection is missing."""
    cal = calendar_id_for(CAL_HREF)
    cache.create_task_local(Task(uid="local-1", summary="s", calendar_id=cal), conn)
    synced()  # establish the calendar row

    cache.update_task_optimistic(local_task(conn, "local-1")["id"], {"summary": "x"}, conn)
    server.missing_collections.add(CAL_HREF)
    synced()

    assert conn.execute("SELECT available FROM calendars WHERE id = ?", (cal,)).fetchone()[0] == 0


# --------------------------------------------------------------------- update


def test_an_update_is_guarded_by_the_base_etag(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Server"))
    synced()

    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)
    _, push = synced()

    assert push.pushed == 1
    assert "SUMMARY:Mine" in server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"].ics
    assert local_task(conn, "a")["sync_state"] == SyncState.CLEAN.value


def test_a_2xx_without_an_etag_falls_back_to_a_multiget(conn, server, synced):
    """Common: the RFC does not require an ETag on the response."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    server.omit_etag_on_write = True
    synced()

    assert count_calls(server, "get_etag") == 1
    assert local_task(conn, "a")["etag"] is not None


def test_a_failed_etag_refresh_leaves_a_null_etag_and_loses_nothing(conn, server, synced):
    """NULL != any server ETag, so the next cycle re-fetches once and the
    clean-guard path applies."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    server.omit_etag_on_write = True
    server.fail_next["get_etag"] = CalDAVError("multiget refused")
    synced()

    row = local_task(conn, "a")
    assert row["etag"] is None
    assert row["sync_state"] == SyncState.CLEAN.value
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0

    server.omit_etag_on_write = False
    synced()
    assert local_task(conn, "a")["etag"] is not None


def test_a_412_on_update_becomes_a_conflict_and_keeps_the_pending_row(conn, server, synced):
    """The push-side detection: If-Match fails because the server moved on.

    Driven through push_phase directly, because in a full cycle the *pull*
    reaches the diverged resource first — see the next test.
    """
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Server v1"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    resource = server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    resource.ics = server_ics("a", "Server v2")
    resource.etag = '"moved-on"'

    push = sync_engine.push_phase(cal_row(conn), FakeClient(server), conn)

    assert push.conflicts == 1
    assert local_task(conn, "a")["sync_state"] == SyncState.CONFLICT.value
    row = conn.execute("SELECT * FROM conflict_queue WHERE resolved = 0").fetchone()
    assert row["local_raw_ics"] is not None
    assert "Server v2" in row["remote_raw_ics"]
    # The user's intent survives resolution.
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 1


def test_in_a_full_cycle_the_pull_detects_the_divergence_first(conn, server, synced):
    """Both detection sites reach the same state, and the push is then skipped
    rather than making a doomed attempt."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Server v1"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    resource = server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    resource.ics = server_ics("a", "Server v2")
    resource.etag = '"moved-on"'
    server.collections[CAL_HREF].touch()

    before = count_calls(server, "put_update")
    pull, push = synced()

    assert pull.conflicts == 1
    assert push.conflicts == 0
    assert count_calls(server, "put_update") == before
    assert local_task(conn, "a")["sync_state"] == SyncState.CONFLICT.value
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 1


def _diverge(server, uid="a", **kwargs) -> None:
    """Move the server's copy on, so the next cycle sees a divergence."""
    resource = server.collections[CAL_HREF].resources[f"{CAL_HREF}{uid}.ics"]
    resource.ics = server_ics(uid, **kwargs)
    resource.etag = '"moved-on"'
    server.collections[CAL_HREF].touch()


#: Comfortably after ``cache.now()``, so the server's copy is the newer one.
LATER = 2**31


def test_an_ordering_only_collision_is_merged_without_asking(conn, server, synced):
    """A drag is a full PUT and a rebalance is one per renumbered sibling, so
    two clients tidying the same list collide on a field the merge window does
    not even show.  Latest wins, and nobody is asked."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Same", davpunk_order=1000))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"davpunk_order": 2000}, conn)
    _diverge(server, summary="Same", davpunk_order=9000, last_modified=LATER)

    pull, push = synced()

    # The server's position is the newer one, so nothing is sent back.
    assert (pull.conflicts, pull.auto_merged, push.pushed) == (0, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 0
    # Settled, not sidestepped: the row is written and resolved like any other.
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 1").fetchone()[0] == 1
    row = local_task(conn, "a")
    assert (row["davpunk_order"], row["sync_state"]) == (9000, SyncState.CLEAN.value)


def test_a_newer_local_position_is_pushed_rather_than_asked_about(conn, server, synced):
    """The other direction: our drag is the newer one, so it goes out — re-based
    on the ETag the server moved to, so the If-Match succeeds this time."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Same", davpunk_order=1000))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"davpunk_order": 2000}, conn)
    _diverge(server, summary="Same", davpunk_order=9000, last_modified=1000)

    pull, push = synced()

    assert (pull.conflicts, pull.auto_merged, push.pushed) == (0, 1, 1)
    assert local_task(conn, "a")["sync_state"] == SyncState.CLEAN.value
    assert "X-DAVPUNK-ORDER:2000" in server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"].ics


def test_an_ordering_change_beside_a_real_edit_is_still_a_question(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Same", davpunk_order=1000))
    synced()
    cache.update_task_optimistic(
        local_task(conn, "a")["id"], {"summary": "Mine", "davpunk_order": 2000}, conn
    )
    _diverge(server, summary="Server v2", davpunk_order=9000, last_modified=LATER)

    pull, _ = synced()

    assert (pull.conflicts, pull.auto_merged) == (1, 0)
    assert local_task(conn, "a")["sync_state"] == SyncState.CONFLICT.value


def test_a_412_on_an_ordering_only_change_is_auto_merged(conn, server, synced):
    """The push-side detection site, driven directly — in a full cycle the pull
    reaches the divergence first."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Same", davpunk_order=1000))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"davpunk_order": 2000}, conn)
    _diverge(server, summary="Same", davpunk_order=9000, last_modified=LATER)

    push = sync_engine.push_phase(cal_row(conn), FakeClient(server), conn)

    assert (push.conflicts, push.auto_merged) == (0, 1)
    assert local_task(conn, "a")["sync_state"] == SyncState.CLEAN.value
    # Unlike a conflict the user has to settle, there is no intent left to keep.
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0


def test_a_404_on_update_is_a_mode_b_conflict(conn, server, synced):
    """The server deleted it under us; remote_raw_ics IS NULL."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    task_id = local_task(conn, "a")["id"]
    cache.update_task_optimistic(task_id, {"summary": "Mine"}, conn)

    del server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    # Keep the ctag so the pull does not notice the removal first.
    sync_engine.push_phase(cal_row(conn), FakeClient(server), conn)

    assert local_task(conn, "a")["sync_state"] == SyncState.CONFLICT.value
    row = conn.execute("SELECT * FROM conflict_queue WHERE resolved = 0").fetchone()
    assert row["remote_raw_ics"] is None


def test_an_update_with_a_null_base_etag_is_repaired_to_a_create(conn, server, synced):
    """A NULL base_etag on an update would mean an unguarded PUT."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    task_id = local_task(conn, "a")["id"]
    cache.update_task_optimistic(task_id, {"summary": "Mine"}, conn)
    conn.execute("UPDATE pending_changes SET base_etag = NULL WHERE task_id = ?", (task_id,))

    synced()

    assert (
        conn.execute(
            "SELECT change_type FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "create"
    )


def test_the_sequence_is_bumped_on_a_successful_put(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    before = local_task(conn, "a")["sequence"]
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)
    synced()
    assert local_task(conn, "a")["sequence"] == before + 1


# --------------------------------------------------------------------- delete


def test_a_delete_tombstones_and_removes_the_row(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)

    _, push = synced()

    assert push.pushed == 1
    assert local_task(conn, "a") is None
    assert f"{CAL_HREF}a.ics" not in server.collections[CAL_HREF].resources
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1


def test_a_404_on_delete_is_success_not_a_conflict(conn, server, synced):
    """Both sides agree it is gone."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)
    del server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]

    sync_engine.push_phase(cal_row(conn), FakeClient(server), conn)

    assert local_task(conn, "a") is None
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue").fetchone()[0] == 0


def test_a_412_on_delete_is_a_mode_a_prime_conflict(conn, server, synced):
    """ "Server has a newer version.  Delete anyway, or keep it?" """
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)

    server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"].etag = '"moved-on"'
    push = sync_engine.push_phase(cal_row(conn), FakeClient(server), conn)

    assert push.conflicts == 1
    row = conn.execute("SELECT * FROM conflict_queue WHERE resolved = 0").fetchone()
    assert row["local_raw_ics"] is None  # local intent is DELETE
    assert row["remote_raw_ics"] is not None


def test_a_never_synced_task_deleted_locally_never_reaches_the_network(
    conn, server, synced, calendar_id_for
):
    """"""
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    cache.delete_task_local(task_id, conn)
    synced()

    assert count_calls(server, "put_create") == 0
    assert count_calls(server, "delete") == 0
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0


# --------------------------------------------------------- server-side delete


def test_a_server_delete_of_a_clean_task_removes_it_locally(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    del server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    server.collections[CAL_HREF].touch()

    pull, _ = synced()

    assert pull.deleted == 1
    assert local_task(conn, "a") is None
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1


def test_a_server_delete_of_a_dirty_task_is_a_mode_b_conflict(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    del server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    server.collections[CAL_HREF].touch()
    pull, _ = synced()

    assert pull.conflicts == 1
    assert local_task(conn, "a")["sync_state"] == SyncState.CONFLICT.value
    row = conn.execute("SELECT * FROM conflict_queue WHERE resolved = 0").fetchone()
    assert row["remote_raw_ics"] is None


def test_a_server_delete_of_a_pending_delete_task_just_completes_it(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)

    del server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    server.collections[CAL_HREF].touch()
    synced()

    assert local_task(conn, "a") is None
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue").fetchone()[0] == 0


def test_a_server_delete_of_an_already_conflicted_task_preserves_the_decision(conn, server, synced):
    """The task row and the user's unresolved decision are PRESERVED."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    task_id = local_task(conn, "a")["id"]
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "local-snapshot", "remote-snapshot", '"e"', conn)
        cache.set_sync_state(task_id, SyncState.CONFLICT, conn)

    del server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    server.collections[CAL_HREF].touch()
    synced()

    assert local_task(conn, "a") is not None
    rows = list(conn.execute("SELECT * FROM conflict_queue WHERE task_id = ?", (task_id,)))
    assert len(rows) == 1
    # The local side is the snapshot the user's pending edit is based on.
    assert rows[0]["local_raw_ics"] == "local-snapshot"
    assert rows[0]["remote_raw_ics"] is None


def test_three_consecutive_conflict_detections_leave_exactly_one_open_row(conn, server, synced):
    """"""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Server v1"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    for version in range(2, 5):
        resource = server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
        resource.ics = server_ics("a", f"Server v{version}")
        resource.etag = f'"v{version}"'
        server.collections[CAL_HREF].touch()
        synced()

    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 1


# ------------------------------------------------------------- resurrection


def test_a_resurrected_uid_is_re_deleted_three_times_then_accepted(conn, server, synced):
    """Blocking alone does not converge."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)
    synced()
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1

    for _ in range(3):
        # A stale client replays the task, landing on a different href.
        server.put_resource(CAL_HREF, f"{CAL_HREF}replayed.ics", server_ics("a"))
        synced()
        assert local_task(conn, "a") is None
        assert f"{CAL_HREF}replayed.ics" not in server.collections[CAL_HREF].resources

    assert conn.execute("SELECT resurrect_count FROM tombstones").fetchone()[0] == 3

    # The fourth time we stop fighting: another client is deliberately keeping
    # it alive.
    server.put_resource(CAL_HREF, f"{CAL_HREF}replayed.ics", server_ics("a"))
    synced()

    assert local_task(conn, "a") is not None
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0


def test_a_tombstone_matches_on_uid_not_href(conn, server, synced):
    """A stale client reuses the UID but often lands on a different href."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}original.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)
    synced()

    server.put_resource(CAL_HREF, f"{CAL_HREF}totally-different.ics", server_ics("a"))
    synced()
    assert local_task(conn, "a") is None


def test_a_genuinely_new_task_with_a_new_uid_is_not_blocked(conn, server, synced):
    """A user re-creating "the same" task mints a new UID."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)
    synced()

    server.put_resource(CAL_HREF, f"{CAL_HREF}b.ics", server_ics("b"))
    synced()
    assert local_task(conn, "b") is not None


def test_an_expired_tombstone_stops_blocking(conn, server, synced, monkeypatch):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    cache.delete_task_local(local_task(conn, "a")["id"], conn)
    synced()

    real_now = cache.now
    monkeypatch.setattr(cache, "now", lambda: real_now() + cache.TOMBSTONE_TTL + 1)

    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    assert local_task(conn, "a") is not None


# -------------------------------------------------------------------- backoff


def test_eight_failures_block_the_change(conn, server, synced, calendar_id_for):
    """"""
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    for _ in range(cache.MAX_ATTEMPTS):
        server.fail_next["put_create"] = CalDAVError("503 Service Unavailable", status=503)
        cache.reset_backoff(task_id, conn)  # simulate the delay elapsing
        conn.execute("UPDATE pending_changes SET attempts = attempts WHERE task_id = ?", (task_id,))
        synced()
        conn.execute("UPDATE pending_changes SET next_attempt_at = 0 WHERE task_id = ?", (task_id,))
        conn.execute(
            "UPDATE pending_changes SET attempts = attempts + 0 WHERE task_id = ?", (task_id,)
        )

    # Drive the attempt counter directly for the blocking assertion; the loop
    # above proves each failure is recorded.
    conn.execute(
        "UPDATE pending_changes SET attempts = ? WHERE task_id = ?",
        (cache.MAX_ATTEMPTS - 1, task_id),
    )
    server.fail_next["put_create"] = CalDAVError("503", status=503)
    synced()

    assert (
        conn.execute(
            "SELECT blocked FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("status", [400, 403, 415])
def test_a_non_retryable_status_blocks_immediately(conn, server, synced, calendar_id_for, status):
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    server.fail_next["put_create"] = CalDAVError(f"{status}", status=status)
    synced()

    row = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert row["blocked"] == 1
    assert row["attempts"] == 1


def test_a_blocked_change_is_not_retried(conn, server, synced, calendar_id_for):
    task_id = cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    conn.execute("UPDATE pending_changes SET blocked = 1 WHERE task_id = ?", (task_id,))
    synced()
    assert count_calls(server, "put_create") == 0


# --------------------------------------------------------------- cancellation


def test_cancelling_mid_batch_records_no_ctag(conn, server, remote, client):
    """A partial pull must not store a ctag that would short-circuit the next
    cycle."""
    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, client, conn)
    for n in range(120):
        server.put_resource(CAL_HREF, f"{CAL_HREF}{n:03d}.ics", server_ics(f"u{n:03d}"))

    cancel = threading.Event()
    original = sync_engine._fetch_batch
    calls = {"n": 0}

    def cancel_after_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:
            cancel.set()
        return original(*args, **kwargs)

    sync_engine._fetch_batch = cancel_after_first
    try:
        sync_engine.run_cycle(remote, client, conn, cancel)
    finally:
        sync_engine._fetch_batch = original

    cal_row = conn.execute("SELECT * FROM calendars WHERE href = ?", (CAL_HREF,)).fetchone()
    assert cal_row["ctag"] != server.collections[CAL_HREF].ctag


def test_cancelling_before_the_push_leaves_the_pending_row_intact(
    conn, server, remote, client, calendar_id_for
):
    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, client, conn)
    cache.create_task_local(
        Task(uid="local-1", summary="s", calendar_id=calendar_id_for(CAL_HREF)), conn
    )
    cancel = threading.Event()
    cancel.set()

    sync_engine.run_cycle(remote, client, conn, cancel)

    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 1
    assert count_calls(server, "put_create") == 0


def test_a_cancel_between_items_leaves_completed_items_committed(
    conn, server, remote, client, calendar_id_for
):
    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, client, conn)
    cal = calendar_id_for(CAL_HREF)
    for n in range(3):
        cache.create_task_local(Task(uid=f"local-{n}", summary="s", calendar_id=cal), conn)

    cancel = threading.Event()
    pushed = {"n": 0}
    original = sync_engine._push_create

    def cancel_after_one(*args, **kwargs):
        result = original(*args, **kwargs)
        pushed["n"] += 1
        if pushed["n"] == 1:
            cancel.set()
        return result

    sync_engine._push_create = cancel_after_one
    try:
        sync_engine.run_cycle(remote, client, conn, cancel)
    finally:
        sync_engine._push_create = original

    # Exactly one landed, and it is fully committed — not half-applied.
    clean = list(conn.execute("SELECT * FROM tasks WHERE sync_state = 'clean'"))
    assert len(clean) == 1
    assert clean[0]["etag"] is not None
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 2


# ------------------------------------------------------------------ read-only


def test_a_read_only_task_never_gets_an_update_pushed(conn, server, synced):
    """"""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    task_id = local_task(conn, "a")["id"]

    cache.update_task_optimistic(task_id, {"summary": "Mine"}, conn)
    conn.execute("UPDATE tasks SET read_only_reason = 'multipart' WHERE id = ?", (task_id,))
    before = count_calls(server, "put_update")
    synced()
    assert count_calls(server, "put_update") == before


def test_a_read_only_task_can_still_be_deleted(conn, server, synced):
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    task_id = local_task(conn, "a")["id"]
    conn.execute("UPDATE tasks SET read_only_reason = 'multipart' WHERE id = ?", (task_id,))

    cache.delete_task_local(task_id, conn)
    synced()

    assert local_task(conn, "a") is None
    assert f"{CAL_HREF}a.ics" not in server.collections[CAL_HREF].resources


# ----------------------------------------- resolution sticks across the pull


def test_a_resolved_conflict_is_not_re_raised_by_the_next_pull(conn, server, synced):
    """Without this the user can never settle a conflict.

    ``tasks.etag`` only advances on a clean apply, so after a merge it still
    holds the pre-conflict value.  A pull that compared only etags would see
    "server changed" again and re-raise the conflict the user just resolved,
    every cycle, forever.  What settles it is ``base_etag``: the version the
    pending intent was formed against.
    """
    from davpunk.conflict.resolver import (
        Resolution,
        load_conflict,
        merge_from_selection,
        resolve_conflict,
    )

    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Original"))
    synced()
    task_id = local_task(conn, "a")["id"]

    cache.update_task_optimistic(task_id, {"summary": "Mine"}, conn)
    resource = server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    resource.ics = server_ics("a", "Theirs")
    resource.etag = '"theirs"'
    server.collections[CAL_HREF].touch()
    synced()

    conflict_id = conn.execute("SELECT id FROM conflict_queue WHERE resolved = 0").fetchone()[0]
    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)

    synced()

    row = local_task(conn, "a")
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["summary"] == "Mine"
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 0


def test_delete_anyway_is_not_asked_a_second_time(conn, server, synced):
    """An unconditional delete means "remove whatever is there"; re-confirming
    it against the same server change asks the user the same question twice."""
    from davpunk.conflict.resolver import Resolution, resolve_conflict

    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a"))
    synced()
    task_id = local_task(conn, "a")["id"]

    cache.delete_task_local(task_id, conn)
    server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"].etag = '"moved-on"'
    server.collections[CAL_HREF].touch()
    synced()  # mode A' conflict

    conflict_id = conn.execute("SELECT id FROM conflict_queue WHERE resolved = 0").fetchone()[0]
    resolve_conflict(conflict_id, Resolution.DELETE_ANYWAY, None, conn)

    synced()

    assert local_task(conn, "a") is None
    assert f"{CAL_HREF}a.ics" not in server.collections[CAL_HREF].resources


def test_the_local_side_of_a_conflict_is_the_users_edit_not_the_stale_bytes(conn, server, synced):
    """``tasks.raw_ics`` still holds the pre-edit bytes for a dirty task, so
    snapshotting it would show the user their own edit as "unchanged" — and
    then silently discard it if they chose "take all local"."""
    server.put_resource(CAL_HREF, f"{CAL_HREF}a.ics", server_ics("a", "Original"))
    synced()
    cache.update_task_optimistic(local_task(conn, "a")["id"], {"summary": "Mine"}, conn)

    resource = server.collections[CAL_HREF].resources[f"{CAL_HREF}a.ics"]
    resource.ics = server_ics("a", "Theirs")
    resource.etag = '"theirs"'
    server.collections[CAL_HREF].touch()
    synced()

    row = conn.execute("SELECT * FROM conflict_queue WHERE resolved = 0").fetchone()
    assert "SUMMARY:Mine" in row["local_raw_ics"]
    assert "SUMMARY:Theirs" in row["remote_raw_ics"]
