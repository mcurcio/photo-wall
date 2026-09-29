import React from "react";

import { Field } from "./Field.jsx";
import { sourceName } from "./sourceNames.js";

/**
 * The one Source picker (pass 2 slice 3 §3), shared by both authoring modes.
 * Its accessible name is "Source". Operators see a plain Source name; option values
 * remain exact stored refs for Scene authoring and candidate reads.
 *
 * @param {{id: string, reason?: string|null, sources: Array<{source_ref: string}>,
 *          value: string, onChange: (sourceRef: string) => void}} props
 */
export function SourcePicker({ id, reason = null, sources, value, onChange }) {
  const selectedIsCurrent = sources.some((source) => source.source_ref === value);
  return (
    <Field id={id} label="Source" reason={reason}>
      {(props) => (
        <select {...props} value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">Choose a Source</option>
          {value && !selectedIsCurrent && (
            <option value={value}>{`Previous selection (${sourceName(value)})`}</option>
          )}
          {sources.map((source) => (
            <option key={source.source_ref} value={source.source_ref}>
              {sourceName(source)}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}
