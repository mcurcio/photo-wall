# Operator Console Pass 2, Pass A: Stay Signed In

**Date:** 2026-09-28 · **Status:** design-gate artifact, awaiting owner approval.
**Builds on:** [the approved console design](operator-console-ux-design.md) (D-c: one admin token, no roles; modes are organization, not permission) and [slice 1](operator-console-ux-pass2.md) (`session.js`, the write fence, the 5 s poll).
**Layer:** two operator routes, a wider admin check and a no-store rule on Central; the console's token form becomes a sign-in screen. No migration. Player, enrollment, media and netboot authentication do not change.
**Size:** 3 beads (1 backend, 1 console, 1 docs), about 330 net production lines and 520 test lines.

## 1. The problem in plain words

| Today | Evidence | Cost |
|---|---|---|
| The admin token lives in one JavaScript variable, never persisted | `session.js:16-24`; `App.jsx:141-150` | Every reload or new tab asks for the token again. |
| Every read and write sends the token as a bearer header | `useSnapshot.js:18-21`, `apiWrite.js:28` | The long-lived secret crosses the LAN every 5 s, in clear text over http, and any script in the page can read it. |
| Central accepts only the bearer header | `app.py:279-283` | A browser cannot keep a sign-in. |
| The bearer check compares `str` values | `app.py:282` | `Authorization: Bearer éé` raises `TypeError` in `secrets.compare_digest` (probe: "comparing strings with non-ASCII characters is not supported"), so Central answers **500** instead of 401. |

**Owner decisions (binding):** sign in once per browser with a cookie; the sign-in lasts 30 days; a **Log out** button; the cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` when the console is served over https.

## 2. One picture, three rules

```mermaid
sequenceDiagram
  participant B as Browser (console)
  participant C as Central
  B->>C: POST /v1/operator/session {token}, Origin, X-Photo-Wall-Console
  C-->>B: 204, Set-Cookie: signed value binding expiry and Origin
  loop every 5 s while visible
    B->>C: GET /v1/operator/inventory (cookie sent by the browser)
    C-->>B: 200, or 401 then the sign-in screen
  end
  B->>C: PUT/POST/PATCH/DELETE /v1/operator/... cookie, same Origin, X-Photo-Wall-Console
  B->>C: DELETE /v1/operator/session, X-Photo-Wall-Console
  C-->>B: 204, clearing Set-Cookie for both names (§5)
```

1. **The cookie never holds the token.** It holds an expiry, the sign-in Origin and a MAC. The key comes from the admin token through a slow KDF, so changing the token signs everyone out and a captured cookie cannot be used to guess the token offline.
2. **A Bearer credential decides alone.** A non-empty `Bearer` credential that matches is allowed; one that does not match gets 401 even with a valid cookie. Other schemes and an empty `Bearer` are ignored, and the cookie is checked.
3. **A write authorized by the cookie must come from the page that signed in.** It needs the same `Origin` stored in the cookie, `Sec-Fetch-Site: same-origin` when that header is sent, and `X-Photo-Wall-Console`.

## 3. Glossary

| Term | Means | Is not |
|---|---|---|
| **Admin token** | `PHOTO_WALL_ADMIN_TOKEN` (at least 32 characters, `app.py:127-129`; `scripts/configure.py:17` generates 64 hex characters) | Stored in any browser |
| **Session key** | 32 bytes derived **once, at app construction** with `hashlib.scrypt(token UTF-8, salt=b"photo-wall/operator-session/v1", n=2**15, r=8, p=1, maxmem=64 MiB, dklen=32)`: 32 MiB of memory and about 60 ms, measured on the development Mac | Stored anywhere; per request |
| **Session value** | The cookie's content (§4) | A token, or a database row |
| **Bound Origin** | The sign-in request's `Origin` (`scheme://host[:port]`), carried inside the MAC | Compared with `Host`; proxies rewrite `Host`, not `Origin` |
| **Marked request** | A request carrying `X-Photo-Wall-Console`, with any value | Proof of identity |

## 4. Decision table: what the cookie holds

