"""The optional background sync daemon.

It owns nothing the UI does not: it instantiates the same :class:`SyncRunner`,
takes the same per-remote ``flock``, and skips a remote the UI is already
syncing.  SIGTERM sets the cancel event; the current item finishes, the lock
releases, exit 0.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from types import FrameType

from davpunk import logging_setup, paths, preflight
from davpunk.config import ConfigError, load_config
from davpunk.core import cache
from davpunk.core.credentials import DecryptWarningLimiter
from davpunk.core.sync_runner import SyncRunner
from davpunk.notifications import alarm_scan

log = logging.getLogger("davpunk.daemon")

#: How often to look for due alarms, independent of any remote's interval.
ALARM_SCAN_INTERVAL = 60


class SyncDaemon:
    def __init__(self, config, conn, *, once: bool = False) -> None:
        self.config = config
        self.conn = conn
        self.once = once
        self.cancel = threading.Event()
        self._warn_limiter = DecryptWarningLimiter()
        self._next_due: dict[str, float] = {}
        self._next_alarm_scan = 0.0

    def request_stop(self, signum: int, _frame: FrameType | None = None) -> None:
        log.info("Signal %s received; finishing the current item", signal.Signals(signum).name)
        self.cancel.set()

    def run(self) -> int:
        configured = list(self.config.remotes)
        if not configured:
            log.error("No remotes configured; nothing to sync")
            return 1

        # Reconcile *every* remote, including the ones this daemon will not
        # touch: their rows, credentials and tasks are still theirs, and an
        # account is excluded from the timer, not from the cache.
        cache.reconcile_remotes(configured, self.conn)

        remotes = [r for r in configured if r.auto_sync]
        excluded = [r.id for r in configured if not r.auto_sync]
        if excluded:
            log.info(
                "Not auto-syncing %s (auto_sync = false); sync them with "
                "`davpunk sync --remote ID` or from the UI",
                ", ".join(excluded),
            )
        if not remotes:
            # Still worth running: alarms are scanned regardless of any remote.
            log.warning("No remote has auto_sync enabled; only alarms will be scanned")
        log.info("Watching %d remote(s)", len(remotes))

        while not self.cancel.is_set():
            now = time.monotonic()  # in-process timing is monotonic

            for config in remotes:
                if self.cancel.is_set():
                    break
                if self._next_due.get(config.id, 0.0) > now:
                    continue
                self._sync_one(config)
                self._next_due[config.id] = time.monotonic() + config.sync_interval

            if now >= self._next_alarm_scan:
                self._scan_alarms()
                self._next_alarm_scan = time.monotonic() + ALARM_SCAN_INTERVAL

            if self.once:
                break
            self._sleep_until_next()

        log.info("Daemon stopped")
        return 0

    def _sync_one(self, config) -> None:
        runner = SyncRunner(config.to_model(), self.conn, warn_limiter=self._warn_limiter)
        result = runner.run(self.cancel)
        if result.error and not result.skipped:
            log.warning("%s", result.summary())
        else:
            log.info("%s", result.summary())

    def _scan_alarms(self) -> None:
        try:
            result = alarm_scan.scan(self.conn)
        except Exception:
            log.exception("Alarm scan failed")
            return
        if result.notified:
            log.info("Delivered %d alarm(s)", result.notified)

    def _sleep_until_next(self) -> None:
        now = time.monotonic()
        candidates = [*self._next_due.values(), self._next_alarm_scan]
        delay = max(1.0, min(candidates) - now) if candidates else 60.0
        # Event.wait returns early when SIGTERM sets the flag, so shutdown is
        # not gated on the sleep.
        self.cancel.wait(min(delay, 60.0))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="davpunk-sync", description="DavPunk background sync daemon"
    )
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--log-level", help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging_setup.setup_logging("daemon", args.log_level)  # stderr → journald
    preflight.preflight_or_die()
    paths.ensure_runtime_dirs()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    conn, report = cache.open_or_recover()
    if report is not None:
        log.error("%s", report.summary())

    daemon = SyncDaemon(config, conn, once=args.once)
    signal.signal(signal.SIGTERM, daemon.request_stop)
    signal.signal(signal.SIGINT, daemon.request_stop)

    try:
        return daemon.run()
    finally:
        cache.close_db(conn)


if __name__ == "__main__":
    raise SystemExit(main())
