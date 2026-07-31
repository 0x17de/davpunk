"""CalDAV over HTTP: discovery, REPORT, multiget batching, guarded writes.

Implemented directly on ``requests`` rather than through the ``caldav``
library, because the sync protocol in the HLD needs control the abstraction does
not expose: ``If-None-Match: *`` on create, ``If-Match`` on update and delete,
the ``Location`` header on a create, per-href statuses inside a 207, and
``calendar-multiget`` batched at 50.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

from davpunk.models.remote import Remote

log = logging.getLogger("davpunk.core.caldav_client")

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"
ICAL = "http://apple.com/ns/ical/"

NS = {"d": DAV, "c": CALDAV, "cs": CS, "ical": ICAL}
for prefix, uri in NS.items():
    ET.register_namespace("" if prefix == "d" else prefix, uri)

#: 10 s to connect, 30 s to read.
TIMEOUTS = (10, 30)

#: ``calendar-multiget`` batch size.  A single GET is used only for the
#: adopt-probe and for a conflict refetch.
MULTIGET_BATCH = 50


def batch_hrefs(hrefs: list[str], size: int = MULTIGET_BATCH) -> list[list[str]]:
    """The multiget batch plan.

    A free function rather than a method so the sync engine can check
    cancellation between batches without this module knowing about
    ``threading``.
    """
    return [hrefs[i : i + size] for i in range(0, len(hrefs), size)]


class CalDAVError(Exception):
    """A protocol-level failure with, where known, its HTTP status."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PreconditionFailed(CalDAVError):
    """412 — the ``If-Match`` / ``If-None-Match`` guard did not hold."""

    def __init__(self, message: str = "precondition failed") -> None:
        super().__init__(message, status=412)


class NotFound(CalDAVError):
    def __init__(self, message: str = "not found") -> None:
        super().__init__(message, status=404)


class CollectionMissing(CalDAVError):
    """409 — the parent collection is gone.  Never retried per task."""

    def __init__(self, message: str = "parent collection missing") -> None:
        super().__init__(message, status=409)


class Unauthorized(CalDAVError):
    def __init__(self, message: str = "unauthorized") -> None:
        super().__init__(message, status=401)


@dataclass
class DiscoveredCalendar:
    href: str
    display_name: str | None = None
    color: str | None = None
    ctag: str | None = None
    sync_token: str | None = None
    supports_sync: bool = False


@dataclass
class ResourceMeta:
    href: str
    etag: str | None = None


@dataclass
class FetchedResource:
    href: str
    etag: str | None
    ics: str


@dataclass
class MultigetResult:
    resources: list[FetchedResource] = field(default_factory=list)
    #: hrefs the server reported an error for; the caller falls back to a
    #: single GET, then to the next cycle.
    failed: list[str] = field(default_factory=list)


@dataclass
class SyncCollectionResult:
    changed: list[ResourceMeta] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    sync_token: str | None = None
    #: The server refused the token (507 / valid-sync-token); re-enumerate fully.
    invalid_token: bool = False


@dataclass
class WriteResult:
    status: int
    etag: str | None = None
    location: str | None = None


