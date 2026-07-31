"""The SQLite cache: connection, transactions, migrations and every write path.

Both roles write **only** through the helpers in this module.  Neither the Qt UI
nor the MCP tools issue SQL of their own.

Every write path runs inside :func:`tx`, which is ``BEGIN IMMEDIATE``.  ``with
conn:`` never appears anywhere in DavPunk: the connection is opened with
``isolation_level=None``, so the ``sqlite3`` module issues no implicit ``BEGIN``
and ``with conn:`` would commit nothing while every statement inside it had
already committed on its own.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from davpunk import paths
from davpunk.core.schema import SCHEMA_V1_STATEMENTS
from davpunk.models.calendar import calendar_id as compute_calendar_id
from davpunk.models.task import (
    Alarm,
    ChangeType,
    ReadOnlyReason,
    SyncState,
    Task,
    canonicalize_fields,
    sort_epoch,
)

log = logging.getLogger("davpunk.core.cache")

BUSY_TIMEOUT_S = 5.0
MAX_ATTEMPTS = 8
MAX_BACKOFF = 3600  # seconds; also the clock-skew clamp horizon
TOMBSTONE_TTL = 30 * 86400
ALARM_FIRE_TTL = 90 * 86400
RESOLVED_CONFLICT_TTL = 90 * 86400
MAX_RESURRECTIONS = 3
SUBTREE_DEPTH_CAP = 32

#: Non-retryable HTTP status classes: block immediately rather than back off.
NON_RETRYABLE_STATUS = (400, 403, 415)


# --------------------------------------------------------------------- errors


class CacheError(Exception):
    """Base for every error this module raises."""


class SchemaTooNew(CacheError):
    """The database was written by a newer DavPunk than this binary."""

    def __init__(self, found: int, known: int) -> None:
        super().__init__(
            f"database schema version {found} is newer than this build understands "
            f"({known}); upgrade davpunk or restore an older database"
        )
        self.found = found
        self.known = known


class DatabaseBusy(CacheError):
    """``BEGIN IMMEDIATE`` could not take the write lock within busy_timeout."""


class ReadOnlyResourceError(CacheError):
    """The resource is quarantined and must not be re-serialized."""

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"task {task_id} is read-only ({reason})")
        self.task_id = task_id
        self.reason = reason


class TaskConflictError(CacheError):
    """The task is in ``conflict``; edits are rejected until resolved."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id} has an unresolved conflict")
        self.task_id = task_id


class TaskNotFound(CacheError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"no such task: {task_id}")
        self.task_id = task_id


# ------------------------------------------------------------------- plumbing


def now() -> int:
    """Wall-clock epoch seconds.  Patched wholesale by tests."""
    return int(time.time())


def new_task_id() -> str:
    return uuid.uuid4().hex


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the four mandatory pragmas.

    ``isolation_level=None`` is autocommit: no implicit BEGIN, which is what
    makes :func:`tx` the only transaction boundary in the codebase.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;
        PRAGMA busy_timeout = 5000;
        PRAGMA foreign_keys = ON;
        """
    )
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit write transaction.

    ``BEGIN IMMEDIATE``, not the default ``DEFERRED``: under WAL with several
    processes, two deferred transactions that each read and then try to upgrade
    to a write deadlock — neither can proceed and ``busy_timeout`` cannot break
    the tie.  ``IMMEDIATE`` takes the write lock at ``BEGIN``, so writers queue
    instead.
    """
    assert not conn.in_transaction, "tx() must not be nested"
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise DatabaseBusy(str(exc)) from exc
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- migrations


def _v1_rev8_initial(conn: sqlite3.Connection) -> None:
    # Statement by statement, never executescript(): that method issues an
    # implicit COMMIT before running, which would end the BEGIN IMMEDIATE that
    # migrate() opened and destroy the migration's atomicity.
    for statement in SCHEMA_V1_STATEMENTS:
        conn.execute(statement)


MIGRATIONS = [_v1_rev8_initial]  # index+1 == target user_version
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    have = conn.execute("PRAGMA user_version").fetchone()[0]
    if have > len(MIGRATIONS):
        raise SchemaTooNew(have, len(MIGRATIONS))
    for i, fn in enumerate(MIGRATIONS[have:], start=have + 1):
        with tx(conn):
            fn(conn)
            conn.execute(f"PRAGMA user_version = {i}")
        log.info("Applied schema migration v%d", i)
    return len(MIGRATIONS)


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    """Connect and migrate.  Raises on a corrupt file — see :func:`open_or_recover`."""
    path = Path(path) if path is not None else paths.database_file()
    if str(path) != ":memory:":
        paths.ensure_dir(path.parent)
    conn = connect(path)
    migrate(conn)
    return conn


def open_or_recover(path: Path | str | None = None) -> tuple[sqlite3.Connection, Any]:
    """Connect + migrate, triaging a corrupt database rather than crashing.

    Returns ``(conn, report)`` where ``report`` is ``None`` on the happy path and
    a :class:`davpunk.core.recovery.RecoveryReport` when the database had to be
    moved aside and rebuilt.
    """
    from davpunk.core import recovery

    path = Path(path) if path is not None else paths.database_file()
    try:
        return open_db(path), None
    except sqlite3.DatabaseError as exc:
        log.error("Database at %s is unusable (%s); recovering", path, exc)
        report = recovery.recover(path)
        return open_db(path), report


