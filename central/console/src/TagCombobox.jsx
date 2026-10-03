import React, { useEffect, useRef, useState } from "react";

import { fact } from "./facts.js";
import { FactLine } from "./FactLine.jsx";
import { Field } from "./Field.jsx";
import {
  chooseTag, MAX_TAGS, pickerAnnouncement, readTags, tagListFact, TAGS_PENDING, TAGS_UNREAD,
} from "./libraryTags.js";
import { tagWords } from "./sourceWords.js";
import { KEY_NOT_ALLOWED } from "./sourcePreview.js";

const UNREAD = fact({ kind: "unknown", why: TAGS_UNREAD });

/** Typing waits this long for the next key before it reads (PR 37 §8). */
const TYPING_MS = 150;

/**
 * The tag picker (console DDD §37; PR 37 §8): a WAI-ARIA combobox over the tags the media
 * worker last listed for `connection` (`GET /v1/operator/library/tags`, at most 20 per
 * read), with up to {@link MAX_TAGS} chosen tags as chips. A Source needs ALL its tags, and
 * each includes its nested tags (libraryTags.js `chooseTag`): a nested tag replaces its
 * ancestor, and an ancestor of a chosen tag is refused with its reason. There is no any-of
 * switch; the hint says how to get one in the library.
 *
 * Reads are dropped by sequence: only the newest read's answer is shown. Every answer
 * teaches the flow its tags' paths (`onLearn`); only a lookup by id says a saved tag is gone.
 * The list's age shows only when it is older than 5 min.
 *
 * @param {{id: string, connection: string, value: string[],
 *          paths: Readonly<Record<string, string|null>>,
 *          onChange: (tags: string[]) => void,
 *          onLearn: (list: object) => void}} props
 */
export function TagCombobox({ id, connection, value, paths, onChange, onLearn }) {
  const [query, setQuery] = useState("");
  const [list, setList] = useState(/** @type {object|null} */ (null));
  const [failed, setFailed] = useState(/** @type {string|null} */ (null));
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [said, setSaid] = useState(/** @type {string|null} */ (null));
  const sequence = useRef(0);
  const learn = useRef(onLearn);
  learn.current = onLearn;

  useEffect(() => {
    const mine = ++sequence.current;
    const timer = window.setTimeout(async () => {
      const answer = await readTags(connection, query);
      if (sequence.current !== mine) return; // a newer read was asked for
      if (answer?.failed) {
        setFailed(answer.failed);
        return;
      }
      setFailed(null);
      setList(answer);
      setActive(-1);
      learn.current(answer);
    }, query === "" ? 0 : TYPING_MS);
    return () => window.clearTimeout(timer);
  }, [connection, query]);

  const options = list?.tags ?? [];
  const listId = `${id}-tags`;
  const optionId = (index) => `${id}-tag-${index}`;
  const expanded = open && options.length > 0;

  const pick = (tag) => {
    const known = { ...paths, [tag.tag_ref]: tag.path };
    const next = chooseTag(value, tag, known);
    setQuery("");
    setOpen(false);
    if (next.refused !== null) {
      setSaid(next.refused);
      return;
    }
    if (next.chosen !== value) onChange(next.chosen);
    setSaid(next.replaced.length > 0
      ? `${tag.path} replaced ${next.replaced.map((ref) => tagWords(ref, known)).join(" and ")}: ` +
        "it is nested under it, so it is the narrower choice."
      : null);
  };

  const onKeyDown = (event) => {
    const last = options.length - 1;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActive((index) => (index >= last ? 0 : index + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      setActive((index) => (index <= 0 ? last : index - 1));
    } else if (expanded && event.key === "Home") {
      event.preventDefault();
      setActive(0);
    } else if (expanded && event.key === "End") {
      event.preventDefault();
      setActive(last);
    } else if (event.key === "Enter") {
      event.preventDefault(); // never the form's Continue
      if (expanded && active >= 0 && active <= last) pick(options[active]);
    } else if (event.key === "Escape") {
      setOpen(false);
      setActive(-1);
    }
  };

  const age = tagListFact(list);
  return (
    <div className="tag-picker">
      <Field
        id={id}
        label="Tags in your library"
        hint={<>Each tag includes everything nested under it. For media with <em>any</em> of several tags,
          give those photos one shared tag in your library.</>}
      >
        {(props) => (
          <input
            {...props}
            type="text"
            role="combobox"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            aria-autocomplete="list"
            aria-expanded={expanded}
            aria-controls={listId}
            aria-activedescendant={expanded && active >= 0 ? optionId(active) : undefined}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setOpen(true);
              setSaid(null);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setOpen(false)}
            onKeyDown={onKeyDown}
          />
        )}
      </Field>
      <ul id={listId} role="listbox" aria-label="Tags in your library" className="tag-picker__options"
        hidden={!expanded}>
        {options.map((tag, index) => (
          <li
            key={tag.tag_ref}
            id={optionId(index)}
            role="option"
            aria-selected={value.includes(tag.tag_ref)}
            className={`tag-picker__option${index === active ? " tag-picker__option--active" : ""}`}
            onMouseDown={(event) => event.preventDefault()} // keep focus in the input
            onClick={() => pick(tag)}
          >
            {tag.path}
          </li>
        ))}
      </ul>
      <p className="visually-hidden" aria-live="polite">
        {open && query !== "" ? pickerAnnouncement(list, failed) : ""}
      </p>
      {said !== null && <p role="status" className="tag-picker__said">{said}</p>}
      {value.length > 0 && (
        <ul className="tag-picker__chips"
          aria-label={value.length > 1 ? "Media with all of these tags" : "Chosen tag"}>
          {value.map((ref) => (
            <li key={ref} className="tag-picker__chip">
              <span>{tagWords(ref, paths)}</span>
              <button type="button" aria-label={`Remove tag ${tagWords(ref, paths)}`}
                onClick={() => {
                  onChange(value.filter((other) => other !== ref));
                  setSaid(null);
                }}>
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {list?.status === "pending" && <p>{TAGS_PENDING}</p>}
      {list?.status === "permission" && <p>{KEY_NOT_ALLOWED}</p>}
      {age !== null && <FactLine fact={age} />}
      {failed !== null && list === null && (
        <FactLine fact={UNREAD} />
      )}
    </div>
  );
}
