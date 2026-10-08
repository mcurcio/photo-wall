"""The Node API library: the one package that imports the NATS client (decision 0017 C5).

It imports `contracts`, the NATS client and the stdlib, and nothing else (pyproject's import
contracts). Its own modules point down (pyproject's "The Node API library points down"): `node | hub`
over `documents` over `buffers | pull | envelope` over `epoch`, and siblings never import each other.

- `node`: the Node-role session: attach (apply the slice, create the WALL mirror for a wall reader,
  re-put state and birth, register methods), the event outbox, state, method calls, and read-only
  views of its desired bucket and of WALL.
- `hub`: Central's NodeLink per Node and pipe (reconcile, drain into Central's store, assert
  Central's documents, call, discover) and WallWriter (WALL re-created past every mirror).
- `documents`: a desired bucket's or WALL's writer, bound to the stream's own key table and epoch:
  it refuses its own over-budget write and writes only on condition; and its read (value, writer, token).
- `buffers`: every stream, KV bucket and mirror, circular or sticky; self-describing line streams, a
  component's `Slice` inside its store line, and `apply` (prune, purge, shrink, grow, create).
- `pull`: the one way to pull from a consumer, one byte budget per connection below the server's
  pending limit; consumer liveness; the cursor reader that keeps its own (epoch, sequence).
- `envelope`: the only place the headers are built or read (message id, schema major, writer).
- `epoch`: stream epochs and the tokens and cursors that live inside one.
"""
