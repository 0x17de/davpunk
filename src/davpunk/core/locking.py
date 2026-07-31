"""Process-level exclusion via ``flock``.

Two things make ``flock`` the right primitive here rather than a database
column:

* the kernel releases the lock when the file descriptor closes **and** when the
  process dies, so a ``SIGKILL`` cannot leave a stuck "sync in progress" flag;
* locks attach to the open file description, so a probe conflicts even with a
  holder inside the same process — the UI's sync thread and the UI's main
  thread genuinely contend, which is what we want.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from davpunk import paths

log = logging.getLogger("davpunk.core.locking")


class SyncBusy(Exception):
    """Another holder already owns this remote's sync lock."""

    def __init__(self, remote_id: str) -> None:
        super().__init__(f"a sync is already running for remote {remote_id!r}")
        self.remote_id = remote_id


class UiAlreadyRunning(Exception):
    """A DavPunk UI already holds ``ui.lock``."""


def lock_path(remote_id: str) -> Path:
    paths.ensure_dir(paths.lock_dir())
    return paths.remote_lock_file(remote_id)


def _open_lock(path: Path) -> int:
    paths.ensure_dir(path.parent)
    return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)


@contextmanager
def remote_sync_lock(remote_id: str) -> Iterator[None]:
    """Exactly one sync role per remote.

    Whoever gets here first — the daemon, the UI worker thread, MCP ``sync_now``
    or ``davpunk sync`` — wins; the rest see :class:`SyncBusy` and skip.
    """
    fd = _open_lock(lock_path(remote_id))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SyncBusy(remote_id) from exc
            raise
        log.debug("Acquired sync lock for %s", remote_id)
        yield
    finally:
        # close() releases the lock, and so does process death.
        os.close(fd)


def is_syncing(remote_id: str) -> bool:
    """Whether a sync is running — a lock probe, never a database column.

    A DB flag survives ``SIGKILL`` and then sticks on forever, hiding the
    remote from every future sync.  This state dies with the process.
    """
    fd = _open_lock(lock_path(remote_id))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return True
        raise
    finally:
        os.close(fd)


@contextmanager
def ui_lock() -> Iterator[int]:
    """Single-instance guard for the UI.

    Yields the held descriptor so the caller can keep it alive for the process
    lifetime.  A second launch raises :class:`UiAlreadyRunning`; the entrypoint
    then asks the running instance to raise its window and exits 0.
    """
    paths.ensure_dir(paths.lock_dir())
    fd = _open_lock(paths.ui_lock_file())
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise UiAlreadyRunning() from exc
        raise
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield fd
    finally:
        os.close(fd)


def ui_is_running() -> bool:
    fd = _open_lock(paths.ui_lock_file())
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return True
        raise
    finally:
        os.close(fd)
