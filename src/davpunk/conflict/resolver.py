"""Conflict resolution — always user-driven, never automatic.

Three dialog modes, distinguished by which side of the conflict row is NULL:

===============  ================  ======  ==================================
``local_raw_ics``  ``remote_raw_ics``  Mode  Buttons
===============  ================  ======  ==================================
set              set               A       Take all local · Take all server ·
                                           Merge & save
NULL             set               A′      Delete anyway · Keep server version
set              NULL              B       Recreate on server · Accept deletion
===============  ================  ======  ==================================

*Take all local* maps to ``merge`` with every field taken from the local
column; *Take all server* maps to ``restore_server``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from davpunk.core import cache
from davpunk.core.ical_parser import ICalParseError, parse_resource
from davpunk.models.task import ChangeType, SyncState, Task

log = logging.getLogger("davpunk.conflict.resolver")


class Mode(StrEnum):
    BOTH_CHANGED = "A"
    LOCAL_DELETE = "A-prime"
    SERVER_DELETED = "B"


class Resolution(StrEnum):
    MERGE = "merge"
    RESTORE_SERVER = "restore_server"
    DELETE_ANYWAY = "delete_anyway"
    RECREATE = "recreate"
    ACCEPT_DELETION = "accept_deletion"


#: Which resolutions each mode offers.  The UI builds its buttons from this,
#: and :func:`resolve_conflict` refuses anything outside it.
ALLOWED: dict[Mode, tuple[Resolution, ...]] = {
    Mode.BOTH_CHANGED: (Resolution.MERGE, Resolution.RESTORE_SERVER),
    Mode.LOCAL_DELETE: (Resolution.DELETE_ANYWAY, Resolution.RESTORE_SERVER),
    Mode.SERVER_DELETED: (Resolution.RECREATE, Resolution.ACCEPT_DELETION),
}

#: Fields compared side by side in the dialog.
DIFF_FIELDS = (
    "summary",
    "description",
    "status",
    "priority",
    "percent_complete",
    "due_value",
    "dtstart_value",
    "location",
    "url",
    "categories",
)

#: Fields taken wholesale from one side or the other, never sub-field merged.
#: ``description`` is atomic to preserve inline-checklist integrity.
ATOMIC_FIELDS = frozenset({"description"})


class ConflictError(Exception):
    pass


@dataclass
class FieldDiff:
    field: str
    local: Any
    remote: Any

    @property
    def differs(self) -> bool:
        return self.local != self.remote

    @property
    def atomic(self) -> bool:
        return self.field in ATOMIC_FIELDS


@dataclass
class ConflictView:
    """Everything the dialog needs, with no SQL of its own."""

    conflict_id: int
    task_id: str
    mode: Mode
    summary: str | None
    local: Task | None
    remote: Task | None
    remote_etag: str | None
    diffs: list[FieldDiff]

    @property
    def resolutions(self) -> tuple[Resolution, ...]:
        return ALLOWED[self.mode]

    @property
    def changed(self) -> list[FieldDiff]:
        return [d for d in self.diffs if d.differs]


def mode_of(local_raw_ics: str | None, remote_raw_ics: str | None) -> Mode:
    if local_raw_ics is None and remote_raw_ics is not None:
        return Mode.LOCAL_DELETE
    if remote_raw_ics is None and local_raw_ics is not None:
        return Mode.SERVER_DELETED
    if local_raw_ics is None and remote_raw_ics is None:
        # Both sides gone: nothing to decide.  The pull cannot produce this, but
        # refusing loudly beats presenting an empty dialog.
        raise ConflictError("conflict has neither a local nor a remote version")
    return Mode.BOTH_CHANGED


def load_conflict(conflict_id: int, conn: sqlite3.Connection) -> ConflictView:
    row = conn.execute(
        "SELECT c.*, t.summary AS task_summary FROM conflict_queue c "
        "JOIN tasks t ON t.id = c.task_id WHERE c.id = ? AND c.resolved = 0",
        (conflict_id,),
    ).fetchone()
    if row is None:
        raise ConflictError(f"no open conflict with id {conflict_id}")

    mode = mode_of(row["local_raw_ics"], row["remote_raw_ics"])
    local = _parse_or_none(row["local_raw_ics"])
    remote = _parse_or_none(row["remote_raw_ics"])

    return ConflictView(
        conflict_id=row["id"],
        task_id=row["task_id"],
        mode=mode,
        summary=row["task_summary"],
        local=local,
        remote=remote,
        remote_etag=row["remote_etag"],
        diffs=diff(local, remote),
    )


def _parse_or_none(raw_ics: str | None) -> Task | None:
    if raw_ics is None:
        return None
    try:
        return parse_resource(raw_ics, max_resource_bytes=1 << 62)
    except ICalParseError as exc:
        log.warning("Could not parse a conflict snapshot: %s", exc)
        return None


def diff(local: Task | None, remote: Task | None) -> list[FieldDiff]:
    """Field-by-field comparison for the dialog's table."""
    return [
        FieldDiff(
            field=name,
            local=getattr(local, name, None) if local else None,
            remote=getattr(remote, name, None) if remote else None,
        )
        for name in DIFF_FIELDS
    ]


