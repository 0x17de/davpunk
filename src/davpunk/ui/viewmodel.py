"""View logic with no Qt in it.

Grouping, kanban column assignment, subtask trees and sibling reordering are
all decisions about data, not about widgets.  Keeping them here means they are
testable on a machine with no display — which is also where most of the rules
that matter live (``due_deadline`` grouping, the cycle guard, the partial-run
rebalance).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum

from davpunk.config import KanbanColumn
from davpunk.models.task import (
    ORDER_STEP,
    Status,
    Task,
    due_deadline,
    local_timezone,
    next_local_midnight,
)

log = logging.getLogger("davpunk.ui.viewmodel")

#: Depth cap for the tree builder and recursive search.
DEPTH_CAP = 32

#: Drags inside this window coalesce into one rebalance.
REORDER_COALESCE_S = 2.0


class Bucket(StrEnum):
    OVERDUE = "Overdue"
    TODAY = "Today"
    UPCOMING = "Upcoming"
    NO_DUE_DATE = "No due date"
    COMPLETED = "Completed"


BUCKET_ORDER = (
    Bucket.OVERDUE,
    Bucket.TODAY,
    Bucket.UPCOMING,
    Bucket.NO_DUE_DATE,
    Bucket.COMPLETED,
)


# ------------------------------------------------------------------ grouping


def bucket_of(task: Task, now: datetime | None = None, tz: tzinfo | None = None) -> Bucket:
    """Which list-view group a task belongs in.

    Goes through :func:`due_deadline`, never through the coarse ``due`` sort
    key: a DATE-valued DUE is overdue only after the **end of that day, local
    time**, and comparing the sort key against ``now()`` marks a task due today
    as overdue as soon as the UTC day starts.
    """
    tz = tz or local_timezone()
    now = now or datetime.now(tz)

    if task.status in (Status.COMPLETED, Status.CANCELLED):
        return Bucket.COMPLETED

    deadline = due_deadline(task, tz)
    if deadline is None:
        return Bucket.NO_DUE_DATE
    if now > deadline:
        return Bucket.OVERDUE
    if deadline.astimezone(tz).date() == now.astimezone(tz).date():
        return Bucket.TODAY
    return Bucket.UPCOMING


def group_tasks(
    tasks: list[Task],
    now: datetime | None = None,
    tz: tzinfo | None = None,
    *,
    show_completed: bool = False,
) -> dict[Bucket, list[Task]]:
    tz = tz or local_timezone()
    now = now or datetime.now(tz)

    grouped: dict[Bucket, list[Task]] = {b: [] for b in BUCKET_ORDER}
    for task in tasks:
        bucket = bucket_of(task, now, tz)
        if bucket is Bucket.COMPLETED and not show_completed:
            continue
        grouped[bucket].append(task)

    for bucket in grouped:
        grouped[bucket].sort(key=sort_key)
    return {b: items for b, items in grouped.items() if items}


def sort_key(task: Task) -> tuple:
    """``(davpunk_order ASC NULLS LAST, uid ASC)``."""
    return (task.davpunk_order is None, task.davpunk_order or 0, task.uid)


def seconds_to_midnight(now: datetime | None = None, tz: tzinfo | None = None) -> float:
    """When the list view must re-bucket.

    An app left open overnight otherwise shows yesterday's buckets: a task that
    became overdue at midnight stays under "Today" until something else forces
    a refresh.
    """
    tz = tz or local_timezone()
    now = now or datetime.now(tz)
    return max(1.0, (next_local_midnight(now, tz) - now).total_seconds())


def relative_due(task: Task, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """The due column's text, in the local zone, naming a foreign one."""
    tz = tz or local_timezone()
    now = now or datetime.now(tz)
    deadline = due_deadline(task, tz)
    if deadline is None:
        return ""

    local_day = deadline.astimezone(tz).date()
    today = now.astimezone(tz).date()
    delta = (local_day - today).days

    if delta == 0:
        text = "today"
    elif delta == 1:
        text = "tomorrow"
    elif delta == -1:
        text = "yesterday"
    elif -7 < delta < 0:
        text = f"{-delta}d ago"
    elif 0 < delta < 7:
        text = f"in {delta}d"
    else:
        text = local_day.isoformat()

    if task.due_tzid and _zone_name(tz) != task.due_tzid:
        text += f" ({task.due_tzid})"
    return text


def _zone_name(tz: tzinfo) -> str | None:
    return getattr(tz, "key", None) or tz.tzname(datetime.now())


# -------------------------------------------------------------------- kanban


