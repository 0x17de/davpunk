"""``davpunk doctor`` — every runtime assumption, checked one at a time.

Each check returns a :class:`Check` rather than raising, so one failure never
hides the rest: a user with a cold gpg-agent *and* a loose file mode should see
both on the first run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from davpunk import paths, preflight
from davpunk.config import ConfigError, DavPunkConfig, gpg_key_present, load_config
from davpunk.core import cache, credentials


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    #: Set when ``--fix`` can repair this automatically.
    fixer: Callable[[], None] | None = None

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


def run_all(config_path: Path | str | None = None) -> list[Check]:
    return list(_iter_checks(Path(config_path).expanduser() if config_path else None))


def _iter_checks(config_path: Path | None) -> Iterator[Check]:
    yield from _runtime_checks()

    config, config_check = _load(config_path)
    yield config_check
    if config is None:
        return

    yield from _file_mode_checks(config, config_path)
    yield from _gpg_checks(config)
    yield from _database_checks()
    yield _notification_check()
    yield from _systemd_checks()


# ------------------------------------------------------------------ runtime


def _runtime_checks() -> Iterator[Check]:
    for label, fn in preflight.checks():
        try:
            fn()
        except preflight.PreflightError as exc:
            yield Check(label, Status.FAIL, str(exc))
        else:
            yield Check(label, Status.PASS, _runtime_detail(label))


def _runtime_detail(label: str) -> str:
    if label == "python":
        return sys.version.split()[0]
    if label in ("sqlite-version", "sqlite-fts5"):
        return sqlite3.sqlite_version
    return ""


# ------------------------------------------------------------------- config


def _load(config_path: Path | None) -> tuple[DavPunkConfig | None, Check]:
    path = config_path or paths.config_file()
    try:
        config = load_config(path)
    except ConfigError as exc:
        return None, Check("config", Status.FAIL, str(exc))

    if not config.remotes:
        return config, Check("config", Status.WARN, f"{path}: no remotes configured")
    return config, Check("config", Status.PASS, f"{path}: {len(config.remotes)} remote(s)")


# -------------------------------------------------------------- file modes


def _file_mode_checks(config: DavPunkConfig, config_path: Path | None) -> Iterator[Check]:
    path = config_path or paths.config_file()
    if path.exists():
        yield _mode_check("config-mode", path, 0o600)

    directory = paths.credentials_dir()
    if directory.exists():
        yield _mode_check("credentials-dir-mode", directory, 0o700)

    for remote in config.remotes:
        gpg_file = remote.resolved_gpg_file()
        if gpg_file.exists():
            yield _mode_check(f"credential-mode[{remote.id}]", gpg_file, 0o600)
        else:
            yield Check(
                f"credential[{remote.id}]",
                Status.FAIL,
                f"{gpg_file} does not exist; run the first-run wizard",
            )

    token = config.mcp.token_path()
    if config.mcp.enabled and token.exists():
        yield _mode_check("mcp-token-mode", token, 0o600)


def _mode_check(name: str, path: Path, want: int) -> Check:
    mode = path.stat().st_mode & 0o777
    if mode & ~want:
        return Check(
            name,
            Status.FAIL,
            f"{path} is {mode:04o}, want {want:04o}",
            fixer=lambda: os.chmod(path, want),
        )
    return Check(name, Status.PASS, f"{path} is {mode:04o}")


# ----------------------------------------------------------------------- gpg


def _gpg_checks(config: DavPunkConfig) -> Iterator[Check]:
    if not credentials.gpg_available():
        yield Check("gpg", Status.FAIL, "gpg is not on PATH; DavPunk cannot read credentials")
        return
    yield Check("gpg", Status.PASS, shutil.which("gpg") or "gpg")

    for remote in config.remotes:
        if remote.gpg_key_id:
            present = gpg_key_present(remote.gpg_key_id)
            yield Check(
                f"gpg-key[{remote.id}]",
                Status.PASS if present else Status.FAIL,
                f"{remote.gpg_key_id} {'is in' if present else 'is NOT in'} the keyring",
            )

        gpg_file = remote.resolved_gpg_file()
        if not gpg_file.exists():
            continue
        try:
            credentials.decrypt_credential(gpg_file)
        except credentials.CredentialLocked:
            # Not a FAIL: the daemon is simply idle until the agent is warmed.
            yield Check(
                f"gpg-decrypt[{remote.id}]",
                Status.WARN,
                "gpg-agent cannot decrypt right now (cold cache?); consider "
                "default-cache-ttl / max-cache-ttl in ~/.gnupg/gpg-agent.conf",
            )
        except credentials.CredentialError as exc:
            yield Check(f"gpg-decrypt[{remote.id}]", Status.FAIL, str(exc))
        else:
            yield Check(f"gpg-decrypt[{remote.id}]", Status.PASS, "credential decrypts")


# ------------------------------------------------------------------ database


def _database_checks() -> Iterator[Check]:
    path = paths.database_file()
    if not path.exists():
        yield Check("database", Status.WARN, f"{path} does not exist yet")
        return

    try:
        conn = cache.connect(path)
    except sqlite3.DatabaseError as exc:
        yield Check("database", Status.FAIL, f"{path} cannot be opened: {exc}")
        return

    try:
        # Too slow for every startup, which is exactly why it lives here.
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        yield Check(
            "integrity-check",
            Status.PASS if integrity == "ok" else Status.FAIL,
            integrity,
        )

        try:
            conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('integrity-check')")
            yield Check("fts-integrity", Status.PASS, "ok")
        except sqlite3.DatabaseError as exc:
            yield Check(
                "fts-integrity",
                Status.FAIL,
                f"{exc}; the index can be rebuilt with davpunk doctor --fix",
                fixer=lambda: cache.fts_rebuild(cache.connect(path)),
            )

        have = conn.execute("PRAGMA user_version").fetchone()[0]
        if have > cache.SCHEMA_VERSION:
            yield Check(
                "schema-version",
                Status.FAIL,
                f"database is at v{have}, this build understands v{cache.SCHEMA_VERSION}",
            )
        elif have < cache.SCHEMA_VERSION:
            yield Check(
                "schema-version",
                Status.WARN,
                f"database is at v{have}; it will migrate to v{cache.SCHEMA_VERSION} on next start",
            )
        else:
            yield Check("schema-version", Status.PASS, f"v{have}")
    finally:
        conn.close()


# ------------------------------------------------------------- notifications


def _notification_check() -> Check:
    from davpunk.notifications.dbus_notify import service_available

    if service_available():
        return Check("notifications", Status.PASS, "org.freedesktop.Notifications is reachable")
    return Check(
        "notifications",
        Status.WARN,
        "no notification service; alarms will be logged rather than shown",
    )


# ------------------------------------------------------------------- systemd


def _systemd_checks() -> Iterator[Check]:
    if shutil.which("systemctl") is None:
        yield Check("systemd", Status.WARN, "systemctl not found; the daemon unit is optional")
        return

    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "davpunk-sync.service"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        yield Check("systemd", Status.WARN, str(exc))
        return

    state = result.stdout.strip() or result.stderr.strip()
    if result.returncode == 0:
        yield Check("systemd-unit", Status.PASS, f"davpunk-sync.service is {state}")
    else:
        yield Check(
            "systemd-unit",
            Status.WARN,
            f"davpunk-sync.service is {state or 'not installed'} "
            "(the daemon is optional; the UI syncs on its own)",
        )


# --------------------------------------------------------------------- fixes


def apply_fixes(checks: list[Check]) -> list[str]:
    """``--fix`` repairs file modes only.  Nothing else is safe to do blind."""
    fixed: list[str] = []
    for check in checks:
        if check.fixer is None:
            continue
        try:
            check.fixer()
        except OSError as exc:
            fixed.append(f"{check.name}: could not fix ({exc})")
        else:
            fixed.append(f"{check.name}: fixed")
    return fixed
