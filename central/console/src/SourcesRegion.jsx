import React from "react";

import { SourceFlow } from "./SourceFlow.jsx";

/**
 * The Sources region, the Photo sources section's page (flow design §6, §7 J5): the
 * intro, then the Source cards and the Source flow (SourceFlow.jsx).
 *
 * A Source is a saved live QUERY named `name:rev` (design D-e): Photo Wall selects
 * media that lives in the photo library and never uploads, edits or deletes anything
 * there. Nothing here names a vendor, an album, or anything a Player browses or opens.
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          markDraft: (section: string, dirty: boolean) => void,
 *          handOffs: ReturnType<typeof import("./flow/useHandOff.js").useHandOff>}} props
 */
export function SourcesRegion({ snapshot, route, navigate, markDraft, handOffs }) {
  return (
    <section className="showrunner__region" role="region" aria-label="Sources">
      <h2 className="showrunner__region-title">Sources</h2>
      <p className="showrunner__region-note">
        Photo Wall selects media that lives in your photo library. It never uploads, edits or
        deletes anything there.
      </p>
      <SourceFlow
        snapshot={snapshot}
        route={route}
        navigate={navigate}
        markDraft={markDraft}
        handOffs={handOffs}
      />
    </section>
  );
}
