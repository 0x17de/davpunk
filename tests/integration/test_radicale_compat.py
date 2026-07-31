"""Full round-trip against a real Radicale.  [Step 17]

Everything else mocks the wire.  This is the only place where the XML DavPunk
sends is parsed by a real CalDAV server and the ICS it writes is stored and
handed back — so it is the only place that can catch a discovery request
Radicale rejects, an ``If-None-Match`` it honours differently, or a resource
that comes back re-encoded.

Skipped automatically when ``radicale`` is not installed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from davpunk.core import cache, sync_engine
from davpunk.core.caldav_client import CalDAVClient
from davpunk.core.ical_parser import new_resource, parse_resource
from davpunk.models.remote import Remote
from davpunk.models.task import Status, SyncState, Task

radicale = pytest.importorskip("radicale", reason="radicale is not installed")

pytestmark = pytest.mark.radicale

USER = "davpunk"
PASSWORD = "hunter2"
CAL_NAME = "tasks"
OTHER_NAME = "other"

MULTIPART = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Another Client//EN\r\n"
    "BEGIN:VTIMEZONE\r\n"
    "TZID:Europe/Berlin\r\n"
    "BEGIN:STANDARD\r\n"
    "DTSTART:19701025T030000\r\n"
    "TZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0100\r\n"
    "TZNAME:CET\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE\r\n"
    "BEGIN:VTODO\r\n"
    "UID:series-1\r\n"
    "SUMMARY:Weekly report\r\n"
    "DTSTART;TZID=Europe/Berlin:20260706T090000\r\n"
    "DUE;TZID=Europe/Berlin:20260706T170000\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
    "STATUS:NEEDS-ACTION\r\n"
    "X-FOREIGN-PROP:preserve me\r\n"
    "END:VTODO\r\n"
    "BEGIN:VTODO\r\n"
    "UID:series-1\r\n"
    "RECURRENCE-ID;TZID=Europe/Berlin:20260713T090000\r\n"
    "SUMMARY:Weekly report (moved)\r\n"
    "DTSTART;TZID=Europe/Berlin:20260714T090000\r\n"
    "DUE;TZID=Europe/Berlin:20260714T170000\r\n"
    "STATUS:COMPLETED\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def radicale_server(tmp_path_factory):
    """A real Radicale on loopback, with one user and two task collections."""
    root = tmp_path_factory.mktemp("radicale")
    collections = root / "collections"
    collections.mkdir()

    # htpasswd, plain scheme — this server exists for the length of one test
    # module and listens only on loopback.
    users = root / "users"
    users.write_text(f"{USER}:{PASSWORD}\n")

    config = root / "config"
    port = _free_port()
    config.write_text(
        textwrap.dedent(f"""
        [server]
        hosts = 127.0.0.1:{port}

        [auth]
        type = htpasswd
        htpasswd_filename = {users}
        htpasswd_encryption = plain

        [storage]
        filesystem_folder = {collections}

        [logging]
        level = error
        """).strip()
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "radicale", "--config", str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"radicale exited: {process.stderr.read().decode()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        raise RuntimeError("radicale did not start within 30s")

    _create_collections(base)
    try:
        yield base
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()


def _create_collections(base: str) -> None:
    import requests
    from requests.auth import HTTPBasicAuth

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<mkcol xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<set><prop><resourcetype><collection/><c:calendar/></resourcetype>"
        '<c:supported-calendar-component-set><c:comp name="VTODO"/>'
        "</c:supported-calendar-component-set>"
        "<displayname>{name}</displayname>"
        "</prop></set></mkcol>"
    )
    auth = HTTPBasicAuth(USER, PASSWORD)
    for name in (CAL_NAME, OTHER_NAME):
        response = requests.request(
            "MKCOL",
            f"{base}/{USER}/{name}/",
            data=body.format(name=name).encode(),
            headers={"Content-Type": "application/xml"},
            auth=auth,
            timeout=10,
        )
        assert response.status_code in (201, 405), response.text


@pytest.fixture(autouse=True)
def empty_collections(radicale_server):
    """Start every test with empty collections.

    The server is module-scoped because spawning Radicale per test would
    dominate the runtime, so isolation has to come from clearing its contents
    instead — otherwise a count assertion picks up whatever the previous test
    left behind.
    """
    _clear(radicale_server)
    yield
    _clear(radicale_server)


def _clear(base: str) -> None:
    import requests
    from requests.auth import HTTPBasicAuth

    auth = HTTPBasicAuth(USER, PASSWORD)
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR"/></c:filter></c:calendar-query>'
    )
    for name in (CAL_NAME, OTHER_NAME):
        response = requests.request(
            "REPORT",
            f"{base}/{USER}/{name}/",
            data=body.encode(),
            headers={"Depth": "1", "Content-Type": "application/xml"},
            auth=auth,
            timeout=10,
        )
        if response.status_code != 207:
            continue
        from xml.etree import ElementTree as ET

        for href in ET.fromstring(response.content).findall(".//{DAV:}href"):
            if href.text and href.text.endswith(".ics"):
                requests.delete(f"{base}{href.text}", auth=auth, timeout=10)


@pytest.fixture
def remote(radicale_server):
    return Remote(
        id="radicale",
        name="Radicale",
        url=f"{radicale_server}/{USER}/",
        username=USER,
        allow_insecure=True,  # loopback http:// for the duration of a test
    )


@pytest.fixture
def client(remote):
    c = CalDAVClient(remote, PASSWORD)
    yield c
    c.close()


@pytest.fixture
def sync(conn, remote, client):
    def _run(**kwargs):
        return sync_engine.run_cycle(remote, client, conn, **kwargs)

    cache.reconcile_remotes([remote], conn)
    return _run


@pytest.fixture
def calendars(conn, remote, client, sync):
    """``{name: calendar_id}`` after a real discovery."""
    rows = sync_engine.discover_calendars(remote, client, conn)
    return {row["href"].rstrip("/").rsplit("/", 1)[-1]: row["id"] for row in rows}


def task_by_uid(conn, uid: str):
    rows = list(conn.execute("SELECT * FROM tasks WHERE uid = ?", (uid,)))
    return rows[0] if rows else None


def put_raw(client: CalDAVClient, remote: Remote, name: str, href: str, ics: str) -> None:
    """Seed a resource the way another client would have created it."""
    result = client.put_create(f"/{USER}/{name}/{href}", ics)
    assert result.status in (200, 201, 204)


# ------------------------------------------------------------------ discovery


def test_discovery_finds_the_vtodo_collections(calendars):
    assert CAL_NAME in calendars
    assert OTHER_NAME in calendars


def test_calendar_ids_are_deterministic(conn, remote, client, sync):
    first = {row["id"] for row in sync_engine.discover_calendars(remote, client, conn)}
    conn.execute("DELETE FROM calendars")
    second = {row["id"] for row in sync_engine.discover_calendars(remote, client, conn)}
    assert first == second


# ----------------------------------------------------------------- round trip


def test_create_sync_modify_delete(conn, sync, calendars, client):
    task_id = cache.create_task_local(
        Task(
            uid="rt-1",
            calendar_id=calendars[CAL_NAME],
            summary="Round trip",
            status=Status.NEEDS_ACTION,
            priority=2,
            due_value="20260731",
        ),
        conn,
    )

    sync()
    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["etag"], "Radicale returned no ETag on create"

    # The server really has it, with the fields we sent.
    fetched = client.get(f"/{USER}/{CAL_NAME}/rt-1.ics")
    parsed = parse_resource(fetched.ics)
    assert parsed.summary == "Round trip"
    assert parsed.priority == 2
    assert parsed.due_value == "20260731"

    cache.update_task_optimistic(task_id, {"status": Status.COMPLETED}, conn)
    sync()
    parsed = parse_resource(client.get(f"/{USER}/{CAL_NAME}/rt-1.ics").ics)
    assert parsed.status is Status.COMPLETED
    assert parsed.percent_complete == 100  # canonicalized

    cache.delete_task_local(task_id, conn)
    sync()
    assert task_by_uid(conn, "rt-1") is None
    from davpunk.core.caldav_client import NotFound

    with pytest.raises(NotFound):
        client.get(f"/{USER}/{CAL_NAME}/rt-1.ics")


def test_a_server_side_task_is_imported(conn, sync, calendars, client, remote):
    put_raw(
        client,
        remote,
        CAL_NAME,
        "server-made.ics",
        new_resource(Task(uid="server-made", summary="Made elsewhere")),
    )
    sync()
    assert task_by_uid(conn, "server-made")["summary"] == "Made elsewhere"


# ------------------------------------------------------------------- guards


def test_if_none_match_star_is_honoured(client, remote, calendars):
    """The create's collision signal has to be 412 against a real server."""
    from davpunk.core.caldav_client import PreconditionFailed

    href = f"/{USER}/{CAL_NAME}/guard-1.ics"
    client.put_create(href, new_resource(Task(uid="guard-1", summary="First")))

    with pytest.raises(PreconditionFailed):
        client.put_create(href, new_resource(Task(uid="guard-1", summary="Second")))


