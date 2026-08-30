"""Configuration: pydantic-settings over TOML, read **once at startup**.

The TOML file is authoritative for remotes; the ``remotes`` table is a cache
keyed by config ``id``.  Live reload is out of scope — changes require a
restart, and ``davpunk doctor`` validates the file without one.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from davpunk import paths
from davpunk.core import secret_store
from davpunk.models.remote import Remote

log = logging.getLogger("davpunk.config")

MCP_LIST_LIMIT_CAP = 500  # hard cap, whatever the config says

DEFAULT_KEYS: dict[str, str] = {
    # global
    "down": "j",
    "up": "k",
    "top": "g,g",
    "bottom": "G",
    "search": "/",
    "clear": "Esc",
    "view_list": "1",
    "view_kanban": "2",
    "view_search": "3",
    "sync_now": "Ctrl+R",
    "help_overlay": "?",
    # task
    "new_task": "n",
    # Shift+N rather than a bare "N": QKeySequence folds an unmodified letter,
    # so "N" and "n" would be one ambiguous shortcut and neither would fire.
    "new_subtask": "Shift+N",
    "new_subtasks": "Ctrl+Shift+N",
    "open_editor": "Return",
    "toggle_complete": "Space",
    "delete_task": "d,d",
    "move_task": "m",
    "cut_task": "Ctrl+X",
    "paste_task": "Ctrl+V",
    "inline_rename": "e",
    "indent": "Tab",
    "outdent": "Shift+Tab",
    "reorder_down": "Alt+j",
    "reorder_up": "Alt+k",
    "set_priority": "p",
    "set_tags": "t",
    "set_due": "s",
    "expand_all": "z,R",
    "collapse_all": "z,M",
    # kanban
    "card_prev_column": "h",
    "card_next_column": "l",
    "focus_prev_column": "H",
    "focus_next_column": "L",
    # merge window / conflict dialog
    "take_local": "l",
    "take_server": "s",
    "take_all_local": "a",
    "take_all_server": "A",
    "save_resolution": "Return",
    "skip_resolution": "Escape",
}

#: Actions that share a keystroke legitimately, because they live in different
#: modal contexts.  Duplicate detection is scoped per context.
KEY_CONTEXTS: dict[str, str] = {
    **{k: "global" for k in DEFAULT_KEYS},
    "card_prev_column": "kanban",
    "card_next_column": "kanban",
    "focus_prev_column": "kanban",
    "focus_next_column": "kanban",
    "take_local": "conflict",
    "take_server": "conflict",
    "take_all_local": "conflict",
    "take_all_server": "conflict",
    "save_resolution": "conflict",
    "skip_resolution": "conflict",
}


class ConfigError(Exception):
    """A configuration problem worth showing the user verbatim."""

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path

    def __str__(self) -> str:
        base = super().__str__()
        return f"{self.path}: {base}" if self.path else base


class KanbanColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    #: The VTODO STATUS this column stands for.  ``None`` is the column for
    #: tasks that carry **no** STATUS at all — which is a different thing from
    #: NEEDS-ACTION, and the distinction between a pool to pick from and work
    #: that has been picked.  Dropping a card there clears STATUS.
    status: str | None = None
    #: Start collapsed to a strip of its own name and count.  Done and
    #: Cancelled are the reason this exists: they are worth keeping as drop
    #: targets and worth counting, and worth almost none of the width they
    #: take up.  A startup default only — clicking a column name folds and
    #: unfolds it, and that is not written back, the same way "show
    #: completed" is not.  With "show completed" off at startup the finished
    #: columns fold whatever this says: they can hold nothing else.
    folded: bool = False


class KanbanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[KanbanColumn] = Field(
        default_factory=lambda: [
            # "To Do" is the tasks with no STATUS at all — the pool you pick
            # from. "Needs Action" is the ones somebody has picked. Collapsing
            # the two loses exactly the distinction the board exists to show.
            KanbanColumn(id="todo", label="To Do", status=None),
            KanbanColumn(id="needsaction", label="Needs Action", status="NEEDS-ACTION"),
            KanbanColumn(id="inprogress", label="In Progress", status="IN-PROCESS"),
            KanbanColumn(id="done", label="Done", status="COMPLETED"),
            KanbanColumn(id="cancelled", label="Cancelled", status="CANCELLED"),
        ]
    )

    @field_validator("columns")
    @classmethod
    def _unique_ids(cls, columns: list[KanbanColumn]) -> list[KanbanColumn]:
        """Column ids must be unique; several columns *may* share a status —
        that is exactly what X-DAVPUNK-KANBAN-COL exists to disambiguate.
        """
        if not columns:
            raise ValueError("at least one kanban column is required")
        seen: set[str] = set()
        for column in columns:
            if column.id in seen:
                raise ValueError(f"duplicate kanban column id {column.id!r}")
            seen.add(column.id)
        return columns


class McpCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: bool = False
    write: bool = False  # includes create, update, set_*, move
    delete: bool = False
    sync: bool = False


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    transport: str = "stdio"  # stdio (recommended) | sse
    bind: str = "127.0.0.1"  # SSE binds loopback only
    port: int = 8787
    #: None means "next to the rest of the config".  A hardcoded ~/... default
    #: would ignore XDG_CONFIG_HOME and land the token somewhere other than the
    #: config.toml it belongs to.
    token_file: str | None = None
    #: When set, the bearer token is stored GnuPG-encrypted to this key rather
    #: than as a 0600 plaintext file, and decrypted at startup — the same
    #: treatment CalDAV passwords get.  The cost is the same too: the server
    #: can only start while gpg-agent still holds the passphrase.
    token_gpg_key_id: str | None = None
    #: ``file`` (0600 plaintext), ``gpg`` or ``keyring``.  Unset is resolved
    #: from ``token_gpg_key_id`` so a config written before this key existed
    #: keeps meaning what it meant.
    token_backend: str | None = None
    audit: bool = True
    list_limit: int = 100
    capabilities: McpCapabilities = Field(default_factory=McpCapabilities)

    @field_validator("transport")
    @classmethod
    def _known_transport(cls, value: str) -> str:
        if value not in ("stdio", "sse"):
            raise ValueError("transport must be 'stdio' or 'sse'")
        return value

    @field_validator("bind")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        """SSE binds loopback only — never 0.0.0.0."""
        if value not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                f"mcp.bind must be a loopback address (got {value!r}); "
                "DavPunk's MCP server is not designed to be network-exposed"
            )
        return value

    @field_validator("list_limit")
    @classmethod
    def _capped(cls, value: int) -> int:
        if value < 1:
            raise ValueError("list_limit must be >= 1")
        return min(value, MCP_LIST_LIMIT_CAP)

    @field_validator("token_backend")
    @classmethod
    def _known_token_backend(cls, value: str | None) -> str | None:
        if value is not None and value not in ("file", *secret_store.BACKENDS):
            raise ValueError("mcp.token_backend must be 'file', 'gpg' or 'keyring'")
        return value

    @model_validator(mode="after")
    def _token_backend_is_unambiguous(self) -> McpConfig:
        if self.token_backend == "gpg" and not self.token_gpg_key_id:
            raise ValueError("mcp.token_backend = 'gpg' needs mcp.token_gpg_key_id")
        if self.token_backend in ("file", "keyring") and self.token_gpg_key_id:
            raise ValueError(
                f"mcp.token_gpg_key_id is set but token_backend is "
                f"{self.token_backend!r}, so it would encrypt nothing"
            )
        return self

    @property
    def resolved_token_backend(self) -> str:
        """What the config *means*, with the pre-``token_backend`` spelling
        still meaning what it did: a key id alone said "encrypt it"."""
        if self.token_backend:
            return self.token_backend
        return "gpg" if self.token_gpg_key_id else "file"

    @property
    def token_is_encrypted(self) -> bool:
        """Is the token kept somewhere other than a plaintext file?"""
        return self.resolved_token_backend != "file"

    def token_path(self) -> Path:
        """Where the token file lives, for the backends that use one.

        The keyring keeps no file, but the path is still what names the item's
        neighbours in ``doctor`` output, so it stays defined rather than
        raising and forcing every caller to branch.
        """
        base = self.plain_token_path()
        return base.with_name(base.name + ".gpg") if self.resolved_token_backend == "gpg" else base

    def plain_token_path(self) -> Path:
        if self.token_file:
            return Path(self.token_file).expanduser()
        return paths.mcp_token_file()


class RemoteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None
    url: str
    username: str | None = None
    #: Where this account's password is kept.  ``gpg`` encrypts it to a key
    #: only you hold — safe in a backup, and only readable while gpg-agent is
    #: warm.  ``keyring`` hands it to the desktop's Secret Service, which PAM
    #: opens at login — nothing to set up and the daemon just works, at the
    #: price of every process running as you being able to ask for it.
    credential_backend: str = "gpg"
    gpg_file: str | None = None
    gpg_key_id: str | None = None
    #: Sync this account in the background.  Off does not mean "never sync":
    #: `davpunk sync`, the toolbar button and MCP `sync_now` are explicit acts
    #: and still work.  It means "not behind my back, on a timer".
    auto_sync: bool = True
    sync_interval: int = 300
    color: str | None = None
    pinned_view: bool = False
    allow_insecure: bool = False
    verify_tls: bool = True

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        """The id becomes a filename (lock file, credential file)."""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            raise ValueError(
                "remote id must be 1-64 chars of [A-Za-z0-9._-] (it is used as a filename)"
            )
        return value

    @field_validator("sync_interval")
    @classmethod
    def _sane_interval(cls, value: int) -> int:
        if value < 30:
            raise ValueError("sync_interval must be at least 30 seconds")
        return value

    @field_validator("credential_backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value not in secret_store.BACKENDS:
            raise ValueError(
                f"credential_backend must be one of {', '.join(secret_store.BACKENDS)}"
            )
        return value

    @model_validator(mode="after")
    def _backend_is_unambiguous(self) -> RemoteConfig:
        """A ``gpg_key_id`` beside ``credential_backend = "keyring"`` governs
        nothing, and a setting that governs nothing reads as one that does."""
        if self.credential_backend == "keyring" and self.gpg_key_id:
            raise ValueError(
                f"remote {self.id!r}: gpg_key_id is set but credential_backend is "
                "'keyring', so it would encrypt nothing — remove one of the two"
            )
        return self

    @model_validator(mode="after")
    def _transport_security(self) -> RemoteConfig:
        """``http://`` is rejected unless the remote opts in explicitly."""
        if self.url.startswith("http://"):
            if not self.allow_insecure:
                raise ValueError(
                    f"remote {self.id!r} uses http://; set allow_insecure = true "
                    "to permit an unencrypted CalDAV connection"
                )
            log.warning(
                "Remote %r uses http:// — credentials and task data cross the "
                "network in cleartext (allow_insecure = true)",
                self.id,
            )
        elif not self.url.startswith("https://"):
            raise ValueError(f"remote {self.id!r}: url must be http(s)://")
        return self

    def resolved_gpg_file(self) -> Path:
        if self.gpg_file:
            return Path(self.gpg_file).expanduser()
        return paths.credential_file(self.id)

    def to_model(self) -> Remote:
        return Remote(
            id=self.id,
            name=self.name,
            url=self.url,
            username=self.username,
            credential_backend=self.credential_backend,
            gpg_file=str(self.resolved_gpg_file()),
            gpg_key_id=self.gpg_key_id,
            auto_sync=self.auto_sync,
            sync_interval=self.sync_interval,
            color=self.color,
            pinned_view=self.pinned_view,
            allow_insecure=self.allow_insecure,
            verify_tls=self.verify_tls,
        )


