"""The 13 MCP tools, as plain functions.

MCP is an **editor-role** writer: every write goes through the same
``cache.py`` helpers as the Qt UI, never raw SQL.

The functions here are deliberately transport-free — they take a
:class:`ToolContext` and return JSON-shaped dicts — so the whole surface is
testable without standing up a FastMCP server.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable
from typing import Any

from davpunk.config import MCP_LIST_LIMIT_CAP, DavPunkConfig
from davpunk.core import cache
from davpunk.core.cache import (
    CacheError,
    ReadOnlyResourceError,
    TaskConflictError,
    TaskNotFound,
)
from davpunk.core.locking import is_syncing
from davpunk.mcp.refs import RefError, format_ref, ref_of, resolve
from davpunk.models.task import ORDER_STEP, Status, Task

log = logging.getLogger("davpunk.mcp.tools")

SEARCH_DEFAULT_LIMIT = 50

#: How many tasks one ``create_tasks`` call may write.  A batch is neither a
#: transaction nor cancellable, so the cap is what keeps a mistyped list from
#: becoming a sync queue nobody asked for — and the error names the number,
#: so an agent can split rather than guess.
CREATE_BATCH_LIMIT = 100


class Capability:
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    SYNC = "sync"


class ToolError(Exception):
    """A structured error an agent can act on rather than a traceback."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.extra}


class CapabilityDisabled(ToolError):
    def __init__(self, capability: str, tool: str) -> None:
        super().__init__(
            "capability_disabled",
            f"the {capability!r} capability is off; enable "
            f"[davpunk.mcp.capabilities] {capability} = true to use {tool}",
            capability=capability,
            tool=tool,
        )


class ToolContext:
    """Config plus a connection that belongs to the calling thread.

    The MCP SDK dispatches synchronous tools onto a worker thread pool, so a
    single shared connection would raise ``SQLite objects created in a thread
    can only be used in that same thread`` on the first call.  Passing
    ``db_path`` makes each thread open its own — the same rule the UI's sync
    worker follows.

    Tests pass ``conn`` directly: they are single-threaded, and an explicit
    connection keeps their fixtures in charge of the database.
    """

    def __init__(
        self,
        config: DavPunkConfig,
        conn: sqlite3.Connection | None = None,
        db_path: Any = None,
    ) -> None:
        if conn is None and db_path is None:
            raise ValueError("ToolContext needs either conn= or db_path=")
        self.config = config
        self._explicit = conn
        self._db_path = db_path
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._explicit is not None:
            return self._explicit
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = cache.open_db(self._db_path)
            self._local.conn = conn
            with self._lock:
                self._opened.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            for conn in self._opened:
                # A dying connection must not block the server from exiting.
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            self._opened.clear()

    def require(self, capability: str, tool: str) -> None:
        if not getattr(self.config.mcp.capabilities, capability):
            raise CapabilityDisabled(capability, tool)

    def audit(self, tool: str, task_ref: str | None, detail: str | None = None) -> None:
        if self.config.mcp.audit:
            cache.audit_mcp(tool, task_ref, detail, self.conn)

    @property
    def list_limit(self) -> int:
        return self.config.mcp.list_limit


def _page(limit: int | None, offset: int | None, default: int) -> tuple[int, int]:
    limit = default if limit is None else int(limit)
    return max(1, min(limit, MCP_LIST_LIMIT_CAP)), max(0, int(offset or 0))


def _envelope(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": offset + len(items) < total,
    }


def _projection(row: sqlite3.Row) -> dict[str, Any]:
    """The compact list shape.

    It saves more agent context than the pagination does: ``description`` and
    ``raw_ics`` are the bulk of a task and are almost never what a list is for.
    ``get_task`` returns them.
    """
    return {
        "task_ref": ref_of(row),
        "summary": row["summary"],
        "status": row["status"],
        "due": row["due_value"],
        "priority": row["priority"],
        "calendar": row["calendar_id"],
    }


