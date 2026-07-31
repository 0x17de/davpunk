"""Corrupt-database triage.

A corrupt cache is recoverable in principle — the server holds the truth — but
the one thing that exists *only* locally is unsent intent.  So before the file
is moved aside we make a best effort to read it, and dump whatever is legible.

Deterministic ``calendar_id`` (see :mod:`davpunk.models.calendar`) is what makes
the rebuild safe for agents: MCP ``task_ref``s issued before a rebuild still
resolve afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from davpunk import paths

log = logging.getLogger("davpunk.core.recovery")


@dataclass
class RecoveryReport:
    """What the UI's blocking dialog and the daemon's ERROR line both need."""

    corrupt_path: Path
    dump_path: Path | None
    recovered_changes: int = 0
    recovered_conflicts: int = 0
    lost: bool = False
    read_error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.lost:
            return (
                f"The task cache at {self.corrupt_path.name} was unreadable and has been "
                f"moved aside. No local edits could be recovered "
                f"({self.read_error}). A fresh cache will re-sync from the server."
            )
        return (
            f"The task cache was corrupt and has been moved aside to "
            f"{self.corrupt_path.name}. {self.recovered_changes} unsent local edit(s) "
            f"and {self.recovered_conflicts} unresolved conflict(s) were written to "
            f"{self.dump_path}. A fresh cache will re-sync from the server."
        )


def recover(db_path: Path) -> RecoveryReport:
    """Dump what is legible, move the file aside, and leave room for a fresh DB.

    Does **not** create the new database — the caller re-runs ``open_db``, so
    there is exactly one place that knows how to build a current schema.
    """
    stamp = int(time.time())
    corrupt_path = db_path.with_name(f"{db_path.name}.corrupt-{stamp}")

    dump, read_error = _dump_unsent_intent(db_path)
    dump_path: Path | None = None
    if dump is not None:
        dump_path = _write_dump(dump, stamp)

    _move_aside(db_path, corrupt_path)

    report = RecoveryReport(
        corrupt_path=corrupt_path,
        dump_path=dump_path,
        recovered_changes=len(dump["pending_changes"]) if dump else 0,
        recovered_conflicts=len(dump["conflicts"]) if dump else 0,
        lost=dump is None,
        read_error=read_error,
    )
    log.error("%s", report.summary())
    return report


def _dump_unsent_intent(db_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read-only best effort.  A second failure here is expected, not exceptional."""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return None, str(exc)

    try:
        pending = _try_query(
            conn,
            """SELECT pc.task_id, pc.change_type, pc.base_etag, pc.queued_at,
                      pc.attempts, pc.last_error, pc.blocked,
                      pc.source_calendar_id, pc.source_href, pc.move_stage,
                      t.calendar_id, t.href, t.uid, t.summary, t.raw_ics, t.sync_state
                 FROM pending_changes pc LEFT JOIN tasks t ON t.id = pc.task_id""",
        )
        conflicts = _try_query(
            conn,
            """SELECT c.task_id, c.local_raw_ics, c.remote_raw_ics, c.remote_etag,
                      c.detected_at, t.calendar_id, t.uid, t.summary
                 FROM conflict_queue c LEFT JOIN tasks t ON t.id = c.task_id
                WHERE c.resolved = 0""",
        )
    except sqlite3.DatabaseError as exc:
        conn.close()
        return None, str(exc)

    conn.close()
    if pending is None and conflicts is None:
        return None, "no legible tables"
    return {
        "schema": "davpunk-recovery-1",
        "source": str(db_path),
        "dumped_at": int(time.time()),
        "pending_changes": pending or [],
        "conflicts": conflicts or [],
    }, None


def _try_query(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]] | None:
    try:
        return [dict(row) for row in conn.execute(sql)]
    except sqlite3.Error as exc:
        log.warning("Recovery query failed (%s); continuing", exc)
        return None


def _write_dump(dump: dict[str, Any], stamp: int) -> Path:
    directory = paths.ensure_dir(paths.recovery_dir())
    path = directory / f"pending-{stamp}.json"
    # The dump can contain task summaries and raw ICS; keep it owner-only.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(dump, handle, indent=2, ensure_ascii=False)
    return path


def _move_aside(db_path: Path, corrupt_path: Path) -> None:
    os.replace(db_path, corrupt_path)
    # WAL and SHM siblings belong to the old file; leaving them would attach a
    # stale write-ahead log to the *new* database.
    for suffix in ("-wal", "-shm"):
        sibling = db_path.with_name(db_path.name + suffix)
        if sibling.exists():
            try:
                os.replace(sibling, corrupt_path.with_name(corrupt_path.name + suffix))
            except OSError as exc:
                log.warning("Could not move %s aside: %s", sibling, exc)
