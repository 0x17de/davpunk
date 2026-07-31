"""The sync role, hosted on a dedicated ``QThread``.

The thread is long-lived and owns **its own** sqlite connection.  Nothing but
signals crosses the boundary, and those carry only ``(remote_id, phase, done,
total, error)`` — no Python objects and no connections, because a connection
used from the thread that did not create it is a hard sqlite3 error.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal

from davpunk.core import cache
from davpunk.core.sync_runner import SyncRunner

log = logging.getLogger("davpunk.ui.sync_worker")


class SyncWorker(QObject):
    """Lives on the worker thread; never touched from the GUI thread except
    through queued signal emission."""

    progress = Signal(str, str, int, int, str)  # remote, phase, done, total, error
    syncFinished = Signal(str, str)  # remote, summary
    syncFailed = Signal(str, str)  # remote, error

    def __init__(self, db_path, remotes) -> None:
        super().__init__()
        self._db_path = db_path
        self._remotes = list(remotes)
        self._conn = None
        self.cancel = threading.Event()

    def _connection(self):
        # Opened lazily, on the worker thread, so it belongs to that thread.
        if self._conn is None:
            self._conn = cache.open_db(self._db_path)
        return self._conn

    def sync_all(self) -> None:
        self.cancel.clear()
        for remote_config in self._remotes:
            if self.cancel.is_set():
                return
            self.sync_one(remote_config)

    def sync_one(self, remote_config) -> None:
        remote = remote_config.to_model()
        try:
            runner = SyncRunner(remote, self._connection(), progress=self._emit_progress)
            result = runner.run(self.cancel)
        except Exception as exc:  # a worker crash must not take the UI with it
            log.exception("Sync worker failed for %s", remote.id)
            self.syncFailed.emit(remote.id, str(exc))
            return

        if result.error and not result.skipped:
            self.syncFailed.emit(remote.id, result.error)
        else:
            self.syncFinished.emit(remote.id, result.summary())

    def _emit_progress(self, remote_id, phase, done, total, error) -> None:
        self.progress.emit(remote_id, phase, done, total, error or "")

    def shutdown(self) -> None:
        if self._conn is not None:
            cache.close_db(self._conn)
            self._conn = None


class SyncController(QObject):
    """Owns the thread and the worker, and is what the window talks to."""

    #: Closing waits this long for the current item, then detaches — the flock
    #: releases at process exit regardless.
    SHUTDOWN_WAIT_MS = 5000

    requestSyncAll = Signal()
    requestSyncOne = Signal(object)

    def __init__(self, db_path, remotes, parent=None) -> None:
        super().__init__(parent)
        self.thread = QThread()
        self.thread.setObjectName("davpunk-sync")
        self.worker = SyncWorker(db_path, remotes)
        self.worker.moveToThread(self.thread)

        self.requestSyncAll.connect(self.worker.sync_all)
        self.requestSyncOne.connect(self.worker.sync_one)
        self.thread.finished.connect(self.worker.shutdown)
        self.thread.start()

    @property
    def progress(self):
        return self.worker.progress

    @property
    def syncFinished(self):
        return self.worker.syncFinished

    @property
    def syncFailed(self):
        return self.worker.syncFailed

    def sync_all(self) -> None:
        self.requestSyncAll.emit()

    def stop(self) -> None:
        """Cancel, wait briefly, then detach."""
        self.worker.cancel.set()
        self.thread.quit()
        if not self.thread.wait(self.SHUTDOWN_WAIT_MS):
            log.warning("Sync thread did not stop in %d ms; detaching", self.SHUTDOWN_WAIT_MS)
