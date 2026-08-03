"""The v8 sync cycle: discover, pull, push, expire.

Every write is **one transaction per item**, never one around a loop.  That is
what makes cancellation safe at item boundaries: a cancel between items can
never leave a half-applied state, and a PUT whose response was received is
always recorded before the next check.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from davpunk.conflict import resolver
from davpunk.core import cache
from davpunk.core.caldav_client import (
    CalDAVError,
    CollectionMissing,
    FetchedResource,
    NotFound,
    PreconditionFailed,
    ResourceMeta,
    Unauthorized,
    batch_hrefs,
)
from davpunk.core.ical_parser import (
    DEFAULT_MAX_RESOURCE_BYTES,
    ICalParseError,
    parse_resource,
    serialize_task,
)
from davpunk.models.remote import Remote
from davpunk.models.task import ChangeType, ReadOnlyReason, SyncState

log = logging.getLogger("davpunk.core.sync_engine")

ProgressFn = Callable[..., None]


@dataclass
class PullResult:
    applied: int = 0
    imported: int = 0
    deleted: int = 0
    conflicts: int = 0
    #: Conflicts settled without asking, counted apart from the ones that are
    #: waiting for the user.  Reporting them as ``conflicts`` would badge a
    #: queue that has nothing in it.
    auto_merged: int = 0
    skipped: int = 0
    cancelled: bool = False
    short_circuited: bool = False


@dataclass
class PushResult:
    pushed: int = 0
    conflicts: int = 0
    auto_merged: int = 0
    failed: int = 0
    cancelled: bool = False


# --------------------------------------------------------------------- helpers


def _noop(*_args: Any, **_kwargs: Any) -> None:
    pass


def _join(calendar_href: str, href: str) -> str:
    """A leaf href, resolved against its collection, for use on the wire."""
    if href.startswith("/") or "://" in href:
        return href
    return calendar_href.rstrip("/") + "/" + href.lstrip("/")


def _leaf(href: str) -> str:
    """``tasks.href`` is the **leaf** name, not the absolute path.

    The collection's own href already lives on the ``calendars`` row, so
    storing the full path would duplicate it — and would go stale the moment a
    collection is renamed.  Server responses carry absolute paths, so every
    href crossing the boundary is normalised here, in exactly one place.
    Getting this wrong is subtle: the pull compares server hrefs against
    ``local_rows`` keys, and a leaf/absolute mismatch makes every local row
    invisible, so every resource is re-imported as a duplicate.
    """
    return href.rstrip("/").rsplit("/", 1)[-1]


# ------------------------------------------------------------------ discovery


def discover_calendars(remote: Remote, client: Any, conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Upsert what the server offers; quarantine what has gone away."""
    found = client.discover()
    seen_ids: set[str] = set()

    with cache.tx(conn):
        for discovered in found:
            # ctag and sync_token are deliberately NOT stored here.  They are
            # the pull's short-circuit, and recording them at discovery would
            # make the very first pull short-circuit against a marker it had
            # just written itself — the collection would never be enumerated.
            # Only a completed pull_phase() records them.
            cal_id = cache.upsert_calendar(
                remote.id,
                discovered.href,
                conn,
                display_name=discovered.display_name,
                color=discovered.color,
                supports_sync=discovered.supports_sync,
            )
            seen_ids.add(cal_id)
            cache.mark_calendar_available(cal_id, conn)

        for row in cache.calendar_rows(conn, remote.id):
            if row["id"] not in seen_ids:
                # A 404'd collection is far more often a server hiccup than an
                # intentional removal, so rows are quarantined, never deleted.
                log.warning("Collection %s is no longer offered by %s", row["href"], remote.id)
                cache.mark_calendar_unavailable(row["id"], conn)

    return [row for row in cache.calendar_rows(conn, remote.id) if row["available"]]


# ----------------------------------------------------------------- pull phase


