import React from "react";

import { Field } from "./Field.jsx";
import { sourceName } from "./sourceNames.js";

/**
 * The one Source picker (pass 2 slice 3 §3), shared by both authoring modes.
 * Its accessible name is "Source". Operators see a plain Source name; option values
 * remain exact stored refs for Scene authoring and candidate reads.
 *
 * `label` names it ("Source", or "Add another Source" beside the ones chosen); `exclude`
 * leaves out the Sources already chosen.
 *
 * @param {{id: string, reason?: string|null, sources: Array<{source_ref: string}>,
 *          value: string, onChange: (sourceRef: string) => void, label?: string,
 *          exclude?: ReadonlyArray<string>}} props
 */
export function SourcePicker({ id, reason = null, sources, value, onChange, label = "Source", exclude = [] }) {
  const selectedIsCurrent = sources.some((source) => source.source_ref === value);
  const offered = sources.filter((source) => source.source_ref === value || !exclude.includes(source.source_ref));
  return (
    <Field id={id} label={label} reason={reason}>
      {(props) => (
        <select {...props} value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">Choose a Source</option>
          {value && !selectedIsCurrent && (
            <option value={value}>{`Previous selection (${sourceName(value)})`}</option>
          )}
          {offered.map((source) => (
            <option key={source.source_ref} value={source.source_ref}>
              {sourceName(source)}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}
