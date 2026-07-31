"""The Remote model — the runtime view of one ``[[davpunk.remotes]]`` block."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Remote(BaseModel):
    """A CalDAV server as cached in the ``remotes`` table.

    The TOML config is authoritative; this table is a cache keyed by config
    ``id``.  ``orphaned`` marks a row whose config block has gone away: hidden,
    not synced, **data retained**.
    """

    model_config = ConfigDict(validate_assignment=False)

    id: str
    name: str | None = None
    url: str = ""
    username: str | None = None
    gpg_file: str | None = None
    gpg_key_id: str | None = None
    auto_sync: bool = True
    sync_interval: int = 300
    color: str | None = None
    pinned_view: bool = False
    allow_insecure: bool = False
    verify_tls: bool = True
    orphaned: bool = False