class DavPunkConfig(BaseModel):
    """The ``[davpunk]`` table."""

    model_config = ConfigDict(extra="forbid")

    theme: str = "dark"
    default_view: str = "kanban"
    show_completed: bool = False  # startup default; runtime toggle is not persisted
    max_resource_bytes: int = 262_144  # oversize quarantine threshold
    unified_view: bool = True

    remotes: list[RemoteConfig] = Field(default_factory=list)
    kanban: KanbanConfig = Field(default_factory=KanbanConfig)
    keys: dict[str, str] = Field(default_factory=dict)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @field_validator("default_view")
    @classmethod
    def _known_view(cls, value: str) -> str:
        if value not in ("list", "kanban"):
            raise ValueError("default_view must be 'list' or 'kanban'")
        return value

    @field_validator("max_resource_bytes")
    @classmethod
    def _sane_size(cls, value: int) -> int:
        if value < 4096:
            raise ValueError("max_resource_bytes must be at least 4096")
        return value

    @field_validator("remotes")
    @classmethod
    def _unique_remote_ids(cls, remotes: list[RemoteConfig]) -> list[RemoteConfig]:
        seen: set[str] = set()
        for remote in remotes:
            if remote.id in seen:
                raise ValueError(f"duplicate remote id {remote.id!r}")
            seen.add(remote.id)
        return remotes

    @model_validator(mode="after")
    def _validate_keys(self) -> DavPunkConfig:
        unknown = set(self.keys) - set(DEFAULT_KEYS)
        if unknown:
            raise ValueError("unknown key binding action(s): " + ", ".join(sorted(unknown)))
        detect_duplicate_bindings(self.keymap())
        return self

    def keymap(self) -> dict[str, str]:
        """Defaults with ``[davpunk.keys]`` overrides applied."""
        return {**DEFAULT_KEYS, **self.keys}


