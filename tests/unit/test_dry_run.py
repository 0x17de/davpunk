"""``sync --dry-run``: the same cycle, with both write boundaries sealed.

The point of these tests is not that the report is pretty. It is that the two
guarantees hold — nothing reaches the server, nothing reaches the cache — and
that they hold because of *where* they are enforced rather than because every
write site remembered to check a flag.
"""

from __future__ import annotations

import hashlib

import pytest

from davpunk.core import cache, dry_run, sync_engine
from davpunk.core.caldav_client import Unauthorized
from davpunk.core.ical_parser import new_resource
from davpunk.models.remote import Remote
from davpunk.models.task import Status, SyncState, Task
from tests.fake_caldav import FakeCalDAVServer, FakeClient

CAL_HREF = "/dav/work/tasks/"
#: The fake server keys resources by absolute href, as a real one reports them.
RES = f"{CAL_HREF}srv-1.ics"


@pytest.fixture
def remote():
    return Remote(id="work", name="Work", url="https://cal.example.test/dav/")


class _StubStore:
    """Stands in for whichever backend the remote names, so a dry run does not
    need a keyring or a warm gpg-agent to be tested."""

    def __init__(self, secret: str) -> None:
        self.secret = secret

    def load(self) -> str:
        return self.secret


def _stub_credential(monkeypatch, secret: str = "password") -> None:
    from davpunk.core import secret_store

    monkeypatch.setattr(secret_store, "for_remote", lambda _remote: _StubStore(secret))


@pytest.fixture
def server():
    fake = FakeCalDAVServer()
    fake.add_collection(CAL_HREF, display_name="Tasks")
    return fake


@pytest.fixture
def plan(conn, remote, server, db_path, monkeypatch):
    """A dry run wired to the fake server, with the credential step stubbed."""
    _stub_credential(monkeypatch)
    cache.reconcile_remotes([remote], conn)
    sync_engine.discover_calendars(remote, FakeClient(server), conn)
    # The shared `calendar_id` fixture seeds a ctag, and the fake server's
    # happens to start at the same value — which would short-circuit the pull
    # for a reason that has nothing to do with what is being tested.
    with cache.tx(conn):
        conn.execute("UPDATE calendars SET ctag = 'ctag-unseen'")

    def _plan():
        # Commit and let go of the write lock: the copy is read through a
        # separate connection, exactly as it is in production.
        conn.commit()
        return dry_run.plan(
            remote, db_path, client_factory=lambda _remote, _credential: FakeClient(server)
        )

    return _plan


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def server_ics(uid: str, summary: str = "From the server", **kwargs) -> str:
    return new_resource(Task(uid=uid, summary=summary, **kwargs))


def cal_id() -> str:
    from davpunk.models.calendar import calendar_id

    return calendar_id("work", CAL_HREF)


@pytest.fixture
def in_sync(conn, server):
    """A resource that exists on both sides with the *same* ETag.

    Seeding the local row with an ETag the server never issued makes every
    later assertion a conflict test by accident.
    """

    def _seed(uid: str = "srv-1", **kwargs):
        resource = server.put_resource(CAL_HREF, f"{CAL_HREF}{uid}.ics", server_ics(uid, **kwargs))
        task = Task(uid=uid, calendar_id=cal_id(), href=f"{uid}.ics", **kwargs)
        task.raw_ics = resource.ics
        with cache.tx(conn):
            return cache.insert_server_task(task, resource.etag, conn)

    return _seed


# ------------------------------------------------------------- the guarantees


def test_the_server_is_never_written_to(plan, server, conn, make_task):
    """A pending create is reported, not sent."""
    cache.create_task_local(make_task("local-1", summary="Only local", calendar_id=cal_id()), conn)

    report = plan()

    assert server.resource_count() == 0
    assert not [c for c in server.calls if c[0] in ("put_create", "put_update", "delete")]
    assert [w.verb for w in report.writes] == ["CREATE"]
    assert report.writes[0].summary == "Only local"


def test_the_cache_file_is_byte_identical_afterwards(plan, server, db_path, conn):
    """The strongest form of the promise, and the one the user cares about.

    The server has a task the cache has never seen, so a real sync would write
    plenty.  Nothing may land in the file.
    """
    server.put_resource(CAL_HREF, RES, server_ics("srv-1"))
    conn.commit()
    before = digest(db_path)

    report = plan()

    assert digest(db_path) == before
    assert [c.summary for c in report.local.added] == ["From the server"]


def test_local_rows_are_unchanged_even_where_the_pull_would_rewrite_them(
    plan, server, conn, in_sync
):
    """Byte-identity is checked above; this checks the rows, not the file."""
    task_id = in_sync("srv-1", summary="As cached")
    server.put_resource(CAL_HREF, RES, server_ics("srv-1", summary="Changed upstream"))
    conn.commit()

    report = plan()

    assert cache.get_task(task_id, conn).summary == "As cached"
    assert [c.summary for c in report.local.updated] == ["Changed upstream"]
    assert "summary" in report.local.updated[0].what


def test_a_pending_change_is_still_pending_afterwards(plan, conn, make_task):
    """A dry run that consumed the queue would be worse than no dry run."""
    task_id = cache.create_task_local(make_task("local-1", calendar_id=cal_id()), conn)
    conn.commit()

    plan()

    assert cache.ready_pending_changes(cal_id(), conn)
    assert cache.get_task(task_id, conn).sync_state is SyncState.NEW


# ------------------------------------------------------------------ the report


def test_a_deletion_is_reported_as_a_delete(plan, server, conn, in_sync):
    task_id = in_sync("srv-1")
    cache.delete_task_local(task_id, conn)
    conn.commit()

    report = plan()

    assert [w.verb for w in report.writes] == ["DELETE"]
    assert server.resource_count() == 1


