"""``davpunk sync``, ``status``, ``conflicts`` and ``doctor``."""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from datetime import UTC, datetime

from davpunk.cli import doctor
from davpunk.config import ConfigError, DavPunkConfig, load_config
from davpunk.core import cache
from davpunk.core.locking import is_syncing
from davpunk.core.sync_runner import SyncRunner

log = logging.getLogger("davpunk.cli")


def _open(args) -> tuple[DavPunkConfig, sqlite3.Connection]:
    config = load_config(getattr(args, "config", None))
    conn, report = cache.open_or_recover()
    if report is not None:
        print(report.summary(), file=sys.stderr)
    cache.reconcile_remotes(config.remotes, conn)
    return config, conn


def _ago(epoch: int | None) -> str:
    if not epoch:
        return "never"
    delta = int(datetime.now(UTC).timestamp()) - epoch
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --------------------------------------------------------------------- sync


def cmd_sync(args) -> int:
    """One cycle; a summary on stdout; non-zero on error."""
    dry = getattr(args, "dry_run", False)
    try:
        # A dry run must not open the cache for writing at all — `_open` also
        # reconciles the remotes, which is a write.  Config is enough.
        config = load_config(getattr(args, "config", None))
        conn = None if dry else _open(args)[1]
    except ConfigError as exc:
        print(f"davpunk: {exc}", file=sys.stderr)
        return 2

    remotes = config.remotes
    if args.remote:
        remotes = [r for r in remotes if r.id == args.remote]
        if not remotes:
            print(f"davpunk: no remote named {args.remote!r}", file=sys.stderr)
            if conn is not None:
                cache.close_db(conn)
            return 2
    if not remotes:
        print("davpunk: no remotes configured", file=sys.stderr)
        if conn is not None:
            cache.close_db(conn)
        return 2

    if dry:
        return _dry_run(remotes, args)

    cancel = threading.Event()
    failed = False
    try:
        for remote_config in remotes:
            runner = SyncRunner(remote_config.to_model(), conn)
            result = runner.run(cancel)
            print(result.summary())
            # A remote that was busy is not a failure: another holder has it.
            if result.error and not result.skipped:
                failed = True
    finally:
        cache.close_db(conn)
    return 1 if failed else 0


def _dry_run(remotes, args) -> int:
    from davpunk import paths
    from davpunk.core import dry_run

    cancel = threading.Event()
    failed = False
    reports = []
    for remote_config in remotes:
        report = dry_run.plan(remote_config.to_model(), paths.database_file(), cancel)
        reports.append(report)
        print(report.summary(limit=getattr(args, "limit", 20)))
        if report.result.error and not report.result.skipped:
            failed = True

    print()
    print(
        "Dry run: nothing was written to the server or to your cache. "
        f"{sum(len(r.writes) for r in reports)} request(s) were withheld."
    )
    if any(r.would_write for r in reports):
        print("Run `davpunk sync` without --dry-run to send them.")
    return 1 if failed else 0


# ------------------------------------------------------------------- forget


def cmd_forget(args) -> int:
    """Drop an orphaned remote from the cache.

    ``reconcile_remotes`` deliberately hides a remote whose config block has
    gone rather than purging it, so a typo in ``config.toml`` cannot cost data.
    The consequence is that removing an account leaves a row behind for ever,
    and until now there was no supported way to get rid of it —
    ``cache.purge_remote`` existed with no caller at all.
    """
    try:
        config, conn = _open(args)
    except ConfigError as exc:
        print(f"davpunk: {exc}", file=sys.stderr)
        return 2

    try:
        remote_id = args.remote_id
        known = {row["remote_id"]: row for row in cache.sync_status_rows(conn)}
        if remote_id not in known:
            print(f"davpunk: no remote named {remote_id!r} in the cache", file=sys.stderr)
            orphans = [r for r, row in known.items() if row["orphaned"]]
            if orphans:
                print(f"  orphaned remotes: {', '.join(sorted(orphans))}", file=sys.stderr)
            return 2

        if any(r.id == remote_id for r in config.remotes):
            # Purging it would only bring it straight back: every process
            # re-upserts its configured remotes at startup.
            print(
                f"davpunk: {remote_id!r} is still in config.toml — remove its "
                "[[davpunk.remotes]] block first, or it will reappear on the next start",
                file=sys.stderr,
            )
            return 2

        tasks = _cached_task_count(remote_id, conn)
        what = f"{tasks} cached task(s)" if tasks else "no cached tasks"
        if not args.yes:
            print(f"Forget {remote_id!r} and its {what}? This cannot be undone.")
            if input("Type the remote id to confirm: ").strip() != remote_id:
                print("Cancelled.")
                return 1

        destroyed = cache.purge_remote(remote_id, conn)
        print(f"Forgot {remote_id!r} ({destroyed} task(s) destroyed).")
        return 0
    finally:
        cache.close_db(conn)