def normalize_binding(binding: str) -> str:
    """Canonical form for comparison: modifiers case-folded, key case KEPT.

    For an **unmodified** key, case is the binding: ``h`` and ``H`` are
    genuinely different, and the default keymap uses both pairs (``h``/``H`` for
    kanban, ``a``/``A`` in the conflict dialog), so a blanket ``.lower()`` would
    report them as clashes.

    Once a modifier is present, case stops carrying information — ``Ctrl+R`` and
    ``ctrl+r`` are one binding — and modifier order does not matter either.
    """
    parts = [p.strip() for p in binding.split("+")]
    if len(parts) == 1:
        return parts[0]
    *modifiers, key = parts
    return "+".join([*sorted(m.lower() for m in modifiers), key.lower()])


def detect_duplicate_bindings(keymap: dict[str, str]) -> None:
    """Duplicate bindings are a config error, never silently last-wins.

    Scoped per modal context, so ``l`` meaning both "next kanban column" and
    "take local" is fine — the two contexts are never active at once.
    """
    by_context: dict[tuple[str, str], list[str]] = {}
    for action, binding in keymap.items():
        context = KEY_CONTEXTS.get(action, "global")
        by_context.setdefault((context, normalize_binding(binding)), []).append(action)
    clashes = [
        f"{binding!r} bound to {', '.join(sorted(actions))} (context: {context})"
        for (context, binding), actions in sorted(by_context.items())
        if len(actions) > 1
    ]
    if clashes:
        raise ValueError("duplicate key bindings: " + "; ".join(clashes))


