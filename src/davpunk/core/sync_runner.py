"""The sync role.

``SyncRunner`` lives in ``core/``, not in the daemon: the daemon, the UI's
worker thread, MCP ``sync_now`` and ``davpunk sync`` all instantiate the *same*
class.  Whichever gets the remote's ``flock`` first becomes the sync role for
that remote; the rest see :class:`SyncBusy`.

Cancellation is cooperative and checked **between items** and between multiget
batches, never mid-request.  Every write is a per-item transaction, so
cancelling never leaves a half-applied state, and a PUT whose response was
received is always recorded before the next check.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from davpunk.core import cache
from davpunk.core.credentials import (
    CredentialError,
    DecryptWarningLimiter,
    decrypt_credential,
)
from davpunk.core.locking import SyncBusy, remote_sync_lock
from davpunk.models.remote import Remote

log = logging.getLogger("davpunk.core.sync_runner")

#: ``(remote_id, phase, done, total, error)`` — the only thing that crosses the
#: worker/UI boundary.  No Python objects, no connections.
ProgressCallback = Callable[[str, str, int, int, str | None], None]


@dataclass
class SyncResult:
    remote_id: str
    pulled: int = 0
    pushed: int = 0
    conflicts: int = 0
    deleted: int = 0
    cancelled: bool = False
    skipped: bool = False
    error: str | None = None
    calendars: int = 0
    expired: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.skipped:
            return f"{self.remote_id}: skipped ({self.error or 'busy'})"
        if self.error:
            return f"{self.remote_id}: FAILED — {self.error}"
        parts = [
            f"{self.calendars} calendar(s)",
            f"{self.pulled} pulled",
            f"{self.pushed} pushed",
        ]
        if self.conflicts:
            parts.append(f"{self.conflicts} conflict(s)")
        if self.cancelled:
            parts.append("cancelled")
        return f"{self.remote_id}: " + ", ".join(parts)


class SyncRunner:
    """One remote, one cycle: lock → credentials → discover → pull/push → expire."""

    def __init__(
        self,
        remote: Remote,
        conn: sqlite3.Connection,
        *,
        client_factory: Callable[[Remote, str], object] | None = None,
        progress: ProgressCallback | None = None,
        warn_limiter: DecryptWarningLimiter | None = None,
    ) -> None:
        self.remote = remote
        self.conn = conn
        self.progress = progress
        self._client_factory = client_factory
        self._warn_limiter = warn_limiter or DecryptWarningLimiter()

    # ------------------------------------------------------------------ hooks

    def _emit(self, phase: str, done: int = 0, total: int = 0, error: str | None = None) -> None:
        if self.progress is not None:
            self.progress(self.remote.id, phase, done, total, error)

    def _make_client(self, credential: str):
        if self._client_factory is not None:
            return self._client_factory(self.remote, credential)
        from davpunk.core.caldav_client import CalDAVClient

        return CalDAVClient(self.remote, credential)

    # ------------------------------------------------------------------- run

    def run(self, cancel: threading.Event | None = None) -> SyncResult:
        cancel = cancel or threading.Event()
        result = SyncResult(remote_id=self.remote.id)

        try:
            with remote_sync_lock(self.remote.id):
                return self._cycle(cancel, result)
        except SyncBusy as exc:
            # Not an error: the daemon and the UI legitimately race every cycle.
            log.debug("%s", exc)
            result.skipped = True
            result.error = "another sync is already running"
            return result

    def _cycle(self, cancel: threading.Event, result: SyncResult) -> SyncResult:
        from davpunk.core import sync_engine

        try:
            credential = decrypt_credential(self.remote.gpg_file or "")
        except CredentialError as exc:
            # Skip the remote with a rate-limited warning, and record it so the
            # UI can show "credentials locked".
            if self._warn_limiter.should_warn(self.remote.id):
                log.warning("Skipping remote %s: %s", self.remote.id, exc)
            cache.set_sync_status(self.remote.id, self.conn, last_error=str(exc))
            result.skipped = True
            result.error = str(exc)
            return result
        self._warn_limiter.reset(self.remote.id)

        client = self._make_client(credential)
        try:
            self._emit("discover")
            calendars = sync_engine.discover_calendars(self.remote, client, self.conn)
            result.calendars = len(calendars)

            for calendar in calendars:
                if cancel.is_set():
                    result.cancelled = True
                    break
                self._emit("pull")
                pull = sync_engine.pull_phase(
                    calendar, client, self.conn, cancel, progress=self._emit
                )
                result.pulled += pull.applied
                result.conflicts += pull.conflicts
                result.deleted += pull.deleted

                if cancel.is_set():
                    result.cancelled = True
                    break
                self._emit("push")
                push = sync_engine.push_phase(
                    calendar, client, self.conn, cancel, progress=self._emit
                )
                result.pushed += push.pushed
                result.conflicts += push.conflicts
                if push.cancelled or pull.cancelled:
                    result.cancelled = True

            self._emit("expire")
            result.expired = cache.expire(self.conn)
            cache.set_sync_status(self.remote.id, self.conn, last_sync=cache.now(), last_error=None)
        except Exception as exc:  # remote-level failure
            log.exception("Sync failed for remote %s", self.remote.id)
            result.error = str(exc)
            cache.set_sync_status(self.remote.id, self.conn, last_error=str(exc))
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        self._emit("done", error=result.error)
        return result