def _cached_task_count(remote_id: str, conn) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM tasks t JOIN calendars c ON c.id = t.calendar_id "
            "WHERE c.remote_id = ?",
            (remote_id,),
        ).fetchone()[0]
    )


# ------------------------------------------------------------------- status


def cmd_status(args) -> int:
    try:
        config, conn = _open(args)
    except ConfigError as exc:
        print(f"davpunk: {exc}", file=sys.stderr)
        return 2

    try:
        rows = cache.sync_status_rows(conn)
        if not rows:
            print("No remotes configured.")
            return 0

        manual = {r.id for r in config.remotes if not r.auto_sync}
        for row in rows:
            remote_id = row["remote_id"]
            counts = _counts(remote_id, conn)
            flags = []
            if row["orphaned"]:
                flags.append("orphaned")
            if remote_id in manual:
                # Otherwise "last sync: 6d ago" looks like a fault.
                flags.append("manual only")
            if is_syncing(remote_id):  # a lock probe, never a DB column
                flags.append("syncing")

            header = f"{remote_id}"
            if row["name"] and row["name"] != remote_id:
                header += f" ({row['name']})"
            if flags:
                header += "  [" + ", ".join(flags) + "]"
            print(header)
            print(f"  last sync : {_ago(row['last_sync'])}")
            print(
                f"  pending   : {counts['pending']}"
                f"   blocked: {counts['blocked']}"
                f"   conflicts: {counts['conflicts']}"
            )
            if row["last_error"]:
                print(f"  last error: {row['last_error']}")
        return 0
    finally:
        cache.close_db(conn)


def _counts(remote_id: str, conn: sqlite3.Connection) -> dict[str, int]:
    def one(sql: str) -> int:
        return int(conn.execute(sql, (remote_id,)).fetchone()[0])

    join = (
        "FROM pending_changes pc JOIN tasks t ON t.id = pc.task_id "
        "JOIN calendars c ON c.id = t.calendar_id WHERE c.remote_id = ?"
    )
    return {
        "pending": one(f"SELECT COUNT(*) {join}"),
        "blocked": one(f"SELECT COUNT(*) {join} AND pc.blocked = 1"),
        "conflicts": one(
            "SELECT COUNT(*) FROM conflict_queue q JOIN tasks t ON t.id = q.task_id "
            "JOIN calendars c ON c.id = t.calendar_id "
            "WHERE c.remote_id = ? AND q.resolved = 0"
        ),
    }


# ---------------------------------------------------------------- conflicts


def cmd_conflicts(args) -> int:
    """Read-only.  Resolution is a UI action — it needs a human decision."""
    try:
        _, conn = _open(args)
    except ConfigError as exc:
        print(f"davpunk: {exc}", file=sys.stderr)
        return 2

    try:
        rows = cache.open_conflicts(conn)
        if not rows:
            print("No open conflicts.")
            return 0

        from davpunk.conflict.resolver import mode_of

        print(f"{len(rows)} open conflict(s):\n")
        for row in rows:
            mode = mode_of(row["local_raw_ics"], row["remote_raw_ics"])
            print(f"  [{row['id']}] {row['summary'] or row['uid']}")
            print(f"      mode      : {_mode_label(mode)}")
            print(f"      detected  : {_ago(row['detected_at'])}")
        print("\nResolve them in the DavPunk UI.")
        return 0
    finally:
        cache.close_db(conn)


def _mode_label(mode) -> str:
    from davpunk.conflict.resolver import Mode

    return {
        Mode.BOTH_CHANGED: "A  — both sides changed",
        Mode.LOCAL_DELETE: "A' — you deleted it, the server changed it",
        Mode.SERVER_DELETED: "B  — the server deleted it, you changed it",
    }[mode]


# ------------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    checks = doctor.run_all(getattr(args, "config", None))

    width = max(len(c.name) for c in checks) if checks else 0
    for check in checks:
        print(f"{check.status.value:<4} {check.name:<{width}}  {check.detail}")

    if args.fix:
        fixable = [c for c in checks if c.fixer is not None]
        if fixable:
            print()
            for line in doctor.apply_fixes(fixable):
                print(line)
            checks = doctor.run_all(getattr(args, "config", None))

    failures = [c for c in checks if c.failed]
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED.")
        return 1
    print("All checks passed.")
    return 0
