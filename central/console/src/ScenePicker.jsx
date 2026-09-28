import React from "react";

import { Field } from "./Field.jsx";

/**
 * The one Scene picker (pass 2 slice 3 §3), shared by the Programs form and
 * the activation form. The options are the stored Scenes by id (design J4:
 * the id is the tile; names are not stored).
 *
 * @param {{id: string, label: string, reason?: string|null,
 *          definitions: Record<string, {scene_id: string}>,
 *          value: string, onChange: (sceneId: string) => void}} props
 */
export function ScenePicker({ id, label, reason = null, definitions, value, onChange }) {
  const sceneIds = Object.keys(definitions ?? {}).sort();
  return (
    <Field id={id} label={label} reason={reason}>
      {(props) => (
        <select {...props} value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">Choose a Scene</option>
          {sceneIds.map((sceneId) => (
            <option key={sceneId} value={sceneId}>
              {sceneId}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}
