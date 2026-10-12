import type { LibraryConnectionView, LibraryNote, LibraryProblem } from "./library-health-card";

/** A fixed Central time, so every story renders the same times. */
export const AT = 1791147840;

/** A connection named Family whose last check found `problem`, an hour after it first appeared. */
export function connectionWith(problem: LibraryProblem): LibraryConnectionView {
  return {
    connection_ref: "library-0123456789ab",
    name: "Family",
    url: "http://immich.local:2283/api",
    key_hint: "…1234",
    immich_user: { user_ref: "5b0f3c0e-3d1c-4a8a-9f6e-2a8c1f0f7a11", name: "Anna", email: "anna@example.com" },
    health: { state: "needs_a_look", problem, since: AT - 3600, checked_at: AT },
    counts: { photos: 41203, videos: 2110, albums: 212, people: 96 },
    required_permissions: ["asset.read", "asset.view", "album.read", "person.read"],
    managed: false,
  };
}

/** The same connection, connected, its last check passing with `notes`. */
export function connectedWith(...notes: LibraryNote[]): LibraryConnectionView {
  return { ...connectionWith({ kind: "key_rejected" }), health: { state: "ok", checked_at: AT, notes } };
}

/** The same connection, connected. */
export const CONNECTED: LibraryConnectionView = connectedWith();

/** The Immich the deployment sets (Central's environment), connected: shown read-only. */
export const MANAGED: LibraryConnectionView = { ...CONNECTED, name: "Immich", managed: true };
