"""Alarm delivery, deduplicated by the ``alarm_fires`` ledger.

The ledger key is the **computed** trigger time, not ``valarms.id``: alarm rows
are dropped and reinserted on every server pull, so a flag stored there would
reset itself every cycle and the same alarm would fire forever.  Moving DUE
yields a new ``trigger_at``, so a genuinely rescheduled task legitimately fires
again.

Both the daemon and the UI may scan concurrently; the primary-key insert is the
race guard.

**Alarms fire only while the UI or the daemon is running.**  DavPunk installs no
timer units per alarm and there is no wake-from-suspend delivery.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from davpunk.core import cache
from davpunk.models.task import Alarm, AlarmRelated, Status

log = logging.getLogger("davpunk.notifications.alarm_scan")

#: An alarm whose trigger passed more than this long ago is recorded as fired
#: but never shown — it suppresses the flood on the first start after a long
#: downtime.
STALE_AFTER = 86_400


@dataclass
class ScanResult:
    notified: int = 0
    suppressed: int = 0
    already_fired: int = 0
    pending: int = 0

    def __bool__(self) -> bool:
        return bool(self.notified)


def due_alarms(conn: sqlite3.Connection, now: int | None = None) -> list[tuple[str, int, str]]:
    """``(task_id, trigger_at, summary)`` for every alarm whose time has come.

    Alarms on tasks with no anchor were dropped at parse, so every row here has
    a resolvable ``trigger_at``.
    """
    now = cache.now() if now is None else now
    out: list[tuple[str, int, str]] = []

    rows = conn.execute(
        """SELECT v.task_id, v.related, v.trigger_offset,
                  t.summary, t.status, t.due_value, t.due_tzid,
                  t.dtstart_value, t.dtstart_tzid, t.uid
             FROM valarms v JOIN tasks t ON t.id = v.task_id
            WHERE t.sync_state != 'pending_delete'"""
    )
    for row in rows:
        trigger_at = _trigger_epoch(row)
        if trigger_at is None or trigger_at > now:
            continue
        out.append((row["task_id"], trigger_at, row["summary"] or row["uid"]))
    return out


def _trigger_epoch(row: sqlite3.Row) -> int | None:
    from davpunk.models.task import Task

    task = Task(
        uid=row["uid"],
        due_value=row["due_value"],
        due_tzid=row["due_tzid"],
        dtstart_value=row["dtstart_value"],
        dtstart_tzid=row["dtstart_tzid"],
    )
    alarm = Alarm(related=AlarmRelated(row["related"]), trigger_offset=row["trigger_offset"])
    fired = alarm.trigger_at(task)
    return int(fired.timestamp()) if fired is not None else None


def scan(conn: sqlite3.Connection, now: int | None = None, *, notifier=None) -> ScanResult:
    """Fire what is due, record what fires, and never fire the same thing twice."""
    from davpunk.notifications.dbus_notify import notify as default_notify

    notifier = notifier or default_notify
    now = cache.now() if now is None else now
    result = ScanResult()

    for task_id, trigger_at, summary in due_alarms(conn, now):
        row = conn.execute(
            "SELECT 1 FROM alarm_fires WHERE task_id = ? AND trigger_at = ?",
            (task_id, trigger_at),
        ).fetchone()
        if row is not None:
            result.already_fired += 1
            continue

        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        completed = status in (Status.COMPLETED.value, Status.CANCELLED.value)
        stale = trigger_at < now - STALE_AFTER

        if not _record(task_id, trigger_at, now, conn):
            # Another scanner won the race and is sending it.
            result.already_fired += 1
            continue

        if completed or stale:
            # Recorded as fired, deliberately not shown.
            result.suppressed += 1
            continue

        notifier(_title(trigger_at, now), summary)
        result.notified += 1

    return result


def _record(task_id: str, trigger_at: int, now: int, conn: sqlite3.Connection) -> bool:
    """The primary-key insert is the race guard between the UI and the daemon."""
    try:
        with cache.tx(conn):
            conn.execute(
                "INSERT INTO alarm_fires (task_id, trigger_at, fired_at) VALUES (?,?,?)",
                (task_id, trigger_at, now),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def _title(trigger_at: int, now: int) -> str:
    when = datetime.fromtimestamp(trigger_at, UTC).astimezone()
    if trigger_at > now - 60:
        return "Task reminder"
    return f"Task reminder ({when:%H:%M})"
