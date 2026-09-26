"""GET /v1/locate: Central's identity, decoded with the device's own parser (the drift guard),
and the strict parsers underneath it."""

import http.client

import pytest
from fastapi.testclient import TestClient

from contracts.central_identity import (
    LOCATE_PATH,
    MAX_IDENTITY_BYTES,
    CentralIdentity,
    identity_body,
    parse_identity,
)
from contracts.strict_json import loads_object
from contracts.time import ManualClock
from tests.test_central_health import FakeCoordinator, FakeDatabase, app_with
from tests.tls_fixture import central_stub, serve_stub


@pytest.fixture
def client(monkeypatch):
    app = app_with(FakeDatabase(healthy=False), FakeCoordinator(), ManualClock(1000), monkeypatch,
                   enabled=False)
    with TestClient(app) as client:
        yield client


def test_locate_answers_the_identity_even_when_central_is_unhealthy(client):
    assert client.get("/healthz").status_code == 503
    response = client.get(LOCATE_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert parse_identity(response.content) == CentralIdentity(1)


def test_locate_ignores_a_query_and_needs_no_credential(client):
    response = client.get(LOCATE_PATH + "?serial=ignored", headers={"Authorization": "Bearer x"})
    assert (response.status_code, response.content) == (200, identity_body())


def test_the_shared_test_stand_in_serves_centrals_exact_identity(client):
    with serve_stub(central_stub()) as stub:
        connection = http.client.HTTPConnection("127.0.0.1", stub.port, timeout=5)
        connection.request("GET", LOCATE_PATH)
        response = connection.getresponse()
        assert (response.status, response.read()) == (200, client.get(LOCATE_PATH).content)
        connection.close()
        connection.request("GET", "/v1/netboot/base")     # other paths go to the base handler
        assert connection.getresponse().status == 404
        connection.close()
    assert stub.requests == [LOCATE_PATH, "/v1/netboot/base"]


def test_identity_body_is_the_documented_wire_form():
    assert identity_body() == b'{"service":"photo-wall-central","api":1}'


@pytest.mark.parametrize(("body", "identity"), [
    (b'{"service":"photo-wall-central","api":1}', CentralIdentity(1)),
    (b'{"api": 2, "service": "photo-wall-central"}', CentralIdentity(2)),   # a newer Central
    (b'{"service":"photo-wall-central","api":1,"build":"x"}', CentralIdentity(1)),
    (b'{"service":"photo-wall-central","api":true}', None),                 # a bool is not an int
    (b'{"service":"photo-wall-central","api":0}', None),
    (b'{"service":"photo-wall-central","api":"1"}', None),
    (b'{"service":"photo-wall-central","api":1.0}', None),
    (b'{"service":"photo-wall","api":1}', None),
    (b'{"api":1}', None),
    (b'{"status":"ok","database":true,"protocol":1}', None),                # /healthz is not it
    (b'{"service":"photo-wall-central","service":"photo-wall-central","api":1}', None),
    (b'{"service":"photo-wall-central","api":1,"pad":"' + b"x" * MAX_IDENTITY_BYTES + b'"}',
     None),
    (b"<html>not found</html>", None),
    (b"", None),
])
def test_parse_identity(body, identity):
    assert parse_identity(body) == identity


@pytest.mark.parametrize("data", [
    b'{"a":1,"a":2}',                         # duplicate keys
    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}',
    b'{"a":1e999}',                           # overflows to Infinity
    b'[1,2]', b'"text"', b"null",             # not an object
    b'{"a":1',                                # truncated
    b'\xff{"a":1}',                           # not UTF-8
    '{"a":1}'.encode("utf-16"),               # UTF-16, which json.loads(bytes) would accept
    b'\xef\xbb\xbf{"a":1}',                   # a byte-order mark
    b"[" * 100_000 + b"]" * 100_000,          # nesting deep enough to exhaust recursion
])
def test_loads_object_refuses_anything_but_a_strict_utf8_object(data):
    assert loads_object(data, max_bytes=len(data)) is None


def test_loads_object_bounds_the_input_before_parsing():
    data = b'{"a":1}'
    assert loads_object(data, max_bytes=len(data)) == {"a": 1}
    assert loads_object(data, max_bytes=len(data) - 1) is None
