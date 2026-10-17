import { callApi } from "../../apiWrite.js";
import type { InstallStarted, ReleasesView, VersionChoice } from "../../generated/contract";
import type { Answer } from "../answer";

/* Releases on Central: every observed release and every Player's version (Q2 A, 2026-10-16). A
 * refusal is an InstallRefusal code (said with INSTALL_REFUSAL_WORDS), `http_<status>` for a body
 * outside its shape, or `unreachable` for a lost request. */

const RELEASES = "/v1/operator/releases";

/** Every release Central observed, every Player's version and the default (GET …/releases). */
export function readReleases(): Promise<Answer<"view", ReleasesView>> {
  return callApi("view", RELEASES, { method: "GET" }) as Promise<Answer<"view", ReleasesView>>;
}

/** Update the wall: every Player's version becomes `body.version`; nothing reboots (PUT …/releases/wall). */
export function setWallVersion(body: VersionChoice): Promise<Answer<"view", ReleasesView>> {
  return callApi("view", `${RELEASES}/wall`, { method: "PUT", body }) as Promise<Answer<"view", ReleasesView>>;
}

/** Install `body.version` on one Player only, which restarts onto it (POST …/players/{serial}/install). */
export function installOnPlayer(serial: string, body: VersionChoice): Promise<Answer<"started", InstallStarted>> {
  return callApi("started", `/v1/operator/players/${encodeURIComponent(serial)}/install`, { method: "POST", body }) as
    Promise<Answer<"started", InstallStarted>>;
}
