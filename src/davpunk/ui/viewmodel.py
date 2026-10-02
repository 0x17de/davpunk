"""View logic with no Qt in it.

Grouping, kanban column assignment, subtask trees and sibling reordering are
all decisions about data, not about widgets.  Keeping them here means they are
testable on a machine with no display — which is also where most of the rules
that matter live (``due_deadline`` grouping, the cycle guard, the partial-run
rebalance).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
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


def is_finished(task: Task) -> bool:
    """What "show completed" is about.

    Cancelled counts: it is finished work too, and a board that hid the one
    while keeping the other would only ever look like a bug.
    """
    return task.status in (Status.COMPLETED, Status.CANCELLED)


#: The "show completed" setting: ``False`` hides finished work, ``True`` shows
#: all of it, a number N shows what was finished in the last N days.
ShowCompleted = bool | int


#: What the menu and the preferences offer, in order, as ``(label, setting)``.
#: ``None`` is "custom": a day count typed in rather than picked.
SHOW_COMPLETED_CHOICES: tuple[tuple[str, ShowCompleted | None], ...] = (
    ("&Off", False),
    ("Last &3 days", 3),
    ("Last &7 days", 7),
    ("&Custom", None),
    ("&All", True),
)


def show_completed_choice(show: ShowCompleted) -> int:
    """The index in :data:`SHOW_COMPLETED_CHOICES` of the entry ``show`` is:
    "custom" for a day count none of the fixed ones names.  Matched by type
    as well as by value — ``True == 1``, but "all" is not "the last day"."""
    custom = 0
    for index, (_label, value) in enumerate(SHOW_COMPLETED_CHOICES):
        if value is None:
            custom = index
        elif type(value) is type(show) and value == show:
            return index
    return custom


def finished_at(task: Task) -> datetime | None:
    """When a finished task was finished, as near as the data says.

    COMPLETED where there is one.  A cancelled task never has one — the
    invariant clears it — and neither does one finished by a client that does
    not write it, so LAST-MODIFIED stands in: what finished it was very likely
    the last thing done to it.
    """
    stamp = task.completed if task.completed is not None else task.last_modified
    return None if stamp is None else datetime.fromtimestamp(stamp, UTC)


def finished_cutoff(
    show: ShowCompleted, now: datetime | None = None, tz: tzinfo | None = None
) -> datetime | None:
    """The earliest finish a day window still shows, or ``None`` for none.

    Counted in local calendar days with today as the first, not in 24-hour
    stretches: "the last 3 days" is today, yesterday and the day before, so a
    card does not vanish from the Done column in the middle of an afternoon.
    """
    if isinstance(show, bool):
        return None
    tz = tz or local_timezone()
    now = now or datetime.now(tz)
    first_day = now.astimezone(tz).date() - timedelta(days=show - 1)
    return datetime.combine(first_day, time.min, tzinfo=tz)


def hide_finished(
    tasks: list[Task],
    show: ShowCompleted,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> list[Task]:
    """``tasks`` without the finished ones ``show`` leaves out.

    A finished task with no date at all is left out of a day window: there is
    nothing to say it is recent, and "all" is one click away.
    """
    if show is True:
        return tasks
    cutoff = finished_cutoff(show, now, tz)

    def shown(task: Task) -> bool:
        if not is_finished(task):
            return True
        if cutoff is None:
            return False
        at = finished_at(task)
        return at is not None and at >= cutoff

    return [task for task in tasks if shown(task)]


def bucket_of(task: Task, now: datetime | None = None, tz: tzinfo | None = None) -> Bucket:
    """Which list-view group a task belongs in.

    Goes through :func:`due_deadline`, never through the coarse ``due`` sort
    key: a DATE-valued DUE is overdue only after the **end of that day, local
    time**, and comparing the sort key against ``now()`` marks a task due today
    as overdue as soon as the UTC day starts.
    """
    tz = tz or local_timezone()
    now = now or datetime.now(tz)

    if is_finished(task):
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
    show_completed: ShowCompleted = False,
) -> dict[Bucket, list[Task]]:
    tz = tz or local_timezone()
    now = now or datetime.now(tz)

    grouped: dict[Bucket, list[Task]] = {b: [] for b in BUCKET_ORDER}
    for task in hide_finished(tasks, show_completed, now, tz):
        grouped[bucket_of(task, now, tz)].append(task)

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


def column_of(task: Task, columns: list[KanbanColumn]) -> KanbanColumn | None:
    """Precedence: explicit override, then STATUS, then nowhere.

    An orphaned override — one naming a column that is no longer configured —
    falls through to the STATUS rule and is rewritten on the next drag.  It is
    preserved in the ICS meanwhile.

    **No STATUS is a value here, not a gap.** A task nobody has looked at yet
    and one explicitly marked NEEDS-ACTION are different things — a pool to
    pick from, and work that has been picked — so a column may declare
    ``status = None`` and collect the first.

    ``None`` means "not on this board": the task's status names no configured
    column.  Dropping the Done and Cancelled columns is how you stop looking
    at finished work, and it only works if the cards go with them — piling
    them into the first column instead would make the board *worse*.  Nothing
    is lost: the list view and search show every task regardless, and the
    board says how many it is not showing.
    """
    if not columns:
        raise ValueError("at least one kanban column is required")

    if task.kanban_col:
        for column in columns:
            if column.id == task.kanban_col:
                return column
        log.debug("Orphaned kanban override %r on %s", task.kanban_col, task.uid[:8])

    wanted = task.status.value if task.status else None
    for column in columns:
        if column.status == wanted:
            return column

    return None


def kanban_board(
    tasks: list[Task],
    columns: list[KanbanColumn],
    task_filter: TaskFilter | None = None,
) -> dict[str, list[Task]]:
    board: dict[str, list[Task]] = {column.id: [] for column in columns}
    for task in apply_filter(tasks, task_filter):
        column = column_of(task, columns)
        if column is not None:
            board[column.id].append(task)
    for items in board.values():
        items.sort(key=sort_key)
    return board


def drop_fields(column: KanbanColumn) -> dict[str, object]:
    """A drag sets **both** STATUS and the override, in one update.

    A column standing for "no STATUS" clears the property rather than writing
    one — dragging a task back to the pool has to be able to undo having
    picked it up.
    """
    return {
        "status": Status(column.status) if column.status else None,
        "kanban_col": column.id,
    }


# ------------------------------------------------------------------ filtering


@dataclass(frozen=True)
class TaskFilter:
    """Which tasks a view should show.

    Empty means "no restriction on this axis", not "show nothing" — an empty
    filter is the natural starting state, and treating it as an exclusion would
    make a fresh view look broken.
    """

    calendar_ids: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    #: With several tags selected: require all of them rather than any.
    match_all_tags: bool = False
    text: str = ""

    @property
    def is_active(self) -> bool:
        return bool(self.calendar_ids or self.tags or self.text.strip())

    def matches(self, task: Task) -> bool:
        if self.calendar_ids and task.calendar_id not in self.calendar_ids:
            return False

        if self.tags:
            have = set(task.categories)
            if self.match_all_tags:
                if not self.tags <= have:
                    return False
            elif not (self.tags & have):
                return False

        needle = self.text.strip().casefold()
        if needle:
            haystack = f"{task.summary or ''}\n{task.description or ''}".casefold()
            if needle not in haystack:
                return False
        return True

    def describe(self, calendar_names: dict[str, str] | None = None) -> str:
        """A short summary for the filter bar."""
        if not self.is_active:
            return "No filter"
        names = calendar_names or {}
        parts = []
        if self.calendar_ids:
            listed = sorted(names.get(c, c[:8]) for c in self.calendar_ids)
            parts.append(listed[0] if len(listed) == 1 else f"{len(listed)} lists")
        if self.tags:
            joiner = " + " if self.match_all_tags else ", "
            parts.append(joiner.join(sorted(self.tags)))
        if self.text.strip():
            parts.append(f"“{self.text.strip()}”")
        return " · ".join(parts)


def apply_filter(tasks: list[Task], task_filter: TaskFilter | None) -> list[Task]:
    if task_filter is None or not task_filter.is_active:
        return list(tasks)
    return [task for task in tasks if task_filter.matches(task)]


def available_tags(tasks: list[Task]) -> list[str]:
    """Every tag actually in use, so the picker cannot offer a dead one."""
    return sorted({tag for task in tasks for tag in task.categories})


# ------------------------------------------------------------------- trees


@dataclass
class TreeNode:
    task: Task
    children: list[TreeNode] = field(default_factory=list)
    depth: int = 0
    #: A RELATED-TO pointing outside this calendar; rendered at root with a
    #: marker and preserved in the ICS.
    linked_parent_elsewhere: bool = False
    #: Not one of the tasks in this slice: a relative pulled in so the shape of
    #: the tree survives the slicing — an ancestor above, or a subtask that
    #: lives in another slice below.  Rendered grey and inert: it is context,
    #: not a row you can act on.
    is_context: bool = False


def build_tree(
    tasks: list[Task],
    universe: list[Task] | None = None,
    shown: list[Task] | None = None,
) -> list[TreeNode]:
    """Roots first, children nested, with a visited set and a depth cap.

    Parent resolution is **within one calendar only** — ``uid`` is deliberately
    non-unique across calendars — so a task whose parent is not in this list
    renders at root rather than disappearing.

    ``universe`` is every task that exists, for when ``tasks`` is a slice of a
    view rather than all of it — one bucket, one kanban column, one filter.
    Without it a child whose parent merely landed in a *different* slice looks
    exactly like one whose parent is in another calendar, and wrongly gets the
    "linked parent elsewhere" marker.  With it, that marker means what it says.

    A parent that *is* in the universe but not in this slice comes along as a
    **context node**: a subtask shown at the root of a column it did not choose
    reads as a root task, and the one thing that would explain it — who it
    hangs under — is exactly what the slice threw away.  The chain stops at the
    first ancestor the slice already shows, so a parent in this column and a
    grandparent in another nest the way they look.

    ``shown`` is every task this *view* has on screen, across all its slices,
    and turns the same idea downwards: a subtask that went to another slice is
    pulled back under its parent as a context node instead of vanishing from
    under it.  A parent in Needs Action whose one subtask is In Progress
    otherwise looks like a parent with no subtasks at all, which is exactly
    backwards — it is the only one of the two that is *not* finished with.
    Grey and inert here, live in the slice it actually belongs to.

    It is deliberately a separate list from ``universe``: only what the view
    already shows somewhere may be pulled back in.  Drawing on the universe
    instead would put completed subtasks back under their parents the moment
    "show completed" was turned off, and let a filtered-out task through — the
    two controls whose whole job is to take rows away.
    """
    in_slice: dict[tuple[str | None, str], Task] = {
        (task.calendar_id, task.uid): task for task in tasks
    }
    known: dict[tuple[str | None, str], Task] = (
        {(task.calendar_id, task.uid): task for task in universe}
        if universe is not None
        else dict(in_slice)
    )
    children: dict[tuple[str | None, str], list[Task]] = {}
    roots: list[Task] = []
    orphans: set[str] = set()
    context: dict[tuple[str | None, str], Task] = {}
    elsewhere: dict[tuple[str | None, str], list[Task]] = {}
    for task in shown or ():
        if task.parent_uid:
            elsewhere.setdefault((task.calendar_id, task.parent_uid), []).append(task)

    def pull_in(task: Task) -> None:
        """Hang ``task`` off its ancestors, adding the ones this slice lacks."""
        seen: set[tuple[str | None, str]] = set()
        parent_uid: str | None = task.parent_uid
        while parent_uid is not None:
            key = (task.calendar_id, parent_uid)
            children.setdefault(key, []).append(task)
            if key in in_slice or key in context:
                return  # the rest of the chain is already there
            parent = known[key]
            context[key] = parent

            above = (parent.calendar_id, parent.parent_uid) if parent.parent_uid else None
            if above is None or above not in known or above in seen or len(seen) >= DEPTH_CAP:
                # Nothing further this view can resolve — or a cycle, which
                # ends here rather than dropping the whole chain on the floor.
                roots.append(parent)
                return
            seen.add(key)
            task, parent_uid = parent, parent.parent_uid

    def pull_down(task: Task) -> None:
        """Hang the subtasks that went to other slices under ``task``, greyed.

        Only under rows this slice actually has: a context ancestor keeps just
        the children that brought it here.  Pulling *its* siblings' subtrees in
        too would end with every column drawing the whole tree in grey, which
        is the tree view, not a board.
        """
        stack = [(task, 0)]
        while stack:
            parent, depth = stack.pop()
            if depth >= DEPTH_CAP:
                continue
            key = (parent.calendar_id, parent.uid)
            for child in elsewhere.get(key, ()):
                child_key = (child.calendar_id, child.uid)
                # Already placed — as a row of its own, or as scaffolding the
                # walk up put there.  This is also what ends a cycle.
                if child_key in in_slice or child_key in context:
                    continue
                context[child_key] = child
                children.setdefault(key, []).append(child)
                stack.append((child, depth + 1))

    for task in tasks:
        key = (task.calendar_id, task.parent_uid) if task.parent_uid else None
        if key is None:
            roots.append(task)
        elif key in in_slice:
            children.setdefault(key, []).append(task)
        elif key in known:
            pull_in(task)
        else:
            orphans.add(task.uid)
            roots.append(task)

    # After the walk up, so an ancestor already pulled in is not pulled in a
    # second time as somebody's child.
    for task in tasks:
        pull_down(task)

    def walk(task: Task, depth: int, visited: set[str]) -> TreeNode:
        node = TreeNode(
            task=task,
            depth=depth,
            linked_parent_elsewhere=task.uid in orphans,
            is_context=(task.calendar_id, task.uid) in context,
        )
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


def subtree_size(node: TreeNode) -> int:
    """How many tasks a folded node is hiding."""
    return sum(1 + subtree_size(child) for child in node.children)


@dataclass
class FoldState:
    """Which subtrees are open, remembered across refreshes.

    A refresh rebuilds every row, so without this the tree silently re-opens
    itself every few seconds and folding is useless.  Two sets rather than one:
    "never seen" has to be distinguishable from "the user closed it", so that a
    node appearing later can take the caller's default instead of inheriting
    whatever the last node with that key happened to do.
    """

    open_keys: set = field(default_factory=set)
    closed_keys: set = field(default_factory=set)

    def is_open(self, key, *, default: bool) -> bool:
        if key in self.open_keys:
            return True
        if key in self.closed_keys:
            return False
        return default

    def remember(self, key, is_open: bool) -> None:
        target, other = (
            (self.open_keys, self.closed_keys) if is_open else (self.closed_keys, self.open_keys)
        )
        target.add(key)
        other.discard(key)

    def set_all(self, keys, is_open: bool) -> None:
        for key in keys:
            self.remember(key, is_open)


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


def descendants(task: Task, tasks: list[Task]) -> set[str]:
    """Every uid below ``task`` in its own calendar, cycle-safe."""
    children: dict[str, list[str]] = {}
    for other in tasks:
        if other.calendar_id == task.calendar_id and other.parent_uid:
            children.setdefault(other.parent_uid, []).append(other.uid)

    found: set[str] = set()
    stack = [task.uid]
    while stack:
        for uid in children.get(stack.pop(), ()):
            if uid not in found:
                found.add(uid)
                stack.append(uid)
    return found


def reparent_fields(task: Task, new_parent_uid: str | None, tasks: list[Task]) -> dict | None:
    """What to write to move ``task`` under ``new_parent_uid``.

    ``None`` when the move is not allowed — onto itself, or onto one of its own
    descendants, which would build a cycle the tree walker would then have to
    break.  Refusing here is better than rendering the wreckage afterwards.

    The task lands at the *end* of its new siblings: it has an order value from
    the group it left, and reusing it would drop the task at an arbitrary point
    in a group it has never been in.
    """
    if new_parent_uid == task.uid or new_parent_uid == task.parent_uid:
        return None
    if new_parent_uid is not None and new_parent_uid in descendants(task, tasks):
        return None

    siblings = [
        t
        for t in tasks
        if t.calendar_id == task.calendar_id
        and t.parent_uid == new_parent_uid
        and t.uid != task.uid
    ]
    orders = [t.davpunk_order for t in siblings if t.davpunk_order is not None]
    last = max(orders) if orders else 0
    return {"parent_uid": new_parent_uid, "davpunk_order": last + ORDER_STEP}


def indent_fields(task: Task, tasks: list[Task]) -> dict | None:
    """Make ``task`` a child of the sibling above it — the outliner convention.

    The first task in a group has nothing to indent under, and says so by
    returning ``None`` rather than quietly doing nothing else.
    """
    siblings = sorted(
        (t for t in tasks if t.calendar_id == task.calendar_id and t.parent_uid == task.parent_uid),
        key=sort_key,
    )
    index = next((i for i, t in enumerate(siblings) if t.uid == task.uid), None)
    if index is None or index == 0:
        return None
    return reparent_fields(task, siblings[index - 1].uid, tasks)


def outdent_fields(task: Task, tasks: list[Task]) -> dict | None:
    """Promote ``task`` to sit beside its parent.  A root has nowhere to go."""
    if not task.parent_uid:
        return None
    parent = next(
        (t for t in tasks if t.calendar_id == task.calendar_id and t.uid == task.parent_uid),
        None,
    )
    if parent is None:
        return None
    return reparent_fields(task, parent.parent_uid, tasks)


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


# ------------------------------------------------------------------- drops


class DropPosition(StrEnum):
    """Where the drop indicator was when the mouse came up."""

    ON = "on"
    ABOVE = "above"
    BELOW = "below"


@dataclass
class ListDrop:
    """What a list-view drop has to write.

    ``orders`` carries the dragged task and, when a gap closed, the contiguous
    run that had to be renumbered with it — the same partial rebalance a
    keyboard reorder does, for the same reason.
    """

    parent_uid: str | None
    orders: dict[str, int]
    rebalanced: bool = False

    @property
    def touched(self) -> int:
        return len(self.orders)


def plan_list_drop(
    task: Task, target: Task, position: DropPosition, tasks: list[Task]
) -> ListDrop | None:
    """Nest ``task`` under ``target``, or place it beside ``target``.

    ``None`` for every refusal, so the view has one thing to check: onto
    itself, onto one of its own descendants, into another calendar, or a drop
    that would not move the task at all.  A cross-calendar drop is refused
    rather than turned into a move, because ``RELATED-TO`` resolves within one
    calendar only and the link would never render.

    Dropping onto a bucket heading never reaches here: a heading carries no
    task, and a bucket is a computed view of DUE rather than a settable field.
    """
    if target.calendar_id != task.calendar_id or target.uid == task.uid:
        return None

    if position is DropPosition.ON:
        fields = reparent_fields(task, target.uid, tasks)
        if fields is None:
            return None
        return ListDrop(
            parent_uid=fields["parent_uid"],
            orders={task.uid: fields["davpunk_order"]},
        )

    new_parent = target.parent_uid
    if new_parent == task.uid or (
        new_parent is not None and new_parent in descendants(task, tasks)
    ):
        return None

    siblings = [
        t
        for t in tasks
        if t.calendar_id == task.calendar_id and t.parent_uid == new_parent and t.uid != task.uid
    ]
    ordered = sorted(siblings, key=sort_key)
    index = next((i for i, t in enumerate(ordered) if t.uid == target.uid), None)
    if index is None:
        return None
    if position is DropPosition.BELOW:
        index += 1

    if task.parent_uid == new_parent:
        # Removing the task shifts everything after it left by one, so the slot
        # it already occupies among the *remaining* siblings is its own index.
        full = sorted([*siblings, task], key=sort_key)
        current = next(i for i, t in enumerate(full) if t.uid == task.uid)
        if index == current:
            return None

    result = reorder_siblings([*siblings, task], task.uid, index)
    return ListDrop(new_parent, result.assignments, result.rebalanced)


# ------------------------------------------------------------------- paste


@dataclass
class TaskClipboard:
    """What a cut is holding, in this process only.

    Ids rather than whole tasks: the row can change — or be deleted — between
    the cut and the paste, so a paste re-reads it and a snapshot would write
    stale values back over it.  ``(calendar_id, uid)`` rides along so a
    vanished entry can still be named in a message.

    The system clipboard is deliberately not involved.  A local task id means
    nothing outside this process, and pasting one into a text editor is noise.
    """

    entries: list[tuple[str, str | None, str]] = field(default_factory=list)

    def cut(self, tasks: list[Task]) -> None:
        """Replaces whatever was held: there is one clipboard, not a stack."""
        self.entries = [(t.id, t.calendar_id, t.uid) for t in tasks if t.id]

    def clear(self) -> None:
        self.entries = []

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def task_ids(self) -> list[str]:
        return [entry[0] for entry in self.entries]


@dataclass
class PastePlan:
    """One cut task's destination.  The planner never writes."""

    task: Task
    calendar_id: str | None
    parent_uid: str | None
    davpunk_order: int

    @property
    def needs_move(self) -> bool:
        """A different calendar is a two-stage PUT/DELETE, not a column write."""
        return self.task.calendar_id != self.calendar_id

    @property
    def fields(self) -> dict[str, object]:
        return {"parent_uid": self.parent_uid, "davpunk_order": self.davpunk_order}


