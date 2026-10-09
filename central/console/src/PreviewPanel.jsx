import React, { useEffect, useState } from "react";

import { FactLine } from "./domain/fact-line.tsx";
import {
  previewFacts,
  thumbnailPath,
  TILE_NOT_READY,
  TILE_RETRY_MS,
  tileWords,
} from "./sourcePreview.js";
import { DATES_NOTE } from "./timeWords.js";

/**
 * What the chosen criteria select (console DDD §37, §39, §40): one panel, mounted on the
 * Tags, Narrow and Review steps over the flow's one preview (usePreview.js). Its facts and
 * statements are sourcePreview.js `previewFacts`, rendered through FactLine; then the
 * newest members as tiles, read through Central (Players and this console never reach the
 * library: Central re-encodes each thumbnail from a live preview's member).
 *
 * `blocked` replaces the panel's content with the reason nothing can be previewed (an
 * unannounced connection, or none chosen yet).
 *
 * @param {{preview: import("./sourcePreview.js").Preview, connections: string[],
 *          blocked?: string|null}} props
 */
export function PreviewPanel({ preview, connections, blocked = null }) {
  const said = blocked === null ? previewFacts(preview, connections) : null;
  const shown = said?.answer?.shown ?? [];
  return (
    <section className="source-preview" aria-label="What this selects">
      <h3>What this selects from your library</h3>
      {blocked !== null ? <p>{blocked}</p> : (
        <div role="status" className="source-preview__answer">
          {said.facts.map((fact, index) => <FactLine key={`fact-${index}`} fact={fact} />)}
          {said.notes.map((note) => <p key={note}>{note}</p>)}
        </div>
      )}
      {blocked === null && shown.length > 0 && (
        <>
          <ul className="source-tiles" aria-label="Newest matches">
            {shown.map((member) => (
              <PreviewTile key={`${said.answer.observed_at}:${member.asset_id}`} member={member} />
            ))}
          </ul>
          <p className="source-preview__note">{DATES_NOTE}</p>
        </>
      )}
    </section>
  );
}

/**
 * One member's tile (§40): its thumbnail, retried once {@link TILE_RETRY_MS} after a miss
 * (busy, timeout, a failed fetch, or not servable: an `<img>` cannot tell them apart); a
 * second miss reads "Preview not ready yet". A new answer remounts the tile (its key), so
 * it tries again then.
 *
 * @param {{member: {asset_id: string, kind: string, captured_at: number,
 *          duration_seconds?: number|null}}} props
 */
function PreviewTile({ member }) {
  const [attempt, setAttempt] = useState(0);
  const [missed, setMissed] = useState(false);
  const words = tileWords(member);
  useEffect(() => {
    if (!missed || attempt > 0) return undefined;
    const timer = window.setTimeout(() => {
      setMissed(false);
      setAttempt(1);
    }, TILE_RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [missed, attempt]);
  const notReady = missed && attempt > 0;
  return (
    <li className="source-tile">
      {notReady ? (
        <span className="source-tile__missing" role="img" aria-label={`${words}: ${TILE_NOT_READY}`}>
          {TILE_NOT_READY}
        </span>
      ) : missed ? (
        <span className="source-tile__missing" role="img" aria-label={words} />
      ) : (
        <img
          src={thumbnailPath(member.asset_id, attempt)}
          alt={words}
          loading="lazy"
          width="96"
          height="96"
          onError={() => setMissed(true)}
        />
      )}
    </li>
  );
}
