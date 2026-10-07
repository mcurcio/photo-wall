"""The Node API library: the one package that imports the NATS client (decision 0017 C5).

It imports `contracts`, the NATS client and the stdlib, and nothing else (pyproject's import
contracts). E3a's tracer moved the bus rules here from its test harness, so a shipped caller and
the tests build every buffer the same way (erratum E-W1-TD-S1); E3b grows the sessions, the
envelope and the typed handles around them.

- `buffers`: every stream, KV bucket and mirror, circular or sticky, and the class table that
  declares a Node's whole store at once.
- `documents`: sticky documents' writer (Central's, which refuses its own over-budget write) and
  reader (which finds a missing document), and the epoch-scoped tokens and cursors.
- `pull`: the one way to pull from a consumer, one byte budget per connection below the server's
  pending limit.
"""
