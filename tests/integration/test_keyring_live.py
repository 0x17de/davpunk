"""The keyring backend against a **real** Secret Service.

Skipped unless one is answering, so the ordinary suite does not depend on the
developer's desktop.  To run it against a throwaway gnome-keyring — its own bus,
its own keyring directory, nothing touching your login keyring:

    nix shell nixpkgs#gnome-keyring nixpkgs#dbus -c dbus-run-session -- \\
      env HOME=$(mktemp -d) XDG_DATA_HOME=$HOME/.local/share sh -c \\
      'eval $(printf test | gnome-keyring-daemon --unlock --components=secrets); \\
       pytest -m keyring'

The unit tests cover the logic with a fake collection.  What *this* file is for
is the half a fake cannot vouch for: that `create_item(replace=True)`,
`search_items` and `get_secret` behave the way the code assumes, and — the
claim doctor and the MCP token both rest on — that an item can still be *found*
while the collection is locked, even though its secret cannot be read.
"""

from __future__ import annotations

import contextlib

import pytest

from davpunk.core import secret_store
from davpunk.core.credentials import CredentialLocked

pytestmark = pytest.mark.keyring

KEY = "davpunk-integration-selftest"


@pytest.fixture
def store():
    if not secret_store.keyring_available():
        pytest.skip("no Secret Service on this session bus")
    made = secret_store.KeyringStore(secret_store.CALDAV_PASSWORD, KEY, "DavPunk selftest")
    yield made
    # A locked keyring at teardown is not this test's business to solve.
    with contextlib.suppress(Exception):
        made.delete()


def _lock():
    import secretstorage

    connection = secretstorage.dbus_init()
    secretstorage.get_default_collection(connection).lock()


def test_a_real_secret_survives_the_round_trip(store):
    store.store("hünter2 \n")  # trailing whitespace is part of a password
    assert store.load() == "hünter2 \n"


def test_storing_twice_replaces_rather_than_piles_up(store):
    store.store("first")
    store.store("second")
    assert store.load() == "second"


def test_a_locked_collection_hides_the_secret_but_not_the_item(store):
    """Everything downstream rests on this: doctor tells "locked" from "never
    stored", and the MCP server declines to mint a second token."""
    store.store("hunter2")
    _lock()

    assert store.is_locked() is True
    assert store.exists() is True
    with pytest.raises(CredentialLocked):
        store.load()