def column_of(task: Task, columns: list[KanbanColumn]) -> KanbanColumn:
    """Precedence: explicit override, then STATUS, then the first column.

    An orphaned override — one naming a column that is no longer configured —
    falls through to the STATUS rule and is rewritten on the next drag.  It is
    preserved in the ICS meanwhile.
    """
    if not columns:
        raise ValueError("at least one kanban column is required")

    if task.kanban_col:
        for column in columns:
            if column.id == task.kanban_col:
                return column
        log.debug("Orphaned kanban override %r on %s", task.kanban_col, task.uid[:8])

    if task.status:
        for column in columns:
            if column.status == task.status.value:
                return column

    return columns[0]


def kanban_board(tasks: list[Task], columns: list[KanbanColumn]) -> dict[str, list[Task]]:
    board: dict[str, list[Task]] = {column.id: [] for column in columns}
    for task in tasks:
        board[column_of(task, columns).id].append(task)
    for items in board.values():
        items.sort(key=sort_key)
    return board


def drop_fields(column: KanbanColumn) -> dict[str, object]:
    """A drag sets **both** STATUS and the override, in one update."""
    return {"status": Status(column.status), "kanban_col": column.id}


# ------------------------------------------------------------------- trees


@dataclass
class TreeNode:
    task: Task
    children: list[TreeNode] = field(default_factory=list)
    depth: int = 0
    #: A RELATED-TO pointing outside this calendar; rendered at root with a
    #: marker and preserved in the ICS.
    linked_parent_elsewhere: bool = False


def build_tree(tasks: list[Task]) -> list[TreeNode]:
    """Roots first, children nested, with a visited set and a depth cap.

    Parent resolution is **within one calendar only** — ``uid`` is deliberately
    non-unique across calendars — so a task whose parent is not in this list
    renders at root rather than disappearing.
    """
    by_uid: dict[tuple[str | None, str], Task] = {
        (task.calendar_id, task.uid): task for task in tasks
    }
    children: dict[tuple[str | None, str], list[Task]] = {}
    roots: list[Task] = []
    orphans: set[str] = set()

    for task in tasks:
        key = (task.calendar_id, task.parent_uid) if task.parent_uid else None
        if key is not None and key in by_uid:
            children.setdefault(key, []).append(task)
        else:
            if task.parent_uid:
                orphans.add(task.uid)
            roots.append(task)

    def walk(task: Task, depth: int, visited: set[str]) -> TreeNode:
        node = TreeNode(task=task, depth=depth, linked_parent_elsewhere=task.uid in orphans)
        if depth >= DEPTH_CAP:
            log.warning("Subtask depth cap (%d) reached at %s", DEPTH_CAP, task.uid[:8])
            return node
        for child in sorted(children.get((task.calendar_id, task.uid), []), key=sort_key):
            if child.uid in visited:
                # Break the link at the repeat and render the child at root.
                log.warning("Subtask cycle detected at %s; breaking the link", child.uid[:8])
                continue
            node.children.append(walk(child, depth + 1, visited | {child.uid}))
        return node

    return [walk(task, 0, {task.uid}) for task in sorted(roots, key=sort_key)]


def flatten(nodes: list[TreeNode]) -> list[TreeNode]:
    out: list[TreeNode] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node.children))
    return out


# ----------------------------------------------------------------- ordering


@dataclass
class Reorder:
    """What a drag actually has to write.

    Only the tasks in ``assignments`` are marked dirty; a rebalance that
    renumbered the whole sibling list would push every one of them to the
    server for a change the user did not make.
    """

    assignments: dict[str, int]
    rebalanced: bool = False

    @property
    def touched(self) -> int:
        return len(self.assignments)


