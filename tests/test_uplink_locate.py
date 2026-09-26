"""locate over a scripted Transport: the chain, the final answer, the deadline, the body bound,
on_hop, and LocatedCentral's construction guarantee."""

import copy
import dataclasses

import pytest

from contracts.central_identity import MAX_IDENTITY_BYTES, CentralIdentity, identity_body
from tests.uplink_fakes import FakeReply, FakeTransport, central
from uplink.causes import Cause, UplinkError
from uplink.locate import LOCATE_DEADLINE, LOCATE_HEADERS, LocatedCentral, locate
from uplink.origin import Origin
from uplink.transport import HOP_TIMEOUT


class Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


ROOT = Origin.parse_root("http://photo-wall.localdomain/")
FIRST = "http://photo-wall.localdomain/v1/locate"
FINAL = "https://photo-wall.example/v1/locate"


def refused(script, root=ROOT) -> UplinkError:
    with pytest.raises(UplinkError) as caught:
        locate(root, transport=FakeTransport(script))
    return caught.value


def test_the_located_origin_is_the_final_url_not_the_root():
    redirect, answer = FakeReply(301, location=FINAL, body=b"<html>moved</html>"), central()
    transport = FakeTransport({FIRST: redirect, FINAL: answer})
    located = locate(ROOT, transport=transport)
    assert located.origin == Origin("https", "photo-wall.example", 443) != ROOT
    assert located.identity == CentralIdentity(1)
    assert [str(hop) for hop in located.hops] == [FIRST, FINAL]
    assert [sent[0] for sent in transport.sent] == [FIRST, FINAL]
    assert redirect.reads == [] and redirect.closed and answer.closed   # no redirect body read


def test_every_request_carries_only_the_locate_headers():
    transport = FakeTransport({FIRST: central()})
    locate(ROOT, transport=transport)
    assert transport.sent[0][1] == dict(LOCATE_HEADERS) == {
        "Accept": "application/json", "Accept-Encoding": "identity"}


def test_on_hop_reports_every_hop_with_its_status_and_peer():
    hops = []
    transport = FakeTransport({FIRST: FakeReply(308, location=FINAL, peer="192.0.2.7"),
                               FINAL: central(peer="192.0.2.8")})
    locate(ROOT, transport=transport, on_hop=lambda url, status, peer:
           hops.append((str(url), status, peer)))
    assert hops == [(FIRST, 308, "192.0.2.7"), (FINAL, 200, "192.0.2.8")]


@pytest.mark.parametrize(("reply", "expected"), [
    (FakeReply(404, body=b'{"detail":"Not Found"}'), (Cause.NOT_CENTRAL, "status")),
    (FakeReply(401), (Cause.NOT_CENTRAL, "status")),
    (FakeReply(200, body=b'{"status":"ok","protocol":1}'), (Cause.NOT_CENTRAL, "body")),
    (FakeReply(200, body=b"<html>gateway</html>"), (Cause.NOT_CENTRAL, "body")),
    (FakeReply(502, body=b"<html>bad gateway</html>"), (Cause.HTTP, "status")),
    (FakeReply(503, body=identity_body()), (Cause.HTTP, "status")),
])
def test_a_final_answer_without_an_identity_is_named(reply, expected):
    error = refused({FIRST: reply})
    assert (error.cause, error.reason) == expected
    assert (error.host, error.detail) == ("photo-wall.localdomain", f"status={reply.status}")
    assert reply.closed


def test_the_identity_body_is_read_to_at_most_one_byte_over_its_bound():
    padded = b'{"service":"photo-wall-central","api":1,"pad":"' + b"x" * 10_000 + b'"}'
    reply = FakeReply(200, body=padded)
    assert refused({FIRST: reply}).reason == "body"
    assert len(padded) - len(reply.body) == MAX_IDENTITY_BYTES + 1   # consumed, then stopped


def test_one_deadline_bounds_the_whole_chain_and_every_body_read():
    clock, answer = Clock(100.0), central()
    transport = FakeTransport({FIRST: FakeReply(301, location=FINAL), FINAL: answer})
    original = transport.send

    def send(url, **kwargs):
        clock.now += 12.0                   # each hop takes 12 s
        return original(url, **kwargs)

    transport.send = send
    locate(ROOT, transport=transport, monotonic=clock)
    assert [deadline for _, _, deadline, _ in transport.sent] == [100.0 + LOCATE_DEADLINE] * 2
    assert [bound for *_, bound in transport.sent] == [HOP_TIMEOUT] * 2   # locate keeps the hop
    assert answer.reads[0][1] == pytest.approx(100.0 + LOCATE_DEADLINE - 124.0)


def test_a_transport_failure_propagates_unchanged():
    failure = UplinkError(Cause.DNS, "failed", host="photo-wall.example")
    error = refused({FIRST: FakeReply(301, location=FINAL), FINAL: failure})
    assert error is failure


def test_nothing_is_sent_to_a_refused_redirect():
    root = Origin.parse_root("https://photo-wall.example/")
    transport = FakeTransport({FINAL: FakeReply(302, location="http://b/v1/locate")})
    with pytest.raises(UplinkError) as caught:
        locate(root, transport=transport)
    assert (caught.value.reason, caught.value.host) == ("downgrade", "b")
    assert [sent[0] for sent in transport.sent] == [FINAL]


def hosts(count: int) -> list[str]:
    return [f"https://h{index}/v1/locate" for index in range(count)]


def test_ten_redirects_then_central_is_located():
    chain = [FIRST, *hosts(10)]
    script = {url: FakeReply(301, location=chain[i + 1]) for i, url in enumerate(chain[:-1])}
    script[chain[-1]] = central()
    assert str(locate(ROOT, transport=FakeTransport(script)).origin) == "https://h9"


def test_the_eleventh_redirect_is_refused_and_its_target_never_contacted():
    chain = [FIRST, *hosts(11)]
    script = {url: FakeReply(301, location=chain[i + 1]) for i, url in enumerate(chain[:-1])}
    transport = FakeTransport(script)
    with pytest.raises(UplinkError) as caught:
        locate(ROOT, transport=transport)
    assert (caught.value.reason, caught.value.host) == ("limit", "h10")
    assert len(transport.sent) == 11 and chain[-1] not in [sent[0] for sent in transport.sent]


@pytest.fixture
def located() -> LocatedCentral:
    return locate(ROOT, transport=FakeTransport({FIRST: central()}))


def test_a_located_central_cannot_be_built_outside_locate(located):
    with pytest.raises(TypeError):
        LocatedCentral(ROOT, CentralIdentity(1), (), proof=object())
    with pytest.raises(TypeError):
        LocatedCentral(ROOT, CentralIdentity(1), ())       # no proof at all


def test_a_located_central_cannot_be_replaced_or_changed(located):
    other = Origin.parse_root("https://elsewhere.example/")
    with pytest.raises(TypeError):
        dataclasses.replace(located, origin=other)
    if hasattr(copy, "replace"):                          # Python 3.13, the device's runtime
        with pytest.raises(TypeError):
            copy.replace(located, origin=other)
    for name in ("origin", "_origin", "anything"):
        with pytest.raises(AttributeError):
            setattr(located, name, other)
    with pytest.raises(AttributeError):
        del located._origin
    assert located.origin == ROOT