def pull_phase(
    calendar: sqlite3.Row,
    client: Any,
    conn: sqlite3.Connection,
    cancel: threading.Event | None = None,
    *,
    progress: ProgressFn | None = None,
    max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> PullResult:
    cancel = cancel or threading.Event()
    progress = progress or _noop
    result = PullResult()
    calendar_href = calendar["href"]
    calendar_id = calendar["id"]

    # 1. ctag short-circuit.
    try:
        ctag, sync_token = client.get_ctag(calendar_href)
    except CalDAVError as exc:
        log.warning("ctag probe failed for %s: %s", calendar_href, exc)
        ctag, sync_token = None, None

    if (
        ctag is not None
        and ctag == calendar["ctag"]
        and not cache.has_ready_pending(calendar_id, conn)
    ):
        result.short_circuited = True
        return result

    local_rows = cache.local_rows_for_calendar(calendar_id, conn)

    # 2. Enumerate.
    changed, removed, new_token = _enumerate(client, calendar, local_rows)
    # A collection that supports sync-collection but has no stored token yet is
    # bootstrapped from the token the probe just reported: it describes exactly
    # the state the full enumeration above returned.
    new_token = new_token or sync_token

    # 3. Removals.
    for href in removed:
        row = local_rows.get(href)
        if row is None:
            continue
        _handle_removal(row, calendar_id, conn, result)

    # 4. Changed / new, in batches.
    pending = _pending_by_task(calendar_id, conn)
    to_fetch = [
        meta.href for meta in changed if _needs_fetch(meta, local_rows.get(meta.href), pending)
    ]

    etag_by_href = {meta.href: meta.etag for meta in changed}
    done = 0
    for batch in batch_hrefs(to_fetch):
        if cancel.is_set():
            result.cancelled = True
            return result

        fetched, failed = _fetch_batch(client, calendar_href, batch)
        for href in failed:
            log.info("Deferring %s to the next cycle: no usable multiget result", href)
            result.skipped += 1

        for resource in fetched:
            _apply_one(
                resource,
                calendar_id,
                calendar_href,
                client,
                local_rows,
                conn,
                result,
                max_resource_bytes,
                etag_by_href.get(resource.href),
            )
        done += len(batch)
        progress("pull", done, len(to_fetch))

    # 5. Record the markers only if the phase completed uncancelled: a partial
    #    pull must not store a ctag that would short-circuit the next cycle.
    with cache.tx(conn):
        cache.set_calendar_sync_markers(
            calendar_id, conn, ctag=ctag, sync_token=new_token, last_sync=cache.now()
        )
    return result


def _local_snapshot(task_id: str, conn: sqlite3.Connection) -> str | None:
    """The local side of a conflict: the user's *edited* state, serialized.

    Not ``tasks.raw_ics`` — that column only advances on a successful push, so
    for a dirty task it still holds the pre-edit bytes.  Snapshotting it would
    show the user their own edit as "unchanged" in the conflict dialog and then
    silently discard it if they chose "take all local".

    (This is distinct from ``upsert_conflict`` deliberately not *refreshing*
    ``local_raw_ics`` on a later detection: once a conflict is open, the
    snapshot the user is looking at has to stay still.)
    """
    task = cache.get_task(task_id, conn)
    if task is None:
        return None
    if task.read_only_reason is not None:
        return task.raw_ics  # quarantined: never re-serialize
    try:
        return serialize_task(task)
    except ICalParseError as exc:
        log.debug("Falling back to stored raw_ics for the local snapshot: %s", exc)
        return task.raw_ics


def _pending_by_task(calendar_id: str, conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["task_id"]: row
        for row in conn.execute(
            "SELECT pc.* FROM pending_changes pc JOIN tasks t ON t.id = pc.task_id "
            "WHERE t.calendar_id = ?",
            (calendar_id,),
        )
    }


def _needs_fetch(meta: Any, row: Any, pending: dict[str, sqlite3.Row]) -> bool:
    """Should this href be fetched and applied this cycle?"""
    if row is None:
        return True  # server-new
    if meta.etag == row["etag"]:
        return False  # unchanged

    if row["sync_state"] == SyncState.NEW.value:
        # Our href collides with an existing server resource.  The create path's
        # If-None-Match handles that during push; fetching it here would only
        # produce a spurious conflict.
        return False

    return not _intent_is_based_on(row, pending.get(row["id"]), meta.etag)


def _intent_is_based_on(row: Any, pending: sqlite3.Row | None, server_etag: str | None) -> bool:
    """Is the queued local intent already a decision *about this very version*?

    Without this, resolving a conflict does not stick.  The row's ``etag``
    column still holds the pre-conflict value — the sync role only advances it
    on a clean apply — so the very next pull sees "server changed" again and
    re-raises the conflict the user just settled, forever.

    ``base_etag`` is the version the user's pending intent was formed against.
    When it equals what the server holds now, the push's ``If-Match`` is going
    to succeed and there is nothing left to ask about.
    """
    if pending is None:
        return False
    if pending["base_etag"] is not None:
        return pending["base_etag"] == server_etag

    # An unconditional delete (base_etag NULL) means "remove whatever is there"
    # — the user chose *delete anyway* with the server's change in front of
    # them.  Re-confirming it would be asking the same question twice.
    return (
        pending["change_type"] == ChangeType.DELETE.value
        and row["sync_state"] == SyncState.PENDING_DELETE.value
    )


def _enumerate(client: Any, calendar: sqlite3.Row, local_rows: dict[str, Any]):
    """``sync-collection`` where supported, else a full ``calendar-query``."""
    calendar_href = calendar["href"]

    if calendar["supports_sync"] and calendar["sync_token"]:
        result = client.sync_collection(calendar_href, calendar["sync_token"])
        if not result.invalid_token:
            return (
                [ResourceMeta(href=_leaf(m.href), etag=m.etag) for m in result.changed],
                [_leaf(h) for h in result.removed],
                result.sync_token,
            )

    metas = [
        ResourceMeta(href=_leaf(meta.href), etag=meta.etag)
        for meta in client.list_vtodo_etags(calendar_href)
    ]
    server_hrefs = {meta.href for meta in metas}
    removed = [
        href
        for href, row in local_rows.items()
        # A locally-created task is absent from the server by definition; it is
        # not a removal.
        if href not in server_hrefs and row["sync_state"] != SyncState.NEW.value
    ]
    return metas, removed, None


def _handle_removal(
    row: sqlite3.Row, calendar_id: str, conn: sqlite3.Connection, result: PullResult
) -> None:
    state = row["sync_state"]

    if state == SyncState.NEW.value:  # defensive; _enumerate already excludes these
        return

    with cache.tx(conn):
        if state in (SyncState.PENDING_DELETE.value, SyncState.CLEAN.value):
            cache.add_tombstone(calendar_id, row["href"], row["uid"], conn)
            conn.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))  # cascades
            result.deleted += 1
        elif state == SyncState.DIRTY.value:
            # Dialog mode B: the server deleted it, we have local changes.
            cache.upsert_conflict(row["id"], _local_snapshot(row["id"], conn), None, None, conn)
            cache.set_sync_state(row["id"], SyncState.CONFLICT, conn)
            result.conflicts += 1
        elif state == SyncState.CONFLICT.value:
            # Refresh the OPEN conflict.  The task row and the user's
            # unresolved decision are preserved.
            cache.upsert_conflict(row["id"], _local_snapshot(row["id"], conn), None, None, conn)


