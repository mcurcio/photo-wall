import React, { useMemo, useRef, useState } from "react";

import { useConfirm } from "./ConfirmAction.jsx";
import { installOnPlayer } from "./domain/releases/api";
import { FactLine } from "./domain/fact-line.tsx";
import {
  deployable, installWords, playerRows, readinessLine, refusalWords, releaseRows, updateTarget,
} from "./domain/releases/model";
import { clock, fact } from "./facts.js";
import { answerLost, sendCatalogCheck, useReleaseRead } from "./releases.js";
import { formatRoute } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";
import { UNKNOWN_MESSAGE } from "./sendOutcome.js";

/** "Release read as of <time>", whether its last refresh failed, or why it has no answer. */
function ReadLine({ releases }) {
  // role=status with aria-busy while a read is in flight: busy clears only once the read is
  // settled, matching the snapshot status in the shell.
  const status = { role: "status", "aria-label": "Release read", "aria-busy": releases.busy ? "true" : undefined };
  if (releases.read === null) {
    return releases.error === null
      ? <p className="player__read-time" {...status}>Release read: not read yet</p>
      : (
        <div className="player__read-time" {...status}>
          <FactLine label="Release read" fact={fact({ kind: "unknown", why: `Central did not answer (${releases.error.code})` })} />
        </div>
      );
  }
  return (
    <p className="player__read-time" {...status}>
      {typeof releases.readAt === "number" ? `Release read as of ${clock(releases.readAt)}` : "Release read time not served"}
      {releases.error !== null && ", refresh failed"}
    </p>
  );
}

/**
 * One Player's Install: a release Central may put on (newest first) and the button that opens
 * the one confirmation. Install changes this Player only (owner, 2026-10-16: "Updates can also be
 * triggered by the Player device and apply only to that device").
 */
function InstallControl({ read, row, onInstall }) {
  const choices = read.releases.filter(deployable);
  const [version, setVersion] = useState(() => updateTarget(read) ?? choices[0]?.version ?? "");
  if (choices.length === 0) return <p className="roster__note">No release can be installed yet.</p>;
  return (
    <p>
      <label>
        {`Release for ${row.name} `}
        <select value={version} onChange={(event) => setVersion(event.target.value)}>
          {choices.map((release) => <option key={release.version} value={release.version}>{release.version}</option>)}
        </select>
      </label>
      {" "}
      <button type="button" onClick={(event) => onInstall(event, row, version)}>{`Install on ${row.name}…`}</button>
    </p>
  );
}

/** Every Player with the release it boots next, its previous, an install's progress and what its host reported. */
function PlayersSection({ read, onInstall }) {
  const rows = playerRows(read);
  if (rows.length === 0) return <p className="roster__empty">Central knows no Player yet.</p>;
  return (
    <ul className="player__commands" aria-label="Players">
      {rows.map((row) => (
        <li key={row.serial} className="player__command" aria-label={row.name}>
          <p className="player__state">{`${row.name} · boots ${row.version}`}</p>
          {row.previous !== null && <p className="roster__note">{`Previous: ${row.previous}`}</p>}
          <p className="roster__note">{`Running: ${row.running}`}</p>
          {row.install !== null && <p className="player__state" role="status">{row.install}</p>}
          <InstallControl read={read} row={row} onInstall={onInstall} />
        </li>
      ))}
    </ul>
  );
}

/**
 * The releases Central observed: one row per version, newest first, each with its readiness and
 * the Players that boot it next, and one verb, "Put vX on the wall…", which opens Update the wall
 * for it (the one confirmation lives there). A Rejected release has none.
 */
