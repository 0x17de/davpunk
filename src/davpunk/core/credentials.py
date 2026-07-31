"""GnuPG-encrypted per-remote credentials.-

``python-gnupg`` is deliberately not used: some of its paths stage data through
temporary files, which is exactly what this design avoids.  Every call here goes
through ``subprocess`` directly, and GnuPG is a runtime binary dependency.

> The plaintext credential is never written to disk, never appears in ``argv``,
> never enters shell history, and is never logged or stored in SQLite.  It is
> held in process memory for the duration of one request.  Python cannot
> guarantee prompt zeroing of string memory; treat process memory as in scope
> for an attacker who already has code execution as this user.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from davpunk import paths

log = logging.getLogger("davpunk.core.credentials")

GPG = "gpg"
GPG_TIMEOUT = 30
#: Once per hour per remote, so a cold gpg-agent does not flood the log.
WARN_INTERVAL = 3600


class CredentialError(Exception):
    """Encryption or decryption failed."""


class CredentialLocked(CredentialError):
    """gpg could not decrypt — most often a cold ``gpg-agent``."""


def gpg_available() -> bool:
    try:
        result = subprocess.run(
            [GPG, "--version"], capture_output=True, timeout=GPG_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def list_secret_keys() -> list[tuple[str, str]]:
    """``(key_id, uid)`` pairs for the first-run key picker."""
    try:
        result = subprocess.run(
            [GPG, "--batch", "--list-secret-keys", "--with-colons"],
            capture_output=True,
            timeout=GPG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not list GPG secret keys: %s", exc)
        return []
    if result.returncode != 0:
        return []

    keys: list[tuple[str, str]] = []
    pending: str | None = None
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split(":")
        if fields[0] == "fpr" and pending is None:
            pending = fields[9]
        elif fields[0] == "uid" and pending is not None:
            keys.append((pending, fields[9]))
            pending = None
    return keys


def store_credential(password: str, gpg_file: Path, gpg_key_id: str) -> None:
    """Encrypt ``password`` to ``gpg_file``, atomically and owner-only.

    A temp file in the *same directory*, created ``O_EXCL`` 0600, then
    ``os.replace`` — an interrupted run never leaves a truncated credential, and
    the file is never briefly world-readable.
    """
    gpg_file = Path(gpg_file).expanduser()
    paths.ensure_dir(gpg_file.parent, paths.CREDENTIALS_DIR_MODE)

    tmp = gpg_file.with_suffix(gpg_file.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, paths.CREDENTIAL_MODE)
    os.close(fd)

    try:
        result = subprocess.run(
            [
                GPG,
                "--batch",
                # --yes is required, or --batch turns the overwrite prompt into
                # an error; gpg created nothing here but writes to an existing
                # path when the wizard is re-run.
                "--yes",
                # Required for a recipient key that is not fully trusted, which
                # is the normal case for a key the user made for this purpose.
                "--trust-model",
                "always",
                "--recipient",
                gpg_key_id,
                "--encrypt",
                "-o",
                str(tmp),
            ],
            input=password.encode("utf-8"),  # stdin, never argv
            capture_output=True,
            timeout=GPG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        tmp.unlink(missing_ok=True)
        raise CredentialError(f"gpg could not be run: {exc}") from exc

    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise CredentialError(
            f"gpg --encrypt failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )

    os.chmod(tmp, paths.CREDENTIAL_MODE)
    os.replace(tmp, gpg_file)
    log.info("Wrote credential %s", gpg_file)


def decrypt_credential(gpg_file: Path) -> str:
    """Return the plaintext credential.

    **No stripping.**  An earlier ``.rstrip("\\n")`` removed *all* trailing
    newlines and so corrupted any password ending in whitespace.  The wizard
    adds no newline, so there is nothing to strip; a hand-encrypted credential
    made with ``echo`` carries one, which is why the docs recommend
    ``printf %s``.
    """
    gpg_file = Path(gpg_file).expanduser()
    if not gpg_file.exists():
        raise CredentialError(f"credential file not found: {gpg_file}")

    try:
        result = subprocess.run(
            [GPG, "--batch", "--decrypt", str(gpg_file)],
            capture_output=True,
            timeout=GPG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialError(f"gpg could not be run: {exc}") from exc

    if result.returncode != 0:
        # stderr may name the key but never the plaintext.
        raise CredentialLocked(
            f"gpg --decrypt failed for {gpg_file.name}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout.decode("utf-8")


class DecryptWarningLimiter:
    """Rate-limits the "credentials locked" warning to once an hour.

    The daemon runs on a short interval; without this, a cold ``gpg-agent``
    would fill the journal with an identical line every cycle.
    """

    def __init__(self, interval: int = WARN_INTERVAL) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    def should_warn(self, remote_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        previous = self._last.get(remote_id)
        if previous is not None and now - previous < self.interval:
            return False
        self._last[remote_id] = now
        return True

    def reset(self, remote_id: str) -> None:
        self._last.pop(remote_id, None)


def check_modes(gpg_file: Path) -> list[str]:
    """Problems with the on-disk permissions, as human-readable strings."""
    gpg_file = Path(gpg_file).expanduser()
    problems: list[str] = []
    directory = gpg_file.parent
    if directory.exists() and (directory.stat().st_mode & 0o077):
        problems.append(f"{directory} is not 0700")
    if gpg_file.exists() and (gpg_file.stat().st_mode & 0o077):
        problems.append(f"{gpg_file} is not 0600")
    return problems


def fix_modes(gpg_file: Path) -> None:
    gpg_file = Path(gpg_file).expanduser()
    if gpg_file.parent.exists():
        os.chmod(gpg_file.parent, paths.CREDENTIALS_DIR_MODE)
    if gpg_file.exists():
        os.chmod(gpg_file, paths.CREDENTIAL_MODE)
