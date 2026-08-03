"""Schema rev 8 DDL, as one migration.

Kept apart from :mod:`davpunk.core.cache` so the DDL reads as a document rather
than as string literals wedged between helper functions.

The DDL is applied statement by statement, **not** via
``sqlite3.Connection.executescript``: that method issues an implicit ``COMMIT``
before it runs, which would silently end the ``BEGIN IMMEDIATE`` that
``migrate()`` opened and leave a half-applied schema uncommitted-but-durable.
:func:`split_statements` does the splitting, and understands that a
``CREATE TRIGGER`` body contains semicolons of its own.
"""

from __future__ import annotations

SCHEMA_V1 = """
------------------------------------------------------------------ remotes
-- Cache of [[davpunk.remotes]]; the TOML config is authoritative.
-- Reconciled at startup by every process from its own config read.
CREATE TABLE remotes (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    url            TEXT,
    username       TEXT,
    gpg_file       TEXT,
    gpg_key_id     TEXT,
    sync_interval  INTEGER,
    color          TEXT,
    pinned_view    INTEGER NOT NULL DEFAULT 0,
    allow_insecure INTEGER NOT NULL DEFAULT 0,
    verify_tls     INTEGER NOT NULL DEFAULT 1,
    orphaned       INTEGER NOT NULL DEFAULT 0
);

------------------------------------------------------------------ calendars
-- id is DETERMINISTIC: sha256(remote_id || '\\0' || href)[:16].
-- Recovery-triggered rebuilds (T44) are a normal event, and MCP task_refs
-- embed this value — an arbitrary key minted at discovery would invalidate
-- every reference an agent holds.
CREATE TABLE calendars (
    id             TEXT PRIMARY KEY,
    remote_id      TEXT NOT NULL REFERENCES remotes(id) ON DELETE CASCADE,
    href           TEXT NOT NULL,
    display_name   TEXT,
    color          TEXT,
    ctag           TEXT,                        -- short-circuits the pull
    sync_token     TEXT,                        -- RFC 6578, when supported
    supports_sync  INTEGER NOT NULL DEFAULT 0,
    available      INTEGER NOT NULL DEFAULT 1,
    last_sync      INTEGER,
    UNIQUE (remote_id, href)
);

------------------------------------------------------------------ tasks
-- Canonical remote identity: (calendar_id, href)
-- uid: iCalendar identity; indexed; NOT unique across calendars; never changes
--
-- sync_state:
--   new             created locally, or moved and not yet placed at the target
--   clean           server and local agree
--   dirty           editor has unsent changes to an existing resource
--   conflict        diverged; awaiting resolve_conflict(); edits REJECTED
--   pending_delete  user deleted; DELETE not yet confirmed by the server
--
-- read_only_reason:  NULL = editable
--   'multipart'             >1 VTODO in the resource (recurring series with
--                           RECURRENCE-ID overrides); re-serializing would
--                           destroy them
--   'oversize'              raw_ics exceeds max_resource_bytes
--   'calendar-unavailable'  the collection vanished from the server
--   DELETE is permitted for all three, and MOVE for the first two: both
--   relocate or remove bytes verbatim and never re-serialize.
--
-- Date fidelity:
--   *_value  original RFC 5545 value, verbatim; AUTHORITATIVE for serialization
--   *_tzid   original TZID param; NULL = DATE, floating, or UTC
--   due / dtstart (INTEGER) are COARSE sort keys ONLY — never serialized, and
--   never used for overdue decisions (see due_deadline(), T69)
CREATE TABLE tasks (
    id               TEXT PRIMARY KEY,          -- local UUID; NEVER over MCP
    calendar_id      TEXT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    href             TEXT NOT NULL,
    uid              TEXT NOT NULL,
    etag             TEXT,
    raw_ics          TEXT,                      -- the COMPLETE VCALENDAR
    read_only_reason TEXT,
    summary          TEXT,
    description      TEXT,
    status           TEXT,
    priority         INTEGER,                   -- NULL = undefined; never 0
    percent_complete INTEGER,
    dtstart          INTEGER,                   -- coarse sort key
    dtstart_value    TEXT,
    dtstart_tzid     TEXT,
    due              INTEGER,                   -- coarse sort key
    due_value        TEXT,
    due_tzid         TEXT,
    completed        INTEGER,                   -- always UTC per RFC; exact
    rrule            TEXT,                      -- opaque; not expanded in MVP
    url              TEXT,
    location         TEXT,
    last_modified    INTEGER,
    created          INTEGER,
    sequence         INTEGER NOT NULL DEFAULT 0,-- bumped on local PUT
    parent_uid       TEXT,                      -- resolved in calendar_id
    davpunk_order    INTEGER,
    kanban_col       TEXT,
    sync_state       TEXT NOT NULL DEFAULT 'clean'
);
CREATE UNIQUE INDEX tasks_remote    ON tasks (calendar_id, href);
CREATE INDEX        tasks_uid       ON tasks (uid);
CREATE INDEX        tasks_cal_uid   ON tasks (calendar_id, uid);
CREATE INDEX        tasks_parent    ON tasks (calendar_id, parent_uid);
CREATE INDEX        tasks_cal_state ON tasks (calendar_id, sync_state);

------------------------------------------------------------------ categories
CREATE TABLE task_categories (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (task_id, category)
) WITHOUT ROWID;
CREATE INDEX task_categories_cat ON task_categories (category);

------------------------------------------------------------------ valarms
-- Display action; relative trigger only; no REPEAT/DURATION.
-- A relative trigger with no anchor is invalid: dropped at parse.
CREATE TABLE valarms (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    related        TEXT NOT NULL DEFAULT 'END',  -- START=DTSTART | END=DUE
    trigger_offset INTEGER NOT NULL              -- seconds; negative = before
);
CREATE INDEX valarms_task ON valarms (task_id);

------------------------------------------------------------------ alarm_fires
-- Keyed on the COMPUTED absolute trigger time, not on valarms.id: alarm rows
-- are dropped and reinserted on every server pull, so a flag stored there
-- would reset itself.  Moving DUE yields a new trigger_at, so a rescheduled
-- task legitimately fires again.
CREATE TABLE alarm_fires (
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    trigger_at INTEGER NOT NULL,
    fired_at   INTEGER NOT NULL,
    PRIMARY KEY (task_id, trigger_at)
) WITHOUT ROWID;

------------------------------------------------------------------ pending_changes
-- One row per task with unsent intent. Editor upserts; sync role drains.
-- base_etag: ETag at the FIRST edit after a clean sync; never overwritten by
--            later edits. If-Match on PUT/DELETE. NULL = unguarded/create.
CREATE TABLE pending_changes (
    task_id            TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    change_type        TEXT NOT NULL,               -- create|update|delete|move
    base_etag          TEXT,
    queued_at          INTEGER NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    INTEGER NOT NULL DEFAULT 0,  -- wall clock; clamped
    last_error         TEXT,
    blocked            INTEGER NOT NULL DEFAULT 0,
    -- move only
    source_calendar_id TEXT,
    source_href        TEXT,
    move_stage         INTEGER NOT NULL DEFAULT 0
    --   0 = target PUT still needed
    --   1 = target PUT confirmed; only the source DELETE remains.
    --   Persisted so a crash between the stages never re-PUTs.
);
CREATE INDEX pending_ready ON pending_changes (next_attempt_at) WHERE blocked = 0;

------------------------------------------------------------------ conflict_queue
-- local_raw_ics  NULL -> local intent is DELETE (dialog mode A')
-- remote_raw_ics NULL -> server deleted the resource (dialog mode B)
CREATE TABLE conflict_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    local_raw_ics  TEXT,
    remote_raw_ics TEXT,
    remote_etag    TEXT,
    detected_at    INTEGER NOT NULL,
    resolved       INTEGER NOT NULL DEFAULT 0
    -- deferred_at INTEGER is added by migration v2 (cache.MIGRATIONS), NOT
    -- here: this DDL is v1 and every existing database has already run it.
);
-- At most ONE open conflict per task, enforced by the database so a future
-- detection site cannot reintroduce the unbounded-insert bug.
-- Requires SQLite >= 3.24 for partial-index UPSERT inference.
CREATE UNIQUE INDEX conflict_open ON conflict_queue (task_id) WHERE resolved = 0;

------------------------------------------------------------------ tombstones
-- Matched on (calendar_id, uid) — a stale client replaying a deleted task
-- reuses the UID but often lands on a different href.  A user genuinely
-- re-creating "the same" task mints a new UID.  TTL 30 d.
-- Also written pre-emptively by move_task_local() to guard the source
-- collection during the PUT->DELETE window.
CREATE TABLE tombstones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id     TEXT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    href            TEXT NOT NULL,
    uid             TEXT NOT NULL,
    deleted_at      INTEGER NOT NULL,
    synced_at       INTEGER NOT NULL,
    resurrect_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX tombstones_lookup ON tombstones (calendar_id, uid);

------------------------------------------------------------------ sync_status
-- Remote-level errors only. Per-task errors live on pending_changes.last_error.
-- NOTE: deliberately NO in_progress column — a DB flag survives SIGKILL and
-- sticks on forever. Whether a sync is running is a flock probe.
CREATE TABLE sync_status (
    remote_id  TEXT PRIMARY KEY REFERENCES remotes(id) ON DELETE CASCADE,
    last_sync  INTEGER,
    last_error TEXT
);

------------------------------------------------------------------ mcp_audit
CREATE TABLE mcp_audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    tool     TEXT NOT NULL,
    task_ref TEXT,
    detail   TEXT
);

------------------------------------------------------------------ search
CREATE VIRTUAL TABLE tasks_fts USING fts5(
    summary, description, content='tasks', content_rowid='rowid');

CREATE TRIGGER tasks_ai AFTER INSERT ON tasks BEGIN
  INSERT INTO tasks_fts(rowid, summary, description)
  VALUES (new.rowid, new.summary, new.description);
END;
CREATE TRIGGER tasks_ad AFTER DELETE ON tasks BEGIN
  INSERT INTO tasks_fts(tasks_fts, rowid, summary, description)
  VALUES ('delete', old.rowid, old.summary, old.description);
END;
-- UPDATE OF, not bare UPDATE: a bare trigger fires on every sync_state / etag /
-- last_modified write, so every apply_server_version() in every cycle would
-- rewrite the FTS index.
CREATE TRIGGER tasks_au AFTER UPDATE OF summary, description ON tasks BEGIN
  INSERT INTO tasks_fts(tasks_fts, rowid, summary, description)
  VALUES ('delete', old.rowid, old.summary, old.description);
  INSERT INTO tasks_fts(rowid, summary, description)
  VALUES (new.rowid, new.summary, new.description);
END;

-- External-content FTS5 starts EMPTY and its 'delete' commands corrupt an
-- unbuilt index, so the creating migration must end with this.
INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild');
"""