def _fetch_batch(client: Any, calendar_href: str, batch: list[str]):
    """Multiget, then a single GET for any href the 207 could not deliver.

    ``batch`` is leaf hrefs; the wire wants absolute paths and gives them back,
    so both directions are normalised here.
    """
    result = client.multiget(calendar_href, [_join(calendar_href, h) for h in batch])
    fetched = [
        FetchedResource(href=_leaf(r.href), etag=r.etag, ics=r.ics) for r in result.resources
    ]
    still_failed: list[str] = []

    for href in result.failed:
        try:
            one = client.get(_join(calendar_href, href))
            fetched.append(FetchedResource(href=_leaf(one.href), etag=one.etag, ics=one.ics))
        except CalDAVError as exc:
            log.debug("Per-href GET fallback failed for %s: %s", href, exc)
            still_failed.append(_leaf(href))
    return fetched, still_failed


def _apply_one(
    resource: Any,
    calendar_id: str,
    calendar_href: str,
    client: Any,
    local_rows: dict[str, Any],
    conn: sqlite3.Connection,
    result: PullResult,
    max_resource_bytes: int,
    fallback_etag: str | None,
) -> None:
    try:
        parsed = parse_resource(resource.ics, max_resource_bytes=max_resource_bytes)
    except ICalParseError as exc:
        # A VEVENT-only resource in a mixed collection, or one malformed
        # property from another client.  Skip this href, keep the cycle.
        log.info("Skipping %s: %s", resource.href, exc)
        result.skipped += 1
        return

    etag = resource.etag or fallback_etag
    row = local_rows.get(resource.href)

    if row is None:
        _import_new(resource, parsed, etag, calendar_id, calendar_href, client, conn, result)
        return

    if row["sync_state"] == SyncState.CLEAN.value:
        if cache.apply_server_version(row["id"], parsed, etag, conn):
            result.applied += 1
        return

    # dirty | conflict | pending_delete → a conflict, with the local side NULL
    # when the local intent was a delete (dialog mode A′).
    local_snapshot = (
        None
        if row["sync_state"] == SyncState.PENDING_DELETE.value
        else _local_snapshot(row["id"], conn)
    )
    with cache.tx(conn):
        cache.upsert_conflict(row["id"], local_snapshot, parsed.raw_ics, etag, conn)
        cache.set_sync_state(row["id"], SyncState.CONFLICT, conn)
    if resolver.auto_resolve(row["id"], conn):
        result.auto_merged += 1
        return
    result.conflicts += 1


