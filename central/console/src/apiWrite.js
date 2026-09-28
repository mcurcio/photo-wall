import { getToken, noteWrite } from "./session.js";

// Every operator write shares one timeout budget (design §3): a write that does
// not resolve inside this window is aborted rather than left hanging.
const TIMEOUT_MS = 15000;

/**
 * Low-level operator-write helper (bead R-apiwrite). Every operator mutation —
 * bind/unbind, calibration, and the frame writes (create/move/delete/drop) —
 * shares the SAME scaffolding: a bearer-authenticated `fetch` with a 15s abort
 * budget, a JSON body when one is supplied, and a normalized result whether the
 * server answered 2xx or not. It deliberately does NOT map per-endpoint error
 * codes to operator copy: each caller keeps its own interpret/message table
 * (bind's 409 conflict, calibration's §4b tokens, delete's §9a guard), because
 * those tables differ and are individually load-bearing.
 *
 * When `body` is supplied it is JSON-encoded and a `Content-Type` header is set;
 * when it is omitted (e.g. DELETE frame) neither is sent, matching the requests
 * the endpoints issued before this extraction. The response body is parsed as
 * JSON best-effort on BOTH paths — an empty/non-JSON body yields `data: null`
 * and, on failure, `error: null` — so a caller reads one shape regardless.
 *
 * @param {string} path
 * @param {{method: string, body?: any}} options
 * @returns {Promise<{ok: boolean, status: number, error: string|null, data: any}>}
 */
export async function apiWrite(path, { method, body } = {}) {
  const headers = { Authorization: "Bearer " + getToken() };
  const init = { method, headers, signal: AbortSignal.timeout(TIMEOUT_MS) };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  // The write fence (session.js) moves at the start and the completion of every
  // non-GET call; a GET routed through here is a read and does not move it.
  const isWrite = (method ?? "GET").toUpperCase() !== "GET";
  if (isWrite) {
    noteWrite();
  }
  let response;
  let data = null;
  try {
    response = await fetch(path, init);
    try {
      data = await response.json();
    } catch {
      // A non-JSON / empty body (e.g. a 204 or a network-level error page) leaves
      // data null; callers treat that as "no code" exactly as the hand-rolled
      // per-endpoint parsers did.
      data = null;
    }
  } finally {
    if (isWrite) {
      noteWrite();
    }
  }
  return {
    ok: response.ok,
    status: response.status,
    error: response.ok ? null : (data?.error ?? null),
    data,
  };
}
