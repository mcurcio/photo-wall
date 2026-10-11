import { useState } from "react";

import {
  type ConnectionWrite,
  ConnectionForm,
  LibraryConnection,
  type WriteOutcome,
  connectionRefusalWords,
} from "../domain/library-connection";
import type { LibraryConnectionView } from "../domain/library-health-card";
import {
  addConnection,
  changeConnection,
  checkConnection,
  removeConnection,
  useLibraryConnections,
} from "../libraryApi.js";
import { EmptyState } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { Button } from "../ui/button";
import { Section } from "../ui/section";
import { Inline, Stack } from "../ui/stack";

/** How often the page re-reads the connections while it is shown: a check answers in seconds. */
const CADENCE_MS = 3000;

/** Check now's receipt for one connection: the health it was asked from, and its words. */
interface CheckReceipt { before: string; message: string }

const healthKey = (connection: LibraryConnectionView) => JSON.stringify(connection.health);

function outcome(result: { ok: boolean; error?: string }): WriteOutcome {
  return result.ok ? null : connectionRefusalWords(result.error ?? "unreachable");
}

/**
 * Settings › Photo library (`#/photo-library`; design-language T8, row A3; console design #8-#12):
 * connect Immich by pasting its address and an API key, with no file and no restart. Save checks
 * at once; each connection shows Immich's counts and its health, with the one fix when it needs a
 * look. Photos keep playing whatever the connection's health (C7).
 */
export function PhotoLibraryPage() {
  const read = useLibraryConnections({ cadenceMs: CADENCE_MS });
  const connections = read.connections as LibraryConnectionView[] | null;
  const [receipts, setReceipts] = useState<Record<string, CheckReceipt>>({});
  const [added, setAdded] = useState(0);
  // "Add a connection" opens the form; with no connection yet it is open from the start.
  const [adding, setAdding] = useState(false);

  // A receipt shows until the row's health differs from the one it was asked from.
  const checkNote = (connection: LibraryConnectionView): string | null => {
    const receipt = receipts[connection.connection_ref];
    return receipt !== undefined && receipt.before === healthKey(connection) ? receipt.message : null;
  };

  const add = async (write: ConnectionWrite) => {
    const result = await addConnection({ name: write.name, url: write.url, key: write.key ?? "" });
    if (result.ok) {
      setAdded((count) => count + 1);   // a fresh, empty form, closed again
      setAdding(false);
      await read.refresh();
    }
    return outcome(result);
  };

  const rowProps = (connection: LibraryConnectionView) => ({
    connection,
    checkNote: checkNote(connection),
    onSave: async (write: ConnectionWrite) => {
      const result = await changeConnection(connection.connection_ref, write);
      if (result.ok) await read.refresh();
      return outcome(result);
    },
    onCheck: () => {
      const before = healthKey(connection);
      void checkConnection(connection.connection_ref).then(async (result) => {
        setReceipts((all) => ({
          ...all,
          [connection.connection_ref]: {
            before,
            message: result.ok
              ? "Check requested. Photos keep playing while Photo Wall asks Immich."
              : `Check request failed: ${connectionRefusalWords(result.error ?? "unreachable")}`,
          },
        }));
        await read.refresh();
      });
    },
    onRemove: async () => {
      const result = await removeConnection(connection.connection_ref);
      if (result.ok) await read.refresh();
      return outcome(result);
    },
  });

  return (
    <Stack>
      <Note>Photo Wall reads your photos from Immich. It never uploads, edits or deletes anything there.</Note>
      {read.failed && <Note live>Photo Wall could not read the connections just now; it keeps trying.</Note>}
      {connections === null ? (
        <Note>Loading…</Note>
      ) : connections.length === 0 ? (
        <EmptyState>Connect your photo library: paste the address of your Immich and an API key.</EmptyState>
      ) : (
        connections.map((connection) => {
          const row = rowProps(connection);
          return (
            <LibraryConnection key={connection.connection_ref} connection={connection} checkNote={row.checkNote}
              onSave={row.onSave} onCheck={row.onCheck} onRemove={row.onRemove} />
          );
        })
      )}
      {adding || connections?.length === 0 ? (
        <Section title="New connection" heading="name">
          <ConnectionForm
            key={added}
            label="Add a connection"
            initial={{ name: "", url: "" }}
            asks={{ name: true, url: true, key: true }}
            submitLabel="Save connection"
            onSubmit={add}
            onCancel={connections?.length === 0 ? undefined : () => setAdding(false)}
          />
        </Section>
      ) : (
        <Inline><Button onClick={() => setAdding(true)}>Add a connection</Button></Inline>
      )}
    </Stack>
  );
}
