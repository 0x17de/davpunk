# DavPunk — High-Level Design

**Work it. Sync it. Check it. Done.**

Schema revision **8**. This document is the authority for behaviour; the
implementation follows it and the tests assert it.

---

## 1. Overview

DavPunk is a power-user CalDAV VTODO client for Linux (Wayland-primary, X11
compatible). It syncs task collections with CalDAV servers — primarily Radicale
— caches everything offline in SQLite, and optionally exposes an MCP server so
an AI agent can read and manipulate tasks. The UI targets information density
and keyboard-driven workflows.

### Goals

- **Offline-first.** Every edit lands in the local cache immediately and is
  pushed later. The network is never in the interaction path.
- **Never lose user data.** Every ambiguous outcome resolves toward duplication
  or a user prompt, never toward silent loss.
- **Byte-fidelity with other clients.** A resource DavPunk did not create and
  did not semantically change comes back byte-identical.
- **Keyboard-complete.** Every action is reachable without a mouse.
- **Agent-addressable.** Stable, opaque task references that survive a cache
  rebuild.

### Non-goals

See §16.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  EDITOR ROLE                                                     │
│  ┌────────────────────────────┐   ┌───────────────────────────┐  │
│  │  davpunk (PySide6 UI)      │   │  davpunk-mcp (FastMCP)    │  │
│  │  List │ Kanban │ Search    │   │  12 tools, all off by     │  │
│  │  TaskEditor │ ConflictDlg  │   │  default; task_ref addr.  │  │
│  │  FirstRun │ Keymap         │   │  paginated lists          │  │
│  └────────────────────────────┘   └───────────────────────────┘  │
│                                                                  │
│  Both write ONLY through cache.py helpers, each in an explicit   │
│  BEGIN IMMEDIATE transaction:                                    │
│    create_task_local() · update_task_optimistic()                │
│    delete_task_local() · move_task_local() · resolve_conflict()  │
│  Neither writes SQL directly.                                    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│           SQLite Cache (~/.local/share/davpunk/davpunk.db)       │
│  WAL · synchronous=NORMAL · busy_timeout=5 s · foreign_keys=ON   │
│  SQLite ≥ 3.35 with FTS5, asserted at startup                    │
└──────┬────────────────────────────────────────┬──────────────────┘
       │                                        │
┌──────▼─────────────────────────────┐   ┌──────▼───────────────────┐
│  SYNC ROLE — core/sync_runner.py   │   │  CalDAV remotes          │
│  Exactly one holder per remote,    │──▶│  (Radicale etc.)         │
│  enforced by flock().              │   └──────────────────────────┘
│  Instantiated by whichever of:     │
│    · davpunk-sync (systemd unit)   │
│    · UI worker QThread             │
│    · MCP sync_now                  │
│    · davpunk sync (CLI)            │
│  gets there first; others see      │
│  SyncBusy and skip.                │
│  Cancellable between items.        │
└──────┬─────────────────────────────┘
       │ D-Bus
┌──────▼───────────────────┐
│  org.freedesktop         │
│  .Notifications          │
└──────────────────────────┘
```

### Roles, not processes

The writer model is **role-based**. There is an *editor role* (the Qt UI and the
MCP write tools) and a *sync role* (whichever process currently holds a remote's
`flock`). Both roles may live in one process — the UI's sync worker thread is
the sync role while the UI's main thread is the editor role — and the invariants
are stated in terms of roles, never of processes.

### Table ownership

| Table | Editor role | Sync role |
|---|---|---|
| `remotes` | startup reconcile | — |
| `calendars` | read | full (discovery) |
| `tasks` | insert (new), update →dirty, →pending_delete, retarget (move) | insert (server-new), guarded update WHERE `sync_state='clean'`, delete, →conflict |
| `task_categories` | replace with parent edit | replace inside the guarded parent txn |
| `valarms` | replace with parent edit | replace inside the guarded parent txn |
| `pending_changes` | upsert, reset retry state | read, stage, backoff, delete on success |
| `tombstones` | move pre-guard | full ownership |
| `conflict_queue` | read, `resolved=1` | `upsert_conflict()` |
| `alarm_fires` | insert (UI scan) | insert (daemon scan) |
| `sync_status` | read | full ownership |

---

## 3. Components

```
src/davpunk/
├── __main__.py               CLI dispatch
├── cli/{commands,doctor}.py  sync · status · conflicts · doctor
├── config.py                 pydantic-settings over TOML
├── preflight.py              runtime floor assertions
├── logging_setup.py          per-entrypoint handlers; stdout unused
├── models/{task,calendar,remote}.py
├── core/
│   ├── caldav_client.py      discovery, REPORT, multiget batching
│   ├── ical_parser.py        VCALENDAR-level preservation
│   ├── sync_runner.py        lock → pull → push → expire; cancellable
│   ├── sync_engine.py        pull/push/expire phases
│   ├── locking.py            remote_sync_lock(), is_syncing(), ui_lock()
│   ├── cache.py              schema, migrations, tx(), ALL write helpers
│   ├── recovery.py           corrupt-DB triage
│   └── credentials.py
├── conflict/resolver.py
├── notifications/{dbus_notify,alarm_scan}.py
├── ui/…                      main_window, first_run, keymap, sync_worker,
│                             list_view, kanban_view, search_view,
│                             task_editor, conflict_dialog, move_dialog
├── daemon/sync_daemon.py
└── mcp/{server,refs,tools}.py
```

---

## 4. Runtime preflight

Executed by every entrypoint before touching the database:

- Python ≥ 3.11.
- `sqlite3.sqlite_version_info` ≥ (3, 35, 0) — required for partial-index
  UPSERT inference.
- FTS5 available in this Python's SQLite build.
- `zoneinfo` resolves a sample TZID (system tzdata or the `tzdata` wheel).

Each check is also reported individually by `davpunk doctor`.

---

## 5. Process model

### 5.1 Connections and transactions

```python
conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
```

`isolation_level=None` puts the connection in autocommit mode: the `sqlite3`
module issues **no** implicit `BEGIN`, so `with conn:` commits nothing and every
statement inside it has already committed on its own. Read-then-write helpers
are therefore not atomic without an explicit transaction.

```python
@contextmanager
def tx(conn):
    assert not conn.in_transaction, "tx() must not be nested"
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK"); raise
    else:
        conn.execute("COMMIT")
