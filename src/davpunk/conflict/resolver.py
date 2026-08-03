"""Conflict resolution.  User-driven wherever there is a question to ask.

Three dialog modes, distinguished by which side of the conflict row is NULL:

===============  ================  ======  ==================================
``local_raw_ics``  ``remote_raw_ics``  Mode  Buttons
===============  ================  ======  ==================================
set              set               A       three-pane merge window: Accept · Skip
NULL             set               A′      Delete anyway · Keep server version
set              NULL              B       Recreate on server · Accept deletion
===============  ================  ======  ==================================

Mode A is a **merge**, not a choice of side: :class:`MergeState` holds one
decision per :class:`FieldGroup` — take the server's, take mine, or a value the
user typed — and :func:`resolution_for` turns the result into the cheapest
resolution that expresses it.

Fields travel in **groups** because some of them are only meaningful together:
``DUE`` without its ``TZID`` is a different instant, so the two move as one.

A few fields are not the user's to decide at all.  :data:`AUTO_FIELDS` is
positional bookkeeping written by drag and drop, never typed, so the newer side
simply wins — and when two versions differ in *nothing else*,
:func:`auto_resolve` settles the whole conflict without a window.
"""

from __future__ import annotations

import copy
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


class Side(StrEnum):
    """Which column of the merge window a value came from."""

    REMOTE = "remote"
    LOCAL = "local"

    @property
    def other(self) -> Side:
        return Side.LOCAL if self is Side.REMOTE else Side.REMOTE


class FieldKind(StrEnum):
    """How the merge window renders and edits a group."""

    TEXT = "text"
    MULTILINE = "multiline"
    STATUS = "status"
    PRIORITY = "priority"
    PERCENT = "percent"
    DATETIME = "datetime"
    TAGS = "tags"
    OPAQUE = "opaque"  # shown and transferable, but never hand-edited


@dataclass(frozen=True)
class FieldGroup:
    """One row of the merge window.

    ``fields`` move together or not at all.  A ``DUE`` value carried over
    without its ``TZID`` names a different instant, which is exactly the kind of
    silent corruption a merge window exists to prevent.
    """

    name: str
    label: str
    fields: tuple[str, ...]
    kind: FieldKind

    @property
    def primary(self) -> str:
        return self.fields[0]


#: The rows of the merge window, in the order they are shown.
DIFF_GROUPS: tuple[FieldGroup, ...] = (
    FieldGroup("summary", "Summary", ("summary",), FieldKind.TEXT),
    FieldGroup("description", "Description", ("description",), FieldKind.MULTILINE),
    # kanban_col rides with the status: a card drag writes both, and the board
    # lets the override outrank the STATUS it was set beside — so a status from
    # one side with an override from the other puts the card in a column that
    # contradicts it.
    FieldGroup("status", "Status", ("status", "kanban_col"), FieldKind.STATUS),
    FieldGroup("priority", "Priority", ("priority",), FieldKind.PRIORITY),
    FieldGroup("percent_complete", "% complete", ("percent_complete",), FieldKind.PERCENT),
    FieldGroup("due", "Due", ("due_value", "due_tzid"), FieldKind.DATETIME),
    FieldGroup("dtstart", "Start", ("dtstart_value", "dtstart_tzid"), FieldKind.DATETIME),
    FieldGroup("location", "Location", ("location",), FieldKind.TEXT),
    FieldGroup("url", "URL", ("url",), FieldKind.TEXT),
    FieldGroup("categories", "Tags", ("categories",), FieldKind.TAGS),
    FieldGroup("alarms", "Alarms", ("alarms",), FieldKind.OPAQUE),
    FieldGroup("rrule", "Recurrence", ("rrule",), FieldKind.OPAQUE),
)

GROUPS_BY_NAME: dict[str, FieldGroup] = {group.name: group for group in DIFF_GROUPS}

#: Every model field the merge window can carry across, flattened.
DIFF_FIELDS: tuple[str, ...] = tuple(f for group in DIFF_GROUPS for f in group.fields)

#: Everything a resolution writes: the diffed fields plus the ones that ride
#: along with the base task.  :func:`resolution_for` compares over these, so the
#: "this is just the server's version" shortcut can never quietly drop one.
MERGED_FIELDS: tuple[str, ...] = (
    *DIFF_FIELDS,
    "completed",
    "parent_uid",
    "davpunk_order",
)

#: Fields the merge window never shows, because there is nothing to ask.
#:
#: ``X-DAVPUNK-ORDER`` is a position in a list, written by dragging and never
#: typed, so a wrong choice costs a re-drag rather than lost text.  The newer
#: side takes it, on the same whole-resource judgement :func:`newer_side` makes
#: for the centre column — iCalendar timestamps the resource, not the property,
#: so there is no finer "latest" available for it either.
#:
#: ``parent_uid`` is deliberately *not* here.  Reparenting a subtask is
#: something the user meant, not bookkeeping, and it rides with the base task.
AUTO_FIELDS: tuple[str, ...] = ("davpunk_order",)