function CatalogSection({ read, checking, onCheck }) {
  const rows = releaseRows(read);
  return (
    <>
      <button type="button" onClick={onCheck} disabled={checking}>
        {checking ? "Checking GitHub releases…" : "Check GitHub releases now"}
      </button>
      {rows.length === 0 ? <p className="roster__empty">GitHub releases has reported no node release yet.</p> : (
        <ul className="player__commands" aria-label="Release catalog">
          {rows.map((row) => (
            <li key={row.version} className="player__command" aria-label={`Release ${row.version}`}>
              <p>{`Release ${row.version}`}</p>
              {row.prerelease && <p className="roster__note">Pre-release: listed, not downloaded ahead</p>}
              {row.contents !== null && <p className="roster__note">{row.contents}</p>}
              <FactLine label="Download" suffix="not proof a Player holds it" fact={row.readiness === null
                ? fact({ kind: "unknown", why: "Central did not serve whether it is downloaded" })
                : fact({ kind: "set", value: row.readiness })} />
              {row.isDefault && <p className="roster__note">Newest ready: a Player with no version of its own boots it</p>}
              {row.bootedBy.length > 0 && <p className="player__state">{`Booted next by ${row.bootedBy.join(", ")}`}</p>}
              {row.deployable && (
                <a href={formatRoute({ section: "releases", flow: "update", id: row.version })}>
                  {`Put ${row.version} on the wall…`}
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * The release sections: one read feeds them all (releases.js `useReleaseRead`); Install sends
 * one request for one Player.
 */
function ReleaseRecords() {
  const releases = useReleaseRead();
  // This page's unanswered "Check GitHub releases now" (releases.js `HeldCheck`).
  const checkRef = useRef(false);
  const [checking, setChecking] = useState(false);
  const heldCheck = useMemo(() => ({
    get: () => checkRef.current,
    set: (value) => {
      checkRef.current = value;
      setChecking(value);
    },
  }), []);
  const { open, setStatus, confirmation } = useConfirm(null);

  const onInstall = (event, row, version) => {
    const release = releases.read?.releases.find((each) => each.version === version);
    if (release === undefined) {
      setStatus("Install unavailable: Central no longer lists this release.");
      return;
    }
    open(event, {
      key: `install-${row.serial}-${version}`,
      title: `Install ${version} on ${row.name}?`,
      body: (
        <>
          <p>{`Only ${row.name} changes: it restarts onto ${version}, and every Frame it drives is blank while it `
            + "restarts. Every other Player keeps its own version."}</p>
          <p>{readinessLine(release)}</p>
        </>
      ),
      confirmLabel: "Install and restart",
      run: async () => {
        const answer = await installOnPlayer(row.serial, { version });
        void releases.refresh();
        if (answer.ok) return { state: "done", message: `${row.name}: ${installWords(answer.started.install, version)}.` };
        return answerLost(answer.error) ? { state: "unknown", message: UNKNOWN_MESSAGE }
          : { state: "refused", message: refusalWords(answer.error) };
      },
    });
  };

  const onCheck = async () => {
    const outcome = await sendCatalogCheck(heldCheck);
    void releases.refresh();
    setStatus(outcome.message);
  };

  const { read, readAt } = releases;
  const target = updateTarget(read);
  return (
    <>
      <ReadLine releases={releases} />
      {target !== null && (
        <p>
          <a className="releases__update" href={formatRoute({ section: "releases", flow: "update", id: target })}>
            Update the wall…
          </a>
        </p>
      )}
      {confirmation("roster__status-line")}
      {read !== null && (
        <>
          <SectionBoundary title="Players" resetKey={readAt}>
            <PlayersSection read={read} onInstall={onInstall} />
          </SectionBoundary>
          <SectionBoundary title="Release catalog" resetKey={readAt}>
            <CatalogSection read={read} checking={checking} onCheck={onCheck} />
          </SectionBoundary>
        </>
      )}
    </>
  );
}

/**
 * Fleet › Releases: the releases Central observed and downloads by itself, and a version per
 * Player (Q2 A, 2026-10-16): each Player boots its own version, or the newest ready release when
 * it has none; Install puts a release on one Player, Update the wall on every Player.
 */
export function ReleasesPage() {
  return (
    <div className="player">
      <ReleaseRecords />
    </div>
  );
}
