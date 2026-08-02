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


KEYRING_CONFIG = """
[davpunk]
theme = "dark"

[[davpunk.remotes]]
id = "work"
name = "Work"
url = "https://cal.example.test/dav/"
username = "user"
credential_backend = "keyring"
"""


@pytest.fixture
def keyring_config(davpunk_home):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    path = paths.config_file()
    path.write_text(KEYRING_CONFIG)
    path.chmod(0o600)
    return path


def _keyring(monkeypatch, *, present=True, locked=False, stored=True):
    """Stand in for the Secret Service, so doctor is testable without one."""
    from davpunk.core import secret_store
    from davpunk.core.credentials import CredentialError, CredentialLocked

    monkeypatch.setattr(secret_store, "keyring_available", lambda: present)

    def _load(self):
        if not present:
            raise CredentialError("no Secret Service is available")
        if locked:
            raise CredentialLocked("the login keyring is locked")
        return "hunter2"

    monkeypatch.setattr(secret_store.KeyringStore, "exists", lambda self: present and stored)
    monkeypatch.setattr(secret_store.KeyringStore, "load", _load)


def test_doctor_does_not_fail_a_gnupg_free_setup(keyring_config, monkeypatch):
    """Nothing here is encrypted with gpg, so a missing gpg is not a problem
    the user has — reporting it as one is reporting a solved problem."""
    from davpunk.core import credentials

    _keyring(monkeypatch)
    monkeypatch.setattr(credentials, "gpg_available", lambda: False)

    checks = {c.name: c for c in doctor.run_all(keyring_config)}

    assert checks["gpg"].status is Status.PASS
    assert "not needed" in checks["gpg"].detail
    # And no credential *file* is looked for either.
    assert "credential[work]" not in checks


def test_doctor_reads_the_keyring_back(keyring_config, monkeypatch):
    _keyring(monkeypatch)
    checks = {c.name: c for c in doctor.run_all(keyring_config)}
    assert checks["keyring[work]"].status is Status.PASS


def test_doctor_treats_a_locked_keyring_as_wait_not_broken(keyring_config, monkeypatch):
    """The same answer a cold gpg-agent gets: the daemon is idle, not broken,
    and DavPunk will not raise a password prompt to fix it."""
    _keyring(monkeypatch, locked=True)
    checks = {c.name: c for c in doctor.run_all(keyring_config)}

    assert checks["keyring[work]"].status is Status.WARN
    assert "locked" in checks["keyring[work]"].detail


def test_doctor_fails_when_nothing_was_ever_stored(keyring_config, monkeypatch):
    """No amount of waiting produces a secret that is not there."""
    _keyring(monkeypatch, stored=False)
    checks = {c.name: c for c in doctor.run_all(keyring_config)}
    assert checks["keyring[work]"].status is Status.FAIL


def test_doctor_says_when_there_is_no_keyring_at_all(keyring_config, monkeypatch):
    _keyring(monkeypatch, present=False)
    checks = {c.name: c for c in doctor.run_all(keyring_config)}

    assert checks["keyring"].status is Status.FAIL
    assert "session bus" in checks["keyring"].detail


def test_doctor_says_nothing_about_the_keyring_when_nothing_uses_it(config_file, monkeypatch):
    checks = {c.name: c for c in doctor.run_all(config_file)}
    assert not [name for name in checks if name.startswith("keyring")]


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


def test_status_marks_an_account_that_only_syncs_on_demand(cli, capsys, config_file):
    """ "last sync: never" on a manual account is not a fault, and must not read
    like one."""
    config_file.write_text(CONFIG + "auto_sync = false\n")
    config_file.chmod(0o600)

    assert cli("status") == 0
    assert "manual only" in capsys.readouterr().out


# ------------------------------------------------------------------- forget


@pytest.fixture
def orphan(cli, config_file):
    """A remote in the cache that config.toml no longer mentions."""
    from davpunk import paths
    from davpunk.models.remote import Remote

    conn = cache.open_db(paths.database_file())
    cache.reconcile_remotes(
        [
            Remote(id="work", url="https://cal.example.test/dav/"),
            Remote(id="gone", url="https://g/"),
        ],
        conn,
    )
    # A second pass with only the configured remote is what marks it orphaned.
    cache.reconcile_remotes([Remote(id="work", url="https://cal.example.test/dav/")], conn)
    cache.close_db(conn)
    return "gone"


def test_forget_drops_an_orphaned_remote(cli, capsys, orphan):
    assert cli("forget", orphan, "--yes") == 0
    assert "Forgot 'gone'" in capsys.readouterr().out

    from davpunk import paths

    conn = cache.open_db(paths.database_file())
    try:
        assert [r["remote_id"] for r in cache.sync_status_rows(conn)] == ["work"]
    finally:
        cache.close_db(conn)


def test_forget_refuses_a_remote_that_is_still_configured(cli, capsys, orphan):
    """Purging it would only bring it back on the next start."""
    assert cli("forget", "work", "--yes") == 2
    assert "still in config.toml" in capsys.readouterr().err


def test_forget_names_the_orphans_when_the_id_is_wrong(cli, capsys, orphan):
    assert cli("forget", "typo", "--yes") == 2
    err = capsys.readouterr().err
    assert "no remote named 'typo'" in err
    assert "orphaned remotes: gone" in err


def test_forget_without_yes_requires_the_id_to_be_typed_back(cli, capsys, orphan, monkeypatch):
    """A y/n prompt is too easy to answer wrong for something irreversible."""
    monkeypatch.setattr("builtins.input", lambda *_a: "not-the-id")
    assert cli("forget", orphan) == 1
    assert "Cancelled." in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda *_a: orphan)
    assert cli("forget", orphan) == 0


def test_forget_says_how_many_tasks_it_would_destroy(cli, capsys, orphan, monkeypatch):
    """The count has to be in the prompt, not only in the result."""
    seen = []
    monkeypatch.setattr("builtins.input", lambda *_a: seen.append(1) or orphan)
    cli("forget", orphan)
    assert "no cached tasks" in capsys.readouterr().out
