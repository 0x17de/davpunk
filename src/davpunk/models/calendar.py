"""Calendar collections, and the deterministic identity they are keyed by."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict

#: Length of the hex prefix used as ``calendars.id``.
CALENDAR_ID_LEN = 16


def calendar_id(remote_id: str, href: str) -> str:
    """``sha256(remote_id || '\\0' || href)[:16]`` — deterministic.

    Recovery-triggered rebuilds are a normal event and MCP ``task_ref``s embed
    this value; a key minted at discovery would invalidate every reference an
    agent holds the moment the cache was rebuilt.
    """
    digest = hashlib.sha256(f"{remote_id}\0{href}".encode()).hexdigest()
    return digest[:CALENDAR_ID_LEN]


class Calendar(BaseModel):
    model_config = ConfigDict(validate_assignment=False)

    id: str
    remote_id: str
    href: str
    display_name: str | None = None
    color: str | None = None
    ctag: str | None = None
    sync_token: str | None = None
    supports_sync: bool = False
    available: bool = True
    last_sync: int | None = None

    @classmethod
    def create(cls, remote_id: str, href: str, **kwargs: object) -> Calendar:
        return cls(id=calendar_id(remote_id, href), remote_id=remote_id, href=href, **kwargs)  # type: ignore[arg-type]
