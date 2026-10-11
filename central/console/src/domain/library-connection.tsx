import { useState } from "react";

import { FactRow, Note } from "../patterns/fact-row";
import { HealthBadge } from "../patterns/health-badge";
import { Button } from "../ui/button";
import { Dialog, DialogClose } from "../ui/dialog";
import { Field, TextInput } from "../ui/field";
import { Section } from "../ui/section";
import { Inline, Stack } from "../ui/stack";
import {
  LibraryHealthCard,
  type LibraryConnectionView,
  type LibraryFix,
  libraryCountsWords,
  libraryVerdict,
  noteWords,
  problemWords,
  waitingForKey,
} from "./library-health-card";

/*
 * One Immich connection on Settings › Photo library (design-language §8 row A3, T8; console
 * design #8-#12): its address, the key's last four characters, the Immich user, Immich's own
 * counts, its notes, its health with the one fix, Check now, Change, Paste a new key and Remove.
 * The key is typed once into a password field and sent; nothing here ever holds it after that.
 */

/** What a connection write asks for; `key` only when one is pasted. */
export interface ConnectionWrite { name: string; url: string; key?: string }

/** A write's outcome: null when it was taken, else its refusal in words. */
export type WriteOutcome = string | null;

/** A refusal of the connection routes (central/photo_library_routes.py) in words. */
export function connectionRefusalWords(code: string): string {
  switch (code) {
    case "connection_name_exists": return "A connection with this name exists.";
    case "invalid_url": return "Type the Immich address, for example http://immich.local:2283.";
    case "key_required": return "Paste the API key as well.";
    case "sources_use_connection": return "Photo sources use this connection. Change or delete them first.";
    case "connection_unknown": return "This connection was removed meanwhile.";
    case "unreachable": return "Photo Wall did not answer. Try again.";
    default: return `Photo Wall did not take this (${code}).`;
  }
}

export interface ConnectionFormProps {
  /** The form's accessible name. */
  label: string;
  /** What the form starts with: a new connection starts empty. */
  initial: { name: string; url: string };
  /** Which fields it asks: a name and address, a key, or both. */
  asks: { name: boolean; url: boolean; key: boolean };
  submitLabel: string;
  onSubmit: (write: ConnectionWrite) => Promise<WriteOutcome>;
  onCancel?: () => void;
}

/** ConnectionForm: the name, the Immich address and the API key (a password field). */
export function ConnectionForm({ label, initial, asks, submitLabel, onSubmit, onCancel }: ConnectionFormProps) {
  const [name, setName] = useState(initial.name);
  const [url, setUrl] = useState(initial.url);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const send = async () => {
    setBusy(true);
    setRefused(null);
    const outcome = await onSubmit({ name: name.trim(), url: url.trim(), ...(key.trim() ? { key: key.trim() } : {}) });
    // The key is sent once and never kept: the field empties whatever the answer.
    setKey("");
    setBusy(false);
    setRefused(outcome);
  };
  return (
    <form aria-label={label} onSubmit={(event) => { event.preventDefault(); void send(); }}>
      <Stack>
        {asks.name && (
          <Field label="Name" help="How Photo Wall names this Immich, for example Family.">
            <TextInput value={name} onChange={setName} disabled={busy} />
          </Field>
        )}
        {asks.url && (
          <Field label="Immich address" help="The address you open Immich at, for example http://immich.local:2283.">
            <TextInput value={url} onChange={setUrl} type="url" width="long" disabled={busy} />
          </Field>
        )}
        {asks.key && (
          <Field label="API key" help="Make one in Immich › Account settings › API keys. Photo Wall keeps it and never shows it again.">
            <TextInput value={key} onChange={setKey} type="password" width="long" disabled={busy} />
          </Field>
        )}
        {refused !== null && <Note live>{refused}</Note>}
        <Inline>
          <Button type="submit" variant="primary" disabled={busy}>{busy ? "Saving…" : submitLabel}</Button>
          {onCancel && <Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>}
        </Inline>
      </Stack>
    </form>
  );
}

export interface LibraryConnectionProps {
  connection: LibraryConnectionView;
  onSave: (write: ConnectionWrite) => Promise<WriteOutcome>;
  onCheck: () => void;
  /** Check now's receipt, until the row's health answers it; null when none is waiting. */
  checkNote: string | null;
  onRemove: () => Promise<WriteOutcome>;
}

type Editing = "none" | "change" | "key";

