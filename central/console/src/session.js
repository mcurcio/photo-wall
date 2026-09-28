/**
 * Per-tab session state shared by the Plane A reader (useSnapshot.js) and the
 * write helper (apiWrite.js), held here so neither imports the other.
 *
 * The console holds NO credential (pass A §7). Signing in exchanges the admin
 * token once for an HttpOnly session cookie that the browser keeps and sends on
 * every same-origin fetch; the token is passed straight into that one request
 * and never stored in a variable, storage or the page. Every operator fetch
 * carries the marker header below instead: Central refuses a cookie-authorized
 * write that lacks it (403 `request_unmarked`) or that comes from another Origin
 * (403 `origin_mismatch`).
 *
 * The write fence (design pass 2 §7) is a counter apiWrite bumps at the START and
 * again at the COMPLETION of every non-GET call. A Plane A refresh records it
 * when it starts and drops its result if it moved, so a read that overlapped a
 * write — in flight, or finished while the read ran — never lands over the
 * write's effect.
 */

/** The marker header every operator fetch sends (any value; it is not a secret). */
export const CONSOLE_HEADER = Object.freeze({ "X-Photo-Wall-Console": "1" });

/** The refusal codes of a write that did not come from the signed-in page. */
export const ORIGIN_REFUSALS = new Set(["request_unmarked", "origin_mismatch"]);

let writes = 0;
const refusedListeners = new Set();

/** Move the write fence: called when a write starts and when it completes. */
export function noteWrite() {
  writes += 1;
}

/** The write-fence counter; a change means a write started or completed. */
export function writeCount() {
  return writes;
}

/** Tell every listener Central refused a write for its origin (apiWrite calls this). */
export function noteOriginRefused() {
  for (const listener of refusedListeners) {
    listener();
  }
}

/**
 * Listen for origin refusals; returns the unsubscribe function.
 *
 * @param {() => void} listener
 * @returns {() => void}
 */
export function onOriginRefused(listener) {
  refusedListeners.add(listener);
  return () => {
    refusedListeners.delete(listener);
  };
}
