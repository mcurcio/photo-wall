import { apiWrite } from "../../apiWrite.js";
import type { PaperProfilesView, PaperProfileView } from "../../generated/contract";

/* The paper profiles' reads and writes on Central (central/paper_profile_routes.py): Settings › Paper
 * profiles lists, adds and removes; a Frame's lighting picks its paper's profile from the list
 * (decision 0028). A refusal is a PaperProfilesRefusal code, `http_<status>` for a body outside its
 * shape, or `unreachable` for a lost request. */

const PAPER_PROFILES = "/v1/operator/paper-profiles";

/** An ICC profile's media type: the upload's body is the file as chosen. */
const ICC_PROFILE = "application/vnd.iccprofile";

/** Central's answer to an add: `added` is false when the same profile was already on the list. */
export type AddAnswer = { ok: true; profile: PaperProfileView; added: boolean } | { ok: false; error: string };

/** Central's answer to a remove: a refusal names the Frames whose paper uses it, when one does. */
export type RemoveAnswer = { ok: true } | { ok: false; error: string; frames: string[] };

type Sent = Awaited<ReturnType<typeof apiWrite>>;

const refusal = (sent: Sent): string => sent.error ?? `http_${sent.status}`;

/** Every paper profile on the list (GET …/paper-profiles). */
export async function listPaperProfiles(): Promise<{ ok: true; view: PaperProfilesView } | { ok: false; error: string }> {
  try {
    const sent = await apiWrite(PAPER_PROFILES, { method: "GET" });
    return sent.ok ? { ok: true, view: sent.data as PaperProfilesView } : { ok: false, error: refusal(sent) };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/** Add a paper profile (POST …/paper-profiles, the .icc file's bytes): Central checks it and bakes
 * its LUT once. */
export async function addPaperProfile(profile: Blob): Promise<AddAnswer> {
  try {
    const sent = await apiWrite(PAPER_PROFILES, { method: "POST", body: profile, contentType: ICC_PROFILE });
    return sent.ok ? { ok: true, profile: sent.data as PaperProfileView, added: sent.status === 201 }
      : { ok: false, error: refusal(sent) };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/** Remove a paper profile no Frame's paper uses (DELETE …/paper-profiles/{profile_sha256}). */
export async function removePaperProfile(profileSha256: string): Promise<RemoveAnswer> {
  try {
    const sent = await apiWrite(`${PAPER_PROFILES}/${encodeURIComponent(profileSha256)}`, { method: "DELETE" });
    return sent.ok ? { ok: true }
      : { ok: false, error: refusal(sent), frames: (sent.data as { frames?: string[] } | null)?.frames ?? [] };
  } catch {
    return { ok: false, error: "unreachable", frames: [] };
  }
}