def test_if_match_guards_an_update(client, calendars):
    from davpunk.core.caldav_client import PreconditionFailed

    href = f"/{USER}/{CAL_NAME}/guard-2.ics"
    created = client.put_create(href, new_resource(Task(uid="guard-2", summary="v1")))
    etag = created.etag or client.get_etag(f"/{USER}/{CAL_NAME}/", href)

    ok = client.put_update(href, new_resource(Task(uid="guard-2", summary="v2")), etag)
    assert ok.status in (200, 204)

    with pytest.raises(PreconditionFailed):
        client.put_update(href, new_resource(Task(uid="guard-2", summary="v3")), etag)


def test_a_guarded_delete_is_refused_with_a_stale_etag(client, calendars):
    from davpunk.core.caldav_client import PreconditionFailed

    href = f"/{USER}/{CAL_NAME}/guard-3.ics"
    created = client.put_create(href, new_resource(Task(uid="guard-3", summary="v1")))
    stale = created.etag or client.get_etag(f"/{USER}/{CAL_NAME}/", href)
    client.put_update(href, new_resource(Task(uid="guard-3", summary="v2")), stale)

    with pytest.raises(PreconditionFailed):
        client.delete(href, stale)


def test_deleting_something_already_gone_is_success(client, calendars):
    """Both sides agree it is gone."""
    assert client.delete(f"/{USER}/{CAL_NAME}/never-existed.ics", None).status in (204, 404)


