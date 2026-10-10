import { useEffect, useState } from "react";

import { readTags } from "./libraryTags.js";

/**
 * Each connection's stored tag-list status (`library_tags.status`: "ok", "pending",
 * "permission", …), read once per set of connections from the one stored row
 * (`GET /v1/operator/library/tags`), never kept anywhere else. A failed read names no
 * status, so nothing is said about it.
 *
 * @param {ReadonlyArray<string>} connections the connections to read; empty reads nothing
 * @returns {Readonly<Record<string, string>>}
 */
export function useTagListStatus(connections) {
  const [statuses, setStatuses] = useState({});
  const key = JSON.stringify([...new Set(connections)].filter(Boolean).sort());
  useEffect(() => {
    let current = true;
    for (const connection of JSON.parse(key)) {
      readTags(connection).then((list) => {
        if (current && typeof list?.status === "string") {
          setStatuses((known) => ({ ...known, [connection]: list.status }));
        }
      });
    }
    return () => { current = false; };
  }, [key]);
  return statuses;
}
