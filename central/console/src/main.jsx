import React from "react";
import { createRoot } from "react-dom/client";

import App from "./App.jsx";
import { SnapshotProvider } from "./useSnapshot.js";

// Plane A (the read snapshot) is held once, near the top of the tree, by
// SnapshotProvider. Every region — and useMutate's post-write refresh — reads
// that one instance, so no consumer hand-rolls its own snapshot (design §4a).
createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <SnapshotProvider>
      <App />
    </SnapshotProvider>
  </React.StrictMode>,
);
