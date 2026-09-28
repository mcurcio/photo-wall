/**
 * Per-tab session state shared by the Plane A reader (useSnapshot.js) and the
 * write helper (apiWrite.js), held here so neither imports the other.
 *
 * The admin bearer token is held in memory only for this tab (never persisted) —
 * the same discipline as the legacy operator page. It defaults to empty, and the
 * shell renders its empty state until the token form calls setToken().
 *
 * The write fence (design pass 2 §7) is a counter apiWrite bumps at the START and
 * again at the COMPLETION of every non-GET call. A Plane A refresh records it
 * when it starts and drops its result if it moved, so a read that overlapped a
 * write — in flight, or finished while the read ran — never lands over the
 * write's effect.
 */

let adminToken = "";
let writes = 0;

/** Set the admin bearer token used by every Plane A / mutation fetch. */
export function setToken(token) {
  adminToken = token || "";
}

/** The admin bearer token currently held for this tab. */
export function getToken() {
  return adminToken;
}

/** Move the write fence: called when a write starts and when it completes. */
export function noteWrite() {
  writes += 1;
}

/** The write-fence counter; a change means a write started or completed. */
export function writeCount() {
  return writes;
}
