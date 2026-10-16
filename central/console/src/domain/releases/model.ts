import { type InstallRefusal } from "../../generated/contract";

/*
 * Releases in the console's words: a version per Player (Q2 A, 2026-10-16). Pure: no React, no
 * request. The Releases page and a Player's Install say Central's refusals with these words.
 */

/** Why Central would not put a release on a Player (contract InstallRefusal). */
export const INSTALL_REFUSAL_WORDS: Record<InstallRefusal, string> = {
  unknown_player: "Central does not know this Player.",
  unknown_version: "Central's list of releases has no such release.",
  not_deployable: "Central cannot put this release on a Player: its upload was refused.",
};