class CalDAVClient:
    def __init__(
        self,
        remote: Remote,
        credential: str,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.remote = remote
        self.base_url = remote.url
        self._session = session or requests.Session()
        self._session.auth = HTTPBasicAuth(remote.username or "", credential)
        self._session.verify = remote.verify_tls
        self._session.headers.update({"User-Agent": "DavPunk/0.1"})

    def close(self) -> None:
        self._session.close()

    # ---------------------------------------------------------------- plumbing

    def _url(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def _request(self, method: str, href: str, **kwargs: Any) -> requests.Response:
        url = self._url(href)
        try:
            response = self._session.request(method, url, timeout=TIMEOUTS, **kwargs)
        except requests.Timeout as exc:
            raise CalDAVError(f"{method} {url} timed out") from exc
        except requests.RequestException as exc:
            raise CalDAVError(f"{method} {url} failed: {exc}") from exc

        if response.status_code == 401:
            raise Unauthorized(f"{method} {url}: 401")
        return response

    def _propfind(self, href: str, body: str, depth: str = "0") -> ET.Element:
        response = self._request(
            "PROPFIND",
            href,
            data=body.encode("utf-8"),
            headers={"Depth": depth, "Content-Type": 'application/xml; charset="utf-8"'},
        )
        if response.status_code == 404:
            raise NotFound(f"PROPFIND {href}: 404")
        if response.status_code not in (207, 200):
            raise CalDAVError(
                f"PROPFIND {href}: {response.status_code}", status=response.status_code
            )
        return _parse_xml(response.content)

    def _report(self, href: str, body: str, depth: str = "1") -> ET.Element:
        response = self._request(
            "REPORT",
            href,
            data=body.encode("utf-8"),
            headers={"Depth": depth, "Content-Type": 'application/xml; charset="utf-8"'},
        )
        if response.status_code == 404:
            raise NotFound(f"REPORT {href}: 404")
        if response.status_code not in (207, 200):
            raise CalDAVError(f"REPORT {href}: {response.status_code}", status=response.status_code)
        return _parse_xml(response.content)

    # --------------------------------------------------------------- discovery

    def discover(self) -> list[DiscoveredCalendar]:
        """principal → calendar-home-set → collections.

        Only collections whose ``supported-calendar-component-set`` contains
        VTODO are kept: a mixed collection full of VEVENTs is not ours.
        """
        principal = self._current_user_principal()
        home = self._calendar_home_set(principal)
        return self._list_collections(home)

    def _current_user_principal(self) -> str:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="{DAV}"><d:prop><d:current-user-principal/>'
            "</d:prop></d:propfind>"
        )
        root = self._propfind("", body)
        for response in root.findall("d:response", NS):
            href = response.find("d:propstat/d:prop/d:current-user-principal/d:href", NS)
            if href is not None and href.text:
                return href.text
        # Some servers (Radicale with a direct collection URL) answer no
        # principal; the configured URL is then already the home set.
        log.debug("No current-user-principal; using the configured URL as the principal")
        return urlparse(self.base_url).path or "/"

    def _calendar_home_set(self, principal: str) -> str:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop>'
            "<c:calendar-home-set/></d:prop></d:propfind>"
        )
        try:
            root = self._propfind(principal, body)
        except NotFound:
            return principal
        for response in root.findall("d:response", NS):
            href = response.find("d:propstat/d:prop/c:calendar-home-set/d:href", NS)
            if href is not None and href.text:
                return href.text
        return principal

    def _list_collections(self, home: str) -> list[DiscoveredCalendar]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}" xmlns:cs="{CS}" '
            f'xmlns:ical="{ICAL}"><d:prop>'
            "<d:resourcetype/><d:displayname/><ical:calendar-color/>"
            "<cs:getctag/><c:supported-calendar-component-set/>"
            "<d:supported-report-set/><d:sync-token/>"
            "</d:prop></d:propfind>"
        )
        root = self._propfind(home, body, depth="1")

        out: list[DiscoveredCalendar] = []
        for response in root.findall("d:response", NS):
            href_el = response.find("d:href", NS)
            if href_el is None or not href_el.text:
                continue
            href = unquote(href_el.text)

            prop = _ok_prop(response)
            if prop is None:
                continue
            if prop.find("d:resourcetype/c:calendar", NS) is None:
                continue
            if not _supports_vtodo(prop):
                log.debug("Skipping %s: no VTODO in supported-calendar-component-set", href)
                continue

            out.append(
                DiscoveredCalendar(
                    href=href,
                    display_name=_text(prop, "d:displayname"),
                    color=_text(prop, "ical:calendar-color"),
                    ctag=_text(prop, "cs:getctag"),
                    sync_token=_text(prop, "d:sync-token"),
                    supports_sync=_supports_sync_collection(prop),
                )
            )
        return out

    def get_ctag(self, calendar_href: str) -> tuple[str | None, str | None]:
        """``(ctag, sync_token)`` — the pull's short-circuit."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="{DAV}" xmlns:cs="{CS}"><d:prop>'
            "<cs:getctag/><d:sync-token/></d:prop></d:propfind>"
        )
        root = self._propfind(calendar_href, body)
        for response in root.findall("d:response", NS):
            prop = _ok_prop(response)
            if prop is not None:
                return _text(prop, "cs:getctag"), _text(prop, "d:sync-token")
        return None, None

    # ------------------------------------------------------------ enumeration

    def list_vtodo_etags(self, calendar_href: str) -> list[ResourceMeta]:
        """``REPORT calendar-query`` with a VTODO comp-filter, ETags only."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:calendar-query xmlns:d="{DAV}" xmlns:c="{CALDAV}">'
            "<d:prop><d:getetag/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VTODO"/>'
            "</c:comp-filter></c:filter></c:calendar-query>"
        )
        root = self._report(calendar_href, body)
        out: list[ResourceMeta] = []
        for response in root.findall("d:response", NS):
            href_el = response.find("d:href", NS)
            if href_el is None or not href_el.text:
                continue
            prop = _ok_prop(response)
            etag = _text(prop, "d:getetag") if prop is not None else None
            out.append(ResourceMeta(href=unquote(href_el.text), etag=etag))
        return out

    def sync_collection(self, calendar_href: str, sync_token: str | None) -> SyncCollectionResult:
        """RFC 6578 incremental enumeration, where the collection supports it."""
        token = f"<d:sync-token>{sync_token}</d:sync-token>" if sync_token else "<d:sync-token/>"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:sync-collection xmlns:d="{DAV}">{token}'
            "<d:sync-level>1</d:sync-level>"
            "<d:prop><d:getetag/></d:prop></d:sync-collection>"
        )
        try:
            root = self._report(calendar_href, body, depth="0")
        except CalDAVError as exc:
            if exc.status in (403, 409, 507):
                # A token the server no longer accepts.  Fall back to a full
                # enumeration rather than silently losing changes.
                log.info("sync-collection rejected for %s (%s); re-enumerating", calendar_href, exc)
                return SyncCollectionResult(invalid_token=True)
            raise

        result = SyncCollectionResult()
        token_el = root.find("d:sync-token", NS)
        if token_el is not None:
            result.sync_token = token_el.text

        for response in root.findall("d:response", NS):
            href_el = response.find("d:href", NS)
            if href_el is None or not href_el.text:
                continue
            href = unquote(href_el.text)
            status = _text(response, "d:status") or ""
            if "404" in status:
                result.removed.append(href)
                continue
            prop = _ok_prop(response)
            result.changed.append(
                ResourceMeta(href=href, etag=_text(prop, "d:getetag") if prop is not None else None)
            )
        return result

    # ---------------------------------------------------------------- fetching

    def multiget(self, calendar_href: str, hrefs: list[str]) -> MultigetResult:
        """One ``calendar-multiget`` for up to :data:`MULTIGET_BATCH` hrefs.

        A 207 may carry per-href error statuses; those hrefs come back in
        ``failed`` so the caller can fall back to an individual GET.
        """
        if not hrefs:
            return MultigetResult()

        href_xml = "".join(f"<d:href>{_escape(h)}</d:href>" for h in hrefs)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:calendar-multiget xmlns:d="{DAV}" xmlns:c="{CALDAV}">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            f"{href_xml}</c:calendar-multiget>"
        )
        root = self._report(calendar_href, body)

        result = MultigetResult()
        seen: set[str] = set()
        for response in root.findall("d:response", NS):
            href_el = response.find("d:href", NS)
            if href_el is None or not href_el.text:
                continue
            href = unquote(href_el.text)
            seen.add(href)

            prop = _ok_prop(response)
            data = prop.find("c:calendar-data", NS) if prop is not None else None
            if prop is None or data is None or not data.text:
                result.failed.append(href)
                continue
            result.resources.append(
                FetchedResource(href=href, etag=_text(prop, "d:getetag"), ics=data.text)
            )

        # A server that silently omits an href we asked for is a failure too.
        result.failed.extend(h for h in hrefs if h not in seen)
        return result

    def get(self, href: str) -> FetchedResource:
        """A single GET.  Used only for the adopt-probe and conflict refetch."""
        response = self._request("GET", href, headers={"Accept": "text/calendar"})
        if response.status_code == 404:
            raise NotFound(f"GET {href}: 404")
        if response.status_code >= 400:
            raise CalDAVError(f"GET {href}: {response.status_code}", status=response.status_code)
        return FetchedResource(href=href, etag=response.headers.get("ETag"), ics=response.text)

    def get_etag(self, calendar_href: str, href: str) -> str | None:
        """A single-href multiget for ``getetag`` — the post-PUT refresh."""
        result = self.multiget(calendar_href, [href])
        return result.resources[0].etag if result.resources else None

    # ----------------------------------------------------------------- writing

    def put_create(self, href: str, ics: str) -> WriteResult:
        """``If-None-Match: *``.  The collision signal is **412**, not 409."""
        return self._put(href, ics, {"If-None-Match": "*"})

    def put_update(self, href: str, ics: str, base_etag: str) -> WriteResult:
        """``If-Match: base_etag``.  412 → conflict, 404 → server-deleted."""
        return self._put(href, ics, {"If-Match": base_etag})

    def _put(self, href: str, ics: str, guard: dict[str, str]) -> WriteResult:
        headers = {"Content-Type": "text/calendar; charset=utf-8", **guard}
        response = self._request("PUT", href, data=ics.encode("utf-8"), headers=headers)

        if response.status_code == 412:
            raise PreconditionFailed(f"PUT {href}: 412")
        if response.status_code == 404:
            raise NotFound(f"PUT {href}: 404")
        if response.status_code == 409:
            raise CollectionMissing(f"PUT {href}: 409")
        if response.status_code >= 400:
            raise CalDAVError(f"PUT {href}: {response.status_code}", status=response.status_code)

        return WriteResult(
            status=response.status_code,
            etag=response.headers.get("ETag"),
            location=response.headers.get("Location"),
        )

    def delete(self, href: str, base_etag: str | None = None) -> WriteResult:
        """Guarded when ``base_etag`` is known, unconditional when it is NULL."""
        headers = {"If-Match": base_etag} if base_etag else {}
        response = self._request("DELETE", href, headers=headers)

        if response.status_code == 412:
            raise PreconditionFailed(f"DELETE {href}: 412")
        # 404 is SUCCESS, not a conflict: both sides agree it is gone.
        if response.status_code not in (200, 202, 204, 404) and response.status_code >= 400:
            raise CalDAVError(f"DELETE {href}: {response.status_code}", status=response.status_code)
        return WriteResult(status=response.status_code)