#: What :func:`only_auto_differs` compares: everything a resolution writes,
#: minus the fields that settle themselves.
DECIDED_FIELDS: tuple[str, ...] = tuple(f for f in MERGED_FIELDS if f not in AUTO_FIELDS)

#: Groups taken wholesale from one side or the other, never sub-field merged.
#: ``description`` is atomic to preserve inline-checklist integrity.
ATOMIC_FIELDS = frozenset({"description"})


class ConflictError(Exception):
    pass


@dataclass
class FieldDiff:
    """One group's value on each side."""

    group: FieldGroup
    local_values: dict[str, Any]
    remote_values: dict[str, Any]

    @property
    def field(self) -> str:
        return self.group.name

    @property
    def label(self) -> str:
        return self.group.label

    @property
    def local(self) -> Any:
        return self.local_values[self.group.primary]

    @property
    def remote(self) -> Any:
        return self.remote_values[self.group.primary]

    @property
    def differs(self) -> bool:
        return self.local_values != self.remote_values

    @property
    def atomic(self) -> bool:
        return self.group.name in ATOMIC_FIELDS

    def values(self, side: Side) -> dict[str, Any]:
        source = self.remote_values if Side(side) is Side.REMOTE else self.local_values
        return copy.deepcopy(source)


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
    deferred_at: int | None = None

    @property
    def resolutions(self) -> tuple[Resolution, ...]:
        return ALLOWED[self.mode]

    @property
    def changed(self) -> list[FieldDiff]:
        return [d for d in self.diffs if d.differs]

    def diff_for(self, group: str) -> FieldDiff:
        for candidate in self.diffs:
            if candidate.field == group:
                return candidate
        raise KeyError(group)

    def task_for(self, side: Side) -> Task | None:
        return self.remote if Side(side) is Side.REMOTE else self.local


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
        deferred_at=row["deferred_at"],
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
    """Group-by-group comparison for the merge window's table."""
    return [
        FieldDiff(
            group=group,
            local_values=_group_values(local, group),
            remote_values=_group_values(remote, group),
        )
        for group in DIFF_GROUPS
    ]


def _group_values(task: Task | None, group: FieldGroup) -> dict[str, Any]:
    if task is None:
        return dict.fromkeys(group.fields)
    return {name: copy.deepcopy(getattr(task, name)) for name in group.fields}


def newer_of(local: Task | None, remote: Task | None) -> Side:
    """The "latest" of two versions, whole-resource.

    iCalendar carries no per-property timestamps, so *latest* is necessarily a
    judgement about the whole resource rather than about one field:
    ``LAST-MODIFIED`` first, then ``SEQUENCE``, then local.  Local wins the tie
    because it is the edit the user can still remember making, and because it
    is the one that would otherwise be lost without a trace.
    """
    if remote is None:
        return Side.LOCAL
    if local is None:
        return Side.REMOTE

    local_modified, remote_modified = local.last_modified, remote.last_modified
    if local_modified is not None and remote_modified is not None:
        if remote_modified > local_modified:
            return Side.REMOTE
        if local_modified > remote_modified:
            return Side.LOCAL
    elif remote_modified is not None and local_modified is None:
        return Side.REMOTE

    if remote.sequence > local.sequence:
        return Side.REMOTE
    return Side.LOCAL


def newer_side(view: ConflictView) -> Side:
    """Which side the centre column starts from."""
    return newer_of(view.local, view.remote)


def only_auto_differs(local: Task | None, remote: Task | None) -> bool:
    """Do these two versions differ in nothing but :data:`AUTO_FIELDS`?

    Nothing is asserted about the auto fields themselves — equal is fine, and
    is how a *spurious* conflict (two clients that agree, racing on ETags)
    reads.  What matters is that no field the user would be asked about moved,
    because then there is no question to put in front of them.
    """
    if local is None or remote is None:
        return False  # one side is a deletion: modes A′ and B, always a question
    return all(getattr(local, name) == getattr(remote, name) for name in DECIDED_FIELDS)


