import React from "react";

import { Field } from "./Field.jsx";

/**
 * The one Source picker (pass 2 slice 3 §3), shared by both authoring modes.
 * Its accessible name is "Source". A Source is a saved live query named
 * `name:rev` (design D-e); the option value is that ref.
 *
 * @param {{id: string, reason?: string|null, sources: Array<{source_ref: string}>,
 *          value: string, onChange: (sourceRef: string) => void}} props
 */
export function SourcePicker({ id, reason = null, sources, value, onChange }) {
  return (
    <Field id={id} label="Source" reason={reason}>
      {(props) => (
        <select {...props} value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">Choose a Source</option>
          {sources.map((source) => (
            <option key={source.source_ref} value={source.source_ref}>
              {source.source_ref}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}
