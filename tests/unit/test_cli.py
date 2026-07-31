"""The CLI surface: dispatch, sync, status, conflicts, doctor."""

from __future__ import annotations

import pytest

from davpunk.__main__ import build_parser, main
from davpunk.cli import doctor
from davpunk.cli.doctor import Status
from davpunk.core import cache
from davpunk.core.ical_parser import new_resource
from davpunk.models.task import Task

CONFIG = """
[davpunk]
theme = "dark"

[[davpunk.remotes]]
id = "work"
name = "Work"
url = "https://cal.example.test/dav/"
username = "user"
"""


@pytest.fixture
def config_file(davpunk_home):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    path = paths.config_file()
    path.write_text(CONFIG)
    path.chmod(0o600)
    return path


@pytest.fixture
def cli(config_file, monkeypatch, davpunk_home):
    """Run ``davpunk ...`` against the relocated home."""

    def _run(*argv):
        return main(["--config", str(config_file), *argv])

    return _run


# ------------------------------------------------------------------ dispatch


def test_no_subcommand_means_the_ui():
    assert build_parser().parse_args([]).command is None


@pytest.mark.parametrize("command", ["sync", "status", "conflicts", "doctor"])
def test_every_documented_subcommand_parses(command):
    assert build_parser().parse_args([command]).command == command


def test_sync_takes_a_remote_filter():
    assert build_parser().parse_args(["sync", "--remote", "work"]).remote == "work"


def test_doctor_takes_fix():
    assert build_parser().parse_args(["doctor", "--fix"]).fix is True


def test_new_instance_is_available_for_the_ui():
    assert build_parser().parse_args(["--new-instance"]).new_instance is True


def test_the_ui_absence_is_reported_not_traced_back(monkeypatch, capsys, config_file):
    """The CLI subcommands must work on a machine with no Qt at all."""
    import builtins

    real_import = builtins.__import__

    def no_pyside(name, *args, **kwargs):
        if name.startswith("davpunk.ui"):
            raise ImportError("No module named 'PySide6'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyside)
    assert main(["--config", str(config_file)]) == 2
    assert "PySide6" in capsys.readouterr().err


# ---------------------------------------------------------------------- sync


def test_sync_with_an_unknown_remote_is_an_error(cli, capsys):
    assert cli("sync", "--remote", "nope") == 2
    assert "no remote named" in capsys.readouterr().err


def test_sync_reports_a_credential_failure_without_crashing(cli, capsys):
    """No credential file exists, so every remote is skipped — not a crash."""
    assert cli("sync") == 0
    assert "work" in capsys.readouterr().out


def test_sync_with_no_remotes_configured_is_an_error(davpunk_home, capsys):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    empty = paths.config_dir() / "empty.toml"
    empty.write_text("[davpunk]\n")
    assert main(["--config", str(empty), "sync"]) == 2
    assert "no remotes configured" in capsys.readouterr().err


def test_an_unparseable_config_is_reported_verbatim(davpunk_home, capsys):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    broken = paths.config_dir() / "broken.toml"
    broken.write_text("[davpunk\nnot toml")
    assert main(["--config", str(broken), "sync"]) == 2
    assert "broken.toml" in capsys.readouterr().err


# -------------------------------------------------------------------- status


def test_status_lists_each_remote(cli, capsys):
    assert cli("status") == 0
    out = capsys.readouterr().out
    assert "work (Work)" in out
    assert "last sync : never" in out
    assert "pending" in out


def test_status_counts_pending_blocked_and_conflicts(cli, capsys, davpunk_home):
    from davpunk import paths
    from davpunk.models.calendar import calendar_id
    from davpunk.models.remote import Remote

    conn = cache.open_db(paths.database_file())
    cache.reconcile_remotes([Remote(id="work", url="https://cal.example.test/dav/")], conn)
    with cache.tx(conn):
        cal = cache.upsert_calendar("work", "/dav/tasks/", conn)
    task = Task(uid="t1", calendar_id=cal, href="t1.ics", summary="s")
    task.raw_ics = new_resource(task)
    with cache.tx(conn):
        task_id = cache.insert_server_task(task, 'W/"e"', conn)
    cache.update_task_optimistic(task_id, {"summary": "edited"}, conn)
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "l", "r", None, conn)
    cache.close_db(conn)

    assert cli("status") == 0
    out = capsys.readouterr().out
    assert "pending   : 1" in out
    assert "conflicts: 1" in out
    assert calendar_id("work", "/dav/tasks/") not in out  # ids are not UI


def test_status_shows_a_syncing_remote(cli, capsys):
    """Whether a sync is running is a lock probe, not a database column."""
    from davpunk.core.locking import remote_sync_lock

    cli("status")  # create the remote row
    with remote_sync_lock("work"):
        cli("status")
    assert "syncing" in capsys.readouterr().out


