# Authenticated media HTTP gateway

The HTTP gateway adapts [MediaStore read leases](module-media-store.md) to the source-neutral Player protocol. It owns no files, query configuration, credentials, or publication policy. `GET /v1/media/{sha256}` accepts a Player bearer token and delegates exact current-offer/secured authorization to `MediaStore.open_read` before starting the response.

The response has exact Content-Length, prepared media Content-Type, a quoted digest ETag, and private no-store caching. It rejects Range requests explicitly and never redirects or performs upstream requests. Digest paths are strictly validated. No admin token substitutes for Player authority. Each new request obtains a fresh authorization decision; a bounded active descriptor has its existing transfer lease.

At most eight transfers run per server process; excess requests receive a coded 503 without opening another descriptor. Database connections have a five-second connection timeout, with five-second lock and ten-second statement bounds. Deployment uses one server process; multiplying processes also multiplies this transfer capacity.

`MediaResponse` opens and reads on the thread pool, sends at most 64 KiB per chunk, and wraps opening plus the complete ASGI send lifecycle in a 120-second deadline. Cancellation waits for an in-progress local open/read to finish so it can close the returned descriptor; the store also has an independent descriptor timer. Closing occurs in a finally block for successful completion, send failure, cancellation, timeout, and disconnect. Before headers, coded store errors become sanitized HTTP errors. After headers, failure terminates the response without claiming a complete body.

Acceptance checks use real PostgreSQL and generated local bytes for exact headers/body, unauthorized and guessed-digest refusal, Range refusal, binding/epoch invalidation, full-response backpressure timeout and descriptor/reference cleanup. Native media decoding and physical output remain separate validation layers.
