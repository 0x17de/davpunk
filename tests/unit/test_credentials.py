"""Credential storage and decrypt.-

The gpg invocations are mocked at ``subprocess.run``.  What matters here is the
*shape* of the call — the password on stdin and never in ``argv``, ``--yes``,
``--trust-model always`` — plus the file modes and the atomic replace.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from davpunk import paths
from davpunk.core import credentials
from davpunk.core.credentials import (
    CredentialError,
    CredentialLocked,
    DecryptWarningLimiter,
    decrypt_credential,
    store_credential,
)


@pytest.fixture
def gpg_file(davpunk_home):
    paths.ensure_dir(paths.credentials_dir(), paths.CREDENTIALS_DIR_MODE)
    return paths.credential_file("work")


class FakeGpg:
    """Records the invocation and writes plausible ciphertext."""

    def __init__(self, *, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if "--encrypt" in argv and self.returncode == 0:
            out = argv[argv.index("-o") + 1]
            with open(out, "wb") as handle:
                handle.write(b"-----BEGIN PGP MESSAGE-----\n")
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)

    @property
    def last(self) -> dict:
        return self.calls[-1]


# --------------------------------------------------------------------- store


def test_store_writes_the_file_0600(monkeypatch, gpg_file):
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    store_credential("hunter2", gpg_file, "0xDEADBEEF")

    assert gpg_file.exists()
    assert gpg_file.stat().st_mode & 0o077 == 0


def test_the_credentials_directory_is_0700(monkeypatch, davpunk_home):
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    target = paths.credential_file("work")
    store_credential("hunter2", target, "0xDEADBEEF")
    assert target.parent.stat().st_mode & 0o077 == 0


def test_the_password_goes_to_stdin_and_never_to_argv(monkeypatch, gpg_file):
    fake = FakeGpg()
    monkeypatch.setattr(subprocess, "run", fake)
    store_credential("hunter2", gpg_file, "0xDEADBEEF")

    assert fake.last["input"] == b"hunter2"
    assert not any("hunter2" in str(arg) for arg in fake.last["argv"])


def test_batch_yes_and_trust_model_always_are_passed(monkeypatch, gpg_file):
    """--yes or --batch turns the overwrite prompt into an error; the trust
    model or a not-fully-trusted recipient key is refused."""
    fake = FakeGpg()
    monkeypatch.setattr(subprocess, "run", fake)
    store_credential("hunter2", gpg_file, "0xDEADBEEF")

    argv = fake.last["argv"]
    assert "--batch" in argv
    assert "--yes" in argv
    assert argv[argv.index("--trust-model") + 1] == "always"
    assert argv[argv.index("--recipient") + 1] == "0xDEADBEEF"


def test_overwriting_an_existing_credential_succeeds(monkeypatch, gpg_file):
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    store_credential("first", gpg_file, "0xKEY")
    store_credential("second", gpg_file, "0xKEY")
    assert gpg_file.exists()


def test_a_stale_temp_file_does_not_block_a_rewrite(monkeypatch, gpg_file):
    """An interrupted earlier run leaves the O_EXCL temp behind."""
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    stale = gpg_file.with_suffix(gpg_file.suffix + ".tmp")
    stale.write_text("truncated")
    store_credential("hunter2", gpg_file, "0xKEY")
    assert not stale.exists()


def test_the_write_is_atomic_via_a_same_directory_temp(monkeypatch, gpg_file):
    fake = FakeGpg()
    monkeypatch.setattr(subprocess, "run", fake)
    store_credential("hunter2", gpg_file, "0xKEY")

    written_to = fake.last["argv"][fake.last["argv"].index("-o") + 1]
    assert written_to != str(gpg_file)
    assert os.path.dirname(written_to) == str(gpg_file.parent)
    assert not os.path.exists(written_to)  # replaced, not left behind


def test_a_failed_encrypt_leaves_no_partial_credential(monkeypatch, gpg_file):
    monkeypatch.setattr(subprocess, "run", FakeGpg(returncode=2, stderr=b"no such key"))
    with pytest.raises(CredentialError, match="no such key"):
        store_credential("hunter2", gpg_file, "0xMISSING")

    assert not gpg_file.exists()
    assert not gpg_file.with_suffix(gpg_file.suffix + ".tmp").exists()


def test_a_failed_encrypt_does_not_destroy_the_previous_credential(monkeypatch, gpg_file):
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    store_credential("good", gpg_file, "0xKEY")
    before = gpg_file.read_bytes()

    monkeypatch.setattr(subprocess, "run", FakeGpg(returncode=2, stderr=b"boom"))
    with pytest.raises(CredentialError):
        store_credential("bad", gpg_file, "0xKEY")

    assert gpg_file.read_bytes() == before


def test_gpg_missing_is_reported_not_crashed(monkeypatch, gpg_file):
    def missing(*_a, **_k):
        raise FileNotFoundError("gpg")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(CredentialError, match="could not be run"):
        store_credential("hunter2", gpg_file, "0xKEY")


# ------------------------------------------------------------------- decrypt


def test_decrypt_returns_the_plaintext(monkeypatch, gpg_file):
    gpg_file.write_bytes(b"ciphertext")
    monkeypatch.setattr(subprocess, "run", FakeGpg(stdout=b"hunter2"))
    assert decrypt_credential(gpg_file) == "hunter2"


def test_decrypt_does_not_strip_trailing_whitespace(monkeypatch, gpg_file):
    """The regression T27 names.

    ``.rstrip("\\n")`` removes *all* trailing newlines, so a password that
    legitimately ends in whitespace came back corrupted and every request 401'd
    with no clue why.
    """
    gpg_file.write_bytes(b"ciphertext")
    monkeypatch.setattr(subprocess, "run", FakeGpg(stdout=b"trailing space \n\n"))
    assert decrypt_credential(gpg_file) == "trailing space \n\n"


def test_decrypt_preserves_a_password_that_is_only_whitespace(monkeypatch, gpg_file):
    gpg_file.write_bytes(b"ciphertext")
    monkeypatch.setattr(subprocess, "run", FakeGpg(stdout=b"   "))
    assert decrypt_credential(gpg_file) == "   "


def test_decrypt_handles_non_ascii(monkeypatch, gpg_file):
    gpg_file.write_bytes(b"ciphertext")
    monkeypatch.setattr(subprocess, "run", FakeGpg(stdout="pässwörd➜".encode()))
    assert decrypt_credential(gpg_file) == "pässwörd➜"


def test_a_cold_agent_raises_credential_locked(monkeypatch, gpg_file):
    """The daemon's normal failure mode: gpg-agent has forgotten the passphrase."""
    gpg_file.write_bytes(b"ciphertext")
    monkeypatch.setattr(
        subprocess, "run", FakeGpg(returncode=2, stderr=b"gpg: decryption failed: No secret key")
    )
    with pytest.raises(CredentialLocked):
        decrypt_credential(gpg_file)