def _import_new(
    resource: Any,
    parsed: Any,
    etag: str | None,
    calendar_id: str,
    calendar_href: str,
    client: Any,
    conn: sqlite3.Connection,
    result: PullResult,
) -> None:
    tomb = cache.find_tombstone(calendar_id, parsed.uid, conn)
    if tomb is not None:
        _handle_resurrection(
            tomb, resource, parsed, etag, calendar_id, calendar_href, client, conn, result
        )
        return

    parsed.calendar_id = calendar_id
    parsed.href = resource.href
    with cache.tx(conn):
        cache.insert_server_task(parsed, etag, conn)
    result.imported += 1


def _handle_resurrection(
    tomb: sqlite3.Row,
    resource: Any,
    parsed: Any,
    etag: str | None,
    calendar_id: str,
    calendar_href: str,
    client: Any,
    conn: sqlite3.Connection,
    result: PullResult,
) -> None:
    """Converge by re-deleting, then yield.

    Blocking alone does not converge: the resource stays on the server, invisible
    locally, until the tombstone expires — and then silently reappears.
    """
    if tomb["resurrect_count"] < cache.MAX_RESURRECTIONS:
        try:
            # Unconditional: this is our intent, not a guarded retry.
            client.delete(_join(calendar_href, resource.href), None)
        except CalDAVError as exc:
            log.warning("Could not re-delete resurrected %s: %s", resource.href, exc)
            return
        with cache.tx(conn):
            cache.bump_resurrect_count(tomb["id"], conn)
        log.info("Re-deleted resurrected task %s", parsed.uid[:8])
        result.deleted += 1
        return

    # Another client is deliberately keeping it alive.  Stop fighting.
    log.info(
        "Accepting resurrected task %s after %d attempts", parsed.uid[:8], tomb["resurrect_count"]
    )
    parsed.calendar_id = calendar_id
    parsed.href = resource.href
    with cache.tx(conn):
        cache.drop_tombstone(tomb["id"], conn)
        cache.insert_server_task(parsed, etag, conn)
    result.imported += 1
    _notify_restored(parsed)


def _notify_restored(parsed: Any) -> None:
    try:
        from davpunk.notifications.dbus_notify import notify

        notify(
            "Task restored by another client",
            f"A task you deleted was restored: {parsed.summary or parsed.uid}",
        )
    except Exception as exc:  # notifications are best-effort
        log.debug("Could not send the restoration notification: %s", exc)


# ----------------------------------------------------------------- push phase


