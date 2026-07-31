"""Runtime floor assertions, executed by every entrypoint before the database.

Each check is also reported individually by ``davpunk doctor`` (see
:mod:`davpunk.cli.doctor`), which imports :func:`checks` rather than
duplicating the logic.
"""

from __future__ import annotations

import platform
import sqlite3
import sys
import zoneinfo
from collections.abc import Callable

MIN_PYTHON = (3, 11)
MIN_SQLITE = (3, 35, 0)
SAMPLE_TZID = "Europe/Berlin"


class PreflightError(RuntimeError):
    """A runtime floor was not met."""


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise PreflightError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required (found {platform.python_version()})"
        )


def check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise PreflightError(
            "SQLite 3.35+ required for partial-index UPSERT "
            f"(found {sqlite3.sqlite_version}); "
            "your Python is linked against an older library"
        )


def check_fts5() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError as exc:
        raise PreflightError(
            "This Python's SQLite was built without FTS5; search is unavailable"
        ) from exc
    finally:
        conn.close()


def check_tzdata() -> None:
    try:
        zoneinfo.ZoneInfo(SAMPLE_TZID)
    except zoneinfo.ZoneInfoNotFoundError as exc:
        raise PreflightError(
            "No time-zone database found; install system tzdata or the 'tzdata' Python package"
        ) from exc


#: ``(label, callable)`` pairs, in the order they are run.
CHECKS: list[tuple[str, Callable[[], None]]] = [
    ("python", check_python),
    ("sqlite-version", check_sqlite_version),
    ("sqlite-fts5", check_fts5),
    ("tzdata", check_tzdata),
]


def checks() -> list[tuple[str, Callable[[], None]]]:
    return list(CHECKS)


def preflight() -> None:
    """Raise :class:`PreflightError` on the first unmet requirement."""
    for _label, fn in CHECKS:
        fn()


def preflight_or_die() -> None:
    """Entrypoint helper: print to stderr and exit 1 rather than traceback."""
    try:
        preflight()
    except PreflightError as exc:
        print(f"davpunk: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
