/** One Central call's answer as the domains' request modules fold it (apiWrite.js `callApi`):
 * `{ok: true, [key]: value}`, or `{ok: false, error}` with Central's refusal code, `http_<status>`
 * for a body outside its shape, or `unreachable` for a lost request. */
export type Answer<K extends string, T> = ({ ok: true } & Record<K, T>) | { ok: false; error: string };
