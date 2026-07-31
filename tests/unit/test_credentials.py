"""Credential storage and decrypt.-

The gpg invocations are mocked at ``subprocess.run``.  What matters here is the
*shape* of the call — the password on stdin and never in ``argv``, ``--yes``,
``--trust-model always`` — plus the file modes and the atomic replace.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
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


#: A real keyring shape: one primary with two encryption subkeys, only one of
#: which has its private half on this machine.  Captured from the case that
#: prompted this code, with the identity anonymised.
TWO_ENCRYPTION_SUBKEYS = """\
sec:u:4096:1:1A2B3C4D5E6F0001:1700951442:1795559442:::::cESCA:::#::23:
fpr:::::::::9F8E7D6C5B4A3928170615041A2B3C4D5E6F0001:
uid:u::::1700951442::ABC::Ada Lovelace <ada@example.com>::::::::::0:
uid:u::::1700951442::DEF::Ada Lovelace <ada@example.org>::::::::::0:
ssb:u:4096:1:1A2B3C4D5E6F0002:1700951442:1795559442:::::s:::D2760001240100000006000000000000::23:
fpr:::::::::4C3D2E1F0A9B8C7D6E5F40311A2B3C4D5E6F0002:
ssb:u:4096:1:1A2B3C4D5E6F0003:1700951442:1795559442:::::e:::D2760001240100000006000000000000::23:
fpr:::::::::7B6A594837261504F3E2D1C01A2B3C4D5E6F0003:
ssb:u:4096:1:1A2B3C4D5E6F0004:1739483354:1865627354:::::s:::#::23:
fpr:::::::::2D1C0B9A88776655443322111A2B3C4D5E6F0004:
ssb:u:4096:1:1A2B3C4D5E6F0005:1739483354:1865627354:::::e:::#::23:
fpr:::::::::6E5D4C3B2A1908F7E6D5C4B31A2B3C4D5E6F0005:
"""


def keys():
    return credentials.parse_colon_listing(TWO_ENCRYPTION_SUBKEYS)


# --------------------------------------------------------------- key parsing


def test_parsing_finds_the_primary_and_every_subkey():
    parsed = keys()
    assert [k.key_id for k in parsed] == [
        "1A2B3C4D5E6F0001",
        "1A2B3C4D5E6F0002",
        "1A2B3C4D5E6F0003",
        "1A2B3C4D5E6F0004",
        "1A2B3C4D5E6F0005",
    ]
    assert [k.is_subkey for k in parsed] == [False, True, True, True, True]


def test_the_uid_is_carried_down_to_the_subkeys():
    """gpg emits uid records after the primary, so they have to be back-filled."""
    assert all(k.uid == "Ada Lovelace <ada@example.com>" for k in keys())


def test_fingerprints_attach_to_the_right_key():
    by_id = {k.key_id: k for k in keys()}
    assert by_id["1A2B3C4D5E6F0003"].fingerprint.endswith("1A2B3C4D5E6F0003")
    assert by_id["1A2B3C4D5E6F0005"].fingerprint.endswith("1A2B3C4D5E6F0005")


def test_capabilities_are_read_per_key():
    by_id = {k.key_id: k for k in keys()}
    assert by_id["1A2B3C4D5E6F0003"].can_encrypt
    assert not by_id["1A2B3C4D5E6F0002"].can_encrypt  # signing subkey


def test_an_uppercase_capability_on_the_primary_is_not_an_encryption_key():
    """'E' on a primary means "a subkey of mine can", which is a different
    claim from "I can", and conflating them is how a primary ends up offered
    as if it were an encryption key."""
    primary = keys()[0]
    assert "E" in primary.capabilities
    assert not primary.can_encrypt


# ---------------------------------------------------- secret key availability


def test_a_hash_in_field_15_means_the_private_half_is_elsewhere():
    """The whole point: gpg encrypts to such a key happily and then cannot
    decrypt, which fails silently and totally."""
    by_id = {k.key_id: k for k in keys()}
    assert by_id["1A2B3C4D5E6F0005"].secret_available is False
    assert by_id["1A2B3C4D5E6F0005"].usable is False


def test_a_serial_number_in_field_15_means_a_smartcard():
    by_id = {k.key_id: k for k in keys()}
    key = by_id["1A2B3C4D5E6F0003"]
    assert key.secret_available is True
    assert key.on_smartcard is True
    assert key.usable is True


def test_an_offline_primary_does_not_disqualify_its_subkeys():
    """Keeping the primary offline is standard practice, not a problem."""
    primary = keys()[0]
    assert primary.secret_available is False
    assert credentials.encryption_options(keys())


@pytest.mark.parametrize("validity", ["r", "d", "i"])
def test_revoked_disabled_and_invalid_keys_are_unusable(validity):
    listing = TWO_ENCRYPTION_SUBKEYS.replace(
        "ssb:u:4096:1:1A2B3C4D5E6F0003", f"ssb:{validity}:4096:1:1A2B3C4D5E6F0003"
    )
    by_id = {k.key_id: k for k in credentials.parse_colon_listing(listing)}
    assert by_id["1A2B3C4D5E6F0003"].usable is False


def test_an_expired_subkey_is_unusable():
    listing = TWO_ENCRYPTION_SUBKEYS.replace(":1700951442:1795559442:::::e:", ":1000:2000:::::e:")
    by_id = {k.key_id: k for k in credentials.parse_colon_listing(listing)}
    assert by_id["1A2B3C4D5E6F0003"].usable is False


# ------------------------------------------------------------ what is offered


def test_only_usable_encryption_subkeys_are_offered():
    """The regression this exists for: two encryption subkeys, one of them
    undecryptable, and the picker previously showed neither."""
    offered = credentials.encryption_options(keys())
    assert [k.key_id for k in offered] == ["1A2B3C4D5E6F0003"]


def test_the_offered_key_is_pinned_with_a_bang():
    """Without the ! gpg re-runs its own selection at encrypt time and can
    land on the subkey whose secret half is missing."""
    assert credentials.encryption_options(keys())[0].recipient == "1A2B3C4D5E6F0003!"


def test_the_primary_key_is_never_offered():
    """Offering it means letting gpg choose, and gpg chooses wrong here."""
    assert all(k.is_subkey for k in credentials.encryption_options(keys()))


def test_signing_and_auth_subkeys_are_not_offered():
    offered = {k.key_id for k in credentials.encryption_options(keys())}
    assert "1A2B3C4D5E6F0002" not in offered  # signing
    assert "1A2B3C4D5E6F0004" not in offered


def test_the_skipped_key_is_reported_with_a_reason():
    """ "It is right there in my keyring, why can I not pick it?\""""
    skipped = credentials.unusable_encryption_keys(keys())
    assert [k.key_id for k in skipped] == ["1A2B3C4D5E6F0005"]
    assert "not on this machine" in credentials.describe_unusable(skipped[0])