def push_phase(
    calendar: sqlite3.Row,
    client: Any,
    conn: sqlite3.Connection,
    cancel: threading.Event | None = None,
    *,
    progress: ProgressFn | None = None,
) -> PushResult:
    cancel = cancel or threading.Event()
    progress = progress or _noop
    result = PushResult()
    calendar_href = calendar["href"]

    rows = cache.ready_pending_changes(calendar["id"], conn)
    for index, row in enumerate(rows):
        if cancel.is_set():
            result.cancelled = True
            return result

        change_type = row["change_type"]
        try:
            if change_type == ChangeType.CREATE.value:
                _push_create(row, client, calendar_href, conn, result)
            elif change_type == ChangeType.UPDATE.value:
                _push_update(row, client, calendar_href, conn, result)
            elif change_type == ChangeType.DELETE.value:
                _push_delete(row, client, calendar_href, conn, result)
            elif change_type == ChangeType.MOVE.value:
                _push_move(row, client, calendar_href, conn, result)
            else:
                log.error("Unknown change_type %r on %s", change_type, row["task_id"])
        except CollectionMissing as exc:
            # Not per-task retryable: surface it and stop touching this calendar.
            log.error("Collection %s is missing: %s", calendar_href, exc)
            with cache.tx(conn):
                cache.mark_calendar_unavailable(calendar["id"], conn)
            return result
        except CalDAVError as exc:
            _fail(row["task_id"], exc, conn, result)
        progress("push", index + 1, len(rows))

    return result


def _fail(task_id: str, exc: CalDAVError, conn: sqlite3.Connection, result: PushResult) -> None:
    with cache.tx(conn):
        cache.record_failure(task_id, str(exc), conn, status=exc.status)
    result.failed += 1


def _rehref(uid: str) -> str:
    return f"{uid}-{uuid.uuid4().hex[:8]}.ics"


def _push_create(
    row: sqlite3.Row, client: Any, calendar_href: str, conn: sqlite3.Connection, result: PushResult
) -> None:
    task_id = row["task_id"]
    task = cache.get_task(task_id, conn)
    ics = serialize_task(task)
    href = _join(calendar_href, task.href)

    try:
        write = client.put_create(href, ics)
    except PreconditionFailed:
        _resolve_create_collision(row, task, client, calendar_href, conn, result)
        return

    _record_created(task_id, write, href, calendar_href, client, conn, result)


def _resolve_create_collision(
    row: sqlite3.Row,
    task: Any,
    client: Any,
    calendar_href: str,
    conn: sqlite3.Connection,
    result: PushResult,
) -> None:
    """412 on a create: adopt if it is ours, re-href once if it is not."""
    href = _join(calendar_href, task.href)
    existing = client.get(href)

    try:
        existing_uid = parse_resource(existing.ics, max_resource_bytes=1 << 62).uid
    except ICalParseError:
        existing_uid = None

    if existing_uid == task.uid:
        # Our earlier PUT landed but the response was lost.  ADOPT it —
        # creating a second copy here is the classic duplicate-task bug.
        log.info("Adopting the existing resource at %s (same UID)", href)
        with cache.tx(conn):
            cache.set_task_identity(task.id, conn, etag=existing.etag, raw_ics=existing.ics)
            cache.set_sync_state(task.id, SyncState.CLEAN, conn)
            cache.clear_pending(task.id, conn)
        result.pushed += 1
        return

    # Someone else's resource is at our href.  Same UID, different href.
    new_href = _rehref(task.uid)
    with cache.tx(conn):
        cache.set_task_identity(task.id, conn, href=new_href)
    retry_href = _join(calendar_href, new_href)

    try:
        write = client.put_create(retry_href, serialize_task(task))
    except PreconditionFailed as exc:
        # Do not loop: back off and try again next cycle.
        _fail(task.id, exc, conn, result)
        return
    _record_created(task.id, write, retry_href, calendar_href, client, conn, result)


def _record_created(
    task_id: str,
    write: Any,
    href: str,
    calendar_href: str,
    client: Any,
    conn: sqlite3.Connection,
    result: PushResult,
) -> None:
    final_href = write.location or href
    etag = write.etag or _refresh_etag(client, calendar_href, final_href)

    with cache.tx(conn):
        cache.set_task_identity(task_id, conn, href=_leaf(final_href), etag=etag)
        if etag is None:
            cache.clear_etag(task_id, conn)
        cache.set_sync_state(task_id, SyncState.CLEAN, conn)
        cache.clear_pending(task_id, conn)
    result.pushed += 1


def _refresh_etag(client: Any, calendar_href: str, href: str) -> str | None:
    """A 2xx without an ETag is common; the RFC does not require one."""
    try:
        return client.get_etag(calendar_href, href)
    except CalDAVError as exc:
        # NULL != any server ETag, so the next cycle re-fetches this href once
        # and the clean-guard path applies.  No data is lost.
        log.debug("ETag refresh failed for %s: %s", href, exc)
        return None


