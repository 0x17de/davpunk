"""The backend seam: GnuPG or the desktop's login keyring.

The keyring tests never touch D-Bus.  ``_open_collection`` is the one function
that does, so a fake collection goes in there and everything above it — the
attribute schema, the locked/absent distinction, the promise never to unlock —
is testable on a machine with no Secret Service at all.
"""

from __future__ import annotations

import pytest

from davpunk.core import secret_store
from davpunk.core.credentials import CredentialError, CredentialLocked
from davpunk.models.remote import Remote


class FakeItem:
    def __init__(self, label, attributes, secret):
        self.label = label
        self.attributes = attributes
        self.secret = secret
        self.deleted = False

    def get_secret(self):
        return self.secret

    def delete(self):
        self.deleted = True


class FakeCollection:
    """Enough Secret Service to be wrong in the ways that matter."""

    def __init__(self, locked=False):
        self.items: list[FakeItem] = []
        self.locked = locked
        self.unlocked_by_us = False

    def is_locked(self):
        return self.locked

    def unlock(self):
        self.unlocked_by_us = True
        self.locked = False

    def search_items(self, attributes):
        return [i for i in self.items if not i.deleted and i.attributes == attributes]

    def create_item(self, label, attributes, secret, replace=False):
        if replace:
            for item in self.search_items(attributes):
                item.deleted = True
        item = FakeItem(label, attributes, secret)
        self.items.append(item)
        return item


@pytest.fixture
def collection(monkeypatch):
    fake = FakeCollection()

    class _Connection:
        def close(self):
            self.closed = True

    monkeypatch.setattr(secret_store, "_open_collection", lambda: (_Connection(), fake))
    return fake


@pytest.fixture
def no_service(monkeypatch):
    def _boom():
        raise CredentialError("no Secret Service is available: nothing on the bus")

    monkeypatch.setattr(secret_store, "_open_collection", _boom)


def store(key="work"):
    return secret_store.KeyringStore(secret_store.CALDAV_PASSWORD, key, "DavPunk — Work")


# ------------------------------------------------------------------- keyring


def test_a_secret_survives_the_round_trip(collection):
    store().store("hunter2")
    assert store().load() == "hunter2"


def test_the_secret_is_not_mangled_on_the_way_through(collection):
    """The GnuPG backend learned this the hard way: a password ending in
    whitespace is a password, and stripping it corrupts the credential."""
    store().store("pässwörd➜ \n\n")
    assert store().load() == "pässwörd➜ \n\n"


def test_storing_twice_replaces_rather_than_piles_up(collection):
    store().store("first")
    store().store("second")

    assert store().load() == "second"
    assert len(collection.search_items(store().attributes)) == 1


def test_the_attributes_identify_one_secret_of_one_kind(collection):
    store("work").store("a")
    store("home").store("b")

    assert store("work").attributes == {
        "application": "davpunk",
        "kind": "caldav-password",
        "key": "work",
    }
    assert store("work").load() == "a"
    assert store("home").load() == "b"


def test_a_token_and_a_password_for_the_same_key_do_not_collide(collection):
    secret_store.KeyringStore(secret_store.CALDAV_PASSWORD, "mcp").store("password")
    secret_store.KeyringStore(secret_store.MCP_TOKEN, "mcp").store("token")

    assert secret_store.KeyringStore(secret_store.MCP_TOKEN, "mcp").load() == "token"


def test_a_locked_keyring_is_not_now_rather_than_broken(collection):
    """The same answer a cold gpg-agent gives, and the callers already know
    what to do with it: skip the cycle, warn once an hour."""
    store().store("hunter2")
    collection.locked = True

    with pytest.raises(CredentialLocked):
        store().load()


def test_reading_never_unlocks_the_keyring(collection):
    """Unlocking raises a desktop prompt.  A daemon that pops a password dialog
    behind the user's back is worse than one that waits."""
    store().store("hunter2")
    collection.locked = True

    with pytest.raises(CredentialLocked):
        store().load()
    assert collection.unlocked_by_us is False


def test_unlocking_is_something_only_a_click_can_do(collection):
    collection.locked = True
    store().unlock()
    assert collection.unlocked_by_us is True


def test_a_missing_secret_says_so_rather_than_returning_nothing(collection):
    with pytest.raises(CredentialError) as caught:
        store().load()
    assert not isinstance(caught.value, CredentialLocked)  # not a "try later"


def test_existence_is_answerable_while_locked(collection):
    """Which is what lets doctor tell "locked" from "never stored", and stops
    the MCP server minting a second token every cold start."""
    store().store("hunter2")
    collection.locked = True

    assert store().exists() is True
    assert store().is_locked() is True


def test_deleting_what_is_not_there_is_not_an_error(collection):
    store().delete()  # a half-finished backend switch must still be finishable


def test_deleting_removes_only_the_named_secret(collection):
    store("work").store("a")
    store("home").store("b")

    store("work").delete()

    assert store("home").load() == "b"
    with pytest.raises(CredentialError):
        store("work").load()


def test_no_secret_service_is_an_error_not_a_crash(no_service):
    with pytest.raises(CredentialError):
        store().load()
    assert secret_store.keyring_available() is False


def test_a_present_service_is_available_even_when_locked(collection):
    collection.locked = True
    assert secret_store.keyring_available() is True


# ------------------------------------------------------------------- factory


def test_the_factory_reads_the_remotes_backend(collection):
    keyring = secret_store.for_remote(
        Remote(id="work", name="Work", username="me", credential_backend="keyring")
    )
    gpg = secret_store.for_remote(Remote(id="work", gpg_file="/tmp/work.gpg", gpg_key_id="ABC!"))

    assert isinstance(keyring, secret_store.KeyringStore)
    assert isinstance(gpg, secret_store.GpgStore)


def test_a_remote_with_no_backend_named_is_still_gpg(collection):
    """Every config written before this existed means GnuPG, and reading it as
    anything else would look for a secret that was never stored there."""
    assert secret_store.for_remote(Remote(id="work")).backend == "gpg"


def test_the_keyring_label_names_the_account_a_human_would_recognise(collection):
    secret_store.for_remote(
        Remote(id="work", name="Work", username="me@example.com", credential_backend="keyring")
    ).store("hunter2")

    assert collection.items[0].label == "DavPunk — Work (me@example.com)"


def test_the_gpg_backend_refuses_to_store_without_a_key(tmp_path):
    """Better than encrypting to nothing and finding out at the next sync."""
    with pytest.raises(CredentialError):
        secret_store.GpgStore(tmp_path / "work.gpg", None).store("hunter2")


def test_describe_names_where_the_secret_actually_is(collection, tmp_path):
    assert "login keyring" in store().describe()
    assert str(tmp_path) in secret_store.GpgStore(tmp_path / "w.gpg", "ABC!").describe()