def topmost(chosen: list[Task], tasks: list[Task]) -> list[Task]:
    """``chosen`` with anything an ancestor in the same set already carries.

    Selecting a parent and one of its children and then acting on both moves
    the child twice — once inside its parent's subtree, once on its own, which
    is what un-nests it.  Dropping it here is what makes "select a range and
    drag it" behave the way it looks.
    """
    return [
        entry
        for entry in chosen
        if not any(
            other.uid != entry.uid
            and other.calendar_id == entry.calendar_id
            and entry.uid in descendants(other, tasks)
            for other in chosen
        )
    ]


def plan_paste(cut: list[Task], target: Task | None, tasks: list[Task]) -> list[PastePlan]:
    """Where each cut task lands when pasted onto ``target``.

    Cut and paste exists for the case a drag cannot reach: a filter is hiding
    the parent you want, so the two tasks are never on screen together.  The
    plan is therefore computed against the whole task list, not the slice a
    view happens to be rendering.

    ``target`` of ``None`` means "become a root task", each in its own
    calendar.  An empty list means the paste was refused — pasting into your
    own cut set would build a cycle.  Descendants of another cut task are
    dropped silently: the ancestor's move already carries them.
    """
    if not cut:
        return []

    if target is not None:
        for entry in cut:
            if (target.calendar_id, target.uid) == (entry.calendar_id, entry.uid):
                return []
            if target.calendar_id == entry.calendar_id and target.uid in descendants(entry, tasks):
                return []

    tops = topmost(cut, tasks)
    cut_keys = {(t.calendar_id, t.uid) for t in cut}
    new_parent = target.uid if target is not None else None

    plans: list[PastePlan] = []
    last_order: dict[str | None, int] = {}
    for entry in sorted(tops, key=sort_key):
        calendar_id = target.calendar_id if target is not None else entry.calendar_id
        if calendar_id == entry.calendar_id and new_parent == entry.parent_uid:
            continue  # already exactly where it is being pasted

        if calendar_id not in last_order:
            siblings = [
                t
                for t in tasks
                if t.calendar_id == calendar_id
                and t.parent_uid == new_parent
                and (t.calendar_id, t.uid) not in cut_keys
            ]
            last_order[calendar_id] = max(
                (t.davpunk_order for t in siblings if t.davpunk_order is not None), default=0
            )
        last_order[calendar_id] += ORDER_STEP
        plans.append(PastePlan(entry, calendar_id, new_parent, last_order[calendar_id]))
    return plans


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