def close_db(conn: sqlite3.Connection) -> None:
    """Clean shutdown: checkpoint the WAL and let SQLite tidy its statistics."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
    except sqlite3.Error as exc:  # a dying DB must not stop us exiting
        log.warning("Shutdown maintenance failed: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------- row mapping

#: Columns of ``tasks`` that carry task content, in DDL order.
TASK_COLUMNS: tuple[str, ...] = (
    "calendar_id",
    "href",
    "uid",
    "etag",
    "raw_ics",
    "read_only_reason",
    "summary",
    "description",
    "status",
    "priority",
    "percent_complete",
    "dtstart",
    "dtstart_value",
    "dtstart_tzid",
    "due",
    "due_value",
    "due_tzid",
    "completed",
    "rrule",
    "url",
    "location",
    "last_modified",
    "created",
    "sequence",
    "parent_uid",
    "davpunk_order",
    "kanban_col",
    "sync_state",
)

#: Columns an editor-role update may address.  ``etag``, ``sync_state`` and the
#: identity columns are owned by the helpers, not by callers.
EDITABLE_COLUMNS: frozenset[str] = frozenset(
    {
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
        "raw_ics",
        "sequence",
    }
)


def row_to_task(row: sqlite3.Row) -> Task:
    # sqlite3.Row iterates over *values*, so `in row` is not a key test:
    # .keys() is required here, not a lint artefact.
    data = {key: row[key] for key in row.keys() if key in {*TASK_COLUMNS, "id"}}  # noqa: SIM118
    return Task.model_validate(data)


def _task_params(task: Task) -> dict[str, Any]:
    dumped = task.model_dump(mode="json")
    return {column: dumped.get(column) for column in TASK_COLUMNS}


def _derive_sort_keys(fields: dict[str, Any]) -> dict[str, Any]:
    """Recompute the coarse epoch sort keys whenever their value changes."""
    out = dict(fields)
    if "due_value" in out:
        out["due"] = sort_epoch(out.get("due_value"), out.get("due_tzid"))
    if "dtstart_value" in out:
        out["dtstart"] = sort_epoch(out.get("dtstart_value"), out.get("dtstart_tzid"))
    return out


def _reject_unknown_columns(fields: dict[str, Any]) -> None:
    unknown = set(fields) - EDITABLE_COLUMNS - {"categories", "alarms"}
    if unknown:
        raise CacheError(f"not editable through update_task_optimistic: {sorted(unknown)}")


# -------------------------------------------------------------- remotes/cals


def reconcile_remotes(remote_configs: Sequence[Any], conn: sqlite3.Connection) -> None:
    """Upsert every configured remote and recompute ``orphaned``.

    Every process runs this at startup from its own config read.  The upserts
    are idempotent and ``orphaned`` is recomputed each time, so concurrent
    reconcilers are benign.  Cached tasks are never destroyed here — a remote
    whose config block vanished is hidden, not purged.
    """
    with tx(conn):
        configured: list[str] = []
        for config in remote_configs:
            remote = config.to_model() if hasattr(config, "to_model") else config
            configured.append(remote.id)
            conn.execute(
                """INSERT INTO remotes
                     (id, name, url, username, gpg_file, gpg_key_id, sync_interval,
                      color, pinned_view, allow_insecure, verify_tls, orphaned)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, url=excluded.url, username=excluded.username,
                     gpg_file=excluded.gpg_file, gpg_key_id=excluded.gpg_key_id,
                     sync_interval=excluded.sync_interval, color=excluded.color,
                     pinned_view=excluded.pinned_view,
                     allow_insecure=excluded.allow_insecure,
                     verify_tls=excluded.verify_tls, orphaned=0""",
                (
                    remote.id,
                    remote.name,
                    remote.url,
                    remote.username,
                    remote.gpg_file,
                    remote.gpg_key_id,
                    remote.sync_interval,
                    remote.color,
                    int(remote.pinned_view),
                    int(remote.allow_insecure),
                    int(remote.verify_tls),
                ),
            )
            conn.execute(
                "INSERT INTO sync_status (remote_id) VALUES (?) ON CONFLICT DO NOTHING",
                (remote.id,),
            )

        if configured:
            placeholders = ",".join("?" * len(configured))
            conn.execute(
                f"UPDATE remotes SET orphaned = 1 WHERE id NOT IN ({placeholders})",
                configured,
            )
        else:
            conn.execute("UPDATE remotes SET orphaned = 1")


def purge_remote(remote_id: str, conn: sqlite3.Connection) -> int:
    """Explicit user action: drop an orphaned remote and its cached tasks.

    Returns the number of tasks destroyed, so the UI can name the count in its
    confirmation prompt before this is ever called.
    """
    with tx(conn):
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks t JOIN calendars c ON c.id = t.calendar_id "
            "WHERE c.remote_id = ?",
            (remote_id,),
        ).fetchone()[0]
        conn.execute("DELETE FROM remotes WHERE id = ?", (remote_id,))
        return int(count)


def upsert_calendar(
    remote_id: str,
    href: str,
    conn: sqlite3.Connection,
    *,
    display_name: str | None = None,
    color: str | None = None,
    ctag: str | None = None,
    sync_token: str | None = None,
    supports_sync: bool = False,
) -> str:
    """Insert or refresh a discovered collection.  Returns its ``calendar_id``.

    ``ctag`` / ``sync_token`` are **not** overwritten with NULL: discovery may
    not report them, and clobbering them would disable the pull short-circuit.
    """
    cal_id = compute_calendar_id(remote_id, href)
    conn.execute(
        """INSERT INTO calendars
             (id, remote_id, href, display_name, color, ctag, sync_token,
              supports_sync, available)
           VALUES (?,?,?,?,?,?,?,?,1)
           ON CONFLICT(id) DO UPDATE SET
             display_name  = excluded.display_name,
             color         = excluded.color,
             ctag          = COALESCE(excluded.ctag, calendars.ctag),
             sync_token    = COALESCE(excluded.sync_token, calendars.sync_token),
             supports_sync = excluded.supports_sync,
             available     = 1""",
        (cal_id, remote_id, href, display_name, color, ctag, sync_token, int(supports_sync)),
    )
    return cal_id


def mark_calendar_unavailable(calendar_id: str, conn: sqlite3.Connection) -> None:
    """The collection vanished from the server.

    Rows are **not** deleted: a 404'd collection is far more often a server
    hiccup than an intentional removal.  Its tasks become read-only so nothing
    is re-serialized against a collection that may not exist.
    """
    conn.execute("UPDATE calendars SET available = 0 WHERE id = ?", (calendar_id,))
    conn.execute(
        "UPDATE tasks SET read_only_reason = ? WHERE calendar_id = ? AND read_only_reason IS NULL",
        (ReadOnlyReason.CALENDAR_UNAVAILABLE.value, calendar_id),
    )


def mark_calendar_available(calendar_id: str, conn: sqlite3.Connection) -> None:
    """The collection came back; lift the quarantine we imposed for its absence."""
    conn.execute("UPDATE calendars SET available = 1 WHERE id = ?", (calendar_id,))
    conn.execute(
        "UPDATE tasks SET read_only_reason = NULL WHERE calendar_id = ? AND read_only_reason = ?",
        (calendar_id, ReadOnlyReason.CALENDAR_UNAVAILABLE.value),
    )


def set_calendar_sync_markers(
    calendar_id: str,
    conn: sqlite3.Connection,
    *,
    ctag: str | None = None,
    sync_token: str | None = None,
    last_sync: int | None = None,
) -> None:
    conn.execute(
        "UPDATE calendars SET ctag = COALESCE(?, ctag), sync_token = COALESCE(?, sync_token), "
        "last_sync = COALESCE(?, last_sync) WHERE id = ?",
        (ctag, sync_token, last_sync, calendar_id),
    )


# ------------------------------------------------------------- write helpers


def create_task_local(task: Task, conn: sqlite3.Connection) -> str:
    """Insert a locally-created task and queue its create."""
    with tx(conn):
        return _create_task_locked(task, conn)


def _create_task_locked(task: Task, conn: sqlite3.Connection) -> str:
    task = task.canonicalized()
    if not task.calendar_id:
        raise CacheError("create_task_local requires a calendar_id")
    task_id = task.id or new_task_id()
    href = task.href or f"{task.uid}.ics"
    stamp = now()

    params = _task_params(task)
    params.update(
        href=href,
        etag=None,
        sync_state=SyncState.NEW.value,
        created=task.created or stamp,
        last_modified=stamp,
        due=sort_epoch(task.due_value, task.due_tzid),
        dtstart=sort_epoch(task.dtstart_value, task.dtstart_tzid),
    )

    columns = ", ".join(("id", *TASK_COLUMNS))
    placeholders = ", ".join(["?"] * (len(TASK_COLUMNS) + 1))
    conn.execute(
        f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
        (task_id, *(params[c] for c in TASK_COLUMNS)),
    )
    _replace_categories(task_id, task.categories, conn)
    _replace_alarms(task_id, task.alarms, conn)
    _queue(conn, task_id, ChangeType.CREATE, None)
    return task_id


def update_task_optimistic(
    task_id: str,
    fields: dict[str, Any],
    conn: sqlite3.Connection,
    *,
    rewrite_alarms: bool = False,
) -> None:
    """The single editor-role update path.  UI and MCP both come through here."""
    with tx(conn):
        _update_task_locked(task_id, fields, conn, rewrite_alarms=rewrite_alarms)


def _update_task_locked(
    task_id: str,
    fields: dict[str, Any],
    conn: sqlite3.Connection,
    *,
    rewrite_alarms: bool = False,
) -> None:
    row = conn.execute(
        "SELECT etag, sync_state, read_only_reason FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskNotFound(task_id)

    if row["read_only_reason"] is not None:
        raise ReadOnlyResourceError(task_id, row["read_only_reason"])
    if row["sync_state"] == SyncState.CONFLICT.value:
        raise TaskConflictError(task_id)

    _reject_unknown_columns(fields)

    # A locally-created task stays 'new'; its pending row stays 'create'.
    is_new = row["sync_state"] == SyncState.NEW.value
    new_state = SyncState.NEW if is_new else SyncState.DIRTY
    change_type = ChangeType.CREATE if is_new else ChangeType.UPDATE
    # The clean base ETag, read inside the transaction so no sync-role write can
    # land between the read and the write.
    pre_etag = row["etag"]

    fields = dict(fields)  # never mutate the caller's mapping
    categories = fields.pop("categories", None)
    alarms = fields.pop("alarms", None)

    writable = _derive_sort_keys(canonicalize_fields(fields))
    writable["last_modified"] = now()

    assignments = ", ".join(f"{column} = ?" for column in writable)
    conn.execute(
        f"UPDATE tasks SET {assignments}, sync_state = ? WHERE id = ?",
        (*_serialize(writable.values()), new_state.value, task_id),
    )

    if categories is not None:
        _replace_categories(task_id, categories, conn)
    if rewrite_alarms:
        _replace_alarms(task_id, alarms or [], conn)

    # base_etag is set only on INSERT (the first edit after a clean sync); later
    # edits before a sync keep the original base_etag intact.  A user-driven
    # edit also resets the backoff — it is an explicit retry.
    conn.execute(
        """INSERT INTO pending_changes (task_id, change_type, base_etag, queued_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
             change_type     = excluded.change_type,
             queued_at       = excluded.queued_at,
             attempts        = 0,
             next_attempt_at = 0,
             blocked         = 0,
             last_error      = NULL
             -- base_etag intentionally NOT in the DO UPDATE list
        """,
        (task_id, change_type.value, pre_etag, now()),
    )


def delete_task_local(task_id: str, conn: sqlite3.Connection) -> None:
    """Queue a delete, or purge outright if the task never reached a server.

    Deletion is permitted for read-only resources: DELETE removes the whole
    resource and involves no serialization.
    """
    with tx(conn):
        _delete_task_locked(task_id, conn)


def _delete_task_locked(task_id: str, conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT etag, sync_state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskNotFound(task_id)
    pending = conn.execute(
        "SELECT change_type, base_etag FROM pending_changes WHERE task_id = ?", (task_id,)
    ).fetchone()

    if pending and pending["change_type"] == ChangeType.CREATE.value:
        # Never existed remotely.  Purge locally; no network, no tombstone.
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))  # cascades
        return

    if row["sync_state"] == SyncState.CONFLICT.value:
        # Implicit delete_anyway: the user has decided.
        conn.execute(
            "UPDATE conflict_queue SET resolved = 1 WHERE task_id = ? AND resolved = 0",
            (task_id,),
        )
        base_etag = None  # unconditional DELETE
    else:
        base_etag = (pending["base_etag"] if pending else None) or row["etag"]

    _promote_children_to_root(task_id, conn)
    conn.execute(
        "UPDATE tasks SET sync_state = ? WHERE id = ?", (SyncState.PENDING_DELETE.value, task_id)
    )
    _queue(conn, task_id, ChangeType.DELETE, base_etag)
    # The tasks row is NOT deleted here; the sync role does final cleanup.


def move_task_local(
    task_id: str,
    target_calendar_id: str,
    conn: sqlite3.Connection,
    *,
    move_subtree: bool = True,
) -> None:
    """Retarget a task (and by default its subtree) to another calendar."""
    with tx(conn):
        _move_task_locked(task_id, target_calendar_id, conn, move_subtree=move_subtree)


def _move_task_locked(
    task_id: str,
    target_calendar_id: str,
    conn: sqlite3.Connection,
    *,
    move_subtree: bool = True,
) -> None:
    row = conn.execute(
        "SELECT calendar_id, href, uid, etag, sync_state, read_only_reason FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise TaskNotFound(task_id)

    if row["calendar_id"] == target_calendar_id:
        return  # no-op
    if row["sync_state"] == SyncState.CONFLICT.value:
        raise TaskConflictError(task_id)
    if row["read_only_reason"] == ReadOnlyReason.CALENDAR_UNAVAILABLE.value:
        raise ReadOnlyResourceError(task_id, row["read_only_reason"])
    # 'multipart' and 'oversize' ARE movable: a move relocates the bytes verbatim
    # and never re-serializes, so the T42 hazard does not apply.

    if (
        conn.execute("SELECT 1 FROM calendars WHERE id = ?", (target_calendar_id,)).fetchone()
        is None
    ):
        raise CacheError(f"no such calendar: {target_calendar_id}")

    # Collect the subtree BEFORE retargeting the parent — descendants are found
    # through (calendar_id, parent_uid), which the parent's move invalidates.
    descendants = _descendants(task_id, conn) if move_subtree else []
    if not move_subtree:
        _promote_children_to_root(task_id, conn)

    _move_one(task_id, target_calendar_id, conn)
    for child_id in descendants:
        _move_one(child_id, target_calendar_id, conn)


def _move_one(task_id: str, target_calendar_id: str, conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT calendar_id, href, uid, etag, sync_state, read_only_reason FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["calendar_id"] == target_calendar_id:
        return
    if row["sync_state"] == SyncState.CONFLICT.value:
        # A conflicted descendant cannot be moved — its resolution is still the
        # user's to make.  It stays behind holding its RELATED-TO, which renders
        # as a root task with a "linked parent elsewhere" marker.
        log.info("Leaving conflicted subtask %s behind during move", task_id)
        return
    if row["read_only_reason"] == ReadOnlyReason.CALENDAR_UNAVAILABLE.value:
        log.info("Leaving unavailable-calendar subtask %s behind during move", task_id)
        return

    if row["sync_state"] == SyncState.NEW.value:
        # Never pushed anywhere.  Retarget locally; it stays a create.
        conn.execute("UPDATE tasks SET calendar_id = ? WHERE id = ?", (target_calendar_id, task_id))
        return

    # Guard the source against re-import during the PUT->DELETE window: a pull of
    # the source would otherwise see an href with no local row and import it as a
    # brand-new task.
    stamp = now()
    conn.execute(
        "INSERT INTO tombstones (calendar_id, href, uid, deleted_at, synced_at) VALUES (?,?,?,?,?)",
        (row["calendar_id"], row["href"], row["uid"], stamp, stamp),
    )
    conn.execute(
        "UPDATE tasks SET calendar_id = ?, href = ?, etag = NULL, sync_state = ? WHERE id = ?",
        (target_calendar_id, f"{row['uid']}.ics", SyncState.NEW.value, task_id),
    )
    conn.execute(
        """INSERT OR REPLACE INTO pending_changes
             (task_id, change_type, base_etag, queued_at, attempts,
              next_attempt_at, blocked, last_error,
              source_calendar_id, source_href, move_stage)
           VALUES (?, 'move', ?, ?, 0, 0, 0, NULL, ?, ?, 0)""",
        (task_id, row["etag"], stamp, row["calendar_id"], row["href"]),
    )


# ------------------------------------------------------------ sync-role apply


def apply_server_version(
    task_id: str, parsed: Task, new_etag: str | None, conn: sqlite3.Connection
) -> bool:
    """Guarded write of a server version over a **clean** local row.

    The guard and the child-table rewrite are one transaction — splitting them is
    exactly the interleaving this invariant exists to prevent.

    Returns False when the row stopped being clean between the pull's read and
    this write; the caller retries next cycle.
    """
    with tx(conn):
        return _apply_server_version_locked(task_id, parsed, new_etag, conn)


def _apply_server_version_locked(
    task_id: str, parsed: Task, new_etag: str | None, conn: sqlite3.Connection
) -> bool:
    parsed = parsed.canonicalized()
    params = _task_params(parsed)
    params["due"] = sort_epoch(parsed.due_value, parsed.due_tzid)
    params["dtstart"] = sort_epoch(parsed.dtstart_value, parsed.dtstart_tzid)

    # calendar_id, href and sync_state are identity/state, not content: the
    # parser never knew the href, only move_task_local() retargets a calendar,
    # and the state is set by the guard below.
    columns = [c for c in TASK_COLUMNS if c not in ("calendar_id", "href", "sync_state")]
    assignments = ", ".join(f"{c} = ?" for c in columns)
    cursor = conn.execute(
        f"UPDATE tasks SET {assignments}, etag = ?, sync_state = 'clean' "
        "WHERE id = ? AND sync_state = 'clean'",
        (*_serialize(params[c] for c in columns), new_etag, task_id),
    )
    if cursor.rowcount == 0:
        log.debug("Task %s became dirty between read and write; retry next cycle", task_id)
        return False

    _replace_categories(task_id, parsed.categories, conn)
    _replace_alarms(task_id, parsed.alarms, conn)
    return True


def insert_server_task(
    task: Task, etag: str | None, conn: sqlite3.Connection, *, task_id: str | None = None
) -> str:
    """Import a resource the server has and we do not.  Sync role only."""
    task = task.canonicalized()
    task_id = task_id or new_task_id()
    params = _task_params(task)
    params.update(
        etag=etag,
        sync_state=SyncState.CLEAN.value,
        due=sort_epoch(task.due_value, task.due_tzid),
        dtstart=sort_epoch(task.dtstart_value, task.dtstart_tzid),
    )
    columns = ", ".join(("id", *TASK_COLUMNS))
    placeholders = ", ".join(["?"] * (len(TASK_COLUMNS) + 1))
    conn.execute(
        f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
        (task_id, *_serialize(params[c] for c in TASK_COLUMNS)),
    )
    _replace_categories(task_id, task.categories, conn)
    _replace_alarms(task_id, task.alarms, conn)
    return task_id


def set_sync_state(task_id: str, state: SyncState | str, conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE tasks SET sync_state = ? WHERE id = ?",
        (state.value if isinstance(state, SyncState) else state, task_id),
    )


def set_task_identity(
    task_id: str,
    conn: sqlite3.Connection,
    *,
    href: str | None = None,
    etag: str | None = None,
    raw_ics: str | None = None,
    sequence: int | None = None,
) -> None:
    """Post-PUT bookkeeping: Location rewrite, ETag, SEQUENCE bump."""
    sets, values = [], []
    if href is not None:
        sets.append("href = ?")
        values.append(href)
    if etag is not None:
        sets.append("etag = ?")
        values.append(etag)
    if raw_ics is not None:
        sets.append("raw_ics = ?")
        values.append(raw_ics)
    if sequence is not None:
        sets.append("sequence = ?")
        values.append(sequence)
    if not sets:
        return
    values.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", values)


def clear_etag(task_id: str, conn: sqlite3.Connection) -> None:
    """A 2xx PUT with no ETag and no multiget fallback.

    NULL != any server ETag, so the next cycle re-fetches this href exactly once
    and the clean-guard path applies.  No data is lost.
    """
    conn.execute("UPDATE tasks SET etag = NULL WHERE id = ?", (task_id,))


# ---------------------------------------------------------------- conflicts


def upsert_conflict(
    task_id: str,
    local_raw_ics: str | None,
    remote_raw_ics: str | None,
    remote_etag: str | None,
    conn: sqlite3.Connection,
) -> None:
    """At most one OPEN conflict per task, enforced by the database.

    Called from within an enclosing :func:`tx`, never on its own.
    """
    conn.execute(
        """
        INSERT INTO conflict_queue
          (task_id, local_raw_ics, remote_raw_ics, remote_etag, detected_at, resolved)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT (task_id) WHERE resolved = 0 DO UPDATE SET
          remote_raw_ics = excluded.remote_raw_ics,   -- refresh the server side
          remote_etag    = excluded.remote_etag,      -- NULL if server deleted
          detected_at    = excluded.detected_at
          -- local_raw_ics is deliberately NOT refreshed: it is the snapshot the
          -- user's pending edit is based on, and must stay stable while the
          -- conflict dialog is open
        """,
        (task_id, local_raw_ics, remote_raw_ics, remote_etag, now()),
    )


def open_conflicts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT c.*, t.summary, t.uid, t.calendar_id FROM conflict_queue c "
            "JOIN tasks t ON t.id = c.task_id WHERE c.resolved = 0 "
            "ORDER BY c.detected_at"
        )
    )


# ------------------------------------------------------------ pending changes


def _queue(
    conn: sqlite3.Connection,
    task_id: str,
    change_type: ChangeType | str,
    base_etag: str | None,
    *,
    source_calendar_id: str | None = None,
    source_href: str | None = None,
) -> None:
    """Replace any prior intent and reset the backoff.

    ``INSERT OR REPLACE`` because a user-driven action is an explicit "try again
    now": queueing a delete over a stale failed update must not inherit its
    attempt count.
    """
    value = change_type.value if isinstance(change_type, ChangeType) else change_type
    conn.execute(
        """INSERT OR REPLACE INTO pending_changes
             (task_id, change_type, base_etag, queued_at, attempts, next_attempt_at,
              blocked, last_error, source_calendar_id, source_href, move_stage)
           VALUES (?, ?, ?, ?, 0, 0, 0, NULL, ?, ?, 0)""",
        (task_id, value, base_etag, now(), source_calendar_id, source_href),
    )


def clear_pending(task_id: str, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM pending_changes WHERE task_id = ?", (task_id,))


def set_move_stage(task_id: str, stage: int, conn: sqlite3.Connection) -> None:
    """Persisted so a crash between the move's stages never re-PUTs."""
    conn.execute("UPDATE pending_changes SET move_stage = ? WHERE task_id = ?", (stage, task_id))


def record_failure(
    task_id: str, error: str, conn: sqlite3.Connection, *, status: int | None = None
) -> None:
    """Exponential backoff, or an immediate block for a non-retryable class."""
    row = conn.execute(
        "SELECT attempts FROM pending_changes WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return
    attempts = int(row["attempts"]) + 1

    if status in NON_RETRYABLE_STATUS:
        conn.execute(
            "UPDATE pending_changes SET attempts = ?, blocked = 1, last_error = ?, "
            "next_attempt_at = 0 WHERE task_id = ?",
            (attempts, error, task_id),
        )
        return

    blocked = attempts >= MAX_ATTEMPTS
    delay = min(60 * (2**attempts), MAX_BACKOFF)
    conn.execute(
        "UPDATE pending_changes SET attempts = ?, next_attempt_at = ?, last_error = ?, "
        "blocked = ? WHERE task_id = ?",
        (attempts, now() + delay, error, int(blocked), task_id),
    )


def reset_backoff(task_id: str, conn: sqlite3.Connection) -> None:
    """An explicit *Retry now*."""
    conn.execute(
        "UPDATE pending_changes SET attempts = 0, next_attempt_at = 0, blocked = 0, "
        "last_error = NULL WHERE task_id = ?",
        (task_id,),
    )


def ready_pending_changes(calendar_id: str, conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The push-phase scan.

    ``next_attempt_at > now() + MAX_BACKOFF`` counts as ready: that clamp
    self-heals a backward clock jump and a corrupt stored value, neither of which
    should strand a change forever.
    """
    stamp = now()
    return list(
        conn.execute(
            """SELECT pc.task_id, pc.change_type, pc.base_etag, pc.attempts,
                      pc.source_calendar_id, pc.source_href, pc.move_stage,
                      t.*
                 FROM pending_changes pc
                 JOIN tasks t ON t.id = pc.task_id
                WHERE t.calendar_id = ?
                  AND pc.blocked = 0
                  AND (pc.next_attempt_at <= ? OR pc.next_attempt_at > ?)
                  AND t.sync_state != 'conflict'
                  AND (t.read_only_reason IS NULL
                       OR pc.change_type IN ('delete', 'move'))
                ORDER BY pc.queued_at""",
            (calendar_id, stamp, stamp + MAX_BACKOFF),
        )
    )


def has_ready_pending(calendar_id: str, conn: sqlite3.Connection) -> bool:
    """Does the ctag short-circuit have to be skipped for this calendar?"""
    return bool(ready_pending_changes(calendar_id, conn))


def blocked_count(conn: sqlite3.Connection, remote_id: str | None = None) -> int:
    sql = (
        "SELECT COUNT(*) FROM pending_changes pc "
        "JOIN tasks t ON t.id = pc.task_id "
        "JOIN calendars c ON c.id = t.calendar_id WHERE pc.blocked = 1"
    )
    params: tuple[Any, ...] = ()
    if remote_id:
        sql += " AND c.remote_id = ?"
        params = (remote_id,)
    return int(conn.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------- tombstones


def add_tombstone(calendar_id: str, href: str, uid: str, conn: sqlite3.Connection) -> None:
    stamp = now()
    conn.execute(
        "INSERT INTO tombstones (calendar_id, href, uid, deleted_at, synced_at) VALUES (?,?,?,?,?)",
        (calendar_id, href, uid, stamp, stamp),
    )


def find_tombstone(calendar_id: str, uid: str, conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Matched on ``(calendar_id, uid)``, not href.

    A stale client replaying a deleted task reuses the UID but often lands on a
    different href, so href-matching misses the common case.  A user genuinely
    re-creating "the same" task mints a new UID.
    """
    return conn.execute(
        "SELECT * FROM tombstones WHERE calendar_id = ? AND uid = ? AND synced_at > ? "
        "ORDER BY synced_at DESC LIMIT 1",
        (calendar_id, uid, now() - TOMBSTONE_TTL),
    ).fetchone()


def bump_resurrect_count(tombstone_id: int, conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE tombstones SET resurrect_count = resurrect_count + 1 WHERE id = ?",
        (tombstone_id,),
    )


def drop_tombstone(tombstone_id: int, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tombstones WHERE id = ?", (tombstone_id,))


def finalize_delete(task_id: str, conn: sqlite3.Connection) -> None:
    """Tombstone the resource and drop the row (children cascade)."""
    row = conn.execute(
        "SELECT calendar_id, href, uid FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return
    add_tombstone(row["calendar_id"], row["href"], row["uid"], conn)
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# --------------------------------------------------------------- expiry


def expire(conn: sqlite3.Connection) -> dict[str, int]:
    with tx(conn):
        stamp = now()
        tombstones = conn.execute(
            "DELETE FROM tombstones WHERE synced_at < ?", (stamp - TOMBSTONE_TTL,)
        ).rowcount
        alarms = conn.execute(
            "DELETE FROM alarm_fires WHERE fired_at < ?", (stamp - ALARM_FIRE_TTL,)
        ).rowcount
        conflicts = conn.execute(
            "DELETE FROM conflict_queue WHERE resolved = 1 AND detected_at < ?",
            (stamp - RESOLVED_CONFLICT_TTL,),
        ).rowcount
    return {"tombstones": tombstones, "alarm_fires": alarms, "conflicts": conflicts}


# ------------------------------------------------------------- child tables


def _replace_categories(
    task_id: str, categories: Iterable[str] | None, conn: sqlite3.Connection
) -> None:
    conn.execute("DELETE FROM task_categories WHERE task_id = ?", (task_id,))
    unique = sorted({c for c in (categories or []) if c})
    if unique:
        conn.executemany(
            "INSERT INTO task_categories (task_id, category) VALUES (?, ?)",
            [(task_id, c) for c in unique],
        )


def _replace_alarms(
    task_id: str, alarms: Iterable[Alarm | dict[str, Any]] | None, conn: sqlite3.Connection
) -> None:
    conn.execute("DELETE FROM valarms WHERE task_id = ?", (task_id,))
    rows = []
    for alarm in alarms or []:
        model = alarm if isinstance(alarm, Alarm) else Alarm.model_validate(alarm)
        rows.append((task_id, model.related.value, model.trigger_offset))
    if rows:
        conn.executemany(
            "INSERT INTO valarms (task_id, related, trigger_offset) VALUES (?,?,?)", rows
        )


def load_categories(task_id: str, conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT category FROM task_categories WHERE task_id = ? ORDER BY category",
            (task_id,),
        )
    ]


def load_alarms(task_id: str, conn: sqlite3.Connection) -> list[Alarm]:
    return [
        Alarm(related=r["related"], trigger_offset=r["trigger_offset"])
        for r in conn.execute(
            "SELECT related, trigger_offset FROM valarms WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]


# ------------------------------------------------------------------ subtasks


def children_of(task_id: str, conn: sqlite3.Connection) -> list[str]:
    """Direct children, resolved **within the same calendar only**."""
    row = conn.execute("SELECT calendar_id, uid FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return []
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM tasks WHERE calendar_id = ? AND parent_uid = ?",
            (row["calendar_id"], row["uid"]),
        )
    ]


def _descendants(task_id: str, conn: sqlite3.Connection) -> list[str]:
    """Breadth-first subtree walk with a visited set and a depth cap."""
    out: list[str] = []
    visited = {task_id}
    frontier = [task_id]
    for depth in range(SUBTREE_DEPTH_CAP):
        nxt: list[str] = []
        for parent in frontier:
            for child in children_of(parent, conn):
                if child in visited:
                    log.warning("Subtask cycle detected at %s; breaking the link", child)
                    continue
                visited.add(child)
                out.append(child)
                nxt.append(child)
        if not nxt:
            return out
        frontier = nxt
        if depth == SUBTREE_DEPTH_CAP - 1:
            log.warning("Subtask depth cap (%d) reached below %s", SUBTREE_DEPTH_CAP, task_id)
    return out


def _promote_children_to_root(task_id: str, conn: sqlite3.Connection) -> None:
    """Children are never cascade-deleted; they become root tasks.

    Each is marked dirty so the ``RELATED-TO`` removal actually reaches the
    server.  A child that is ``new`` stays ``new``/``create``; a conflicted or
    quarantined child is left alone — we cannot rewrite its ICS.
    """
    for child_id in children_of(task_id, conn):
        row = conn.execute(
            "SELECT sync_state, read_only_reason, etag FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        if row is None:
            continue
        if row["sync_state"] == SyncState.CONFLICT.value or row["read_only_reason"] is not None:
            log.info("Not re-parenting %s: %s", child_id, row["sync_state"])
            continue
        if row["sync_state"] == SyncState.PENDING_DELETE.value:
            continue
        is_new = row["sync_state"] == SyncState.NEW.value
        conn.execute(
            "UPDATE tasks SET parent_uid = NULL, sync_state = ?, last_modified = ? WHERE id = ?",
            (
                SyncState.NEW.value if is_new else SyncState.DIRTY.value,
                now(),
                child_id,
            ),
        )
        conn.execute(
            """INSERT INTO pending_changes (task_id, change_type, base_etag, queued_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 queued_at = excluded.queued_at, attempts = 0,
                 next_attempt_at = 0, blocked = 0, last_error = NULL""",
            (
                child_id,
                ChangeType.CREATE.value if is_new else ChangeType.UPDATE.value,
                row["etag"],
                now(),
            ),
        )


# --------------------------------------------------------------------- reads


def get_task(task_id: str, conn: sqlite3.Connection, *, with_children: bool = True) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = row_to_task(row)
    if with_children:
        task.categories = load_categories(task_id, conn)
        task.alarms = load_alarms(task_id, conn)
    return task


def get_task_row(task_id: str, conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def find_by_href(calendar_id: str, href: str, conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tasks WHERE calendar_id = ? AND href = ?", (calendar_id, href)
    ).fetchone()


def find_by_uid(calendar_id: str, uid: str, conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """``uid`` is not unique within a calendar; a bad server can duplicate it.

    Returning every candidate is what lets the MCP layer raise a structured
    ambiguity error instead of silently picking one.
    """
    return list(
        conn.execute(
            "SELECT * FROM tasks WHERE calendar_id = ? AND uid = ? ORDER BY href",
            (calendar_id, uid),
        )
    )


def calendar_rows(conn: sqlite3.Connection, remote_id: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT c.*, r.name AS remote_name FROM calendars c JOIN remotes r ON r.id = c.remote_id"
    params: tuple[Any, ...] = ()
    if remote_id:
        sql += " WHERE c.remote_id = ?"
        params = (remote_id,)
    sql += " ORDER BY r.id, c.display_name"
    return list(conn.execute(sql, params))


def local_rows_for_calendar(calendar_id: str, conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """``href -> row`` for the pull phase."""
    return {
        row["href"]: row
        for row in conn.execute("SELECT * FROM tasks WHERE calendar_id = ?", (calendar_id,))
    }


def search_tasks(
    query: str, conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[sqlite3.Row]:
    """FTS5 full-text search over summary and description."""
    return list(
        conn.execute(
            "SELECT t.* FROM tasks_fts f JOIN tasks t ON t.rowid = f.rowid "
            "WHERE tasks_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
            (query, limit, offset),
        )
    )


def fts_rebuild(conn: sqlite3.Connection) -> None:
    with tx(conn):
        conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")


# ------------------------------------------------------------- sync_status


def set_sync_status(
    remote_id: str,
    conn: sqlite3.Connection,
    *,
    last_sync: int | None = None,
    last_error: str | None = None,
) -> None:
    with tx(conn):
        conn.execute(
            """INSERT INTO sync_status (remote_id, last_sync, last_error) VALUES (?,?,?)
               ON CONFLICT(remote_id) DO UPDATE SET
                 last_sync  = COALESCE(excluded.last_sync, sync_status.last_sync),
                 last_error = excluded.last_error""",
            (remote_id, last_sync, last_error),
        )


def sync_status_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT r.id AS remote_id, r.name, r.orphaned, s.last_sync, s.last_error "
            "FROM remotes r LEFT JOIN sync_status s ON s.remote_id = r.id ORDER BY r.id"
        )
    )


# ------------------------------------------------------------------- audit


def audit_mcp(
    tool: str, task_ref: str | None, detail: str | None, conn: sqlite3.Connection
) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO mcp_audit (ts, tool, task_ref, detail) VALUES (?,?,?,?)",
            (now(), tool, task_ref, detail),
        )


# ------------------------------------------------------------------ helpers


def _serialize(values: Iterable[Any]) -> tuple[Any, ...]:
    """sqlite3 cannot bind enums, bools or lists; normalise on the way in."""
    out = []
    for value in values:
        if isinstance(value, bool):
            out.append(int(value))
        elif isinstance(value, (list, dict)):
            out.append(json.dumps(value))
        elif hasattr(value, "value") and not isinstance(value, (str, int, float, bytes)):
            out.append(value.value)
        else:
            out.append(value)
    return tuple(out)


def database_is_writable(path: Path | None = None) -> bool:
    path = path or paths.database_file()
    return os.access(path.parent, os.W_OK)