class MergeState:
    """The centre column: one decision per group, plus any hand-edited values.

    A decision is either a :class:`Side` or — once the user has typed something
    that matches neither side — an explicit value.  Nothing here touches the
    database; :func:`resolution_for` turns the accumulated decisions into a
    resolution and a task.
    """

    def __init__(self, view: ConflictView, sides: dict[str, Side] | None = None) -> None:
        self.view = view
        self.default_side = newer_side(view)
        self.sides: dict[str, Side] = {group.name: self.default_side for group in DIFF_GROUPS}
        if sides:
            self.sides.update({name: Side(side) for name, side in sides.items()})
        self.edits: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- decisions

    def take(self, group: str, side: Side | str) -> None:
        """Transfer one side into the centre, discarding any edit of that row."""
        self.sides[group] = Side(side)
        self.edits.pop(group, None)

    def take_all(self, side: Side | str) -> None:
        for group in DIFF_GROUPS:
            self.take(group.name, side)

    def edit(self, group: str, values: dict[str, Any]) -> None:
        """Record a hand-typed centre value.

        Typing a value that happens to equal one side is recorded as *taking*
        that side: the row is then no longer "edited", which is what the user
        sees and what the provenance marker has to say.
        """
        for side in (Side.LOCAL, Side.REMOTE):
            if values == self.side_values(group, side):
                self.take(group, side)
                return
        self.edits[group] = copy.deepcopy(values)

    # ----------------------------------------------------------------- reads

    def side_values(self, group: str, side: Side | str) -> dict[str, Any]:
        """One side's values, falling back to the other when that side is gone."""
        side = Side(side)
        if self.view.task_for(side) is None:
            side = side.other
        return self.view.diff_for(group).values(side)

    def values(self, group: str) -> dict[str, Any]:
        if group in self.edits:
            return copy.deepcopy(self.edits[group])
        return self.side_values(group, self.sides[group])

    def origin(self, group: str) -> Side | None:
        """The side this row came from, or ``None`` when it was hand-edited."""
        return None if group in self.edits else self.sides[group]

    def differs(self, group: str) -> bool:
        return self.view.diff_for(group).differs

    # ---------------------------------------------------------------- result

    def apply(self) -> Task:
        """The task the centre column describes."""
        base = self.view.local or self.view.remote
        if base is None:
            raise ConflictError("nothing to merge")

        merged = base.model_copy(deep=True)
        for group in DIFF_GROUPS:
            for name, value in self.values(group.name).items():
                setattr(merged, name, value)

        # The base is local whenever local exists, so without this the auto
        # fields would silently keep the local value however the rest went —
        # and "take all server" could not come out as a restore_server.
        auto_source = self.view.task_for(self.default_side) or base
        for name in AUTO_FIELDS:
            setattr(merged, name, getattr(auto_source, name))
        return merged.canonicalized()


def resolution_for(state: MergeState) -> tuple[Resolution, Task | None]:
    """The cheapest resolution that expresses the centre column.

    A result identical to the server's version is a ``restore_server``, not a
    ``merge``: the server already holds those bytes, so PUTing them back is
    churn and one more chance to lose a race.
    """
    merged = state.apply()
    remote = state.view.remote
    if remote is not None and all(
        getattr(merged, name) == getattr(remote, name) for name in MERGED_FIELDS
    ):
        return Resolution.RESTORE_SERVER, None
    return Resolution.MERGE, merged


def merge_from_selection(view: ConflictView, take_remote: set[str] | None = None) -> Task:
    """Build the merged task from a per-group selection.

    ``take_remote`` names the groups the user chose the server's value for;
    everything else keeps the local value.  *Take all local* is an empty set,
    which is why it is a ``merge`` and not its own resolution.
    """
    take_remote = take_remote or set()
    sides = {
        group.name: Side.REMOTE if group.name in take_remote else Side.LOCAL
        for group in DIFF_GROUPS
    }
    return MergeState(view, sides).apply()


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


def auto_resolve(task_id: str, conn: sqlite3.Connection) -> bool:
    """Settle a conflict that has no question in it.  Sync role only.

    A reorder is a full PUT, and a rebalance is one per renumbered sibling, so
    two clients tidying the same list collide on a field the merge window does
    not even show.  Opening a three-pane window with every row dimmed is not a
    question, it is a puzzle — so when nothing but :data:`AUTO_FIELDS` moved,
    the newer side wins and the conflict is resolved where it was detected.

    The row is still written and still resolved through the ordinary paths:
    ``restore_server`` when the server's version is the newer one (no PUT at
    all), a ``merge`` re-based on ``remote_etag`` when ours is.  That leaves the
    same audit trail in ``conflict_queue`` as a hand-merged one.

    Returns whether it resolved anything.  **Must not be called inside a
    :func:`~davpunk.core.cache.tx`** — it opens its own.
    """
    row = conn.execute(
        "SELECT id FROM conflict_queue WHERE task_id = ? AND resolved = 0", (task_id,)
    ).fetchone()
    if row is None:
        return False

    view = load_conflict(row["id"], conn)
    if view.mode is not Mode.BOTH_CHANGED or not only_auto_differs(view.local, view.remote):
        return False

    resolution, merged = resolution_for(MergeState(view))
    resolve_conflict(view.conflict_id, resolution, merged, conn)
    log.info(
        "Auto-merged %s: only ordering differed, %s version is newer (%s)",
        task_id,
        newer_side(view).value,
        resolution.value,
    )
    return True


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
        "rrule",
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
