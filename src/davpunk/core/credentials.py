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
from dataclasses import dataclass, replace
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


#: gpg colon-listing validity codes that mean the key cannot be used.
UNUSABLE_VALIDITY = frozenset("ird")  # invalid, revoked, disabled


@dataclass(frozen=True)
class GpgKey:
    """One secret key or subkey, as the picker needs to describe it.

    Subkeys matter here rather than being an advanced detail: a keyring with
    **two** encryption subkeys is common (rotation, or one per device), and
    ``gpg --recipient <primary-id>`` then silently picks whichever it prefers.
    If that is not the subkey whose private half you actually hold on this
    machine, every decrypt fails and nothing says why.
    """

    key_id: str
    fingerprint: str
    uid: str
    capabilities: str
    is_subkey: bool
    created: int | None = None
    expires: int | None = None
    validity: str = ""
    #: Colon field 15.  ``#`` means gpg holds only a stub — the private half is
    #: elsewhere.  Any other value is a smartcard serial number.  Empty means an
    #: ordinary on-disk secret key.
    secret_token: str = ""

    @property
    def can_encrypt(self) -> bool:
        # Lowercase 'e' on this key itself; uppercase 'E' on a primary means
        # "some subkey of mine can", which is not the same thing.
        return "e" in self.capabilities

    @property
    def secret_available(self) -> bool:
        """Is the private half actually reachable from this machine?

        A keyring routinely contains subkeys whose secret material lives
        somewhere else — an offline backup, or another laptop.  gpg happily
        *encrypts* to those and then cannot decrypt, which is a silent and
        total failure: the credential file looks fine and every sync 401s.
        """
        return self.secret_token != "#"

    @property
    def on_smartcard(self) -> bool:
        return bool(self.secret_token) and self.secret_token != "#"

    @property
    def usable(self) -> bool:
        if self.validity in UNUSABLE_VALIDITY:
            return False
        if not self.secret_available:
            return False
        return not (self.expires and self.expires < time.time())

    @property
    def recipient(self) -> str:
        """What to pass to ``gpg --recipient``.

        The trailing ``!`` pins a specific subkey.  Without it gpg re-runs its
        own selection at encrypt time — and with more than one encryption
        subkey it may well choose one whose secret half you do not have.
        """
        return f"{self.key_id}!" if self.is_subkey else self.key_id

    @property
    def short_id(self) -> str:
        return self.key_id[-16:]

    def label(self) -> str:
        parts = [self.uid or self.short_id]
        if self.is_subkey:
            parts.append(f"key {self.short_id}")
        if self.created:
            parts.append(time.strftime("%Y-%m-%d", time.localtime(self.created)))
        if self.on_smartcard:
            parts.append("on a smartcard")
        return "  ·  ".join(parts)


def list_secret_keys() -> list[GpgKey]:
    """Every secret key and subkey in the keyring, primaries first."""
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
    return parse_colon_listing(result.stdout.decode("utf-8", "replace"))


def parse_colon_listing(text: str) -> list[GpgKey]:
    """Parse ``gpg --with-colons`` output into keys and their subkeys.

    Split out from the subprocess call so the parsing — which is where the
    subkey bug lived — is testable against captured fixtures.
    """
    keys: list[GpgKey] = []
    #: index into `keys` of the record a following `fpr` line describes
    awaiting_fingerprint: int | None = None
    #: indices belonging to the primary key currently being read.  gpg emits
    #: the uid *between* the primary and its subkeys, so it has to be
    #: back-filled onto what came before and carried forward onto what follows.
    current_group: list[int] = []
    current_uid = ""

    def field(fields: list[str], index: int) -> str:
        return fields[index] if len(fields) > index else ""

    def as_epoch(raw: str) -> int | None:
        try:
            return int(raw) or None
        except ValueError:
            return None  # gpg also emits ISO dates for very old keys

    for line in text.splitlines():
        fields = line.split(":")
        record = fields[0]

        if record in ("sec", "pub", "ssb", "sub"):
            is_subkey = record in ("ssb", "sub")
            if not is_subkey:
                current_group = []
                current_uid = ""
            keys.append(
                GpgKey(
                    key_id=field(fields, 4),
                    fingerprint="",
                    uid=current_uid,
                    capabilities=field(fields, 11),
                    is_subkey=is_subkey,
                    created=as_epoch(field(fields, 5)),
                    expires=as_epoch(field(fields, 6)),
                    validity=field(fields, 1),
                    secret_token=field(fields, 14),
                )
            )
            awaiting_fingerprint = len(keys) - 1
            current_group.append(len(keys) - 1)

        elif record == "fpr" and awaiting_fingerprint is not None:
            index = awaiting_fingerprint
            keys[index] = replace(keys[index], fingerprint=field(fields, 9))
            awaiting_fingerprint = None

        elif record == "uid" and current_group and not current_uid:
            # The first uid names the whole group, subkeys included.
            current_uid = field(fields, 9)
            for index in current_group:
                keys[index] = replace(keys[index], uid=current_uid)

    return keys