def _push_update(
    row: sqlite3.Row, client: Any, calendar_href: str, conn: sqlite3.Connection, result: PushResult
) -> None:
    task_id = row["task_id"]
    base_etag = row["base_etag"]
    task = cache.get_task(task_id, conn)
    href = _join(calendar_href, task.href)

    if base_etag is None:
        # A NULL base_etag means this row should have been a create.  Repair it
        # rather than sending an unguarded PUT that would clobber the server.
        log.error("Update for %s has no base_etag; repairing to 'create'", task_id)
        with cache.tx(conn):
            conn.execute(
                "UPDATE pending_changes SET change_type = 'create' WHERE task_id = ?", (task_id,)
            )
            cache.set_sync_state(task_id, SyncState.NEW, conn)
        return

    task.sequence = (task.sequence or 0) + 1
    ics = serialize_task(task)

    try:
        write = client.put_update(href, ics, base_etag)
    except PreconditionFailed:
        remote = client.get(href)
        with cache.tx(conn):
            # `ics` is what we just tried to send: the user's edited state.
            cache.upsert_conflict(task_id, ics, remote.ics, remote.etag, conn)
            cache.set_sync_state(task_id, SyncState.CONFLICT, conn)
            # pending_changes is KEPT: the user's intent survives resolution.
        if resolver.auto_resolve(task_id, conn):
            # Ours newer → re-queued against remote.etag, so the next cycle's
            # If-Match succeeds.  Theirs newer → restored, nothing left to send.
            result.auto_merged += 1
            return
        result.conflicts += 1
        return
    except NotFound:
        with cache.tx(conn):
            cache.upsert_conflict(task_id, ics, None, None, conn)
            cache.set_sync_state(task_id, SyncState.CONFLICT, conn)
        result.conflicts += 1
        return

    etag = write.etag or _refresh_etag(client, calendar_href, href)
    with cache.tx(conn):
        cache.set_task_identity(task_id, conn, etag=etag, raw_ics=ics, sequence=task.sequence)
        if etag is None:
            cache.clear_etag(task_id, conn)
        cache.set_sync_state(task_id, SyncState.CLEAN, conn)
        cache.clear_pending(task_id, conn)
    result.pushed += 1


def _push_delete(
    row: sqlite3.Row, client: Any, calendar_href: str, conn: sqlite3.Connection, result: PushResult
) -> None:
    task_id = row["task_id"]
    href = _join(calendar_href, row["href"])

    try:
        client.delete(href, row["base_etag"])
    except PreconditionFailed:
        # Dialog mode A′: "Server has a newer version.  Delete anyway, or keep it?"
        remote = client.get(href)
        with cache.tx(conn):
            cache.upsert_conflict(task_id, None, remote.ics, remote.etag, conn)
            cache.set_sync_state(task_id, SyncState.CONFLICT, conn)
        result.conflicts += 1
        return

    # 2xx and 404 are both success: either we removed it or both sides agree it
    # is already gone.
    with cache.tx(conn):
        cache.finalize_delete(task_id, conn)
    result.pushed += 1


def _push_move(
    row: sqlite3.Row, client: Any, calendar_href: str, conn: sqlite3.Connection, result: PushResult
) -> None:
    """PUT to the target, then DELETE from the source.  The order is the design.

    A failure between the stages duplicates the task rather than losing it,
    which is the correct failure mode for user data — and the duplicate is
    self-healing through the tombstone ``move_task_local`` wrote.
    """
    task_id = row["task_id"]

    if row["move_stage"] == 0 and not _move_stage_put(row, client, calendar_href, conn, result):
        return
    # move_stage is persisted precisely so a crash here never re-PUTs.
    _move_stage_delete(task_id, row, client, conn, result)