```

`BEGIN IMMEDIATE`, not the default `DEFERRED`: under WAL with several
processes, two deferred transactions that each read and then try to upgrade to a
write deadlock — neither can proceed and `busy_timeout` cannot break the tie.
`IMMEDIATE` takes the write lock at `BEGIN`, so writers queue instead.

**Rules.**

- Every write path uses `with tx(conn):`. `with conn:` never appears.
- Read-only queries stay in autocommit.
- Sync loops take **one transaction per item**, never one around the loop. That
  is what makes cancellation safe at item boundaries.
- A `BEGIN IMMEDIATE` that fails after `busy_timeout` raises: the sync role
  retries next cycle; the editor role surfaces "database busy".
- The UI must never hold a read transaction across an event-loop turn — a
  long-lived reader pins the WAL and blocks checkpointing.
- Each thread and each process opens its own connection. Connections are never
  shared across threads and `check_same_thread=False` is never used.

### 5.2 Sync exclusivity

```python
@contextmanager
def remote_sync_lock(remote_id):
    fd = os.open(lock_path(remote_id), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncBusy(remote_id)
        yield
    finally:
        os.close(fd)      # kernel releases on close AND on process death
```

"Is a sync running?" is a **lock probe**, not a database column — a DB flag
survives `SIGKILL` and sticks on forever:

```python
def is_syncing(remote_id) -> bool:
    fd = os.open(lock_path(remote_id), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
```

`flock` locks attach to the open file description, so the probe conflicts even
with a holder inside the same process. State dies with the process.

### 5.3 UI threading

`SyncRunner` runs on a dedicated `QThread` — long-lived, owning its own sqlite
connection. Worker → UI signals carry only `(remote_id, phase, done, total,
error)`; no Python objects and no connections cross the thread boundary. The UI
refreshes on `syncFinished` plus a 2 s poll combining `MAX(last_modified),
COUNT(*)` with `is_syncing()`.

### 5.4 Cancellation and shutdown

`SyncRunner.run(cancel: threading.Event)` checks between items and between
multiget batches — never mid-request. Every write is a per-item transaction, so
cancellation never leaves a half-applied state, and a PUT whose response was
received is always recorded before the next check.

- **Daemon:** SIGTERM/SIGINT set the event; the current item finishes, the lock
  releases, exit 0. `TimeoutStopSec=30`.
- **UI:** closing cancels and waits up to 5 s, then detaches — the flock
  releases at process exit regardless.

### 5.5 Single instance

The UI acquires a flock on `ui.lock`. A second launch raises and focuses the
existing window over D-Bus, then exits 0. `--new-instance` bypasses it.

---

## 6. Data model (schema rev 8)

The full DDL lives in `core/cache.py`. The design decisions:

### remotes

A cache of `[[davpunk.remotes]]`; the TOML config is authoritative. Reconciled
at startup by **every** process from its own config read — the upserts are
idempotent and `orphaned` is recomputed each time, so concurrent reconcilers are
benign. A remote in the DB but absent from config gets `orphaned = 1`: hidden,
not synced, **data retained**, with an explicit purge action offered.

### calendars

`id = sha256(remote_id || '\0' || href)[:16]` — **deterministic**.
Recovery-triggered rebuilds are a normal event and MCP `task_ref`s embed this
value; an arbitrary key minted at discovery would invalidate every reference an
agent holds. `ctag` and `sync_token` short-circuit the pull. A collection that
disappears is marked `available = 0`, never deleted.

### tasks

Canonical remote identity is `(calendar_id, href)`. `uid` is the iCalendar
identity: indexed, **not unique across calendars**, never changed by DavPunk.
`id` is a local UUID and is **never exposed over MCP**.

`sync_state` ∈ `new · clean · dirty · conflict · pending_delete` — see §7.

`read_only_reason` ∈ `NULL · multipart · oversize · calendar-unavailable` —
see §11.2.

**Date fidelity.** `*_value` holds the original RFC 5545 value verbatim and is
**authoritative for serialization**; `*_tzid` holds the original TZID param
(NULL = DATE, floating, or UTC). The `due` / `dtstart` INTEGER columns are
**coarse sort keys only** — never serialized, and never used for overdue
decisions (§11.3).

`priority` is NULL when undefined; never 0.

Indexes: `UNIQUE (calendar_id, href)`, `(uid)`, `(calendar_id, uid)` for
`task_ref` resolution, `(calendar_id, parent_uid)`, `(calendar_id, sync_state)`.

### task_categories, valarms

Child tables, `ON DELETE CASCADE` from `tasks`. VALARM support is display
action, relative trigger only, no `REPEAT`/`DURATION`. A relative trigger with
no anchor is invalid and dropped at parse.

### alarm_fires

Keyed on `(task_id, computed trigger_at)`, **not** on `valarms.id`: alarm rows
are dropped and reinserted on every server pull, so a flag stored there would
reset itself. Moving DUE yields a new `trigger_at`, so a rescheduled task
legitimately fires again.

### pending_changes

One row per task with unsent intent. `change_type` ∈ `create · update · delete ·
move`. `base_etag` is the ETag at the **first** edit after a clean sync and is
never overwritten by later edits; it is the `If-Match` value. Retry state:
`attempts`, `next_attempt_at` (wall clock, clamped on read), `last_error`,
`blocked`. Move-only columns: `source_calendar_id`, `source_href`, `move_stage`
(0 = target PUT still needed, 1 = target PUT confirmed, only the source DELETE
remains — persisted so a crash between the stages never re-PUTs).

### conflict_queue

`local_raw_ics IS NULL` → the local intent is DELETE (dialog mode A′).
`remote_raw_ics IS NULL` → the server deleted the resource (dialog mode B).

```sql
CREATE UNIQUE INDEX conflict_open ON conflict_queue (task_id) WHERE resolved = 0;
```

At most **one** open conflict per task, enforced by the database so a future
detection site cannot reintroduce an unbounded-insert bug. This is why SQLite
≥ 3.24 (in practice ≥ 3.35) is required: partial-index UPSERT inference.

### tombstones

Matched on `(calendar_id, uid)` — a stale client replaying a deleted task reuses
the UID but often lands on a different href, so href-matching misses the common
case. A user genuinely re-creating "the same" task mints a new UID. TTL 30 days.
Also written pre-emptively by `move_task_local()` to guard the source collection
during the PUT→DELETE window.

### sync_status

Remote-level errors only; per-task errors live on `pending_changes.last_error`.
Deliberately **no** `in_progress` column (§5.2).

### mcp_audit

`(ts, tool, task_ref, detail)` — every write/delete tool call when
`audit = true`.

### tasks_fts

External-content FTS5 over `(summary, description)`. The update trigger is
`AFTER UPDATE OF summary, description` — a bare `AFTER UPDATE` would fire on
every `sync_state` / `etag` / `last_modified` write, so every
`apply_server_version()` in every cycle would rewrite the index. External-content
FTS5 starts empty and its `'delete'` commands corrupt an unbuilt index, so the
creating migration ends with `INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')`.

### Migrations

`PRAGMA user_version`, one function per version, each applied inside `tx()`. A
`user_version` greater than the binary knows raises `SchemaTooNew` rather than
guessing. Nothing has shipped, so all of rev 8 is migration `v1`.

### Corruption recovery

`cache.open_or_recover()` wraps connect + migrate. On `sqlite3.DatabaseError`:

1. Attempt a read-only connection and dump whatever is legible —
   `pending_changes` joined to `tasks`, plus unresolved `conflict_queue` rows —
   to `~/.local/share/davpunk/recovery/pending-<ts>.json`.
2. `os.replace` the database to `davpunk.db.corrupt-<ts>`.
3. Create a fresh database at the current schema version.
4. UI: a blocking dialog naming the corrupt file, the recovery JSON, and the
   count of local edits recovered vs lost. Daemon and CLI: the same at ERROR.

Deterministic `calendar_id` means MCP `task_ref`s issued before a rebuild still
resolve afterwards. `PRAGMA integrity_check` is too slow for every startup and
lives in `davpunk doctor`. Clean shutdown runs `PRAGMA wal_checkpoint(TRUNCATE)`
and `PRAGMA optimize`.

---

## 7. Task state machine

```
       create_task_local()                move_task_local()
               │                    (already-synced task retargeted)
               ▼                                  │
            ┌─────┐  edit → stays new             │
            │ new │◄─┐  delete → purge locally    │
            └──┬──┘  │                            │
  PUT 201/204   │    │◄───────────────────────────┘
                ▼    │
   edit  ┌───────────────────┐│      delete_task_local()
  ┌─────►│      clean        ││   ┌──────────────────────┐
  │      └────┬──────────────┘│   │                      │
  │           │ edit          │   │                      ▼
  │           ▼               │   │            ┌──────────────────┐
  └──────┬─ ┌───────┐ ────────┘   │            │  pending_delete  │
         │  │ dirty │◄─────────────┘            └────────┬─────────┘
         │  └───┬───┘                                    │ 2xx / 404
         │      │ 412, 404, or server-changed            ▼
         │      ▼                                    tombstone
         │  ┌──────────┐  resolve_conflict()         + row deleted (cascades)
         └──│ conflict │──────────► dirty | new | pending_delete | clean | purged
            └──────────┘
                 ▲   │
                 │   └── delete_task_local() → implicit delete_anyway
                 └── edits and moves from the editor role are REJECTED here
```

**Invariants.**

- `new` never decays to `dirty`. An edit to a `new` task leaves both the state
  and `change_type='create'` alone.
- A move retargets an already-synced task to `new` — the target collection has
  no such resource, so `If-None-Match: *` is exactly right there.
- The push phase skips any task in `conflict`, and any task with a non-NULL
  `read_only_reason` except for `delete` and `move` intents.
- `resolve_conflict()` and `delete_task_local()` are the only exits from
  `conflict`.

---

## 8. Write helpers (`core/cache.py`)

The editor role — the Qt UI **and** the MCP write tools — uses these
exclusively. Every one runs inside `tx()` and applies `Task.canonicalized()`.

| Helper | Effect |
|---|---|
| `create_task_local(task, conn) -> id` | insert `sync_state='new'`, href `<uid>.ics`, queue `create` |
| `update_task_optimistic(task_id, fields, conn, *, rewrite_alarms=False)` | guards read-only and conflict; `new` stays `new`/`create`, otherwise `dirty`/`update`; `base_etag` set on INSERT only, never on the DO UPDATE branch; resets backoff |
| `delete_task_local(task_id, conn)` | never-synced → purge locally, no network, no tombstone; conflicted → implicit `delete_anyway` with an unconditional DELETE; otherwise `pending_delete` + queue `delete`. Permitted for read-only resources. Promotes children to root. |
| `move_task_local(task_id, target_calendar_id, conn, *, move_subtree=True)` | see §10 |
| `resolve_conflict(conflict_id, resolution, merged_task, conn)` | see §12 |
| `apply_server_version(task_id, parsed, etag, conn) -> bool` | **sync role.** Guarded `UPDATE … WHERE sync_state='clean'`; on `rowcount == 0` returns False and retries next cycle. The guard and the child-table rewrite are one transaction — splitting them is exactly the interleaving this invariant exists to prevent. |
| `upsert_conflict(...)` | `ON CONFLICT (task_id) WHERE resolved = 0 DO UPDATE` refreshing only the server side; `local_raw_ics` is deliberately **not** refreshed — it is the snapshot the user's pending edit is based on and must stay stable while the dialog is open. Called from within an enclosing `tx()`. |

---

## 9. Sync cycle (v8)

```
sync_cycle(remote, cancel):
  with remote_sync_lock(remote.id):                       # else SyncBusy
    cred = decrypt_credential(remote)                     # failure → rate-limited
                                                          #  warn, last_error, return
    discover_calendars(remote)
    for calendar in remote.calendars where available:
        if cancel.is_set(): break
        pull_phase(calendar, cancel)
        push_phase(calendar, cancel)
    expire_phase(remote)
    sync_status.last_sync = now(); last_error = NULL
```

### 9.1 Discovery

```
PROPFIND current-user-principal
PROPFIND calendar-home-set
PROPFIND Depth:1 → resourcetype, displayname, calendar-color, getctag,
                    supported-calendar-component-set, supported-report-set

keep only collections whose supported-calendar-component-set contains VTODO
calendar_id = sha256(remote_id || '\0' || href)[:16]
record supports_sync = ('sync-collection' in supported-report-set)

a previously known collection that is absent → available = 0, and its tasks get
read_only_reason = 'calendar-unavailable'
  (rows are NOT deleted — a 404'd collection is far more often a server hiccup
   than an intentional removal)
```

### 9.2 Pull phase

1. `PROPFIND getctag` (+ sync-token). If the ctag is unchanged **and** there are
   no ready `pending_changes` for this calendar: skip.
2. Enumerate — `REPORT sync-collection` (RFC 6578) when the collection supports
   it and we hold a token, else `REPORT calendar-query` with a VTODO comp-filter
   for `getetag`. In the calendar-query case `removed_hrefs` is
   `local_rows.keys() - server_etags.keys()`, **excluding** rows with
   `sync_state='new'`.
3. Removals, dispatched on the local `sync_state`:
   - `new` — skip (defensive).
   - `pending_delete` — tombstone, delete the row (cascades).
   - `dirty` — `upsert_conflict(local=raw_ics, remote=NULL)`, state `conflict`
     (dialog mode B).
   - `conflict` — refresh the **open** conflict; the task row and the user's
     unresolved decision are preserved.
   - `clean` — tombstone, delete the row.
4. Changed / new, fetched in **batches of 50** via `calendar-multiget`, never
   one GET per href. A 207 may carry per-href error statuses: those fall back to
   an individual GET, then to the next cycle. Rows whose local `sync_state` is
   `new` are dropped from the fetch list — our href collides with an existing
   server resource, and the create path's `If-None-Match` handles it during
   push. For each result, in its own `tx()`:
   - local row `clean` → `apply_server_version()`.
   - local row `dirty` / `conflict` / `pending_delete` → `upsert_conflict`,
     state `conflict` (`local_raw_ics` is NULL for `pending_delete`).
   - no local row → check tombstones on `(calendar_id, uid)` within 30 days;
     resurrection handling (§10.3) or a fresh insert as `clean`.
5. Record `ctag` / `sync_token` **only if the phase completed uncancelled** — a
   partial pull must not record a ctag that would short-circuit the next cycle.

### 9.3 Push phase

```sql
SELECT pc.*, t.*
  FROM pending_changes pc JOIN tasks t ON t.id = pc.task_id
 WHERE t.calendar_id = ?
   AND pc.blocked = 0
   AND (pc.next_attempt_at <= now()
        OR pc.next_attempt_at > now() + MAX_BACKOFF)   -- clock-skew clamp
   AND t.sync_state != 'conflict'                      -- no doomed attempts
   AND (t.read_only_reason IS NULL
        OR pc.change_type IN ('delete', 'move'))       -- verbatim ops only
 ORDER BY pc.queued_at
```

A move's target calendar is `tasks.calendar_id` (already retargeted), so the row
is picked up while scanning the **target** calendar; its DELETE stage addresses
`source_calendar_id` / `source_href` directly.

### 9.4 Expire phase

```sql
DELETE FROM tombstones     WHERE synced_at   < now() - 30d;
DELETE FROM alarm_fires    WHERE fired_at    < now() - 90d;
DELETE FROM conflict_queue WHERE resolved = 1 AND detected_at < now() - 90d;
```

---

## 10. Push semantics

### 10.1 Create

A bare PUT is an unconditional replace. The correct create is
`If-None-Match: *`, and the collision signal is **412**, not 409 (409 in WebDAV
means the parent collection is missing).

```
href = f"{uid}.ics"          ← UID never changes; only href varies on retry

PUT <calendar_href>/<href>   If-None-Match: *

→ 201 / 204 / 200:
    if a Location header is present: tasks.href = Location
    tasks.etag = response ETag  (absent → the etag-refresh path of §10.2)
    sync_state = 'clean';  DELETE pending_changes row

→ 412 Precondition Failed:
    GET the existing resource
    if its UID == our UID:
        # Our earlier PUT landed but the response was lost. ADOPT it —
        # creating a second copy here is the classic duplicate-task bug.
        store its etag and raw_ics; sync_state = 'clean'; DELETE pending row
    else:
        tasks.href = f"{uid}-{uuid4().hex[:8]}.ics"    # different href, same UID
        retry PUT once; if 412 again: backoff, do not loop

→ 409:  the parent collection is missing — calendar.available = 0; surface it;
        do NOT retry per-task.
→ 401 / 403 / 5xx / timeout:  backoff
```

### 10.2 Update

```
PUT <href>   If-Match: base_etag
    (base_etag is never NULL for an update; a NULL means the row should have
     been change_type='create' — assert, log, and repair to 'create')

→ 2xx with an ETag header:
    tasks.etag = header; SEQUENCE bumped; sync_state = 'clean'; drop pending row

→ 2xx WITHOUT an ETag header:            ← common; the RFC does not require one
    issue a single-href calendar-multiget for getetag
    on success: store it, sync_state = 'clean'
    on failure: tasks.etag = NULL, sync_state = 'clean'
                (the next cycle sees NULL != server_etag and re-fetches once;
                 the clean-guard path applies, so no data is lost)
    DELETE pending_changes row either way

→ 412:  GET the server ICS; upsert_conflict(local=raw_ics, remote=fetched);
        sync_state='conflict'; KEEP pending_changes
→ 404:  upsert_conflict(local=raw_ics, remote=NULL); 'conflict' (mode B)
→ 401:  re-decrypt the credential once, retry once, then backoff + surface
→ 403 / 5xx / timeout:  backoff
```

### 10.3 Delete

```
DELETE <href>  with If-Match: base_etag when non-NULL, unguarded otherwise

→ 2xx:  tombstone(calendar_id, href, uid); DELETE tasks row (cascades)
→ 404:  same as 2xx — both sides agree it is gone. SUCCESS, not a conflict.
→ 412:  re-fetch the server ICS; upsert_conflict(local=NULL, remote=fetched);
        'conflict' → dialog mode A′: "Server has a newer version.
        Delete anyway, or keep it?"
→ other 4xx / 5xx:  backoff
```

A never-synced task never reaches this path — `delete_task_local` purges it
locally. Read-only resources **do** reach it: DELETE removes the whole resource
and involves no serialization.

**Tombstones and resurrection.** A tombstone answers one question: *the server
is offering us a task we already deleted — is this our own stale view, or did
someone deliberately bring it back?* Blocking alone does not converge — the
resource stays on the server, invisible locally, until the tombstone expires and
silently reappears. So DavPunk converges by re-deleting up to three times, then
yields: it drops the tombstone, imports the task as `clean`, and notifies "A
task you deleted was restored by another client".

### 10.4 Move

CalDAV has no reliable cross-collection move: `MOVE` is optional and rarely
implemented, so a move is `PUT` to the target followed by `DELETE` from the
source. The ordering is the whole design.

```
stage 0 — PUT <target_href>   If-None-Match: *
          body = raw_ics UNCHANGED  (a move relocates bytes; it never
                                     re-serializes, which is why read-only
                                     'multipart' and 'oversize' tasks are movable)

    → 2xx:              tasks.etag = response ETag; move_stage = 1
                        fall through to stage 1 in the same cycle
    → 412, UID matches: our PUT landed and the response was lost — adopt the
                        existing resource's etag; move_stage = 1
    → 412, foreign UID: tasks.href = f"{uid}-{hex8}.ics"; retry once
    → other:            backoff.  NOTHING IS LOST — the source is intact and
                        the task is still visible there after a pull.

stage 1 — DELETE <source_href> in source_calendar_id,  If-Match: base_etag

    → 2xx / 404:  DELETE the pending_changes row; sync_state = 'clean'.
    → 412:        the source changed after the move began. The copy already
                  exists at the target, so surface the conflict on the SOURCE
                  → dialog mode A′: "The original changed after you moved it —
                  delete it, or keep both?"
    → other:      backoff; retry the DELETE ONLY. Never re-run stage 0.
```

**PUT before DELETE is deliberate.** A failure between the stages duplicates the
task rather than losing it, which is the correct failure mode for user data. The
duplicate is also self-healing: `move_task_local` writes a tombstone on
`(source_calendar_id, uid)` before the push, so a pull of the source collection
re-issues the DELETE through the convergence path above. `move_stage` is
persisted precisely so a crash between the stages never re-PUTs.

Subtasks move with their parent by default: parent resolution is per-calendar,
so leaving children behind would orphan them. Declining moves only the parent
and promotes the left-behind children to root in the source calendar. The UI
asks; MCP's `move_task` takes `move_subtree` (default `true`).

Cross-*remote* moves are out of scope.

---

## 11. Retry, fidelity and dates

### 11.1 Retry and backoff

- Push scan: `blocked = 0` and `next_attempt_at <= now()`.
- Retryable failure → `attempts += 1`,
  `next_attempt_at = now() + min(60 * 2**attempts, 3600)`, store `last_error`.
- `attempts >= 8` (≈ 2 h) → `blocked = 1`, surfaced in the UI sync bar as
  "N changes need attention", expandable to the per-change error.
- Non-retryable by class — **400, 403, 415** — block immediately.
- Any success, user edit, conflict resolution, or explicit *Retry now* resets
  `attempts / next_attempt_at / blocked / last_error`.
- **Clock skew:** `next_attempt_at` is wall-clock so it survives a restart, and
  is clamped on read — `next_attempt_at > now() + MAX_BACKOFF` is treated as
  ready. That self-heals a backward clock jump and a corrupt value. In-process
  timing uses `time.monotonic()`. TTLs stay wall-clock.
- HTTP: 10 s connect / 30 s read timeouts. A 401 gets one credential re-decrypt
  and one retry before counting as a failure.
- `sync_status.last_error` is **remote-level**; per-task errors live on
  `pending_changes.last_error`.

### 11.2 iCalendar fidelity

> DavPunk preserves the **entire VCALENDAR component tree**. Every
> calendar-level property (`PRODID`, `VERSION`, `CALSCALE`, `METHOD`, `X-WR-*`)
> and every sibling component (`VTIMEZONE`, additional `VTODO`s) is written back
> untouched. Only the owned properties inside the single target VTODO are
> mutated. DavPunk never rebuilds a resource from scratch.

Scoping this at the VTODO level would permit an implementation that rebuilds the
wrapper and drops the `VTIMEZONE` definitions, breaking every `TZID=` reference
for other clients.

Owned property set: `SUMMARY`, `DESCRIPTION`, `STATUS`, `PRIORITY`,
`PERCENT-COMPLETE`, `CATEGORIES`, `DTSTART`, `DUE`, `COMPLETED`,
`RELATED-TO;RELTYPE=PARENT`, `LAST-MODIFIED`, `SEQUENCE`, `X-DAVPUNK-*`.
`RRULE` is read-only pass-through. Everything else survives byte-identical.

- `PRODID` identifies the creating client: set it only on resources DavPunk
  creates; never rewrite it on an existing resource.
- When the user sets a DTSTART/DUE `TZID` with no matching `VTIMEZONE` in the
  VCALENDAR, generate one from `zoneinfo` and add it. Never prune
  apparently-unreferenced `VTIMEZONE`s.
- VALARM subcomponents pass through untouched unless the user edited alarms in
  this transaction (`rewrite_alarms=True`), in which case all VALARMs are
  regenerated from the `valarms` table.
- Output uses CRLF and 75-**octet** folding, verified in tests including a
  multi-byte UTF-8 DESCRIPTION that must fold without splitting a codepoint.
- **A move transmits `raw_ics` verbatim** — no parse, no re-serialize.

**Read-only quarantine.**

| `read_only_reason` | Trigger | Editable | Deletable | Movable |
|---|---|---|---|---|
| `multipart` | the VCALENDAR contains >1 VTODO | no | yes | **yes** |
| `oversize` | `raw_ics` exceeds `max_resource_bytes` (default 256 KiB) | no | yes | **yes** |
| `calendar-unavailable` | the collection vanished from the server | no | yes | no |

Delete and move are permitted where they are byte-verbatim operations; only
re-serialization is dangerous. `multipart` is what makes "RRULE deferred, opaque
round-trip" actually safe: a recurring series with `RECURRENCE-ID` overrides
lives in one resource as several VTODOs, and re-serializing the master would
destroy the overrides. On a multipart resource the master (the VTODO without a
`RECURRENCE-ID`) supplies the displayed fields; `raw_ics` is always the complete
VCALENDAR.

Enforcement: `update_task_optimistic` raises `ReadOnlyResourceError`; the push
phase permits only `delete` and `move` intents for these rows; MCP write tools
return a structured error naming the reason; the UI shows a badge.

### 11.3 Dates, time zones, and what "overdue" means

Overdue and grouping go through **one** function:

```python
def due_deadline(task, tz=None) -> datetime | None:
    """The instant after which the task is overdue."""
    tz = tz or local_timezone()
    if not task.due_value:
        return None
    if "T" not in task.due_value:                      # DATE → end of that day
        d = date.fromisoformat(task.due_value)
        return datetime.combine(d, time.max, tzinfo=tz)
    dt = parse_datetime(task.due_value)
    if task.due_value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc)
    if task.due_tzid:
        return dt.replace(tzinfo=ZoneInfo(task.due_tzid))
    return dt.replace(tzinfo=tz)                       # floating → local
```

A task due `20260731` is overdue after 23:59:59 local on the 31st — not from
00:00 UTC, which is what a naive comparison against the sort key would give.

- SQL filters needing day-accurate boundaries pass a precomputed local-midnight
  epoch range rather than comparing to `now()`.
- Today / Overdue / Upcoming grouping uses `due_deadline`. The UI arms a timer
  for the next local midnight and re-buckets when it fires, and again on a
  timezone change — otherwise an app left open overnight shows yesterday's
  buckets.
- Display renders in the local zone; when `due_tzid` differs from local, the
  original zone is shown alongside.
- The same end-of-day rule applies to a DATE-valued `DTSTART` for "not started
  yet" filtering.
- An unresolvable TZID falls back to UTC for the sort key only; the original
  TZID is stored and written back verbatim, and logs once per TZID.
- A relative VALARM trigger with no anchor is invalid: dropped at parse, and the
  alarm editor is disabled for that task.

---

## 12. Conflict resolution

| `local_raw_ics` | `remote_raw_ics` | Mode | Buttons |
|---|---|---|---|
| set | set | **A** — both sides changed | Take all local · Take all server · Merge & save |
| NULL | set | **A′** — local delete (or move) vs server change | Delete anyway · Keep server version |
| set | NULL | **B** — server deleted, local changed | Recreate on server · Accept deletion |

*Take all local* maps to `resolution='merge'` with every field taken from the
local column. *Take all server* maps to `restore_server`. Atomic fields (one
side or the other, no sub-field merging): `description`, to preserve
inline-checklist integrity.

`resolve_conflict(conflict_id, resolution, merged_task, conn)` — all five paths
inside one `tx()`:

- `delete_anyway` (A′) → `pending_delete` + unconditional DELETE queued.
- `restore_server` (A, A′) → write the server fields, `clean`, drop the pending
  row. **No PUT** — the server already holds this version; re-PUTing adds churn
  and risks a conflict loop if the server moved again since detection. A stale
  `remote_etag` is harmless; the next pull corrects it.
- `merge` (A) → write the merged, canonicalized task, `dirty`, queue an
  `update` whose `base_etag` is `remote_etag` — the server is at that version,
  so `If-Match` guards against another race before we send.
- `recreate` (B) → `new` + `etag=NULL`, queue a `create`; the href is kept and
  `If-None-Match: *` will succeed since it is gone.
- `accept_deletion` (B) → tombstone + delete the row (the conflict row cascades
  with it).

`delete_task_local()` on a conflicted task is an implicit `delete_anyway`.
Queueing uses `INSERT OR REPLACE`, which atomically replaces any prior intent
and resets the backoff — a user-driven resolution is an explicit "try again
now". Resolved rows are pruned after 90 days.

**Post-merge canonicalization** lives on the model, not the resolver, and runs on
**every** write path — UI edit, MCP `set_status`, inbound server parse, conflict
resolution:

```python
if status == "COMPLETED":
    completed = completed or utcnow(); percent_complete = 100
elif status in ("NEEDS-ACTION", "IN-PROCESS"):
    completed = None            # percent_complete: keep the user's value
elif status == "CANCELLED":
    completed = None            # percent_complete: PRESERVED
```

---

## 13. Alarms

```
for each valarm whose anchor (DUE for related='END', DTSTART for 'START') is set:
    trigger_at = anchor + trigger_offset
    if trigger_at > now():                       continue   # not due yet
    if (task_id, trigger_at) in alarm_fires:     continue   # already fired
    if task.status in (COMPLETED, CANCELLED):    record as fired, do NOT notify
    if trigger_at < now() - 86400:               record as fired, do NOT notify
        # suppresses the flood on first start after a long downtime
    else:
        send the D-Bus notification
        INSERT INTO alarm_fires VALUES (task_id, trigger_at, now())
```

The ledger key is the *computed* trigger time, not `valarms.id`. Both the daemon
and the UI may scan concurrently; the primary-key insert is the race guard
(catch `IntegrityError`, skip).

**Alarms fire only while the UI or the daemon is running.** DavPunk installs no
timer units per alarm; there is no wake-from-suspend delivery.

---

## 14. Credentials, logging, CLI, keymap

### 14.1 Credentials

- `~/.config/davpunk/credentials/<remote-id>.gpg`, mode **0600 at creation**;
  the directory is **0700**. Modes are verified at startup and by
  `davpunk doctor --fix`.
- The setup wizard prompts with a Qt no-echo dialog and pipes the password
  directly to `gpg` **stdin**. It writes to a temp file in the same directory
  created `O_EXCL` 0600, then `os.replace`s it — an interrupted run never leaves
  a truncated credential. `--yes` is required or `--batch` turns the overwrite
  prompt into an error; `--trust-model always` is required for a recipient key
  that is not fully trusted.
- Runtime decrypt returns `result.stdout.decode("utf-8")` with **no stripping**.
  An earlier `.rstrip("\n")` removed *all* trailing newlines, corrupting any
  password ending in whitespace. The wizard adds no newline, so there is nothing
  to strip. Hand-encrypted credentials made with `echo` carry a trailing
  newline — documented, with `printf %s` as the manual alternative.
- `gpg_key_id` names an **encryption subkey**, pinned with a trailing `!`.
  Given a primary key id GnuPG selects a subkey by its own rules, and a keyring
  with more than one encryption subkey — after a rotation, or with one per
  device — can resolve to the subkey whose private half is on another machine.
  Encryption then succeeds and decryption never does, which is silent and
  total: the credential file looks fine and every request 401s. The picker
  therefore offers only subkeys whose secret material is reachable here (colon
  field 15 is not `#`), already pinned, and names the ones it skipped.
  `davpunk doctor` verifies the choice by encrypting a throwaway byte and
  reading the recipient key id back out of the packet, rather than
  reimplementing GnuPG's selection rules.

> The plaintext credential is never written to disk, never appears in `argv`,
> never enters shell history, and is never logged or stored in SQLite. It is
> held in process memory for the duration of one request. Python cannot
> guarantee prompt zeroing of string memory; treat process memory as in scope
> for an attacker who already has code execution as this user.

**Transport.** `http://` is rejected at config validation unless the remote sets
`allow_insecure = true`, which logs a prominent warning. `verify_tls = true` is
the default, disableable per-remote for self-signed Radicale setups.

**Daemon and gpg-agent.** The daemon inherits the standard GnuPG environment and
never forces a pinentry; it is functional only while gpg-agent holds the
passphrase. `default-cache-ttl` / `max-cache-ttl` are recommended in the unit
file's comments and checked by `davpunk doctor`. On decrypt failure the remote
is skipped with a **rate-limited** warning (once per hour) and
`sync_status.last_error` is set so the UI can show "credentials locked".
`loginctl enable-linger` is mentioned for daemon-without-session use, with the
caveat that it makes the agent-cache problem worse.

### 14.2 Logging

- Library modules use `logging.getLogger("davpunk.<module>")` and configure no
  handlers; `logging_setup.py` installs them per entrypoint.
- **Daemon** → stderr → journald. **UI** →
  `~/.local/state/davpunk/davpunk.log`, rotating 5 × 1 MB. **MCP** → **stderr
  only, never stdout** — on the stdio transport stdout is the protocol channel,
  and a stray write corrupts it.
- Never logged at any level: credential bytes, `Authorization` headers.
- Task summaries and `raw_ics` appear at DEBUG only; INFO refers to tasks by a
  UID prefix.
- `--log-level` flag and `DAVPUNK_LOG_LEVEL`.

### 14.3 CLI

| Command | Behavior |
|---|---|
| `davpunk` | launch the UI (default; single-instance) |
| `davpunk sync [--remote ID]` | one cycle, print a summary, exit non-zero on error |
| `davpunk status` | per remote: last sync, pending / blocked / conflict counts |
| `davpunk conflicts` | list open conflicts (read-only; resolution is a UI action) |
| `davpunk doctor [--fix]` | preflight checks |

`doctor` checks: Python ≥ 3.11; SQLite ≥ 3.35 with FTS5; `PRAGMA
integrity_check` and FTS `integrity-check` (offering `'rebuild'` on failure);
schema version vs binary; file modes on config (0600), credentials dir (0700),
each `.gpg` (0600), MCP token (0600); `gpg` present; each remote's `gpg_key_id`
in the keyring; a test decrypt per remote (detects a cold gpg-agent); `zoneinfo`
resolves a sample TZID; the D-Bus notification service is reachable; each
remote's URL resolves with a valid certificate; systemd unit installed and
active. Exit non-zero on any FAIL. `--fix` repairs file modes only.

### 14.4 Settings

The first-run wizard and **Edit → Preferences** share one `RemoteForm`, so the
two cannot drift apart. Every field carries an explanation on a `?` badge
beside its label — a CalDAV URL, a GPG key id and a sync interval are not
self-explanatory, but printing seven explanations inline cost two or three
wrapped lines each, which overflowed the wizard page and clipped all of them.
The same text is set as the field's tooltip and `whatsThis`, so hovering the
input works and Shift+F1 reaches it from the keyboard. Inline text is reserved
for what the user must *act* on: a validation problem, or a GPG subkey that was
skipped.

Preferences is reachable from the menu bar at all times. Config is read once at
startup, so the dialog writes `config.toml` through **tomlkit** —
preserving comments, key bindings and anything else it does not itself edit —
and states that changes apply on the next start. It reloads the file after
writing and refuses to close silently if the result would not parse, because a
config that fails at the *next* launch has no dialog left to explain itself.

### 14.5 Keymap

```
Global     j / k          down / up
           gg / G         top / bottom
           / , Esc        search / clear
           1 2 3          list / kanban / search
           Ctrl+R         sync now
           ?              keybinding overlay
Task       n              new task
           Enter          open editor
           Space          toggle complete
           dd             delete
           m              move to another list
           e              inline rename
           Tab / S-Tab    indent / outdent (reparent)
           Alt+j / Alt+k  reorder within siblings
           p / t / s      priority / tags / due date
Kanban     h / l          move card to previous / next column
           H / L          focus previous / next column
Conflict   l / s          take local / server for the focused field
           a / A          take all local / all server
           Enter          save resolution
```

Overrides live in `[davpunk.keys]`, validated at load: duplicate bindings are a
config error, never silently last-wins. **Hard requirement:** every action is
reachable without a mouse.

### 14.6 Subtasks, ordering, kanban

**Parent resolution.** `parent_uid` is resolved **within the same `calendar_id`
only** — `uid` is deliberately non-unique across calendars. A `RELATED-TO`
pointing outside the calendar renders as a root task with a "linked parent
elsewhere" marker and is preserved in the ICS. Only the first
`RELATED-TO;RELTYPE=PARENT` is honored.

**Orphan policy.** Deleting a parent promotes its children to root: `parent_uid`
cleared, each marked `dirty` so the `RELATED-TO` removal reaches the server.
Children are never cascade-deleted.

**Cycle guard.** The tree builder and recursive search walk with a `visited` set
and a depth cap of 32. On a cycle the link is broken at the repeat, the task
renders at root, and one log line is emitted.

**Ordering.** `X-DAVPUNK-ORDER` values are multiples of 1000; insertion takes the
midpoint. When a gap closes below 2, only the **contiguous run whose gaps
actually closed** is renumbered, and unchanged siblings are not marked dirty.
Drags within a 2 s window (`time.monotonic()`) coalesce into one rebalance, with
a single "reordering N tasks" toast. Sort key is
`(davpunk_order ASC NULLS LAST, uid ASC)`.

**Kanban columns.** (1) If `X-DAVPUNK-KANBAN-COL` is set **and** matches a
configured column `id`, that column wins. (2) Otherwise, the first configured
column whose `status` equals the task's STATUS. (3) Otherwise, the first column.
Dragging sets **both** `STATUS` (from the target column) and
`X-DAVPUNK-KANBAN-COL` (the column `id`) in one `update_task_optimistic` call.
Column `id`s must be unique; several columns *may* share a `status` — that is
what the override exists for. An orphaned override falls through to rule 2 and
is rewritten on the next drag; it is preserved in the ICS meanwhile.

**Hierarchy in the views.** Both the list and the kanban board render tasks as
trees. Every view shows a *slice* of the task list — one bucket, one column,
one filter — so the tree builder is given the whole set alongside the slice: a
child whose parent landed in a different slice is a root here, not a task whose
parent is missing, and only the latter earns the "linked parent elsewhere"
marker. In kanban that follows from the model rather than being a special case:
a column is a status, nesting is `RELATED-TO`, and the two are orthogonal.

**Folding.** Fold state is remembered per `(calendar_id, uid)` across refreshes
— a rebuild happens every poll, and an unremembered fold re-opens itself. Two
sets, not one: "never seen" must stay distinguishable from "the user closed
it". A *heading* is open unless it was closed; a *subtree* is closed unless it
was opened, because one parent can carry hundreds of children and expanding by
default buries the rest of the view. A folded parent shows its total descendant
count, so a fold never hides the fact that there is something to open.
`expand_all` / `collapse_all` set every currently visible node at once.

Expansion is applied in a **second pass over the finished tree**:
`QTreeWidgetItem.setExpanded` before the item is inserted into its widget is
silently dropped.

**Reparenting.** `indent` makes a task a child of the sibling above it,
`outdent` promotes it to sit beside its parent, and a kanban drop onto a card
does both nesting and the column move in one `update_task_optimistic` call.
A move onto the task itself, onto one of its own descendants, or across
calendars is refused before anything is written — a cycle the tree walker would
then have to break, or a `RELATED-TO` that could never resolve. The task
lands at the **end** of its new siblings: the order value it carries belongs to
the group it left.

**Board filter.** The board carries a filter bar over three axes, all optional
and combined with AND: **lists** (multi-select), **tags** (multi-select, `any`
or `all`), and a free-text substring over summary and description. Selection is
runtime state, not a stored preference — like `show_completed`, it is a way of
looking at the board right now, not a setting.

The two multi-selects normalise differently, because the data does. Every task
belongs to exactly one calendar, so ticking *every* list is the same as ticking
none and is reported as "all"; a task may carry *no* tags, so ticking every tag
still means "has at least one tag" and stays an active filter. Only tags
actually in use are offered, so the picker cannot offer a dead one. The filter
applies before columns are assigned, and each column header shows the count it
is currently displaying.

### 14.7 Automatic sync

Only the daemon syncs on a timer. The UI's 2 s poll reads the local cache and
probes the flock; it never initiates a cycle. Syncing from the UI is always an
explicit act.

`auto_sync` (per remote, default `true`) removes an account from the daemon's
timer and nothing else. Off does not mean "never sync": `davpunk sync`, *Sync
now* and MCP `sync_now` are all things the user asked for and keep working. An
excluded remote is still reconciled into the cache — its rows, credentials and
tasks are unaffected — and `davpunk status` labels it `[manual only]`, because
otherwise a long-stale `last_sync` is indistinguishable from a fault. With no
remote auto-syncing the daemon still runs, since the alarm scan is independent
of any remote.

The interval field is disabled in the UI when `auto_sync` is off: a live
interval beside a disabled toggle reads as "something is still syncing".

### 14.8 Dry run

`sync --dry-run` and **File → Preview sync…** report what a cycle would do and
write nothing. The guarantee is enforced at the two boundaries a sync can write
through, never by a flag threaded through the engine:

- **The server.** `ReadOnlyClient` wraps the real client. Reads pass through
  untouched, so authentication, discovery and fetch failures are genuine.
  `put_create`, `put_update` and `delete` record the intended request and
  return a synthetic 201/204 with an ETag, so the engine follows its normal
  bookkeeping path rather than an error path a real sync would not take.
- **The cache.** The cycle runs against a throwaway copy made with `VACUUM
  INTO` — not a file copy, because the cache is in WAL mode and the committed
  tail is often still in the sidecar. The real file is never opened for
  writing; the CLI does not even call `_open`, whose `reconcile_remotes` is
  itself a write.

Consequently the dry run executes the *same* code path as a real sync —
`SyncRunner`, the same flock, the same `sync_engine`. A separate planner would
be a second implementation of the decision rules, free to drift from the one
that runs.

The local half of the report is a diff of the copy against the original over
the content columns only; `etag` and `sync_state` move on almost every row of a
real pull without the task differing, and listing them would bury the changes
that matter. A collection skipped by the ctag short-circuit is reported as such
: "nothing to do" and "I did not look" must not render identically, so
`SyncResult` carries `up_to_date`.

What a dry run cannot know is how the server would answer a write it never
sent — a 412 collision, a quota rejection, an ETag the server rewrites.

---

## 15. MCP exposure

MCP is an **editor-role** writer: its write tools go through the same `cache.py`
helpers as the UI, never raw SQL.

### Addressing

- **`task_ref = "<calendar_id>:<uid>"`** — one opaque string, returned by every
  tool and accepted by every tool. Stable across syncs, restarts, and cache
  rebuilds, because `calendar_id` is `sha256(remote_id | href)[:16]` rather than
  a key minted at discovery.
- **`tasks.id` never appears in any MCP payload.** It is a local UUID that
  changes if a task is deleted and re-imported, and means nothing outside this
  database.
- If a ref resolves to more than one row — `uid` is not unique within a
  calendar, and a misbehaving server can duplicate one — tools return a
  structured error listing the candidate hrefs. An optional `href` parameter
  disambiguates.
- `list_calendars` returns the `calendar_id` values that refs are built from.

### Tools

| Tool | Capability | Description |
|---|---|---|
| `list_calendars` | read | Calendar collections across remotes, with `calendar_id` |
| `list_tasks` | read | Filter by calendar, status, tag, priority; paginated |
| `get_task` | read | Full detail for one `task_ref` |
| `search_tasks` | read | FTS5 full-text + field search (incl. completed); paginated |
| `create_task` | write | `create_task_local()`; returns the new `task_ref` |
| `update_task` | write | `update_task_optimistic()`; errors on conflicted or read-only tasks |
| `set_status` | write | STATUS shortcut; canonicalization is automatic |
| `set_progress` | write | PERCENT-COMPLETE shortcut |
| `move_task` | write | `move_task_local()`; target `calendar_id` + `move_subtree` (default true) |
| `delete_task` | delete | `delete_task_local()`; purges locally if never synced |
| `sync_now` | sync | Runs `SyncRunner`; returns `SyncBusy` if another holder has the lock |
| `sync_status` | sync | Last sync time, remote errors, blocked-change counts per remote |

All four capabilities — read, write, delete, sync — are **off by default**.

**Pagination and projection.** `list_tasks` takes `limit` (default 100) and
`offset`; `search_tasks` takes `limit` (default 50). Hard cap 500.

```json
{"items": [...], "total": 5000, "limit": 100, "offset": 0, "truncated": true}
```

List tools return a compact projection — `task_ref, summary, status, due,
priority, calendar` — while `get_task` returns full detail including
`description` and `raw_ics`. The projection saves more agent context than the
pagination does.

**Transport hardening.** SSE binds `127.0.0.1` — a non-loopback `bind` is
rejected at config load — and requires a bearer token, generated on first use.
stdio is recommended and needs no token at all.

The token is a 0600 file by default. Setting `token_gpg_key_id` stores it
GnuPG-encrypted instead, in `mcp-token.gpg`, decrypted at startup; the
trade-off is the same as for a CalDAV credential, so a cold `gpg-agent` is
reported as a token that cannot be *read* rather than as a token that is wrong.
`--rotate-token` replaces it and clears any leftover plaintext file, so
switching from a plain to an encrypted token cannot leave the readable one
behind.

With `audit = true`, every write/delete call is appended to `mcp_audit` keyed by
`task_ref`.

---

## 16. Config reference

Read **once at startup**; changes require a restart. `davpunk doctor` validates
the file without one. The first-run wizard is the sole exception.

```toml
[davpunk]
theme               = "dark"
default_view        = "list"        # list | kanban
show_completed      = false         # startup default; runtime toggle non-persistent
max_resource_bytes  = 262144        # oversize quarantine threshold

[[davpunk.remotes]]
id             = "work"
name           = "Work"
url            = "https://cal.example.com/dav/"
username       = "user@example.com"
gpg_file       = "~/.config/davpunk/credentials/work.gpg"
gpg_key_id     = "0x1A2B3C4D5E6F0003"     # validated against the keyring
sync_interval  = 300
color          = "#4A9EFF"
pinned_view    = false
allow_insecure = false                    # required to permit http://
verify_tls     = true

[davpunk.kanban]
columns = [
  {id = "todo",       label = "To Do",       status = "NEEDS-ACTION"},
  {id = "inprogress", label = "In Progress", status = "IN-PROCESS"},
  {id = "done",       label = "Done",        status = "COMPLETED"},
  {id = "cancelled",  label = "Cancelled",   status = "CANCELLED"},
]

[davpunk.keys]                            # overrides; duplicates are an error
# "sync_now" = "Ctrl+S"

[davpunk.mcp]
enabled     = false
transport   = "stdio"          # stdio (recommended) | sse
bind        = "127.0.0.1"      # SSE binds loopback only
port        = 8787
token_file  = "~/.config/davpunk/mcp-token"   # 0600, generated on first enable
audit       = true
list_limit  = 100              # default page size; hard cap 500

[davpunk.mcp.capabilities]
read   = false
write  = false                 # includes create, update, set_*, move
delete = false
sync   = false
```

---

## 17. First run

Shown when the config file is missing, unparseable, or has no remotes.

1. Welcome pane → "Add your first remote".
2. Remote details form (id, name, URL, username, sync interval, color) with
   `https://` enforced and live validation.
3. GPG key picker populated from `gpg --list-secret-keys --with-colons`.
4. Password dialog → credential file written 0600.
5. `config.toml` created 0600 (or appended to) via **tomlkit**, preserving
   existing comments and formatting.
6. Discovery runs; the calendars found are listed for confirmation.
7. Initial sync with a progress bar fed by the multiget batch progress.

An unparseable config shows the Pydantic validation error verbatim, naming the
file and the offending key, and does not crash.

---

## 18. Out of scope (MVP)

- RRULE expansion / instance scheduling / RECURRENCE-ID editing — multi-component
  resources are quarantined read-only rather than partially supported.
- VALARM `REPEAT` and `DURATION`; VALARM `EMAIL` action.
- iCalendar `ATTENDEE` / `ORGANIZER` / scheduling.
- Import/export of standalone `.ics` files.
- Cross-*remote* moves — `move_task_local` is scoped to calendars within one
  remote; moving between servers is a copy-then-delete the user performs.
- Mobile sync or sync-adapter integration.
- An "archive" concept — completed tasks are searchable via the search view.
- Automatic conflict merging — resolution is always user-driven.
- Multi-user or shared-calendar ACL management.
- Live config reload — changes require a restart.