| Option | Rotation signs everyone out? | Migration | Logout ends the session on the server? | Captured cookie reveals |
|---|---|---|---|---|
| The raw admin token | yes | none | no | **the token**: every route, forever, including scripts |
| **MAC session value, scrypt key (chosen)** | **yes: the key derives from the token** | **none** | **no: it clears this browser only** | until expiry, reads from anywhere and writes only from the bound Origin; not the token, because each offline guess costs a full scrypt |
| Server-side session row | yes, if rows are tied to the token's hash | one table | yes | the same, revocable per session |

**Format:** `v1.<expires_at>.<nonce>.<origin>.<mac>`, total at most 512 bytes. The parts are:
- `expires_at`: 10 ASCII digits of unix seconds.
- `nonce`: 16 random bytes, base64url (22 characters).
- `origin`: the bound Origin as base64url, 1 to 344 characters.
- `mac`: HMAC-SHA256 under the session key over every byte before the last `.`, base64url (43 characters).

**The codec is total: verification returns the session or nothing, and never raises.** The steps, in order:
1. Match the whole value against one ASCII-only regex (`re.ASCII`, explicit `[0-9]` and `[A-Za-z0-9_-]` classes; no `\d`, so Unicode digits fail).
2. Recompute the MAC over the raw prefix bytes and compare it in constant time **before parsing anything**.
3. Parse `expires_at` and decode `origin`.
4. Accept only if `now < expires_at ≤ now + 30 days`, using the injected `Clock` (`contracts/time.py:9-11`).

A value that is malformed, oversized, wrongly signed, expired or too far in the future gets **401 `unauthorized`, never 422 or 500**.

**The key:** it is memoized per process by token, so the test suite's many apps (37 `create_app(` sites) pay for it once per distinct token. Every Central process derives the same key, so any process accepts a value minted by another, with no shared state.

**What this gives up:** Log out cannot end a copied cookie before it expires. The only way to end every session is to rotate the token (§9).

## 5. Protocol contracts

| Route | Auth | Request | Responses (all `Cache-Control: no-store`) |
|---|---|---|---|
| `POST /v1/operator/session` | none; must be marked; `Origin` required; `Sec-Fetch-Site`, when sent, must be `same-origin` | JSON `{"token": str}` | **204** plus `Set-Cookie`; **401 `unauthorized`** for a wrong token; **403 `request_unmarked`** or **`origin_mismatch`** (also sent when `Origin` is missing); **422 `invalid_request`**, which never echoes the body (`app.py:302-305`) |
| `DELETE /v1/operator/session` | none; must be marked | none | **204** plus two clearing `Set-Cookie` headers (below), idempotent; **403 `request_unmarked`** |
| every other `/v1/operator/*` | `admin` (below) | unchanged | unchanged, plus **403 `request_unmarked` / `origin_mismatch`**; all now `no-store` |

**The `admin` dependency, in order:**
1. **Bearer.** If `Authorization` uses the `Bearer` scheme with a non-empty credential, compare it with the token as UTF-8 **bytes** using `secrets.compare_digest`. The result is final.
2. **Cookie.** Otherwise verify the `__Host-` cookie if it is present, or else the plain one. No usable cookie gets 401.
3. **Writes.** For any method other than GET, HEAD or OPTIONS:
   - no marker gets 403 `request_unmarked`;
   - an `Origin` that differs from the bound Origin gets 403 `origin_mismatch`;
   - a `Sec-Fetch-Site` that is sent and is not `same-origin` gets 403 `origin_mismatch`.

`fastapi.security.HTTPBearer(auto_error=False)` already returns nothing for other schemes and for an empty `Bearer` (probe: `get_authorization_scheme_param("Bearer ")` gives `("Bearer", "")`). A-1 pins both cases with tests.

**One compare for everything.** A-1 fixes the non-ASCII 500: the bearer path and sign-in share one byte compare.

**Players never see cookies.** The `player` dependency and the WebSocket and media bearer checks (`app.py:285-288,425-428,471-479`) never read a cookie. A test sends a valid operator cookie to `/v1/player/config` and asserts 401.

**No-store.** A path-prefix response hook sets `Cache-Control: no-store` on every `/v1/operator/*` response, including error responses from the exception handlers (`app.py:290-309`).

**Cookie attributes (no `Domain` on either name).** The name and `Secure` follow the scheme of the sign-in `Origin`.

