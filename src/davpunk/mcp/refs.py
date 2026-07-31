"""``task_ref`` — the only task identity an agent ever sees.

``task_ref = "<calendar_id>:<uid>"``.  One opaque string, returned by every tool
and accepted by every tool.  It is stable across syncs, restarts and cache
rebuilds, because ``calendar_id`` is ``sha256(remote_id | href)[:16]`` rather
than a key minted at discovery.

**``tasks.id`` never appears in any MCP payload.**  It is a local UUID that
changes if a task is deleted and re-imported, and means nothing outside this
database.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from davpunk.models.calendar import CALENDAR_ID_LEN

SEPARATOR = ":"
_CALENDAR_ID_RE = re.compile(rf"^[0-9a-f]{{{CALENDAR_ID_LEN}}}$")


class RefError(Exception):
    """A malformed or unresolvable ``task_ref``, as a structured error."""

    def __init__(self, message: str, *, code: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra

    def as_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": str(self), **self.extra}


class AmbiguousRef(RefError):
    """More than one row matches.

    ``uid`` is not unique within a calendar, and a misbehaving server can
    duplicate one, so the candidates are listed rather than one being picked
    silently.  An optional ``href`` disambiguates.
    """

    def __init__(self, task_ref: str, hrefs: list[str]) -> None:
        super().__init__(
            f"{task_ref} matches {len(hrefs)} resources; pass href= to disambiguate",
            code="ambiguous_task_ref",
            task_ref=task_ref,
            candidates=hrefs,
        )


@dataclass(frozen=True)
class TaskRef:
    calendar_id: str
    uid: str

    def __str__(self) -> str:
        return format_ref(self.calendar_id, self.uid)


def format_ref(calendar_id: str, uid: str) -> str:
    return f"{calendar_id}{SEPARATOR}{uid}"


def parse_ref(task_ref: str) -> TaskRef:
    """Split a ref.  A UID may itself contain colons, so split only once."""
    if not isinstance(task_ref, str) or SEPARATOR not in task_ref:
        raise RefError(
            f"{task_ref!r} is not a task_ref; the form is '<calendar_id>:<uid>'",
            code="malformed_task_ref",
            task_ref=task_ref,
        )

    calendar_id, uid = task_ref.split(SEPARATOR, 1)
    if not _CALENDAR_ID_RE.match(calendar_id):
        raise RefError(
            f"{calendar_id!r} is not a calendar_id ({CALENDAR_ID_LEN} lowercase hex characters)",
            code="malformed_task_ref",
            task_ref=task_ref,
        )
    if not uid:
        raise RefError("task_ref has an empty uid", code="malformed_task_ref", task_ref=task_ref)
    return TaskRef(calendar_id=calendar_id, uid=uid)


def resolve(task_ref: str, conn: sqlite3.Connection, *, href: str | None = None) -> sqlite3.Row:
    """``task_ref`` → the ``tasks`` row, or a structured error."""
    from davpunk.core import cache

    ref = parse_ref(task_ref)
    rows = cache.find_by_uid(ref.calendar_id, ref.uid, conn)

    if href is not None:
        rows = [row for row in rows if row["href"] == href]

    if not rows:
        raise RefError(f"no task matches {task_ref}", code="task_not_found", task_ref=task_ref)
    if len(rows) > 1:
        raise AmbiguousRef(task_ref, [row["href"] for row in rows])
    return rows[0]


def ref_of(row: sqlite3.Row) -> str:
    return format_ref(row["calendar_id"], row["uid"])
