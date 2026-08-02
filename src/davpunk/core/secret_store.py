"""Where a secret is kept: GnuPG, or the desktop's login keyring.

One seam, two backends, and every caller — sync, MCP, the wizard, settings,
``doctor`` — asks for a store rather than naming a mechanism.  :mod:`credentials`
is unchanged and *is* the GnuPG backend: its key discovery, subkey pinning and
colon-listing are GnuPG's own problems and stay there.

The two are not equals in every way and the UI says so rather than picking for
you.  GnuPG keeps the secret encrypted to a key only the user holds, which
survives a stolen disk or a backup that went somewhere it should not have, and
costs a warm ``gpg-agent`` for background sync.  The login keyring is opened by
PAM at login, so the daemon just works — and once it is open, anything running
as this user can ask for the secret.

> The plaintext secret is never written to disk by DavPunk, never appears in
> ``argv``, never enters shell history, and is never logged or stored in
> SQLite.  That holds for both backends.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from davpunk.core.credentials import (
    CredentialError,
    CredentialLocked,
    decrypt_credential,
    store_credential,
)

log = logging.getLogger("davpunk.core.secret_store")

#: What the config may name.  ``file`` is the MCP token's own third option — a
#: 0600 plaintext file — and is never offered for a CalDAV password.
BACKENDS = ("gpg", "keyring")

#: Secret Service attributes.  ``application`` scopes the search to us, ``kind``
#: separates a CalDAV password from an MCP token, and ``key`` is the remote id
#: (or ``mcp`` for the token).  Together they are a unique lookup, which is what
#: lets ``doctor`` find an item without having read it.
APPLICATION = "davpunk"
CALDAV_PASSWORD = "caldav-password"
MCP_TOKEN = "mcp-token"


class SecretStore(Protocol):
    """One secret, wherever it happens to live."""

    def store(self, secret: str) -> None:
        """Write it, replacing whatever was there."""

    def load(self) -> str:
        """Read it back.

        Raises :class:`CredentialLocked` when the backend is merely shut — a
        cold ``gpg-agent``, a locked keyring — which every caller treats as
        "not now" rather than as an error, and :class:`CredentialError` for
        anything that will not fix itself.
        """
        ...

    def delete(self) -> None:
        """Remove it.  Missing is not an error: this is used to clean up after
        a backend switch, and half a switch must still be finishable."""

    def describe(self) -> str:
        """One line naming where the secret is, for ``doctor`` and the UI."""
        ...


# ---------------------------------------------------------------------- gnupg


class GpgStore:
    """The original backend, unchanged: a file encrypted to one subkey."""

    backend = "gpg"

    def __init__(self, path: Path | str, key_id: str | None) -> None:
        self.path = Path(path).expanduser()
        self.key_id = key_id

    def store(self, secret: str) -> None:
        if not self.key_id:
            raise CredentialError(
                "no GnuPG key is configured for this account; pick an encryption "
                "subkey in Preferences, or keep the password in the login keyring"
            )
        store_credential(secret, self.path, self.key_id)

    def load(self) -> str:
        return decrypt_credential(self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def describe(self) -> str:
        return f"{self.path} encrypted to {self.key_id or '(no key configured)'}"


# -------------------------------------------------------------------- keyring


class KeyringStore:
    """A Secret Service item — gnome-keyring, KWallet, KeePassXC.

    **Never unlocks.**  A locked collection raises :class:`CredentialLocked`,
    the same class of answer as a cold ``gpg-agent``, and the caller skips the
    cycle and warns once an hour.  Unlocking means a desktop prompt, and a
    background daemon that raises a password dialog behind the user's back is
    worse than one that waits for them.  Only :func:`unlock` — reached from an
    explicit click — may do that.
    """

    backend = "keyring"

    def __init__(self, kind: str, key: str, label: str | None = None) -> None:
        self.kind = kind
        self.key = key
        self.label = label or f"DavPunk — {key}"

    @property
    def attributes(self) -> dict[str, str]:
        return {"application": APPLICATION, "kind": self.kind, "key": self.key}

    def store(self, secret: str) -> None:
        with _connection() as (_conn, collection):
            if collection.is_locked():
                raise CredentialLocked("the login keyring is locked; unlock it and try again")
            collection.create_item(
                self.label, self.attributes, secret.encode("utf-8"), replace=True
            )
        log.info("Stored %s for %s in the login keyring", self.kind, self.key)

    def load(self) -> str:
        with _connection() as (_conn, collection):
            if collection.is_locked():
                raise CredentialLocked(
                    f"the login keyring is locked, so the {self.kind} for {self.key} "
                    "cannot be read right now"
                )
            item = self._find(collection)
            if item is None:
                raise CredentialError(
                    f"no {self.kind} for {self.key} in the login keyring; "
                    "set the password again in Preferences"
                )
            return item.get_secret().decode("utf-8")

    def delete(self) -> None:
        with _connection() as (_conn, collection):
            if collection.is_locked():
                raise CredentialLocked("the login keyring is locked; unlock it and try again")
            item = self._find(collection)
            if item is not None:
                item.delete()

    def unlock(self) -> None:
        """Ask the desktop to open the keyring.  Prompts — UI only."""
        with _connection() as (_conn, collection):
            if collection.is_locked():
                collection.unlock()

    def exists(self) -> bool:
        """Is the item there?  Answerable while locked — the attributes are not
        the secret — which is what lets ``doctor`` tell "locked" from "empty"."""
        with _connection() as (_conn, collection):
            return self._find(collection) is not None

    def is_locked(self) -> bool:
        with _connection() as (_conn, collection):
            return bool(collection.is_locked())

    def describe(self) -> str:
        return f"the login keyring ({self.kind} for {self.key})"

    def _find(self, collection):
        return next(iter(collection.search_items(self.attributes)), None)


class _Connection:
    """``with`` around a D-Bus connection and the default collection.

    A context manager rather than a cached connection: the daemon holds one for
    days, and a session bus that went away in the meantime should fail this
    cycle rather than poison every future one.
    """

    def __enter__(self):
        self._connection, collection = _open_collection()
        return self._connection, collection

    def __exit__(self, *exc_info) -> None:
        close = getattr(self._connection, "close", None)
        if close is not None:
            close()


def _connection() -> _Connection:
    return _Connection()


def _open_collection():
    """``(connection, default collection)``, or a :class:`CredentialError`.

    The one place that touches D-Bus, so the tests replace exactly this.
    """
    try:
        import secretstorage
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise CredentialError(
            "the login keyring needs the 'secretstorage' package "
            "(install DavPunk's 'keyring' extra)"
        ) from exc

    try:
        connection = secretstorage.dbus_init()
        return connection, secretstorage.get_default_collection(connection)
    except Exception as exc:  # secretstorage raises several unrelated classes
        raise CredentialError(f"no Secret Service is available: {exc}") from exc


def keyring_available() -> bool:
    """Is there a Secret Service to talk to at all?

    Locked still counts as available — a locked keyring is a keyring you can
    use, in a minute.  Used to decide whether the option is offerable, not
    whether it works right now.
    """
    try:
        with _connection():
            return True
    except CredentialError as exc:
        log.debug("No Secret Service: %s", exc)
        return False


# -------------------------------------------------------------------- factory


def for_remote(remote) -> SecretStore:
    """The store holding one remote's CalDAV password."""
    if getattr(remote, "credential_backend", "gpg") == "keyring":
        return KeyringStore(CALDAV_PASSWORD, remote.id, _remote_label(remote))
    return GpgStore(remote.gpg_file or "", remote.gpg_key_id)


def for_remote_backend(backend: str, remote_id: str, **details) -> SecretStore:
    """The same, from loose values — the wizard and settings have no model yet."""
    if backend == "keyring":
        return KeyringStore(
            CALDAV_PASSWORD,
            remote_id,
            _label(details.get("name") or remote_id, details.get("username")),
        )
    from davpunk import paths

    return GpgStore(
        details.get("gpg_file") or paths.credential_file(remote_id), details.get("key_id")
    )


def for_mcp_token(mcp) -> SecretStore:
    """The store holding the MCP bearer token.

    ``file`` has no store: it is a 0600 plaintext file the server writes
    itself, and pretending it is one of these would hide that difference.
    """
    if mcp.resolved_token_backend == "keyring":
        return KeyringStore(MCP_TOKEN, "mcp", "DavPunk — MCP bearer token")
    return GpgStore(mcp.token_path(), mcp.token_gpg_key_id)


def _remote_label(remote) -> str:
    return _label(getattr(remote, "name", None) or remote.id, getattr(remote, "username", None))


def _label(name: str, username: str | None) -> str:
    return f"DavPunk — {name} ({username})" if username else f"DavPunk — {name}"
