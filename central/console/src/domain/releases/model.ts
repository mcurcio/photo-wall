import type {
  InstallRefusal, PlayerVersionView, ReleasesView, ReleaseTag, ReleaseView,
} from "../../generated/contract";
import { appHandle, words } from "../../facts.js";
import { gigabytes } from "../../health.js";

/*
 * Releases in the console's words: a version per Player (Q2 A, 2026-10-16). Pure: no React, no
 * request. Central keeps no wall-wide selection: each Player boots its own version, and a Player
 * with none boots the newest ready stable release (`default`). The Releases page, a Player's
 * Install and Update the wall say Central's facts and refusals with these words.
 */

/** Why Central would not put a release on a Player, or on the wall (contract InstallRefusal). */
export const INSTALL_REFUSAL_WORDS: Record<InstallRefusal, string> = {
  unknown_player: "Central does not know this Player.",
  unknown_version: "Central's list of releases has no such release.",
  not_deployable: "Central cannot put this release on a Player: its upload was refused.",
};

/** A refusal or a lost answer in words: an InstallRefusal code's own sentence, else what happened. */
export function refusalWords(error: string): string {
  if (Object.hasOwn(INSTALL_REFUSAL_WORDS, error)) return INSTALL_REFUSAL_WORDS[error as InstallRefusal];
  if (error === "unreachable") return "Central did not answer: the outcome is unknown until the next read.";
  return `Central refused it (${error}).`;
}

/** "1.2 GB", or "less than 0.1 GB". */
export function downloadSize(bytes: number | null): string {
  return Number(bytes) < 1e8 ? "less than 0.1 GB" : `${gigabytes(bytes)} GB`;
}

/** Whether Central may put this release on a Player: its upload was not refused. */
export const deployable = (release: ReleaseView): boolean => release.problem === null;

/** A release's state in one phrase: "Rejected: …", else its readiness; null when Central did not serve it. */
export function readinessWords(release: ReleaseView): string | null {
  if (!deployable(release)) return `Rejected: ${words(release.problem)}`;
  switch (release.readiness) {
    case "ready":
      return "Ready";
    case "downloading":
      return `Downloading · ${downloadSize(release.missing_bytes)} left`;
    case "not_downloaded":
      return "Not downloaded";
    case "failed":
      return `Failed: ${words(release.readiness_reason)}`;
    default:
      return null;
  }
}

/** What a Player that boots this release meets now, said in every confirmation that puts it on. */
export function readinessLine(release: ReleaseView): string {
  const { version } = release;
  switch (release.readiness) {
    case "ready":
      return `Ready: Central has every file of ${version}.`;
    case "downloading":
      return `Downloading: Central still has ${downloadSize(release.missing_bytes)} of ${version} to download; a `
        + "Player that boots it before then waits for the download.";
    case "not_downloaded":
      return `Not downloaded: a Player that boots ${version} waits while Central downloads it.`;
    case "failed":
      return `Failed: Central could not download ${version} (${words(release.readiness_reason)}); a Player that boots `
        + "it waits while Central tries again.";
    default:
      return `Central did not serve whether ${version} is downloaded.`;
  }
}

/** The version a Player boots next: its own, else the default (none chosen). */
export const bootsNext = (player: PlayerVersionView, view: ReleasesView): ReleaseTag | null =>
  player.version ?? (player.chosen ? null : view.default);

/** One release as the Releases page lists it. */
export interface ReleaseRow {
  version: ReleaseTag;
  release: ReleaseView;
  /** Its readiness or why it was rejected; null: Central did not serve the readiness. */
  readiness: string | null;
  /** "Base v1.2.0 · app 9f8e7d…" / "Base v1.2.0 · no app"; null while rejected. */
  contents: string | null;
  prerelease: boolean;
  deployable: boolean;
  /** The Players that boot it next, by name. */
  bootedBy: string[];
  /** Whether a Player with no version of its own boots it. */
  isDefault: boolean;
}

/** Every release Central observed, newest first, each with the Players that boot it next. */
export function releaseRows(view: ReleasesView): ReleaseRow[] {
  return view.releases.map((release) => ({
    version: release.version,
    release,
    readiness: readinessWords(release),
    contents: !deployable(release) || release.base_tag === null ? null
      : `Base ${release.base_tag} · ${release.app_environment_sha256 === null ? "no app" : `app ${appHandle(release.app_environment_sha256)}`}`,
    prerelease: !release.stable,
    deployable: deployable(release),
    bootedBy: view.players.filter((player) => bootsNext(player, view) === release.version).map((player) => player.name),
    isDefault: view.default === release.version,
  }));
}

/** What an install in progress is doing, in words. */
const INSTALL_WORDS: Record<NonNullable<PlayerVersionView["install"]>, (version: string) => string> = {
  downloading: (version) => `Installing ${version}: Central is downloading it`,
  restarting: (version) => `Installing ${version}: the Player is restarting onto it`,
};

/** An install's progress in words, for the version it puts on. */
export const installWords = (install: NonNullable<PlayerVersionView["install"]>, version: string): string =>
  INSTALL_WORDS[install](version);

/** One Player as the Releases page lists it. */
export interface PlayerVersionRow {
  serial: string;
  name: string;
  playerId: string | null;
  /** The release it boots next: "v1.2.0", "v1.2.0 · newest ready" (none chosen), or why none. */
  version: string;
  /** The version it booted before its last change, or null. */
  previous: string | null;
  /** An install's progress, or null. */
  install: string | null;
  /** The base its host last reported running, or that it did not report this boot. */
  running: string;
}

/** One row per Player: its version (the newest ready release when none is chosen), its previous, an
 * install's progress and what its host last reported running. */
export function playerRows(view: ReleasesView): PlayerVersionRow[] {
  return view.players.map((player) => {
    const next = bootsNext(player, view);
    return {
      serial: player.serial,
      name: player.name,
      playerId: player.player_id,
      version: next === null ? "No release is ready to boot" : player.chosen ? next : `${next} · newest ready`,
      previous: player.previous,
      install: player.install === null ? null : installWords(player.install, next ?? "a release"),
      running: player.running === null ? "Not reported on this boot" : `Base ${player.running}`,
    };
  });
}

/** Whether every Player Central knows boots `version` next, by its own choice (Update the wall done). */
export function wallOn(view: ReleasesView, version: ReleaseTag): boolean {
  return view.players.length > 0 && view.players.every((player) => player.chosen && player.version === version);
}

/** The one version every Player booted before (Undo), or null when they differ or none has one. */
export function wallPrevious(view: ReleasesView): ReleaseTag | null {
  const previous = new Set(view.players.map((player) => player.previous));
  if (previous.size !== 1) return null;
  const [only] = previous;
  return only ?? null;
}

/** The release Update the wall opens on: the default (newest ready stable), else the newest deployable stable. */
export function updateTarget(view: ReleasesView | null): ReleaseTag | null {
  if (view === null) return null;
  return view.default ?? view.releases.find((release) => release.stable && deployable(release))?.version ?? null;
}