def test_a_revoked_primary_disqualifies_its_subkeys():
    listing = TWO_ENCRYPTION_SUBKEYS.replace("sec:u:", "sec:r:")
    assert credentials.encryption_options(credentials.parse_colon_listing(listing)) == []


def test_the_label_names_the_key_and_where_it_lives():
    label = credentials.encryption_options(keys())[0].label()
    assert "1A2B3C4D5E6F0003" in label
    assert "smartcard" in label


def test_an_empty_keyring_offers_nothing():
    assert credentials.encryption_options([]) == []
    assert credentials.parse_colon_listing("") == []


def test_malformed_lines_do_not_crash_the_parser():
    assert credentials.parse_colon_listing("garbage\n::\nsec\n") is not None


def test_list_secret_keys_shells_out_and_parses(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=TWO_ENCRYPTION_SUBKEYS.encode(), stderr=b""
        ),
    )
    assert [k.key_id for k in credentials.list_secret_keys()][:2] == [
        "1A2B3C4D5E6F0001",
        "1A2B3C4D5E6F0002",
    ]


def test_list_secret_keys_is_empty_when_gpg_is_missing(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("gpg")

    monkeypatch.setattr(subprocess, "run", missing)
    assert credentials.list_secret_keys() == []


# ------------------------------------------------------- resolving the target


def test_the_encryption_target_is_read_from_the_packet(monkeypatch):
    """gpg cannot be asked which subkey it would choose, so a throwaway
    encryption is used to find out rather than reimplementing its rules."""
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if "--encrypt" in argv:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(
            returncode=0,
            stdout=b":pubkey enc packet: version 3, algo 1, keyid 1A2B3C4D5E6F0005\n",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake)
    assert credentials.resolve_encryption_target("1A2B3C4D5E6F0001") == "1A2B3C4D5E6F0005"
    assert any("--encrypt" in argv for argv in calls)
    assert any("--list-packets" in argv for argv in calls)


def test_a_hex_prefixed_keyid_is_normalised(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **k: SimpleNamespace(
            returncode=0,
            stdout=(b"" if "--encrypt" in argv else b"keyid 0x1a2b3c4d5e6f0003\n"),
            stderr=b"",
        ),
    )
    assert credentials.resolve_encryption_target("x") == "1A2B3C4D5E6F0003"


def test_a_failed_probe_returns_none_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=2, stdout=b"", stderr=b"no such key"),
    )
    assert credentials.resolve_encryption_target("nope") is None


def test_the_probe_never_writes_the_plaintext_anywhere_lasting(monkeypatch, tmp_path):
    """It encrypts a throwaway byte, not a credential, and into a temp dir."""
    seen = {}

    def fake(argv, **kwargs):
        if "--encrypt" in argv:
            seen["input"] = kwargs.get("input")
            seen["out"] = argv[argv.index("-o") + 1]
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"keyid AAAA\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake)
    credentials.resolve_encryption_target("x")
    assert seen["input"] == b"x"
    assert not Path(seen["out"]).exists()  # the temp dir is gone