/** LibraryConnection: one connection's section on Settings › Photo library. */
export function LibraryConnection({ connection, onSave, onCheck, checkNote, onRemove }: LibraryConnectionProps) {
  const [editing, setEditing] = useState<Editing>("none");
  const [removing, setRemoving] = useState(false);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeRefused, setRemoveRefused] = useState<string | null>(null);
  const waiting = waitingForKey(connection);
  const save = async (write: ConnectionWrite) => {
    const outcome = await onSave(write);
    if (outcome === null) setEditing("none");
    return outcome;
  };
  const fix = (chosen: LibraryFix) => (chosen === "paste_key" ? setEditing("key") : onCheck());
  // A connection that needs a look shows its ProblemCard, whose chip and one button are its
  // status and its fix: neither is repeated beside it.
  const { health } = connection;
  const problemFix = health.state === "needs_a_look" ? problemWords(connection, health.problem).fix : null;
  const remove = async () => {
    setRemoveBusy(true);
    const outcome = await onRemove();
    setRemoveBusy(false);
    if (outcome === null) {
      setRemoving(false);
    } else {
      setRemoving(false);
      setRemoveRefused(outcome);
    }
  };
  return (
    <Section title={connection.name} heading="name">
      <Stack>
        {problemFix === null && <HealthBadge verdict={libraryVerdict(connection)} />}
        {waiting ? (
          <>
            <Note>Paste the address and key of this Immich to start new photos again.</Note>
            <ConnectionForm
              label={`Connect ${connection.name}`}
              initial={{ name: connection.name, url: "" }}
              asks={{ name: false, url: true, key: true }}
              submitLabel="Save"
              onSubmit={save}
            />
          </>
        ) : (
          <div>
            <FactRow label="Immich address" tone="set">{connection.url}</FactRow>
            {connection.key_hint !== null && <FactRow label="API key" tone="set">{`Key ending ${connection.key_hint}`}</FactRow>}
            {connection.immich_user !== null && (
              <FactRow label="Immich user" tone="reported">{`${connection.immich_user.name} (${connection.immich_user.email})`}</FactRow>
            )}
            {connection.counts !== null && <FactRow label="In Immich" tone="reported">{libraryCountsWords(connection.counts)}</FactRow>}
            {connection.health.state === "ok" && connection.health.notes.map((note) => (
              <Note key={note.kind}>{noteWords(note)}</Note>
            ))}
          </div>
        )}
        <LibraryHealthCard connection={connection} onFix={fix} />
        {checkNote !== null && <Note live>{checkNote}</Note>}
        {removeRefused !== null && <Note live>{removeRefused}</Note>}
        {!waiting && editing === "change" && (
          <ConnectionForm
            label={`Change ${connection.name}`}
            initial={{ name: connection.name, url: connection.url ?? "" }}
            asks={{ name: true, url: true, key: false }}
            submitLabel="Save"
            onSubmit={save}
            onCancel={() => setEditing("none")}
          />
        )}
        {!waiting && editing === "key" && (
          <ConnectionForm
            label={`New key for ${connection.name}`}
            initial={{ name: connection.name, url: connection.url ?? "" }}
            asks={{ name: false, url: false, key: true }}
            submitLabel="Save key"
            onSubmit={save}
            onCancel={() => setEditing("none")}
          />
        )}
        <Inline label={`${connection.name} actions`}>
          {!waiting && problemFix !== "check_again" && <Button onClick={onCheck}>Check now</Button>}
          {!waiting && editing === "none" && <Button variant="ghost" onClick={() => setEditing("change")}>Change name or address</Button>}
          {!waiting && editing === "none" && problemFix !== "paste_key" && (
            <Button variant="ghost" onClick={() => setEditing("key")}>Paste a new key</Button>
          )}
          <Button variant="ghost" onClick={() => { setRemoveRefused(null); setRemoving(true); }}>Remove</Button>
        </Inline>
      </Stack>
      <Dialog
        open={removing}
        onOpenChange={setRemoving}
        busy={removeBusy}
        title={`Remove ${connection.name}?`}
        actions={(
          <>
            <DialogClose>Cancel</DialogClose>
            <Button variant="danger" disabled={removeBusy} onClick={() => void remove()}>Confirm remove</Button>
          </>
        )}
      >
        <p>Photo Wall forgets this Immich and its key. Nothing in Immich changes.</p>
      </Dialog>
    </Section>
  );
}
