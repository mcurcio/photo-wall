import React, { useId } from "react";

import { SourceCard } from "./domain/source-card.tsx";
import { MediaCardAdd, MediaCardList, MediaCardListItem } from "./patterns/media-card.tsx";
import { sourceName } from "./sourceNames.js";

/**
 * The one Source picker (pass 2 slice 3 §3; follow-ups D1): the saved Photo sources as cards,
 * each with its first photos, its count and its check state, chosen by its checkbox (any of
 * several: the Scene plays the photos of all of them) or, with `single`, its radio (a
 * hand-picked Scene picks from one Source). "New Photo source" is the last card. Its group is
 * named "Photo sources"; each control is named "Choose <name>" and carries the Source's stored
 * ref as its value. A chosen ref no longer saved stays listed, chosen, until it is unchosen.
 *
 * @param {{id: string, reason?: string|null, sources: Array<{source_ref: string, check?: object|null}>,
 *          value: ReadonlyArray<string>, onChange: (sourceRefs: string[]) => void,
 *          single?: boolean, now?: number|null, onNewSource?: () => void}} props
 */
export function SourcePicker({ id, reason = null, sources, value, onChange, single = false, now = null, onNewSource }) {
  const name = useId();
  const reasonId = `${id}-reason`;
  const saved = new Set(sources.map((source) => source.source_ref));
  const gone = value.filter((ref) => !saved.has(ref)).map((ref) => ({ source_ref: ref, gone: true }));
  const choose = (ref, checked) => {
    if (single) onChange(checked ? [ref] : []);
    else onChange(checked ? [...value.filter((chosen) => chosen !== ref), ref] : value.filter((chosen) => chosen !== ref));
  };
  return (
    <div id={id} role="group" tabIndex={-1} aria-label="Photo sources"
      aria-describedby={reason ? reasonId : undefined}>
      {reason && <p id={reasonId} className="field__reason">{reason}</p>}
      <MediaCardList label="Saved Photo sources">
        {[...gone, ...sources].map((source) => {
          const label = sourceName(source);
          return (
            <MediaCardListItem key={source.source_ref}>
              <SourceCard
                source={source}
                name={source.gone ? `${label} (no longer saved)` : label}
                now={now}
                choice={{
                  type: single ? "radio" : "checkbox",
                  name,
                  checked: value.includes(source.source_ref),
                  value: source.source_ref,
                  label: `Choose ${label}`,
                  onChange: (checked) => choose(source.source_ref, checked),
                }}
              />
            </MediaCardListItem>
          );
        })}
        {onNewSource && (
          <MediaCardListItem>
            <MediaCardAdd label="New Photo source" onAdd={onNewSource} />
          </MediaCardListItem>
        )}
      </MediaCardList>
    </div>
  );
}