def _is_only_comments(chunk: str) -> bool:
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    return all(ln.startswith("--") for ln in lines)


def _code_part(line: str) -> str:
    """The line with any trailing ``--`` comment removed.

    Statement terminators in this DDL routinely sit before a trailing citation
    comment (``CREATE INDEX … ;   --``), so the terminator test has to
    look past it.  No string literal in the schema contains ``--``.
    """
    return line.split("--", 1)[0].strip()


def split_statements(script: str) -> list[str]:
    """Split a DDL script into individually-executable statements.

    A ``CREATE TRIGGER`` body is delimited by ``BEGIN`` ... ``END;`` and contains
    its own semicolons, so a naive ``split(";")`` would tear it in half.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger = False

    for line in script.splitlines():
        buffer.append(line)
        code = _code_part(line)
        upper = code.upper()

        if not in_trigger and upper.startswith("CREATE TRIGGER"):
            in_trigger = True

        if in_trigger:
            if upper == "END;":
                in_trigger = False
                statements.append("\n".join(buffer))
                buffer = []
            continue

        if code.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []

    tail = "\n".join(buffer)
    if tail.strip():
        statements.append(tail)

    return [s for s in statements if s.strip() and not _is_only_comments(s)]


SCHEMA_V1_STATEMENTS = split_statements(SCHEMA_V1)
