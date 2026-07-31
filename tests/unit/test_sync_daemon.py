"""The daemon loop: intervals, cancellation, and the shared sync role."""

from __future__ import annotations

import signal
import time

import pytest

from davpunk.config import DavPunkConfig, RemoteConfig
from davpunk.core import cache
from davpunk.core.locking import remote_sync_lock
from davpunk.daemon.sync_daemon import SyncDaemon, build_parser
from davpunk.notifications import alarm_scan


@pytest.fixture
def config():
    return DavPunkConfig(
        remotes=[
            RemoteConfig(
                id="work", url="https://cal.example.test/dav/", username="u", sync_interval=30
            )
        ]
    )


@pytest.fixture
def daemon(config, conn):
    return SyncDaemon(config, conn, once=True)


def test_a_daemon_with_no_remotes_reports_rather_than_spinning(conn):
    assert SyncDaemon(DavPunkConfig(), conn, once=True).run() == 1


def test_one_pass_reconciles_the_remotes(daemon, conn):
    daemon.run()
    assert conn.execute("SELECT COUNT(*) FROM remotes").fetchone()[0] == 1


def test_a_missing_credential_skips_the_remote_without_failing(daemon, conn):
    """The remote is skipped with a rate-limited warning and last_error is
    recorded so the UI can show "credentials locked"."""
    assert daemon.run() == 0
    row = conn.execute("SELECT last_error FROM sync_status WHERE remote_id = 'work'").fetchone()
    assert row["last_error"] is not None


def test_the_credential_warning_limiter_is_shared_across_cycles(daemon):
    """One limiter for the daemon's whole life, not one per cycle.

    A fresh limiter per cycle would mean a warning every ``sync_interval``
    seconds for as long as gpg-agent stays cold — which on a 30 s interval is
    2880 identical journal lines a day.
    """
    cache.reconcile_remotes(daemon.config.remotes, daemon.conn)
    daemon._sync_one(daemon.config.remotes[0])
    assert daemon._warn_limiter.should_warn("work") is False


def test_sigterm_sets_the_cancel_event(daemon):
    assert not daemon.cancel.is_set()
    daemon.request_stop(signal.SIGTERM)
    assert daemon.cancel.is_set()


def test_a_cancelled_daemon_exits_zero(config, conn):
    daemon = SyncDaemon(config, conn)
    daemon.cancel.set()
    assert daemon.run() == 0


def test_the_sleep_returns_immediately_once_cancelled(config, conn):
    """Shutdown is not gated on the sleep interval: SIGTERM sets the event and
    Event.wait returns at once, so the loop re-checks and exits."""
    daemon = SyncDaemon(config, conn)
    daemon._next_due["work"] = time.monotonic() + 3600

    daemon.request_stop(signal.SIGTERM)
    started = time.monotonic()
    daemon._sleep_until_next()

    assert time.monotonic() - started < 1.0


def test_a_remote_another_process_is_syncing_is_skipped(daemon, conn):
    """The daemon and the UI legitimately race every cycle; the loser skips."""
    with remote_sync_lock("work"):
        assert daemon.run() == 0


def test_the_alarm_scan_runs_in_the_loop(daemon, conn, monkeypatch):
    calls = {"n": 0}

    def counting_scan(_conn):
        calls["n"] += 1
        return alarm_scan.ScanResult()

    monkeypatch.setattr(alarm_scan, "scan", counting_scan)
    daemon.run()
    assert calls["n"] == 1


def test_a_failing_alarm_scan_does_not_kill_the_daemon(daemon, monkeypatch):
    def boom(_conn):
        raise RuntimeError("dbus went away")

    monkeypatch.setattr(alarm_scan, "scan", boom)
    assert daemon.run() == 0


def test_intervals_are_honoured(config, conn, monkeypatch):
    """A remote is not re-synced before its sync_interval has elapsed."""
    daemon = SyncDaemon(config, conn, once=True)
    synced: list[str] = []
    monkeypatch.setattr(daemon, "_sync_one", lambda c: synced.append(c.id))

    daemon.run()
    daemon.run()  # immediately again: still inside the 30 s interval

    assert synced == ["work"]


def test_the_parser_exposes_the_documented_flags():
    args = build_parser().parse_args(["--once", "--log-level", "DEBUG", "--config", "/tmp/x.toml"])
    assert args.once is True
    assert args.log_level == "DEBUG"
    assert args.config == "/tmp/x.toml"


def test_the_systemd_unit_matches_the_shutdown_contract():
    """The unit's stop timeout has to outlast one item finishing."""
    from pathlib import Path

    unit = Path(__file__).resolve().parents[2] / "systemd" / "davpunk-sync.service"
    text = unit.read_text()
    assert "TimeoutStopSec=30" in text
    assert "KillSignal=SIGTERM" in text
    # The gpg-agent caveat is documented where the user will look for it.
    assert "gpg-agent.conf" in text
    assert "enable-linger" in text
    # stdout is not a log sink anywhere in DavPunk.
    assert "StandardOutput=null" in text


# ---------------------------------------------------------------- auto_sync


def _configs(**overrides):
    return DavPunkConfig(
        remotes=[
            RemoteConfig(id="work", url="https://a.test/dav/", sync_interval=30),
            RemoteConfig(id="home", url="https://b.test/dav/", sync_interval=30, **overrides),
        ]
    )


def test_a_remote_with_auto_sync_off_is_not_synced_on_the_timer(conn, monkeypatch):
    synced = []
    monkeypatch.setattr(SyncDaemon, "_sync_one", lambda _self, c: synced.append(c.id))

    SyncDaemon(_configs(auto_sync=False), conn, once=True).run()

    assert synced == ["work"]


def test_an_excluded_remote_is_still_reconciled_into_the_cache(conn, monkeypatch):
    """Excluded from the timer, not from the cache: its tasks are still yours."""
    monkeypatch.setattr(SyncDaemon, "_sync_one", lambda _self, _c: None)

    SyncDaemon(_configs(auto_sync=False), conn, once=True).run()

    stored = {r["id"] for r in conn.execute("SELECT id FROM remotes")}
    assert stored == {"work", "home"}


def test_a_daemon_where_nothing_auto_syncs_still_runs_for_the_alarms(conn, monkeypatch):
    """Exiting would silently stop alarm notifications too."""
    scanned = []
    monkeypatch.setattr(SyncDaemon, "_scan_alarms", lambda _self: scanned.append(1))
    monkeypatch.setattr(SyncDaemon, "_sync_one", lambda _self, _c: pytest.fail("must not sync"))

    config = DavPunkConfig(
        remotes=[RemoteConfig(id="work", url="https://a.test/dav/", auto_sync=False)]
    )
    assert SyncDaemon(config, conn, once=True).run() == 0
    assert scanned