def reorder_siblings(siblings: list[Task], moved_uid: str, new_index: int) -> Reorder:
    """Place ``moved_uid`` at ``new_index`` among its siblings.

    Insertion takes the midpoint of the surrounding orders.  When the gap has
    closed below 2 there is no midpoint left, and only the **contiguous run
    whose gaps actually closed** is renumbered.
    """
    ordered = sorted(siblings, key=sort_key)
    moved = next((t for t in ordered if t.uid == moved_uid), None)
    if moved is None:
        raise ValueError(f"{moved_uid} is not among these siblings")

    remaining = [t for t in ordered if t.uid != moved_uid]
    new_index = max(0, min(new_index, len(remaining)))

    before = remaining[new_index - 1] if new_index > 0 else None
    after = remaining[new_index] if new_index < len(remaining) else None

    low = before.davpunk_order if before and before.davpunk_order is not None else 0
    high = (
        after.davpunk_order if after and after.davpunk_order is not None else low + 2 * ORDER_STEP
    )

    if high - low >= 2:
        return Reorder({moved_uid: (low + high) // 2})

    # No midpoint left: renumber the closed run, and nothing beyond it.
    final = [*remaining[:new_index], moved, *remaining[new_index:]]
    run = _closed_run(final, new_index)
    assignments = {
        final[i].uid: (i + 1) * ORDER_STEP for i in run if _changes(final[i], (i + 1) * ORDER_STEP)
    }
    assignments[moved_uid] = (final.index(moved) + 1) * ORDER_STEP
    return Reorder(assignments, rebalanced=True)


def _closed_run(tasks: list[Task], around: int) -> range:
    """The contiguous span whose gaps are too small to insert into."""
    start = around
    while start > 0 and _gap(tasks[start - 1], tasks[start]) < 2:
        start -= 1
    end = around
    while end + 1 < len(tasks) and _gap(tasks[end], tasks[end + 1]) < 2:
        end += 1
    return range(start, end + 1)


def _gap(first: Task, second: Task) -> int:
    left = first.davpunk_order if first.davpunk_order is not None else 0
    right = second.davpunk_order if second.davpunk_order is not None else left + ORDER_STEP
    return right - left


def _changes(task: Task, value: int) -> bool:
    return task.davpunk_order != value


def initial_order(siblings: list[Task]) -> int:
    """The order for a task appended to a sibling list."""
    existing = [t.davpunk_order for t in siblings if t.davpunk_order is not None]
    return (max(existing) + ORDER_STEP) if existing else ORDER_STEP


# --------------------------------------------------------------- checklists


CHECKED = "- [x] "
UNCHECKED = "- [ ] "


def toggle_checklist_item(description: str, line_index: int) -> str:
    """Flip one ``- [ ]`` / ``- [x]`` line.

    Deliberately does **not** touch PERCENT-COMPLETE: an inline checklist is
    the user's own scratch structure, and inferring progress from it would
    overwrite a value they set explicitly.
    """
    lines = description.split("\n")
    if not 0 <= line_index < len(lines):
        return description

    line = lines[line_index]
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]

    if stripped.startswith(UNCHECKED):
        lines[line_index] = indent + CHECKED + stripped[len(UNCHECKED) :]
    elif stripped.startswith(CHECKED):
        lines[line_index] = indent + UNCHECKED + stripped[len(CHECKED) :]
    return "\n".join(lines)


def checklist_progress(description: str | None) -> tuple[int, int]:
    """``(done, total)`` — shown as a hint, never written to the task."""
    if not description:
        return (0, 0)
    done = total = 0
    for line in description.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(UNCHECKED):
            total += 1
        elif stripped.startswith(CHECKED):
            total += 1
            done += 1
    return (done, total)


# ------------------------------------------------------------------ loading


def load_tasks(
    conn: sqlite3.Connection,
    *,
    calendar_ids: list[str] | None = None,
    include_completed: bool = True,
) -> list[Task]:
    """Read tasks for a view.

    Short and in autocommit: the UI must never hold a read transaction across
    an event-loop turn, because a long-lived reader pins the WAL and blocks
    checkpointing.
    """
    from davpunk.core import cache

    sql = "SELECT * FROM tasks WHERE sync_state != 'pending_delete'"
    params: list[object] = []
    if calendar_ids:
        sql += f" AND calendar_id IN ({','.join('?' * len(calendar_ids))})"
        params.extend(calendar_ids)
    if not include_completed:
        sql += " AND (status IS NULL OR status NOT IN ('COMPLETED', 'CANCELLED'))"

    tasks = []
    for row in conn.execute(sql, params):
        task = cache.row_to_task(row)
        task.categories = cache.load_categories(row["id"], conn)
        tasks.append(task)
    return tasks


def refresh_fingerprint(conn: sqlite3.Connection) -> tuple[int, int]:
    """``(MAX(last_modified), COUNT(*))`` — the 2 s poll's cheap change check."""
    row = conn.execute("SELECT COALESCE(MAX(last_modified), 0), COUNT(*) FROM tasks").fetchone()
    return (int(row[0]), int(row[1]))


def day_range(day: date, tz: tzinfo | None = None) -> tuple[int, int]:
    """A precomputed local-midnight epoch range for a day-accurate SQL filter.

    SQL comparisons never use ``now()`` for day boundaries.
    """
    from davpunk.models.task import local_day_bounds

    return local_day_bounds(day, tz)


def upcoming_range(days: int, now: datetime | None = None, tz: tzinfo | None = None):
    tz = tz or local_timezone()
    now = now or datetime.now(tz)
    return day_range(now.date(), tz)[0], day_range(now.date() + timedelta(days=days), tz)[1]
