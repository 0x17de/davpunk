"""An in-memory CalDAV server with the same surface as ``CalDAVClient``.

The unit tests for the sync engine are about *protocol semantics* — adopt on a
UID match, 412 as the collision signal, PUT-before-DELETE on a move — not about
XML parsing, which ``test_caldav_client.py`` covers against real HTTP fixtures.
A fake that enforces the same preconditions makes those semantics testable
without a wire format in the way.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from davpunk.core.caldav_client import (
    CalDAVError,
    CollectionMissing,
    DiscoveredCalendar,
    FetchedResource,
    MultigetResult,
    NotFound,
    PreconditionFailed,
    ResourceMeta,
    SyncCollectionResult,
    WriteResult,
    batch_hrefs,
)


@dataclass
class StoredResource:
    href: str
    ics: str
    etag: str


@dataclass
class FakeCollection:
    href: str
    display_name: str = "Tasks"
    color: str | None = "#4A9EFF"
    supports_sync: bool = False
    resources: dict[str, StoredResource] = field(default_factory=dict)
    ctag_counter: itertools.count = field(default_factory=lambda: itertools.count(1))
    ctag: str = "ctag-0"

    def touch(self) -> None:
        self.ctag = f"ctag-{next(self.ctag_counter)}"


class FakeCalDAVServer:
    """The state a real server would hold, plus a call log for assertions."""

    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.calls: list[tuple[str, str]] = []
        self.etag_counter = itertools.count(1)
        #: Set to a status to make the next matching call fail.
        self.fail_next: dict[str, Exception] = {}
        #: Servers that answer 2xx with no ETag header are common.
        self.omit_etag_on_write = False
        #: Emulate a server that rewrites the href it was given.
        self.location_rewrite: str | None = None
        self.missing_collections: set[str] = set()

    # ------------------------------------------------------------------ setup

    def add_collection(self, href: str, **kwargs) -> FakeCollection:
        collection = FakeCollection(href=href, **kwargs)
        collection.touch()
        self.collections[href] = collection
        return collection

    def put_resource(self, collection_href: str, href: str, ics: str) -> StoredResource:
        """Seed a resource the way another client would have created it."""
        collection = self.collections[collection_href]
        resource = StoredResource(href=href, ics=ics, etag=self._new_etag())
        collection.resources[href] = resource
        collection.touch()
        return resource

    def _new_etag(self) -> str:
        return f'"e{next(self.etag_counter)}"'

    def _collection_of(self, href: str) -> FakeCollection:
        for collection in self.collections.values():
            if href.startswith(collection.href):
                if collection.href in self.missing_collections:
                    raise CollectionMissing(f"{collection.href} is gone")
                return collection
        raise CollectionMissing(f"no collection contains {href}")

    def _maybe_fail(self, key: str) -> None:
        exc = self.fail_next.pop(key, None)
        if exc is not None:
            raise exc

    def resource_count(self) -> int:
        return sum(len(c.resources) for c in self.collections.values())


class FakeClient:
    """The ``CalDAVClient`` surface, backed by :class:`FakeCalDAVServer`."""

    def __init__(self, server: FakeCalDAVServer) -> None:
        self.server = server

    def close(self) -> None:
        pass

    # -------------------------------------------------------------- discovery

    def discover(self) -> list[DiscoveredCalendar]:
        self.server.calls.append(("discover", ""))
        return [
            DiscoveredCalendar(
                href=c.href,
                display_name=c.display_name,
                color=c.color,
                ctag=c.ctag,
                supports_sync=c.supports_sync,
            )
            for href, c in self.server.collections.items()
            if href not in self.server.missing_collections
        ]

    def get_ctag(self, calendar_href: str) -> tuple[str | None, str | None]:
        self.server.calls.append(("get_ctag", calendar_href))
        collection = self.server.collections.get(calendar_href)
        if collection is None:
            raise NotFound(calendar_href)
        return collection.ctag, None

    # ------------------------------------------------------------ enumeration

    def list_vtodo_etags(self, calendar_href: str) -> list[ResourceMeta]:
        self.server.calls.append(("list", calendar_href))
        collection = self.server.collections[calendar_href]
        return [
            ResourceMeta(href=r.href, etag=r.etag)
            for r in sorted(collection.resources.values(), key=lambda r: r.href)
        ]

    def sync_collection(self, calendar_href: str, sync_token: str | None) -> SyncCollectionResult:
        self.server.calls.append(("sync_collection", calendar_href))
        return SyncCollectionResult(invalid_token=True)

    # --------------------------------------------------------------- fetching

    def multiget(self, calendar_href: str, hrefs: list[str]) -> MultigetResult:
        self.server.calls.append(("multiget", f"{calendar_href}:{len(hrefs)}"))
        self.server._maybe_fail("multiget")
        collection = self.server.collections[calendar_href]

        result = MultigetResult()
        for href in hrefs:
            resource = collection.resources.get(href)
            if resource is None:
                result.failed.append(href)
            else:
                result.resources.append(
                    FetchedResource(href=href, etag=resource.etag, ics=resource.ics)
                )
        return result

    def get(self, href: str) -> FetchedResource:
        self.server.calls.append(("get", href))
        collection = self.server._collection_of(href)
        resource = collection.resources.get(href)
        if resource is None:
            raise NotFound(href)
        return FetchedResource(href=href, etag=resource.etag, ics=resource.ics)

    def get_etag(self, calendar_href: str, href: str) -> str | None:
        self.server.calls.append(("get_etag", href))
        self.server._maybe_fail("get_etag")
        collection = self.server.collections[calendar_href]
        resource = collection.resources.get(href)
        return resource.etag if resource else None

    # ---------------------------------------------------------------- writing

    def put_create(self, href: str, ics: str) -> WriteResult:
        self.server.calls.append(("put_create", href))
        self.server._maybe_fail("put_create")
        collection = self.server._collection_of(href)

        if href in collection.resources:
            raise PreconditionFailed(f"PUT {href}: 412 (If-None-Match: *)")

        target = self.server.location_rewrite or href
        self.server.location_rewrite = None
        etag = self.server._new_etag()
        collection.resources[target] = StoredResource(href=target, ics=ics, etag=etag)
        collection.touch()

        return WriteResult(
            status=201,
            etag=None if self.server.omit_etag_on_write else etag,
            location=target if target != href else None,
        )

    def put_update(self, href: str, ics: str, base_etag: str) -> WriteResult:
        self.server.calls.append(("put_update", href))
        self.server._maybe_fail("put_update")
        collection = self.server._collection_of(href)

        resource = collection.resources.get(href)
        if resource is None:
            raise NotFound(f"PUT {href}: 404")
        if resource.etag != base_etag:
            raise PreconditionFailed(f"PUT {href}: 412 (If-Match)")

        resource.ics = ics
        resource.etag = self.server._new_etag()
        collection.touch()
        return WriteResult(
            status=204, etag=None if self.server.omit_etag_on_write else resource.etag
        )

    def delete(self, href: str, base_etag: str | None = None) -> WriteResult:
        self.server.calls.append(("delete", href))
        self.server._maybe_fail("delete")
        collection = self.server._collection_of(href)

        resource = collection.resources.get(href)
        if resource is None:
            return WriteResult(status=404)  # SUCCESS: both sides agree it is gone
        if base_etag is not None and resource.etag != base_etag:
            raise PreconditionFailed(f"DELETE {href}: 412")

        del collection.resources[href]
        collection.touch()
        return WriteResult(status=204)


def count_calls(server: FakeCalDAVServer, kind: str) -> int:
    return sum(1 for k, _ in server.calls if k == kind)


__all__ = [
    "CalDAVError",
    "FakeCalDAVServer",
    "FakeClient",
    "PreconditionFailed",
    "batch_hrefs",
    "count_calls",
]
