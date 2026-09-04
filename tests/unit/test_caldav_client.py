"""The CalDAV client, against mocked HTTP."""

from __future__ import annotations

import pytest
import responses

from davpunk.core.caldav_client import (
    MAX_RESPONSE_BYTES,
    MULTIGET_BATCH,
    TIMEOUTS,
    CalDAVClient,
    CalDAVError,
    CollectionMissing,
    NotFound,
    PreconditionFailed,
    Unauthorized,
    _parse_xml,
    batch_hrefs,
    same_origin,
)
from davpunk.models.remote import Remote

BASE = "https://cal.example.test/dav/"


@pytest.fixture
def remote():
    return Remote(id="work", url=BASE, username="user")


@pytest.fixture
def client(remote):
    c = CalDAVClient(remote, "hunter2")
    yield c
    c.close()


def multistatus(*responses_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/" '
        'xmlns:ical="http://apple.com/ns/ical/">' + "".join(responses_xml) + "</d:multistatus>"
    )


def collection(href, *, name="Tasks", comps=("VTODO",), ctag="ctag-1", sync=True):
    comp_xml = "".join(f'<c:comp name="{c}"/>' for c in comps)
    report = (
        "<d:supported-report-set><d:supported-report><d:report>"
        + ("<d:sync-collection/>" if sync else "<d:expand-property/>")
        + "</d:report></d:supported-report></d:supported-report-set>"
    )
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat>"
        "<d:prop>"
        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        f"<d:displayname>{name}</d:displayname>"
        "<ical:calendar-color>#4A9EFF</ical:calendar-color>"
        f"<cs:getctag>{ctag}</cs:getctag>"
        f"<c:supported-calendar-component-set>{comp_xml}"
        "</c:supported-calendar-component-set>"
        f"{report}"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def register_discovery(*collections_xml: str):
    responses.add(
        "PROPFIND",
        BASE,
        status=207,
        body=multistatus(
            "<d:response><d:href>/dav/</d:href><d:propstat><d:prop>"
            "<d:current-user-principal><d:href>/dav/user/</d:href>"
            "</d:current-user-principal></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        ),
    )
    responses.add(
        "PROPFIND",
        "https://cal.example.test/dav/user/",
        status=207,
        body=multistatus(
            "<d:response><d:href>/dav/user/</d:href><d:propstat><d:prop>"
            "<c:calendar-home-set><d:href>/dav/user/cals/</d:href></c:calendar-home-set>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        ),
    )
    responses.add(
        "PROPFIND",
        "https://cal.example.test/dav/user/cals/",
        status=207,
        body=multistatus(*collections_xml),
    )


# ------------------------------------------------------------------ discovery


@responses.activate
def test_discovery_walks_principal_then_home_then_collections(client):
    register_discovery(collection("/dav/user/cals/tasks/"))
    found = client.discover()

    assert len(found) == 1
    assert found[0].href == "/dav/user/cals/tasks/"
    assert found[0].display_name == "Tasks"
    assert found[0].color == "#4A9EFF"
    assert found[0].ctag == "ctag-1"
    assert found[0].supports_sync is True


@responses.activate
def test_a_collection_without_vtodo_support_is_skipped(client):
    """A calendar full of VEVENTs is not ours."""
    register_discovery(
        collection("/dav/user/cals/events/", name="Events", comps=("VEVENT",)),
        collection("/dav/user/cals/tasks/", name="Tasks"),
    )
    assert [c.href for c in client.discover()] == ["/dav/user/cals/tasks/"]


@responses.activate
def test_a_collection_that_declares_both_is_kept(client):
    register_discovery(collection("/dav/user/cals/mixed/", comps=("VEVENT", "VTODO")))
    assert len(client.discover()) == 1


@responses.activate
def test_a_missing_component_set_is_not_read_as_a_refusal(client):
    """The property is optional; omitting it does not mean "no VTODOs here"."""
    xml = collection("/dav/user/cals/tasks/")
    xml = xml.replace(
        '<c:supported-calendar-component-set><c:comp name="VTODO"/>'
        "</c:supported-calendar-component-set>",
        "",
    )
    register_discovery(xml)
    assert len(client.discover()) == 1


@responses.activate
def test_supports_sync_is_false_without_sync_collection(client):
    register_discovery(collection("/dav/user/cals/tasks/", sync=False))
    assert client.discover()[0].supports_sync is False


@responses.activate
def test_a_non_calendar_collection_is_skipped(client):
    register_discovery(
        "<d:response><d:href>/dav/user/cals/</d:href><d:propstat><d:prop>"
        "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
        collection("/dav/user/cals/tasks/"),
    )
    assert len(client.discover()) == 1


@responses.activate
def test_a_404_propstat_is_not_read_as_data(client):
    """Servers return one propstat per status; reading the first blindly picks
    up a 404 propstat's empty prop."""
    xml = (
        "<d:response><d:href>/dav/user/cals/tasks/</d:href>"
        "<d:propstat><d:prop><cs:getctag/></d:prop>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        "<d:propstat><d:prop>"
        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        "<d:displayname>Tasks</d:displayname>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )
    register_discovery(xml)
    found = client.discover()
    assert len(found) == 1
    assert found[0].display_name == "Tasks"


# ----------------------------------------------------------------- ctag probe


@responses.activate
def test_get_ctag(client):
    responses.add(
        "PROPFIND",
        "https://cal.example.test/dav/user/cals/tasks/",
        status=207,
        body=multistatus(
            "<d:response><d:href>/dav/user/cals/tasks/</d:href><d:propstat><d:prop>"
            "<cs:getctag>ctag-7</cs:getctag><d:sync-token>tok-7</d:sync-token>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        ),
    )
    assert client.get_ctag("/dav/user/cals/tasks/") == ("ctag-7", "tok-7")


# ---------------------------------------------------------------- enumeration


@responses.activate
def test_calendar_query_returns_hrefs_and_etags(client):
    responses.add(
        "REPORT",
        "https://cal.example.test/dav/user/cals/tasks/",
        status=207,
        body=multistatus(
            *[
                f"<d:response><d:href>/dav/user/cals/tasks/{n}.ics</d:href>"
                f'<d:propstat><d:prop><d:getetag>"e{n}"</d:getetag></d:prop>'
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                for n in ("a", "b")
            ]
        ),
    )
    metas = client.list_vtodo_etags("/dav/user/cals/tasks/")
    assert [(m.href, m.etag) for m in metas] == [
        ("/dav/user/cals/tasks/a.ics", '"ea"'),
        ("/dav/user/cals/tasks/b.ics", '"eb"'),
    ]


@responses.activate
def test_calendar_query_filters_on_vtodo(client):
    responses.add(
        "REPORT", "https://cal.example.test/dav/user/cals/tasks/", status=207, body=multistatus()
    )
    client.list_vtodo_etags("/dav/user/cals/tasks/")
    body = responses.calls[0].request.body.decode()
    assert '<c:comp-filter name="VTODO"/>' in body
    assert '<c:comp-filter name="VCALENDAR">' in body


@responses.activate
def test_sync_collection_splits_changed_from_removed(client):
    responses.add(
        "REPORT",
        "https://cal.example.test/dav/user/cals/tasks/",
        status=207,
        body=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:">'
            "<d:response><d:href>/dav/user/cals/tasks/a.ics</d:href>"
            '<d:propstat><d:prop><d:getetag>"ea"</d:getetag></d:prop>'
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "<d:response><d:href>/dav/user/cals/tasks/b.ics</d:href>"
            "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
            "<d:sync-token>tok-2</d:sync-token></d:multistatus>"
        ),
    )
    result = client.sync_collection("/dav/user/cals/tasks/", "tok-1")

    assert [m.href for m in result.changed] == ["/dav/user/cals/tasks/a.ics"]
    assert result.removed == ["/dav/user/cals/tasks/b.ics"]
    assert result.sync_token == "tok-2"
    assert result.invalid_token is False


@responses.activate
def test_a_rejected_sync_token_asks_for_a_full_re_enumeration(client):
    """Losing changes silently would be far worse than one expensive cycle."""
    responses.add(
        "REPORT", "https://cal.example.test/dav/user/cals/tasks/", status=507, body="gone"
    )
    result = client.sync_collection("/dav/user/cals/tasks/", "stale-token")
    assert result.invalid_token is True
    assert result.changed == [] and result.removed == []


# ------------------------------------------------------------------ multiget


@responses.activate
def test_multiget_returns_etag_and_calendar_data(client):
    ics = "BEGIN:VCALENDAR\nBEGIN:VTODO\nUID:a\nEND:VTODO\nEND:VCALENDAR\n"
    responses.add(
        "REPORT",
        "https://cal.example.test/dav/user/cals/tasks/",
        status=207,
        body=multistatus(
            "<d:response><d:href>/dav/user/cals/tasks/a.ics</d:href><d:propstat><d:prop>"
            f'<d:getetag>"ea"</d:getetag><c:calendar-data>{ics}</c:calendar-data>'
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        ),
    )
    result = client.multiget("/dav/user/cals/tasks/", ["/dav/user/cals/tasks/a.ics"])

    assert len(result.resources) == 1
    assert result.resources[0].etag == '"ea"'
    assert "UID:a" in result.resources[0].ics
    assert result.failed == []


@responses.activate
def test_a_per_href_error_inside_a_207_is_reported_as_failed(client):
    """The caller falls back to an individual GET, then to the next cycle."""
    responses.add(
        "REPORT",
        "https://cal.example.test/dav/user/cals/tasks/",
        status=207,
        body=multistatus(
            "<d:response><d:href>/dav/user/cals/tasks/a.ics</d:href>"
            "<d:propstat><d:prop/><d:status>HTTP/1.1 404 Not Found</d:status>"
            "</d:propstat></d:response>"
        ),
    )
    result = client.multiget("/dav/user/cals/tasks/", ["/dav/user/cals/tasks/a.ics"])
    assert result.resources == []
    assert result.failed == ["/dav/user/cals/tasks/a.ics"]


@responses.activate
def test_an_href_the_server_silently_omitted_counts_as_failed(client):
    responses.add(
        "REPORT", "https://cal.example.test/dav/user/cals/tasks/", status=207, body=multistatus()
    )
    result = client.multiget("/dav/user/cals/tasks/", ["/a.ics", "/b.ics"])
    assert sorted(result.failed) == ["/a.ics", "/b.ics"]


def test_batching_is_fifty_at_a_time():
    """200 changed hrefs are 4 batches, not 200 GETs."""
    hrefs = [f"/dav/user/cals/tasks/{i}.ics" for i in range(200)]
    batches = batch_hrefs(hrefs)
    assert len(batches) == 4
    assert all(len(b) <= MULTIGET_BATCH for b in batches)
    assert sum(len(b) for b in batches) == 200


def test_an_empty_multiget_makes_no_request(client):
    assert client.multiget("/dav/user/cals/tasks/", []).resources == []


# -------------------------------------------------------------------- create


@responses.activate
def test_put_create_sends_if_none_match_star(client):
    """A bare PUT is an unconditional replace; this is the correct create."""
    responses.add("PUT", f"{BASE}tasks/a.ics", status=201, headers={"ETag": '"e1"'})
    result = client.put_create("/dav/tasks/a.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

    assert responses.calls[0].request.headers["If-None-Match"] == "*"
    assert result.status == 201
    assert result.etag == '"e1"'


@responses.activate
def test_a_create_collision_is_412_not_409(client):
    responses.add("PUT", f"{BASE}tasks/a.ics", status=412)
    with pytest.raises(PreconditionFailed):
        client.put_create("/dav/tasks/a.ics", "x")


@responses.activate
def test_409_means_the_parent_collection_is_missing(client):
    """WebDAV 409 is not a create collision; it must not be retried per task."""
    responses.add("PUT", f"{BASE}tasks/a.ics", status=409)
    with pytest.raises(CollectionMissing):
        client.put_create("/dav/tasks/a.ics", "x")


@responses.activate
def test_a_location_header_is_surfaced(client):
    responses.add(
        "PUT",
        f"{BASE}tasks/a.ics",
        status=201,
        headers={"ETag": '"e1"', "Location": "/dav/tasks/server-chosen.ics"},
    )
    result = client.put_create("/dav/tasks/a.ics", "x")
    assert result.location == "/dav/tasks/server-chosen.ics"


@responses.activate
def test_a_2xx_without_an_etag_is_not_an_error(client):
    """Common: the RFC does not require an ETag on the response."""
    responses.add("PUT", f"{BASE}tasks/a.ics", status=204)
    assert client.put_create("/dav/tasks/a.ics", "x").etag is None


# -------------------------------------------------------------------- update


@responses.activate
def test_put_update_sends_if_match(client):
    responses.add("PUT", f"{BASE}tasks/a.ics", status=204, headers={"ETag": '"e2"'})
    client.put_update("/dav/tasks/a.ics", "x", '"e1"')
    assert responses.calls[0].request.headers["If-Match"] == '"e1"'


@responses.activate
def test_update_412_is_a_conflict_signal(client):
    responses.add("PUT", f"{BASE}tasks/a.ics", status=412)
    with pytest.raises(PreconditionFailed):
        client.put_update("/dav/tasks/a.ics", "x", '"e1"')


@responses.activate
def test_update_404_means_the_server_deleted_it(client):
    responses.add("PUT", f"{BASE}tasks/a.ics", status=404)
    with pytest.raises(NotFound):
        client.put_update("/dav/tasks/a.ics", "x", '"e1"')


# -------------------------------------------------------------------- delete


@responses.activate
def test_delete_is_guarded_when_a_base_etag_is_known(client):
    responses.add("DELETE", f"{BASE}tasks/a.ics", status=204)
    client.delete("/dav/tasks/a.ics", '"e1"')
    assert responses.calls[0].request.headers["If-Match"] == '"e1"'


@responses.activate
def test_delete_is_unguarded_when_the_base_etag_is_null(client):
    responses.add("DELETE", f"{BASE}tasks/a.ics", status=204)
    client.delete("/dav/tasks/a.ics", None)
    assert "If-Match" not in responses.calls[0].request.headers


@responses.activate
def test_delete_404_is_success_not_a_conflict(client):
    """Both sides agree it is gone."""
    responses.add("DELETE", f"{BASE}tasks/a.ics", status=404)
    assert client.delete("/dav/tasks/a.ics", '"e1"').status == 404


@responses.activate
def test_delete_412_is_a_conflict(client):
    responses.add("DELETE", f"{BASE}tasks/a.ics", status=412)
    with pytest.raises(PreconditionFailed):
        client.delete("/dav/tasks/a.ics", '"e1"')


# ------------------------------------------------------------------- errors


@responses.activate
def test_401_is_distinguishable_so_it_can_trigger_a_re_decrypt(client):
    responses.add("GET", f"{BASE}tasks/a.ics", status=401)
    with pytest.raises(Unauthorized):
        client.get("/dav/tasks/a.ics")


@responses.activate
def test_a_5xx_carries_its_status(client):
    responses.add("GET", f"{BASE}tasks/a.ics", status=503)
    with pytest.raises(CalDAVError) as excinfo:
        client.get("/dav/tasks/a.ics")
    assert excinfo.value.status == 503


@responses.activate
def test_malformed_xml_is_a_caldav_error_not_a_traceback(client):
    responses.add(
        "PROPFIND", "https://cal.example.test/dav/user/cals/tasks/", status=207, body="<not xml"
    )
    with pytest.raises(CalDAVError, match="malformed XML"):
        client.get_ctag("/dav/user/cals/tasks/")


@responses.activate
def test_a_connection_failure_is_wrapped(client):
    import requests

    responses.add("GET", f"{BASE}tasks/a.ics", body=requests.ConnectionError("network unreachable"))
    with pytest.raises(CalDAVError, match="failed"):
        client.get("/dav/tasks/a.ics")


# ------------------------------------------------------------------ transport


def test_timeouts_are_ten_and_thirty_seconds():
    assert TIMEOUTS == (10, 30)


@responses.activate
def test_timeouts_are_applied_to_every_request(client, monkeypatch):
    seen = {}
    original = client._session.request

    def record(method, url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return original(method, url, **kwargs)

    monkeypatch.setattr(client._session, "request", record)
    responses.add("GET", f"{BASE}tasks/a.ics", status=200, body="ics")
    client.get("/dav/tasks/a.ics")
    assert seen["timeout"] == TIMEOUTS


def test_verify_tls_is_honoured():
    remote = Remote(id="self-signed", url=BASE, username="u", verify_tls=False)
    client = CalDAVClient(remote, "pw")
    try:
        assert client._session.verify is False
    finally:
        client.close()


def test_basic_auth_is_configured(client):
    assert client._session.auth.username == "user"
    assert client._session.auth.password == "hunter2"


# ------------------------------------------------- hostile-server hardening


@pytest.mark.parametrize(
    "href",
    [
        "https://attacker.example/steal",  # absolute, another host
        "//attacker.example/steal",  # protocol-relative, same trick
        "http://cal.example.test/dav/x",  # scheme downgrade off TLS
        "https://cal.example.test.attacker.example/x",  # suffix, not the host
    ],
)
def test_an_href_that_leaves_the_origin_is_refused(client, href):
    """The password rides on every request; the server does not get to say where.

    ``urljoin`` honours an absolute reference, so without this check a single
    ``<d:href>`` in a 207 would put the account's HTTP Basic credentials on a
    request to a host the user never configured.
    """
    with pytest.raises(CalDAVError, match="same origin"):
        client._url(href)


@pytest.mark.parametrize(
    "href",
    ["item.ics", "/dav/other/item.ics", "https://cal.example.test/dav/item.ics"],
)
def test_ordinary_hrefs_still_resolve(client, href):
    assert client._url(href).startswith("https://cal.example.test/")


def test_an_explicit_default_port_is_the_same_origin():
    """A server may answer with :443 spelled out; that is not an origin change."""
    assert same_origin("https://cal.example.test/dav/", "https://cal.example.test:443/dav/x")
    assert not same_origin("https://cal.example.test/dav/", "https://cal.example.test:8443/dav/x")


def test_the_host_comparison_ignores_case():
    assert same_origin("https://Cal.Example.Test/dav/", "https://cal.example.test/dav/x")


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<d:multistatus xmlns:d="DAV:"><d:response>&lol2;</d:response></d:multistatus>"""


def test_an_entity_bomb_is_refused_before_it_expands():
    with pytest.raises(CalDAVError, match="DOCTYPE"):
        _parse_xml(BILLION_LAUGHS)


def test_a_doctype_after_a_comment_or_pi_is_still_caught():
    """The prolog may hold an XML declaration, comments and PIs first."""
    payload = (
        b'<?xml version="1.0"?><!-- a comment --><?target data?>'
        b'<!DOCTYPE x [ <!ENTITY e "boom"> ]><x/>'
    )
    with pytest.raises(CalDAVError, match="DOCTYPE"):
        _parse_xml(payload)


def test_a_task_that_merely_mentions_a_doctype_still_parses():
    """Only the prolog is inspected, so escaped body text is not a false positive."""
    payload = multistatus(
        "<d:response><d:href>/dav/x.ics</d:href>"
        "<d:propstat><d:status>HTTP/1.1 200 OK</d:status>"
        "<d:prop><d:displayname>write &lt;!DOCTYPE html&gt; first</d:displayname>"
        "</d:prop></d:propstat></d:response>"
    ).encode()
    root = _parse_xml(payload)
    assert root.findtext(".//{DAV:}displayname") == "write <!DOCTYPE html> first"


def test_a_utf8_bom_does_not_hide_a_doctype():
    with pytest.raises(CalDAVError, match="DOCTYPE"):
        _parse_xml(b"\xef\xbb\xbf" + BILLION_LAUGHS)


def test_an_oversized_body_is_refused():
    with pytest.raises(CalDAVError, match="over the"):
        _parse_xml(b"<x/>" + b" " * MAX_RESPONSE_BYTES)


def test_an_ordinary_response_parses(client):
    assert _parse_xml(multistatus().encode()).tag == "{DAV:}multistatus"