| Scheme | Issued `Set-Cookie` | Clearing `Set-Cookie` (Log out sends both) |
|---|---|---|
| https | `__Host-photo_wall_session=<value>; Path=/; Max-Age=2592000; Secure; HttpOnly; SameSite=Strict` | `__Host-photo_wall_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict` |
| http | `photo_wall_session=<value>; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict` | `photo_wall_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict`, plus `Secure` when the logout request's `Origin` is https |

- **`__Host-`** makes the browser accept the cookie only with `Secure`, `Path=/` and no `Domain`, so no sibling host or http page can plant or shadow it.
- **Distinct names** mean an http sign-in never collides with an https one; browsers refuse to let an http page overwrite a `Secure` cookie of the same name.
- **`Max-Age`** is relative, so the browser's clock does not matter; the server's `expires_at` is the authority.
- **The page load and `SameSite=Strict`:** the console page does not depend on the cookie; the page's own fetches send it.
- **Where the scheme comes from:** from the browser's `Origin`, not `request.url.scheme`. Uvicorn trusts `X-Forwarded-Proto` only from `127.0.0.1` by default and `Dockerfile:129` sets no `--forwarded-allow-ips`, so behind a TLS-terminating proxy Central sees `http`.

## 6. CSRF: what each layer stops

Five operator POSTs take no body (`app.py:548,556,633,668,725`; retire and run operations among them). A plain form POST could reach them, so `SameSite` alone is not enough.

| Layer | Stops | Does not stop |
|---|---|---|
| `SameSite=Strict` | cross-**site** requests | same-site hosts: another host under the same registrable domain, or another port where same-site checks ignore the scheme |
| `X-Photo-Wall-Console` | any form, and any cross-origin fetch, while Central answers no CORS preflight | a cross-origin page if a proxy **reflects CORS**, allowing the origin, credentials and headers |
| **Bound Origin plus `Sec-Fetch-Site`** | writes from any other origin, **including through a CORS-reflecting proxy**, because that page's `Origin` is not the bound one | a script already running on the bound origin (see the failure table) |

- **Rejected: an Origin/Host comparison.** Proxies rewrite `Host` (nginx sends the upstream name by default), and trusting `X-Forwarded-Host` needs per-deployment proxy trust. The bound Origin needs neither: it compares the browser's view with the browser's view.
- **`Sec-Fetch-Site` is a bonus layer.** Browsers send it only to secure origins, so over http it is absent.
- **Scripts are unaffected.** A request with a Bearer credential needs no marker and no Origin, so `curl` (runbook release and pin examples), `scripts/demo_wall.py:785` and `scripts/test_netboot_e2e.py:341` keep working.

## 7. Console changes

```mermaid
stateDiagram-v2
  [*] --> Checking: page load (first Plane A read)
  Checking --> SignedIn: 2xx
  Checking --> SignedOut: 401
  Checking --> Checking: network or 5xx (existing failure notice, next poll retries)
  SignedOut --> SigningIn: submit token
  SigningIn --> Checking: 204
  SigningIn --> SignedOut: 401 "token not accepted"
  SignedIn --> SignedOut: Plane A 401 "signed out: expired or the token changed"
  SignedIn --> SignedOut: Log out (DELETE, then snapshot cleared)
```

- **`session.js`** stops holding the token: `setToken` and `getToken` are removed, and the write fence (`noteWrite`, `writeCount`) is unchanged. It exports the marker header, which `useSnapshot.fetchJson` and `apiWrite` send in place of `Authorization`. These are the only two operator fetch sites (`useSnapshot.js:18`, `apiWrite.js:43`). The browser adds `Origin` to same-origin writes by itself.
- **Sign-in screen:** the "Operator token" field and a **Sign in** button replace the header form. The field is cleared on submit. If a 204 is followed by a 401, the screen says "Your browser did not keep the sign-in; allow cookies for this site."
- **Signed-in state** comes from Plane A: `authRejected` becomes `signedIn`/`signedOut`, and the mount effect and poll gate become "not signed out". `bootFacts.js` never signs anyone out (`bootFacts.js:13`). A write that gets 401 shows its own error, and the next poll (within 5 s) shows the sign-in screen.
- **Log out:** a header button sends `DELETE /v1/operator/session`, clears Plane A and shows the sign-in screen.
- **403 `request_unmarked` / `origin_mismatch`:** "Central refused this write because it did not come from the page you signed in on. Reload the console from the address you signed in at, or sign in again." A proxy that strips headers produces the same message.
- **Unchanged:** the write fence, the 5 s cadence, the pause while the tab is hidden, and the dropping of superseded reads.