def test_a_missing_credential_file_is_reported_clearly(davpunk_home):
    with pytest.raises(CredentialError, match="not found"):
        decrypt_credential(paths.credential_file("absent"))


def test_decrypt_never_puts_the_path_contents_in_argv(monkeypatch, gpg_file):
    fake = FakeGpg(stdout=b"pw")
    gpg_file.write_bytes(b"ciphertext")
    monkeypatch.setattr(subprocess, "run", fake)
    decrypt_credential(gpg_file)
    assert fake.last["argv"] == ["gpg", "--batch", "--decrypt", str(gpg_file)]


# ------------------------------------------------------------- rate limiting


def test_the_warning_limiter_fires_once_per_hour():
    limiter = DecryptWarningLimiter(interval=3600)
    assert limiter.should_warn("work", now=0.0) is True
    assert limiter.should_warn("work", now=60.0) is False
    assert limiter.should_warn("work", now=3599.0) is False
    assert limiter.should_warn("work", now=3601.0) is True


def test_the_warning_limiter_is_per_remote():
    limiter = DecryptWarningLimiter()
    assert limiter.should_warn("work", now=0.0) is True
    assert limiter.should_warn("personal", now=0.0) is True


def test_a_successful_decrypt_resets_the_limiter():
    limiter = DecryptWarningLimiter()
    limiter.should_warn("work", now=0.0)
    limiter.reset("work")
    assert limiter.should_warn("work", now=1.0) is True


# -------------------------------------------------------------------- modes


def test_check_modes_flags_a_loose_credential(monkeypatch, gpg_file):
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    store_credential("hunter2", gpg_file, "0xKEY")
    assert credentials.check_modes(gpg_file) == []

    os.chmod(gpg_file, 0o644)
    assert any("0600" in problem for problem in credentials.check_modes(gpg_file))

    credentials.fix_modes(gpg_file)
    assert credentials.check_modes(gpg_file) == []


def test_check_modes_flags_a_loose_directory(monkeypatch, gpg_file):
    monkeypatch.setattr(subprocess, "run", FakeGpg())
    store_credential("hunter2", gpg_file, "0xKEY")
    os.chmod(gpg_file.parent, 0o755)
    assert any("0700" in problem for problem in credentials.check_modes(gpg_file))
    credentials.fix_modes(gpg_file)
    assert credentials.check_modes(gpg_file) == []


# ------------------------------------------------------------------ keyring


def test_list_secret_keys_parses_colon_output(monkeypatch):
    colons = (
        b"sec:u:255:22:AAAA1111BBBB2222::::::::::\n"
        b"fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n"
        b"uid:u::::::::Ada Lovelace <ada@example.com>::::::::::0:\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=colons, stderr=b""),
    )
    assert credentials.list_secret_keys() == [
        ("0123456789ABCDEF0123456789ABCDEF01234567", "Ada Lovelace <ada@example.com>")
    ]


def test_list_secret_keys_is_empty_when_gpg_is_missing(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("gpg")

    monkeypatch.setattr(subprocess, "run", missing)
    assert credentials.list_secret_keys() == []
