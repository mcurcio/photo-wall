"""Stream epochs and the tokens that live inside one (errata E-W1-TD-5, E-W1-FV-1, E-E3B-R2-3).

A stream sequence (a cursor, a KV revision) means something only inside one creation of its stream.
Each create writes a fresh epoch into the stream's metadata (`buffers`), and a Node stream starts at a
sequence derived from it, so a token from one creation names no message of the next. Every reader and
writer carries `Token(epoch, seq)`. The bottom of `nodeapi`: it imports no other `nodeapi` module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Final, NamedTuple

if TYPE_CHECKING:
    from nats.js.api import StreamInfo
    from nats.js.client import JetStreamContext

EPOCH_KEY: Final = "photo_wall_epoch"   # stream metadata: a fresh uuid at every create


class Token(NamedTuple):
    """A cursor or a conditional-write token: `seq` names a message only inside the stream creation
    `epoch` identifies."""
    epoch: str
    seq: int


def epoch_origin(epoch: str) -> int:
    """The first sequence of a Node stream created with `epoch`: 1 plus 40 bits of the epoch, so two
    creations of one stream share no sequence unless their origins fall within a stream's length."""
    return 1 + int(epoch[:10], 16)


def epoch_of(info: StreamInfo) -> str:
    """The epoch the stream was created with."""
    epoch = (info.config.metadata or {}).get(EPOCH_KEY)
    if not epoch:
        raise ValueError("stream_has_no_epoch")
    return epoch


async def stream_epoch(jetstream: JetStreamContext, stream: str) -> str:
    return epoch_of(await jetstream.stream_info(stream))


def epoch_start(epoch: str) -> Token:
    """A reader's cursor before the first message of a creation: nothing of it read yet."""
    return Token(epoch, epoch_origin(epoch) - 1)
