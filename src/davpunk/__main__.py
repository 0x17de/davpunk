"""``davpunk`` — CLI dispatch.  With no subcommand, the UI launches."""

from __future__ import annotations

import argparse
import sys

from davpunk import logging_setup, paths, preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="davpunk",
        description="Work it. Sync it. Check it. Done. — a CalDAV VTODO client",
    )
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--log-level", help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument(
        "--new-instance",
        action="store_true",
        help="bypass the single-instance lock and start another UI",
    )

    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="run one sync cycle and print a summary")
    sync.add_argument("--remote", help="sync only this remote id")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="show what a sync would send and change, writing nothing",
    )
    sync.add_argument(
        "--limit",
        type=int,
        default=20,
        help="how many items to name per section in a --dry-run (default 20)",
    )

    sub.add_parser("status", help="per-remote sync state and pending counts")
    sub.add_parser("conflicts", help="list open conflicts (read-only)")

    doctor = sub.add_parser("doctor", help="check every runtime assumption")
    doctor.add_argument("--fix", action="store_true", help="repair file modes")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # doctor reports the floors individually rather than dying on the first one.
    entrypoint = "cli" if args.command else "ui"
    logging_setup.setup_logging(entrypoint, args.log_level)
    if args.command != "doctor":
        preflight.preflight_or_die()
    paths.ensure_runtime_dirs()

    from davpunk.cli import commands

    if args.command == "sync":
        return commands.cmd_sync(args)
    if args.command == "status":
        return commands.cmd_status(args)
    if args.command == "conflicts":
        return commands.cmd_conflicts(args)
    if args.command == "doctor":
        return commands.cmd_doctor(args)

    return _launch_ui(args)


def _launch_ui(args) -> int:
    try:
        from davpunk.ui.app import run
    except ImportError as exc:
        print(
            f"davpunk: the UI needs PySide6, which is not importable ({exc}).\n"
            "Install it with:  uv sync --extra ui\n"
            "The CLI subcommands (sync, status, conflicts, doctor) work without it.",
            file=sys.stderr,
        )
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
