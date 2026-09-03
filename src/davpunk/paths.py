"""XDG-derived filesystem locations.

Every path is derived through these helpers so tests can relocate the whole
application state by setting ``XDG_*`` or ``DAVPUNK_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "davpunk"

CONFIG_MODE = 0o600
CREDENTIALS_DIR_MODE = 0o700
CREDENTIAL_MODE = 0o600
TOKEN_MODE = 0o600


def _xdg(var: str, default: str) -> Path:
    override = os.environ.get("DAVPUNK_HOME")
    if override:
        return Path(override).expanduser() / default.rsplit("/", 1)[-1]
    value = os.environ.get(var)
    if value:
        return Path(value).expanduser()
    return Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def runtime_dir() -> Path:
    value = os.environ.get("DAVPUNK_HOME") or os.environ.get("XDG_RUNTIME_DIR")
    if value:
        return Path(value).expanduser() / APP
    return state_dir() / "run"


def config_file() -> Path:
    return config_dir() / "config.toml"


def credentials_dir() -> Path:
    return config_dir() / "credentials"


def credential_file(remote_id: str) -> Path:
    return credentials_dir() / f"{remote_id}.gpg"


def database_file() -> Path:
    return data_dir() / "davpunk.db"


def recovery_dir() -> Path:
    return data_dir() / "recovery"


def ui_state_file() -> Path:
    """Machine-written view state — last filter and the like.  Not config: it
    lives in the state dir precisely because nobody is meant to edit it."""
    return state_dir() / "ui-state.json"


def log_file() -> Path:
    return state_dir() / "davpunk.log"


def lock_dir() -> Path:
    return runtime_dir()


def remote_lock_file(remote_id: str) -> Path:
    return lock_dir() / f"sync-{remote_id}.lock"


def ui_lock_file() -> Path:
    return lock_dir() / "ui.lock"


def mcp_token_file() -> Path:
    return config_dir() / "mcp-token"


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    """mkdir -p with an explicit mode applied to the leaf when we create it."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed:
        path.chmod(mode)
    return path


def ensure_runtime_dirs() -> None:
    ensure_dir(config_dir())
    ensure_dir(credentials_dir(), CREDENTIALS_DIR_MODE)
    ensure_dir(data_dir())
    ensure_dir(state_dir())
    ensure_dir(lock_dir())
