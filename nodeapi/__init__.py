"""The Node API library: the one package that imports the NATS client (decision 0017 C5).

It imports `contracts`, the NATS client and the stdlib, and nothing else (pyproject's import
contracts). Its own modules point down (pyproject's "The Node API library points down"): `node | hub`
over `documents` over `buffers | pull | envelope` over `epoch`, and siblings never import each other.

- `node`: the Node-role session: attach (apply the slice, re-put state and birth, register methods),
  the event outbox, state, method calls.
- `hub`: Central's NodeLink per Node and pipe: reconcile, drain into Central's store, call, discover.
- `documents`: sticky documents' writer (Central's, which refuses its own over-budget write) and
  reader (which finds a missing document).
- `buffers`: every stream, KV bucket and mirror, circular or sticky; self-describing line streams, a
  component's `Slice` inside its store line and `apply`; the wave-1 class table until E3b retires it.
- `pull`: the one way to pull from a consumer, one byte budget per connection below the server's
  pending limit; consumer liveness; the cursor reader that keeps its own (epoch, sequence).
- `envelope`: the only place the headers are built or read (message id, schema major, writer).
- `epoch`: stream epochs and the tokens and cursors that live inside one.
"""
