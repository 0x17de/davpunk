"""flock-based exclusion.

The subprocess tests are the point: a lock that only worked within one process
would not stop the daemon and the UI from syncing the same remote at once, and
one that survived process death would strand the remote forever.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from davpunk.core.locking import (
    SyncBusy,
    UiAlreadyRunning,
    is_syncing,
    remote_sync_lock,
    ui_is_running,
    ui_lock,
)

HOLDER = textwrap.dedent(
    """
    import os, sys, time
    from davpunk.core.locking import remote_sync_lock
    with remote_sync_lock(sys.argv[1]):
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)


def _spawn_holder(remote_id: str, seconds: float, env_home) -> subprocess.Popen:
    env = dict(os.environ, DAVPUNK_HOME=str(env_home))
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, remote_id, str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held", proc.stderr.read()
    return proc


# ------------------------------------------------------------ within-process


def test_the_lock_is_exclusive_within_one_process():
    """flock attaches to the open file description, so the probe conflicts even
    with a holder in the same process — the UI thread and its sync thread do
    genuinely contend."""
    with remote_sync_lock("work"), pytest.raises(SyncBusy), remote_sync_lock("work"):
        pass


def test_different_remotes_do_not_contend():
    with remote_sync_lock("work"), remote_sync_lock("personal"):
        pass


def test_the_lock_is_released_on_normal_exit():
    with remote_sync_lock("work"):
        pass
    with remote_sync_lock("work"):
        pass


def test_the_lock_is_released_when_the_body_raises():
    with pytest.raises(RuntimeError), remote_sync_lock("work"):
        raise RuntimeError("boom")
    assert is_syncing("work") is False


def test_is_syncing_is_false_when_nothing_holds_it():
    assert is_syncing("work") is False


def test_is_syncing_does_not_leave_the_lock_held():
    assert is_syncing("work") is False
    with remote_sync_lock("work"):
        pass


def test_is_syncing_sees_an_in_process_holder():
    with remote_sync_lock("work"):
        assert is_syncing("work") is True


# ---------------------------------------------------------- across processes


def test_a_second_process_sees_sync_busy(davpunk_home):
    proc = _spawn_holder("work", 5, davpunk_home)
    try:
        assert is_syncing("work") is True
        with pytest.raises(SyncBusy) as excinfo, remote_sync_lock("work"):
            pass
        assert excinfo.value.remote_id == "work"
    finally:
        proc.terminate()
        proc.wait(10)


def test_the_kernel_releases_the_lock_when_the_holder_is_killed(davpunk_home):
    """SIGKILL, the case a database column could never recover from."""
    proc = _spawn_holder("work", 30, davpunk_home)
    assert is_syncing("work") is True

    proc.send_signal(signal.SIGKILL)
    proc.wait(10)

    deadline = time.monotonic() + 5
    while is_syncing("work") and time.monotonic() < deadline:
        time.sleep(0.05)

    assert is_syncing("work") is False
    with remote_sync_lock("work"):
        pass


def test_a_released_lock_is_immediately_reacquirable(davpunk_home):
    proc = _spawn_holder("work", 0.2, davpunk_home)
    proc.wait(10)
    with remote_sync_lock("work"):
        pass


# ----------------------------------------------------------- single instance


def test_ui_lock_is_single_instance():
    with ui_lock():
        assert ui_is_running() is True
        with pytest.raises(UiAlreadyRunning), ui_lock():
            pass


def test_ui_lock_records_the_holders_pid():
    from davpunk import paths

    with ui_lock():
        assert paths.ui_lock_file().read_text().strip() == str(os.getpid())


def test_ui_lock_is_released_on_exit():
    with ui_lock():
        pass
    assert ui_is_running() is False


def test_ui_lock_and_sync_locks_are_independent():
    with ui_lock(), remote_sync_lock("work"):
        pass


def test_lock_files_are_owner_only():
    from davpunk import paths

    with remote_sync_lock("work"):
        mode = paths.remote_lock_file("work").stat().st_mode
    assert mode & 0o077 == 0