## 8. Failure table

| Failure | What happens | Recovery | Guarantee |
|---|---|---|---|
| Session reaches 30 days | 401 on the next poll, then the sign-in screen | Sign in | server-side expiry check (test) |
| Admin token rotated | Every value fails its MAC; every tab shows the sign-in screen within 5 s | Sign in with the new token | the key is derived from the token at construction |
| Cookie forged, malformed, oversized, has Unicode digits, or has its expiry pushed out | 401, never 422 or 500 | none | total codec; MAC checked before parsing (tests) |
| Cookie captured on the wire (http) or copied from disk | Reads from anywhere and writes from the bound Origin until it expires; the token stays out of reach | Rotate the token | **stated cost**; scrypt makes guessing the token from the cookie cost a full scrypt per guess |
| Non-ASCII bearer | 401 (today 500) | none | byte compare (test) |
| Script injected into the console | Can act as the operator, cannot read the cookie or the token | Fix the injection; CSP `script-src 'self'` stays (`app.py:375`) | `HttpOnly` (browser) |
| Invalid Bearer sent with a valid cookie | 401 | Fix the script | dependency order (test) |
| Cross-origin page, including through a CORS-reflecting proxy | 403 `origin_mismatch` or `request_unmarked` | none | bound Origin and marker (tests) |
| **Cookie tossing over http:** a same-site host or on-path attacker plants `photo_wall_session` | Without the token it can only plant an invalid value (401, so a forced sign-out) or its own valid one | Sign in again; prefer https | **stated cost**; `__Host-` removes this over https |
| Operator opens the console at another address (IP instead of name) | Reads work; writes get 403 `origin_mismatch` | Sign in at that address | stated cost of the Origin binding |
| Browser blocks cookies | The sign-in 204 is followed by 401; the screen says so | Allow cookies | console copy (browser test) |
| Central's wall clock jumps | Sessions end early (forward jump), or a backward jump can push `expires_at` beyond `now + 30 days`, which then gets 401 | Sign in again | stated cost |
| Rolling restart while the token changes | Brief 401s until every process runs the new token | Sign in again | stated cost |
| Session expires while a draft is open | Unsaved drafts are lost (as today) | Re-enter | stated cost (Question 4) |

**Also not covered:**
- **Sign-in has no rate limit.** This is the same as the bearer path today; a generated token is 256 bits.
- **No HSTS.** A first visit over http can be downgraded. Deferred (Question 3).

## 9. Rotation and "log out everywhere"

To rotate, change `PHOTO_WALL_ADMIN_TOKEN` in `.env` (or the deployment secret) and restart every Central process. Every session ends. **Scripts break until they get the new token:** the runbook's `curl` examples, `scripts/demo_wall.py` (`DEMO_ADMIN_TOKEN`) and the netboot end-to-end harness. **Players do not break:** they authenticate with their own enrollment credentials (`app.py:285-288`). This is the only "log out everywhere"; the runbook will say so (bead A-3).

## 10. Tracer bullet, beads and tests