# ----------------------------------------------------------------- conflicts


def test_conflicts_is_empty_by_default(cli, capsys):
    assert cli("conflicts") == 0
    assert "No open conflicts" in capsys.readouterr().out


def test_conflicts_names_the_dialog_mode(cli, capsys, davpunk_home):
    from davpunk import paths
    from davpunk.models.remote import Remote

    conn = cache.open_db(paths.database_file())
    cache.reconcile_remotes([Remote(id="work", url="https://cal.example.test/dav/")], conn)
    with cache.tx(conn):
        cal = cache.upsert_calendar("work", "/dav/tasks/", conn)
    task = Task(uid="t1", calendar_id=cal, href="t1.ics", summary="Fix login bug")
    with cache.tx(conn):
        task_id = cache.insert_server_task(task, 'W/"e"', conn)
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "local", "remote", 'W/"e2"', conn)
    cache.close_db(conn)

    assert cli("conflicts") == 0
    out = capsys.readouterr().out
    assert "Fix login bug" in out
    assert "both sides changed" in out
    assert "Resolve them in the DavPunk UI" in out


# -------------------------------------------------------------------- doctor


def test_doctor_reports_each_check_individually(cli, capsys):
    """One failure must not hide the rest."""
    cli("doctor")
    out = capsys.readouterr().out
    for name in ("python", "sqlite-version", "sqlite-fts5", "tzdata", "config"):
        assert name in out


def test_doctor_passes_the_runtime_floors(config_file):
    checks = {c.name: c for c in doctor.run_all(config_file)}
    for name in ("python", "sqlite-version", "sqlite-fts5", "tzdata"):
        assert checks[name].status is Status.PASS


def test_doctor_flags_a_world_readable_config(config_file):
    config_file.chmod(0o644)
    checks = {c.name: c for c in doctor.run_all(config_file)}
    assert checks["config-mode"].status is Status.FAIL
    assert checks["config-mode"].fixer is not None


def test_doctor_fix_repairs_file_modes(cli, config_file, capsys):
    config_file.chmod(0o644)
    cli("doctor", "--fix")
    assert config_file.stat().st_mode & 0o077 == 0


def test_doctor_fix_only_touches_modes(config_file):
    """--fix repairs file modes only.  Nothing else is safe to do blind."""
    checks = doctor.run_all(config_file)
    fixable = [c.name for c in checks if c.fixer is not None]
    assert all("mode" in name or name == "fts-integrity" for name in fixable)


def test_doctor_exits_non_zero_on_a_failure(cli, config_file, capsys):
    config_file.chmod(0o644)
    assert cli("doctor") == 1
    assert "FAILED" in capsys.readouterr().out


def test_doctor_reports_a_missing_credential(config_file):
    checks = {c.name: c for c in doctor.run_all(config_file)}
    assert checks["credential[work]"].status is Status.FAIL


def test_doctor_warns_rather_than_failing_on_an_absent_daemon(config_file):
    checks = {c.name: c for c in doctor.run_all(config_file)}
    systemd = checks.get("systemd-unit") or checks.get("systemd")
    assert systemd.status is not Status.FAIL


def test_doctor_survives_an_unparseable_config(davpunk_home):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    broken = paths.config_dir() / "broken.toml"
    broken.write_text("[davpunk\nnope")

    checks = {c.name: c for c in doctor.run_all(broken)}
    assert checks["config"].status is Status.FAIL
    # The runtime checks still ran and reported.
    assert checks["python"].status is Status.PASS


def test_dry_run_reports_without_touching_anything(cli, capsys, monkeypatch, davpunk_home):
    """``sync --dry-run`` must not fall through to a real cycle."""
    from davpunk.core import dry_run, sync_runner

    def refuse(*_args, **_kwargs):
        raise AssertionError("a dry run must never construct a real SyncRunner cycle")

    monkeypatch.setattr(sync_runner.SyncRunner, "_cycle", refuse)
    monkeypatch.setattr(
        dry_run,
        "plan",
        lambda remote, _db, _cancel=None, **_kw: dry_run.DryRunReport(
            remote_id=remote.id,
            result=sync_runner.SyncResult(remote_id=remote.id, calendars=1),
            writes=[dry_run.PlannedWrite(verb="CREATE", href="/a.ics", summary="Planned")],
        ),
    )

    assert cli("sync", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "CREATE Planned" in out
    assert "nothing was written" in out
    assert "without --dry-run" in out


def test_the_parser_offers_dry_run_and_defaults_to_off():
    assert build_parser().parse_args(["sync"]).dry_run is False
    assert build_parser().parse_args(["sync", "--dry-run"]).dry_run is True
