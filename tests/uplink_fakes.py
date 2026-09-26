"""Scripted uplink doubles shared by the uplink and stage-1 tests: a Reply that serves a body in
bounded reads, a Transport that answers each URL from a script, and `located`, which builds a
LocatedCentral the only way one can be built, through locate()."""

from collections.abc import Mapping

from contracts.central_identity import LOCATE_PATH, identity_body
from uplink.causes import UplinkError
from uplink.locate import LocatedCentral, locate
from uplink.origin import Origin, Url


class FakeReply:
    """A scripted Reply: serves `body` in reads of at most `step` bytes, then raises `fail` (if
    given) instead of reporting the end."""

    def __init__(self, status: int, *, location: str | None = None, body: bytes = b"",
                 peer: str = "192.0.2.1", step: int = 64,
                 headers: Mapping[str, str] | None = None,
                 fail: UplinkError | None = None) -> None:
        self.status, self.peer, self.body, self.step, self.fail = status, peer, body, step, fail
        self.headers = dict(headers or {})
        if location is not None:
            self.headers["Location"] = location
        self.reads: list[tuple[int, float]] = []
        self.closed = False

    def read(self, amount: int, *, timeout: float) -> bytes:
        self.reads.append((amount, timeout))
        if not self.body and self.fail is not None:
            raise self.fail
        chunk = self.body[:min(amount, self.step)]
        self.body = self.body[len(chunk):]
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    """Answers each URL from a script; records every request it was asked to send."""

    def __init__(self, script: dict[str, FakeReply | UplinkError]) -> None:
        self.script = script
        self.sent: list[tuple[str, dict[str, str], float]] = []

    def send(self, url: Url, *, headers, deadline: float) -> FakeReply:
        self.sent.append((str(url), dict(headers), deadline))
        answer = self.script[str(url)]
        if isinstance(answer, UplinkError):
            raise answer
        return answer


def central(**kwargs) -> FakeReply:
    """Central's answer to GET /v1/locate."""
    return FakeReply(200, body=identity_body(), **kwargs)


def located(origin: str) -> LocatedCentral:
    """A LocatedCentral for `origin` (a root URL), located directly with no redirect."""
    root = Origin.parse_root(origin)
    return locate(root, transport=FakeTransport({str(root.url(LOCATE_PATH)): central()}))