def _full(row: sqlite3.Row, ctx: ToolContext) -> dict[str, Any]:
    task_id = row["id"]
    return {
        "task_ref": ref_of(row),
        "uid": row["uid"],
        "calendar": row["calendar_id"],
        "href": row["href"],
        "summary": row["summary"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "percent_complete": row["percent_complete"],
        "due": row["due_value"],
        "due_tzid": row["due_tzid"],
        "dtstart": row["dtstart_value"],
        "dtstart_tzid": row["dtstart_tzid"],
        "completed": row["completed"],
        "url": row["url"],
        "location": row["location"],
        "parent": (
            format_ref(row["calendar_id"], row["parent_uid"]) if row["parent_uid"] else None
        ),
        "categories": cache.load_categories(task_id, ctx.conn),
        "sync_state": row["sync_state"],
        "read_only_reason": row["read_only_reason"],
        "raw_ics": row["raw_ics"],
    }


# ------------------------------------------------------------------ read tools


def list_calendars(ctx: ToolContext) -> dict[str, Any]:
    """The ``calendar_id`` values every ``task_ref`` is built from."""
    ctx.require(Capability.READ, "list_calendars")
    rows = cache.calendar_rows(ctx.conn)
    return {
        "calendars": [
            {
                "calendar_id": row["id"],
                "name": row["display_name"],
                "remote": row["remote_id"],
                "remote_name": row["remote_name"],
                "color": row["color"],
                "available": bool(row["available"]),
            }
            for row in rows
        ]
    }


def list_tasks(
    ctx: ToolContext,
    *,
    calendar_id: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    priority: int | None = None,
    include_completed: bool = False,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    ctx.require(Capability.READ, "list_tasks")
    limit, offset = _page(limit, offset, ctx.list_limit)

    where = ["t.sync_state != 'pending_delete'"]
    params: list[Any] = []

    if calendar_id:
        where.append("t.calendar_id = ?")
        params.append(calendar_id)
    if status:
        where.append("t.status = ?")
        params.append(Status(status.upper()).value)
    elif not include_completed:
        where.append("(t.status IS NULL OR t.status NOT IN ('COMPLETED', 'CANCELLED'))")
    if priority is not None:
        where.append("t.priority = ?")
        params.append(int(priority))
    if tag:
        where.append(
            "EXISTS (SELECT 1 FROM task_categories c WHERE c.task_id = t.id AND c.category = ?)"
        )
        params.append(tag)

    clause = " AND ".join(where)
    total = int(
        ctx.conn.execute(f"SELECT COUNT(*) FROM tasks t WHERE {clause}", params).fetchone()[0]
    )
    rows = ctx.conn.execute(
        f"SELECT t.* FROM tasks t WHERE {clause} "
        "ORDER BY t.due IS NULL, t.due, t.davpunk_order IS NULL, t.davpunk_order, t.uid "
        "LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return _envelope([_projection(row) for row in rows], total, limit, offset)


def get_task(ctx: ToolContext, task_ref: str, *, href: str | None = None) -> dict[str, Any]:
    ctx.require(Capability.READ, "get_task")
    return _full(resolve(task_ref, ctx.conn, href=href), ctx)


def search_tasks(
    ctx: ToolContext,
    query: str,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """FTS5 over summary and description, completed tasks included."""
    ctx.require(Capability.READ, "search_tasks")
    limit, offset = _page(limit, offset, SEARCH_DEFAULT_LIMIT)

    try:
        total = int(
            ctx.conn.execute(
                "SELECT COUNT(*) FROM tasks_fts WHERE tasks_fts MATCH ?", (query,)
            ).fetchone()[0]
        )
        rows = cache.search_tasks(query, ctx.conn, limit=limit, offset=offset)
    except sqlite3.OperationalError as exc:
        # An agent can and will send FTS5 syntax the parser rejects.
        raise ToolError("invalid_query", f"{query!r} is not a valid FTS5 query: {exc}") from exc

    return _envelope([_projection(row) for row in rows], total, limit, offset)


# ----------------------------------------------------------------- write tools


def _require_calendar(ctx: ToolContext, calendar_id: str) -> None:
    if ctx.conn.execute("SELECT 1 FROM calendars WHERE id = ?", (calendar_id,)).fetchone() is None:
        raise ToolError(
            "calendar_not_found",
            f"no calendar {calendar_id!r}; call list_calendars for valid ids",
            calendar_id=calendar_id,
        )


def _parent_uid(ctx: ToolContext, calendar_id: str, parent_ref: str | None) -> str | None:
    """The uid a ``parent_ref`` names, refusing one in another calendar.

    ``RELATED-TO`` resolves within a single collection, so a cross-calendar
    parent is not a link that renders badly — it is one that never renders at
    all, and silently creating a root task where a subtask was asked for is
    worse than saying no.
    """
    if not parent_ref:
        return None
    parent = resolve(parent_ref, ctx.conn)
    if parent["calendar_id"] != calendar_id:
        raise ToolError(
            "parent_in_other_calendar",
            "a parent must live in the same calendar as its subtask",
            parent_ref=parent_ref,
        )
    return parent["uid"]


def create_task(
    ctx: ToolContext,
    calendar_id: str,
    summary: str,
    *,
    description: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    due: str | None = None,
    due_tzid: str | None = None,
    categories: list[str] | None = None,
    parent_ref: str | None = None,
) -> dict[str, Any]:
    ctx.require(Capability.WRITE, "create_task")
    _require_calendar(ctx, calendar_id)
    parent_uid = _parent_uid(ctx, calendar_id, parent_ref)

    task = Task(
        uid=uuid.uuid4().hex,
        calendar_id=calendar_id,
        summary=summary,
        description=description,
        status=Status(status.upper()) if status else Status.NEEDS_ACTION,
        priority=priority,
        due_value=due,
        due_tzid=due_tzid,
        parent_uid=parent_uid,
        categories=categories or [],
    )
    task_id = cache.create_task_local(task, ctx.conn)
    task_ref = format_ref(calendar_id, task.uid)
    ctx.audit("create_task", task_ref, summary)

    return {"task_ref": task_ref, "task": _projection_of(task_id, ctx.conn)}


def create_tasks(
    ctx: ToolContext,
    calendar_id: str,
    summaries: list[str],
    *,
    status: str | None = None,
    categories: list[str] | None = None,
    parent_ref: str | None = None,
) -> dict[str, Any]:
    """Create several tasks at once, optionally all under one parent."""
    ctx.require(Capability.WRITE, "create_tasks")
    _require_calendar(ctx, calendar_id)

    titles = [s.strip() for s in summaries if isinstance(s, str) and s.strip()]
    if not titles:
        raise ToolError("no_summaries", "create_tasks needs at least one non-empty summary")
    if len(titles) > CREATE_BATCH_LIMIT:
        raise ToolError(
            "batch_too_large",
            f"create_tasks writes at most {CREATE_BATCH_LIMIT} tasks per call; "
            f"{len(titles)} were given",
            limit=CREATE_BATCH_LIMIT,
            given=len(titles),
        )

    parent_uid = _parent_uid(ctx, calendar_id, parent_ref)
    # Ordered against the siblings that are already there, in one read: every
    # task in the batch is written before any of them could be read back, so
    # asking per task would hand out the same "end of the list" each time and
    # leave the order given here to chance.
    order = _next_order(ctx, calendar_id, parent_uid)

    created: list[dict[str, Any]] = []
    for title in titles:
        task = Task(
            uid=uuid.uuid4().hex,
            calendar_id=calendar_id,
            summary=title,
            status=Status(status.upper()) if status else Status.NEEDS_ACTION,
            parent_uid=parent_uid,
            categories=categories or [],
            davpunk_order=order,
        )
        # One create per task, each in its own transaction: cache.tx() does not
        # nest, so a batch is a sequence of writes rather than one atomic one.
        # A failure part-way is reported with what already exists named, which
        # is what makes the call safe to repeat for the rest.
        try:
            task_id = cache.create_task_local(task, ctx.conn)
        except CacheError as exc:
            raise ToolError(
                "batch_partially_created",
                f"{len(created)} of {len(titles)} tasks were created before this failed: {exc}",
                created=[entry["task_ref"] for entry in created],
                failed_summary=title,
            ) from exc
        order += ORDER_STEP
        task_ref = format_ref(calendar_id, task.uid)
        created.append({"task_ref": task_ref, "task": _projection_of(task_id, ctx.conn)})

    ctx.audit("create_tasks", parent_ref, f"{len(created)} task(s) in {calendar_id}")
    return {"created": created, "count": len(created)}


def _next_order(ctx: ToolContext, calendar_id: str, parent_uid: str | None) -> int:
    """One past the last ``X-DAVPUNK-ORDER`` in a sibling group.

    ``parent_uid IS ?`` rather than ``=``: root tasks have a NULL parent, and
    ``= NULL`` matches nothing, which would put every batch of root tasks back
    at the start of the list.
    """
    row = ctx.conn.execute(
        "SELECT MAX(davpunk_order) FROM tasks "
        "WHERE calendar_id = ? AND parent_uid IS ? AND sync_state != 'pending_delete'",
        (calendar_id, parent_uid),
    ).fetchone()
    return (row[0] + ORDER_STEP) if row and row[0] is not None else ORDER_STEP


def _update(ctx: ToolContext, tool: str, task_ref: str, fields: dict[str, Any], href: str | None):
    row = resolve(task_ref, ctx.conn, href=href)
    try:
        cache.update_task_optimistic(row["id"], fields, ctx.conn)
    except TaskConflictError as exc:
        # Naming the cause is what lets an agent leave it alone.
        raise ToolError(
            "task_conflicted",
            f"{task_ref} has an unresolved conflict; resolve it in the DavPunk UI first",
            task_ref=task_ref,
        ) from exc
    except ReadOnlyResourceError as exc:
        raise ToolError(
            "task_read_only",
            f"{task_ref} is read-only ({exc.reason}); it can be deleted or moved, not edited",
            task_ref=task_ref,
            reason=exc.reason,
        ) from exc
    except TaskNotFound as exc:
        raise ToolError("task_not_found", str(exc), task_ref=task_ref) from exc
    except CacheError as exc:
        raise ToolError("invalid_update", str(exc), task_ref=task_ref) from exc

    ctx.audit(tool, task_ref, json.dumps(_auditable(fields), sort_keys=True))
    return {"task_ref": task_ref, "task": _projection_of(row["id"], ctx.conn)}


def _projection_of(task_id: str, conn: sqlite3.Connection) -> dict[str, Any]:
    """The projection of a task this call has just written, so it is there."""
    row = cache.get_task_row(task_id, conn)
    assert row is not None
    return _projection(row)


def _auditable(fields: dict[str, Any]) -> dict[str, Any]:
    """Field names and small values only — never a whole description."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        out[key] = value if isinstance(value, (int, float, bool, type(None))) else "<set>"
    return out


def update_task(
    ctx: ToolContext,
    task_ref: str,
    *,
    href: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    percent_complete: int | None = None,
    due: str | None = None,
    due_tzid: str | None = None,
    location: str | None = None,
    url: str | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    ctx.require(Capability.WRITE, "update_task")

    fields: dict[str, Any] = {}
    for name, value in (
        ("summary", summary),
        ("description", description),
        ("priority", priority),
        ("percent_complete", percent_complete),
        ("due_value", due),
        ("due_tzid", due_tzid),
        ("location", location),
        ("url", url),
        ("categories", categories),
    ):
        if value is not None:
            fields[name] = value
    if status is not None:
        fields["status"] = Status(status.upper())

    if not fields:
        raise ToolError("empty_update", "update_task needs at least one field to change")
    return _update(ctx, "update_task", task_ref, fields, href)


def set_status(
    ctx: ToolContext, task_ref: str, status: str, *, href: str | None = None
) -> dict[str, Any]:
    """Canonicalization is automatic: COMPLETED forces PERCENT-COMPLETE."""
    ctx.require(Capability.WRITE, "set_status")
    try:
        value = Status(status.upper())
    except ValueError as exc:
        raise ToolError(
            "invalid_status",
            f"{status!r} is not a VTODO status",
            allowed=[s.value for s in Status],
        ) from exc
    return _update(ctx, "set_status", task_ref, {"status": value}, href)


def set_progress(
    ctx: ToolContext, task_ref: str, percent_complete: int, *, href: str | None = None
) -> dict[str, Any]:
    ctx.require(Capability.WRITE, "set_progress")
    if not 0 <= int(percent_complete) <= 100:
        raise ToolError("invalid_progress", "percent_complete must be between 0 and 100")
    return _update(ctx, "set_progress", task_ref, {"percent_complete": int(percent_complete)}, href)


def move_task(
    ctx: ToolContext,
    task_ref: str,
    target_calendar_id: str,
    *,
    move_subtree: bool = True,
    href: str | None = None,
) -> dict[str, Any]:
    """Permitted for ``multipart`` and ``oversize`` tasks: a move relocates the
    bytes verbatim and never re-serializes."""
    ctx.require(Capability.WRITE, "move_task")
    row = resolve(task_ref, ctx.conn, href=href)

    try:
        cache.move_task_local(row["id"], target_calendar_id, ctx.conn, move_subtree=move_subtree)
    except TaskConflictError as exc:
        raise ToolError(
            "task_conflicted",
            f"{task_ref} has an unresolved conflict; resolve it in the DavPunk UI first",
            task_ref=task_ref,
        ) from exc
    except ReadOnlyResourceError as exc:
        raise ToolError(
            "task_read_only", f"{task_ref} cannot be moved ({exc.reason})", reason=exc.reason
        ) from exc
    except CacheError as exc:
        raise ToolError("invalid_move", str(exc), task_ref=task_ref) from exc

    # calendar_id is part of the ref, so a move mints a new one.
    new_ref = format_ref(target_calendar_id, row["uid"])
    ctx.audit("move_task", new_ref, f"from {row['calendar_id']}")
    return {"task_ref": new_ref, "previous_task_ref": task_ref, "moved_subtree": move_subtree}


# ---------------------------------------------------------------- delete tool


def delete_task(ctx: ToolContext, task_ref: str, *, href: str | None = None) -> dict[str, Any]:
    ctx.require(Capability.DELETE, "delete_task")
    row = resolve(task_ref, ctx.conn, href=href)

    never_synced = row["etag"] is None and row["sync_state"] == "new"
    cache.delete_task_local(row["id"], ctx.conn)
    ctx.audit("delete_task", task_ref, row["summary"])

    return {
        "task_ref": task_ref,
        "purged_locally": never_synced,
        "queued": not never_synced,
    }


# ------------------------------------------------------------------ sync tools


def sync_now(
    ctx: ToolContext, *, remote_id: str | None = None, factory: Callable | None = None
) -> dict[str, Any]:
    ctx.require(Capability.SYNC, "sync_now")
    from davpunk.core.sync_runner import SyncRunner

    remotes = ctx.config.remotes
    if remote_id:
        remotes = [r for r in remotes if r.id == remote_id]
        if not remotes:
            raise ToolError("remote_not_found", f"no remote {remote_id!r}", remote_id=remote_id)

    cancel = threading.Event()
    results = []
    for remote in remotes:
        runner = (factory or SyncRunner)(remote.to_model(), ctx.conn)
        result = runner.run(cancel)
        results.append(
            {
                "remote": result.remote_id,
                "busy": result.skipped,  # SyncBusy: another holder has the lock
                "error": result.error,
                "pulled": result.pulled,
                "pushed": result.pushed,
                "conflicts": result.conflicts,
                "auto_merged": result.auto_merged,
            }
        )
    ctx.audit("sync_now", None, remote_id or "all")
    return {"results": results}


def sync_status(ctx: ToolContext) -> dict[str, Any]:
    ctx.require(Capability.SYNC, "sync_status")
    out = []
    for row in cache.sync_status_rows(ctx.conn):
        remote_id = row["remote_id"]
        out.append(
            {
                "remote": remote_id,
                "name": row["name"],
                "last_sync": row["last_sync"],
                "last_error": row["last_error"],
                "orphaned": bool(row["orphaned"]),
                "syncing": is_syncing(remote_id),  # a lock probe
                "blocked_changes": cache.blocked_count(ctx.conn, remote_id),
                "open_conflicts": int(
                    ctx.conn.execute(
                        "SELECT COUNT(*) FROM conflict_queue q JOIN tasks t ON t.id = q.task_id "
                        "JOIN calendars c ON c.id = t.calendar_id "
                        "WHERE c.remote_id = ? AND q.resolved = 0",
                        (remote_id,),
                    ).fetchone()[0]
                ),
            }
        )
    return {"remotes": out}


#: Tool name → (function, capability).  The server builds its registration from
#: this, and the tests assert the count and the gating from it too.
TOOLS: dict[str, tuple[Callable[..., Any], str]] = {
    "list_calendars": (list_calendars, Capability.READ),
    "list_tasks": (list_tasks, Capability.READ),
    "get_task": (get_task, Capability.READ),
    "search_tasks": (search_tasks, Capability.READ),
    "create_task": (create_task, Capability.WRITE),
    "create_tasks": (create_tasks, Capability.WRITE),
    "update_task": (update_task, Capability.WRITE),
    "set_status": (set_status, Capability.WRITE),
    "set_progress": (set_progress, Capability.WRITE),
    "move_task": (move_task, Capability.WRITE),
    "delete_task": (delete_task, Capability.DELETE),
    "sync_now": (sync_now, Capability.SYNC),
    "sync_status": (sync_status, Capability.SYNC),
}


def call(name: str, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    """Invoke a tool, turning every expected failure into a structured error."""
    fn, capability = TOOLS[name]
    try:
        # Checked here as well as inside each tool, because the in-function
        # check happens *after* Python has bound the arguments: a disabled tool
        # called with a missing parameter would otherwise raise a TypeError
        # instead of saying which capability to enable.
        ctx.require(capability, name)
        return fn(ctx, **kwargs)
    except (ToolError, RefError) as exc:
        return exc.as_dict()
    except TypeError as exc:
        raise ToolError("invalid_arguments", f"{name}: {exc}") from exc