def _move_stage_put(
    row: sqlite3.Row, client: Any, calendar_href: str, conn: sqlite3.Connection, result: PushResult
) -> bool:
    task_id = row["task_id"]
    # A move relocates bytes; it never re-serializes, which is why read-only
    # 'multipart' and 'oversize' tasks are movable at all.
    ics = row["raw_ics"]
    href = _join(calendar_href, row["href"])

    try:
        write = client.put_create(href, ics)
    except PreconditionFailed:
        existing = client.get(href)
        try:
            existing_uid = parse_resource(existing.ics, max_resource_bytes=1 << 62).uid
        except ICalParseError:
            existing_uid = None

        if existing_uid == row["uid"]:
            # Our PUT landed and the response was lost; adopt it.
            with cache.tx(conn):
                cache.set_task_identity(task_id, conn, etag=existing.etag)
                cache.set_move_stage(task_id, 1, conn)
            return True

        new_href = _rehref(row["uid"])
        with cache.tx(conn):
            cache.set_task_identity(task_id, conn, href=new_href)
        try:
            write = client.put_create(_join(calendar_href, new_href), ics)
        except CalDAVError as exc:
            _fail(task_id, exc, conn, result)
            return False

    with cache.tx(conn):
        cache.set_task_identity(task_id, conn, etag=write.etag)
        cache.set_move_stage(task_id, 1, conn)
    return True


def _move_stage_delete(
    task_id: str, row: sqlite3.Row, client: Any, conn: sqlite3.Connection, result: PushResult
) -> None:
    source_calendar = conn.execute(
        "SELECT href FROM calendars WHERE id = ?", (row["source_calendar_id"],)
    ).fetchone()
    if source_calendar is None:
        log.warning("Source calendar %s is gone; the move is complete", row["source_calendar_id"])
        with cache.tx(conn):
            cache.set_sync_state(task_id, SyncState.CLEAN, conn)
            cache.clear_pending(task_id, conn)
        result.pushed += 1
        return

    source_href = _join(source_calendar["href"], row["source_href"])
    try:
        client.delete(source_href, row["base_etag"])
    except PreconditionFailed:
        # The source changed after the move began.  The copy already exists at
        # the target, so the conflict is surfaced on the SOURCE → mode A′:
        # "The original changed after you moved it — delete it, or keep both?"
        remote = client.get(source_href)
        with cache.tx(conn):
            cache.upsert_conflict(task_id, None, remote.ics, remote.etag, conn)
            cache.set_sync_state(task_id, SyncState.CONFLICT, conn)
        result.conflicts += 1
        return
    except CalDAVError as exc:
        # Retry the DELETE only.  Never re-run stage 0.
        _fail(task_id, exc, conn, result)
        return

    with cache.tx(conn):
        cache.set_sync_state(task_id, SyncState.CLEAN, conn)
        cache.clear_pending(task_id, conn)
    result.pushed += 1


# ---------------------------------------------------------------- entrypoint


def run_cycle(
    remote: Remote,
    client: Any,
    conn: sqlite3.Connection,
    cancel: threading.Event | None = None,
    *,
    progress: ProgressFn | None = None,
    max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> tuple[PullResult, PushResult]:
    """Discover, then pull/push each available calendar, then expire.

    Used directly by tests; production goes through :class:`SyncRunner`, which
    adds the flock and the credential decrypt.
    """
    cancel = cancel or threading.Event()
    totals_pull, totals_push = PullResult(), PushResult()

    for calendar in discover_calendars(remote, client, conn):
        if cancel.is_set():
            totals_pull.cancelled = True
            break
        pull = pull_phase(
            calendar,
            client,
            conn,
            cancel,
            progress=progress,
            max_resource_bytes=max_resource_bytes,
        )
        _accumulate_pull(totals_pull, pull)

        if cancel.is_set():
            totals_push.cancelled = True
            break
        push = push_phase(calendar, client, conn, cancel, progress=progress)
        _accumulate_push(totals_push, push)

    cache.expire(conn)
    return totals_pull, totals_push


def _accumulate_pull(total: PullResult, one: PullResult) -> None:
    total.applied += one.applied
    total.imported += one.imported
    total.deleted += one.deleted
    total.conflicts += one.conflicts
    total.auto_merged += one.auto_merged
    total.skipped += one.skipped
    total.cancelled = total.cancelled or one.cancelled


def _accumulate_push(total: PushResult, one: PushResult) -> None:
    total.pushed += one.pushed
    total.conflicts += one.conflicts
    total.auto_merged += one.auto_merged
    total.failed += one.failed
    total.cancelled = total.cancelled or one.cancelled


__all__ = [
    "PullResult",
    "PushResult",
    "ReadOnlyReason",
    "Unauthorized",
    "discover_calendars",
    "pull_phase",
    "push_phase",
    "run_cycle",
]