def load_config(path: Path | str | None = None, *, validate_gpg_key: bool = False) -> DavPunkConfig:
    """Read and validate the TOML config.

    Raises :class:`ConfigError` with the Pydantic message verbatim, naming the
    file and the offending key, so first-run can show it without crashing.

    ``validate_gpg_key`` was ``validate_keyring``, which meant GnuPG's keyring
    and now sits one line away from a login-keyring backend that is a different
    thing entirely.
    """
    # ``--config`` arrives as a string from argparse; coerce at the boundary so
    # no caller has to remember to.
    path = Path(path).expanduser() if path else paths.config_file()
    if not path.exists():
        return DavPunkConfig()

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}", path=path) from exc
    except OSError as exc:
        raise ConfigError(f"cannot be read: {exc}", path=path) from exc

    section: Any = raw.get("davpunk", {})
    if not isinstance(section, dict):
        raise ConfigError("the [davpunk] table must be a table", path=path)

    try:
        config = DavPunkConfig.model_validate(section)
    except ValidationError as exc:
        raise ConfigError(str(exc), path=path) from exc

    if validate_gpg_key:
        for remote in config.remotes:
            if remote.credential_backend != "gpg":
                continue
            if remote.gpg_key_id and not gpg_key_present(remote.gpg_key_id):
                raise ConfigError(
                    f"remote {remote.id!r}: gpg_key_id {remote.gpg_key_id!r} is not in the keyring",
                    path=path,
                )
    return config


def gpg_key_present(key_id: str) -> bool:
    """Is ``key_id`` a secret key in GnuPG's own keyring?"""
    try:
        result = subprocess.run(
            ["gpg", "--batch", "--list-secret-keys", "--with-colons", key_id],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and b"sec:" in result.stdout


def config_file_mode_ok(path: Path | None = None) -> bool:
    path = path or paths.config_file()
    if not path.exists():
        return True
    return (path.stat().st_mode & 0o077) == 0


def env_override_level() -> str | None:
    return os.environ.get("DAVPUNK_LOG_LEVEL")
