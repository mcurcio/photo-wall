import * as React from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";
import { Field, TextInput } from "../ui/field";

/** What a search answers: the items found, and a line about the list itself (its age, a failure). */
export interface PickerAnswer<T> {
  items: readonly T[];
  note: string | null;
}

export interface ItemPickerProps<T> {
  /** The picker's label ("Albums"), its field's and its chosen list's name. */
  label: string;
  /** One plain sentence under the field. */
  help?: string;
  /** Items matching `q` (the empty text lists the first few). */
  search: (q: string) => Promise<PickerAnswer<T>>;
  /** The named items, for chosen keys this picker has not seen in a search yet. */
  byId?: (keys: readonly string[]) => Promise<readonly T[]>;
  /** The chosen keys, in order. */
  value: readonly string[];
  onChange: (keys: string[], chosen: T[]) => void;
  keyOf: (item: T) => string;
  render: (item: T) => string;
  /** At most this many may be chosen: 1 makes it a single choice. */
  max?: number;
  /** A line under the chosen items (a rule about choosing several). */
  chosenNote?: string | null;
  disabled?: boolean;
}

const SEARCH_DELAY_MS = 250;

/**
 * ItemPicker (design language §4, roadmap 4): one combobox for choosing library items (albums,
 * people, tags, a place) by typing part of a name; the chosen items are listed under it, each
 * with Remove. A chosen item whose name is not known yet is named once `byId` answers; one the
 * library no longer has reads "Deleted in Immich".
 */
export function ItemPicker<T>({
  label, help, search, byId, value, onChange, keyOf, render, max, chosenNote = null, disabled = false,
}: ItemPickerProps<T>) {
  const listId = React.useId();
  const [text, setText] = React.useState("");
  const [open, setOpen] = React.useState(false);
  const [answer, setAnswer] = React.useState<PickerAnswer<T> | null>(null);
  const [active, setActive] = React.useState(0);
  const [known, setKnown] = React.useState<ReadonlyMap<string, T>>(new Map());
  const [missing, setMissing] = React.useState<ReadonlySet<string>>(new Set());

  const learn = React.useCallback((items: readonly T[]) => {
    setKnown((current) => {
      const next = new Map(current);
      for (const item of items) next.set(keyOf(item), item);
      return next;
    });
  }, [keyOf]);

  // Search as the text changes, while the list is open; the latest answer wins.
  React.useEffect(() => {
    if (!open) return undefined;
    let current = true;
    const timer = setTimeout(() => {
      search(text.trim()).then((found) => {
        if (!current) return;
        learn(found.items);
        setAnswer(found);
        setActive(0);
      }, () => {});
    }, SEARCH_DELAY_MS);
    return () => { current = false; clearTimeout(timer); };
  }, [open, text, search, learn]);

  // Name the chosen keys no search has named.
  const unnamed = value.filter((key) => !known.has(key) && !missing.has(key));
  const unnamedKey = unnamed.join("\n");
  React.useEffect(() => {
    if (byId === undefined || unnamedKey === "") return undefined;
    let current = true;
    const keys = unnamedKey.split("\n");
    byId(keys).then((items) => {
      if (!current) return;
      learn(items);
      const found = new Set(items.map(keyOf));
      setMissing((was) => new Set([...was, ...keys.filter((key) => !found.has(key))]));
    }, () => {});
    return () => { current = false; };
  }, [unnamedKey, byId, keyOf, learn]);

  const offered = (answer?.items ?? []).filter((item) => !value.includes(keyOf(item)));
  const full = max !== undefined && value.length >= max;
  const chosenItems = (keys: readonly string[]) =>
    keys.map((key) => known.get(key)).filter((item): item is T => item !== undefined);

  const choose = (item: T) => {
    const key = keyOf(item);
    const keys = max === 1 ? [key] : [...value, key];
    onChange(keys, chosenItems(keys.filter((k) => k !== key)).concat(item));
    setText("");
    setOpen(max !== 1 && !(max !== undefined && keys.length >= max));
  };
  const remove = (key: string) => {
    const keys = value.filter((k) => k !== key);
    onChange(keys, chosenItems(keys));
  };
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActive((index) => Math.min(index + 1, Math.max(0, offered.length - 1)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => Math.max(0, index - 1));
    } else if (event.key === "Enter" && open && offered[active] !== undefined) {
      event.preventDefault();
      choose(offered[active]);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  };
  const optionId = (index: number) => `${listId}-${index}`;
  const shown = open && !full;

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Field label={label} help={help}>
        <TextInput
          value={text}
          onChange={(next) => { setText(next); setOpen(true); }}
          type="search"
          width="long"
          placeholder={full ? (max === 1 ? "Remove the chosen one to choose another" : "As many as can be chosen") : "Type to search"}
          disabled={disabled || full}
          combobox={{ listId, expanded: shown && offered.length > 0, activeId: shown && offered.length > 0 ? optionId(active) : undefined }}
          onKeyDown={onKeyDown}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
        />
      </Field>
      {shown && (
        <div className="flex min-w-0 flex-col gap-1">
          <ul
            id={listId}
            role="listbox"
            aria-label={`${label} found`}
            className="m-0 flex max-h-60 min-w-0 list-none flex-col overflow-y-auto rounded-input border border-line bg-surface-raised p-1"
          >
            {offered.map((item, index) => (
              <li
                key={keyOf(item)}
                id={optionId(index)}
                role="option"
                aria-selected={index === active}
                className={cn(
                  "cursor-pointer rounded-input px-2 py-1 text-sm wrap-anywhere text-text",
                  index === active && "bg-accent-tint text-accent",
                )}
                onMouseDown={(event) => { event.preventDefault(); choose(item); }}
              >
                {render(item)}
              </li>
            ))}
            {answer !== null && offered.length === 0 && (
              <li role="option" aria-selected={false} aria-disabled className="px-2 py-1 text-sm text-muted">
                {text.trim() === "" ? "Nothing to choose" : "Nothing matches"}
              </li>
            )}
          </ul>
          {answer?.note ? <span className="text-xs text-muted">{answer.note}</span> : null}
        </div>
      )}
      {value.length > 0 && (
        <ul aria-label={`Chosen ${label.toLowerCase()}`} className="m-0 flex min-w-0 list-none flex-wrap gap-2 p-0">
          {value.map((key) => {
            const item = known.get(key);
            const name = item !== undefined ? render(item) : missing.has(key) ? "Deleted in Immich" : "Naming…";
            return (
              <li key={key} className="flex items-center gap-1 rounded-pill border border-line bg-surface-sunken py-0.5 pr-1 pl-3 text-sm text-text">
                <span className="wrap-anywhere">{name}</span>
                <Button variant="ghost" className="px-2 py-0.5" aria-label={`Remove ${name}`} disabled={disabled} onClick={() => remove(key)}>
                  ×
                </Button>
              </li>
            );
          })}
        </ul>
      )}
      {value.length > 1 && chosenNote ? <span className="text-xs text-muted">{chosenNote}</span> : null}
    </div>
  );
}
