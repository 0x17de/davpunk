"""``davpunk sync --dry-run``: what a sync would do, without doing any of it.

The guarantee is enforced at the two boundaries a sync can write through, not
by a flag threaded through the engine:

* **The server.**  :class:`ReadOnlyClient` wraps the real client.  Reads —
  discovery, ctag, enumeration, multiget, GET — go to the network untouched, so
  authentication, a missing collection or an unparseable resource surface for
  real.  ``put_create``, ``put_update`` and ``delete`` never reach it: they
  record what would have been sent and return a synthetic success.

* **The cache.**  The cycle runs against a throwaway *copy* of the database,
  made with ``VACUUM INTO``.  The real file is never opened for writing.

Which means the dry run executes **the same code path as a real sync** —
``SyncRunner``, the same lock, the same ``sync_engine``.  A separate planner
would be a second implementation of the decision rules, free to drift from the
one that actually runs; a ``dry_run=True`` flag would be only as good as the
one write site somebody forgets.  Here, being read-only is structural.

What it cannot tell you is how the server will answer a write it never sent: a
412 collision, a quota rejection or an ETag the server rewrites are only
knowable by writing.  Everything reported about the *pull* side is real.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from davpunk.core import cache
from davpunk.core.caldav_client import WriteResult
from davpunk.core.sync_runner import SyncResult, SyncRunner
from davpunk.models.remote import Remote

log = logging.getLogger("davpunk.core.dry_run")

#: What the synthetic write responses claim.  A 201/204 with an ETag is what a
#: compliant server returns, so the engine's bookkeeping runs its normal path
#: rather than an error path it would not take in production.
_DRY_ETAG = 'W/"dry-run"'


@dataclass(frozen=True)
class PlannedWrite:
    """One request the sync would have sent to the server."""

    verb: str
    href: str
    bytes: int = 0
    uid: str = ""
    summary: str = ""

    def describe(self) -> str:
        what = self.summary or self.uid or self.href
        size = f", {self.bytes} bytes" if self.bytes else ""
        return f"{self.verb:6} {what}  ({self.href}{size})"


class ReadOnlyClient:
    """Passes reads through; records writes instead of sending them."""

    def __init__(self, inner: Any, writes: list[PlannedWrite]) -> None:
        self._inner = inner
        self.writes = writes

    def __getattr__(self, name: str) -> Any:
        # Only the three mutating methods are overridden below, so anything
        # reaching here is a read and is meant to hit the network.
        return getattr(self._inner, name)

    # ------------------------------------------------------------- intercepted

    def put_create(self, href: str, ics: str) -> WriteResult:
        self._record("CREATE", href, ics)
        return WriteResult(status=201, etag=_DRY_ETAG)

    def put_update(self, href: str, ics: str, base_etag: str) -> WriteResult:
        self._record("UPDATE", href, ics)
        return WriteResult(status=204, etag=_DRY_ETAG)

    def delete(self, href: str, base_etag: str | None = None) -> WriteResult:
        self._record("DELETE", href, "")
        return WriteResult(status=204)

    def _record(self, verb: str, href: str, ics: str) -> None:
        self.writes.append(
            PlannedWrite(
                verb=verb,
                href=href,
                bytes=len(ics.encode("utf-8")) if ics else 0,
                uid=_field(ics, "UID"),
                summary=_field(ics, "SUMMARY"),
            )
        )


def _field(ics: str, name: str) -> str:
    """Pull one property out of the ICS for the report.

    Deliberately not the full parser: this is label text, and a resource too
    malformed to parse is exactly one the user wants named in the report rather
    than swallowed by an exception.
    """
    for line in ics.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return ""


# ------------------------------------------------------------------- local diff


@dataclass
class LocalChange:
    uid: str
    summary: str
    calendar_id: str
    what: str


@dataclass
class LocalDiff:
    """How the cache would differ afterwards."""

    added: list[LocalChange] = field(default_factory=list)
    updated: list[LocalChange] = field(default_factory=list)
    removed: list[LocalChange] = field(default_factory=list)
    conflicted: list[LocalChange] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.updated) + len(self.removed) + len(self.conflicted)


#: The columns whose change is worth reporting.  ``etag`` and ``sync_state``
#: move on almost every row of a real pull without the task itself differing,
#: and listing those would bury the changes a user actually cares about.
_COMPARED = (
    "summary",
    "description",
    "status",
    "priority",
    "percent_complete",
    "due_value",
    "due_tzid",
    "dtstart_value",
    "dtstart_tzid",
    "completed",
    "parent_uid",
    "raw_ics",
)


def _snapshot(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT calendar_id, uid, summary, sync_state, " + ", ".join(_COMPARED) + " FROM tasks"
    )
    return {(row["calendar_id"], row["uid"]): row for row in rows}


def _diff(before: dict, after: dict) -> LocalDiff:
    diff = LocalDiff()

    for key, row in after.items():
        entry = LocalChange(uid=key[1], summary=row["summary"] or "", calendar_id=key[0], what="")
        old = before.get(key)
        if old is None:
            diff.added.append(entry)
            continue
        changed = [name for name in _COMPARED if old[name] != row[name]]
        if changed:
            entry.what = ", ".join(n for n in changed if n != "raw_ics") or "contents"
            diff.updated.append(entry)
        if row["sync_state"] == "conflict" and old["sync_state"] != "conflict":
            diff.conflicted.append(entry)

    for key, row in before.items():
        if key not in after:
            diff.removed.append(
                LocalChange(uid=key[1], summary=row["summary"] or "", calendar_id=key[0], what="")
            )
    return diff


# ----------------------------------------------------------------------- report


@dataclass
class DryRunReport:
    remote_id: str
    result: SyncResult
    writes: list[PlannedWrite] = field(default_factory=list)
    local: LocalDiff = field(default_factory=LocalDiff)

    @property
    def would_write(self) -> bool:
        return bool(self.writes)

    def summary(self, limit: int = 20) -> str:
        if self.result.skipped:
            return f"{self.remote_id}: skipped ({self.result.error or 'busy'})"
        if self.result.error:
            return f"{self.remote_id}: FAILED — {self.result.error}"

        lines = [f"{self.remote_id}: {self.result.calendars} calendar(s)"]
        if self.result.up_to_date:
            # Otherwise "0 new, 0 updated" reads the same whether the
            # collection was examined and found unchanged or never opened.
            lines.append(
                f"  {self.result.up_to_date} of them unchanged since the last sync "
                "(not enumerated — the server's ctag had not moved)"
            )

        lines.append(f"  to the server: {len(self.writes)} request(s)")
        if not self.writes:
            lines.append("    nothing — no local changes are waiting to be pushed")
        for write in self.writes[:limit]:
            lines.append(f"    {write.describe()}")
        if len(self.writes) > limit:
            lines.append(f"    … and {len(self.writes) - limit} more")

        local = self.local
        lines.append(
            f"  to the local cache: {len(local.added)} new, {len(local.updated)} updated, "
            f"{len(local.removed)} removed, {len(local.conflicted)} conflict(s)"
        )
        for label, entries in (
            ("new", local.added),
            ("upd", local.updated),
            ("del", local.removed),
            ("CONFLICT", local.conflicted),
        ):
            for entry in entries[:limit]:
                detail = f"  [{entry.what}]" if entry.what else ""
                lines.append(f"    {label:8} {entry.summary or entry.uid}{detail}")
            if len(entries) > limit:
                lines.append(f"    {label:8} … and {len(entries) - limit} more")

        return "\n".join(lines)


def plan(
    remote: Remote,
    db_path: Path | str,
    cancel: threading.Event | None = None,
    *,
    client_factory=None,
) -> DryRunReport:
    """Run a full cycle against a copy of the cache and a write-blocking client.

    ``client_factory`` is for tests; production builds a real
    :class:`~davpunk.core.caldav_client.CalDAVClient` and wraps it.
    """
    db_path = Path(db_path)
    writes: list[PlannedWrite] = []

    def factory(remote_model: Remote, credential: str):
        if client_factory is not None:
            inner = client_factory(remote_model, credential)
        else:
            from davpunk.core.caldav_client import CalDAVClient

            inner = CalDAVClient(remote_model, credential)
        return ReadOnlyClient(inner, writes)

    with tempfile.TemporaryDirectory(prefix="davpunk-dry-run-") as tmp:
        copy_path = Path(tmp) / "cache.sqlite3"
        before = _copy_database(db_path, copy_path)

        copy_conn = cache.open_db(copy_path)
        try:
            runner = SyncRunner(remote, copy_conn, client_factory=factory)
            result = runner.run(cancel)
            after = _snapshot(copy_conn)
        finally:
            cache.close_db(copy_conn)

    return DryRunReport(
        remote_id=remote.id,
        result=result,
        writes=writes,
        local=_diff(before, after),
    )


def _copy_database(db_path: Path, copy_path: Path) -> dict:
    """``VACUUM INTO`` a consistent copy, and snapshot it before anything runs.

    ``VACUUM INTO`` rather than copying the file: the cache runs in WAL mode, so
    the ``.sqlite3`` on its own is not the current state — the committed tail
    is still in the ``-wal``.  This reads through a real connection and writes a
    single consistent file, which also cannot be confused for a backup of a
    database still being written to.
    """
    source = cache.connect(db_path)
    try:
        source.execute("VACUUM INTO ?", (str(copy_path),))
        return _snapshot(source)
    finally:
        source.close()


__all__ = [
    "DryRunReport",
    "LocalChange",
    "LocalDiff",
    "PlannedWrite",
    "ReadOnlyClient",
    "plan",
]
