"""Shared fixtures.

Every test that touches the filesystem is relocated by ``DAVPUNK_HOME`` so no
test can reach the developer's real cache, config or credentials.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from davpunk.core import cache
from davpunk.models.task import Status, Task


@pytest.fixture(autouse=True)
def davpunk_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DAVPUNK_HOME", str(home))
    return home


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "davpunk.db"


@pytest.fixture
def conn(db_path) -> sqlite3.Connection:
    connection = cache.open_db(db_path)
    yield connection
    connection.close()


@pytest.fixture
def remote_id(conn) -> str:
    """A configured remote, reconciled the way every process does at startup."""
    from davpunk.models.remote import Remote

    cache.reconcile_remotes([Remote(id="work", name="Work", url="https://example.test/dav/")], conn)
    return "work"


@pytest.fixture
def calendar_id(conn, remote_id) -> str:
    with cache.tx(conn):
        return cache.upsert_calendar(
            remote_id, "/dav/work/tasks/", conn, display_name="Tasks", ctag="ctag-1"
        )


@pytest.fixture
def other_calendar_id(conn, remote_id) -> str:
    with cache.tx(conn):
        return cache.upsert_calendar(remote_id, "/dav/work/other/", conn, display_name="Other")


@pytest.fixture
def calendar_id_for():
    """The deterministic id of a calendar href on the ``work`` remote.

    Deterministic, so a test can name a calendar before discovery has run.
    """
    from davpunk.models.calendar import calendar_id as compute

    def _for(href: str, remote: str = "work") -> str:
        return compute(remote, href)

    return _for


@pytest.fixture
def make_task(calendar_id):
    """Build an unsaved Task in the default calendar."""

    def _make(uid: str = "task-1", **kwargs) -> Task:
        kwargs.setdefault("summary", f"Task {uid}")
        kwargs.setdefault("status", Status.NEEDS_ACTION)
        kwargs.setdefault("calendar_id", calendar_id)
        return Task(uid=uid, **kwargs)

    return _make


@pytest.fixture
def synced_task(conn, make_task):
    """A task that already exists on the server: clean, with an ETag."""

    def _synced(uid: str = "srv-1", etag: str = 'W/"v1"', **kwargs):
        task = make_task(uid, **kwargs)
        task.href = f"{uid}.ics"
        task.raw_ics = (
            f"BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:{uid}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
        )
        with cache.tx(conn):
            return cache.insert_server_task(task, etag, conn)

    return _synced


@pytest.fixture
def frozen_now(monkeypatch):
    """A controllable clock for backoff and TTL assertions."""

    state = {"t": 1_700_000_000}

    def _now() -> int:
        return state["t"]

    monkeypatch.setattr(cache, "now", _now)

    class Clock:
        @property
        def value(self) -> int:
            return state["t"]

        def advance(self, seconds: int) -> int:
            state["t"] += seconds
            return state["t"]

        def set(self, value: int) -> int:
            state["t"] = value
            return value

    return Clock()