def merge_from_selection(view: ConflictView, take_remote: set[str] | None = None) -> Task:
    """Build the merged task from a per-field selection.

    ``take_remote`` names the fields the user chose the server's value for;
    everything else keeps the local value.  *Take all local* is an empty set,
    which is why it is a ``merge`` and not its own resolution.
    """
    take_remote = take_remote or set()
    base = view.local or view.remote
    if base is None:
        raise ConflictError("nothing to merge")

    merged = base.model_copy(deep=True)
    for field in DIFF_FIELDS:
        source = view.remote if field in take_remote else view.local
        if source is not None:
            setattr(merged, field, getattr(source, field))
    return merged.canonicalized()


# ------------------------------------------------------------------ resolving


def resolve_conflict(
    conflict_id: int,
    resolution: Resolution | str,
    merged_task: Task | None,
    conn: sqlite3.Connection,
) -> None:
    """Apply one of the five resolution paths, atomically."""
    resolution = Resolution(resolution)

    with cache.tx(conn):
        row = conn.execute(
            "SELECT task_id, remote_etag, remote_raw_ics, local_raw_ics "
            "FROM conflict_queue WHERE id = ? AND resolved = 0",
            (conflict_id,),
        ).fetchone()
        if row is None:
            raise ConflictError(f"no open conflict with id {conflict_id}")

        mode = mode_of(row["local_raw_ics"], row["remote_raw_ics"])
        if resolution not in ALLOWED[mode]:
            raise ConflictError(
                f"resolution {resolution.value!r} is not offered for dialog mode "
                f"{mode.value} (offered: {[r.value for r in ALLOWED[mode]]})"
            )

        task_id = row["task_id"]

        if resolution is Resolution.DELETE_ANYWAY:
            _delete_anyway(task_id, conn)
        elif resolution is Resolution.RESTORE_SERVER:
            _restore_server(task_id, row, conn)
        elif resolution is Resolution.MERGE:
            _merge(task_id, row, merged_task, conn)
        elif resolution is Resolution.RECREATE:
            _recreate(task_id, merged_task, conn)
        elif resolution is Resolution.ACCEPT_DELETION:
            _accept_deletion(task_id, conn)
            return  # the conflict row went with the task

        conn.execute("UPDATE conflict_queue SET resolved = 1 WHERE id = ?", (conflict_id,))


def _delete_anyway(task_id: str, conn: sqlite3.Connection) -> None:
    """Mode A′.  The user has decided; the DELETE goes out unconditional."""
    cache.set_sync_state(task_id, SyncState.PENDING_DELETE, conn)
    cache._queue(conn, task_id, ChangeType.DELETE, None)