# ------------------------------------------------------------ bulk entry


#: What a list dragged in from somewhere else carries with it: markdown
#: bullets, and ordinals from a numbered list.
_BULLET = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
#: A checklist box, once its bullet is gone — or written without one.
_CHECKBOX = re.compile(r"^\[[ xX]\]\s*")


def parse_subtask_lines(text: str) -> list[str]:
    """One summary per non-empty line, for the bulk-subtask dialog.

    The markers are stripped rather than kept, because a SUMMARY reading
    ``- [ ] Buy milk`` is a checkbox nothing will ever tick: the box is a
    convention :func:`toggle_checklist_item` honours *inside a description*,
    and outside one it is just four characters in front of the title.

    Indentation is stripped too rather than read as nesting.  Every line
    becomes a direct child of the one task the user picked — a depth inferred
    from leading spaces is a tree nobody asked for, and one that a paste from
    a wrapped editor would get wrong.
    """
    summaries = []
    for raw in text.splitlines():
        line = _BULLET.sub("", raw.strip(), count=1)
        line = _CHECKBOX.sub("", line, count=1).strip()
        if line:
            summaries.append(line)
    return summaries


def bulk_subtasks(
    parent: Task,
    summaries: list[str],
    siblings: list[Task],
    *,
    status: Status | None = None,
) -> list[Task]:
    """The tasks a bulk add is about to create, in the order they were typed.

    Orders are handed out here in one pass rather than by calling
    :func:`initial_order` per task: that reads the sibling group back out of
    the database, and every task in a batch is created before any of them can
    be read again — so each would be told the same "end of the list" and the
    typed order would survive only by accident.
    """
    order = initial_order(siblings)
    tasks = []
    for summary in summaries:
        tasks.append(
            Task(
                uid=uuid.uuid4().hex,
                calendar_id=parent.calendar_id,
                parent_uid=parent.uid,
                summary=summary,
                status=status,
                davpunk_order=order,
            )
        )
        order += ORDER_STEP
    return tasks


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