# --------------------------------------------------------------- enumeration


def test_multiget_batches_against_a_real_server(conn, sync, calendars, client, remote):
    """120 resources are three batches, not 120 GETs."""
    for n in range(120):
        put_raw(
            client,
            remote,
            CAL_NAME,
            f"bulk-{n:03d}.ics",
            new_resource(Task(uid=f"bulk-{n:03d}", summary=f"Bulk {n}")),
        )

    pull, _ = sync()
    assert pull.imported == 120
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 120


def test_the_ctag_short_circuits_an_idle_cycle(conn, sync, calendars, client, remote):
    put_raw(client, remote, CAL_NAME, "ctag-1.ics", new_resource(Task(uid="ctag-1", summary="s")))
    sync()
    before = cache.calendar_rows(conn)
    sync()
    after = cache.calendar_rows(conn)
    assert [r["ctag"] for r in before] == [r["ctag"] for r in after]


def test_a_vevent_in_the_collection_is_ignored(conn, sync, calendars, client):
    """Mixed collections exist."""
    client.put_create(
        f"/{USER}/{CAL_NAME}/an-event.ics",
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\n"
        "BEGIN:VEVENT\r\nUID:evt-1\r\nDTSTAMP:20260701T090000Z\r\n"
        "DTSTART:20260701T090000Z\r\nSUMMARY:A meeting\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    sync()
    assert task_by_uid(conn, "evt-1") is None


# ------------------------------------------------------------------ fidelity


def test_x_davpunk_properties_survive_the_server(conn, sync, calendars, client):
    task_id = cache.create_task_local(
        Task(
            uid="xprops-1",
            calendar_id=calendars[CAL_NAME],
            summary="Ordered",
            davpunk_order=3000,
            kanban_col="inprogress",
        ),
        conn,
    )
    sync()

    stored = client.get(f"/{USER}/{CAL_NAME}/xprops-1.ics").ics
    assert "X-DAVPUNK-ORDER:3000" in stored
    assert "X-DAVPUNK-KANBAN-COL:inprogress" in stored

    reparsed = parse_resource(stored)
    assert reparsed.davpunk_order == 3000
    assert reparsed.kanban_col == "inprogress"
    assert cache.get_task_row(task_id, conn)["kanban_col"] == "inprogress"


def test_a_tzid_due_round_trips_through_the_server(conn, sync, calendars, client):
    cache.create_task_local(
        Task(
            uid="tz-1",
            calendar_id=calendars[CAL_NAME],
            summary="Timezoned",
            due_value="20260731T170000",
            due_tzid="Europe/Berlin",
        ),
        conn,
    )
    sync()

    parsed = parse_resource(client.get(f"/{USER}/{CAL_NAME}/tz-1.ics").ics)
    assert parsed.due_value == "20260731T170000"
    assert parsed.due_tzid == "Europe/Berlin"


def test_a_date_due_keeps_its_value_type(conn, sync, calendars, client):
    cache.create_task_local(
        Task(
            uid="date-1",
            calendar_id=calendars[CAL_NAME],
            summary="All day",
            due_value="20260731",
        ),
        conn,
    )
    sync()

    stored = client.get(f"/{USER}/{CAL_NAME}/date-1.ics").ics
    assert "DUE;VALUE=DATE:20260731" in stored
    assert parse_resource(stored).due_value == "20260731"


def test_a_multibyte_summary_survives(conn, sync, calendars, client):
    summary = "日本語のタスク — with an em dash and a → arrow"
    cache.create_task_local(
        Task(uid="utf8-1", calendar_id=calendars[CAL_NAME], summary=summary), conn
    )
    sync()
    assert parse_resource(client.get(f"/{USER}/{CAL_NAME}/utf8-1.ics").ics).summary == summary


# ----------------------------------------------------------------- multipart


def test_a_recurring_resource_survives_a_full_cycle_byte_identical(
    conn, sync, calendars, client, remote
):
    """The whole justification for the read-only quarantine."""
    put_raw(client, remote, CAL_NAME, "series-1.ics", MULTIPART)
    sync()

    task_id = task_by_uid(conn, "series-1")["id"]
    assert cache.get_task_row(task_id, conn)["read_only_reason"] == "multipart"
    assert cache.get_task_row(task_id, conn)["summary"] == "Weekly report"  # the master

    sync()
    sync()

    stored = client.get(f"/{USER}/{CAL_NAME}/series-1.ics").ics
    assert stored.count("BEGIN:VTODO") == 2
    assert "RECURRENCE-ID" in stored
    assert "X-FOREIGN-PROP:preserve me" in stored
    assert "BEGIN:VTIMEZONE" in stored


def test_a_quarantined_resource_can_still_be_deleted(conn, sync, calendars, client, remote):
    put_raw(client, remote, CAL_NAME, "series-2.ics", MULTIPART.replace("series-1", "series-2"))
    sync()

    task_id = task_by_uid(conn, "series-2")["id"]
    cache.delete_task_local(task_id, conn)
    sync()

    from davpunk.core.caldav_client import NotFound

    assert task_by_uid(conn, "series-2") is None
    with pytest.raises(NotFound):
        client.get(f"/{USER}/{CAL_NAME}/series-2.ics")


# ---------------------------------------------------------------------- move


def test_a_move_across_real_collections(conn, sync, calendars, client, remote):
    put_raw(client, remote, CAL_NAME, "mv-1.ics", new_resource(Task(uid="mv-1", summary="Moving")))
    sync()
    task_id = task_by_uid(conn, "mv-1")["id"]

    cache.move_task_local(task_id, calendars[OTHER_NAME], conn)
    sync()

    from davpunk.core.caldav_client import NotFound

    assert client.get(f"/{USER}/{OTHER_NAME}/mv-1.ics").ics
    with pytest.raises(NotFound):
        client.get(f"/{USER}/{CAL_NAME}/mv-1.ics")

    row = cache.get_task_row(task_id, conn)
    assert row["calendar_id"] == calendars[OTHER_NAME]
    assert row["sync_state"] == SyncState.CLEAN.value
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE uid = 'mv-1'").fetchone()[0] == 1


def test_moving_a_quarantined_resource_keeps_it_byte_identical(
    conn, sync, calendars, client, remote
):
    """A move relocates the bytes and never re-serializes."""
    ics = MULTIPART.replace("series-1", "series-3")
    put_raw(client, remote, CAL_NAME, "series-3.ics", ics)
    sync()

    task_id = task_by_uid(conn, "series-3")["id"]
    cache.move_task_local(task_id, calendars[OTHER_NAME], conn)
    sync()

    moved = client.get(f"/{USER}/{OTHER_NAME}/series-3.ics").ics
    assert moved.count("BEGIN:VTODO") == 2
    assert "X-FOREIGN-PROP:preserve me" in moved
    assert "BEGIN:VTIMEZONE" in moved


# --------------------------------------------------------------- convergence


def test_a_tombstone_re_deletes_a_replayed_task(conn, sync, calendars, client, remote):
    """A stale client replaying a delete, against a real server."""
    put_raw(
        client, remote, CAL_NAME, "tomb-1.ics", new_resource(Task(uid="tomb-1", summary="Gone"))
    )
    sync()
    cache.delete_task_local(task_by_uid(conn, "tomb-1")["id"], conn)
    sync()

    # Another client puts it back, on a different href — the common case.
    put_raw(
        client,
        remote,
        CAL_NAME,
        "tomb-1-replay.ics",
        new_resource(Task(uid="tomb-1", summary="Gone")),
    )
    sync()

    from davpunk.core.caldav_client import NotFound

    assert task_by_uid(conn, "tomb-1") is None
    with pytest.raises(NotFound):
        client.get(f"/{USER}/{CAL_NAME}/tomb-1-replay.ics")


def test_a_conflict_is_detected_and_can_be_resolved(conn, sync, calendars, client, remote):
    from davpunk.conflict.resolver import Resolution, load_conflict, resolve_conflict

    put_raw(
        client, remote, CAL_NAME, "cf-1.ics", new_resource(Task(uid="cf-1", summary="Original"))
    )
    sync()
    task_id = task_by_uid(conn, "cf-1")["id"]

    cache.update_task_optimistic(task_id, {"summary": "Mine"}, conn)
    # Another client writes first.
    href = f"/{USER}/{CAL_NAME}/cf-1.ics"
    current = client.get(href)
    client.put_update(href, new_resource(Task(uid="cf-1", summary="Theirs")), current.etag)

    sync()
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CONFLICT.value

    conflict_id = conn.execute("SELECT id FROM conflict_queue WHERE resolved = 0").fetchone()[0]
    view = load_conflict(conflict_id, conn)
    assert view.local.summary == "Mine"
    assert view.remote.summary == "Theirs"

    resolve_conflict(conflict_id, Resolution.RESTORE_SERVER, None, conn)
    sync()

    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["summary"] == "Theirs"


def test_subtasks_survive_the_server(conn, sync, calendars, client):
    parent = cache.create_task_local(
        Task(uid="p-1", calendar_id=calendars[CAL_NAME], summary="Parent"), conn
    )
    cache.create_task_local(
        Task(uid="c-1", calendar_id=calendars[CAL_NAME], summary="Child", parent_uid="p-1"), conn
    )
    sync()

    stored = client.get(f"/{USER}/{CAL_NAME}/c-1.ics").ics
    assert "RELATED-TO;RELTYPE=PARENT:p-1" in stored
    assert parse_resource(stored).parent_uid == "p-1"

    # Deleting the parent promotes the child to root, and the server sees it.
    cache.delete_task_local(parent, conn)
    sync()
    assert "RELATED-TO" not in client.get(f"/{USER}/{CAL_NAME}/c-1.ics").ics