def _restore_server(task_id: str, row: sqlite3.Row, conn: sqlite3.Connection) -> None:
    """Modes A and A′.

    **No PUT.**  The server already holds this version; re-PUTing adds churn
    and risks a conflict loop if the server moved again since detection.  A
    stale ``remote_etag`` is harmless — the next pull corrects it.
    """
    remote_ics = row["remote_raw_ics"]
    if remote_ics is None:
        raise ConflictError("restore_server needs a remote version")

    parsed = _parse_or_none(remote_ics)
    if parsed is None:
        raise ConflictError("the server version could not be parsed")

    _write_fields(task_id, parsed, conn)
    conn.execute(
        "UPDATE tasks SET sync_state = 'clean', etag = ?, raw_ics = ? WHERE id = ?",
        (row["remote_etag"], remote_ics, task_id),
    )
    cache.clear_pending(task_id, conn)


def _merge(
    task_id: str, row: sqlite3.Row, merged_task: Task | None, conn: sqlite3.Connection
) -> None:
    """Mode A.

    ``remote_etag`` becomes ``base_etag``: the server is at that version, so
    ``If-Match`` guards against another race before we send.
    """
    if merged_task is None:
        raise ConflictError("merge needs a merged task")

    _write_fields(task_id, merged_task.canonicalized(), conn)
    cache.set_sync_state(task_id, SyncState.DIRTY, conn)
    cache._queue(conn, task_id, ChangeType.UPDATE, row["remote_etag"])


def _recreate(task_id: str, merged_task: Task | None, conn: sqlite3.Connection) -> None:
    """Mode B.

    The server no longer has this resource, so the href is kept and
    ``If-None-Match: *`` will succeed.
    """
    if merged_task is not None:
        _write_fields(task_id, merged_task.canonicalized(), conn)
    conn.execute("UPDATE tasks SET sync_state = 'new', etag = NULL WHERE id = ?", (task_id,))
    cache._queue(conn, task_id, ChangeType.CREATE, None)


def _accept_deletion(task_id: str, conn: sqlite3.Connection) -> None:
    """Mode B.  Tombstone it so a stale client cannot replay it back."""
    row = conn.execute(
        "SELECT calendar_id, href, uid FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return
    cache.add_tombstone(row["calendar_id"], row["href"], row["uid"], conn)
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))  # cascades


def _write_fields(task_id: str, task: Task, conn: sqlite3.Connection) -> None:
    """Write the resolved content, leaving identity and state to the caller."""
    columns = [
        "summary",
        "description",
        "status",
        "priority",
        "percent_complete",
        "dtstart_value",
        "dtstart_tzid",
        "due_value",
        "due_tzid",
        "completed",
        "url",
        "location",
        "parent_uid",
        "davpunk_order",
        "kanban_col",
    ]
    from davpunk.models.task import sort_epoch

    dumped = task.model_dump(mode="json")
    values = [dumped.get(column) for column in columns]
    assignments = ", ".join(f"{c} = ?" for c in columns)

    conn.execute(
        f"UPDATE tasks SET {assignments}, due = ?, dtstart = ?, last_modified = ? WHERE id = ?",
        (
            *values,
            sort_epoch(task.due_value, task.due_tzid),
            sort_epoch(task.dtstart_value, task.dtstart_tzid),
            cache.now(),
            task_id,
        ),
    )
    cache._replace_categories(task_id, task.categories, conn)
    cache._replace_alarms(task_id, task.alarms, conn)


def prune_resolved(conn: sqlite3.Connection) -> int:
    """Resolved rows are pruned after 90 days."""
    with cache.tx(conn):
        return conn.execute(
            "DELETE FROM conflict_queue WHERE resolved = 1 AND detected_at < ?",
            (cache.now() - cache.RESOLVED_CONFLICT_TTL,),
        ).rowcount