def encryption_options(keys: list[GpgKey] | None = None) -> list[GpgKey]:
    """Every encryption subkey this machine can actually decrypt with.

    Deliberately **only** subkeys, each pinned with ``!``.  Offering the
    primary key id — letting gpg choose, which survives a subkey rotation
    without re-encrypting — reads like the friendlier default and is a trap:
    with several encryption subkeys gpg picks by its own rules, and if that
    lands on one whose secret half is not here, the credential encrypts fine
    and can never be read back.

    Pinning costs one re-entry of the password after a rotation, and the
    preferences dialog makes that a two-click job.  A silently undecryptable
    credential costs an afternoon.
    """
    keys = list_secret_keys() if keys is None else keys
    return [
        key
        for key in keys
        if key.is_subkey and key.can_encrypt and key.usable and _primary_of(keys, key)
    ]


def _primary_of(keys: list[GpgKey], subkey: GpgKey) -> GpgKey | None:
    """The primary this subkey hangs off, if it is itself usable."""
    primary: GpgKey | None = None
    for key in keys:
        if not key.is_subkey:
            primary = key
        elif key is subkey:
            # A revoked or expired primary invalidates its subkeys, but a
            # primary kept offline (secret_token '#') is completely normal and
            # says nothing about whether the subkey can decrypt.
            if primary is None or primary.validity in UNUSABLE_VALIDITY:
                return None
            return primary
    return None


def unusable_encryption_keys(keys: list[GpgKey] | None = None) -> list[GpgKey]:
    """Encryption subkeys that exist but cannot be used from this machine.

    Surfaced by ``davpunk doctor`` and by the picker, because "my key is right
    there in the list, why is it not offered?" is otherwise a mystery.
    """
    keys = list_secret_keys() if keys is None else keys
    return [k for k in keys if k.is_subkey and k.can_encrypt and not k.usable]


def resolve_encryption_target(recipient: str) -> str | None:
    """Which key id gpg *actually* encrypts to for this recipient.

    There is no way to ask gpg this directly, and guessing its subkey-selection
    rules is exactly the mistake that makes an undecryptable credential.  So we
    encrypt a throwaway byte and read the recipient key id back out of the
    packet.  Encryption never prompts, so this is safe to run from ``doctor``.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "probe.gpg"
        try:
            encrypted = subprocess.run(
                [
                    GPG,
                    "--batch",
                    "--yes",
                    "--trust-model",
                    "always",
                    "--recipient",
                    recipient,
                    "--encrypt",
                    "-o",
                    str(target),
                ],
                input=b"x",
                capture_output=True,
                timeout=GPG_TIMEOUT,
                check=False,
            )
            if encrypted.returncode != 0:
                return None
            packets = subprocess.run(
                [GPG, "--batch", "--list-packets", str(target)],
                capture_output=True,
                timeout=GPG_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("Could not probe the encryption target: %s", exc)
            return None

    for line in packets.stdout.decode("utf-8", "replace").splitlines():
        if "keyid " in line:
            return line.split("keyid ", 1)[1].strip().removeprefix("0x").upper()
    return None


def describe_unusable(key: GpgKey) -> str:
    if not key.secret_available:
        return "its private half is not on this machine"
    if key.validity in UNUSABLE_VALIDITY:
        return {"r": "revoked", "e": "expired", "d": "disabled", "i": "invalid"}.get(
            key.validity, "unusable"
        )
    if key.expires and key.expires < time.time():
        return f"expired on {time.strftime('%Y-%m-%d', time.localtime(key.expires))}"
    return "unusable"


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