def test_an_edit_is_reported_as_an_update_with_its_size(plan, server, conn, in_sync):
    task_id = in_sync("srv-1")
    cache.update_task_optimistic(task_id, {"summary": "Edited here"}, conn)
    conn.commit()

    report = plan()

    [write] = report.writes
    assert write.verb == "UPDATE"
    assert write.summary == "Edited here"
    assert write.bytes > 0


def test_a_removal_on_the_server_is_reported_as_a_local_removal(plan, server, conn, in_sync):
    in_sync("srv-1", summary="Going away")
    server.collections[CAL_HREF].resources.clear()
    server.collections[CAL_HREF].touch()
    conn.commit()

    report = plan()

    assert [c.summary for c in report.local.removed] == ["Going away"]


def test_a_conflict_is_called_a_conflict(plan, server, conn, in_sync):
    """Both sides moved: the user must see this before it happens."""
    task_id = in_sync("srv-1", summary="Mine")
    server.put_resource(CAL_HREF, RES, server_ics("srv-1", summary="Theirs"))
    cache.update_task_optimistic(task_id, {"summary": "Mine, edited"}, conn)
    conn.commit()

    report = plan()

    assert report.local.conflicted
    assert cache.get_task(task_id, conn).sync_state is not SyncState.CONFLICT


def test_nothing_to_do_says_so(plan):
    report = plan()
    assert not report.would_write
    assert "nothing" in report.summary()


def test_the_summary_caps_a_long_list_and_says_how_many_it_dropped(plan, conn, make_task):
    """A silent truncation would read as "that is all of it"."""
    for index in range(5):
        cache.create_task_local(make_task(f"local-{index}", calendar_id=cal_id()), conn)
    conn.commit()

    report = plan()

    assert len(report.writes) == 5
    assert "… and 3 more" in report.summary(limit=2)


# ------------------------------------------------------- reads are still real


def test_a_read_failure_surfaces_rather_than_being_simulated_away(
    conn, remote, server, db_path, monkeypatch
):
    """Reads go to the network, so a bad password is found by a dry run."""
    _stub_credential(monkeypatch)
    cache.reconcile_remotes([remote], conn)
    conn.commit()

    class Rejecting(FakeClient):
        def discover(self):
            raise Unauthorized("401")

    report = dry_run.plan(remote, db_path, client_factory=lambda _r, _c: Rejecting(server))

    assert report.result.error
    assert "FAILED" in report.summary()


# ------------------------------------------------------------ the proxy itself


def test_the_proxy_forwards_everything_except_the_three_writes(server):
    writes = []
    client = dry_run.ReadOnlyClient(FakeClient(server), writes)

    assert client.discover()  # a read reaches the inner client
    assert not writes

    client.put_create("/dav/work/tasks/a.ics", "BEGIN:VCALENDAR\r\nUID:a\r\nEND:VCALENDAR\r\n")
    client.put_update("/dav/work/tasks/b.ics", "BEGIN:VCALENDAR\r\nUID:b\r\nEND:VCALENDAR\r\n", "e")
    client.delete("/dav/work/tasks/c.ics", "e")

    assert [w.verb for w in writes] == ["CREATE", "UPDATE", "DELETE"]
    assert [w.uid for w in writes] == ["a", "b", ""]
    assert server.resource_count() == 0


def test_the_proxy_returns_a_plausible_success_so_the_engine_takes_its_normal_path(server):
    """A synthetic failure would exercise error handling a real sync would not."""
    client = dry_run.ReadOnlyClient(FakeClient(server), [])
    assert client.put_create("/x.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n").status == 201
    assert client.put_update("/x.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "e").etag


def test_a_status_only_edit_is_reported_by_the_field_that_moved(plan, server, conn, in_sync):
    task_id = in_sync("srv-1")
    server.put_resource(CAL_HREF, RES, server_ics("srv-1", status=Status.COMPLETED))
    conn.commit()

    report = plan()

    assert "status" in report.local.updated[0].what
    assert cache.get_task(task_id, conn).status is not Status.COMPLETED


def test_a_matching_ctag_short_circuits_exactly_as_a_real_sync_would(plan, server, conn, in_sync):
    """The dry run must not report work a real sync would skip."""
    in_sync("srv-1")
    with cache.tx(conn):
        conn.execute("UPDATE calendars SET ctag = ?", (server.collections[CAL_HREF].ctag,))
    conn.commit()

    report = plan()

    assert report.local.total == 0
    assert not report.writes
    assert ("list", CAL_HREF) not in server.calls


def test_rows_still_sitting_in_the_wal_are_part_of_the_copy(plan, conn, make_task, db_path):
    """``VACUUM INTO``, not a file copy.

    The cache runs in WAL mode, so a committed row is often still only in the
    ``-wal`` sidecar.  Copying the ``.sqlite3`` alone would miss it, and the
    dry run would confidently report an empty cache about to be filled.
    """
    cache.create_task_local(make_task("local-1", summary="Fresh", calendar_id=cal_id()), conn)
    conn.commit()
    assert db_path.with_name(db_path.name + "-wal").exists()

    report = plan()

    # The pending CREATE is only visible if the copy carried the WAL across.
    assert [w.summary for w in report.writes] == ["Fresh"]


def test_the_report_says_when_a_collection_was_not_even_looked_at(plan, server, conn, in_sync):
    """ "Nothing to do" and "I did not check" must not read the same."""
    in_sync("srv-1")
    with cache.tx(conn):
        conn.execute("UPDATE calendars SET ctag = ?", (server.collections[CAL_HREF].ctag,))
    conn.commit()

    summary = plan().summary()

    assert "1 of them unchanged since the last sync" in summary
    assert "ctag" in summary
