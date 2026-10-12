import type * as React from "react";

import { cn } from "../ui/cn";
import { Link } from "../ui/link";
import { Menu, type MenuProps } from "../ui/menu";
import { Split } from "../ui/split";
import { HealthBadge, type Verdict } from "./health-badge";

/** One photo of a card's mosaic: Central's thumbnail and what it shows, in words. */
export interface MosaicPhoto { key: string; src: string; alt: string }

/** A card that can be chosen: a checkbox (any of several) or a radio (one of them). */
export interface CardChoice {
  type: "checkbox" | "radio";
  /** The radio group's name; unused for a checkbox. */
  name?: string;
  checked: boolean;
  /** The chosen thing's key, as the form reads it. */
  value: string;
  onChange: (checked: boolean) => void;
  /** The control's accessible name ("Choose garden"). */
  label: string;
}

export interface MediaCardProps {
  /** The card's name: its heading and its article's accessible name. */
  title: string;
  /** Where the title leads (the thing's editor); without it the title is plain text. */
  href?: string;
  /** Up to four photos of it; none draws an empty well with `emptyText`. */
  mosaic: readonly MosaicPhoto[];
  emptyText?: string;
  /** Its count line ("2,301 photos · 14 videos"). */
  line: string;
  /** What it holds, in one line ("Anna and Ben · last 3 years · no screenshots"). */
  detail?: string;
  /** Its status, drawn by the one chip; status colour stays inside it. */
  verdict?: Verdict | null;
  /** A receipt or a note under the line, announced when it changes. */
  note?: React.ReactNode;
  /** Its overflow actions. */
  menu?: MenuProps;
  /** The card is a choice in a form. */
  choice?: CardChoice;
  /** A line under the card, outside its choice (how its photos fit the Frames). */
  footer?: React.ReactNode;
}

/**
 * MediaCard (design language §4, T2's card): a thing made of photos (a Photo source) as a card:
 * a four-photo mosaic, its name, its count, its status chip and its overflow actions; or, in a
 * form, a choice with a checkbox or a radio. Immich's album card is the model.
 */
export function MediaCard({ title, href, mosaic, emptyText = "No photos yet", line, detail, verdict = null, note, menu, choice, footer }: MediaCardProps) {
  const shown = mosaic.slice(0, 4);
  const content = (
    <>
      <div
        className={cn(
          "grid aspect-video min-w-0 gap-0.5 overflow-hidden rounded-input bg-surface-sunken",
          shown.length > 1 ? "grid-cols-2" : "grid-cols-1",
        )}
      >
        {shown.length === 0 ? (
          <span className="flex items-center justify-center p-2 text-xs text-muted">{emptyText}</span>
        ) : shown.map((photo) => (
          <img key={photo.key} src={photo.src} alt={photo.alt} loading="lazy"
            className={cn("size-full min-h-0 object-cover", shown.length === 3 && photo === shown[0] ? "row-span-2" : "")} />
        ))}
      </div>
      <Split align="start" action={(
        <>
          {choice ? (
            <input
              type={choice.type}
              name={choice.name}
              value={choice.value}
              checked={choice.checked}
              aria-label={choice.label}
              onChange={(event) => choice.onChange(event.target.checked)}
              className="mt-1 size-5 shrink-0 cursor-pointer accent-accent"
            />
          ) : null}
          {menu ? <Menu label={menu.label} items={menu.items} /> : null}
        </>
      )}>
        <div className="flex min-w-0 flex-col gap-1">
          <h3 className="m-0 text-base font-medium wrap-anywhere text-text">
            {href ? <Link href={href}>{title}</Link> : title}
          </h3>
          <span className="text-sm text-muted">{line}</span>
          {detail ? <span className="text-sm wrap-anywhere text-text">{detail}</span> : null}
          {verdict !== null && <HealthBadge verdict={verdict} />}
          {note ? <div role="status" className="text-sm text-muted">{note}</div> : null}
        </div>
      </Split>
    </>
  );
  const frame = cn(
    "flex min-w-0 flex-col gap-2 rounded-card border bg-surface-raised p-3",
    choice?.checked ? "border-accent" : "border-line",
  );
  return choice ? (
    <article aria-label={title} className={frame}>
      {/* The whole card chooses: a press anywhere on it toggles its control. */}
      <label className="flex min-w-0 cursor-pointer flex-col gap-2">{content}</label>
      {footer}
    </article>
  ) : (
    <article aria-label={title} className={frame}>{content}{footer}</article>
  );
}

/** MediaCardAdd: the last card of a list, which makes a new one ("New Photo source"). */
export function MediaCardAdd({ label, onAdd }: { label: string; onAdd: () => void }) {
  return (
    <button
      type="button"
      onClick={onAdd}
      className={
        "flex min-h-32 w-full cursor-pointer items-center justify-center rounded-card border border-dashed border-line-input "
        + "bg-transparent p-3 font-sans text-sm font-medium text-accent hover:bg-accent-tint "
        + "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      }
    >
      {`+ ${label}`}
    </button>
  );
}

/** MediaCardList: cards in a grid, one column on a phone, up to three on a wide screen. */
export function MediaCardList({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <ul aria-label={label} className="m-0 grid min-w-0 list-none grid-cols-1 gap-3 p-0 sm:grid-cols-2 xl:grid-cols-3">
      {children}
    </ul>
  );
}

/** MediaCardListItem: one card's place in a MediaCardList. */
export function MediaCardListItem({ children }: { children: React.ReactNode }) {
  return <li className="min-w-0">{children}</li>;
}