**Tracer bullet (bead A-1's first test):** sign in with `Origin: http://testserver`; `GET /v1/operator/inventory` with only the cookie returns 200; a marked `POST` with the same Origin succeeds and with another Origin gets 403; a second app on the same database with a different token returns 401 for the same cookie. It proves the codec, the key, the dependency order, the Origin binding and rotation. **Non-goals:** the console UI, sliding renewal, per-session revocation, roles, rate limiting and HSTS.

| Bead | Scope | Estimate |
|---|---|---|
| **A-1 backend** | total codec with the scrypt key; the `admin` dependency (§5) with the byte compare, which fixes the non-ASCII 500; the two session routes; the no-store hook; a route-table test that every `/v1/operator/*` route except the two session routes depends on `admin` | ≈180 production, ≈370 test |
| **A-2 console** | `session.js`, `useSnapshot`, `apiWrite`, sign-in screen, Log out, 403 copy; the five browser files' repeated sign-in steps (`test_operator_binding_browser.py:51-52` and 4 others) collapse into one harness helper | ≈150 production, ≈150 test |
| **A-3 docs** | runbook sign-in, rotation and its script breakage, CSRF layers and the `origin_mismatch` remedy; README line 57; errata; ledger rows | ≈60 doc |

**pytest (A-1):**
- **Issued headers:** the exact attributes for https (`__Host-`, `Secure`, `Path=/`, no `Domain`) and for http.
- **Clearing headers:** the exact attributes of both clearing headers, including `Max-Age=0`.
- **Sign-in refusals:** a wrong token gets 401 with no cookie; a missing Origin or a cross-site `Sec-Fetch-Site` gets 403.
- **Cookie writes:** a cookie read works; a write gets 403 without the marker, 403 with another Origin, 403 with `Sec-Fetch-Site: same-site`, and 2xx when all checks pass.
- **Bearer:** reads and writes work with no marker; an invalid Bearer with a valid cookie gets 401; a `Basic` header or an empty `Bearer` falls through to the cookie; a non-ASCII Bearer gets 401.
- **Codec:** it rejects values that are malformed, oversized (over 512 bytes), non-ASCII, have Unicode-digit expiries, are tampered, are expired, or have `expires_at > now + 30 days`. Each gets 401.
- **Rotation:** a cookie from before the rotation gets 401.
- **Caching:** `no-store` is on 200, 401, 403 and 422 responses and on sign-in and sign-out.
- **CORS:** a preflight `OPTIONS` returns no `Access-Control-Allow-*` headers.
- **Players:** a cookie on `/v1/player/config` gets 401.

**Browser (A-2):** sign in once, reload and stay signed in; a second tab is signed in too; Log out, reload and stay signed out; advance the harness clock 30 days, or restart with a new token, and see the sign-in screen.

**Mutation probes (verifier; each must turn a test red):** skip the MAC compare; parse before the MAC; derive the key from a constant; drop the expiry check or the future cap; let an invalid Bearer fall through; drop the marker, Origin or `Sec-Fetch-Site` check; compare `str` values again; drop `Secure` or `HttpOnly` from a clearing header; drop the no-store hook; read the cookie in `player`.

## 11. Design it twice

| | **A. Signed stateless cookie (chosen)** | **B. Server-side session table** |
|---|---|---|
| Shape | The cookie value is verified by recomputing a MAC | The cookie holds a random id; the operator dependency looks up a row |
| Gains | No migration; no read per request; works across processes with no shared state; rotation is automatic | Log out ends the session on the server; sessions can be listed and revoked one at a time |
| Costs | A copied cookie lives until it expires; "log out everywhere" means rotating the token; about 60 ms of scrypt at startup | A migration; one database read per operator request (12 a minute per tab); expired rows to clean up; rotation must also delete rows |

A is chosen because the owner prefers no migration and there is one principal, so per-session revocation buys little. B is the upgrade path if roles ever arrive (design Q5).

## 12. Owner questions (each lists its default)

1. **Fixed or sliding 30 days?** Default: **fixed from sign-in**. Sliding means re-issuing the cookie on reads, so every 5 s poll would carry a `Set-Cookie`. Build proceeds on the default.
2. **What does Log out cover?** Default: **this browser only**; "everywhere" means rotating the token (§9). The alternative is option B. Build proceeds on the default.
3. **Should Central send HSTS when served over https?** Default: **no, deferred**. HSTS would pin the whole host to https, including anything served on it over http. Build proceeds on the default.
4. **What happens to open drafts when the session expires?** Default: **they are lost** (as today). The alternative is a sign-in overlay that keeps the console mounted. Build proceeds on the default.

## History

- 2026-09-28, revision 1 (the security review failed revision 0): tokens compared as bytes, fixing the non-ASCII 500; scrypt key derived once per process; total codec with the MAC checked before parsing and a future-expiry cap; writes bound to the sign-in Origin and to `Sec-Fetch-Site` when sent; `__Host-` name over https with `Path=/` and exact clearing headers; the Authorization rule stated precisely; every operator response no-store; costs stated (no rate limit, scripts break on rotation, no HSTS).
