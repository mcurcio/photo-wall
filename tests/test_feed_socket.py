"""The kernel node feed socket: reader allowlist, one request per accept, the reply bound."""
import json
import os
import socket
import stat
import sys
import time
from uuid import uuid4

import pytest

from appliance import feed_socket
from appliance.feed_socket import RESPONSE_BOUND, FeedListener

LINUX = sys.platform.startswith("linux")
KIND = socket.SOCK_SEQPACKET if LINUX else socket.SOCK_STREAM
READERS = frozenset({0, 10006})


def echo(request: dict) -> dict:
    if request.get("refuse"):
        raise ValueError("refused_here")
    return {"echo": request}


def make(tmp_path, *, answer=echo, max_reply=4096, peer=None, name="feed"):
    directory = tmp_path / name
    directory.mkdir(mode=0o750)
    uid = {"value": 0}
    listener = FeedListener(directory / "feed.sock", owner_uid=os.getuid(), group=os.getgid(),
                            readers=READERS, answer=answer, max_reply=max_reply,
                            peer=peer or (lambda connection: uid["value"]), kind=KIND)
    return listener, uid


@pytest.fixture
def served(tmp_path):
    listener, uid = make(tmp_path)
    yield listener, uid
    listener.close()


def read(listener, request: bytes, *, refused=False):
    client = socket.socket(socket.AF_UNIX, listener.listener.type)
    client.settimeout(2)
    try:
        client.connect(str(listener.path))
        client.sendall(request)
        listener.serve()
        try:
            raw = client.recv(1 << 20)
        except ConnectionResetError:
            # A refused reader's connection is closed with its request unread; Linux AF_UNIX
            # then reports ECONNRESET instead of EOF (E-B7-6). Either way it received no byte.
            if not refused:
                raise
            raw = b""
    finally:
        client.close()
    return json.loads(raw) if raw else None


def test_the_socket_is_owner_and_group_only(served):
    listener, _ = served
    info = listener.path.lstat()
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o660
    assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid())


def test_each_reader_gets_the_answer_in_the_accepted_envelope(served):
    listener, uid = served
    for reader in sorted(READERS):
        uid["value"] = reader
        assert read(listener, b'{"after":3}') == {"accepted": True, "echo": {"after": 3}}


def test_any_other_uid_is_closed_unread_with_no_reply(tmp_path):
    seen = []
    listener, uid = make(tmp_path, answer=lambda request: seen.append(request) or {})
    try:
        for other in (10004, 10005, 10003, 65534, 1000):
            uid["value"] = other
            assert read(listener, b'{"after":0}', refused=True) is None
    finally:
        listener.close()
    assert seen == []  # the answer never ran for a non-reader


def test_a_refusal_or_a_malformed_request_is_a_reason_only(served):
    listener, _ = served
    assert read(listener, b'{"refuse":true}') == {"accepted": False, "reason": "refused_here"}
    for raw in (b"[]", b"not json", b'{"a":1,"a":2}', b"{" + b" " * 4096 + b"}"):
        assert read(listener, raw) == {"accepted": False, "reason": "feed_read_request"}


def test_a_reply_over_the_bound_is_replaced_by_response_bound(tmp_path):
    listener, _ = make(tmp_path, answer=lambda request: {"blob": "x" * request["size"]},
                       max_reply=200)
    try:
        envelope = len(json.dumps({"accepted": True, "blob": ""}, separators=(",", ":")))
        at_bound = read(listener, json.dumps({"size": 200 - envelope}).encode())
        assert at_bound["accepted"] is True and len(at_bound["blob"]) == 200 - envelope
        over = read(listener, json.dumps({"size": 201 - envelope}).encode())
        assert over == json.loads(RESPONSE_BOUND) == {"accepted": False,
                                                      "reason": "response_bound"}
    finally:
        listener.close()


def test_identities_in_an_answer_are_sent_as_strings(tmp_path):
    identity = uuid4()
    listener, _ = make(tmp_path, answer=lambda request: {"id": identity})
    try:
        assert read(listener, b"{}") == {"accepted": True, "id": str(identity)}
    finally:
        listener.close()


def test_serving_never_waits_for_an_absent_reader(served):
    listener, _ = served
    started = time.monotonic()
    listener.serve()
    assert time.monotonic() - started < 0.05


def test_a_stale_socket_is_replaced_but_a_foreign_file_or_open_directory_is_not(tmp_path):
    directory = tmp_path / "feed"
    directory.mkdir(mode=0o750)
    arguments = dict(owner_uid=os.getuid(), group=os.getgid(), readers=READERS, answer=echo,
                     max_reply=4096, kind=KIND)
    (directory / "feed.sock").write_text("not a socket")
    with pytest.raises(ValueError, match="^feed_socket_ownership$"):
        FeedListener(directory / "feed.sock", **arguments)
    (directory / "feed.sock").unlink()
    first = FeedListener(directory / "feed.sock", **arguments)
    first.listener.close()  # a crashed publisher leaves its socket behind
    with FeedListener(directory / "feed.sock", **arguments) as second:
        assert second.path.exists()
    assert not (directory / "feed.sock").exists()  # closing removes its own socket
    directory.chmod(0o770)
    with pytest.raises(ValueError, match="^feed_directory$"):
        FeedListener(directory / "feed.sock", **arguments)
    directory.chmod(0o750)
    with pytest.raises(ValueError, match="^feed_directory$"):
        FeedListener(directory / "feed.sock", **{**arguments, "owner_uid": os.getuid() + 1})


@pytest.mark.skipif(not LINUX, reason="SO_PEERCRED is Linux-only")
def test_peer_uid_is_the_kernel_credential():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with first, second:
        assert feed_socket.peer_uid(first) == os.getuid()