# ------------------------------------------------------------------ XML utils


def _parse_xml(payload: bytes) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CalDAVError(f"malformed XML response: {exc}") from exc


def _ok_prop(response: ET.Element) -> ET.Element | None:
    """The ``<prop>`` from the ``2xx`` propstat, ignoring 404 propstats.

    Servers routinely return one propstat per status; picking the first
    unconditionally reads a 404 propstat's empty prop as real data.
    """
    fallback: ET.Element | None = None
    for propstat in response.findall("d:propstat", NS):
        status = (propstat.findtext("d:status", "", NS) or "").strip()
        prop = propstat.find("d:prop", NS)
        if prop is None:
            continue
        if " 200 " in status or status.endswith(" 200 OK"):
            return prop
        if fallback is None and not status:
            fallback = prop
    return fallback


def _text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    found = element.find(path, NS)
    if found is None:
        return None
    text = (found.text or "").strip()
    return text or None


def _supports_vtodo(prop: ET.Element) -> bool:
    comp_set = prop.find("c:supported-calendar-component-set", NS)
    if comp_set is None:
        # The property is optional; a server that omits it is not telling us
        # the collection has no VTODOs.  Keep it and let the comp-filter sort
        # it out.
        return True
    names = {c.get("name", "").upper() for c in comp_set.findall("c:comp", NS)}
    return "VTODO" in names


def _supports_sync_collection(prop: ET.Element) -> bool:
    report_set = prop.find("d:supported-report-set", NS)
    if report_set is None:
        return False
    return any(report.tag == f"{{{DAV}}}sync-collection" for report in report_set.iter())


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
